"""Deterministic compliance screens.

These tests are the reason the screens are worth having: they run in
milliseconds, need no API key, and would fail loudly if someone loosened a
threshold. A Shari'ah board can be shown this file as evidence that the ratios
it signed off are the ratios enforced.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from finagent_nexus.checks import run_machine_checks
from finagent_nexus.constitution import SHARIA_THRESHOLDS
from finagent_nexus.state import Allocation, FindingStatus, Recommendation

from .conftest import CAPITAL_AT_RISK, build_recommendation


def findings_by_id(findings):
    return {f.principle_id: f for f in findings}


def test_compliant_portfolio_passes_every_screen(
    compliant_recommendation, sharia_request, provider
):
    findings = run_machine_checks(compliant_recommendation, sharia_request, provider)
    assert findings, "the Sharia mandate must trigger deterministic screens"
    assert all(f.status is FindingStatus.PASS for f in findings), [
        (f.principle_id, f.status, f.rationale)
        for f in findings
        if f.status is not FindingStatus.PASS
    ]


def test_interest_bearing_instrument_fails_riba_screen(sharia_request, provider):
    # TLT is a conventional bond fund: riba-bearing by construction.
    recommendation = build_recommendation({"SUKUK.GCC": 70.0, "TLT": 30.0})
    result = findings_by_id(run_machine_checks(recommendation, sharia_request, provider))

    assert result["SHARIA-RIBA-01"].status is FindingStatus.FAIL
    assert "TLT" in result["SHARIA-RIBA-01"].evidence
    assert result["SHARIA-RIBA-01"].remediation, "a fixable breach must offer a remediation"
    # 100% interest-bearing securities and 100% impure revenue also breach.
    assert result["SHARIA-SCREEN-03"].status is FindingStatus.FAIL
    assert result["SHARIA-SCREEN-04"].status is FindingStatus.FAIL


def test_leverage_screen_uses_the_aaoifi_threshold(sharia_request, provider):
    # EMAAR.DU carries 34.8% debt to market cap against a 30% limit.
    recommendation = build_recommendation({"SUKUK.GCC": 75.0, "EMAAR.DU": 25.0})
    result = findings_by_id(run_machine_checks(recommendation, sharia_request, provider))

    finding = result["SHARIA-SCREEN-02"]
    assert finding.status is FindingStatus.FAIL
    assert "34.8" in finding.evidence
    assert f"{SHARIA_THRESHOLDS['max_debt_to_market_cap_pct']:.0f}%" in finding.rationale


def test_prohibited_sector_is_caught(sharia_request, provider):
    recommendation = build_recommendation({"SUKUK.GCC": 80.0, "DEMO.ALC": 20.0})
    result = findings_by_id(run_machine_checks(recommendation, sharia_request, provider))

    assert result["SHARIA-SECTOR-05"].status is FindingStatus.FAIL
    assert "alcohol" in result["SHARIA-SECTOR-05"].evidence


def test_concentration_limit(sharia_request, provider):
    recommendation = build_recommendation({"SUKUK.GCC": 60.0, "2222.SR": 40.0})
    result = findings_by_id(run_machine_checks(recommendation, sharia_request, provider))

    finding = result["REG-CONC-02"]
    assert finding.status is FindingStatus.FAIL
    assert "40.0%" in finding.evidence


@pytest.mark.parametrize(
    "weights,reason",
    [
        ({"SUKUK.GCC": 60.0, "2222.SR": 25.0}, "under 100%"),
        ({"SUKUK.GCC": 60.0, "2222.SR": 25.0, "GLD": 30.0}, "over 100%"),
    ],
)
def test_weights_must_sum_to_one_hundred(weights, reason, sharia_request, provider):
    recommendation = build_recommendation(weights)
    result = findings_by_id(run_machine_checks(recommendation, sharia_request, provider))
    assert result["REG-ALLOC-03"].status is FindingStatus.FAIL, reason


def test_weights_within_tolerance_pass(sharia_request, provider):
    # 99.7% is inside the documented ±0.5pp rounding tolerance.
    recommendation = build_recommendation({"SUKUK.GCC": 60.0, "2222.SR": 24.7, "GLD": 15.0})
    result = findings_by_id(run_machine_checks(recommendation, sharia_request, provider))
    assert result["REG-ALLOC-03"].status is FindingStatus.PASS


@pytest.mark.parametrize(
    "phrase",
    [
        "This portfolio delivers a guaranteed 8% return.",
        "A risk-free way to grow capital.",
        "An assured return over the horizon.",
        "There is no downside to this allocation.",
    ],
)
def test_guarantee_language_is_rejected(phrase, sharia_request, provider):
    recommendation = build_recommendation(
        {"SUKUK.GCC": 60.0, "2222.SR": 25.0, "GLD": 15.0},
        summary=phrase,
        disclosures=[CAPITAL_AT_RISK],
    )
    result = findings_by_id(run_machine_checks(recommendation, sharia_request, provider))
    assert result["REG-DISC-04"].status is FindingStatus.FAIL


def test_missing_capital_at_risk_disclosure_is_rejected(sharia_request, provider):
    recommendation = build_recommendation(
        {"SUKUK.GCC": 60.0, "2222.SR": 25.0, "GLD": 15.0},
        disclosures=["Past performance is not indicative of future results."],
    )
    result = findings_by_id(run_machine_checks(recommendation, sharia_request, provider))

    finding = result["REG-DISC-04"]
    assert finding.status is FindingStatus.FAIL
    assert "capital-at-risk" in finding.rationale.lower()


def test_unknown_symbol_is_unverifiable_not_pass(sharia_request, provider):
    """The critical fail-closed property: absent data is never a pass."""
    recommendation = build_recommendation({"SUKUK.GCC": 70.0, "NOT.A.REAL.SYMBOL": 30.0})
    result = findings_by_id(run_machine_checks(recommendation, sharia_request, provider))

    for principle_id in ("SHARIA-RIBA-01", "SHARIA-SCREEN-02", "SHARIA-SECTOR-05"):
        assert result[principle_id].status is FindingStatus.UNVERIFIABLE
        assert result[principle_id].status is not FindingStatus.PASS


def test_conventional_mandate_skips_sharia_screens(conventional_request, provider):
    """A conventional mandate must not be judged against Shari'ah principles."""
    recommendation = build_recommendation(
        {"SPY": 25.0, "TLT": 25.0, "GLD": 25.0, "SPUS": 25.0}
    )
    result = findings_by_id(run_machine_checks(recommendation, conventional_request, provider))

    assert not any(pid.startswith("SHARIA-") for pid in result)
    assert result["REG-ALLOC-03"].status is FindingStatus.PASS
    assert result["REG-CONC-02"].status is FindingStatus.PASS


