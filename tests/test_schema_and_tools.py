"""Structured-output schemas and the tool surface.

The schema tests guard a subtle failure mode: Pydantic emits ``$ref``/``$defs``
for nested models, and a schema that still contains a ``$ref`` is rejected by
the structured-output validator at request time — in production, not in CI.
"""

from __future__ import annotations

import json

import pytest

from finagent_nexus.llm import strict_json_schema
from finagent_nexus.state import (
    ComplianceFindings,
    MarketBrief,
    Plan,
    Recommendation,
)
from finagent_nexus.tools import TOOL_SPECS, ToolDispatcher

ALL_OUTPUT_MODELS = [Plan, MarketBrief, Recommendation, ComplianceFindings]


@pytest.mark.parametrize("model", ALL_OUTPUT_MODELS, ids=lambda m: m.__name__)
def test_schema_is_self_contained(model):
    """No dangling references anywhere in the tree."""
    schema = strict_json_schema(model)
    raw = json.dumps(schema)
    assert "$ref" not in raw, f"{model.__name__} schema still contains a $ref"
    assert "$defs" not in raw, f"{model.__name__} schema still contains $defs"


@pytest.mark.parametrize("model", ALL_OUTPUT_MODELS, ids=lambda m: m.__name__)
def test_every_object_node_is_strict(model):
    schema = strict_json_schema(model)

    def walk(node, path="root"):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False, f"{path} allows extra keys"
                assert set(node["required"]) == set(node["properties"]), (
                    f"{path} does not require every property"
                )
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(schema)


def test_nested_models_are_inlined():
    """``Recommendation.allocations`` holds ``Allocation`` objects, not a $ref."""
    schema = strict_json_schema(Recommendation)
    items = schema["properties"]["allocations"]["items"]
    assert items["type"] == "object"
    assert set(items["properties"]) == {"symbol", "weight_pct", "rationale"}


def test_enum_fields_survive_inlining():
    """``FindingStatus`` is an Enum — Pydantic emits it via $defs."""
    schema = strict_json_schema(ComplianceFindings)
    status = schema["properties"]["findings"]["items"]["properties"]["status"]
    assert set(status["enum"]) == {"pass", "fail", "unverifiable"}


# --------------------------------------------------------------------------- #
# Tool surface
# --------------------------------------------------------------------------- #
def test_every_tool_declares_a_strict_schema():
    for spec in TOOL_SPECS:
        assert spec["strict"] is True, f"{spec['name']} is not strict"
        schema = spec["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert spec["description"].strip(), f"{spec['name']} has no description"


def test_tool_names_are_unique():
    names = [spec["name"] for spec in TOOL_SPECS]
    assert len(names) == len(set(names))


def test_dispatcher_handles_every_declared_tool(dispatcher):
    """A declared tool with no handler would surface as a model-visible error."""
    arguments = {
        "get_quote": {"symbol": "2222.SR"},
        "get_price_history": {"symbol": "2222.SR", "days": 60},
        "compute_risk_metrics": {"symbol": "2222.SR", "days": 60, "risk_free_rate_pct": 0.0},
        "get_screening_data": {"symbol": "2222.SR"},
        "list_universe": {"mandate": "sharia"},
    }
    for spec in TOOL_SPECS:
        payload, is_error = dispatcher(spec["name"], arguments[spec["name"]])
        assert not is_error, f"{spec['name']} returned an error: {payload}"
        assert json.loads(payload)


def test_unknown_symbol_is_returned_as_an_error_not_raised(dispatcher):
    payload, is_error = dispatcher("get_quote", {"symbol": "NOPE"})
    assert is_error
    assert "unknown symbol" in json.loads(payload)["error"]


def test_unknown_tool_is_returned_as_an_error(dispatcher):
    payload, is_error = dispatcher("drop_tables", {})
    assert is_error
    assert "unknown tool" in json.loads(payload)["error"]


def test_malformed_arguments_do_not_raise(dispatcher):
    payload, is_error = dispatcher("get_price_history", {"symbol": "2222.SR", "days": "many"})
    assert is_error
    assert "error" in json.loads(payload)


def test_results_echo_their_arguments(dispatcher):
    """Echoed arguments are what make a transcript reconcilable as evidence."""
    payload, _ = dispatcher("get_quote", {"symbol": "2222.SR"})
    assert json.loads(payload)["symbol"] == "2222.SR"


def test_price_history_is_deterministic(dispatcher):
    first, _ = dispatcher("get_price_history", {"symbol": "2222.SR", "days": 120})
    second, _ = dispatcher("get_price_history", {"symbol": "2222.SR", "days": 120})
    assert first == second
