"""Verdict policy.

``aggregate_verdict`` is the institution's risk appetite expressed as code, so
this file is effectively the policy's specification. If a risk committee changes
its mind about what a material breach warrants, the change lands here first and
these tests are what prove it took effect.
"""

from __future__ import annotations

from finagent_nexus.agents import aggregate_verdict
from finagent_nexus.state import FindingStatus, PrincipleFinding, Verdict


def finding(
    principle_id: str,
    status: FindingStatus = FindingStatus.FAIL,
    remediation: str = "Do the fix.",
) -> PrincipleFinding:
    return PrincipleFinding(
        principle_id=principle_id,
        status=status,
        rationale="test rationale",
        evidence="test evidence",
        remediation=remediation if status is FindingStatus.FAIL else "",
    )


# --------------------------------------------------------------------------- #
# Clean paths
# --------------------------------------------------------------------------- #
def test_all_passing_yields_pass():
    findings = [
        finding("SHARIA-RIBA-01", FindingStatus.PASS),
        finding("REG-CONC-02", FindingStatus.PASS),
    ]
    verdict, blocking, _ = aggregate_verdict(findings, revisions_remaining=2)
    assert verdict is Verdict.PASS
    assert blocking == []


def test_empty_findings_yield_pass():
    verdict, blocking, remediations = aggregate_verdict([], revisions_remaining=2)
    assert verdict is Verdict.PASS
    assert blocking == [] and remediations == []


# --------------------------------------------------------------------------- #
# Severity handling
# --------------------------------------------------------------------------- #
def test_blocking_failure_with_remediation_requests_revision():
    verdict, blocking, remediations = aggregate_verdict(
        [finding("SHARIA-RIBA-01")], revisions_remaining=2
    )
    assert verdict is Verdict.REVISE
    assert any("SHARIA-RIBA-01" in item for item in blocking)
    assert any("SHARIA-RIBA-01" in item for item in remediations)


def test_blocking_failure_without_remediation_blocks_immediately():
    """No remediation means another pass cannot help — do not burn the budget."""
    verdict, _, _ = aggregate_verdict(
        [finding("SHARIA-RIBA-01", remediation="")], revisions_remaining=2
    )
    assert verdict is Verdict.BLOCK


def test_exhausted_revision_budget_escalates():
    verdict, _, _ = aggregate_verdict([finding("SHARIA-RIBA-01")], revisions_remaining=0)
    assert verdict is Verdict.BLOCK


def test_material_failure_requests_revision_then_blocks():
    assert aggregate_verdict([finding("REG-CONC-02")], revisions_remaining=1)[0] is Verdict.REVISE
    assert aggregate_verdict([finding("REG-CONC-02")], revisions_remaining=0)[0] is Verdict.BLOCK


def test_advisory_failure_never_changes_the_verdict():
    verdict, blocking, remediations = aggregate_verdict(
        [finding("SHARIA-PURIFY-07")], revisions_remaining=0
    )
    assert verdict is Verdict.PASS
    assert blocking == []
    # The caveat is still surfaced, it just does not gate approval.
    assert any("SHARIA-PURIFY-07" in item for item in remediations)


# --------------------------------------------------------------------------- #
# Fail-closed behaviour
# --------------------------------------------------------------------------- #
def test_unverifiable_blocking_principle_counts_as_failure():
    """The single most important line of policy: unproven is not approved."""
    verdict, blocking, _ = aggregate_verdict(
        [finding("SHARIA-RIBA-01", FindingStatus.UNVERIFIABLE)], revisions_remaining=2
    )
    assert verdict is Verdict.REVISE
    assert blocking, "an unverifiable blocking principle must be reported as a failure"


def test_unverifiable_blocking_principle_blocks_when_budget_spent():
    verdict, _, _ = aggregate_verdict(
        [finding("SHARIA-RIBA-01", FindingStatus.UNVERIFIABLE)], revisions_remaining=0
    )
    assert verdict is Verdict.BLOCK


def test_unverifiable_advisory_principle_is_tolerated():
    verdict, _, _ = aggregate_verdict(
        [finding("SHARIA-PURIFY-07", FindingStatus.UNVERIFIABLE)], revisions_remaining=0
    )
    assert verdict is Verdict.PASS


def test_unknown_principle_id_defaults_to_material():
    """An id absent from the constitution must not silently become harmless."""
    verdict, blocking, _ = aggregate_verdict(
        [finding("NOT-A-REAL-PRINCIPLE")], revisions_remaining=0
    )
    assert verdict is Verdict.BLOCK
    assert blocking


# --------------------------------------------------------------------------- #
# Mixed severities
# --------------------------------------------------------------------------- #
def test_unfixable_blocking_wins_over_remediable_failures():
    findings = [
        finding("REG-CONC-02"),
        finding("SHARIA-RIBA-01", remediation=""),
    ]
    verdict, _, _ = aggregate_verdict(findings, revisions_remaining=2)
    assert verdict is Verdict.BLOCK


def test_passes_alongside_failures_do_not_dilute_the_verdict():
    findings = [
        finding("SHARIA-SCREEN-02", FindingStatus.PASS),
        finding("SHARIA-SCREEN-03", FindingStatus.PASS),
        finding("REG-CONC-02"),
    ]
    verdict, blocking, _ = aggregate_verdict(findings, revisions_remaining=2)
    assert verdict is Verdict.REVISE
    assert len(blocking) == 1
