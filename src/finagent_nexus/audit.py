"""Tamper-evident audit trail.

Regulators do not accept "the logs say so" when the logs are an append-only file
anyone can edit. Each event here carries the SHA-256 of the previous event, so
altering or deleting event *n* invalidates every hash from *n* onward and
:meth:`AuditTrail.verify` fails loudly.

This is not a blockchain and does not pretend to be one — it is a hash chain,
which is the cheapest mechanism that makes silent retroactive edits detectable.
Pair it with write-once storage (S3 Object Lock, a WORM volume) for the full
control.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

GENESIS_HASH = "0" * 64


def _canonical(payload: Any) -> str:
    """Stable JSON encoding — sorted keys, no incidental whitespace.

    Canonicalisation matters: ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` must hash
    identically, or the chain breaks on a dict-ordering change that means
    nothing.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class AuditEvent(BaseModel):
    seq: int
    ts: str
    correlation_id: str
    actor: str
    action: str
    status: str
    detail: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None
    usage: dict[str, Any] | None = None
    latency_ms: int | None = None
    prev_hash: str
    hash: str


class AuditTrail:
    """Sequential, hash-chained event recorder.

    Args:
        correlation_id: Ties every event of one run together. Generated if absent.
        directory: Where to append ``{correlation_id}.jsonl``. ``None`` keeps the
            trail in memory only — used by tests and dry runs.
    """

    def __init__(self, correlation_id: str | None = None, directory: Path | None = None):
        self.correlation_id = correlation_id or uuid.uuid4().hex
        self.directory = directory
        self._events: list[AuditEvent] = []
        self._last_hash = GENESIS_HASH
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            self.path: Path | None = directory / f"{self.correlation_id}.jsonl"
        else:
            self.path = None

    # -- recording --------------------------------------------------------- #
    def record(
        self,
        *,
        actor: str,
        action: str,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
        latency_ms: int | None = None,
    ) -> AuditEvent:
        """Append one event and return it."""
        seq = len(self._events)
        # Annotated rather than inferred: without it the value type widens to
        # `object` and the `**body` splat below cannot be checked against
        # AuditEvent's fields at all. The splat itself is deliberate — it makes
        # the hashed payload and the stored event one dict, so a field can never
        # be hashed but not recorded, or recorded but not hashed. That guarantee
        # is worth keeping visible to the type checker.
        body: dict[str, Any] = {
            "seq": seq,
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "correlation_id": self.correlation_id,
            "actor": actor,
            "action": action,
            "status": status,
            "detail": detail or {},
            "model": model,
            "usage": usage,
            "latency_ms": latency_ms,
            "prev_hash": self._last_hash,
        }
        event = AuditEvent(**body, hash=digest(body))
        self._events.append(event)
        self._last_hash = event.hash

        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(event.model_dump_json() + "\n")
        return event

    # -- inspection -------------------------------------------------------- #
    @property
    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def verify(self) -> bool:
        """Recompute the chain. ``False`` means an event was altered or removed."""
        prev = GENESIS_HASH
        for index, event in enumerate(self._events):
            body = event.model_dump(exclude={"hash"})
            if event.seq != index or event.prev_hash != prev:
                return False
            if digest(body) != event.hash:
                return False
            prev = event.hash
        return True

    def to_jsonl(self) -> str:
        return "\n".join(event.model_dump_json() for event in self._events)

    def summary(self) -> dict[str, Any]:
        """Compact roll-up for a run report."""
        total_in = sum((e.usage or {}).get("input_tokens", 0) for e in self._events)
        total_out = sum((e.usage or {}).get("output_tokens", 0) for e in self._events)
        cached = sum((e.usage or {}).get("cache_read_input_tokens", 0) for e in self._events)
        return {
            "correlation_id": self.correlation_id,
            "events": len(self._events),
            "chain_valid": self.verify(),
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cache_read_input_tokens": cached,
            "latency_ms": sum(e.latency_ms or 0 for e in self._events),
            "path": str(self.path) if self.path else None,
        }

    @classmethod
    def load(cls, path: Path) -> AuditTrail:
        """Rehydrate a trail from disk so an auditor can re-verify it offline."""
        trail = cls(correlation_id=path.stem, directory=None)
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    trail._events.append(AuditEvent.model_validate_json(line))
        if trail._events:
            trail._last_hash = trail._events[-1].hash
        return trail
