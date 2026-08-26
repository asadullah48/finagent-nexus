"""Command-line entry point.

    finagent run --mandate examples/mandates/balanced_sharia.json
    finagent run --objective "Preserve capital" --capital 2500000 --horizon 7
    finagent verify-audit audit/<correlation-id>.jsonl

The ``verify-audit`` subcommand exists so that checking a run's integrity does
not require the rest of the system: it reads the JSONL, recomputes the hash
chain, and prints the result. An auditor with the file and Python can verify a
decision without credentials, network access, or trust in this codebase.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from finagent_nexus.audit import AuditTrail
from finagent_nexus.config import Settings
from finagent_nexus.graph import NexusRunner
from finagent_nexus.state import ClientRequest, Mandate, NexusState, Verdict


def _load_request(args: argparse.Namespace) -> ClientRequest:
    if args.mandate:
        payload = json.loads(Path(args.mandate).read_text(encoding="utf-8"))
        return ClientRequest.model_validate(payload)
    return ClientRequest(
        client_id=args.client_id,
        objective=args.objective,
        capital_usd=args.capital,
        horizon_years=args.horizon,
        risk_tolerance=args.risk,
        jurisdiction=args.jurisdiction,
        mandate=Mandate(args.mandate_type),
        constraints=args.constraint or [],
    )


def _print_report(state: NexusState) -> int:
    """Render the run to stdout. Returns the process exit code."""
    trail: AuditTrail = state["trail"]
    review = state.get("compliance_review")
    recommendation = state.get("recommendation")
    halted = state.get("halted")

    print("=" * 72)
    print("FinAgent-Nexus — run report")
    print("=" * 72)
    print(f"Correlation ID : {trail.correlation_id}")
    print(f"Revisions used : {state.get('revision', 0)} / {state.get('max_revisions', 0)}")

    if halted:
        print(f"Outcome        : HALTED — {halted}")
    elif review is not None:
        print(f"Outcome        : {review.verdict.value.upper()}")
    else:
        print("Outcome        : INCOMPLETE")

    # Bound to a local rather than re-subscripted: `if state.get("plan")` does
    # not narrow `state["plan"]` for a type checker, so the guard and the access
    # were only safe by adjacency — the arrangement that breaks the moment
    # anything is inserted between them.
    plan = state.get("plan")
    if plan is not None:
        print(f"\nThesis\n------\n{plan.thesis}")

    if recommendation is not None:
        print("\nRecommendation")
        print("--------------")
        print(recommendation.summary)
        print(f"\n{'Symbol':<14}{'Weight':>9}   Rationale")
        for allocation in recommendation.allocations:
            print(
                f"{allocation.symbol:<14}{allocation.weight_pct:>8.1f}%   "
                f"{allocation.rationale[:60]}"
            )
        print(f"{'TOTAL':<14}{recommendation.total_weight_pct():>8.1f}%")
        print(
            f"\nExpected return {recommendation.expected_return_pct:.2f}% | "
            f"Expected volatility {recommendation.expected_volatility_pct:.2f}% | "
            f"Review {recommendation.review_cadence}"
        )
        for disclosure in recommendation.disclosures:
            print(f"  · {disclosure}")

    if review is not None:
        print("\nCompliance findings")
        print("-------------------")
        for finding in review.findings:
            mark = {"pass": "PASS", "fail": "FAIL", "unverifiable": "????"}[finding.status.value]
            print(f"[{mark}] {finding.principle_id}: {finding.rationale}")
            if finding.evidence:
                print(f"        evidence: {finding.evidence}")
        if review.remediations:
            print("\nOutstanding remediations")
            for item in review.remediations:
                print(f"  · {item}")

    summary = trail.summary()
    print("\nAudit")
    print("-----")
    print(
        f"{summary['events']} events | chain valid: {summary['chain_valid']} | "
        f"tokens in/out: {summary['input_tokens']}/{summary['output_tokens']} "
        f"(cached {summary['cache_read_input_tokens']}) | "
        f"{summary['latency_ms']} ms of model time"
    )
    if summary["path"]:
        print(f"Trail written to {summary['path']}")

    if halted:
        return 2
    if review is not None and review.verdict is Verdict.PASS:
        return 0
    return 1


def _cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    # ``replace`` rather than rebuilding field by field: Settings is frozen, and
    # an explicit constructor call silently resets every field it forgets to
    # list — which is how a security-relevant flag goes missing when the next
    # one is added.
    overrides: dict[str, Any] = {}
    if args.max_revisions is not None:
        overrides["max_revisions"] = args.max_revisions
    if args.allow_synthetic_data:
        overrides["allow_synthetic_data"] = True
    if overrides:
        settings = replace(settings, **overrides)
    runner = NexusRunner(settings=settings)
    state = runner.run(_load_request(args))
    return _print_report(state)


def _cmd_verify_audit(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"No such audit file: {path}", file=sys.stderr)
        return 2
    trail = AuditTrail.load(path)
    valid = trail.verify()
    print(f"{path}: {len(trail.events)} events — chain {'VALID' if valid else 'BROKEN'}")
    if not valid:
        print("The trail has been altered since it was written.", file=sys.stderr)
        return 1
    for event in trail.events:
        print(f"  {event.seq:>3}  {event.ts}  {event.actor:<18} {event.action:<16} {event.status}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finagent", description="FinAgent-Nexus — Multi-Agent Financial Intelligence"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run one mandate end to end.")
    run.add_argument("--mandate", help="Path to a JSON mandate file.")
    run.add_argument("--client-id", default="CL-DEMO-001")
    run.add_argument("--objective", default="Grow capital within a Shari'ah-compliant mandate.")
    run.add_argument("--capital", type=float, default=1_000_000.0)
    run.add_argument("--horizon", type=int, default=10)
    run.add_argument(
        "--risk",
        default="balanced",
        choices=["conservative", "balanced", "growth", "aggressive"],
    )
    run.add_argument("--jurisdiction", default="SA")
    run.add_argument("--mandate-type", default="sharia", choices=["sharia", "conventional"])
    run.add_argument("--constraint", action="append", help="Repeatable client constraint.")
    run.add_argument("--max-revisions", type=int, help="Override FINAGENT_MAX_REVISIONS.")
    run.add_argument(
        "--allow-synthetic-data",
        action="store_true",
        help=(
            "Permit the bundled synthetic market data fixture. Demos only: it "
            "fabricates prices from a hash of the symbol and must never inform "
            "live advice."
        ),
    )
    run.set_defaults(func=_cmd_run)

    verify = sub.add_parser("verify-audit", help="Recompute an audit trail's hash chain.")
    verify.add_argument("path", help="Path to a .jsonl audit trail.")
    verify.set_defaults(func=_cmd_verify_audit)

    return parser


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


def main(argv: list[str] | None = None) -> int:
    _use_utf8_stdout()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
