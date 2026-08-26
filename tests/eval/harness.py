"""Test-tree view of the evaluation harness.

The harness itself moved to :mod:`finagent_nexus.evaluation` so that a second
line of defence can run it against an installed package via ``finagent eval``,
instead of needing a clone and a pytest invocation. What stays here is the part
that is genuinely test-scoped: the location of *this repository's* golden cases.

The package deliberately refuses to default that path — the golden cases are an
institution's claims about its own constitution, and a default would let a
deployment believe it was monitoring its own thresholds while replaying a demo.
Inside this repository the demo cases *are* the subject, so binding the default
here is correct.
"""

from __future__ import annotations

from pathlib import Path

from finagent_nexus.evaluation import (
    CaseResult,
    GoldenCase,
    evaluate_case,
    report,
)
from finagent_nexus.evaluation import (
    load_cases as _load_cases,
)
from finagent_nexus.evaluation import (
    run_suite as _run_suite,
)
from finagent_nexus.tools.market_data import MarketDataProvider

DATASET_PATH = Path(__file__).parent / "datasets" / "golden_cases.json"

__all__ = [
    "DATASET_PATH",
    "CaseResult",
    "GoldenCase",
    "evaluate_case",
    "load_cases",
    "report",
    "run_suite",
]


def load_cases(path: Path = DATASET_PATH) -> list[GoldenCase]:
    return _load_cases(path)


def run_suite(provider: MarketDataProvider, path: Path = DATASET_PATH) -> list[CaseResult]:
    return _run_suite(provider, path)


if __name__ == "__main__":  # pragma: no cover
    from finagent_nexus.tools.market_data import SyntheticMarketData

    print(report(run_suite(SyntheticMarketData())))
