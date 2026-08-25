"""Runtime configuration.

Every knob that changes cost, latency, or governance posture is surfaced here
rather than scattered through agent code. Financial institutions need to be able
to answer "what model decided this, at what effort, under which revision cap?"
from a single object that is snapshotted into the audit trail.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

VALID_EFFORT = frozenset({"low", "medium", "high", "xhigh", "max"})

#: Orchestration and compliance default to the strongest available model.
#: Downgrading the ComplianceOfficer is a governance decision, not a cost decision.
DEFAULT_MODEL = "claude-opus-5"


@dataclass(frozen=True)
class Settings:
    """Immutable run configuration, snapshotted into the audit trail."""

    model: str = DEFAULT_MODEL
    analyst_model: str = DEFAULT_MODEL
    effort: str = "high"
    max_tokens: int = 16_000
    max_revisions: int = 2
    max_tool_iterations: int = 8
    audit_dir: Path | None = field(default=Path("./audit"))

    #: Whether the bundled :class:`SyntheticMarketData` fixture may be used as
    #: an implicit fallback. It fabricates deterministic prices from a hash of
    #: the symbol and must never inform live advice.
    #:
    #: **Defaults to False: synthetic data is opt-in, never inherited.** The
    #: alternative — defaulting to True so the repository runs out of the box —
    #: makes forgetting to inject a provider indistinguishable from a working
    #: deployment, because every other control still passes and the audit trail
    #: notarises the result. A caller that wants fabricated prices must say so,
    #: either here or by passing a provider explicitly.
    allow_synthetic_data: bool = False

    def __post_init__(self) -> None:
        if self.effort not in VALID_EFFORT:
            raise ValueError(
                f"FINAGENT_EFFORT={self.effort!r} is not one of {sorted(VALID_EFFORT)}"
            )
        if self.max_revisions < 0:
            raise ValueError("max_revisions must be >= 0")
        if self.max_tool_iterations < 1:
            raise ValueError("max_tool_iterations must be >= 1")

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the environment (see ``.env.example``)."""
        audit_dir = os.getenv("FINAGENT_AUDIT_DIR", "./audit").strip()
        return cls(
            model=os.getenv("FINAGENT_MODEL", DEFAULT_MODEL),
            analyst_model=os.getenv("FINAGENT_ANALYST_MODEL", DEFAULT_MODEL),
            effort=os.getenv("FINAGENT_EFFORT", "high").lower(),
            max_tokens=int(os.getenv("FINAGENT_MAX_TOKENS", "16000")),
            max_revisions=int(os.getenv("FINAGENT_MAX_REVISIONS", "2")),
            max_tool_iterations=int(os.getenv("FINAGENT_MAX_TOOL_ITERATIONS", "8")),
            audit_dir=Path(audit_dir) if audit_dir else None,
            allow_synthetic_data=os.getenv(
                "FINAGENT_ALLOW_SYNTHETIC_DATA", "false"
            ).strip().lower()
            in {"true", "1", "yes"},
        )

    def fingerprint(self) -> dict[str, object]:
        """Serialisable snapshot for the audit record."""
        data = asdict(self)
        data["audit_dir"] = str(self.audit_dir) if self.audit_dir else None
        return data
