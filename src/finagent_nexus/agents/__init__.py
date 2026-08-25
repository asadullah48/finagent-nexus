"""The three specialised agents."""

from finagent_nexus.agents.compliance_officer import ComplianceOfficer, aggregate_verdict
from finagent_nexus.agents.market_analyst import MarketAnalyst
from finagent_nexus.agents.wealth_strategist import WealthStrategist

__all__ = ["ComplianceOfficer", "MarketAnalyst", "WealthStrategist", "aggregate_verdict"]
