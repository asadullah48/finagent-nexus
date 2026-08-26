"""Which market data a run is allowed to execute on.

This policy used to live inside ``NexusRunner._resolve_provider``, which meant
the only way to ask "may this process use synthetic prices?" was to construct a
runner — and constructing a runner imports LangGraph and the Anthropic SDK. Any
deterministic-side caller wanting the same guarantee had to restate the rule, and
a restated rule is a rule with two versions of the truth.

It is the same argument that moved :func:`~finagent_nexus.verdict.aggregate_verdict`
out of the ComplianceOfficer module. A control that cannot be reached without the
model half installed is not available to the half of the system that is supposed
to run without it.

This module imports the market data Protocol and nothing else.
"""

from __future__ import annotations

from finagent_nexus.tools.market_data import MarketDataProvider, SyntheticMarketData

__all__ = ["REFUSAL", "SyntheticDataNotPermitted", "resolve_provider"]


class SyntheticDataNotPermitted(RuntimeError):
    """No provider was supplied and fabricated prices are not permitted here."""


#: Kept as one string so the runner, the CLI, and the eval harness all refuse in
#: the same words. Divergent refusal messages are how an operator discovers that
#: a control has quietly acquired two implementations.
REFUSAL = (
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


def resolve_provider(
    provider: MarketDataProvider | None, *, allow_synthetic: bool
) -> MarketDataProvider:
    """Decide what market data a caller is allowed to run on.

    Synthetic data is **opt-in and never inherited**. An explicit provider always
    wins; absent one, :class:`SyntheticMarketData` is supplied only when
    ``allow_synthetic`` says so, and otherwise this raises rather than falling
    back.

    The rejected alternative was to default the fallback on, so the repository
    ran out of the box. That makes forgetting to inject a provider
    indistinguishable from a working deployment: the arithmetic screens still
    run, the constitution is still applied, and the hash-chained trail still
    notarises a beautifully compliant recommendation — built entirely on prices
    fabricated from a hash of the symbol. The audit trail does not protect you
    there; it records the wrong thing, credibly. A loud failure at construction
    is cheaper than a credible record of a fictional portfolio.

    Args:
        provider: An explicit provider, or ``None`` to fall back to policy.
        allow_synthetic: Whether the bundled fixture may be substituted.

    Raises:
        SyntheticDataNotPermitted: No provider was supplied and synthetic data
            is not permitted by the caller's settings.
    """
    if provider is not None:
        return provider
    if not allow_synthetic:
        raise SyntheticDataNotPermitted(REFUSAL)
    return SyntheticMarketData()
