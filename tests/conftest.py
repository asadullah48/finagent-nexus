"""Shared fixtures.

The whole offline suite runs without an API key. That is achieved with
:class:`ScriptedLLM`, which satisfies the ``ClaudeClient`` interface but returns
pre-built typed values instead of calling Claude.

Scripting *typed* responses rather than raw JSON matters: the stub cannot return
a shape the real client would have rejected, so a test that passes against the
stub is testing orchestration and policy, not a fiction.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from finagent_nexus.config import Settings
from finagent_nexus.llm import LLMResult, ToolCallRecord
from finagent_nexus.state import (
    Allocation,
    ClientRequest,
    InstrumentView,
    Mandate,
    MarketBrief,
    Plan,
    PlanStep,
    Recommendation,
)
from finagent_nexus.tools import SyntheticMarketData, ToolDispatcher

CAPITAL_AT_RISK = "Capital is at risk and the value of investments may fall as well as rise."


class ScriptedLLM:
    """A ``ClaudeClient`` stand-in that replays a queue of typed values.

    Records every call so tests can assert on what an agent was actually asked,
    which is how the prompt-construction behaviour (compliance feedback reaching
    the strategist, for instance) is verified without a model.
    """

    def __init__(self, settings: Settings, script: list[BaseModel]):
        self.settings = settings
        self._script = list(script)
        self.calls: list[dict] = []

    def structured(
        self,
        *,
        system: str,
        user: str,
        output_model: type[BaseModel],
        model: str | None = None,
        effort: str | None = None,
        tools=None,
        dispatch=None,
    ) -> LLMResult:
        if not self._script:
            raise AssertionError(
                f"ScriptedLLM exhausted: an agent asked for {output_model.__name__} "
                f"but no scripted value remains"
            )
        value = self._script.pop(0)
        if not isinstance(value, output_model):
            raise AssertionError(
                f"script mismatch: agent expected {output_model.__name__}, "
                f"script had {type(value).__name__}"
            )
        self.calls.append(
            {"system": system, "user": user, "output_model": output_model.__name__}
        )

        # When tools are offered, actually execute a couple so the evidence
        # receipts in the resulting brief are real rather than invented.
        records: list[ToolCallRecord] = []
        if tools and dispatch is not None:
            for symbol in ("2222.SR", "SUKUK.GCC"):
                payload, is_error = dispatch("get_quote", {"symbol": symbol})
                records.append(
                    ToolCallRecord(
                        name="get_quote",
                        arguments={"symbol": symbol},
                        result=payload,
                        is_error=is_error,
                    )
                )

        return LLMResult(
            value=value,
            raw_text=value.model_dump_json(),
            usage={"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 80},
            latency_ms=5,
            stop_reason="end_turn",
            tool_calls=records,
        )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(max_revisions=2, audit_dir=tmp_path / "audit")


@pytest.fixture
def provider() -> SyntheticMarketData:
    return SyntheticMarketData()


@pytest.fixture
def dispatcher(provider) -> ToolDispatcher:
    return ToolDispatcher(provider)


@pytest.fixture
def sharia_request() -> ClientRequest:
    return ClientRequest(
        client_id="CL-TEST-0001",
        objective="Grow capital within a Shari'ah-compliant mandate.",
        capital_usd=2_500_000,
        horizon_years=10,
        risk_tolerance="balanced",
        jurisdiction="SA",
        mandate=Mandate.SHARIA,
        constraints=["No leveraged products."],
    )


@pytest.fixture
def conventional_request() -> ClientRequest:
    return ClientRequest(
        client_id="CL-TEST-0002",
        objective="Balanced growth under a conventional mandate.",
        capital_usd=1_000_000,
        horizon_years=8,
        risk_tolerance="balanced",
        jurisdiction="GB",
        mandate=Mandate.CONVENTIONAL,
    )


@pytest.fixture
def sample_plan() -> Plan:
    return Plan(
        thesis="Sukuk-anchored core with Shari'ah-screened equity satellites.",
        universe=["SUKUK.GCC", "2222.SR", "1120.SR", "SPUS", "GLD"],
        steps=[
            PlanStep(
                id="S1",
                agent="MarketAnalyst",
                objective="Establish risk metrics for each proposed holding.",
                success_criteria="Annualised volatility and max drawdown recorded per symbol.",
            ),
            PlanStep(
                id="S2",
                agent="WealthStrategist",
                objective="Construct a balanced allocation.",
                success_criteria="Weights sum to 100% with no position above 25%.",
            ),
        ],
    )


@pytest.fixture
def sample_brief() -> MarketBrief:
    return MarketBrief(
        regime="Range-bound rates with firm energy prices.",
        key_risks=["Oil price reversal", "Regional geopolitical risk"],
        views=[
            InstrumentView(
                symbol="SUKUK.GCC",
                name="GCC Sukuk Income Fund (synthetic)",
                asset_class="fixed_income",
                thesis="Low-volatility income anchor.",
                expected_return_pct=4.2,
                volatility_pct=4.5,
                confidence=0.8,
            ),
            InstrumentView(
                symbol="2222.SR",
                name="Saudi Aramco",
                asset_class="equity",
                thesis="Cash-generative energy exposure.",
                expected_return_pct=6.5,
                volatility_pct=18.0,
                confidence=0.7,
            ),
            InstrumentView(
                symbol="SPUS",
                name="SP Funds S&P 500 Sharia ETF",
                asset_class="equity",
                thesis="Screened global equity diversification.",
                expected_return_pct=9.1,
                volatility_pct=16.5,
                confidence=0.75,
            ),
        ],
        evidence=["get_quote(symbol='2222.SR') -> [ok] {...}"],
    )


def build_recommendation(
    weights: dict[str, float],
    summary: str = "A sukuk-anchored balanced portfolio.",
    disclosures: list[str] | None = None,
) -> Recommendation:
    """Construct a recommendation from a symbol -> weight mapping."""
    return Recommendation(
        summary=summary,
        allocations=[
            Allocation(symbol=symbol, weight_pct=weight, rationale=f"Position in {symbol}.")
            for symbol, weight in weights.items()
        ],
        expected_return_pct=6.4,
        expected_volatility_pct=9.1,
        review_cadence="Quarterly",
        disclosures=disclosures if disclosures is not None else [CAPITAL_AT_RISK],
    )


@pytest.fixture
def compliant_recommendation() -> Recommendation:
    """A portfolio that clears every deterministic screen.

    Four equal positions, not three larger ones: the 25% single-name limit in
    REG-CONC-02 means a genuinely compliant book needs at least four lines. It
    is easy to write a "compliant" fixture that quietly breaches concentration.
    """
    return build_recommendation(
        {"SUKUK.GCC": 25.0, "2222.SR": 25.0, "SPUS": 25.0, "GLD": 25.0}
    )