def test_negative_weight_cannot_be_constructed():
    """First line of defence: the schema forbids it, so a model cannot emit one."""
    with pytest.raises(ValidationError):
        Allocation(symbol="2222.SR", weight_pct=-20.0, rationale="Short position.")


def test_negative_weights_are_rejected(sharia_request, provider):
    """Second line: the screen still catches one in an object built in code.

    ``model_construct`` bypasses validation, which is the only way to reach this
    branch — worth testing anyway, because objects assembled by future callers
    (a migration, an import, a portfolio optimiser) do not pass through the
    model's JSON schema.
    """
    recommendation = Recommendation.model_construct(
        summary="A leveraged long-short book.",
        allocations=[
            Allocation.model_construct(
                symbol="SUKUK.GCC", weight_pct=120.0, rationale="Levered long."
            ),
            Allocation.model_construct(
                symbol="2222.SR", weight_pct=-20.0, rationale="Funding short."
            ),
        ],
        expected_return_pct=9.0,
        expected_volatility_pct=14.0,
        review_cadence="Quarterly",
        disclosures=[CAPITAL_AT_RISK],
    )
    result = findings_by_id(run_machine_checks(recommendation, sharia_request, provider))

    finding = result["REG-ALLOC-03"]
    assert finding.status is FindingStatus.FAIL
    assert "short" in finding.rationale.lower()


def test_machine_checks_are_pure(compliant_recommendation, sharia_request, provider):
    """Same inputs, same findings — a precondition for offline reproducibility."""
    first = run_machine_checks(compliant_recommendation, sharia_request, provider)
    second = run_machine_checks(compliant_recommendation, sharia_request, provider)
    assert [f.model_dump() for f in first] == [f.model_dump() for f in second]
