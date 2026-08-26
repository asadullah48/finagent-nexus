"""Claude access layer.

One class, :class:`ClaudeClient`, wraps every model call the system makes. It
exists to enforce four properties that are easy to lose when agents call the API
directly:

1. **Typed output.** Every call declares a Pydantic model and gets a validated
   instance back, or raises. No downstream ``.get("maybe_present")``.
2. **Recorded tool calls.** The client — not the model — records what actually
   executed, so a brief's evidence can be reconciled against the transcript.
3. **Bounded loops.** Tool iteration is capped. An agent cannot spin.
4. **Uniform usage accounting.** Token counts come back on every result and flow
   into the audit trail.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

import anthropic
from anthropic.types import MessageParam, ToolResultBlockParam
from pydantic import BaseModel, ValidationError

from finagent_nexus.config import Settings

T = TypeVar("T", bound=BaseModel)

ToolDispatch = Callable[[str, dict[str, Any]], tuple[str, bool]]


class AgentError(RuntimeError):
    """Raised when a model call cannot be turned into a valid typed result."""


class ModelRefusal(AgentError):
    """The model declined the request on safety grounds.

    Distinct from a malformed response: a refusal is a governance event and is
    recorded as such rather than retried blindly.
    """

    def __init__(self, category: str | None, explanation: str | None):
        self.category = category
        self.explanation = explanation
        super().__init__(f"model refused (category={category}): {explanation}")


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    result: str
    is_error: bool

    def as_evidence(self) -> str:
        """One transcript line, suitable for the brief's evidence list."""
        args = ", ".join(f"{k}={v!r}" for k, v in sorted(self.arguments.items()))
        status = "ERROR" if self.is_error else "ok"
        return f"{self.name}({args}) -> [{status}] {self.result}"


@dataclass
class LLMResult(Generic[T]):
    value: T
    raw_text: str
    usage: dict[str, Any]
    latency_ms: int
    stop_reason: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    def evidence(self) -> list[str]:
        return [record.as_evidence() for record in self.tool_calls]


# --------------------------------------------------------------------------- #
# JSON schema hardening
# --------------------------------------------------------------------------- #
def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Replace every ``$ref`` with a copy of its definition.

    Pydantic emits ``$defs``/``$ref`` for nested models and enums. Inlining
    keeps the schema self-contained, which is the shape the structured-output
    validator expects. The models here are non-recursive by construction; a
    recursive model would need a depth guard.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.split("/")[-1], {})
            merged = _inline_refs(dict(target), defs)
            # Sibling keys (e.g. `description`) win over the referenced definition.
            for key, value in node.items():
                if key != "$ref":
                    merged[key] = _inline_refs(value, defs)
            return merged
        return {key: _inline_refs(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    return node


_DROPPED_KEYS = frozenset({"title", "default", "$defs", "examples"})


def _harden(node: Any) -> Any:
    """Make every object node strict: all properties required, none extra."""
    if isinstance(node, dict):
        out = {k: _harden(v) for k, v in node.items() if k not in _DROPPED_KEYS}
        if out.get("type") == "object" and isinstance(out.get("properties"), dict):
            out["required"] = sorted(out["properties"].keys())
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_harden(item) for item in node]
    return node


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Build a self-contained, strict JSON schema for a Pydantic model."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    return _harden(_inline_refs(schema, defs))


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class ClaudeClient:
    """Thin, opinionated wrapper over ``client.messages.create``."""

    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None):
        self.settings = settings
        self._client = client or anthropic.Anthropic()

    def structured(
        self,
        *,
        system: str,
        user: str,
        output_model: type[T],
        model: str | None = None,
        effort: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        dispatch: ToolDispatch | None = None,
    ) -> LLMResult[T]:
        """Run one agent turn and return a validated ``output_model`` instance.

        If ``tools`` is supplied, the agentic loop runs here: the model may make
        as many tool calls as it needs, up to ``settings.max_tool_iterations``,
        before producing its final structured answer.
        """
        if tools and dispatch is None:
            raise ValueError("tools were supplied without a dispatch function")

        started = time.perf_counter()
        usage_total: dict[str, int] = {}
        tool_records: list[ToolCallRecord] = []

        request: dict[str, Any] = {
            "model": model or self.settings.model,
            "max_tokens": self.settings.max_tokens,
            # The system prompt is the stable prefix; caching it is the single
            # largest cost lever in a multi-revision run, where the same
            # constitution is re-sent on every pass.
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": effort or self.settings.effort,
                "format": {
                    "type": "json_schema",
                    "schema": strict_json_schema(output_model),
                },
            },
        }
        if tools:
            request["tools"] = tools

        messages: list[MessageParam] = [{"role": "user", "content": user}]
        response = None

        for _ in range(self.settings.max_tool_iterations):
            response = self._client.messages.create(**request, messages=messages)
            _accumulate_usage(usage_total, response.usage)

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                raise ModelRefusal(
                    getattr(details, "category", None), getattr(details, "explanation", None)
                )

            if response.stop_reason == "pause_turn":
                # A server-side tool hit its iteration limit mid-turn. Echo the
                # partial assistant turn back to resume it.
                messages.append({"role": "assistant", "content": response.content})
                continue

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break

            messages.append({"role": "assistant", "content": response.content})
            # Typed as the SDK's own block type rather than `dict[str, Any]`:
            # the four keys below are an API contract, and a typo in one of them
            # surfaces as a 400 from Anthropic mid-run rather than at the edit.
            results: list[ToolResultBlockParam] = []
            for block in tool_uses:
                arguments = dict(block.input or {})
                payload, is_error = dispatch(block.name, arguments)  # type: ignore[misc]
                tool_records.append(
                    ToolCallRecord(
                        name=block.name, arguments=arguments, result=payload, is_error=is_error
                    )
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": payload,
                        "is_error": is_error,
                    }
                )
            # All results go back in a single user message. Splitting them
            # teaches the model to stop issuing parallel calls.
            messages.append({"role": "user", "content": results})
        else:
            raise AgentError(
                f"tool loop exceeded {self.settings.max_tool_iterations} iterations "
                f"without a final answer"
            )

        if response is None:  # pragma: no cover — unreachable given the loop bound
            raise AgentError("no response received")

        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text.strip():
            raise AgentError(
                f"model returned no text block (stop_reason={response.stop_reason})"
            )

        try:
            value = output_model.model_validate_json(text)
        except ValidationError as exc:
            raise AgentError(
                f"{output_model.__name__} validation failed: {exc}\n"
                f"raw response: {text[:1000]}"
            ) from exc

        return LLMResult(
            value=value,
            raw_text=text,
            usage=usage_total,
            latency_ms=int((time.perf_counter() - started) * 1000),
            stop_reason=response.stop_reason or "end_turn",
            tool_calls=tool_records,
        )


def _accumulate_usage(total: dict[str, int], usage: Any) -> None:
    """Sum token counters across every turn of a tool loop."""
    if usage is None:
        return
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    for name in fields:
        value = getattr(usage, name, None)
        if isinstance(value, int):
            total[name] = total.get(name, 0) + value


def compact_json(payload: Any) -> str:
    """Render a payload for inclusion in a prompt — stable key order, no fluff."""
    return json.dumps(payload, sort_keys=True, indent=2, default=str)
