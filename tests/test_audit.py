"""Audit trail integrity.

The claim made to a regulator is "you can detect if this record was altered."
These tests are what backs that claim.
"""

from __future__ import annotations

import json

from finagent_nexus.audit import GENESIS_HASH, AuditTrail, digest


def test_chain_links_each_event_to_its_predecessor():
    trail = AuditTrail(correlation_id="test-chain")
    first = trail.record(actor="A", action="one")
    second = trail.record(actor="B", action="two")

    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.hash
    assert trail.verify()


def test_sequence_numbers_are_dense_and_ordered():
    trail = AuditTrail()
    for index in range(5):
        trail.record(actor="A", action=f"step-{index}")
    assert [e.seq for e in trail.events] == [0, 1, 2, 3, 4]


def test_altering_an_event_breaks_verification():
    trail = AuditTrail()
    trail.record(actor="ComplianceOfficer", action="review", status="block")
    trail.record(actor="Orchestrator", action="finalize")

    # Simulate someone quietly turning a block into an approval.
    trail._events[0].status = "pass"
    assert not trail.verify()


def test_deleting_an_event_breaks_verification():
    trail = AuditTrail()
    for index in range(3):
        trail.record(actor="A", action=f"step-{index}")
    del trail._events[1]
    assert not trail.verify()


def test_canonical_digest_is_key_order_independent():
    """Key reordering must not break a chain; a changed value must."""
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"a": 1, "b": 2}) != digest({"a": 1, "b": 3})


def test_roundtrip_through_disk_preserves_verifiability(tmp_path):
    trail = AuditTrail(correlation_id="ondisk", directory=tmp_path)
    trail.record(actor="MarketAnalyst", action="analyse", usage={"input_tokens": 10})
    trail.record(actor="ComplianceOfficer", action="review", status="pass")

    assert trail.path is not None and trail.path.exists()

    reloaded = AuditTrail.load(trail.path)
    assert len(reloaded.events) == 2
    assert reloaded.verify()
    assert [e.hash for e in reloaded.events] == [e.hash for e in trail.events]


def test_tampering_with_the_file_is_detected_on_reload(tmp_path):
    """An auditor reading the file alone must be able to detect the edit."""
    trail = AuditTrail(correlation_id="tampered", directory=tmp_path)
    trail.record(actor="ComplianceOfficer", action="review", status="block")
    trail.record(actor="Orchestrator", action="finalize", status="escalated")

    assert trail.path is not None
    lines = trail.path.read_text(encoding="utf-8").strip().splitlines()
    payload = json.loads(lines[0])
    payload["status"] = "pass"
    lines[0] = json.dumps(payload)
    trail.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not AuditTrail.load(trail.path).verify()


def test_summary_aggregates_usage_across_events():
    trail = AuditTrail()
    trail.record(
        actor="A",
        action="one",
        usage={"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 80},
        latency_ms=500,
    )
    trail.record(
        actor="B",
        action="two",
        usage={"input_tokens": 200, "output_tokens": 30, "cache_read_input_tokens": 150},
        latency_ms=700,
    )

    summary = trail.summary()
    assert summary["input_tokens"] == 300
    assert summary["output_tokens"] == 50
    assert summary["cache_read_input_tokens"] == 230
    assert summary["latency_ms"] == 1200
    assert summary["chain_valid"] is True


def test_in_memory_trail_writes_nothing(tmp_path):
    trail = AuditTrail(directory=None)
    trail.record(actor="A", action="one")
    assert trail.path is None
    assert list(tmp_path.iterdir()) == []
