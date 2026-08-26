"""Risk metrics.

Pure functions over a price series — no numpy, no pandas, no I/O. That is a
deliberate constraint: these numbers end up in a client recommendation, so an
auditor should be able to read the implementation in one sitting and reproduce
any figure in a spreadsheet.

Volatility and return are annualised on a 252-trading-day convention. Value at
Risk is *historical* (an empirical quantile of realised returns), not parametric
— parametric VaR assumes normality, which is precisely the assumption that
fails in the tail a risk report is meant to describe.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from itertools import pairwise

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class RiskMetrics:
    observations: int
    annualised_return_pct: float
    annualised_volatility_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    historical_var_95_pct: float
    historical_cvar_95_pct: float

    def as_dict(self) -> dict[str, float | int]:
        return asdict(self)


def simple_returns(prices: list[float]) -> list[float]:
    """Period-over-period simple returns. Zero or negative prices are skipped.

    ``pairwise`` rather than ``zip(prices, prices[1:])``: the slice copies the
    whole series on every call, and a bare ``zip`` over two sequences of
    deliberately different lengths is the shape that hides an off-by-one
    truncation. ``pairwise`` states the intent — successive pairs — so there is
    no length contract left to get wrong.
    """
    out: list[float] = []
    for previous, current in pairwise(prices):
        if previous > 0:
            out.append((current - previous) / previous)
    return out


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: list[float]) -> float:
    """Sample standard deviation (Bessel-corrected)."""
    if len(values) < 2:
        return 0.0
    mu = mean(values)
    variance = sum((v - mu) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def annualised_return_pct(prices: list[float]) -> float:
    """CAGR implied by the first and last observation."""
    if len(prices) < 2 or prices[0] <= 0:
        return 0.0
    years = (len(prices) - 1) / TRADING_DAYS_PER_YEAR
    if years <= 0:
        return 0.0
    growth = prices[-1] / prices[0]
    if growth <= 0:
        return -100.0
    return (growth ** (1 / years) - 1) * 100.0


def annualised_volatility_pct(prices: list[float]) -> float:
    return stdev(simple_returns(prices)) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0


def sharpe_ratio(prices: list[float], risk_free_rate_pct: float = 0.0) -> float:
    """Excess return per unit of volatility.

    ``risk_free_rate_pct`` defaults to 0 rather than a T-bill yield. Under a
    Sharia mandate an interest-based risk-free rate is not a legitimate
    benchmark, so the caller must opt into one explicitly — for a conventional
    mandate, pass the relevant sovereign yield; for a Sharia mandate, pass a
    sukuk index yield or leave it at zero.
    """
    vol = annualised_volatility_pct(prices)
    if vol == 0:
        return 0.0
    return (annualised_return_pct(prices) - risk_free_rate_pct) / vol


def max_drawdown_pct(prices: list[float]) -> float:
    """Largest peak-to-trough decline, as a positive percentage."""
    if not prices:
        return 0.0
    peak = prices[0]
    worst = 0.0
    for price in prices:
        peak = max(peak, price)
        if peak > 0:
            worst = min(worst, (price - peak) / peak)
    return abs(worst) * 100.0


def historical_var_pct(prices: list[float], confidence: float = 0.95) -> float:
    """Empirical one-day VaR at ``confidence``, as a positive percentage loss."""
    returns = sorted(simple_returns(prices))
    if not returns:
        return 0.0
    index = max(0, min(len(returns) - 1, int((1 - confidence) * len(returns))))
    return abs(min(returns[index], 0.0)) * 100.0


def historical_cvar_pct(prices: list[float], confidence: float = 0.95) -> float:
    """Expected shortfall — the mean loss in the tail beyond VaR.

    Reported alongside VaR because VaR alone says nothing about how bad the bad
    days are, which is the question a client is actually asking.
    """
    returns = sorted(simple_returns(prices))
    if not returns:
        return 0.0
    cutoff = max(1, int((1 - confidence) * len(returns)))
    tail = returns[:cutoff]
    return abs(min(mean(tail), 0.0)) * 100.0


def compute(prices: list[float], risk_free_rate_pct: float = 0.0) -> RiskMetrics:
    """Full metric set for one price series."""
    return RiskMetrics(
        observations=len(prices),
        annualised_return_pct=round(annualised_return_pct(prices), 4),
        annualised_volatility_pct=round(annualised_volatility_pct(prices), 4),
        sharpe_ratio=round(sharpe_ratio(prices, risk_free_rate_pct), 4),
        max_drawdown_pct=round(max_drawdown_pct(prices), 4),
        historical_var_95_pct=round(historical_var_pct(prices, 0.95), 4),
        historical_cvar_95_pct=round(historical_cvar_pct(prices, 0.95), 4),
    )


def portfolio_volatility_pct(
    weights_pct: dict[str, float],
    series: dict[str, list[float]],
    assumed_correlation: float = 0.35,
) -> float:
    """Portfolio volatility under a single-correlation approximation.

    A full covariance matrix is the right answer and is what a production risk
    engine supplies. This approximation exists so the strategist has *a* number
    to sanity-check its own estimate against, and it is deliberately explicit
    about the assumption it makes rather than hiding it behind a matrix.
    """
    symbols = [s for s in weights_pct if s in series and len(series[s]) > 1]
    if not symbols:
        return 0.0
    vols = {s: annualised_volatility_pct(series[s]) / 100.0 for s in symbols}
    weights = {s: weights_pct[s] / 100.0 for s in symbols}

    variance = 0.0
    for i in symbols:
        for j in symbols:
            rho = 1.0 if i == j else assumed_correlation
            variance += weights[i] * weights[j] * vols[i] * vols[j] * rho
    return math.sqrt(max(variance, 0.0)) * 100.0
