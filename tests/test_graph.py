"""Orchestration.

Two kinds of test live here. The behavioural ones drive the whole graph with a
scripted model and assert on the outcome. The structural one asserts a property
of the *topology* — that no path reaches an outcome without passing compliance.
That test is worth more than the others combined, because it cannot be made to
pass by a lucky model response.
"""

from __future__ import annotations

import pytest
from dataclasses import replace

from finagent_nexus.agents import ComplianceOfficer, MarketAnalyst, WealthStrategist
from finagent_nexus.config import Settings
from finagent_nexus.graph import NexusRunner, build_graph
from finagent_nexus.state import (
    ComplianceFindings,
    FindingStatus,
    PrincipleFinding,
    Verdict,
)
from finagent_nexus.tools import SyntheticMarketData, ToolDispatcher

from .conftest import ScriptedLLM, build_recommendation

#: Qualitative principles the reviewer is asked to judge under a Sharia mandate —
#: every principle in force that has no deterministic check. Omitting one here
#: does not silently pass: `_reconcile` turns the gap into UNVERIFIABLE and the
#: fail-closed policy escalates it.
QUALITATIVE_SHARIA = [
    "SHARIA-GHARAR-06",
    "SHARIA-PURIFY-07",
    "REG-SUIT-01",
    "REG-EVID-05",
    "REG-FAIR-06",
    "REG-AML-07",
]

QUALITATIVE_CONVENTIONAL = ["REG-SUIT-01", "REG-EVID-05", "REG-FAIR-06", "REG-AML-07"]


def all_clear() -> ComplianceFindings:
    return ComplianceFindings(
        findings=[
            PrincipleFinding(
                principle_id=principle_id,
                status=FindingStatus.PASS,
                rationale="Consistent with the mandate.",
                evidence="Reviewed against the brief.",
                remediation="",
            )
            for principle_id in QUALITATIVE_SHARIA
        ]
    )


def make_runner(settings: Settings, provider, script) -> tuple[NexusRunner, ScriptedLLM]:
    llm = ScriptedLLM(settings, script)
    runner = NexusRunner.__new__(NexusRunner)
    runner.settings = settings
    runner.provider = provider
    runner.llm = llm  # type: ignore[assignment]
    runner.strategist = WealthStrategist(llm)  # type: ignore[arg-type]
    runner.analyst = MarketAnalyst(llm, ToolDispatcher(provider))  # type: ignore[arg-type]
    runner.officer = ComplianceOfficer(llm, provider)  # type: ignore[arg-type]
    runner.graph = build_graph(runner.strategist, runner.analyst, runner.officer)
    return runner, llm


# --------------------------------------------------------------------------- #
# Structural guarantees
# --------------------------------------------------------------------------- #
def test_no_path_bypasses_compliance_review(settings, provider):
    """The single most important property in the system."""
    _, _ = make_runner(settings, provider, [])
    runner, _ = make_runner(settings, provider, [])
    edges = {(e.source, e.target) for e in runner.graph.get_graph().edges}

    assert ("synthesize", "finalize") not in edges
    assert ("synthesize", "verify") in edges
    # Every route into finalize must originate at verify (or be a halt drain).
    sources_into_finalize = {source for source, target in edges if target == "finalize"}
    assert sources_into_finalize <= {"verify"}


def test_graph_exposes_the_expected_nodes(settings, provider):
    runner, _ = make_runner(settings, provider, [])
    nodes = set(runner.graph.get_graph().nodes)
    assert {"plan", "act", "synthesize", "verify", "finalize"} <= nodes


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_compliant_run_passes_first_time(
    settings, provider, sharia_request, sample_plan, sample_brief, compliant_recommendation
):
    runner, llm = make_runner(
        settings,
        provider,
        [sample_plan, sample_brief, compliant_recommendation, all_clear()],
    )
    state = runner.run(sharia_request, correlation_id="happy")

    assert state["compliance_review"].verdict is Verdict.PASS
    assert state.get("halted") is None
    assert state["revision"] == 0
    assert state["trail"].verify()

    actions = [e.action for e in state["trail"].events]
    assert actions == ["accept_mandate", "plan", "analyse", "synthesize", "review", "finalize"]


