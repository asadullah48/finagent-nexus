"""Deterministic compliance checks.

These are the principles that arithmetic can settle. They run *before* the model
sees anything, and their findings enter the review as facts rather than
opinions. Two consequences worth stating plainly:

1. A model cannot talk its way past a 31% debt ratio.
2. An auditor can reproduce these findings with no API key and no Claude call —
   :func:`run_machine_checks` is a pure function of the recommendation and the
   reference data.

Every check returns a :class:`PrincipleFinding` so machine and model findings
share one shape downstream.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from finagent_nexus.constitution import (
    MAX_SINGLE_POSITION_PCT,
    PROHIBITED_SECTORS,
    SHARIA_THRESHOLDS,
    applicable_principles,
)
from finagent_nexus.state import (
    ClientRequest,
    FindingStatus,
    Mandate,
    PrincipleFinding,
    Recommendation,
)
from finagent_nexus.tools.market_data import MarketDataProvider, ScreeningData

GUARANTEE_PATTERNS = (
    r"\bguarantee(?:d|s)?\b",
    r"\brisk[-\s]?free\b",
    r"\bassured\s+return\b",
    r"\bno\s+downside\b",
    r"\bzero\s+risk\b",
)

CAPITAL_AT_RISK_PATTERN = (
    r"capital (?:is )?at risk|capital may|may (?:fall|lose)|"
    r"value can go down|principal at risk|losses? (?:are|is) possible"
)

WEIGHT_TOLERANCE_PCT = 0.5

CheckFn = Callable[[Recommendation, Mapping[str, ScreeningData], ClientRequest], PrincipleFinding]


def _ok(principle_id: str, rationale: str, evidence: str) -> PrincipleFinding:
    return PrincipleFinding(
        principle_id=principle_id,
        status=FindingStatus.PASS,
        rationale=rationale,
        evidence=evidence,
        remediation="",
    )


def _fail(principle_id: str, rationale: str, evidence: str, remediation: str) -> PrincipleFinding:
    return PrincipleFinding(
        principle_id=principle_id,
        status=FindingStatus.FAIL,
        rationale=rationale,
        evidence=evidence,
        remediation=remediation,
    )


def _unverifiable(principle_id: str, rationale: str) -> PrincipleFinding:
    return PrincipleFinding(
        principle_id=principle_id,
        status=FindingStatus.UNVERIFIABLE,
        rationale=rationale,
        evidence="",
        remediation="Supply reference data for the affected instruments and re-run.",
    )


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #
def check_weights_sum(
    recommendation: Recommendation,
    screening: Mapping[str, ScreeningData],
    request: ClientRequest,
) -> PrincipleFinding:
    total = recommendation.total_weight_pct()
    negatives = [a.symbol for a in recommendation.allocations if a.weight_pct < 0]
    if negatives:
        return _fail(
            "REG-ALLOC-03",
            "Negative weights imply short positions, which this mandate does not permit.",
            f"Negative weights on: {', '.join(negatives)}",
            "Remove short positions and rebuild the allocation long-only.",
        )
    if abs(total - 100.0) > WEIGHT_TOLERANCE_PCT:
        return _fail(
            "REG-ALLOC-03",
            "Allocation weights do not sum to 100%.",
            f"Weights sum to {total:.2f}%.",
            f"Rescale allocations to total 100% (currently off by {total - 100.0:+.2f} pp).",
        )
    return _ok("REG-ALLOC-03", "Weights are long-only and sum to 100%.", f"Total = {total:.2f}%.")


def check_concentration(
    recommendation: Recommendation,
    screening: Mapping[str, ScreeningData],
    request: ClientRequest,
) -> PrincipleFinding:
    breaches = [
        (a.symbol, a.weight_pct)
        for a in recommendation.allocations
        if a.weight_pct > MAX_SINGLE_POSITION_PCT
    ]
    if breaches:
        detail = ", ".join(f"{sym} at {w:.1f}%" for sym, w in breaches)
        return _fail(
            "REG-CONC-02",
            f"One or more positions exceed the {MAX_SINGLE_POSITION_PCT:.0f}% single-name limit.",
            detail,
            f"Trim {detail} to at most {MAX_SINGLE_POSITION_PCT:.0f}% and redistribute.",
        )
    largest = max((a.weight_pct for a in recommendation.allocations), default=0.0)
    return _ok(
        "REG-CONC-02",
        "All positions are within the single-name concentration limit.",
        f"Largest position = {largest:.1f}% (limit {MAX_SINGLE_POSITION_PCT:.0f}%).",
    )


def check_guaranteed_language(
    recommendation: Recommendation,
    screening: Mapping[str, ScreeningData],
    request: ClientRequest,
) -> PrincipleFinding:
    corpus = " ".join(
        [recommendation.summary, *recommendation.disclosures]
        + [a.rationale for a in recommendation.allocations]
    )
    hits = [p for p in GUARANTEE_PATTERNS if re.search(p, corpus, flags=re.IGNORECASE)]
    if hits:
        return _fail(
            "REG-DISC-04",
            "Recommendation contains guarantee or risk-free language.",
            f"Matched patterns: {', '.join(hits)}",
            "Remove guarantee wording and state plainly that capital is at risk.",
        )
    if not re.search(CAPITAL_AT_RISK_PATTERN, corpus, flags=re.IGNORECASE):
        return _fail(
            "REG-DISC-04",
            "No capital-at-risk disclosure is present.",
            "Disclosures do not mention that capital is at risk.",
            "Add an explicit capital-at-risk disclosure to `disclosures`.",
        )
    return _ok(
        "REG-DISC-04",
        "Capital-at-risk disclosure present and no guarantee language detected.",
        "Disclosure text matched the required capital-at-risk pattern.",
    )


def check_prohibited_sector(
    recommendation: Recommendation,
    screening: Mapping[str, ScreeningData],
    request: ClientRequest,
) -> PrincipleFinding:
    missing = [a.symbol for a in recommendation.allocations if a.symbol not in screening]
    if missing:
        return _unverifiable("SHARIA-SECTOR-05", f"No screening data for: {', '.join(missing)}.")
    breaches = [
        f"{a.symbol} ({screening[a.symbol].sector})"
        for a in recommendation.allocations
        if screening[a.symbol].sector in PROHIBITED_SECTORS
    ]
    if breaches:
        return _fail(
            "SHARIA-SECTOR-05",
            "Allocation includes issuers in prohibited business activities.",
            ", ".join(breaches),
            f"Remove {', '.join(breaches)} and substitute compliant instruments.",
        )
    sectors = sorted({screening[a.symbol].sector for a in recommendation.allocations})
    return _ok(
        "SHARIA-SECTOR-05",
        "No allocation is to a prohibited sector.",
        f"Sectors held: {', '.join(sectors)}.",
    )


def _ratio_check(principle_id: str, attribute: str, threshold_key: str, label: str) -> CheckFn:
    """Build one of the three AAOIFI ratio screens.

    The screens differ only in which field they read and which threshold they
    compare against, so they are generated rather than copy-pasted — one place to
    fix if the ratio semantics change (e.g. moving from market cap to a 36-month
    trailing average).
    """
    threshold = SHARIA_THRESHOLDS[threshold_key]

    def _check(
        recommendation: Recommendation,
        screening: Mapping[str, ScreeningData],
        request: ClientRequest,
    ) -> PrincipleFinding:
        missing = [a.symbol for a in recommendation.allocations if a.symbol not in screening]
        if missing:
            return _unverifiable(principle_id, f"No screening data for: {', '.join(missing)}.")
        breaches = [
            f"{a.symbol} at {getattr(screening[a.symbol], attribute):.1f}%"
            for a in recommendation.allocations
            if getattr(screening[a.symbol], attribute) > threshold
        ]
        if breaches:
            symbols = ", ".join(b.split(" at ")[0] for b in breaches)
            return _fail(
                principle_id,
                f"{label} exceeds the {threshold:.0f}% AAOIFI threshold.",
                ", ".join(breaches),
                f"Replace {symbols} with instruments whose {label.lower()} is at or "
                f"below {threshold:.0f}%.",
            )
        worst = max(
            (getattr(screening[a.symbol], attribute) for a in recommendation.allocations),
            default=0.0,
        )
        return _ok(
            principle_id,
            f"{label} is within the AAOIFI threshold for every holding.",
            f"Highest observed = {worst:.1f}% (threshold {threshold:.0f}%).",
        )

    return _check


check_debt_ratio = _ratio_check(
    "SHARIA-SCREEN-02",
    "debt_to_market_cap_pct",
    "max_debt_to_market_cap_pct",
    "Debt to market cap",
)
check_interest_securities_ratio = _ratio_check(
    "SHARIA-SCREEN-03",
    "interest_bearing_securities_pct",
    "max_interest_bearing_securities_pct",
    "Interest-bearing securities to market cap",
)
check_impure_revenue_ratio = _ratio_check(
    "SHARIA-SCREEN-04",
    "non_compliant_revenue_pct",
    "max_non_compliant_revenue_pct",
    "Non-compliant revenue share",
)


def check_riba_bearing_instruments(
    recommendation: Recommendation,
    screening: Mapping[str, ScreeningData],
    request: ClientRequest,
) -> PrincipleFinding:
    missing = [a.symbol for a in recommendation.allocations if a.symbol not in screening]
    if missing:
        return _unverifiable("SHARIA-RIBA-01", f"No screening data for: {', '.join(missing)}.")
    breaches = [
        f"{a.symbol} ({screening[a.symbol].instrument_kind})"
        for a in recommendation.allocations
        if screening[a.symbol].is_riba_bearing
    ]
    if breaches:
        symbols = ", ".join(b.split(" (")[0] for b in breaches)
        return _fail(
            "SHARIA-RIBA-01",
            "Allocation includes interest-bearing instruments.",
            ", ".join(breaches),
            f"Replace {symbols} with sukuk or asset-backed alternatives.",
        )
    kinds = sorted({screening[a.symbol].instrument_kind for a in recommendation.allocations})
    return _ok(
        "SHARIA-RIBA-01",
        "No interest-bearing instrument is held.",
        f"Instrument kinds held: {', '.join(kinds)}.",
    )


#: Maps ``Principle.check`` ids to their implementations.
CHECK_REGISTRY: dict[str, CheckFn] = {
    "weights_sum": check_weights_sum,
    "concentration": check_concentration,
    "guaranteed_language": check_guaranteed_language,
    "prohibited_sector": check_prohibited_sector,
    "debt_ratio": check_debt_ratio,
    "interest_securities_ratio": check_interest_securities_ratio,
    "impure_revenue_ratio": check_impure_revenue_ratio,
    "riba_bearing_instruments": check_riba_bearing_instruments,
}


def run_machine_checks(
    recommendation: Recommendation,
    request: ClientRequest,
    provider: MarketDataProvider,
) -> list[PrincipleFinding]:
    """Run every deterministic principle in force for this mandate.

    Only reads reference data from ``provider``, so the whole function is
    reproducible offline given a snapshot of that data.
    """
    screening: dict[str, ScreeningData] = {}
    for allocation in recommendation.allocations:
        data = provider.get_screening_data(allocation.symbol)
        if data is not None:
            screening[allocation.symbol] = data

    findings: list[PrincipleFinding] = []
    for principle in applicable_principles(request.mandate, request.jurisdiction):
        if not principle.is_deterministic:
            continue
        check = CHECK_REGISTRY.get(principle.check)
        if check is None:  # pragma: no cover — guards a typo in the constitution
            findings.append(
                _unverifiable(
                    principle.id, f"No implementation registered for check {principle.check!r}."
                )
            )
            continue
        findings.append(check(recommendation, screening, request))
    return findings


def deterministic_principle_ids(mandate: Mandate, jurisdiction: str | None = None) -> set[str]:
    """Ids the model must NOT be asked to judge — arithmetic already settled them."""
    return {p.id for p in applicable_principles(mandate, jurisdiction) if p.is_deterministic}
