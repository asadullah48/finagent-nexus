"""Command-line entry point.

    finagent run          --mandate examples/mandates/balanced_sharia.json
    finagent verify-audit audit/<correlation-id>.jsonl
    finagent replay       audit/<correlation-id>.jsonl
    finagent eval         --dataset tests/eval/datasets/golden_cases.json

Three of those four subcommands are **deterministic-side tools**: they recompute
a hash chain, re-render a recorded decision, or replay golden cases through the
arithmetic screens. None of them needs a model, a credential, or a network.

That is why :class:`~finagent_nexus.graph.NexusRunner` is imported inside
``_cmd_run`` rather than at module scope. Importing it here pulled ``anthropic``,
``langgraph``, ``langchain_core`` and ``httpx`` into every invocation, including
``verify-audit`` — so the promise that "an auditor with the file and Python can
verify a decision without credentials, network access, or trust in this codebase"
was false the moment they tried to install only what they needed. It is the same
leak that moved ``aggregate_verdict`` into :mod:`finagent_nexus.verdict`, one
layer further out. ``tests/test_deterministic_isolation.py`` now holds this
module to that boundary in a subprocess.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from finagent_nexus.audit import AuditEvent, AuditTrail
from finagent_nexus.config import Settings
from finagent_nexus.evaluation import report, run_suite
from finagent_nexus.provider_policy import SyntheticDataNotPermitted, resolve_provider
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
    # Imported here, not at module scope: this is the only subcommand that needs
    # the model half of the system. See the module docstring.
    from finagent_nexus.graph import NexusRunner

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


def _events_by_action(trail: AuditTrail) -> dict[str, list[AuditEvent]]:
    grouped: dict[str, list[AuditEvent]] = {}
    for event in trail.events:
        grouped.setdefault(event.action, []).append(event)
    return grouped


def _cmd_replay(args: argparse.Namespace) -> int:
    """Reconstruct a decision from its audit trail alone.

    This is the read-back half of the tamper-evidence claim. ``verify-audit``
    answers "has this file been altered?"; ``replay`` answers "what was decided,
    on what evidence, and under which configuration?" — six months later, from
    the file, with no model and no credentials.

    It **refuses to render an invalid chain**. A trail that does not verify is
    not a weaker record, it is an unknown one, and printing a tidy report from it
    would launder exactly the tampering the chain exists to expose. Same
    fail-closed rule the verdict policy applies to an unverifiable finding.
    """
    path = Path(args.path)
    if not path.exists():
        print(f"No such audit file: {path}", file=sys.stderr)
        return 2

    trail = AuditTrail.load(path)
    if not trail.verify():
        print(f"{path}: hash chain BROKEN — refusing to replay.", file=sys.stderr)
        print(
            "The trail has been altered since it was written, so nothing "
            "reconstructed from it can be relied upon. Run `finagent "
            "verify-audit` to locate the break.",
            file=sys.stderr,
        )
        return 1

    grouped = _events_by_action(trail)
    print("=" * 72)
    print("FinAgent-Nexus — replayed from audit trail")
    print("=" * 72)
    print(f"Source         : {path}")
    print(f"Correlation ID : {trail.correlation_id}")
    print(f"Events         : {len(trail.events)} | chain valid: True")

    for event in grouped.get("accept_mandate", []):
        request = event.detail.get("request", {})
        settings = event.detail.get("settings", {})
        print(f"\nMandate accepted {event.ts}")
        print("-" * 72)
        print(f"  Client      : {request.get('client_id')}")
        print(f"  Objective   : {request.get('objective')}")
        print(
            f"  Capital     : {request.get('capital_usd')} USD over "
            f"{request.get('horizon_years')} years"
        )
        print(
            f"  Mandate     : {request.get('mandate')}/{request.get('jurisdiction')} "
            f"({request.get('risk_tolerance')})"
        )
        for constraint in request.get("constraints") or []:
            print(f"    · {constraint}")
        # The data regime is the first thing a reviewer should see. A flawless
        # trail built on fabricated prices is the failure mode this records.
        print(f"  Model       : {settings.get('model')} at effort {settings.get('effort')}")
        print(f"  Synthetic   : {settings.get('allow_synthetic_data')}")

    for event in grouped.get("plan", []):
        print(f"\nPlan  ({event.model})")
        print("-" * 72)
        print(f"  {event.detail.get('thesis')}")
        print(f"  Universe: {', '.join(event.detail.get('universe') or [])}")

    for event in grouped.get("analyse", []):
        detail = event.detail
        print(f"\nAnalysis  revision {detail.get('revision')}  ({event.model})")
        print("-" * 72)
        print(
            f"  {detail.get('tool_calls')} tool calls, "
            f"{detail.get('tool_errors')} errors, "
            f"instruments: {', '.join(detail.get('instruments') or [])}"
        )
        for line in detail.get("evidence") or []:
            print(f"    · {line}")

    for event in grouped.get("synthesize", []):
        detail = event.detail
        allocations: dict[str, float] = detail.get("allocations") or {}
        print(f"\nRecommendation  revision {detail.get('revision')}  ({event.model})")
        print("-" * 72)
        for symbol, weight in allocations.items():
            print(f"  {symbol:<14}{weight:>8.1f}%")
        print(f"  {'TOTAL':<14}{sum(allocations.values()):>8.1f}%")
        print(
            f"  Expected return {detail.get('expected_return_pct')}% | "
            f"volatility {detail.get('expected_volatility_pct')}%"
        )

    for event in grouped.get("review", []):
        detail = event.detail
        print(
            f"\nCompliance review  revision {detail.get('revision')}  "
            f"-> {str(detail.get('verdict')).upper()}"
        )
        print("-" * 72)
        for principle_id, status in (detail.get("findings") or {}).items():
            mark = {"pass": "PASS", "fail": "FAIL", "unverifiable": "????"}.get(status, status)
            print(f"  [{mark}] {principle_id}")
        for failure in detail.get("blocking_failures") or []:
            print(f"  BLOCKING: {failure}")
        for remediation in detail.get("remediations") or []:
            print(f"  fix: {remediation}")

    for event in grouped.get("finalize", []):
        detail = event.detail

        # The issued artefact, if the retention policy recorded it. This is the
        # only place the client-facing prose exists in the trail; everything
        # above is a summary of the process that produced it.
        issued = detail.get("recommendation")
        if issued:
            print("\nAs issued")
            print("-" * 72)
            print(f"  {issued.get('summary')}")
            print(f"\n  {'Symbol':<14}{'Weight':>8}   Rationale")
            for allocation in issued.get("allocations") or []:
                print(
                    f"  {allocation.get('symbol'):<14}"
                    f"{allocation.get('weight_pct'):>7.1f}%   "
                    f"{allocation.get('rationale')}"
                )
            print(f"  Review cadence: {issued.get('review_cadence')}")
            for disclosure in issued.get("disclosures") or []:
                print(f"    · {disclosure}")
            print(f"  Digest: {detail.get('recommendation_digest')}")

        detailed = detail.get("findings_detail")
        if detailed:
            print("\nFindings, in full")
            print("-" * 72)
            for finding in detailed:
                mark = {"pass": "PASS", "fail": "FAIL", "unverifiable": "????"}.get(
                    finding.get("status"), str(finding.get("status"))
                )
                print(f"  [{mark}] {finding.get('principle_id')}: {finding.get('rationale')}")
                if finding.get("evidence"):
                    print(f"          evidence: {finding['evidence']}")
                if finding.get("remediation"):
                    print(f"          fix:      {finding['remediation']}")

        print(f"\nOutcome: {str(detail.get('outcome')).upper()}")
        print("-" * 72)
        print(f"  Revisions used : {detail.get('revisions_used')}")
        if detail.get("halted_reason"):
            print(f"  Halted         : {detail.get('halted_reason')}")

    summary = trail.summary()
    print("\nCost and latency")
    print("-" * 72)
    print(
        f"  tokens in/out: {summary['input_tokens']}/{summary['output_tokens']} "
        f"(cached {summary['cache_read_input_tokens']}) | "
        f"{summary['latency_ms']} ms of model time"
    )
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    """Replay golden cases through the deterministic screens.

    Exits non-zero when a case fails, so a scheduled validation run is a build
    step rather than a report somebody has to read.
    """
    dataset = Path(args.dataset)
    if not dataset.exists():
        print(f"No such dataset: {dataset}", file=sys.stderr)
        return 2

    settings = Settings.from_env()
    allow_synthetic = args.allow_synthetic_data or settings.allow_synthetic_data
    try:
        # Same policy object the runner uses, so the eval cannot be run under a
        # data regime the runner would have refused.
        provider = resolve_provider(None, allow_synthetic=allow_synthetic)
    except SyntheticDataNotPermitted as exc:
        print(str(exc), file=sys.stderr)
        return 2

    results = run_suite(provider, dataset)
    print(report(results))
    failures = [r for r in results if not r.passed]
    if failures:
        print(f"\n{len(failures)} case(s) failed.", file=sys.stderr)
        return 1
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

    replay = sub.add_parser(
        "replay",
        help="Reconstruct a decision from its audit trail. Refuses a broken chain.",
    )
    replay.add_argument("path", help="Path to a .jsonl audit trail.")
    replay.set_defaults(func=_cmd_replay)

    evaluate = sub.add_parser(
        "eval",
        help="Replay golden cases through the deterministic screens; non-zero on failure.",
    )
    evaluate.add_argument(
        "--dataset",
        required=True,
        help=(
            "Path to a golden-case JSON dataset. Required by design: these are "
            "your claims about your constitution, and a default would let a "
            "deployment replay the bundled demo while believing it had "
            "validated its own thresholds."
        ),
    )
    evaluate.add_argument(
        "--allow-synthetic-data",
        action="store_true",
        help="Permit the bundled synthetic market data fixture. Demos only.",
    )
    evaluate.set_defaults(func=_cmd_eval)

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
