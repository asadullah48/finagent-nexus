"""Market data access.

The system talks to market data through :class:`MarketDataProvider`, a Protocol.
Everything downstream — the analyst's tools, the deterministic Sharia screens,
the eval harness — depends on the *interface*, never on a vendor SDK. Swapping
in Bloomberg, Refinitiv, LSEG or an internal golden-source service is a matter
of writing one class that satisfies the Protocol.

The bundled :class:`SyntheticMarketData` is **not** a market simulator and must
not be used for live advice. It exists so the repository is runnable, the tests
are hermetic, and the eval harness is reproducible: every price path is derived
deterministically from a SHA-256 seed of the symbol, so the same symbol yields
the same series on every machine, forever.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    asset_class: str
    sector: str
    currency: str
    exchange: str
    price: float
    day_change_pct: float
    market_cap_usd: float


@dataclass(frozen=True)
class ScreeningData:
    """Reference data the Sharia and regulatory screens consume.

    These are the four numbers a Sharia board actually asks for, plus the
    instrument's economic nature. Keeping them in one frozen record means
    :mod:`finagent_nexus.checks` can be re-run offline from a snapshot.
    """

    symbol: str
    sector: str
    instrument_kind: str
    debt_to_market_cap_pct: float
    interest_bearing_securities_pct: float
    non_compliant_revenue_pct: float
    is_riba_bearing: bool


@runtime_checkable
class MarketDataProvider(Protocol):
    """The seam between this system and whatever supplies your market data."""

    def get_quote(self, symbol: str) -> Quote | None: ...

    def get_price_history(self, symbol: str, days: int = 252) -> list[float]: ...

    def get_screening_data(self, symbol: str) -> ScreeningData | None: ...

    def list_universe(self, mandate: str | None = None) -> list[str]: ...


# --------------------------------------------------------------------------- #
# Synthetic reference table
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Instrument:
    symbol: str
    name: str
    asset_class: str
    sector: str
    instrument_kind: str
    currency: str
    exchange: str
    base_price: float
    market_cap_usd: float
    annual_drift_pct: float
    annual_vol_pct: float
    debt_to_market_cap_pct: float
    interest_bearing_securities_pct: float
    non_compliant_revenue_pct: float
    is_riba_bearing: bool


#: Synthetic reference universe.
#:
#: Every number below is invented. Tickers of real listings are used so the
#: demonstrations read naturally, but the ratios, prices and volatilities are
#: NOT those companies' actual figures and must never be presented as such.
#: The issuer used to exercise the prohibited-sector screen is given a
#: deliberately fictional name, because attributing a prohibited activity to a
#: real, identifiable company would be a false statement about that company
#: rather than a placeholder.
#:
#: The set exercises every branch of the constitution: compliant equities and
#: sukuk, a conventional bank, an interest-bearing bond fund, a name that
#: breaches the leverage screen, and a prohibited-sector issuer.
#:
#: Keyword arguments throughout — a fifteen-field positional constructor
#: silently mis-assigns a volatility to a debt ratio the moment one row is a
#: value short.
_UNIVERSE: tuple[_Instrument, ...] = (
    _Instrument(
        symbol="2222.SR", name="Saudi Aramco", asset_class="equity", sector="energy",
        instrument_kind="common_stock", currency="SAR", exchange="Tadawul",
        base_price=28.40, market_cap_usd=1_800_000_000_000,
        annual_drift_pct=6.5, annual_vol_pct=18.0,
        debt_to_market_cap_pct=12.4, interest_bearing_securities_pct=3.1,
        non_compliant_revenue_pct=0.0, is_riba_bearing=False,
    ),
    _Instrument(
        symbol="1120.SR", name="Al Rajhi Bank", asset_class="equity",
        sector="islamic_banking", instrument_kind="common_stock",
        currency="SAR", exchange="Tadawul",
        base_price=86.20, market_cap_usd=92_000_000_000,
        annual_drift_pct=8.2, annual_vol_pct=21.0,
        debt_to_market_cap_pct=5.4, interest_bearing_securities_pct=1.2,
        non_compliant_revenue_pct=0.0, is_riba_bearing=False,
    ),
    # Conventional bank: fails the sector, leverage and impure-revenue screens.
    _Instrument(
        symbol="1010.SR", name="Riyad Bank", asset_class="equity",
        sector="conventional_banking", instrument_kind="common_stock",
        currency="SAR", exchange="Tadawul",
        base_price=27.15, market_cap_usd=22_000_000_000,
        annual_drift_pct=7.0, annual_vol_pct=24.0,
        debt_to_market_cap_pct=61.0, interest_bearing_securities_pct=48.0,
        non_compliant_revenue_pct=74.0, is_riba_bearing=False,
    ),
    _Instrument(
        symbol="2010.SR", name="SABIC", asset_class="equity", sector="materials",
        instrument_kind="common_stock", currency="SAR", exchange="Tadawul",
        base_price=72.80, market_cap_usd=58_000_000_000,
        annual_drift_pct=5.4, annual_vol_pct=23.0,
        debt_to_market_cap_pct=26.5, interest_bearing_securities_pct=6.2,
        non_compliant_revenue_pct=0.4, is_riba_bearing=False,
    ),
    # Breaches the 30% leverage screen — exercises SHARIA-SCREEN-02.
    _Instrument(
        symbol="EMAAR.DU", name="Emaar Properties", asset_class="equity",
        sector="real_estate", instrument_kind="common_stock",
        currency="AED", exchange="DFM",
        base_price=8.05, market_cap_usd=19_000_000_000,
        annual_drift_pct=7.8, annual_vol_pct=25.0,
        debt_to_market_cap_pct=34.8, interest_bearing_securities_pct=4.4,
        non_compliant_revenue_pct=2.1, is_riba_bearing=False,
    ),
    _Instrument(
        symbol="DIB.DU", name="Dubai Islamic Bank", asset_class="equity",
        sector="islamic_banking", instrument_kind="common_stock",
        currency="AED", exchange="DFM",
        base_price=6.42, market_cap_usd=12_500_000_000,
        annual_drift_pct=6.9, annual_vol_pct=21.0,
        debt_to_market_cap_pct=7.9, interest_bearing_securities_pct=2.0,
        non_compliant_revenue_pct=0.0, is_riba_bearing=False,
    ),
    _Instrument(
        symbol="SUKUK.GCC", name="GCC Sukuk Income Fund (synthetic)",
        asset_class="fixed_income", sector="diversified", instrument_kind="sukuk",
        currency="USD", exchange="OTC",
        base_price=104.60, market_cap_usd=2_400_000_000,
        annual_drift_pct=4.2, annual_vol_pct=4.5,
        debt_to_market_cap_pct=0.0, interest_bearing_securities_pct=0.0,
        non_compliant_revenue_pct=0.0, is_riba_bearing=False,
    ),
    _Instrument(
        symbol="SPUS", name="SP Funds S&P 500 Sharia ETF", asset_class="equity",
        sector="diversified", instrument_kind="etf_sharia",
        currency="USD", exchange="NYSE",
        base_price=48.90, market_cap_usd=1_100_000_000,
        annual_drift_pct=9.1, annual_vol_pct=16.5,
        debt_to_market_cap_pct=9.8, interest_bearing_securities_pct=2.4,
        non_compliant_revenue_pct=0.6, is_riba_bearing=False,
    ),
    _Instrument(
        symbol="GLD", name="SPDR Gold Shares", asset_class="commodity",
        sector="precious_metals", instrument_kind="commodity_etf",
        currency="USD", exchange="NYSE",
        base_price=212.35, market_cap_usd=68_000_000_000,
        annual_drift_pct=4.8, annual_vol_pct=14.0,
        debt_to_market_cap_pct=0.0, interest_bearing_securities_pct=0.0,
        non_compliant_revenue_pct=0.0, is_riba_bearing=False,
    ),
    # Broad conventional index: fails leverage and impure-revenue screens.
    _Instrument(
        symbol="SPY", name="SPDR S&P 500 ETF Trust", asset_class="equity",
        sector="diversified", instrument_kind="etf_conventional",
        currency="USD", exchange="NYSE",
        base_price=548.20, market_cap_usd=540_000_000_000,
        annual_drift_pct=9.4, annual_vol_pct=15.2,
        debt_to_market_cap_pct=41.0, interest_bearing_securities_pct=12.0,
        non_compliant_revenue_pct=21.5, is_riba_bearing=False,
    ),
    # Interest-bearing by construction — exercises SHARIA-RIBA-01.
    _Instrument(
        symbol="TLT", name="iShares 20+ Year Treasury Bond ETF",
        asset_class="fixed_income", sector="government_debt",
        instrument_kind="conventional_bond", currency="USD", exchange="NASDAQ",
        base_price=92.10, market_cap_usd=48_000_000_000,
        annual_drift_pct=2.1, annual_vol_pct=13.5,
        debt_to_market_cap_pct=0.0, interest_bearing_securities_pct=100.0,
        non_compliant_revenue_pct=100.0, is_riba_bearing=True,
    ),
    # Fictional issuer — exercises SHARIA-SECTOR-05 without attributing a
    # prohibited activity to a real, identifiable company.
    _Instrument(
        symbol="DEMO.ALC", name="Northgate Beverages (fictional)", asset_class="equity",
        sector="alcohol", instrument_kind="common_stock",
        currency="EUR", exchange="Euronext",
        base_price=64.00, market_cap_usd=12_000_000_000,
        annual_drift_pct=7.2, annual_vol_pct=22.0,
        debt_to_market_cap_pct=22.0, interest_bearing_securities_pct=3.0,
        non_compliant_revenue_pct=9.4, is_riba_bearing=False,
    ),
)

_BY_SYMBOL: dict[str, _Instrument] = {i.symbol: i for i in _UNIVERSE}

#: Symbols an analyst may consider under a Sharia mandate. Computed from the
#: screens rather than hand-maintained, so the list cannot drift from the rules.
_SHARIA_SAFE = tuple(
    i.symbol
    for i in _UNIVERSE
    if not i.is_riba_bearing
    and i.debt_to_market_cap_pct <= 30.0
    and i.interest_bearing_securities_pct <= 30.0
    and i.non_compliant_revenue_pct <= 5.0
    and i.sector not in {"conventional_banking", "conventional_insurance", "alcohol"}
)


def _seed_for(symbol: str) -> int:
    """Stable 64-bit seed. ``hash()`` is salted per process and would break reproducibility."""
    return int.from_bytes(hashlib.sha256(symbol.encode("utf-8")).digest()[:8], "big")


class SyntheticMarketData:
    """Deterministic stand-in provider. Reproducible, offline, and clearly fake."""

    def __init__(self, universe: tuple[_Instrument, ...] = _UNIVERSE):
        self._by_symbol = {i.symbol: i for i in universe}

    def get_quote(self, symbol: str) -> Quote | None:
        inst = self._by_symbol.get(symbol)
        if inst is None:
            return None
        history = self.get_price_history(symbol, days=2)
        price = history[-1]
        prev = history[0] if len(history) > 1 else price
        change = ((price - prev) / prev * 100.0) if prev else 0.0
        return Quote(
            symbol=inst.symbol,
            name=inst.name,
            asset_class=inst.asset_class,
            sector=inst.sector,
            currency=inst.currency,
            exchange=inst.exchange,
            price=round(price, 4),
            day_change_pct=round(change, 4),
            market_cap_usd=inst.market_cap_usd,
        )

    def get_price_history(self, symbol: str, days: int = 252) -> list[float]:
        """Geometric random walk with the instrument's drift and volatility.

        Seeded per symbol, so the series is identical on every run and every
        machine — a precondition for the eval harness meaning anything.
        """
        inst = self._by_symbol.get(symbol)
        if inst is None:
            return []
        days = max(2, min(days, 2520))
        rng = random.Random(_seed_for(symbol))
        mu = inst.annual_drift_pct / 100.0 / TRADING_DAYS_PER_YEAR
        sigma = inst.annual_vol_pct / 100.0 / math.sqrt(TRADING_DAYS_PER_YEAR)

        prices = [inst.base_price]
        for _ in range(days - 1):
            shock = rng.gauss(0.0, 1.0)
            step = math.exp((mu - 0.5 * sigma**2) + sigma * shock)
            prices.append(prices[-1] * step)
        # Rebase so the *final* price is the quoted base price; the walk then
        # reads as history leading up to today rather than drifting away from it.
        scale = inst.base_price / prices[-1]
        return [round(p * scale, 6) for p in prices]

    def get_screening_data(self, symbol: str) -> ScreeningData | None:
        inst = self._by_symbol.get(symbol)
        if inst is None:
            return None
        return ScreeningData(
            symbol=inst.symbol,
            sector=inst.sector,
            instrument_kind=inst.instrument_kind,
            debt_to_market_cap_pct=inst.debt_to_market_cap_pct,
            interest_bearing_securities_pct=inst.interest_bearing_securities_pct,
            non_compliant_revenue_pct=inst.non_compliant_revenue_pct,
            is_riba_bearing=inst.is_riba_bearing,
        )

    def list_universe(self, mandate: str | None = None) -> list[str]:
        if mandate == "sharia":
            return [s for s in _SHARIA_SAFE if s in self._by_symbol]
        return list(self._by_symbol)
