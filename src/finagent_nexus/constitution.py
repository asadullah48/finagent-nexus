"""The written constitution the ComplianceOfficer reviews against.

Constitutional AI works by giving a model an explicit, enumerated set of
principles and asking it to critique a candidate output against each one, then
revise. The value for financial services is that the constitution is a
*versioned artefact*: a Sharia board or a compliance function reviews this file,
signs it off, and any change to it appears in a diff.

Each principle carries:

* ``severity``  — drives the deterministic verdict (see ``aggregate_verdict``)
* ``check``     — the id of a deterministic check in :mod:`finagent_nexus.checks`,
                  or an empty string when the principle is genuinely qualitative
* ``source``    — the standard it derives from, so a finding is citable

Sharia screening thresholds follow the AAOIFI Shari'ah Standard No. 21
(Financial Paper) ratios in common use for equity screening. Institutions using
a different board — e.g. the Dow Jones Islamic Market or S&P Shariah
methodologies, which screen debt against a 36-month average market cap rather
than total assets — should edit :data:`SHARIA_THRESHOLDS` and re-run the eval
harness before signing off.
"""

from __future__ import annotations

from dataclasses import dataclass

from finagent_nexus.state import Mandate, Severity

# --------------------------------------------------------------------------- #
# Screening thresholds — the numbers a Sharia board actually signs off on.
# --------------------------------------------------------------------------- #
SHARIA_THRESHOLDS: dict[str, float] = {
    "max_debt_to_market_cap_pct": 30.0,
    "max_interest_bearing_securities_pct": 30.0,
    "max_non_compliant_revenue_pct": 5.0,
}

PROHIBITED_SECTORS: frozenset[str] = frozenset(
    {
        "conventional_banking",
        "conventional_insurance",
        "alcohol",
        "tobacco",
        "gambling",
        "adult_entertainment",
        "pork",
        "weapons",
    }
)

#: Regulatory concentration ceiling for a single line item in a discretionary
#: mandate. Institutions with a stricter house limit should lower this.
MAX_SINGLE_POSITION_PCT = 25.0


@dataclass(frozen=True)
class Principle:
    id: str
    title: str
    source: str
    severity: Severity
    rule: str
    mandates: frozenset[Mandate]
    check: str = ""
    jurisdictions: frozenset[str] | None = None  # None == all jurisdictions

    @property
    def is_deterministic(self) -> bool:
        return bool(self.check)


_BOTH = frozenset({Mandate.SHARIA, Mandate.CONVENTIONAL})
_SHARIA_ONLY = frozenset({Mandate.SHARIA})


