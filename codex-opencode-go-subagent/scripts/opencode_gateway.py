"""Pure protocol-core gateway for the OpenCode Go subagent (DP-20260811-03-V1).

This module performs only in-memory request translation, stream translation and
reasoning-state handling.  It never opens sockets, reads configuration or
credentials, or persists state to disk.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import secrets
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

_MAX_REASONING_ENTRIES = 2048
_REASONING_TTL_SECONDS = 7200.0
_MESSAGES_MAX_TOKENS = 32000


class GatewayError(Exception):
    """Structured protocol error; messages never contain secrets."""

    def __init__(self, code, status, message):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


class ReasoningStore:
    """Instance-local in-memory reasoning state store with LRU and TTL."""

    def __init__(self, clock=time.monotonic, token_factory=lambda: secrets.token_urlsafe(32)):
        self._clock = clock
        self._token_factory = token_factory
        self._entries = OrderedDict()

    def save(self, state):
        handle = "ocg1:" + self._token_factory()
        self._entries[handle] = (self._clock(), copy.deepcopy(state))
        self._entries.move_to_end(handle)
        if len(self._entries) > _MAX_REASONING_ENTRIES:
            self._entries.popitem(last=False)
        return handle

    def get(self, handle):
        entry = self._entries.pop(handle, None)
        if entry is None:
            raise GatewayError(
                "reasoning_state_expired", 409, "reasoning state is missing or expired"
            )
        created, state = entry
        if self._clock() - created >= _REASONING_TTL_SECONDS:
            raise GatewayError(
                "reasoning_state_expired", 409, "reasoning state is missing or expired"
            )
        self._entries[handle] = (created, state)
        self._entries.move_to_end(handle)
        return copy.deepcopy(state)

    def __repr__(self):
        return f"ReasoningStore(count={len(self._entries)})"

    __str__ = __repr__


@dataclass(frozen=True)
class UpstreamRequest:
    transport: str
    path: str
    headers: dict
    body: dict
    custom_tool_names: tuple

    def __repr__(self):
        return (
            f"UpstreamRequest(transport={self.transport!r}, path={self.path!r}, "
            f"custom_tool_names={self.custom_tool_names!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str


def _load_models():
    if "opencode_models" in sys.modules:
        return sys.modules["opencode_models"]
    path = Path(__file__).resolve().parent / "opencode_models.py"
    spec = importlib.util.spec_from_file_location("opencode_models", path)
    if spec is None or spec.loader is None:
        raise GatewayError("models_unavailable", 500, "cannot load opencode_models")
    module = importlib.util.module_from_spec(spec)
    sys.modules["opencode_models"] = module
    spec.loader.exec_module(module)
    return module


def _sse(event, payload):
    return SSEEvent(event, json.dumps(payload))


def _new_id_factory():
    counters = {}

    def make(prefix):
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}_{counters[prefix]}"

    return make


def _validate_message_content(content):
    if isinstance(content, str):
        return
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in (
                "input_text",
                "output_text",
            ):
                raise GatewayError(
                    "unsupported_input", 400, "only text content blocks are supported"
                )
            if not isinstance(block.get("text"), str):
                raise GatewayError(
                    "unsupported_input", 400, "text block requires a string text"
                )
        return
    raise GatewayError(
        "unsupported_input", 400, "message content must be a string or text blocks"
    )


def _validate_input_item(item):
    if not isinstance(item, dict):
        raise GatewayError("invalid_input", 400, "input item must be an object")
    itype = item.get("type")
    if itype == "message":
        if item.get("role") not in ("user", "assistant"):
            raise GatewayError(
                "unsupported_input", 400,
                f"unsupported message role: {item.get('role')!r}",
            )
        _validate_message_content(item.get("content"))
    elif itype in ("function_call", "custom_tool_call"):
        if not isinstance(item.get("name"), str) or not isinstance(item.get("call_id"), str):
            raise GatewayError("invalid_input", 400, f"invalid {itype} item")
        if itype == "function_call" and not isinstance(item.get("arguments"), str):
            raise GatewayError(
                "invalid_input", 400, "function_call requires string arguments"
            )
        if itype == "custom_tool_call" and not isinstance(item.get("input"), str):
            raise GatewayError(
                "invalid_request", 400, "custom_tool_call requires string input"
            )
    elif itype in ("function_call_output", "custom_tool_call_output"):
        if not isinstance(item.get("call_id"), str):
            raise GatewayError("invalid_input", 400, f"invalid {itype} item")
        if not isinstance(item.get("output"), str):
            raise GatewayError(
                "unsupported_output", 400, "tool output must be a plain string"
            )
    elif itype == "reasoning":
        if not isinstance(item.get("encrypted_content"), str):
            raise GatewayError(
                "invalid_input", 400, "reasoning item requires string encrypted_content"
            )
    else:
        raise GatewayError(
            "unsupported_input", 400, f"unsupported input item type: {itype!r}"
        )


def _validate_request(request):
    if not isinstance(request, dict):
        raise GatewayError("invalid_request", 400, "request must be an object")
    tools = request.get("tools") or []
    if not isinstance(tools, list):
        raise GatewayError("invalid_tools", 400, "tools must be a list")
    custom_names = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise GatewayError("invalid_tool", 400, "tool must be an object with a name")
        ttype = tool.get("type")
        if ttype == "function":
            continue
        if ttype == "custom":
            custom_names.append(tool["name"])
            continue
        raise GatewayError("unsupported_tool", 400, f"unsupported tool type: {ttype!r}")
    items = request.get("input") or []
    if not isinstance(items, list):
        raise GatewayError("invalid_input", 400, "input must be a list")
    for item in items:
        _validate_input_item(item)
    return tuple(custom_names)


def _message_text(content):
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content)


def _resolve_reasoning(reasoning_store, handle, transport):
    if reasoning_store is None:
        raise GatewayError("reasoning_state_expired", 409, "reasoning store is required")
    state = reasoning_store.get(handle)
    if state.get("transport") != transport:
        raise GatewayError(
            "reasoning_state_transport_mismatch", 409,
            "reasoning state was created for a different transport",
        )
    return state.get("payload")


def _store_reasoning(reasoning_store, transport, payload):
    if reasoning_store is None:
        raise GatewayError("reasoning_state_expired", 409, "reasoning store is required")
    return reasoning_store.save({"transport": transport, "payload": payload})


def _build_chat_body(request, model, effort, reasoning_store, custom_tool_names):
    messages = []
    pending_outputs = []
    pending_assistant = None
    items = request.get("input") or []
    count = len(items)

    def flush_outputs():
        for call_id, output in pending_outputs:
            messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": output}
            )
        pending_outputs.clear()

    def commit_assistant():
        nonlocal pending_assistant
        if pending_assistant is not None:
            messages.append(pending_assistant)
            pending_assistant = None

    def ensure_assistant():
        nonlocal pending_assistant
        if pending_assistant is None:
            pending_assistant = {"role": "assistant", "content": ""}
        return pending_assistant

    def append_tool_call(call):
        flush_outputs()
        assistant = ensure_assistant()
        tool_calls = assistant.setdefault("tool_calls", [])
        if call["type"] == "function_call":
            tool_calls.append(
                {
                    "id": call["call_id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": call["arguments"],
                    },
                }
            )
        else:
            tool_calls.append(
                {
                    "id": call["call_id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps({"input": call["input"]}),
                    },
                }
            )

    index = 0
    while index < count:
        item = items[index]
        itype = item["type"]
        if itype == "message":
            text = _message_text(item["content"])
            if item["role"] == "user":
                commit_assistant()
                flush_outputs()
                messages.append({"role": "user", "content": text})
            else:
                if pending_assistant is not None and pending_assistant.get("content"):
                    commit_assistant()
                    flush_outputs()
                elif pending_assistant is None:
                    flush_outputs()
                assistant = ensure_assistant()
                if text and not assistant.get("content"):
                    assistant["content"] = text
                cursor = index + 1
                while cursor < count and items[cursor]["type"] in (
                    "function_call",
                    "custom_tool_call",
                ):
                    append_tool_call(items[cursor])
                    cursor += 1
                index = cursor - 1
        elif itype in ("function_call", "custom_tool_call"):
            append_tool_call(item)
        elif itype in ("function_call_output", "custom_tool_call_output"):
            commit_assistant()
            pending_outputs.append((item["call_id"], item["output"]))
        elif itype == "reasoning":
            flush_outputs()
            payload = _resolve_reasoning(
                reasoning_store, item["encrypted_content"], "chat_completions"
            )
            assistant = ensure_assistant()
            assistant["reasoning_content"] = payload["reasoning_content"]
        index += 1
    commit_assistant()
    flush_outputs()

    tools = []
    for tool in request.get("tools") or []:
        if tool["type"] == "custom":
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description")
                        or f"Custom tool {tool['name']}",
                        "parameters": {
                            "type": "object",
                            "properties": {"input": {"type": "string"}},
                            "required": ["input"],
                        },
                    },
                }
            )
        else:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description")
                        or f"Function {tool['name']}",
                        "parameters": copy.deepcopy(tool.get("parameters", {})),
                    },
                }
            )

    body = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": request.get("tool_choice", "auto"),
        "parallel_tool_calls": request.get("parallel_tool_calls", True),
        "stream": request.get("stream", True),
        "stream_options": {"include_usage": True},
    }
    instructions = request.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.insert(0, {"role": "system", "content": instructions})
    if effort != "default":
        body["reasoning_effort"] = effort
    return body


def _thinking_for(model_id, effort):
    if model_id == "minimax-m3":
        if effort == "none":
            return {"type": "disabled"}
        return {"type": "enabled"}
    if model_id.startswith("qwen"):
        if effort == "none":
            return {"type": "disabled"}
        budget = 16000 if effort == "high" else 31999
        return {"type": "enabled", "budget_tokens": budget}
    raise GatewayError(
        "invalid_effort", 400,
        f"thinking not supported for {model_id} effort {effort!r}",
    )


def _messages_tool_choice(choice, parallel_tool_calls):
    if choice in (None, "auto"):
        return {
            "type": "auto",
            "disable_parallel_tool_use": not bool(parallel_tool_calls),
        }
    raise GatewayError(
        "unsupported_tool_choice", 400, f"unsupported tool_choice: {choice!r}"
    )


def _build_messages_body(request, model, effort, reasoning_store, custom_tool_names):
    messages = []
    pending_outputs = []
    pending_blocks = None
    items = request.get("input") or []
    count = len(items)

    def flush_outputs():
        if not pending_outputs:
            return
        results = [
            {"type": "tool_result", "tool_use_id": call_id, "content": output}
            for call_id, output in pending_outputs
        ]
        messages.append({"role": "user", "content": results})
        pending_outputs.clear()

    def commit_assistant():
        nonlocal pending_blocks
        if pending_blocks is not None:
            messages.append({"role": "assistant", "content": pending_blocks})
            pending_blocks = None

    def ensure_blocks():
        nonlocal pending_blocks
        if pending_blocks is None:
            pending_blocks = []
        return pending_blocks

    def append_tool_use(call):
        flush_outputs()
        blocks = ensure_blocks()
        if call["type"] == "function_call":
            try:
                parsed_input = json.loads(call["arguments"])
            except (TypeError, ValueError) as exc:
                raise GatewayError(
                    "invalid_tool_input", 400,
                    "function arguments must be valid JSON",
                ) from exc
            if not isinstance(parsed_input, dict):
                raise GatewayError(
                    "invalid_tool_input", 400,
                    "function arguments must be a JSON object",
                )
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call["call_id"],
                    "name": call["name"],
                    "input": parsed_input,
                }
            )
        else:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call["call_id"],
                    "name": call["name"],
                    "input": {"input": call["input"]},
                }
            )

    index = 0
    while index < count:
        item = items[index]
        itype = item["type"]
        if itype == "message":
            text = _message_text(item["content"])
            if item["role"] == "user":
                commit_assistant()
                flush_outputs()
                messages.append(
                    {"role": "user", "content": [{"type": "text", "text": text}]}
                )
            else:
                if pending_blocks is not None and pending_blocks:
                    commit_assistant()
                    flush_outputs()
                elif pending_blocks is None:
                    flush_outputs()
                blocks = ensure_blocks()
                if text:
                    blocks.append({"type": "text", "text": text})
                cursor = index + 1
                while cursor < count and items[cursor]["type"] in (
                    "function_call",
                    "custom_tool_call",
                ):
                    append_tool_use(items[cursor])
                    cursor += 1
                index = cursor - 1
        elif itype in ("function_call", "custom_tool_call"):
            append_tool_use(item)
        elif itype in ("function_call_output", "custom_tool_call_output"):
            commit_assistant()
            pending_outputs.append((item["call_id"], item["output"]))
        elif itype == "reasoning":
            flush_outputs()
            payload = _resolve_reasoning(
                reasoning_store, item["encrypted_content"], "anthropic_messages"
            )
            signature = payload.get("signature")
            thinking_blocks = []
            for thinking in payload.get("thinking", []):
                block = copy.deepcopy(thinking)
                if isinstance(block, dict) and block.get("type") == "thinking":
                    if "signature" not in block and signature:
                        block["signature"] = signature
                    thinking_blocks.append(block)
            if thinking_blocks:
                blocks = ensure_blocks()
                blocks[:0] = thinking_blocks
        index += 1
    commit_assistant()
    flush_outputs()

    tools = []
    for tool in request.get("tools") or []:
        if tool["type"] == "custom":
            tools.append(
                {
                    "name": tool["name"],
                    "description": tool.get("description")
                    or f"Custom tool {tool['name']}",
                    "input_schema": {
                        "type": "object",
                        "properties": {"input": {"type": "string"}},
                        "required": ["input"],
                    },
                }
            )
        else:
            tools.append(
                {
                    "name": tool["name"],
                    "description": tool.get("description")
                    or f"Function {tool['name']}",
                    "input_schema": copy.deepcopy(tool.get("parameters", {})),
                }
            )

    body = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "max_tokens": _MESSAGES_MAX_TOKENS,
        "stream": request.get("stream", True),
        "tool_choice": _messages_tool_choice(
            request.get("tool_choice", "auto"),
            request.get("parallel_tool_calls", True),
        ),
    }
    instructions = request.get("instructions")
    if isinstance(instructions, str) and instructions:
        body["system"] = instructions
    if effort != "default":
        body["thinking"] = _thinking_for(model, effort)
    return body


def _build_responses_body(request, model, effort, reasoning_store):
    body = copy.deepcopy(request)
    body["model"] = model
    source_reasoning = request.get("reasoning")
    if source_reasoning is None:
        body["reasoning"] = {"effort": effort} if effort != "default" else {}
    else:
        if not isinstance(source_reasoning, dict):
            raise GatewayError(
                "invalid_request", 400, "reasoning must be an object"
            )
        reasoning = dict(source_reasoning)
        if effort != "default":
            reasoning["effort"] = effort
        else:
            reasoning.pop("effort", None)
        body["reasoning"] = reasoning
    for item in body.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            payload = _resolve_reasoning(
                reasoning_store, item["encrypted_content"], "responses"
            )
            item["encrypted_content"] = payload["encrypted_content"]
    return body


def prepare_upstream_request(
    request, model, effort, api_key, reasoning_store=None, model_registry_snapshot=None
):
    models = _load_models()
    registry = None
    if model_registry_snapshot is not None:
        try:
            registry = models.registry_from_snapshot(model_registry_snapshot)
        except models.ModelError:
            raise GatewayError(
                "invalid_registry", 500, "invalid model registry"
            ) from None
    try:
        spec = models.get_model(model, effort, registry=registry)
    except Exception as exc:
        raise GatewayError(
            getattr(exc, "code", "invalid_profile"),
            getattr(exc, "status", 400),
            str(exc),
        ) from exc
    custom_tool_names = _validate_request(request)
    transport = spec.transport
    if transport == "chat_completions":
        body = _build_chat_body(
            request, model, effort, reasoning_store, custom_tool_names
        )
        headers = {"Authorization": f"Bearer {api_key}"}
        path = "/chat/completions"
    elif transport == "responses":
        body = _build_responses_body(request, model, effort, reasoning_store)
        headers = {"Authorization": f"Bearer {api_key}"}
        path = "/responses"
    elif transport == "anthropic_messages":
        body = _build_messages_body(
            request, model, effort, reasoning_store, custom_tool_names
        )
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        path = "/messages"
    else:
        raise GatewayError(
            "unsupported_transport", 400, f"unsupported transport: {transport!r}"
        )
    return UpstreamRequest(
        transport=transport,
        path=path,
        headers=headers,
        body=body,
        custom_tool_names=custom_tool_names,
    )


def _empty_usage():
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def _merge_usage(current, incoming):
    if current is None:
        return dict(incoming)
    merged = dict(current)
    merged.update({key: value for key, value in incoming.items() if key in merged})
    return merged


def _normalize_chat_usage(usage):
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def _normalize_anthropic_usage(usage):
    return {
        key: usage[key]
        for key in ("input_tokens", "output_tokens", "total_tokens")
        if key in usage
    }


def _extract_custom_input(arguments):
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError) as exc:
        raise GatewayError(
            "malformed_custom_input", 400, "custom tool arguments are not valid JSON"
        ) from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("input"), str):
        raise GatewayError(
            "malformed_custom_input", 400,
            "custom tool arguments must wrap a string input",
        )
    return parsed["input"]


def _translate_chat_stream(chunks, custom_tool_names, reasoning_store):
    make_id = _new_id_factory()
    rid = make_id("resp")
    started = False
    reasoning_parts = []
    reasoning_emitted = False
    text_parts = []
    msg_id = None
    msg_added = False
    tool_calls = {}
    first_indexes = []
    usage = None
    finish_seen = False

    def emit_reasoning():
        nonlocal reasoning_emitted
        if reasoning_emitted or not reasoning_parts:
            return
        payload = {"reasoning_content": "".join(reasoning_parts)}
        handle = _store_reasoning(
            reasoning_store, "chat_completions", payload
        )
        item_id = make_id("rs")
        item = {
            "type": "reasoning",
            "id": item_id,
            "summary": [],
            "encrypted_content": handle,
        }
        yield _sse(
            "response.output_item.added",
            {"type": "response.output_item.added", "item": copy.deepcopy(item)},
        )
        yield _sse(
            "response.output_item.done",
            {"type": "response.output_item.done", "item": copy.deepcopy(item)},
        )
        reasoning_emitted = True

    def emit_message_added():
        nonlocal msg_added, msg_id
        if msg_added:
            return
        msg_id = make_id("msg")
        yield _sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "message",
                    "id": msg_id,
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                },
            },
        )
        msg_added = True

    def emit_ordinary_pending(entry):
        if entry["name"] in custom_tool_names:
            return
        if not isinstance(entry["id"], str) or not isinstance(entry["name"], str):
            return
        if not entry["added"]:
            entry["item_id"] = make_id("fc")
            entry["added"] = True
            yield _sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "id": entry["item_id"],
                        "call_id": entry["id"],
                        "name": entry["name"],
                        "arguments": "",
                    },
                },
            )
        while entry["emitted_count"] < len(entry["arguments_parts"]):
            fragment = entry["arguments_parts"][entry["emitted_count"]]
            entry["emitted_count"] += 1
            yield _sse(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": entry["item_id"],
                    "call_id": entry["id"],
                    "delta": fragment,
                },
            )

    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise GatewayError("malformed_chunk", 400, "chat chunk must be an object")
        if not started:
            yield _sse(
                "response.created",
                {"type": "response.created", "response": {"id": rid, "status": "in_progress"}},
            )
            yield _sse(
                "response.in_progress",
                {"type": "response.in_progress", "response": {"id": rid, "status": "in_progress"}},
            )
            started = True
        usage_value = chunk.get("usage")
        usage_present = usage_value is not None
        if usage_present:
            if not isinstance(usage_value, dict):
                raise GatewayError("malformed_chunk", 400, "chat usage must be an object")
            usage = _merge_usage(usage, _normalize_chat_usage(usage_value))
        if "choices" not in chunk:
            if not usage_present:
                raise GatewayError("malformed_chunk", 400, "chat chunk requires a choices list")
            continue
        choices = chunk["choices"]
        if not isinstance(choices, list):
            raise GatewayError("malformed_chunk", 400, "chat chunk requires a choices list")
        for choice in choices:
            if not isinstance(choice, dict):
                raise GatewayError("malformed_chunk", 400, "chat choice must be an object")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise GatewayError("malformed_chunk", 400, "chat delta must be an object")
            reasoning = delta.get("reasoning_content")
            if reasoning is not None:
                if not isinstance(reasoning, str):
                    raise GatewayError(
                        "malformed_chunk", 400, "chat reasoning_content must be a string"
                    )
                reasoning_parts.append(reasoning)
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise GatewayError(
                        "malformed_chunk", 400, "chat content must be a string"
                    )
                yield from emit_reasoning()
                yield from emit_message_added()
                yield _sse(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": msg_id,
                        "content_index": 0,
                        "delta": content,
                    },
                )
                text_parts.append(content)
            tool_deltas = delta.get("tool_calls")
            if tool_deltas is not None:
                if not isinstance(tool_deltas, list):
                    raise GatewayError(
                        "malformed_chunk", 400, "tool_calls must be a list"
                    )
                yield from emit_reasoning()
                for tool_delta in tool_deltas:
                    if not isinstance(tool_delta, dict):
                        raise GatewayError(
                            "malformed_chunk", 400, "tool_call delta must be an object"
                        )
                    index = tool_delta.get("index")
                    if not isinstance(index, int):
                        raise GatewayError(
                            "malformed_chunk", 400,
                            "tool_call delta requires an integer index",
                        )
                    entry = tool_calls.get(index)
                    if entry is None:
                        entry = {
                            "id": None,
                            "name": None,
                            "arguments_parts": [],
                            "item_id": None,
                            "added": False,
                            "emitted_count": 0,
                        }
                        tool_calls[index] = entry
                        first_indexes.append(index)
                    if isinstance(tool_delta.get("id"), str):
                        entry["id"] = tool_delta["id"]
                    function = tool_delta.get("function")
                    if isinstance(function, dict):
                        if isinstance(function.get("name"), str):
                            entry["name"] = function["name"]
                        if isinstance(function.get("arguments"), str):
                            entry["arguments_parts"].append(function["arguments"])
                    yield from emit_ordinary_pending(entry)
            if choice.get("finish_reason") is not None:
                finish_seen = True
    if not finish_seen:
        raise GatewayError(
            "stream_truncated", 400, "chat stream ended before finish_reason"
        )
    yield from emit_reasoning()
    if msg_added:
        full_text = "".join(text_parts)
        yield _sse(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "item_id": msg_id,
                "content_index": 0,
                "text": full_text,
            },
        )
        yield _sse(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "id": msg_id,
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": full_text}],
                },
            },
        )
    for index in first_indexes:
        entry = tool_calls[index]
        if not isinstance(entry["id"], str) or not isinstance(entry["name"], str):
            raise GatewayError(
                "malformed_tool_call", 400, "tool call missing id or name"
            )
        arguments = "".join(entry["arguments_parts"])
        if entry["name"] in custom_tool_names:
            raw = _extract_custom_input(arguments)
            item_id = make_id("ctc")
            yield _sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "custom_tool_call",
                        "id": item_id,
                        "call_id": entry["id"],
                        "name": entry["name"],
                        "input": "",
                    },
                },
            )
            yield _sse(
                "response.custom_tool_call_input.delta",
                {
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": item_id,
                    "call_id": entry["id"],
                    "delta": raw,
                },
            )
            yield _sse(
                "response.custom_tool_call_input.done",
                {
                    "type": "response.custom_tool_call_input.done",
                    "item_id": item_id,
                    "call_id": entry["id"],
                    "input": raw,
                },
            )
            yield _sse(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "custom_tool_call",
                        "id": item_id,
                        "call_id": entry["id"],
                        "name": entry["name"],
                        "input": raw,
                    },
                },
            )
        else:
            yield from emit_ordinary_pending(entry)
            if not entry["added"] or not isinstance(entry["item_id"], str):
                raise GatewayError(
                    "malformed_tool_call", 400, "ordinary tool call missing item state"
                )
            yield _sse(
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": entry["item_id"],
                    "call_id": entry["id"],
                    "arguments": arguments,
                },
            )
            yield _sse(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "id": entry["item_id"],
                        "call_id": entry["id"],
                        "name": entry["name"],
                        "arguments": arguments,
                    },
                },
            )
    if usage is None:
        usage = _empty_usage()
    yield _sse(
        "response.completed",
        {
            "type": "response.completed",
            "response": {"id": rid, "status": "completed", "usage": usage},
        },
    )


def _translate_messages_stream(chunks, custom_tool_names, reasoning_store):
    make_id = _new_id_factory()
    rid = make_id("resp")
    started = False
    usage = None
    blocks = {}
    block_order = []
    msg_id = None
    msg_added = False
    stop_seen = False

    for chunk in chunks:
        if not isinstance(chunk, dict) or not isinstance(chunk.get("type"), str):
            raise GatewayError("malformed_chunk", 400, "anthropic chunk requires a type")
        if not started:
            yield _sse(
                "response.created",
                {"type": "response.created", "response": {"id": rid, "status": "in_progress"}},
            )
            yield _sse(
                "response.in_progress",
                {"type": "response.in_progress", "response": {"id": rid, "status": "in_progress"}},
            )
            started = True
        ctype = chunk["type"]
        if ctype == "ping":
            continue
        if ctype == "message_start":
            message = chunk.get("message")
            if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                usage = _merge_usage(
                    usage, _normalize_anthropic_usage(message["usage"])
                )
            continue
        if ctype == "content_block_start":
            index = chunk.get("index")
            if not isinstance(index, int):
                raise GatewayError(
                    "malformed_chunk", 400, "content_block_start requires index"
                )
            block = chunk.get("content_block")
            if not isinstance(block, dict):
                raise GatewayError(
                    "malformed_chunk", 400, "content_block must be an object"
                )
            btype = block.get("type")
            state = {
                "type": btype,
                "parts": [],
                "input_parts": [],
                "tool_use": None,
                "signature": None,
                "item_id": None,
                "added": False,
            }
            blocks[index] = state
            if index not in block_order:
                block_order.append(index)
            if btype == "thinking":
                thinking = block.get("thinking")
                if isinstance(thinking, str):
                    state["parts"].append(thinking)
                if isinstance(block.get("signature"), str):
                    state["signature"] = block["signature"]
            elif btype == "text":
                if not msg_added:
                    msg_id = make_id("msg")
                    yield _sse(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "item": {
                                "type": "message",
                                "id": msg_id,
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": ""}],
                            },
                        },
                    )
                    msg_added = True
                initial_text = block.get("text")
                if isinstance(initial_text, str) and initial_text:
                    state["parts"].append(initial_text)
                    yield _sse(
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "item_id": msg_id,
                            "content_index": 0,
                            "delta": initial_text,
                        },
                    )
            elif btype == "tool_use":
                tool_use = {
                    "id": block.get("id"),
                    "name": block.get("name"),
                }
                state["tool_use"] = tool_use
                if (
                    isinstance(tool_use.get("id"), str)
                    and isinstance(tool_use.get("name"), str)
                    and tool_use["name"] not in custom_tool_names
                ):
                    state["item_id"] = make_id("fc")
                    state["added"] = True
                    yield _sse(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "item": {
                                "type": "function_call",
                                "id": state["item_id"],
                                "call_id": tool_use["id"],
                                "name": tool_use["name"],
                                "arguments": "",
                            },
                        },
                    )
            else:
                raise GatewayError(
                    "unsupported_block", 400,
                    f"unsupported content block type: {btype!r}",
                )
            continue
        if ctype == "content_block_delta":
            index = chunk.get("index")
            state = blocks.get(index)
            if state is None:
                raise GatewayError("malformed_chunk", 400, "delta before block start")
            delta = chunk.get("delta")
            if not isinstance(delta, dict):
                raise GatewayError(
                    "malformed_chunk", 400, "content delta must be an object"
                )
            dtype = delta.get("type")
            if dtype == "thinking_delta":
                if isinstance(delta.get("thinking"), str):
                    state["parts"].append(delta["thinking"])
            elif dtype == "signature_delta":
                segment = delta.get("signature")
                if not isinstance(segment, str):
                    segment = ""
                state["signature"] = (state["signature"] or "") + segment
            elif dtype == "text_delta":
                if isinstance(delta.get("text"), str):
                    if not msg_added:
                        raise GatewayError(
                            "malformed_chunk", 400, "text delta before block start"
                        )
                    yield _sse(
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "item_id": msg_id,
                            "content_index": 0,
                            "delta": delta["text"],
                        },
                    )
                    state["parts"].append(delta["text"])
            elif dtype == "input_json_delta":
                if isinstance(delta.get("partial_json"), str):
                    state["input_parts"].append(delta["partial_json"])
                    tool_use = state.get("tool_use")
                    if (
                        state.get("added")
                        and isinstance(state.get("item_id"), str)
                        and isinstance(tool_use, dict)
                        and isinstance(tool_use.get("id"), str)
                        and isinstance(tool_use.get("name"), str)
                        and tool_use["name"] not in custom_tool_names
                    ):
                        yield _sse(
                            "response.function_call_arguments.delta",
                            {
                                "type": "response.function_call_arguments.delta",
                                "item_id": state["item_id"],
                                "call_id": tool_use["id"],
                                "delta": delta["partial_json"],
                            },
                        )
            else:
                raise GatewayError(
                    "unsupported_delta", 400,
                    f"unsupported content delta type: {dtype!r}",
                )
            continue
        if ctype == "content_block_stop":
            index = chunk.get("index")
            state = blocks.get(index)
            if state is None:
                raise GatewayError("malformed_chunk", 400, "block stop before block start")
            btype = state["type"]
            if btype == "thinking":
                thinking = "".join(state["parts"])
                signature = state.get("signature")
                block = {"type": "thinking", "thinking": thinking}
                if signature:
                    block["signature"] = signature
                payload = {"thinking": [block]}
                if signature:
                    payload["signature"] = signature
                handle = _store_reasoning(
                    reasoning_store, "anthropic_messages", payload
                )
                item_id = make_id("rs")
                item = {
                    "type": "reasoning",
                    "id": item_id,
                    "summary": [],
                    "encrypted_content": handle,
                }
                yield _sse(
                    "response.output_item.added",
                    {"type": "response.output_item.added", "item": copy.deepcopy(item)},
                )
                yield _sse(
                    "response.output_item.done",
                    {"type": "response.output_item.done", "item": copy.deepcopy(item)},
                )
            elif btype == "text":
                full_text = "".join(state["parts"])
                yield _sse(
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": msg_id,
                        "content_index": 0,
                        "text": full_text,
                    },
                )
                yield _sse(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "message",
                            "id": msg_id,
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": full_text}],
                        },
                    },
                )
            elif btype == "tool_use":
                tool_use = state.get("tool_use")
                if (
                    not isinstance(tool_use, dict)
                    or not isinstance(tool_use.get("id"), str)
                    or not isinstance(tool_use.get("name"), str)
                ):
                    raise GatewayError(
                        "malformed_tool_use", 400, "tool_use requires id and name"
                    )
                arguments = "".join(state["input_parts"])
                name = tool_use["name"]
                call_id = tool_use["id"]
                if name in custom_tool_names:
                    raw = _extract_custom_input(arguments)
                    item_id = make_id("ctc")
                    yield _sse(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "item": {
                                "type": "custom_tool_call",
                                "id": item_id,
                                "call_id": call_id,
                                "name": name,
                                "input": "",
                            },
                        },
                    )
                    yield _sse(
                        "response.custom_tool_call_input.delta",
                        {
                            "type": "response.custom_tool_call_input.delta",
                            "item_id": item_id,
                            "call_id": call_id,
                            "delta": raw,
                        },
                    )
                    yield _sse(
                        "response.custom_tool_call_input.done",
                        {
                            "type": "response.custom_tool_call_input.done",
                            "item_id": item_id,
                            "call_id": call_id,
                            "input": raw,
                        },
                    )
                    yield _sse(
                        "response.output_item.done",
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "type": "custom_tool_call",
                                "id": item_id,
                                "call_id": call_id,
                                "name": name,
                                "input": raw,
                            },
                        },
                    )
                else:
                    if not state.get("added") or not isinstance(state.get("item_id"), str):
                        raise GatewayError(
                            "malformed_tool_use", 400, "tool_use missing added state"
                        )
                    yield _sse(
                        "response.function_call_arguments.done",
                        {
                            "type": "response.function_call_arguments.done",
                            "item_id": state["item_id"],
                            "call_id": call_id,
                            "arguments": arguments,
                        },
                    )
                    yield _sse(
                        "response.output_item.done",
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "type": "function_call",
                                "id": state["item_id"],
                                "call_id": call_id,
                                "name": name,
                                "arguments": arguments,
                            },
                        },
                    )
            continue
        if ctype == "message_delta":
            delta_usage = chunk.get("usage")
            if isinstance(delta_usage, dict):
                usage = _merge_usage(
                    usage, _normalize_anthropic_usage(delta_usage)
                )
            continue
        if ctype == "message_stop":
            stop_seen = True
            continue
        raise GatewayError(
            "unsupported_event", 400, f"unsupported anthropic event: {ctype!r}"
        )
    if not stop_seen:
        raise GatewayError(
            "stream_truncated", 400, "anthropic stream ended before message_stop"
        )
    if usage is None:
        usage = {}
    usage.setdefault("input_tokens", 0)
    usage.setdefault("output_tokens", 0)
    usage.setdefault("total_tokens", usage["input_tokens"] + usage["output_tokens"])
    yield _sse(
        "response.completed",
        {
            "type": "response.completed",
            "response": {"id": rid, "status": "completed", "usage": usage},
        },
    )


def _translate_responses_stream(chunks, reasoning_store):
    handles = {}
    completed_seen = False
    for chunk in chunks:
        if not isinstance(chunk, dict) or not isinstance(chunk.get("type"), str):
            raise GatewayError(
                "malformed_chunk", 400, "responses chunk requires a type"
            )
        ctype = chunk["type"]
        if ctype.startswith("response.reasoning_text."):
            continue
        payload = copy.deepcopy(chunk)
        if ctype in ("response.output_item.added", "response.output_item.done"):
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "reasoning":
                item.pop("content", None)
                item.pop("reasoning_text", None)
                item_id = item.get("id")
                encrypted = item.get("encrypted_content")
                if isinstance(encrypted, str) and encrypted:
                    if item_id not in handles:
                        handle = _store_reasoning(
                            reasoning_store, "responses", {"encrypted_content": encrypted}
                        )
                        handles[item_id] = handle
                    item["encrypted_content"] = handles[item_id]
                elif item_id in handles:
                    item["encrypted_content"] = handles[item_id]
        if ctype == "response.completed":
            completed_seen = True
        if ctype == "response.failed":
            yield _sse(ctype, payload)
            return
        yield _sse(ctype, payload)
    if not completed_seen:
        raise GatewayError(
            "stream_truncated", 400,
            "responses stream ended before response.completed",
        )


def translate_upstream_stream(
    chunks, transport, custom_tool_names=(), reasoning_store=None
):
    if transport == "responses":
        yield from _translate_responses_stream(chunks, reasoning_store)
    elif transport == "chat_completions":
        yield from _translate_chat_stream(
            chunks, custom_tool_names, reasoning_store
        )
    elif transport == "anthropic_messages":
        yield from _translate_messages_stream(
            chunks, custom_tool_names, reasoning_store
        )
    else:
        raise GatewayError(
            "unsupported_transport", 400, f"unsupported transport: {transport!r}"
        )


def encode_sse(event):
    if not isinstance(event, SSEEvent):
        raise TypeError("encode_sse requires an SSEEvent")
    return f"event: {event.event}\ndata: {event.data}\n\n".encode("utf-8")
