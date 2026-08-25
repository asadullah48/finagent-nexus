"""MarketAnalyst — the tool-using agent.

This agent is the only one permitted to touch market data. That is a deliberate
containment boundary: if a figure appears anywhere in the final recommendation,
it entered the system here, through a recorded tool call, and nowhere else.

The strongest guarantee in this module is the last three lines of :meth:`run`.
The model proposes an ``evidence`` list; we discard it and substitute the
receipts the dispatcher actually produced. An agent cannot cite a call it did
not make.
"""

from __future__ import annotations

from finagent_nexus.llm import ClaudeClient, LLMResult, compact_json
from finagent_nexus.state import ClientRequest, MarketBrief, Plan
from finagent_nexus.tools import TOOL_SPECS, ToolDispatcher

SYSTEM_PROMPT = """You are MarketAnalyst, a sell-side research analyst inside a regulated \
financial institution. You produce evidence-backed market briefs. You do not give advice, \
recommend allocations, or set portfolio weights — that is the WealthStrategist's mandate, \
and stepping into it breaks the institution's separation of duties.

HOW YOU WORK

1. Call tools before forming a view. You have no reliable memory of current prices, \
   ratios, or volatilities, and a plausible-sounding number you did not look up is the \
   single most damaging thing you can produce.
2. For every instrument you intend to write about, call at minimum `get_quote` and \
   `compute_risk_metrics`. Under a Sharia mandate, also call `get_screening_data` — the \
   compliance function will screen independently, but a brief that ignores screening data \
   wastes a revision cycle.
3. If a tool returns an error, say so in `key_risks` and lower your `confidence` for that \
   instrument. Do not substitute a guess.
4. `expected_return_pct` and `volatility_pct` must be grounded in what the tools returned. \
   Where you adjust a historical figure for a forward view, say so in the instrument's \
   `thesis` and explain the adjustment.
5. `confidence` is your own calibration, from 0.0 to 1.0. A view built on one tool call \
   with a wide drawdown does not deserve 0.9.

TONE

Write as you would for an investment committee: specific, quantified, and willing to say \
what you do not know. Do not hedge everything into uselessness, and do not present a \
projection as a fact."""


class MarketAnalyst:
    """Produces a :class:`MarketBrief` from tool-gathered evidence."""

    name = "MarketAnalyst"

    def __init__(self, llm: ClaudeClient, dispatcher: ToolDispatcher | None = None):
        self.llm = llm
        self.dispatcher = dispatcher or ToolDispatcher()

    def run(
        self,
        request: ClientRequest,
        plan: Plan | None = None,
        feedback: list[str] | None = None,
    ) -> LLMResult[MarketBrief]:
        """Analyse the planned universe and return a brief.

        Args:
            request: The client mandate.
            plan: The strategist's plan; its ``universe`` scopes the analysis.
            feedback: Remediation notes from a failed compliance review. Present
                only on a revision pass.
        """
        universe = plan.universe if plan else self.dispatcher.provider.list_universe(
            request.mandate.value
        )

        sections = [
            "CLIENT MANDATE",
            compact_json(request.model_dump(mode="json")),
            "",
            "INSTRUMENTS TO ANALYSE",
            ", ".join(universe) if universe else "(none specified — call list_universe first)",
        ]

        if plan is not None:
            sections += [
                "",
                "STRATEGIST'S THESIS",
                plan.thesis,
                "",
                "YOUR ASSIGNED STEPS",
                "\n".join(
                    f"- [{s.id}] {s.objective} (done when: {s.success_criteria})"
                    for s in plan.steps
                    if s.agent == self.name
                )
                or "- Produce a full brief on the instruments above.",
            ]

        if feedback:
            sections += [
                "",
                "COMPLIANCE FEEDBACK ON THE PREVIOUS ATTEMPT",
                "The last recommendation was rejected. Re-run your analysis with these",
                "defects in mind — in particular, supply the evidence needed to settle",
                "the points below, and drop instruments that cannot clear them.",
                "",
                "\n".join(f"- {item}" for item in feedback),
            ]

        result = self.llm.structured(
            system=SYSTEM_PROMPT,
            user="\n".join(sections),
            output_model=MarketBrief,
            model=self.llm.settings.analyst_model,
            tools=TOOL_SPECS,
            dispatch=self.dispatcher,
        )

        # Replace model-authored evidence with the dispatcher's receipts. The
        # model's list is a summary of intent; this list is what happened.
        result.value.evidence = result.evidence()
        return result
