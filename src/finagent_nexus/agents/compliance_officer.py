"""ComplianceOfficer — Constitutional AI review.

The agent runs the classic constitutional loop (critique against enumerated
principles, then feed the critique back for revision) with two hard
modifications for a regulated setting:

* **Arithmetic is settled before the model speaks.** Deterministic principles are
  evaluated in :mod:`finagent_nexus.checks` and their findings are injected as
  facts. The model is not asked to re-judge them and is told so explicitly.
* **The model does not render the verdict.** It emits per-principle findings;
  :func:`aggregate_verdict` maps findings to PASS / REVISE / BLOCK in Python.
  Verdict policy is therefore reviewable in a pull request, unit-testable, and
  identical on every run.
"""

from __future__ import annotations

from finagent_nexus.checks import run_machine_checks
from finagent_nexus.constitution import applicable_principles, by_id, render_for_prompt
from finagent_nexus.llm import ClaudeClient, compact_json
from finagent_nexus.state import (
    ClientRequest,
    ComplianceFindings,
    ComplianceReview,
    FindingStatus,
    MarketBrief,
    PrincipleFinding,
    Recommendation,
    Severity,
    Verdict,
)
from finagent_nexus.tools.market_data import MarketDataProvider

SYSTEM_PROMPT_HEADER = """You are ComplianceOfficer, the second line of defence in a \
regulated financial institution. You review a proposed investment recommendation against a \
written constitution and report findings. You are not the author's colleague and not their \
editor — your job is to find the defect that would embarrass this firm in front of a \
regulator or a Shari'ah board.

RULES OF REVIEW

1. Judge ONLY the principles listed below as requiring your judgement. Principles marked \
   [machine-checked] have already been settled by deterministic code; their findings are \
   supplied to you as established facts. Do not re-litigate them, do not restate them, and \
   do not include them in your output.
2. Return exactly one finding per principle you were asked to judge. No more, no fewer.
3. `status` is `pass`, `fail`, or `unverifiable`. Use `unverifiable` when the material in \
   front of you is genuinely insufficient to decide — not as a way to avoid a hard call. \
   Unverifiable findings on blocking principles are treated as failures downstream.
4. `evidence` must quote or cite the specific text, figure, or absence that drove your \
   status. "The allocation seems aggressive" is not evidence. "72% in equities against a \
   stated conservative tolerance" is.
5. `remediation` must be a concrete instruction the strategist can act on in one pass. \
   Leave it as an empty string when status is `pass`.
6. Do not emit an overall verdict, score, or recommendation to approve. That decision is \
   made outside you.

You are reviewing against this constitution:

"""


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


class ComplianceOfficer:
    """Applies the constitution to a candidate recommendation."""

    name = "ComplianceOfficer"

    def __init__(self, llm: ClaudeClient, provider: MarketDataProvider):
        self.llm = llm
        self.provider = provider

    def _build_system_prompt(self, request: ClientRequest) -> str:
        """Stable per-mandate prefix — identical bytes across revisions, so it caches."""
        principles = applicable_principles(request.mandate, request.jurisdiction)
        return SYSTEM_PROMPT_HEADER + render_for_prompt(principles)

    def review(
        self,
        request: ClientRequest,
        brief: MarketBrief,
        recommendation: Recommendation,
        revisions_remaining: int,
        revision: int = 0,
    ) -> tuple[ComplianceReview, dict[str, int], int]:
        """Run machine checks, then model critique, then derive the verdict.

        Returns:
            ``(review, usage, latency_ms)`` — usage and latency are passed
            straight to the audit trail by the caller.
        """
        machine_findings = run_machine_checks(recommendation, request, self.provider)

        principles = applicable_principles(request.mandate, request.jurisdiction)
        qualitative = [p for p in principles if not p.is_deterministic]

        if not qualitative:
            verdict, blocking, remediations = aggregate_verdict(
                machine_findings, revisions_remaining
            )
            review = ComplianceReview(
                verdict=verdict,
                findings=machine_findings,
                machine_findings=machine_findings,
                blocking_failures=blocking,
                remediations=remediations,
                reviewed_revision=revision,
            )
            return review, {}, 0

        machine_summary = "\n".join(
            f"- {f.principle_id}: {f.status.value.upper()} — {f.rationale} ({f.evidence})"
            for f in machine_findings
        ) or "- (no deterministic principles apply to this mandate)"

        user_prompt = "\n".join(
            [
                "CLIENT MANDATE",
                compact_json(request.model_dump(mode="json")),
                "",
                "MARKET BRIEF UNDER REVIEW",
                compact_json(brief.model_dump(mode="json")),
                "",
                "PROPOSED RECOMMENDATION UNDER REVIEW",
                compact_json(recommendation.model_dump(mode="json")),
                "",
                "ESTABLISHED FACTS FROM DETERMINISTIC CHECKS",
                "(Already decided. Use them as context; do not re-judge or repeat them.)",
                machine_summary,
                "",
                "PRINCIPLES REQUIRING YOUR JUDGEMENT",
                "Return exactly one finding for each of the following, and nothing else:",
                "\n".join(f"- {p.id} ({p.severity.value}): {p.title}" for p in qualitative),
            ]
        )

        result = self.llm.structured(
            system=self._build_system_prompt(request),
            user=user_prompt,
            output_model=ComplianceFindings,
            model=self.llm.settings.model,
        )

        model_findings = self._reconcile(result.value.findings, qualitative)
        all_findings = machine_findings + model_findings
        verdict, blocking, remediations = aggregate_verdict(all_findings, revisions_remaining)

        review = ComplianceReview(
            verdict=verdict,
            findings=all_findings,
            machine_findings=machine_findings,
            blocking_failures=blocking,
            remediations=remediations,
            reviewed_revision=revision,
        )
        return review, result.usage, result.latency_ms

    @staticmethod
    def _reconcile(
        findings: list[PrincipleFinding], expected: list
    ) -> list[PrincipleFinding]:
        """Drop findings for principles not requested; synthesise any that are missing.

        A model that skips a principle must not thereby cause it to pass. A
        silently absent finding becomes an explicit ``unverifiable``, which the
        verdict policy treats as a failure on anything above advisory.
        """
        expected_ids = [p.id for p in expected]
        by_principle = {
            f.principle_id: f for f in findings if f.principle_id in set(expected_ids)
        }
        reconciled: list[PrincipleFinding] = []
        for principle_id in expected_ids:
            found = by_principle.get(principle_id)
            if found is None:
                reconciled.append(
                    PrincipleFinding(
                        principle_id=principle_id,
                        status=FindingStatus.UNVERIFIABLE,
                        rationale="The reviewer returned no finding for this principle.",
                        evidence="",
                        remediation=(
                            "Re-run the review; if this recurs, the principle's wording is "
                            "likely ambiguous and should be revised."
                        ),
                    )
                )
            else:
                reconciled.append(found)
        return reconciled
