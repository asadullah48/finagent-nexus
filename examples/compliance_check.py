"""Demo: the constitution catching real breaches — with no API key.

Run:  python examples/compliance_check.py

This is the demonstration worth showing a compliance function first, because
nothing here depends on a model. The AAOIFI screens and the regulatory checks
are pure functions over a portfolio and its reference data, so their findings
are reproducible on any machine, offline, forever.
"""

from __future__ import annotations

import sys

from finagent_nexus.checks import run_machine_checks

# See tests/eval/harness.py for why this comes from `verdict`, not `agents`:
# the package import drags in `compliance_officer.py` -> `llm.py` ->
# `anthropic`, which would silently break this file's own "no API key" claim.
from finagent_nexus.verdict import aggregate_verdict
from finagent_nexus.constitution import (
    MAX_SINGLE_POSITION_PCT,
    SHARIA_THRESHOLDS,
    applicable_principles,
)
from finagent_nexus.state import (
    Allocation,
    ClientRequest,
    FindingStatus,
    Mandate,
    Recommendation,
)
from finagent_nexus.tools import SyntheticMarketData

CAPITAL_AT_RISK = "Capital is at risk and the value of investments may fall as well as rise."

STATUS_MARK = {
    FindingStatus.PASS: "PASS",
    FindingStatus.FAIL: "FAIL",
    FindingStatus.UNVERIFIABLE: "????",
}


def portfolio(summary: str, weights: dict[str, float], disclosures: list[str]) -> Recommendation:
    return Recommendation(
        summary=summary,
        allocations=[
            Allocation(symbol=symbol, weight_pct=weight, rationale=f"Position in {symbol}.")
            for symbol, weight in weights.items()
        ],
        expected_return_pct=6.5,
        expected_volatility_pct=9.5,
        review_cadence="Quarterly",
        disclosures=disclosures,
    )


SHARIA_MANDATE = ClientRequest(
    client_id="CL-DEMO-0142",
    objective="Balanced growth within a Shari'ah-compliant mandate.",
    capital_usd=2_500_000,
    horizon_years=10,
    risk_tolerance="balanced",
    jurisdiction="SA",
    mandate=Mandate.SHARIA,
)

SCENARIOS: list[tuple[str, Recommendation]] = [
    (
        "A compliant balanced portfolio",
        portfolio(
            "A sukuk-anchored balanced portfolio.",
            {"SUKUK.GCC": 25.0, "2222.SR": 25.0, "SPUS": 25.0, "GLD": 25.0},
            [CAPITAL_AT_RISK],
        ),
    ),
    (
        "A conventional bond fund slipped into a Shari'ah mandate",
        portfolio(
            "An income portfolio blending sukuk with government bonds.",
            {"SUKUK.GCC": 25.0, "2222.SR": 25.0, "GLD": 25.0, "TLT": 25.0},
            [CAPITAL_AT_RISK],
        ),
    ),
    (
        "A holding above the 30% leverage screen",
        portfolio(
            "A GCC growth portfolio with real-estate exposure.",
            {"SUKUK.GCC": 25.0, "2222.SR": 25.0, "EMAAR.DU": 25.0, "GLD": 25.0},
            [CAPITAL_AT_RISK],
        ),
    ),
    (
        "A promise no regulated firm may make",
        portfolio(
            "This portfolio delivers a guaranteed 8% annual return with no downside.",
            {"SUKUK.GCC": 25.0, "2222.SR": 25.0, "SPUS": 25.0, "GLD": 25.0},
            [CAPITAL_AT_RISK],
        ),
    ),
    (
        "An instrument with no reference data (fail-closed)",
        portfolio(
            "A balanced portfolio including an off-platform holding.",
            {"SUKUK.GCC": 25.0, "2222.SR": 25.0, "GLD": 25.0, "MYSTERY.XX": 25.0},
            [CAPITAL_AT_RISK],
        ),
    ),
]


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


def main() -> None:
    _use_utf8_stdout()
    provider = SyntheticMarketData()

    print("=" * 78)
    print("FinAgent-Nexus — deterministic compliance screening")
    print("=" * 78)
    print("No API key. No network. Pure functions over a portfolio and its reference data.\n")

    principles = applicable_principles(SHARIA_MANDATE.mandate, SHARIA_MANDATE.jurisdiction)
    machine = [p for p in principles if p.is_deterministic]
    print(
        f"Mandate: {SHARIA_MANDATE.mandate.value} / {SHARIA_MANDATE.jurisdiction} — "
        f"{len(principles)} principles in force, {len(machine)} settled by arithmetic."
    )
    print(
        f"Thresholds: debt ≤ {SHARIA_THRESHOLDS['max_debt_to_market_cap_pct']:.0f}%, "
        f"interest-bearing securities ≤ "
        f"{SHARIA_THRESHOLDS['max_interest_bearing_securities_pct']:.0f}%, "
        f"impure revenue ≤ {SHARIA_THRESHOLDS['max_non_compliant_revenue_pct']:.0f}%, "
        f"single position ≤ {MAX_SINGLE_POSITION_PCT:.0f}%."
    )

    for title, recommendation in SCENARIOS:
        holdings = ", ".join(
            f"{a.symbol} {a.weight_pct:.0f}%" for a in recommendation.allocations
        )
        print("\n" + "-" * 78)
        print(f"{title}")
        print(f"  Holdings: {holdings}")

        findings = run_machine_checks(recommendation, SHARIA_MANDATE, provider)
        verdict, blocking, remediations = aggregate_verdict(findings, revisions_remaining=2)

        for finding in findings:
            if finding.status is FindingStatus.PASS:
                continue
            print(f"  [{STATUS_MARK[finding.status]}] {finding.principle_id}: {finding.rationale}")
            if finding.evidence:
                print(f"         evidence: {finding.evidence}")
            if finding.remediation:
                print(f"         fix:      {finding.remediation}")

        clean = sum(1 for f in findings if f.status is FindingStatus.PASS)
        print(f"  {clean}/{len(findings)} screens clear  →  VERDICT: {verdict.value.upper()}")

    print("\n" + "=" * 78)
    print(
        "Note what did NOT happen: no model was asked whether 34.8% is under 30%.\n"
        "That is the point — arithmetic is not a matter of judgement, and a\n"
        "regulator can re-run every line above without credentials."
    )


if __name__ == "__main__":
    main()