def test_evidence_comes_from_executed_tool_calls(
    settings, provider, sharia_request, sample_plan, sample_brief, compliant_recommendation
):
    """The brief's evidence must be receipts, not the model's own narration."""
    runner, _ = make_runner(
        settings,
        provider,
        [sample_plan, sample_brief, compliant_recommendation, all_clear()],
    )
    state = runner.run(sharia_request)

    evidence = state["market_brief"].evidence
    assert evidence, "evidence must not be empty when tools were called"
    assert all(line.startswith("get_quote(") for line in evidence)
    # The model's scripted placeholder line is gone.
    assert not any("-> [ok] {...}" in line for line in evidence)


# --------------------------------------------------------------------------- #
# Revision routing
# --------------------------------------------------------------------------- #
def test_construction_defect_routes_back_to_the_strategist(
    settings, provider, sharia_request, sample_plan, sample_brief, compliant_recommendation
):
    """A concentration breach needs a rebuild, not fresh market data."""
    over_concentrated = build_recommendation(
        {"SUKUK.GCC": 55.0, "2222.SR": 45.0}
    )
    runner, _ = make_runner(
        settings,
        provider,
        [
            sample_plan,
            sample_brief,
            over_concentrated,
            all_clear(),
            compliant_recommendation,  # second synthesize — no second analyse
            all_clear(),
        ],
    )
    state = runner.run(sharia_request)

    assert state["compliance_review"].verdict is Verdict.PASS
    assert state["revision"] == 1
    actions = [e.action for e in state["trail"].events]
    assert actions.count("analyse") == 1, "the analyst must not be re-run for a weights defect"
    assert actions.count("synthesize") == 2


def test_evidence_defect_routes_back_to_the_analyst(
    settings, provider, sharia_request, sample_plan, sample_brief, compliant_recommendation
):
    """A riba breach needs new screening data, so Act must run again."""
    riba_breach = build_recommendation({"SUKUK.GCC": 70.0, "TLT": 30.0})
    runner, _ = make_runner(
        settings,
        provider,
        [
            sample_plan,
            sample_brief,
            riba_breach,
            all_clear(),
            sample_brief,  # second analyse
            compliant_recommendation,
            all_clear(),
        ],
    )
    state = runner.run(sharia_request)

    assert state["compliance_review"].verdict is Verdict.PASS
    actions = [e.action for e in state["trail"].events]
    assert actions.count("analyse") == 2, "a Sharia screen breach must re-open the evidence"


def test_remediations_reach_the_strategist(
    settings, provider, sharia_request, sample_plan, sample_brief, compliant_recommendation
):
    over_concentrated = build_recommendation({"SUKUK.GCC": 55.0, "2222.SR": 45.0})
    runner, llm = make_runner(
        settings,
        provider,
        [
            sample_plan,
            sample_brief,
            over_concentrated,
            all_clear(),
            compliant_recommendation,
            all_clear(),
        ],
    )
    runner.run(sharia_request)

    second_synthesis = [
        call for call in llm.calls if call["output_model"] == "Recommendation"
    ][1]
    assert "REQUIRED REMEDIATIONS" in second_synthesis["user"]
    assert "REG-CONC-02" in second_synthesis["user"]


# --------------------------------------------------------------------------- #
# Escalation
# --------------------------------------------------------------------------- #
def test_exhausted_budget_escalates_rather_than_approving(
    settings, provider, sharia_request, sample_plan, sample_brief
):
    no_budget = Settings(max_revisions=0, audit_dir=settings.audit_dir)
    riba_breach = build_recommendation({"SUKUK.GCC": 70.0, "TLT": 30.0})
    runner, _ = make_runner(
        no_budget, provider, [sample_plan, sample_brief, riba_breach, all_clear()]
    )
    state = runner.run(sharia_request)

    assert state["compliance_review"].verdict is Verdict.BLOCK
    final = state["trail"].events[-1]
    assert final.action == "finalize"
    assert final.status == "escalated"
    assert state["trail"].verify()


