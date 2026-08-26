"""The auditor-facing commands.

``verify-audit``, ``replay`` and ``eval`` are the three subcommands a second line
of defence actually runs, and none of them had a test. They are exercised here
through :func:`finagent_nexus.cli.main` — argument parsing, exit codes and all —
because an exit code *is* the interface when these run in CI or a scheduled
validation job. Asserting on the underlying functions would leave the part that
fails a build untested.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finagent_nexus.audit import AuditTrail
from finagent_nexus.cli import main

DATASET = Path(__file__).parent / "eval" / "datasets" / "golden_cases.json"


def _write_trail(directory: Path) -> Path:
    """A trail carrying one event of every action the replay renderer handles."""
    trail = AuditTrail(correlation_id="test-run", directory=directory)
    trail.record(
        actor="Orchestrator",
        action="accept_mandate",
        detail={
            "request": {
                "client_id": "CL-TEST-001",
                "objective": "Grow capital under a Shari'ah mandate.",
                "capital_usd": 1_000_000.0,
                "horizon_years": 10,
                "risk_tolerance": "balanced",
                "jurisdiction": "SA",
                "mandate": "sharia",
                "constraints": ["No conventional banks."],
            },
            "settings": {
                "model": "claude-opus-5",
                "effort": "high",
                "allow_synthetic_data": True,
            },
        },
    )
    trail.record(
        actor="WealthStrategist",
        action="plan",
        model="claude-opus-5",
        usage={"input_tokens": 1200, "output_tokens": 300},
        latency_ms=900,
        detail={
            "thesis": "Sukuk anchor with selective equity.",
            "universe": ["2222.SR", "SUKUK.GCC"],
            "steps": ["s1"],
        },
    )
    trail.record(
        actor="MarketAnalyst",
        action="analyse",
        model="claude-opus-5",
        usage={"input_tokens": 3000, "output_tokens": 800, "cache_read_input_tokens": 1100},
        latency_ms=4200,
        detail={
            "revision": 0,
            "instruments": ["2222.SR", "SUKUK.GCC"],
            "tool_calls": 4,
            "tool_errors": 0,
            "evidence": ["get_quote(2222.SR) -> 28.40 SAR"],
        },
    )
    trail.record(
        actor="WealthStrategist",
        action="synthesize",
        model="claude-opus-5",
        usage={"input_tokens": 2100, "output_tokens": 650},
        latency_ms=3100,
        detail={
            "revision": 0,
            "allocations": {"SUKUK.GCC": 70.0, "2222.SR": 30.0},
            "expected_return_pct": 6.8,
            "expected_volatility_pct": 9.4,
        },
    )
    trail.record(
        actor="ComplianceOfficer",
        action="review",
        status="pass",
        model="claude-opus-5",
        usage={"input_tokens": 5000, "output_tokens": 900},
        latency_ms=5200,
        detail={
            "revision": 0,
            "revisions_remaining": 2,
            "verdict": "pass",
            "findings": {"SHARIA-SCREEN-02": "pass", "REG-CONC-02": "pass"},
            "blocking_failures": [],
            "remediations": [],
        },
    )
    trail.record(
        actor="Orchestrator",
        action="finalize",
        status="approved",
        detail={
            "outcome": "approved",
            "revisions_used": 0,
            "halted_reason": None,
            "verdict": "pass",
        },
    )
    assert trail.path is not None
    return trail.path


@pytest.fixture
def trail_path(tmp_path: Path) -> Path:
    return _write_trail(tmp_path)


def _tamper(path: Path, old: str, new: str) -> Path:
    """Edit a recorded value and leave its hash alone — the attack the chain exists for."""
    tampered = path.with_name("tampered.jsonl")
    text = path.read_text(encoding="utf-8")
    assert old in text, "fixture drifted: nothing to tamper with"
    tampered.write_text(text.replace(old, new), encoding="utf-8")
    return tampered


# --------------------------------------------------------------------------- #
# verify-audit
# --------------------------------------------------------------------------- #
class TestVerifyAudit:
    def test_intact_trail_exits_zero(self, trail_path: Path) -> None:
        assert main(["verify-audit", str(trail_path)]) == 0

    def test_tampered_trail_exits_one(self, trail_path: Path) -> None:
        tampered = _tamper(trail_path, '"SUKUK.GCC":70.0', '"SUKUK.GCC":40.0')
        assert main(["verify-audit", str(tampered)]) == 1

    def test_missing_file_exits_two(self, tmp_path: Path) -> None:
        """Distinct from 1: a missing file is an operator error, not a tampered record."""
        assert main(["verify-audit", str(tmp_path / "absent.jsonl")]) == 2


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
class TestReplay:
    def test_reconstructs_the_decision(self, trail_path: Path, capsys) -> None:
        assert main(["replay", str(trail_path)]) == 0
        out = capsys.readouterr().out

        # The mandate, as accepted.
        assert "CL-TEST-001" in out
        assert "sharia/SA" in out
        assert "No conventional banks." in out
        # Which data regime it ran under. A flawless trail over fabricated prices
        # is the failure mode this line exists to make visible.
        assert "Synthetic   : True" in out
        # Plan, evidence, portfolio, findings, outcome.
        assert "Sukuk anchor with selective equity." in out
        assert "get_quote(2222.SR) -> 28.40 SAR" in out
        assert "SUKUK.GCC" in out
        assert "70.0%" in out
        assert "SHARIA-SCREEN-02" in out
        assert "APPROVED" in out

    def test_totals_the_weights(self, trail_path: Path, capsys) -> None:
        main(["replay", str(trail_path)])
        assert "TOTAL            100.0%" in capsys.readouterr().out

    def test_aggregates_cost_and_latency(self, trail_path: Path, capsys) -> None:
        main(["replay", str(trail_path)])
        out = capsys.readouterr().out
        assert "11300/2650" in out  # summed across every recorded event
        assert "cached 1100" in out
        assert "13400 ms" in out

    def test_refuses_a_tampered_trail(self, trail_path: Path, capsys) -> None:
        """Fail closed: a tidy report from an altered record launders the tampering."""
        tampered = _tamper(trail_path, '"SUKUK.GCC":70.0', '"SUKUK.GCC":40.0')
        assert main(["replay", str(tampered)]) == 1

        captured = capsys.readouterr()
        assert "BROKEN" in captured.err
        # The altered figure must not reach stdout at all.
        assert "40.0" not in captured.out
        assert "Recommendation" not in captured.out

    def test_missing_file_exits_two(self, tmp_path: Path) -> None:
        assert main(["replay", str(tmp_path / "absent.jsonl")]) == 2


# --------------------------------------------------------------------------- #
# eval
# --------------------------------------------------------------------------- #
class TestEval:
    def test_refuses_synthetic_data_without_opt_in(self, monkeypatch, capsys) -> None:
        """The eval must not run under a data regime the runner would have refused."""
        monkeypatch.delenv("FINAGENT_ALLOW_SYNTHETIC_DATA", raising=False)
        assert main(["eval", "--dataset", str(DATASET)]) == 2
        assert "must never inform live advice" in capsys.readouterr().err

    def test_passes_the_bundled_golden_cases(self, capsys) -> None:
        assert main(["eval", "--dataset", str(DATASET), "--allow-synthetic-data"]) == 0
        out = capsys.readouterr().out
        assert "missed:       0" in out
        assert "false alarms: 0" in out

    def test_missing_dataset_exits_two(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "absent.json")
        assert main(["eval", "--dataset", missing, "--allow-synthetic-data"]) == 2

    def test_a_disagreeing_case_fails_the_build(self, tmp_path: Path) -> None:
        """The whole point of the command: a case that no longer holds exits non-zero.

        Rather than break a screen, this flips a case's *expected* verdict — the
        same disagreement from the other side. It proves the exit code is driven
        by the comparison, not merely by the suite having run.
        """
        payload = json.loads(DATASET.read_text(encoding="utf-8"))
        for case in payload["cases"]:
            if not case["expect"].get("must_fail"):
                case["expect"]["verdict_with_budget"] = "block"
                break
        else:  # pragma: no cover — the bundled dataset always has a clean case
            pytest.skip("no clean case to invert")

        broken = tmp_path / "broken.json"
        broken.write_text(json.dumps(payload), encoding="utf-8")
        assert main(["eval", "--dataset", str(broken), "--allow-synthetic-data"]) == 1
