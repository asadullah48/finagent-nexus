"""Tool surface exposed to the MarketAnalyst.

Design rule: **one tool per question an analyst actually asks**, and every tool
returns JSON with the arguments echoed back. The echo is not redundancy — it is
what turns a transcript into evidence. A result line reading
``{"symbol": "2222.SR", "price": 28.4}`` can be matched against the claim in the
brief; a bare ``28.4`` cannot.

Tools are declared ``strict`` so the API validates arguments against the schema
before dispatch reaches Python. Bad input becomes an API-level rejection rather
than a ``KeyError`` three frames deep.
"""

from __future__ import annotations

import json
from typing import Any

from finagent_nexus.tools import risk_metrics
from finagent_nexus.tools.market_data import (
    MarketDataProvider,
    Quote,
    ScreeningData,
    SyntheticMarketData,
)

__all__ = [
    "MarketDataProvider",
    "Quote",
    "ScreeningData",
    "SyntheticMarketData",
    "TOOL_SPECS",
    "ToolDispatcher",
    "risk_metrics",
]


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_SYMBOL = {"type": "string", "description": "Instrument symbol, e.g. 2222.SR or SUKUK.GCC."}
_DAYS = {
    "type": "integer",
    "description": "Number of trading days of history (2-2520).",
}

TOOL_SPECS: list[dict[str, Any]] = [
    _tool(
        "get_quote",
        "Latest price, day change, currency, sector and market cap for one instrument.",
        {"symbol": _SYMBOL},
        ["symbol"],
    ),
    _tool(
        "get_price_history",
        "Closing price series for one instrument. Returns first, last, high, low and the "
        "count of observations rather than the full series, to keep the transcript small.",
        {"symbol": _SYMBOL, "days": _DAYS},
        ["symbol", "days"],
    ),
    _tool(
        "compute_risk_metrics",
        "Annualised return and volatility, Sharpe ratio, maximum drawdown, and historical "
        "95% VaR and CVaR for one instrument over the requested window.",
        {
            "symbol": _SYMBOL,
            "days": _DAYS,
            "risk_free_rate_pct": {
                "type": "number",
                "description": (
                    "Benchmark rate for the Sharpe calculation. Use 0 under a Sharia "
                    "mandate unless a sukuk index yield has been supplied."
                ),
            },
        },
        ["symbol", "days", "risk_free_rate_pct"],
    ),
    _tool(
        "get_screening_data",
        "Sharia and regulatory screening reference data for one instrument: sector, "
        "instrument kind, debt/market-cap, interest-bearing securities and impure "
        "revenue ratios, and whether the instrument is interest-bearing.",
        {"symbol": _SYMBOL},
        ["symbol"],
    ),
    _tool(
        "list_universe",
        "Symbols available for analysis. Pass mandate='sharia' to receive only "
        "instruments that already satisfy the AAOIFI screens.",
        {
            "mandate": {
                "type": "string",
                "enum": ["sharia", "conventional"],
                "description": "The mandate the universe is being built for.",
            }
        },
        ["mandate"],
    ),
]


class ToolDispatcher:
    """Executes tool calls against a :class:`MarketDataProvider`.

    Holds no conversational state — it is a pure function of (name, arguments)
    given a provider, which is what makes replaying an audit trail meaningful.
    """

    def __init__(self, provider: MarketDataProvider | None = None):
        self.provider: MarketDataProvider = provider or SyntheticMarketData()

    # -- individual handlers ------------------------------------------------ #
    def _get_quote(self, symbol: str) -> dict[str, Any]:
        quote = self.provider.get_quote(symbol)
        if quote is None:
            return {"error": f"unknown symbol: {symbol}"}
        return {
            "symbol": quote.symbol,
            "name": quote.name,
            "asset_class": quote.asset_class,
            "sector": quote.sector,
            "currency": quote.currency,
            "exchange": quote.exchange,
            "price": quote.price,
            "day_change_pct": quote.day_change_pct,
            "market_cap_usd": quote.market_cap_usd,
        }

    def _get_price_history(self, symbol: str, days: int) -> dict[str, Any]:
        prices = self.provider.get_price_history(symbol, days)
        if not prices:
            return {"error": f"no history for symbol: {symbol}"}
        return {
            "symbol": symbol,
            "days_requested": days,
            "observations": len(prices),
            "first": prices[0],
            "last": prices[-1],
            "high": max(prices),
            "low": min(prices),
            "period_return_pct": round((prices[-1] / prices[0] - 1) * 100.0, 4),
        }

    def _compute_risk_metrics(
        self, symbol: str, days: int, risk_free_rate_pct: float
    ) -> dict[str, Any]:
        prices = self.provider.get_price_history(symbol, days)
        if len(prices) < 2:
            return {"error": f"insufficient history for symbol: {symbol}"}
        metrics = risk_metrics.compute(prices, risk_free_rate_pct)
        return {"symbol": symbol, "days": days, **metrics.as_dict()}

    def _get_screening_data(self, symbol: str) -> dict[str, Any]:
        data = self.provider.get_screening_data(symbol)
        if data is None:
            return {"error": f"no screening data for symbol: {symbol}"}
        return {
            "symbol": data.symbol,
            "sector": data.sector,
            "instrument_kind": data.instrument_kind,
            "debt_to_market_cap_pct": data.debt_to_market_cap_pct,
            "interest_bearing_securities_pct": data.interest_bearing_securities_pct,
            "non_compliant_revenue_pct": data.non_compliant_revenue_pct,
            "is_riba_bearing": data.is_riba_bearing,
        }

    def _list_universe(self, mandate: str) -> dict[str, Any]:
        return {"mandate": mandate, "symbols": self.provider.list_universe(mandate)}

    # -- dispatch ----------------------------------------------------------- #
    def __call__(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run one tool call.

        Returns:
            ``(json_result, is_error)``. Errors are returned as *results* with
            the flag set, never raised — an unknown symbol is information the
            model should react to, not a crash that loses the whole run.
        """
        handlers = {
            "get_quote": lambda a: self._get_quote(a["symbol"]),
            "get_price_history": lambda a: self._get_price_history(a["symbol"], int(a["days"])),
            "compute_risk_metrics": lambda a: self._compute_risk_metrics(
                a["symbol"], int(a["days"]), float(a.get("risk_free_rate_pct", 0.0))
            ),
            "get_screening_data": lambda a: self._get_screening_data(a["symbol"]),
            "list_universe": lambda a: self._list_universe(a.get("mandate", "conventional")),
        }
        handler = handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"unknown tool: {name}"}), True
        try:
            payload = handler(arguments)
        except (KeyError, TypeError, ValueError) as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"}), True
        return json.dumps(payload, sort_keys=True), "error" in payload
