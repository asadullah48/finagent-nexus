"""Demo: the full Plan-Act-Verify loop.

Run:  python examples/wealth_strategy.py
      python examples/wealth_strategy.py examples/mandates/conservative_uae.json

Requires Claude credentials — either ``ANTHROPIC_API_KEY`` or an ``ant auth
login`` profile.

This runs a mandate end to end and then prints the audit trail, because the
trail is the deliverable a second line of defence actually cares about. Watch
the ``review`` events: if the first recommendation breaches a screen, you will
see the loop return to Act or Synthesize and try again, bounded by the revision
budget.
"""

from __future__ import annotations

import json
from dataclasses import replace
import sys
from pathlib import Path

from finagent_nexus.config import Settings
from finagent_nexus.graph import NexusRunner
from finagent_nexus.state import ClientRequest, Mandate, Verdict

DEFAULT_REQUEST = ClientRequest(
    client_id="CL-DEMO-0142",
    objective="Balanced growth within a Shari'ah-compliant mandate.",
    capital_usd=2_500_000,
    horizon_years=10,
    risk_tolerance="balanced",
    jurisdiction="SA",
    mandate=Mandate.SHARIA,
    constraints=["No leveraged products.", "Minimum 25% in income-generating assets."],
)


def load_request(argv: list[str]) -> ClientRequest:
    if len(argv) > 1:
        payload = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        return ClientRequest.model_validate(payload)
    return DEFAULT_REQUEST


def _use_utf8_stdout() -> None:
    """Windows consoles default to cp1252, which cannot encode the report glyphs.

    Without this, printing a threshold such as "<=" rendered as U+2264 raises
    UnicodeEncodeError and the run dies after the work is already done. Reported
    output is a deliverable, not a debug aid; it should not depend on the host
    console's code page.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    _use_utf8_stdout()
    request = load_request(argv)
    settings = Settings.from_env()
    # This example is a demo and runs on the synthetic fixture, so it opts in
    # explicitly. Synthetic data is never inherited from a default: see
    # NexusRunner._resolve_provider and docs/governance.md section 7.
    runner = NexusRunner(settings=replace(settings, allow_synthetic_data=True))

    print("=" * 78)
    print("FinAgent-Nexus — Plan · Act · Verify")
    print("=" * 78)
    print(
        f"Mandate: {request.mandate.value} / {request.jurisdiction} · "
        f"{request.risk_tolerance} · {request.horizon_years}y · "
        f"${request.capital_usd:,.0f}"
    )
    print(f"Revision budget: {settings.max_revisions}\n")

    state = runner.run(request)

    plan = state.get("plan")
    if plan is not None:
        print(f"THESIS\n  {plan.thesis}")
        print(f"\n  Universe: {', '.join(plan.universe)}")
        for step in plan.steps:
            print(f"  [{step.id}] {step.agent}: {step.objective}")
            print(f"        done when: {step.success_criteria}")

    recommendation = state.get("recommendation")
    if recommendation is not None:
        print(f"\nRECOMMENDATION\n  {recommendation.summary}\n")
        print(f"  {'Symbol':<14}{'Weight':>9}   Rationale")
        for allocation in recommendation.allocations:
            print(
                f"  {allocation.symbol:<14}{allocation.weight_pct:>8.1f}%   "
                f"{allocation.rationale[:52]}"
            )
        print(f"  {'TOTAL':<14}{recommendation.total_weight_pct():>8.1f}%")
        print(
            f"\n  Expected return {recommendation.expected_return_pct:.2f}% · "
            f"volatility {recommendation.expected_volatility_pct:.2f}% · "
            f"review {recommendation.review_cadence}"
        )
        for disclosure in recommendation.disclosures:
            print(f"  · {disclosure}")

    review = state.get("compliance_review")
    if review is not None:
        passes = sum(1 for f in review.findings if f.status.value == "pass")
        print(f"\nCOMPLIANCE — {passes}/{len(review.findings)} principles clear")
        for finding in review.findings:
            if finding.status.value != "pass":
                print(f"  [{finding.status.value.upper()}] {finding.principle_id}: {finding.rationale}")
        if review.remediations:
            print("\n  Outstanding remediations:")
            for item in review.remediations:
                print(f"    · {item}")

    print("\nAUDIT TRAIL")
    trail = state["trail"]
    for event in trail.events:
        print(f"  {event.seq:>2}  {event.actor:<18} {event.action:<16} {event.status}")

    summary = trail.summary()
    print(
        f"\n  chain valid: {summary['chain_valid']} · "
        f"{summary['input_tokens']} in / {summary['output_tokens']} out "
        f"({summary['cache_read_input_tokens']} cached) · {summary['latency_ms']} ms"
    )
    if summary["path"]:
        print(f"  written to {summary['path']}")
        print(f"  verify with: finagent verify-audit {summary['path']}")

    halted = state.get("halted")
    if halted:
        print(f"\nOUTCOME: HALTED — {halted}")
        return 2
    if review is not None and review.verdict is Verdict.PASS:
        print(f"\nOUTCOME: APPROVED after {state.get('revision', 0)} revision(s)")
        return 0
    print(f"\nOUTCOME: ESCALATED TO A HUMAN — {review.verdict.value if review else 'unknown'}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
