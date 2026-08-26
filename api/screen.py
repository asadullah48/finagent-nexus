"""Deterministic compliance screening as an HTTP endpoint.

This exposes the *left-hand side of the determinism boundary* — the half of
FinAgent-Nexus that is settled by arithmetic in :mod:`finagent_nexus.checks`
rather than by model judgement. That choice is what makes this endpoint
deployable at all:

* **No model call, so no API key.** There is no credential in this deployment to
  leak and no per-request token cost to meter.
* **Milliseconds, not minutes.** The agent graph runs Opus with tool loops and
  up to two revisions; that does not belong behind an unauthenticated URL on a
  serverless timeout. Pure functions over a portfolio do.
* **Reproducible.** Same input, same verdict, forever — which is the property a
  regulator actually asks for, and one a visitor can verify by re-running it.

The qualitative half of the constitution (suitability, gharar, fair
presentation) is deliberately *not* served here. It needs a model, and a model
needs a key.

Reference data comes from ``SyntheticMarketData``, which fabricates deterministic
figures from a hash of the symbol. Every response says so in ``data_source``.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from typing import Any

# The package lives in src/ and is not pip-installed in the function bundle.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from pydantic import ValidationError

from finagent_nexus.checks import run_machine_checks
from finagent_nexus.constitution import applicable_principles, by_id
from finagent_nexus.state import (
    Allocation,
    ClientRequest,
    Mandate,
    Recommendation,
)
from finagent_nexus.tools.market_data import SyntheticMarketData
from finagent_nexus.verdict import aggregate_verdict

MAX_BODY_BYTES = 64 * 1024
MAX_ALLOCATIONS = 40

#: One revision of headroom is granted so the verdict distinguishes *remediable*
#: defects (REVISE) from genuinely unfixable ones (BLOCK). Screening with zero
#: headroom would collapse both into BLOCK and lose the distinction that makes
#: the output useful — the remediation text is the point.
SCREENING_REVISION_HEADROOM = 1

_PROVIDER = SyntheticMarketData()


class ScreenError(ValueError):
    """A caller-supplied payload that cannot be screened, with a usable reason."""


def _number(payload: dict[str, Any], key: str, default: float) -> float:
    """Read a finite float, or refuse the request.

    Python's ``json`` module accepts the non-standard ``NaN``, ``Infinity`` and
    ``-Infinity`` literals, so a caller can put a non-finite value into any
    numeric field. ``weight_pct`` happens to be caught by its ``ge``/``le``
    bounds — every comparison against NaN is False — but ``expected_return_pct``
    and ``expected_volatility_pct`` are unbounded and are read by no
    deterministic screen, so ``Infinity`` sails through and the endpoint returns
    a confident verdict having never remarked on it.

    This is deliberately handled as *input validation* and not as a new
    principle. A non-finite figure is not a portfolio that breaches a rule; it
    is a malformed request, and 400 is the honest answer. Adding a constitution
    principle to catch it would be a governance change — see docs/governance.md
    section 3 on who is allowed to make those.
    """
    raw = payload.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ScreenError(f"{key} must be a number.") from None
    if not math.isfinite(value):
        raise ScreenError(f"{key} must be a finite number; got {raw!r}.")
    return value


def _build_request(payload: dict[str, Any]) -> ClientRequest:
    mandate_raw = str(payload.get("mandate", "sharia")).strip().lower()
    try:
        mandate = Mandate(mandate_raw)
    except ValueError:
        valid = ", ".join(m.value for m in Mandate)
        raise ScreenError(f"Unknown mandate {mandate_raw!r}. Expected one of: {valid}.") from None

    horizon_raw = payload.get("horizon_years", 10)
    try:
        horizon = int(horizon_raw)
    except (TypeError, ValueError):
        raise ScreenError(f"horizon_years must be a whole number; got {horizon_raw!r}.") from None

    return ClientRequest(
        client_id=str(payload.get("client_id", "WEB-SCREENER")),
        objective=str(payload.get("objective", "Screen a candidate portfolio for compliance.")),
        capital_usd=_number(payload, "capital_usd", 1_000_000.0),
        horizon_years=horizon,
        risk_tolerance=payload.get("risk_tolerance", "balanced"),
        jurisdiction=str(payload.get("jurisdiction", "SA")).strip().upper(),
        mandate=mandate,
        constraints=list(payload.get("constraints", [])),
    )


def _build_recommendation(payload: dict[str, Any]) -> Recommendation:
    raw = payload.get("allocations")
    if not isinstance(raw, list) or not raw:
        raise ScreenError("Provide a non-empty 'allocations' array.")
    if len(raw) > MAX_ALLOCATIONS:
        raise ScreenError(f"At most {MAX_ALLOCATIONS} allocations per request.")

    allocations: list[Allocation] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScreenError(f"allocations[{index}] must be an object.")
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            raise ScreenError(f"allocations[{index}] is missing 'symbol'.")
        weight = _number(item, "weight_pct", 0.0)
        if not 0.0 <= weight <= 100.0:
            # Caught here rather than left to the model's field bounds, so the
            # caller is told which allocation is wrong instead of receiving a
            # pydantic dump for the whole payload.
            raise ScreenError(
                f"allocations[{index}].weight_pct must be between 0 and 100; got {weight}."
            )
        allocations.append(
            Allocation(
                symbol=symbol,
                weight_pct=weight,
                rationale=str(item.get("rationale", "Supplied by the caller.")),
            )
        )

    # The disclosure and guarantee-language screens read this prose, so it is
    # part of what gets checked — not decoration.
    return Recommendation(
        summary=str(payload.get("summary", "Candidate portfolio submitted for screening.")),
        allocations=allocations,
        expected_return_pct=_number(payload, "expected_return_pct", 7.0),
        expected_volatility_pct=_number(payload, "expected_volatility_pct", 11.0),
        review_cadence=str(payload.get("review_cadence", "Quarterly.")),
        # Disclosures are screened, not assumed: an empty list is a legitimate
        # input that the capital-at-risk principle is entitled to fail.
        disclosures=[str(d) for d in payload.get("disclosures", [])],
    )


def screen(payload: dict[str, Any]) -> dict[str, Any]:
    """Run every deterministic principle in force and aggregate a verdict."""
    started = time.perf_counter()
    try:
        request = _build_request(payload)
        recommendation = _build_recommendation(payload)
    except ValidationError as exc:
        # Whatever the hand-written checks above did not catch, the models will.
        # Translated to a ScreenError so the caller gets the offending field
        # rather than a pydantic dump of the entire payload.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'payload'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ScreenError(f"Invalid portfolio: {problems}") from None

    findings = run_machine_checks(recommendation, request, _PROVIDER)
    verdict, blocking, remediations = aggregate_verdict(findings, SCREENING_REVISION_HEADROOM)

    in_force = applicable_principles(request.mandate, request.jurisdiction)
    deterministic = [p for p in in_force if p.is_deterministic]

    detailed = []
    for finding in findings:
        principle = by_id(finding.principle_id)
        detailed.append(
            {
                "principle_id": finding.principle_id,
                "title": principle.title if principle else finding.principle_id,
                "severity": principle.severity.value if principle else "material",
                "source": principle.source if principle else "",
                "status": finding.status.value,
                "rationale": finding.rationale,
                "evidence": finding.evidence,
                "remediation": finding.remediation,
            }
        )

    return {
        "verdict": verdict.value,
        "mandate": f"{request.mandate.value}/{request.jurisdiction}",
        "principles_in_force": len(in_force),
        "settled_by_arithmetic": len(deterministic),
        "judged_by_model": len(in_force) - len(deterministic),
        "findings": detailed,
        "blocking_failures": blocking,
        "remediations": remediations,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "data_source": "synthetic",
        "notice": (
            "Reference data is fabricated deterministically from a hash of each symbol. "
            "This endpoint demonstrates the screening logic; it is not investment advice "
            "and carries no Shari'ah opinion."
        ),
        "model_calls": 0,
    }


class handler(BaseHTTPRequestHandler):
    """Vercel Python runtime entry point.

    Two naming choices here are imposed from outside and are not free to change:

    * The class is lowercase because Vercel's Python runtime resolves the entry
      point by looking for a module-level object named exactly ``handler``.
      Ruff's N801 is suppressed for this file in ``pyproject.toml``.
    * ``do_OPTIONS`` / ``do_GET`` / ``do_POST`` are dispatched by name from
      :class:`BaseHTTPRequestHandler`, which builds the method name from the
      HTTP verb. Renaming them to snake_case silently disables the route rather
      than failing loudly, so they must keep their shouted suffixes.
    """

    def _send(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()
        self.wfile.write(encoded)

    def do_OPTIONS(self) -> None:
        self._send(204, {})

    def do_GET(self) -> None:
        """A GET returns the contract, so the endpoint is self-describing."""
        self._send(
            200,
            {
                "endpoint": "POST /api/screen",
                "description": (
                    "Deterministic compliance screening. No model call, no API key, "
                    "reproducible output."
                ),
                "example_request": {
                    "mandate": "sharia",
                    "jurisdiction": "SA",
                    "summary": "A balanced Shari'ah portfolio.",
                    "allocations": [
                        {"symbol": "2222.SR", "weight_pct": 31.0},
                        {"symbol": "SUKUK.GCC", "weight_pct": 69.0},
                    ],
                },
            },
        )

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "Invalid Content-Length header."})
            return
        if length <= 0:
            self._send(400, {"error": "Empty request body."})
            return
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": f"Request body exceeds {MAX_BODY_BYTES} bytes."})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": f"Body must be valid UTF-8 JSON: {exc}"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"error": "Body must be a JSON object."})
            return

        try:
            self._send(200, screen(payload))
        except ScreenError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 — never leak a stack trace to the caller
            self._send(
                400,
                {
                    "error": "Could not screen this portfolio.",
                    "detail": f"{type(exc).__name__}: {exc}",
                },
            )

    def log_message(self, *_args: Any) -> None:
        """Silence per-request stderr logging; Vercel captures responses already."""
