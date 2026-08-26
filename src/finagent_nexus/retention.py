"""What the institution notarises at the moment of issuance.

The hash chain in :mod:`finagent_nexus.audit` makes the record tamper-evident.
It says nothing about whether the record is *sufficient*, and those are different
questions. A chain over an incomplete record proves, very rigorously, that an
incomplete record has not been altered.

That was the gap here. ``finalize`` recorded the outcome, the revision count and
the verdict; ``synthesize`` recorded ``{symbol: weight}``; ``verify`` recorded
``{principle_id: status}``. Six months later that trail can prove nobody edited
it, and can tell you a portfolio was approved at 70/30 with SHARIA-SCREEN-02
passing. It cannot tell you **what the client was actually told** — the summary,
the rationale for each holding, the disclosures, or the reasoning behind a
finding. In a complaint, a thematic review, or a suitability challenge, that is
precisely the part that gets asked for.

So the issuance record is its own function in its own module, for the same
reason :func:`~finagent_nexus.verdict.aggregate_verdict` is: it is a policy an
institution must be able to read, argue about, and change without touching
orchestration code. It imports the state contracts and the digest helper, and
nothing else.

**This is a records-retention decision, not a technical one.** Recording the full
client-facing prose maximises evidential value and is what a conduct regulator
generally expects to find. It also means the audit trail now holds the advice
itself, which changes its retention class, its access controls and its
data-protection footprint. Institutions that keep advice in a separate system of
record may prefer the digest-only variant described in :func:`issuance_record`.
Both are a two-line change at one site.
"""

from __future__ import annotations

from typing import Any

from finagent_nexus.audit import digest
from finagent_nexus.state import ComplianceReview, Recommendation

__all__ = ["issuance_record"]


def issuance_record(
    recommendation: Recommendation | None,
    review: ComplianceReview | None,
) -> dict[str, Any]:
    """Build the ``detail`` payload notarised when a decision is issued.

    Called once, at ``finalize``. Intermediate revisions stay summarised: a
    rejected draft is evidence of process, but only the issued artefact is
    evidence of advice, and recording full prose on every loop would multiply the
    trail by the revision budget for no evidential gain.

    The default policy below records the **complete issued artefact**:

    * the recommendation as rendered to the client — summary, every allocation
      with its rationale, expected figures, review cadence and disclosures;
    * every finding with its rationale, evidence and remediation, not merely its
      status;
    * a ``recommendation_digest``, so an advice document held outside this system
      can be proved to be the one that was issued.

    The alternative, for institutions that do not want advice prose inside the
    audit store, is to keep ``recommendation_digest`` and drop the
    ``recommendation`` body: integrity without content. That trades the ability
    to *reconstruct* the advice for the ability only to *verify* a copy you
    already hold — sufficient when the advice is retained elsewhere, and
    insufficient when it is not.

    Args:
        recommendation: The issued portfolio, or ``None`` if the run halted
            before one existed.
        review: The final compliance review, or ``None`` if none was reached.

    Returns:
        A JSON-serialisable payload for the ``finalize`` audit event.
    """
    record: dict[str, Any] = {}

    if recommendation is not None:
        body = recommendation.model_dump(mode="json")
        record["recommendation"] = body
        # Computed over the same canonical encoding the chain uses, so a party
        # holding the advice document can recompute this without our code.
        record["recommendation_digest"] = digest(body)

    if review is not None:
        record["findings_detail"] = [
            {
                "principle_id": finding.principle_id,
                "status": finding.status.value,
                "rationale": finding.rationale,
                "evidence": finding.evidence,
                "remediation": finding.remediation,
            }
            for finding in review.findings
        ]

    return record