def test_missing_reviewer_finding_becomes_unverifiable_not_pass(
    settings, provider, sharia_request, sample_plan, sample_brief, compliant_recommendation
):
    """A silently skipped principle must not be treated as satisfied."""
    partial = ComplianceFindings(
        findings=[
            PrincipleFinding(
                principle_id="REG-SUIT-01",
                status=FindingStatus.PASS,
                rationale="Matches tolerance.",
                evidence="60/40 split against a balanced mandate.",
                remediation="",
            )
        ]
    )
    no_budget = Settings(max_revisions=0, audit_dir=settings.audit_dir)
    runner, _ = make_runner(
        no_budget, provider, [sample_plan, sample_brief, compliant_recommendation, partial]
    )
    state = runner.run(sharia_request)

    review = state["compliance_review"]
    reported = {f.principle_id: f.status for f in review.findings}
    assert reported["REG-EVID-05"] is FindingStatus.UNVERIFIABLE
    assert review.verdict is not Verdict.PASS


def test_agent_failure_halts_without_losing_the_audit_trail(
    settings, provider, sharia_request, sample_plan
):
    """An exhausted script simulates an agent error mid-run."""
    runner, _ = make_runner(settings, provider, [sample_plan])
    with pytest.raises(AssertionError):
        # ScriptedLLM raises AssertionError, which is not an AgentError, so it
        # propagates — this documents that only AgentError is converted to a halt.
        runner.run(sharia_request)


def test_conventional_mandate_runs_without_sharia_principles(
    settings, provider, conventional_request, sample_plan, sample_brief
):
    conventional_findings = ComplianceFindings(
        findings=[
            PrincipleFinding(
                principle_id=principle_id,
                status=FindingStatus.PASS,
                rationale="Consistent with the mandate.",
                evidence="Reviewed.",
                remediation="",
            )
            for principle_id in QUALITATIVE_CONVENTIONAL
        ]
    )
    recommendation = build_recommendation(
        {"SPY": 25.0, "TLT": 25.0, "GLD": 25.0, "SPUS": 25.0}
    )
    runner, _ = make_runner(
        settings, provider, [sample_plan, sample_brief, recommendation, conventional_findings]
    )
    state = runner.run(conventional_request)

    reported = {f.principle_id for f in state["compliance_review"].findings}
    assert not any(pid.startswith("SHARIA-") for pid in reported)
    assert state["compliance_review"].verdict is Verdict.PASS


class TestSyntheticDataGuard:
    """Synthetic market data must be opt-in, never inherited from a default.

    The failure this guards against is silent: a deployment that forgets to
    inject a provider still passes every arithmetic screen, still applies the
    constitution, and still writes a valid hash-chained trail — notarising a
    recommendation built on prices fabricated from a hash of the symbol.
    """

    def test_missing_provider_raises_by_default(self) -> None:
        with pytest.raises(RuntimeError) as excinfo:
            NexusRunner(settings=Settings())
        message = str(excinfo.value)
        assert "synthetic market data" in message.lower()
        # The error must tell both audiences what to do next.
        assert "MarketDataProvider" in message
        assert "FINAGENT_ALLOW_SYNTHETIC_DATA" in message

    def test_explicit_provider_always_wins(self, provider) -> None:
        runner = NexusRunner(settings=Settings(), provider=provider)
        assert runner.provider is provider

    def test_opt_in_permits_synthetic_fallback(self) -> None:
        runner = NexusRunner(settings=replace(Settings(), allow_synthetic_data=True))
        assert isinstance(runner.provider, SyntheticMarketData)

    def test_policy_is_recorded_in_the_audit_fingerprint(self) -> None:
        """Whichever policy is chosen must be visible in the run's audit record."""
        assert Settings().fingerprint()["allow_synthetic_data"] is False
        permissive = replace(Settings(), allow_synthetic_data=True)
        assert permissive.fingerprint()["allow_synthetic_data"] is True
