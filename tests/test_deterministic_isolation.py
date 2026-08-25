"""The determinism boundary must hold in the dependency graph, not just on paper.

The deterministic half of the system — the arithmetic screens and the verdict
policy — is meant to be runnable offline, with no API key, by anyone who wants to
reproduce a decision. That promise is only real if importing it does not drag in
the model client.

This regressed once already: ``aggregate_verdict`` lived in the ComplianceOfficer
module, so screening a portfolio transitively imported ``anthropic``. Nothing
failed, because the SDK happened to be installed everywhere it ran. The leak was
invisible until a deployment tried to install only what it actually needed.

These tests run in a subprocess on purpose. By the time the rest of the suite has
run, the agent modules are already in ``sys.modules``, so an in-process check
would pass no matter how badly the boundary leaked.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

FORBIDDEN = ("anthropic", "langgraph")


def _forbidden_modules_after(statements: str) -> set[str]:
    """Run `statements` in a fresh interpreter; return any forbidden modules loaded."""
    template = textwrap.dedent(
        """
        import json, sys
        {statements}
        print(json.dumps([m for m in {forbidden!r} if m in sys.modules]))
        """
    )
    # The placeholder sits at column 0 in the template, so the block must be
    # fully dedented — indenting it produces an IndentationError in the child.
    code = template.format(
        statements=textwrap.dedent(statements).strip(),
        forbidden=FORBIDDEN,
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, "subprocess failed: " + result.stderr
    return set(json.loads(result.stdout.strip().splitlines()[-1]))


def test_checks_do_not_import_the_model_client() -> None:
    assert _forbidden_modules_after("import finagent_nexus.checks") == set()


def test_verdict_policy_does_not_import_the_model_client() -> None:
    """Risk appetite is arithmetic over severities. It must not require an SDK."""
    assert _forbidden_modules_after("from finagent_nexus.verdict import aggregate_verdict") == set()


def test_a_full_offline_screen_needs_no_model_client() -> None:
    """The exact import surface the deployed screening endpoint depends on."""
    leaked = _forbidden_modules_after(
        """
        from finagent_nexus.checks import run_machine_checks
        from finagent_nexus.state import Allocation, ClientRequest, Mandate, Recommendation
        from finagent_nexus.tools.market_data import SyntheticMarketData
        from finagent_nexus.verdict import aggregate_verdict

        request = ClientRequest(
            client_id="TEST",
            objective="Screen a portfolio.",
            capital_usd=1000000.0,
            horizon_years=10,
            risk_tolerance="balanced",
            jurisdiction="SA",
            mandate=Mandate.SHARIA,
        )
        recommendation = Recommendation(
            summary="A diversified Sharia-compliant allocation.",
            allocations=[
                Allocation(symbol=symbol, weight_pct=20.0, rationale="test")
                for symbol in ("SUKUK.GCC", "2222.SR", "GLD", "SPUS", "DIB.DU")
            ],
            expected_return_pct=7.0,
            expected_volatility_pct=11.0,
            review_cadence="Quarterly.",
            disclosures=["Capital is at risk."],
        )
        findings = run_machine_checks(recommendation, request, SyntheticMarketData())
        verdict, _, _ = aggregate_verdict(findings, 1)
        assert verdict.value == "pass", verdict
        """
    )
    assert leaked == set(), "deterministic path pulled in: " + repr(sorted(leaked))


def test_aggregate_verdict_keeps_its_original_import_paths() -> None:
    """Relocating the policy must not break existing callers."""
    from finagent_nexus.agents import aggregate_verdict as from_package
    from finagent_nexus.agents.compliance_officer import aggregate_verdict as from_module
    from finagent_nexus.verdict import aggregate_verdict as canonical

    assert from_package is canonical
    assert from_module is canonical
