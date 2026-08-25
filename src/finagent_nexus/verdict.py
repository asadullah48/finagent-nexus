"""Verdict policy — the institution's risk appetite, expressed as code.

This module is deliberately separate from :mod:`finagent_nexus.agents.compliance_officer`,
and the reason is the determinism boundary the whole system is built on.

Mapping findings to PASS / REVISE / BLOCK involves no model. It is arithmetic
over severities, and it is the single most consequential policy decision an
institution makes when adopting the system. Leaving it inside the agent module
meant importing it dragged in :mod:`finagent_nexus.llm` and therefore the
Anthropic SDK — so the deterministic half of the system could not actually be
run, deployed, or audited without the model half installed alongside it.

That is a boundary that holds on paper and leaks in the dependency graph. Here
the split is real: this module imports the constitution and the state contracts
and nothing else, which is what lets the screening endpoint, the eval harness,
and a regulator's offline re-run all execute with no API client present.

:func:`aggregate_verdict` remains importable from its original locations, so
existing callers are unaffected.
"""

from __future__ import annotations

from finagent_nexus.constitution import by_id
from finagent_nexus.state import (
    FindingStatus,
    PrincipleFinding,
    Severity,
    Verdict,
)

__all__ = ["aggregate_verdict"]


def _severity_of(principle_id: str) -> Severity:
    principle = by_id(principle_id)
    return principle.severity if principle else Severity.MATERIAL


def aggregate_verdict(
    findings: list[PrincipleFinding], revisions_remaining: int
) -> tuple[Verdict, list[str], list[str]]:
    """Map findings to a verdict. **This is the institution's risk policy in code.**

    The default policy encoded here:

    * ``UNVERIFIABLE`` on a blocking or material principle counts as a failure.
      The system fails closed — an unproven claim is not an approved one.
    * A **blocking** ``FAIL`` with no remediation offered is unfixable by another
      pass, so it terminates the run immediately with ``BLOCK``. Note the
      restriction to ``FAIL``: an ``UNVERIFIABLE`` finding is remediable by
      definition — the fix is to obtain the missing evidence — so it never takes
      this shortcut, even when it arrives without remediation text.
    * Any other blocking or material failure returns ``REVISE`` while revision
      budget remains, and ``BLOCK`` once it is exhausted. A defect that survives
      the revision budget goes to a human, never to the client.
    * **Advisory** failures never change the verdict; they are surfaced as
      caveats attached to the approved recommendation.

    Every institution's second line will want to tune this — a private bank may
    allow a material breach to pass with sign-off, a retail platform almost
    certainly will not. It is one function, with one test file, precisely so that
    tuning it is a reviewable change rather than a prompt edit.

    Returns:
        ``(verdict, blocking_failures, remediations)``.
    """
    blocking_failures: list[str] = []
    remediations: list[str] = []
    unfixable = False
    material_failure = False

    for finding in findings:
        severity = _severity_of(finding.principle_id)
        failed = finding.status is FindingStatus.FAIL or (
            finding.status is FindingStatus.UNVERIFIABLE and severity is not Severity.ADVISORY
        )
        if not failed:
            continue

        summary = f"[{severity.value}] {finding.principle_id}: {finding.rationale}"
        if finding.remediation:
            remediations.append(f"{finding.principle_id}: {finding.remediation}")

        if severity is Severity.BLOCKING:
            blocking_failures.append(summary)
            # Only a definite FAIL can be unfixable. An UNVERIFIABLE finding
            # means "not proven", and the remedy is more evidence, not surrender.
            if finding.status is FindingStatus.FAIL and not finding.remediation:
                unfixable = True
        elif severity is Severity.MATERIAL:
            blocking_failures.append(summary)
            material_failure = True

    if unfixable:
        return Verdict.BLOCK, blocking_failures, remediations
    if blocking_failures or material_failure:
        if revisions_remaining > 0:
            return Verdict.REVISE, blocking_failures, remediations
        return Verdict.BLOCK, blocking_failures, remediations
    return Verdict.PASS, [], remediations
