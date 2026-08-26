"""The deployed screening endpoint.

``api/screen.py`` is the only publicly reachable surface in this repository and
had no tests. It is also the one piece that cannot be imported normally: Vercel
treats the file as an entry point, so it is loaded here by path.

The HTTP layer is driven over a real loopback socket rather than by poking at a
mocked handler. Status codes, headers and body limits are the contract a browser
and a curl user actually meet, and a handler tested only through its own methods
can pass while returning 200 for everything.
"""

from __future__ import annotations

import importlib.util
import json
import math
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

_SCREEN_PY = Path(__file__).resolve().parents[1] / "api" / "screen.py"
_spec = importlib.util.spec_from_file_location("api_screen", _SCREEN_PY)
assert _spec is not None and _spec.loader is not None
screen_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(screen_module)

screen = screen_module.screen
ScreenError = screen_module.ScreenError

VALID = {
    "mandate": "sharia",
    "jurisdiction": "SA",
    "summary": "A balanced Shari'ah portfolio.",
    "allocations": [
        {"symbol": "SUKUK.GCC", "weight_pct": 60.0},
        {"symbol": "2222.SR", "weight_pct": 40.0},
    ],
    "disclosures": ["Capital is at risk."],
}


# --------------------------------------------------------------------------- #
# The screening function
# --------------------------------------------------------------------------- #
class TestScreen:
    def test_returns_a_verdict_without_calling_a_model(self) -> None:
        """The endpoint's entire reason for existing: no model, no credential."""
        result = screen(VALID)
        assert result["verdict"] in {"pass", "revise", "block"}
        assert result["model_calls"] == 0

    def test_declares_that_its_reference_data_is_fabricated(self) -> None:
        """A screener that hid this would be worse than no screener."""
        result = screen(VALID)
        assert result["data_source"] == "synthetic"
        assert "fabricated" in result["notice"]
        assert "not investment advice" in result["notice"]

    def test_accounts_for_every_principle_in_force(self) -> None:
        """The arithmetic/model split is the headline claim; it must add up."""
        result = screen(VALID)
        assert result["principles_in_force"] > 0
        assert (
            result["settled_by_arithmetic"] + result["judged_by_model"]
            == result["principles_in_force"]
        )
        # Only the deterministic half is served here, so findings can never
        # exceed the number of principles settled by arithmetic.
        assert len(result["findings"]) <= result["settled_by_arithmetic"]

    def test_is_reproducible(self) -> None:
        """Same input, same verdict, forever — the property a regulator asks for."""
        first, second = screen(VALID), screen(VALID)
        for result in (first, second):
            result.pop("elapsed_ms")
        assert first == second

    def test_findings_carry_their_provenance(self) -> None:
        for finding in screen(VALID)["findings"]:
            assert finding["principle_id"]
            assert finding["title"]
            assert finding["severity"] in {"blocking", "material", "advisory"}
            assert finding["status"] in {"pass", "fail", "unverifiable"}

    def test_an_absent_disclosure_is_screened_not_assumed(self) -> None:
        """An empty disclosures list is legitimate input the screens may fail."""
        payload = dict(VALID, disclosures=[])
        statuses = {f["principle_id"]: f["status"] for f in screen(payload)["findings"]}
        assert any(status != "pass" for status in statuses.values())


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #
class TestValidation:
    @pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
    def test_non_finite_expectations_are_refused(self, bad: float) -> None:
        """json.loads accepts NaN/Infinity, and no screen reads these fields.

        Without this check the endpoint returns a confident verdict on a
        portfolio claiming an infinite expected return, having never remarked on
        it.
        """
        with pytest.raises(ScreenError, match="finite"):
            screen(dict(VALID, expected_return_pct=bad))
        with pytest.raises(ScreenError, match="finite"):
            screen(dict(VALID, expected_volatility_pct=bad))

    @pytest.mark.parametrize("bad", [math.inf, math.nan])
    def test_non_finite_weights_are_refused(self, bad: float) -> None:
        payload = dict(VALID, allocations=[{"symbol": "SUKUK.GCC", "weight_pct": bad}])
        with pytest.raises(ScreenError, match="finite"):
            screen(payload)

    @pytest.mark.parametrize("weight", [-1.0, 100.1, 1000.0])
    def test_out_of_range_weights_name_the_offending_allocation(self, weight: float) -> None:
        payload = dict(VALID, allocations=[{"symbol": "SUKUK.GCC", "weight_pct": weight}])
        with pytest.raises(ScreenError, match=r"allocations\[0\]"):
            screen(payload)

    def test_unknown_mandate_lists_the_valid_ones(self) -> None:
        with pytest.raises(ScreenError, match="sharia"):
            screen(dict(VALID, mandate="crypto"))

    def test_empty_allocations_are_refused(self) -> None:
        with pytest.raises(ScreenError, match="non-empty"):
            screen(dict(VALID, allocations=[]))

    def test_too_many_allocations_are_refused(self) -> None:
        many = [{"symbol": f"SYM{i}", "weight_pct": 1.0} for i in range(41)]
        with pytest.raises(ScreenError, match="At most"):
            screen(dict(VALID, allocations=many))

    def test_a_missing_symbol_names_its_index(self) -> None:
        payload = dict(VALID, allocations=[{"weight_pct": 50.0}])
        with pytest.raises(ScreenError, match=r"allocations\[0\].*symbol"):
            screen(payload)

    def test_model_validation_is_reported_by_field(self) -> None:
        """A pydantic dump of the whole payload is not a usable error message."""
        with pytest.raises(ScreenError, match="capital_usd"):
            screen(dict(VALID, capital_usd=0))

    def test_a_non_numeric_horizon_is_refused(self) -> None:
        with pytest.raises(ScreenError, match="horizon_years"):
            screen(dict(VALID, horizon_years="soon"))


