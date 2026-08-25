"""Typed state shared across the orchestration graph.

Two families of types live here:

* **Payload models** (:class:`Plan`, :class:`MarketBrief`, ...) — Pydantic models
  that are also the JSON-schema contract handed to Claude via structured
  outputs. Because the schema *is* the type, a malformed agent response fails at
  the boundary rather than three nodes later.
* **Graph state** (:class:`NexusState`) — the TypedDict LangGraph threads
  between nodes.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

from finagent_nexus.audit import AuditTrail


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class Mandate(str, Enum):
    """The rule set a portfolio must be built under."""

    SHARIA = "sharia"
    CONVENTIONAL = "conventional"


class Verdict(str, Enum):
    """Outcome of a compliance review.

    ``REVISE`` is deliberately distinct from ``BLOCK``: a remediable defect
    returns to the Act phase with feedback, while a blocking defect terminates
    the run and escalates to a human. Collapsing the two would let the system
    loop forever on an unfixable mandate breach.
    """

    PASS = "pass"
    REVISE = "revise"
    BLOCK = "block"


class Severity(str, Enum):
    """How badly a violated principle wounds the recommendation."""

    BLOCKING = "blocking"
    MATERIAL = "material"
    ADVISORY = "advisory"


class FindingStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNVERIFIABLE = "unverifiable"


RiskTolerance = Literal["conservative", "balanced", "growth", "aggressive"]


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
class ClientRequest(BaseModel):
    """The mandate handed to the system. This is the contract with the client."""

    client_id: str = Field(
        description="Pseudonymous client reference — never a name or account number."
    )
    objective: str = Field(description="What the client is trying to achieve, in their words.")
    capital_usd: float = Field(gt=0)
    horizon_years: int = Field(ge=1, le=50)
    risk_tolerance: RiskTolerance
    jurisdiction: str = Field(
        description="ISO-3166 alpha-2 of the regulating jurisdiction, e.g. SA, AE, GB."
    )
    mandate: Mandate = Mandate.SHARIA
    constraints: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Plan phase
# --------------------------------------------------------------------------- #
class PlanStep(BaseModel):
    id: str
    agent: Literal["MarketAnalyst", "ComplianceOfficer", "WealthStrategist"]
    objective: str
    success_criteria: str = Field(
        description="An observable condition. 'Looks reasonable' is not a success criterion."
    )


class Plan(BaseModel):
    thesis: str
    universe: list[str] = Field(description="Instrument symbols the analyst must examine.")
    steps: list[PlanStep]


# --------------------------------------------------------------------------- #
# Act phase
# --------------------------------------------------------------------------- #
class InstrumentView(BaseModel):
    symbol: str
    name: str
    asset_class: str
    thesis: str
    expected_return_pct: float
    volatility_pct: float
    confidence: float = Field(ge=0.0, le=1.0)


class MarketBrief(BaseModel):
    regime: str = Field(description="Current macro regime read, one or two sentences.")
    key_risks: list[str]
    views: list[InstrumentView]
    evidence: list[str] = Field(
        description="One line per tool call actually made, e.g. 'get_quote(2222.SR) -> 28.40 SAR'."
    )


# --------------------------------------------------------------------------- #
# Verify phase
# --------------------------------------------------------------------------- #
class PrincipleFinding(BaseModel):
    principle_id: str
    status: FindingStatus
    rationale: str
    evidence: str = Field(description="What in the brief or recommendation supports this status.")
    remediation: str = Field(description="Concrete fix if status is fail; empty string otherwise.")


class ComplianceFindings(BaseModel):
    """Raw model output. Deliberately carries no verdict — see ``aggregate_verdict``."""

    findings: list[PrincipleFinding]


class ComplianceReview(BaseModel):
    """Machine checks + model critique + the deterministically derived verdict."""

    verdict: Verdict
    findings: list[PrincipleFinding]
    machine_findings: list[PrincipleFinding] = Field(default_factory=list)
    blocking_failures: list[str] = Field(default_factory=list)
    remediations: list[str] = Field(default_factory=list)
    reviewed_revision: int = 0


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #
class Allocation(BaseModel):
    symbol: str
    weight_pct: float = Field(ge=0.0, le=100.0)
    rationale: str


class Recommendation(BaseModel):
    summary: str
    allocations: list[Allocation]
    expected_return_pct: float
    expected_volatility_pct: float
    review_cadence: str
    disclosures: list[str]

    def weight_of(self, symbol: str) -> float:
        return sum(a.weight_pct for a in self.allocations if a.symbol == symbol)

    def total_weight_pct(self) -> float:
        return sum(a.weight_pct for a in self.allocations)


# --------------------------------------------------------------------------- #
# Graph state
# --------------------------------------------------------------------------- #
class NexusState(TypedDict, total=False):
    """State threaded through the Plan-Act-Verify graph.

    ``trail`` is a *mutable* object carried by reference rather than a reduced
    list. That is safe only because this graph executes strictly sequentially —
    the audit hash chain requires a total order over events. If you add parallel
    fan-out nodes, give each branch its own sub-chain and merge them with an
    explicit reducer; do not mutate one trail from two branches.
    """

    request: ClientRequest
    plan: Plan | None
    market_brief: MarketBrief | None
    compliance_review: ComplianceReview | None
    recommendation: Recommendation | None
    feedback: list[str]
    revision: int
    max_revisions: int
    halted: str | None
    trail: AuditTrail
    usage: dict[str, Any]
