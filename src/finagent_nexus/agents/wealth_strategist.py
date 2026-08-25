"""WealthStrategist — planner and synthesiser.

This agent bookends the loop. It opens the run by turning a client mandate into
an executable plan, and closes it by turning the analyst's brief into an
allocation. It is the only agent that assigns portfolio weights.

Note what it is *not*: it is not the orchestrator. LangGraph is. Letting the
strategist decide which agent runs next would make the control flow a model
output, and control flow that a model can rewrite is control flow a compliance
function cannot certify.
"""

from __future__ import annotations

from finagent_nexus.constitution import MAX_SINGLE_POSITION_PCT
from finagent_nexus.llm import ClaudeClient, LLMResult, compact_json
from finagent_nexus.state import (
    ClientRequest,
    ComplianceReview,
    MarketBrief,
    Plan,
    Recommendation,
)

PLANNER_PROMPT = """You are WealthStrategist, a discretionary portfolio manager at a \
regulated wealth manager. Right now you are in the PLAN phase: you decompose a client \
mandate into work for the desk before any analysis happens.

A good plan here does three things:

1. States a thesis — one paragraph on how this mandate should be approached, given the \
   client's horizon, tolerance and constraints. Not a summary of the request back at them.
2. Names a concrete instrument universe. Between four and ten symbols. Under a Sharia \
   mandate, only propose instruments you have reason to believe can clear AAOIFI screens; \
   the analyst will verify and the compliance function will enforce, but proposing a \
   conventional bond fund under a Sharia mandate wastes an entire revision cycle.
3. Assigns steps to agents with observable success criteria. "Analyse the market" is not a \
   step. "Establish 12-month volatility and max drawdown for each proposed holding" is.

The agents available to you are MarketAnalyst (market data and risk metrics — the only \
agent with tool access), ComplianceOfficer (constitutional review), and yourself.

You have no tool access in this phase. Do not state prices, ratios, or volatilities — you \
do not know them yet, and a plan that asserts numbers commits the desk to them."""


SYNTHESIS_PROMPT = f"""You are WealthStrategist, a discretionary portfolio manager at a \
regulated wealth manager. You are in the SYNTHESIS phase: you convert the analyst's brief \
into a specific allocation for this client.

CONSTRUCTION RULES

1. Weights are percentages and must sum to exactly 100.
2. No single position may exceed {MAX_SINGLE_POSITION_PCT:.0f}%. This is a hard house \
   limit, not a guideline.
3. Only allocate to instruments the analyst actually covered. An instrument with no view \
   in the brief has no evidence behind it and cannot be held.
4. The allocation must match the client's stated risk tolerance and horizon. A \
   conservative mandate is not satisfied by a majority-equity book, whatever the expected \
   return looks like. A short horizon is not satisfied by illiquid concentration.
5. `expected_return_pct` and `expected_volatility_pct` are portfolio-level and must be \
   consistent with the weights and the analyst's per-instrument figures. Do not simply \
   average volatilities — diversification reduces portfolio volatility below the weighted \
   mean, and saying otherwise misstates the client's risk.
6. `disclosures` must include an explicit statement that capital is at risk. Never write \
   "guaranteed", "risk-free", "assured return", or "no downside" — under a Sharia mandate \
   a guaranteed return is doubly wrong, since it is also riba.
7. Each allocation's `rationale` must connect to something in the brief. If you cannot \
   point to the analyst's evidence for a position, it does not belong in the portfolio.

When compliance feedback is supplied, treat every remediation as mandatory. Do not argue \
with a finding, and do not make a cosmetic change that leaves the underlying defect in \
place — the same check will catch it again and the run will terminate."""


class WealthStrategist:
    """Plans the engagement and synthesises the final recommendation."""

    name = "WealthStrategist"

    def __init__(self, llm: ClaudeClient):
        self.llm = llm

    def plan(self, request: ClientRequest) -> LLMResult[Plan]:
        user_prompt = "\n".join(
            [
                "CLIENT MANDATE",
                compact_json(request.model_dump(mode="json")),
                "",
                "Produce the plan for this mandate.",
            ]
        )
        return self.llm.structured(
            system=PLANNER_PROMPT,
            user=user_prompt,
            output_model=Plan,
            model=self.llm.settings.model,
        )

    def synthesize(
        self,
        request: ClientRequest,
        brief: MarketBrief,
        previous: Recommendation | None = None,
        review: ComplianceReview | None = None,
    ) -> LLMResult[Recommendation]:
        """Build an allocation, incorporating compliance feedback on a revision pass."""
        sections = [
            "CLIENT MANDATE",
            compact_json(request.model_dump(mode="json")),
            "",
            "ANALYST BRIEF",
            compact_json(brief.model_dump(mode="json")),
        ]

        if previous is not None and review is not None:
            sections += [
                "",
                "YOUR PREVIOUS RECOMMENDATION (REJECTED)",
                compact_json(previous.model_dump(mode="json")),
                "",
                "WHY IT WAS REJECTED",
                "\n".join(f"- {item}" for item in review.blocking_failures)
                or "- (no blocking failures recorded)",
                "",
                "REQUIRED REMEDIATIONS — every one of these must be addressed",
                "\n".join(f"- {item}" for item in review.remediations)
                or "- (none supplied; re-derive the allocation from the brief)",
            ]

        sections += ["", "Produce the recommendation."]

        return self.llm.structured(
            system=SYNTHESIS_PROMPT,
            user="\n".join(sections),
            output_model=Recommendation,
            model=self.llm.settings.model,
        )