# --------------------------------------------------------------------------- #
# The HTTP contract
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def base_url():
    server = HTTPServer(("127.0.0.1", 0), screen_module.handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post(url: str, body: bytes | str, content_type: str = "application/json"):
    data = body.encode("utf-8") if isinstance(body, str) else body
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": content_type}, method="POST"
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class TestHttp:
    def test_get_describes_the_contract(self, base_url: str) -> None:
        """A self-describing endpoint; its advertised example must be real."""
        with urllib.request.urlopen(base_url) as response:
            assert response.status == 200
            body = json.loads(response.read())
        assert body["endpoint"] == "POST /api/screen"
        # The documented example must actually screen, or the docs lie.
        assert screen(body["example_request"])["verdict"]

    def test_post_screens_a_portfolio(self, base_url: str) -> None:
        status, body = _post(base_url, json.dumps(VALID))
        assert status == 200
        assert body["verdict"] in {"pass", "revise", "block"}

    def test_cors_and_cache_headers_are_set(self, base_url: str) -> None:
        with urllib.request.urlopen(base_url) as response:
            assert response.headers["Access-Control-Allow-Origin"] == "*"
            # A compliance verdict cached in between would be served stale.
            assert response.headers["Cache-Control"] == "no-store"

    def test_malformed_json_is_a_400_not_a_500(self, base_url: str) -> None:
        status, body = _post(base_url, "{not json")
        assert status == 400
        assert "UTF-8 JSON" in body["error"]

    def test_a_non_object_body_is_refused(self, base_url: str) -> None:
        status, body = _post(base_url, "[1, 2, 3]")
        assert status == 400
        assert "JSON object" in body["error"]

    def test_an_empty_body_is_refused(self, base_url: str) -> None:
        status, body = _post(base_url, b"")
        assert status == 400
        assert "Empty" in body["error"]

    def test_an_oversized_body_is_refused_before_parsing(self, base_url: str) -> None:
        """413 rather than 400: the limit is about resources, not syntax."""
        oversized = json.dumps({"allocations": [], "pad": "x" * (64 * 1024 + 10)})
        status, _ = _post(base_url, oversized)
        assert status == 413

    def test_a_validation_failure_is_a_400_with_a_reason(self, base_url: str) -> None:
        status, body = _post(base_url, json.dumps(dict(VALID, mandate="crypto")))
        assert status == 400
        assert "crypto" in body["error"]

    def test_no_stack_trace_ever_reaches_the_caller(self, base_url: str) -> None:
        status, body = _post(base_url, json.dumps({"allocations": "not-a-list"}))
        assert status == 400
        rendered = json.dumps(body)
        assert "Traceback" not in rendered
        assert 'File "' not in rendered
