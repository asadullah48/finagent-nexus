"""Plan-Act-Verify orchestration.

The graph is the governance artefact. Everything an auditor needs to know about
control flow is visible in :func:`build_graph` — which nodes exist, which edges
connect them, and which function decides each branch. No agent can add an edge
at runtime.

    START -> plan -> act -> synthesize -> verify -> [route] -> finalize -> END
                      ^                     |
                      |                     v
                      +--- REVISE ----------+
                         (to act or synthesize, per the breached principle)

Three properties are enforced structurally rather than by prompt:

* **Verify is not optional.** There is no edge from ``synthesize`` to
  ``finalize``. Every recommendation passes through compliance.
* **The loop is bounded.** ``revision`` increments on each REVISE and the
  routing function converts an exhausted budget into a halt.
* **Failure is a state, not an exception.** A node that raises records the
  failure and sets ``halted``; the graph then drains to ``finalize`` so the
  audit trail is closed properly instead of the process dying mid-run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from finagent_nexus.agents import ComplianceOfficer, MarketAnalyst, WealthStrategist
from finagent_nexus.audit import AuditTrail
from finagent_nexus.config import Settings
from finagent_nexus.constitution import REQUIRES_NEW_EVIDENCE
from finagent_nexus.llm import AgentError, ClaudeClient, ModelRefusal
from finagent_nexus.state import ClientRequest, FindingStatus, NexusState, Verdict
from finagent_nexus.tools import SyntheticMarketData, ToolDispatcher
from finagent_nexus.tools.market_data import MarketDataProvider

NodeFn = Callable[[NexusState], dict[str, Any]]


def _guard(actor: str, action: str) -> Callable[[NodeFn], NodeFn]:
    """Turn an agent exception into a recorded halt.

    An unhandled exception in a node loses the audit trail for the whole run,
    which is the one thing a regulated system may not do. This wrapper records
    what failed and hands back a ``halted`` state that the router drains to
    ``finalize``.
    """

    def decorator(fn: NodeFn) -> NodeFn:
        def wrapper(state: NexusState) -> dict[str, Any]:
            trail: AuditTrail = state["trail"]
            try:
                return fn(state)
            except ModelRefusal as exc:
                trail.record(
                    actor=actor,
                    action=action,
                    status="refused",
                    detail={"category": exc.category, "explanation": exc.explanation},
                )
                return {"halted": f"{actor} refused the request: {exc.explanation}"}
            except AgentError as exc:
                trail.record(
                    actor=actor, action=action, status="error", detail={"error": str(exc)}
                )
                return {"halted": f"{actor} failed during {action}: {exc}"}

        wrapper.__name__ = fn.__name__
        return wrapper

    return decorator


def build_graph(
    strategist: WealthStrategist,
    analyst: MarketAnalyst,
    officer: ComplianceOfficer,
):
    """Compile the Plan-Act-Verify graph over the three supplied agents."""

    # ---------------------------- Plan ------------------------------------ #
    @_guard("WealthStrategist", "plan")
    def plan_node(state: NexusState) -> dict[str, Any]:
        trail: AuditTrail = state["trail"]
        result = strategist.plan(state["request"])
        trail.record(
            actor="WealthStrategist",
            action="plan",
            detail={
                "thesis": result.value.thesis,
                "universe": result.value.universe,
                "steps": [s.id for s in result.value.steps],
            },
            model=strategist.llm.settings.model,
            usage=result.usage,
            latency_ms=result.latency_ms,
        )
        return {"plan": result.value}

    # ----------------------------- Act ------------------------------------ #
    @_guard("MarketAnalyst", "analyse")
    def act_node(state: NexusState) -> dict[str, Any]:
        trail: AuditTrail = state["trail"]
        result = analyst.run(
            request=state["request"],
            plan=state.get("plan"),
            feedback=state.get("feedback") or [],
        )
        trail.record(
            actor="MarketAnalyst",
            action="analyse",
            detail={
                "revision": state.get("revision", 0),
                "instruments": [v.symbol for v in result.value.views],
                "tool_calls": len(result.tool_calls),
                "tool_errors": sum(1 for r in result.tool_calls if r.is_error),
                "evidence": result.value.evidence,
            },
            model=analyst.llm.settings.analyst_model,
            usage=result.usage,
            latency_ms=result.latency_ms,
        )
        return {"market_brief": result.value}

    # -------------------------- Synthesise -------------------------------- #
    @_guard("WealthStrategist", "synthesize")
    def synthesize_node(state: NexusState) -> dict[str, Any]:
        trail: AuditTrail = state["trail"]
        brief = state.get("market_brief")
        if brief is None:
            return {"halted": "no market brief available to synthesise from"}

        result = strategist.synthesize(
            request=state["request"],
            brief=brief,
            previous=state.get("recommendation"),
            review=state.get("compliance_review"),
        )
        trail.record(
            actor="WealthStrategist",
            action="synthesize",
            detail={
                "revision": state.get("revision", 0),
                "allocations": {
                    a.symbol: a.weight_pct for a in result.value.allocations
                },
                "expected_return_pct": result.value.expected_return_pct,
                "expected_volatility_pct": result.value.expected_volatility_pct,
            },
            model=strategist.llm.settings.model,
            usage=result.usage,
            latency_ms=result.latency_ms,
        )
        return {"recommendation": result.value}

    # ---------------------------- Verify ---------------------------------- #
    @_guard("ComplianceOfficer", "review")
    def verify_node(state: NexusState) -> dict[str, Any]:
        trail: AuditTrail = state["trail"]
        recommendation = state.get("recommendation")
        brief = state.get("market_brief")
        if recommendation is None or brief is None:
            return {"halted": "nothing to review"}

        revision = state.get("revision", 0)
        remaining = state.get("max_revisions", 0) - revision

        review, usage, latency_ms = officer.review(
            request=state["request"],
            brief=brief,
            recommendation=recommendation,
            revisions_remaining=remaining,
            revision=revision,
        )
        trail.record(
            actor="ComplianceOfficer",
            action="review",
            status=review.verdict.value,
            detail={
                "revision": revision,
                "revisions_remaining": remaining,
                "verdict": review.verdict.value,
                "findings": {f.principle_id: f.status.value for f in review.findings},
                "blocking_failures": review.blocking_failures,
                "remediations": review.remediations,
            },
            model=officer.llm.settings.model,
            usage=usage,
            latency_ms=latency_ms,
        )

        update: dict[str, Any] = {"compliance_review": review}
        if review.verdict is Verdict.REVISE:
            update["revision"] = revision + 1
            update["feedback"] = review.remediations
        return update

    # --------------------------- Finalise --------------------------------- #
    def finalize_node(state: NexusState) -> dict[str, Any]:
        trail: AuditTrail = state["trail"]
        review = state.get("compliance_review")
        halted = state.get("halted")
        if halted:
            outcome = "halted"
        elif review is not None and review.verdict is Verdict.PASS:
            outcome = "approved"
        else:
            outcome = "escalated"
        trail.record(
            actor="Orchestrator",
            action="finalize",
            status=outcome,
            detail={
                "outcome": outcome,
                "revisions_used": state.get("revision", 0),
                "halted_reason": halted,
                "verdict": review.verdict.value if review else None,
            },
        )
        return {}

    # ---------------------------- Routing --------------------------------- #
    def route_after_verify(state: NexusState) -> str:
        """Decide where a reviewed recommendation goes next.

        Pure function of state, with no model in the loop — this is the branch a
        compliance function will read first.
        """
        if state.get("halted"):
            return "finalize"

        review = state.get("compliance_review")
        if review is None:
            return "finalize"
        if review.verdict is not Verdict.REVISE:
            return "finalize"
        if state.get("revision", 0) > state.get("max_revisions", 0):
            return "finalize"

        # Route by *which* principle broke, read from the structured findings —
        # never by pattern-matching the prose of a remediation string.
        needs_evidence = any(
            finding.principle_id in REQUIRES_NEW_EVIDENCE
            and finding.status is not FindingStatus.PASS
            for finding in review.findings
        )
        return "act" if needs_evidence else "synthesize"

    graph = StateGraph(NexusState)
    graph.add_node("plan", plan_node)
    graph.add_node("act", act_node)
    graph.add_node("synthesize", synthesize_node)
    graph.add_node("verify", verify_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "act")
    graph.add_edge("act", "synthesize")
    # Note the absence of a synthesize -> finalize edge: verification is
    # structurally unavoidable.
    graph.add_edge("synthesize", "verify")
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        {"act": "act", "synthesize": "synthesize", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)
    return graph.compile()


class NexusRunner:
    """Convenience wrapper: wires agents, compiles the graph, runs one mandate."""

    def __init__(
        self,
        settings: Settings | None = None,
        provider: MarketDataProvider | None = None,
        llm: ClaudeClient | None = None,
    ):
        self.settings = settings or Settings.from_env()
        self.provider = self._resolve_provider(provider)
        self.llm = llm or ClaudeClient(self.settings)
        self.strategist = WealthStrategist(self.llm)
        self.analyst = MarketAnalyst(self.llm, ToolDispatcher(self.provider))
        self.officer = ComplianceOfficer(self.llm, self.provider)
        self.graph = build_graph(self.strategist, self.analyst, self.officer)

    def _resolve_provider(self, provider: MarketDataProvider | None) -> MarketDataProvider:
        """Decide what market data this runner is allowed to run on.

        Synthetic data is **opt-in and never inherited**. An explicit provider
        always wins; absent one, :class:`SyntheticMarketData` is supplied only
        when ``settings.allow_synthetic_data`` says so, and otherwise this
        raises rather than falling back.

        The rejected alternative was to default the fallback on, so the
        repository ran out of the box. That makes forgetting to inject a
        provider indistinguishable from a working deployment: the arithmetic
        screens still run, the constitution is still applied, and the
        hash-chained trail still notarises a beautifully compliant
        recommendation — built entirely on prices fabricated from a hash of the
        symbol. The audit trail does not protect you there; it records the
        wrong thing, credibly. A loud failure at construction is cheaper than a
        credible record of a fictional portfolio.

        The chosen policy is captured in :meth:`Settings.fingerprint`, so every
        run's audit record states which market data regime it ran under.

        Raises:
            RuntimeError: No provider was supplied and synthetic data is not
                permitted by the current settings.
        """
        if provider is not None:
            return provider
        if not self.settings.allow_synthetic_data:
            raise RuntimeError(
                "No MarketDataProvider was supplied and synthetic market data "
                "is disabled. SyntheticMarketData fabricates deterministic "
                "prices from a hash of the symbol and must never inform live "
                "advice.\n"
                "  Production: pass a real provider, e.g. "
                "NexusRunner(provider=YourMarketDataProvider()).\n"
                "  Demos and tests: opt in explicitly with "
                "FINAGENT_ALLOW_SYNTHETIC_DATA=true, the "
                "--allow-synthetic-data flag, or by passing "
                "SyntheticMarketData() directly.\n"
                "See docs/governance.md section 7 (pre-production checklist)."
            )
        return SyntheticMarketData()

    def run(self, request: ClientRequest, correlation_id: str | None = None) -> NexusState:
        trail = AuditTrail(correlation_id=correlation_id, directory=self.settings.audit_dir)
        trail.record(
            actor="Orchestrator",
            action="accept_mandate",
            detail={
                "request": request.model_dump(mode="json"),
                "settings": self.settings.fingerprint(),
            },
        )
        initial: NexusState = {
            "request": request,
            "plan": None,
            "market_brief": None,
            "compliance_review": None,
            "recommendation": None,
            "feedback": [],
            "revision": 0,
            "max_revisions": self.settings.max_revisions,
            "halted": None,
            "trail": trail,
        }
        # Each revision costs at most three node visits; the +10 covers plan,
        # finalize, and LangGraph's own bookkeeping.
        limit = (self.settings.max_revisions + 1) * 3 + 10
        return self.graph.invoke(initial, config={"recursion_limit": limit})
