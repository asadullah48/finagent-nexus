"""Evaluation harness — ongoing monitoring of the deterministic screens.

Model risk management frameworks require ongoing monitoring, not a one-time
validation. This is where that lives, and it ships **inside the package** rather
than inside the test tree, for a plain reason: a second-line function that has to
clone a repository and run pytest to re-validate a control will not do it
monthly. ``finagent eval`` runs the same cases against an installed package and
exits non-zero when a screen stops catching what it is meant to catch.

The harness has two tiers, and the split is deliberate:

* **Offline tier** — replays golden cases through the deterministic screens only.
  No API key, no network, no variance. This is the regression suite for the
  constitution itself: loosen a threshold and a case stops failing, and the build
  goes red. It is what this module implements.
* **Live tier** — runs whole mandates through the graph against the real model.
  Slow, costs money, inherently non-deterministic, so it is marked ``live`` and
  excluded from the default run. It stays in the test tree, where it belongs.

Note the absence of a default dataset path. The golden cases are **the
institution's** claims about its own constitution, not ours; a default pointing
at the bundled examples would let a deployment believe it was monitoring its own
thresholds while it was in fact replaying a demo. The caller names the file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from finagent_nexus.checks import run_machine_checks
from finagent_nexus.state import (
    ClientRequest,
    FindingStatus,
    PrincipleFinding,
    Recommendation,
    Verdict,
)
from finagent_nexus.tools.market_data import MarketDataProvider
from finagent_nexus.verdict import aggregate_verdict

__all__ = [
    "CaseResult",
    "GoldenCase",
    "evaluate_case",
    "load_cases",
    "report",
    "run_suite",
]


@dataclass(frozen=True)
class GoldenCase:
    """One labelled portfolio with the outcome the constitution must produce."""

    id: str
    description: str
    request: ClientRequest
    recommendation: Recommendation
    must_fail: tuple[str, ...]
    must_pass: tuple[str, ...]
    verdict_with_budget: Verdict

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GoldenCase:
        expect = payload["expect"]
        return cls(
            id=payload["id"],
            description=payload["description"],
            request=ClientRequest.model_validate(payload["request"]),
            recommendation=Recommendation.model_validate(payload["recommendation"]),
            must_fail=tuple(expect.get("must_fail", [])),
            must_pass=tuple(expect.get("must_pass", [])),
            verdict_with_budget=Verdict(expect["verdict_with_budget"]),
        )


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    verdict: Verdict
    expected_verdict: Verdict
    missed: list[str] = field(default_factory=list)
    false_alarms: list[str] = field(default_factory=list)
    findings: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        if self.passed:
            return f"PASS  {self.case_id}"
        problems = []
        if self.verdict is not self.expected_verdict:
            problems.append(
                f"verdict {self.verdict.value} != expected {self.expected_verdict.value}"
            )
        if self.missed:
            problems.append(f"missed breaches: {', '.join(self.missed)}")
        if self.false_alarms:
            problems.append(f"false alarms: {', '.join(self.false_alarms)}")
        return f"FAIL  {self.case_id} — {'; '.join(problems)}"


def load_cases(path: Path) -> list[GoldenCase]:
    """Parse a golden-case dataset. See the module docstring on why no default."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [GoldenCase.from_dict(case) for case in payload["cases"]]


def evaluate_case(
    case: GoldenCase, provider: MarketDataProvider, revisions_remaining: int = 2
) -> CaseResult:
    """Run the deterministic screens over one case and score the outcome."""
    findings: list[PrincipleFinding] = run_machine_checks(
        case.recommendation, case.request, provider
    )
    by_id = {f.principle_id: f for f in findings}
    verdict, _, _ = aggregate_verdict(findings, revisions_remaining)

    # A breach the screens should have caught but reported as clean.
    missed = [
        principle_id
        for principle_id in case.must_fail
        if by_id.get(principle_id) is None or by_id[principle_id].status is FindingStatus.PASS
    ]
    # A clean holding the screens flagged anyway — the cost side of the ledger,
    # since every false alarm burns a revision cycle and reviewer attention.
    false_alarms = [
        principle_id
        for principle_id in case.must_pass
        if principle_id in by_id and by_id[principle_id].status is not FindingStatus.PASS
    ]

    return CaseResult(
        case_id=case.id,
        passed=(not missed and not false_alarms and verdict is case.verdict_with_budget),
        verdict=verdict,
        expected_verdict=case.verdict_with_budget,
        missed=missed,
        false_alarms=false_alarms,
        findings={pid: f.status.value for pid, f in by_id.items()},
    )


def run_suite(provider: MarketDataProvider, path: Path) -> list[CaseResult]:
    return [evaluate_case(case, provider) for case in load_cases(path)]


def report(results: list[CaseResult]) -> str:
    """Human-readable roll-up, suitable for a CI log or a validation pack."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    missed_total = sum(len(r.missed) for r in results)
    false_alarm_total = sum(len(r.false_alarms) for r in results)

    lines = [
        "FinAgent-Nexus — deterministic screen evaluation",
        "=" * 52,
        *(r.describe() for r in results),
        "-" * 52,
        f"cases:        {passed}/{total} correct",
        f"missed:       {missed_total}  (breaches the screens failed to catch)",
        f"false alarms: {false_alarm_total}  (clean holdings flagged anyway)",
    ]
    return "\n".join(lines)