CONSTITUTION: tuple[Principle, ...] = (
    # ---------------------------- Sharia ---------------------------------- #
    Principle(
        id="SHARIA-RIBA-01",
        title="Prohibition of riba (interest)",
        source="AAOIFI Shari'ah Standard No. 21; Qur'an 2:275",
        severity=Severity.BLOCKING,
        mandates=_SHARIA_ONLY,
        check="riba_bearing_instruments",
        rule=(
            "No allocation may be made to an instrument whose return derives from "
            "interest, including conventional bonds, treasury notes, money-market "
            "funds, certificates of deposit, and interest-bearing cash sweeps. "
            "Sukuk structured on asset ownership are permitted."
        ),
    ),
    Principle(
        id="SHARIA-SCREEN-02",
        title="Leverage screen",
        source="AAOIFI Shari'ah Standard No. 21 §3/4",
        severity=Severity.BLOCKING,
        mandates=_SHARIA_ONLY,
        check="debt_ratio",
        rule=(
            f"Interest-bearing debt must not exceed "
            f"{SHARIA_THRESHOLDS['max_debt_to_market_cap_pct']:.0f}% of market capitalisation."
        ),
    ),
    Principle(
        id="SHARIA-SCREEN-03",
        title="Interest-bearing securities screen",
        source="AAOIFI Shari'ah Standard No. 21 §3/4",
        severity=Severity.BLOCKING,
        mandates=_SHARIA_ONLY,
        check="interest_securities_ratio",
        rule=(
            f"Interest-bearing securities and deposits must not exceed "
            f"{SHARIA_THRESHOLDS['max_interest_bearing_securities_pct']:.0f}% of "
            "market capitalisation."
        ),
    ),
    Principle(
        id="SHARIA-SCREEN-04",
        title="Impure revenue screen",
        source="AAOIFI Shari'ah Standard No. 21 §3/4",
        severity=Severity.BLOCKING,
        mandates=_SHARIA_ONLY,
        check="impure_revenue_ratio",
        rule=(
            f"Revenue from non-compliant activities must not exceed "
            f"{SHARIA_THRESHOLDS['max_non_compliant_revenue_pct']:.0f}% of total revenue."
        ),
    ),
    Principle(
        id="SHARIA-SECTOR-05",
        title="Prohibited business activity",
        source="AAOIFI Shari'ah Standard No. 21 §3/1",
        severity=Severity.BLOCKING,
        mandates=_SHARIA_ONLY,
        check="prohibited_sector",
        rule=(
            "No allocation to issuers whose primary activity is conventional banking "
            "or insurance, alcohol, tobacco, gambling, adult entertainment, pork "
            "products, or weapons manufacture."
        ),
    ),
    Principle(
        id="SHARIA-GHARAR-06",
        title="Prohibition of excessive gharar (uncertainty)",
        source="AAOIFI Shari'ah Standard No. 31",
        severity=Severity.MATERIAL,
        mandates=_SHARIA_ONLY,
        rule=(
            "The recommendation must not rely on conventional options, futures, "
            "swaps, short selling, or any structure where the subject matter or "
            "price is materially uncertain at contract time."
        ),
    ),
    Principle(
        id="SHARIA-PURIFY-07",
        title="Purification of incidental impure income",
        source="AAOIFI Shari'ah Standard No. 21 §3/4/4",
        severity=Severity.ADVISORY,
        mandates=_SHARIA_ONLY,
        rule=(
            "Where an otherwise-compliant holding earns incidental non-compliant "
            "income, the recommendation must disclose the purification obligation "
            "and state how the amount is calculated."
        ),
    ),
    # -------------------------- Regulatory -------------------------------- #
    Principle(
        id="REG-SUIT-01",
        title="Suitability",
        source="MiFID II Art. 25(2); SAMA Investment Accounts Rules; SCA Rulebook",
        severity=Severity.BLOCKING,
        mandates=_BOTH,
        rule=(
            "The allocation's risk profile must be consistent with the client's "
            "stated risk tolerance and time horizon. A 'conservative' mandate may "
            "not be met with a majority-equity allocation; a 1-3 year horizon may "
            "not be met with illiquid or high-volatility concentration."
        ),
    ),
    Principle(
        id="REG-CONC-02",
        title="Single-position concentration limit",
        source="UCITS 5/10/40 rule; house discretionary mandate limits",
        severity=Severity.MATERIAL,
        mandates=_BOTH,
        check="concentration",
        rule=f"No single instrument may exceed {MAX_SINGLE_POSITION_PCT:.0f}% of the portfolio.",
    ),
    Principle(
        id="REG-ALLOC-03",
        title="Allocation integrity",
        source="Internal control standard",
        severity=Severity.BLOCKING,
        mandates=_BOTH,
        check="weights_sum",
        rule="Allocation weights must sum to 100% (±0.5%) and none may be negative.",
    ),
    Principle(
        id="REG-DISC-04",
        title="Risk disclosure and absence of guarantees",
        source="MiFID II Art. 24(3); SAMA Market Conduct Regulation",
        severity=Severity.BLOCKING,
        mandates=_BOTH,
        check="guaranteed_language",
        rule=(
            "The recommendation must carry a capital-at-risk disclosure and must "
            "not contain guarantee language ('guaranteed', 'risk-free', 'assured "
            "return', 'no downside')."
        ),
    ),
    Principle(
        id="REG-EVID-05",
        title="Evidential traceability",
        source="EU AI Act Art. 12 (record-keeping); SR 11-7 model risk management",
        severity=Severity.MATERIAL,
        mandates=_BOTH,
        rule=(
            "Every quantitative claim in the brief and the recommendation must trace "
            "to a recorded tool call. Figures that appear without a corresponding "
            "evidence line are unverifiable and must be flagged."
        ),
    ),
    Principle(
        id="REG-FAIR-06",
        title="Fair, clear and not misleading",
        source="FCA COBS 4.2; SCA Rulebook Art. 32",
        severity=Severity.MATERIAL,
        mandates=_BOTH,
        rule=(
            "Past performance must not be presented as indicative of future results. "
            "Projections must be labelled as estimates. Risks must be presented with "
            "the same prominence as benefits."
        ),
    ),
    Principle(
        id="REG-AML-07",
        title="Sanctions and financial-crime exposure",
        source="FATF Recommendations; OFAC/UN consolidated lists",
        severity=Severity.BLOCKING,
        mandates=_BOTH,
        rule=(
            "No allocation may involve a sanctioned jurisdiction, entity, or an "
            "instrument whose issuer is subject to an active enforcement action."
        ),
    ),
)


#: Principles whose remediation requires *new market evidence* rather than a
#: rebuild of the allocation. A breach of one of these sends the run back to the
#: MarketAnalyst; everything else goes back to the WealthStrategist, which is
#: cheaper and does not re-open settled evidence.
#:
#: Routing lives here, beside the rules, so that adding a principle forces an
#: explicit decision about who owns fixing it.
REQUIRES_NEW_EVIDENCE: frozenset[str] = frozenset(
    {
        "SHARIA-RIBA-01",
        "SHARIA-SCREEN-02",
        "SHARIA-SCREEN-03",
        "SHARIA-SCREEN-04",
        "SHARIA-SECTOR-05",
        "REG-EVID-05",
        "REG-AML-07",
    }
)


def applicable_principles(
    mandate: Mandate, jurisdiction: str | None = None
) -> tuple[Principle, ...]:
    """Principles in force for a given mandate and jurisdiction."""
    where = jurisdiction.upper() if jurisdiction else None
    result = []
    for principle in CONSTITUTION:
        if mandate not in principle.mandates:
            continue
        # A principle with `jurisdictions = None` is universal, and an unknown
        # caller jurisdiction narrows nothing — in both cases the principle
        # stays in force. Scoping here is only ever *subtractive against a
        # known pair*: no combination of inputs can drop a universal principle.
        # That is the property that matters, because this function decides what
        # gets reviewed at all.
        scoped_out = (
            principle.jurisdictions is not None
            and where is not None
            and where not in principle.jurisdictions
        )
        if scoped_out:
            continue
        result.append(principle)
    return tuple(result)


def by_id(principle_id: str) -> Principle | None:
    return next((p for p in CONSTITUTION if p.id == principle_id), None)


def render_for_prompt(principles: tuple[Principle, ...]) -> str:
    """Render the constitution as the stable prefix of the review prompt.

    Kept deterministic — no timestamps, no shuffling — so it caches cleanly.
    This block is the same bytes on every call and is the natural prompt-cache
    breakpoint for the review node.
    """
    lines = []
    for p in principles:
        marker = " [machine-checked]" if p.is_deterministic else ""
        lines.append(
            f"{p.id} — {p.title} ({p.severity.value}){marker}\n"
            f"  Source: {p.source}\n"
            f"  Rule: {p.rule}"
        )
    return "\n\n".join(lines)
