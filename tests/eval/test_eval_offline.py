"""Offline evaluation — the regression suite for the constitution itself.

Every case here is a claim about what the deterministic screens do. If someone
loosens a threshold, adds an instrument that should not clear, or "fixes" a
check in a way that stops it catching a breach, this file goes red and names the
principle that broke.
"""

from __future__ import annotations

import pytest

from finagent_nexus.state import Verdict

from .harness import evaluate_case, load_cases, report, run_suite

CASES = load_cases()


def test_dataset_is_non_trivial():
    """A suite of only-clean cases would pass while catching nothing."""
    assert len(CASES) >= 8
    breaching = [case for case in CASES if case.must_fail]
    clean = [case for case in CASES if not case.must_fail]
    assert breaching, "the dataset must contain portfolios that should be rejected"
    assert clean, "the dataset must contain portfolios that should be approved"


def test_case_ids_are_unique():
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_golden_case(case, provider):
    result = evaluate_case(case, provider)
    assert result.passed, result.describe() + f"\n  findings: {result.findings}"


def test_no_breach_is_missed(provider):
    """Aggregate accuracy: recall on the deterministic layer must be total."""
    results = run_suite(provider)
    missed = {r.case_id: r.missed for r in results if r.missed}
    assert not missed, f"screens failed to catch known breaches: {missed}"


def test_no_false_alarms(provider):
    """Precision matters too — every false alarm costs a revision cycle."""
    results = run_suite(provider)
    noisy = {r.case_id: r.false_alarms for r in results if r.false_alarms}
    assert not noisy, f"screens flagged compliant holdings: {noisy}"


def test_clean_portfolios_are_approved(provider):
    for case in CASES:
        if not case.must_fail:
            result = evaluate_case(case, provider)
            assert result.verdict is Verdict.PASS, result.describe()


def test_exhausted_budget_turns_every_breach_into_a_block(provider):
    """With no revisions left, a remediable breach must escalate, not pass."""
    for case in CASES:
        if case.must_fail:
            result = evaluate_case(case, provider, revisions_remaining=0)
            assert result.verdict is Verdict.BLOCK, case.id


def test_report_renders(provider):
    text = report(run_suite(provider))
    assert "cases:" in text and "missed:" in text and "false alarms:" in text
