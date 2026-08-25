"""FinAgent-Nexus — Multi-Agent Financial Intelligence.

A compliance-first agentic system built on an explicit Plan-Act-Verify state
machine. Three specialised agents cooperate under deterministic guardrails:

    MarketAnalyst      — tool-using analysis of market data and risk metrics
    ComplianceOfficer  — Constitutional AI review against Sharia + regulatory rules
    WealthStrategist   — planning and final recommendation synthesis

The orchestration graph is defined in :mod:`finagent_nexus.graph`.
"""

from finagent_nexus.config import Settings
from finagent_nexus.state import (
    ClientRequest,
    ComplianceReview,
    Mandate,
    MarketBrief,
    NexusState,
    Plan,
    Recommendation,
    Verdict,
)

__version__ = "0.1.0"

__all__ = [
    "ClientRequest",
    "ComplianceReview",
    "Mandate",
    "MarketBrief",
    "NexusState",
    "Plan",
    "Recommendation",
    "Settings",
    "Verdict",
    "__version__",
]
