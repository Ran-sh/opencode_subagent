"""RED behavior contract tests for the OpenCode Go protocol core (DP-20260811-03-V1).

Task DS-20260811-03 REV-2, phase RED-only.  The tests exercise the public
protocol-core API frozen in the approved decision package.  Production modules
are intentionally absent in this phase, so the RED target test
(ModelCatalogContractTests.test_exact_catalog_and_defaults) must fail with a
precise "opencode_models.py is missing" assertion instead of an ImportError or
syntax error.  The production modules are therefore loaded lazily at call time.

These tests only prove that the production contract is not implemented yet in
this phase; they cannot prove that any future implementation is correct.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib.util
import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SKILL_DIR = _REPO_ROOT / "codex-opencode-go-subagent"
_MODELS_PATH = _SKILL_DIR / "scripts" / "opencode_models.py"
_GATEWAY_PATH = _SKILL_DIR / "scripts" / "opencode_gateway.py"

TEST_API_KEY = "test-opencode-key"
PRIVATE_REASONING = "private-thought"
RESPONSES_OPAQUE = "opaque-enc"
PATCH_RAW = "*** Begin Patch\n*** Update File: app.py\n@@\n-hello\n+world\n*** End Patch\n"
REVISION = "2026-08-11"

_MODEL_SPEC_FIELDS = frozenset(
    {
        "id",
        "name",
        "transport",
        "efforts",
        "default_effort",
        "tool_call",
        "context_window",
        "max_output",
        "status",
        "source_revision",
    }
)

# model_id -> (name, transport, efforts, default_effort, context_window, max_output)
_EXPECTED_SPECS = {
    "deepseek-v4-flash": ("DeepSeek V4 Flash", "chat_completions", ("low", "high", "max"), "max", 1000000, 384000),
    "deepseek-v4-pro": ("DeepSeek V4 Pro", "chat_completions", ("high", "max"), "max", 1000000, 384000),
    "glm-5.1": ("GLM-5.1", "chat_completions", ("default",), "default", 202752, 32768),
    "glm-5.2": ("GLM-5.2", "chat_completions", ("high", "max"), "max", 1000000, 131072),
    "gpt-5.6-luna": ("GPT 5.6 Luna", "responses", ("none", "low", "medium", "high", "xhigh", "max"), "max", 1050000, 128000),
    "grok-4.5": ("Grok 4.5", "chat_completions", ("low", "medium", "high"), "high", 500000, 500000),
    "hy3": ("Hy3", "chat_completions", ("none", "low", "high"), "high", 256000, 64000),
    "kimi-k2.6": ("Kimi K2.6", "chat_completions", ("default",), "default", 262144, 65536),
    "kimi-k2.7-code": ("Kimi K2.7 Code", "chat_completions", ("default",), "default", 262144, 262144),
    "kimi-k3": ("Kimi K3", "chat_completions", ("max",), "max", 1048576, 131072),
    "mimo-v2.5": ("MiMo V2.5", "chat_completions", ("default",), "default", 1000000, 128000),
    "mimo-v2.5-pro": ("MiMo V2.5 Pro", "chat_completions", ("default",), "default", 1048576, 128000),
    "minimax-m2.7": ("MiniMax M2.7", "anthropic_messages", ("default",), "default", 204800, 131072),
    "minimax-m3": ("MiniMax M3", "anthropic_messages", ("none", "high"), "high", 1000000, 131072),
    "qwen3.6-plus": ("Qwen3.6 Plus", "anthropic_messages", ("none", "high", "max"), "max", 1000000, 65536),
    "qwen3.7-max": ("Qwen3.7 Max", "anthropic_messages", ("none", "high", "max"), "max", 1000000, 65536),
    "qwen3.7-plus": ("Qwen3.7 Plus", "anthropic_messages", ("none", "high", "max"), "max", 1000000, 65536),
    "qwen3.8-max": ("Qwen3.8 Max", "anthropic_messages", ("none", "high", "max"), "max", 1000000, 131072),
}


def _load_production_module(module_name: str, path: Path):
    """Lazily load a production module, failing with a precise assertion."""
    if not path.is_file():
        raise AssertionError(
            f"{path.name} is missing (expected at {path.relative_to(_REPO_ROOT)})"
        )
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot create import spec for {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _codex_request(**overrides) -> dict:
    request = {
        "model": "deepseek-v4-flash",
        "instructions": "You are a coding agent.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "List files."}],
            }
        ],
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"effort": "high"},
        "store": False,
        "stream": True,
        "include": [],
    }
    request.update(overrides)
    return request


def _function_tool(name: str = "read_file") -> dict:
    return {
        "type": "function",
        "name": name,
        "description": f"Tool {name}.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }


def _custom_tool(name: str = "apply_patch") -> dict:
    return {"type": "custom", "name": name, "format": {"type": "object"}}


def _events_json(events, event_name: str):
    return [json.dumps(event.data) for event in events if event.event == event_name]


class ModelCatalogContractTests(unittest.TestCase):
    """R1: exact 18-model catalog, defaults, and profile validation errors."""

    def test_exact_catalog_and_defaults(self):
        models = _load_production_module("opencode_models", _MODELS_PATH)
        catalog = models.MODELS
        self.assertIsInstance(catalog, dict)
        # exact catalog: 18 models and no 19th entry
        self.assertEqual(set(catalog), set(_EXPECTED_SPECS))
        self.assertEqual(models.DEFAULT_MODEL, "deepseek-v4-flash")
        self.assertEqual(models.DEFAULT_EFFORT, "max")
        for model_id, (
            name,
            transport,
            efforts,
            default_effort,
            context_window,
            max_output,
        ) in _EXPECTED_SPECS.items():
            spec = catalog[model_id]
            self.assertTrue(dataclasses.is_dataclass(spec))
            self.assertTrue(spec.__dataclass_params__.frozen)
            self.assertEqual({f.name for f in dataclasses.fields(spec)}, _MODEL_SPEC_FIELDS)
            self.assertEqual(spec.id, model_id)
            self.assertEqual(spec.name, name)
            self.assertEqual(spec.transport, transport)
            self.assertEqual(tuple(spec.efforts), efforts)
            self.assertEqual(spec.default_effort, default_effort)
            self.assertIs(spec.tool_call, True)
            self.assertEqual(spec.context_window, context_window)
            self.assertEqual(spec.max_output, max_output)
            self.assertEqual(spec.status, "active")
            self.assertEqual(spec.source_revision, REVISION)
            self.assertIn(default_effort, efforts)

    def test_profile_validation_errors(self):
        models = _load_production_module("opencode_models", _MODELS_PATH)
        with self.assertRaises(Exception) as cm:
            models.get_model("no-such-model")
        err = cm.exception
        self.assertEqual(getattr(err, "code", None), "model_not_found")
        self.assertIsInstance(getattr(err, "status", None), int)
        self.assertGreaterEqual(err.status, 400)
        self.assertNotIn(TEST_API_KEY, str(err))
        with self.assertRaises(Exception) as cm:
            models.validate_profile("grok-4.5", "max")
        err = cm.exception
        self.assertEqual(getattr(err, "code", None), "invalid_effort")
        self.assertIsInstance(getattr(err, "status", None), int)
        self.assertGreaterEqual(err.status, 400)
        # boundary: "default" is only valid for models that list it
        with self.assertRaises(Exception) as cm:
            models.get_model("grok-4.5", effort="default")
        err = cm.exception
        self.assertEqual(getattr(err, "code", None), "invalid_effort")
        spec = models.get_model("glm-5.1", effort="default")
        self.assertEqual(spec.id, "glm-5.1")


class RequestTranslationContractTests(unittest.TestCase):
    """R2/R3/R5: request translation across the three transports."""

    def test_chat_request(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        request = _codex_request()
        before = copy.deepcopy(request)
        upstream = gateway.prepare_upstream_request(
            request, model="deepseek-v4-flash", effort="high", api_key=TEST_API_KEY
        )
        self.assertEqual(upstream.transport, "chat_completions")
        self.assertEqual(upstream.path, "/chat/completions")
        headers = {k.lower(): v for k, v in upstream.headers.items()}
        self.assertEqual(headers.get("authorization"), "Bearer " + TEST_API_KEY)
        self.assertNotIn("x-api-key", headers)
        body = upstream.body
        self.assertEqual(body["model"], "deepseek-v4-flash")
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertNotIn("reasoning", body)
        self.assertNotIn(TEST_API_KEY, json.dumps(body))
        self.assertNotIn(TEST_API_KEY, repr(upstream))
        self.assertNotIn(TEST_API_KEY, str(upstream))
        # input object is not mutated in place
        self.assertEqual(request, before)
        # default effort is omitted entirely
        default_upstream = gateway.prepare_upstream_request(
            _codex_request(model="glm-5.1"),
            model="glm-5.1",
            effort="default",
            api_key=TEST_API_KEY,
        )
        self.assertNotIn("reasoning_effort", default_upstream.body)
        # structured failure must not leak the key
        with self.assertRaises(gateway.GatewayError) as cm:
            gateway.prepare_upstream_request(
                _codex_request(tools=[{"type": "web_search"}]),
                model="deepseek-v4-flash",
                effort="high",
                api_key=TEST_API_KEY,
            )
        self.assertNotIn(TEST_API_KEY, str(cm.exception))

    def test_responses_request(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        request = _codex_request(
            model="gpt-5.6-luna",
            instructions="Keep responses input.",
            reasoning={"effort": "xhigh"},
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "patch it"}],
                }
            ],
            tools=[_function_tool("read_file"), _custom_tool("apply_patch")],
            tool_choice="auto",
            parallel_tool_calls=True,
        )
        before = copy.deepcopy(request)
        upstream = gateway.prepare_upstream_request(
            request, model="gpt-5.6-luna", effort="xhigh", api_key=TEST_API_KEY
        )
        self.assertEqual(upstream.transport, "responses")
        self.assertEqual(upstream.path, "/responses")
        headers = {k.lower(): v for k, v in upstream.headers.items()}
        self.assertEqual(headers.get("authorization"), "Bearer " + TEST_API_KEY)
        self.assertNotIn("x-api-key", headers)
        body = upstream.body
        self.assertEqual(body["model"], "gpt-5.6-luna")
        self.assertEqual(body["reasoning"]["effort"], "xhigh")
        self.assertNotIn("reasoning_effort", body)
        # Responses transport keeps the full request shape, including custom tools
        self.assertEqual(body["input"], request["input"])
        self.assertEqual(body["instructions"], request["instructions"])
        self.assertEqual(body["tools"], request["tools"])
        self.assertEqual(body["tool_choice"], request["tool_choice"])
        self.assertEqual(body["parallel_tool_calls"], request["parallel_tool_calls"])
        self.assertEqual(request, before)
        self.assertNotIn(TEST_API_KEY, json.dumps(body))
        self.assertNotIn(TEST_API_KEY, repr(upstream))

    def test_messages_efforts(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        cases = [
            ("qwen3.7-max", "none", {"type": "disabled"}),
            ("qwen3.7-max", "high", {"type": "enabled", "budget_tokens": 16000}),
            ("qwen3.7-max", "max", {"type": "enabled", "budget_tokens": 31999}),
            ("minimax-m3", "none", {"type": "disabled"}),
            ("minimax-m3", "high", {"type": "enabled"}),
        ]
        for model_id, effort, thinking in cases:
            with self.subTest(model=model_id, effort=effort):
                upstream = gateway.prepare_upstream_request(
                    _codex_request(model=model_id, reasoning={"effort": effort}),
                    model=model_id,
                    effort=effort,
                    api_key=TEST_API_KEY,
                )
                self.assertEqual(upstream.transport, "anthropic_messages")
                self.assertEqual(upstream.path, "/messages")
                headers = {k.lower(): v for k, v in upstream.headers.items()}
                self.assertEqual(headers.get("x-api-key"), TEST_API_KEY)
                self.assertEqual(headers.get("anthropic-version"), "2023-06-01")
                self.assertNotIn("authorization", headers)
                body = upstream.body
                self.assertEqual(body.get("thinking"), thinking)
                self.assertEqual(body["max_tokens"], 32000)
                self.assertNotIn(TEST_API_KEY, json.dumps(body))
        # default effort omits thinking entirely but still sets max_tokens
        default_upstream = gateway.prepare_upstream_request(
            _codex_request(model="minimax-m2.7", reasoning={}),
            model="minimax-m2.7",
            effort="default",
            api_key=TEST_API_KEY,
        )
        self.assertNotIn("thinking", default_upstream.body)
        self.assertNotIn("reasoning_effort", default_upstream.body)
        self.assertEqual(default_upstream.body["max_tokens"], 32000)

    def test_history_and_tools(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        request = _codex_request(
            model="deepseek-v4-flash",
            instructions="Keep tool history.",
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": ""}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                },
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": '{"path":"a.txt"}',
                    "call_id": "call_1",
                },
                {
                    "type": "function_call",
                    "name": "mcp__fs__read",
                    "arguments": '{"path":"b.txt"}',
                    "call_id": "call_2",
                },
                {
                    "type": "function_call",
                    "name": "shell_command",
                    "arguments": '{"command":"dir"}',
                    "call_id": "call_3",
                },
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": '{"command":"echo ok"}',
                    "call_id": "call_4",
                },
                {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "input": PATCH_RAW,
                    "call_id": "call_c1",
                },
                {"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'},
                {"type": "function_call_output", "call_id": "call_2", "output": '{"ok": true}'},
                {"type": "function_call_output", "call_id": "call_3", "output": '{"exit": 0}'},
                {"type": "function_call_output", "call_id": "call_4", "output": '{"exit": 0}'},
                {"type": "custom_tool_call_output", "call_id": "call_c1", "output": '{"applied": true}'},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            ],
            tools=[
                _function_tool("read_file"),
                _function_tool("mcp__fs__read"),
                _function_tool("shell_command"),
                _function_tool("exec_command"),
                _custom_tool("apply_patch"),
            ],
            parallel_tool_calls=True,
        )
        # --- Chat (chat_completions) ---
        chat_upstream = gateway.prepare_upstream_request(
            request, model="deepseek-v4-flash", effort="high", api_key=TEST_API_KEY
        )
        self.assertEqual(chat_upstream.transport, "chat_completions")
        self.assertIn("apply_patch", chat_upstream.custom_tool_names)
        body = chat_upstream.body
        # custom tool is wrapped as a same-name function with only required string input
        custom_defs = [
            t["function"]
            for t in body["tools"]
            if t.get("type") == "function" and (t.get("function") or {}).get("name") == "apply_patch"
        ]
        self.assertEqual(len(custom_defs), 1)
        self.assertEqual(custom_defs[0]["name"], "apply_patch")
        self.assertIsInstance(custom_defs[0]["description"], str)
        self.assertGreater(len(custom_defs[0]["description"]), 0)
        self.assertEqual(
            custom_defs[0]["parameters"],
            {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            },
        )
        # every Chat tool uses the standard OpenAI function wrapper shape
        for tool in body["tools"]:
            self.assertEqual(tool["type"], "function")
            self.assertIn("name", tool["function"])
            self.assertIn("description", tool["function"])
            self.assertIn("parameters", tool["function"])
        # normal function schemas are preserved verbatim
        normal_defs = {
            t["function"]["name"]: t["function"]
            for t in body["tools"]
            if (t.get("function") or {}).get("name")
            in ("read_file", "mcp__fs__read", "shell_command", "exec_command")
        }
        self.assertEqual(normal_defs["read_file"]["parameters"], _function_tool("read_file")["parameters"])
        self.assertEqual(normal_defs["mcp__fs__read"]["parameters"], _function_tool("mcp__fs__read")["parameters"])
        self.assertEqual(normal_defs["shell_command"]["parameters"], _function_tool("shell_command")["parameters"])
        self.assertEqual(normal_defs["exec_command"]["parameters"], _function_tool("exec_command")["parameters"])
        # instructions become the first system message
        self.assertEqual(body["messages"][0], {"role": "system", "content": "Keep tool history."})
        roles = [m["role"] for m in body["messages"]]
        self.assertEqual(
            roles,
            ["system", "user", "assistant", "tool", "tool", "tool", "tool", "tool", "assistant"],
        )
        # empty user text is preserved
        self.assertEqual(body["messages"][1]["content"], "")
        # parallel tool calls preserved in order with original ids
        assistant = body["messages"][2]
        self.assertEqual(
            [tc["id"] for tc in assistant["tool_calls"]],
            ["call_1", "call_2", "call_3", "call_4", "call_c1"],
        )
        self.assertEqual(
            [tc["function"]["name"] for tc in assistant["tool_calls"]],
            ["read_file", "mcp__fs__read", "shell_command", "exec_command", "apply_patch"],
        )
        self.assertEqual(assistant["tool_calls"][0]["function"]["arguments"], '{"path":"a.txt"}')
        # custom raw patch is JSON-string wrapped and remains reversible
        self.assertEqual(
            json.loads(assistant["tool_calls"][4]["function"]["arguments"]),
            {"input": PATCH_RAW},
        )
        tool_msgs = body["messages"][3:8]
        self.assertEqual(
            [m["tool_call_id"] for m in tool_msgs],
            ["call_1", "call_2", "call_3", "call_4", "call_c1"],
        )
        self.assertEqual(
            [m["content"] for m in tool_msgs],
            ['{"ok": true}', '{"ok": true}', '{"exit": 0}', '{"exit": 0}', '{"applied": true}'],
        )
        self.assertEqual(body["messages"][8]["content"], "done")
        # --- Messages (anthropic_messages) ---
        msgs_upstream = gateway.prepare_upstream_request(
            _codex_request(
                model="qwen3.7-max",
                instructions="Keep tool history.",
                input=copy.deepcopy(request["input"]),
                tools=request["tools"],
                parallel_tool_calls=True,
                reasoning={"effort": "high"},
            ),
            model="qwen3.7-max",
            effort="high",
            api_key=TEST_API_KEY,
        )
        self.assertEqual(msgs_upstream.transport, "anthropic_messages")
        msgs_body = msgs_upstream.body
        # instructions live at top level; every Messages request carries max_tokens
        self.assertEqual(msgs_body["system"], "Keep tool history.")
        self.assertEqual(msgs_body["max_tokens"], 32000)
        # tool definitions use Anthropic input_schema and preserve names
        self.assertEqual(
            [t["name"] for t in msgs_body["tools"]],
            ["read_file", "mcp__fs__read", "shell_command", "exec_command", "apply_patch"],
        )
        for tool in msgs_body["tools"]:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertIn("input_schema", tool)
        apply_def = next(t for t in msgs_body["tools"] if t["name"] == "apply_patch")
        self.assertEqual(
            apply_def["input_schema"],
            {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            },
        )
        # empty user text is preserved
        self.assertEqual(msgs_body["messages"][0]["content"], [{"type": "text", "text": ""}])
        # assistant tool_use blocks preserve order, then user tool_result blocks
        assistant_blocks = msgs_body["messages"][1]["content"]
        tool_uses = [b for b in assistant_blocks if b.get("type") == "tool_use"]
        self.assertEqual(
            [b["name"] for b in tool_uses],
            ["read_file", "mcp__fs__read", "shell_command", "exec_command", "apply_patch"],
        )
        self.assertEqual(
            [b["id"] for b in tool_uses],
            ["call_1", "call_2", "call_3", "call_4", "call_c1"],
        )
        self.assertEqual(tool_uses[0]["input"], {"path": "a.txt"})
        self.assertEqual(tool_uses[4]["input"], {"input": PATCH_RAW})
        tool_result_msg = msgs_body["messages"][2]
        self.assertEqual(tool_result_msg["role"], "user")
        tool_results = [b for b in tool_result_msg["content"] if b.get("type") == "tool_result"]
        self.assertEqual(
            [b["tool_use_id"] for b in tool_results],
            ["call_1", "call_2", "call_3", "call_4", "call_c1"],
        )
        self.assertEqual(tool_results[0]["content"], '{"ok": true}')
        self.assertEqual(tool_results[4]["content"], '{"applied": true}')
        self.assertEqual(msgs_body["messages"][3]["content"], [{"type": "text", "text": "done"}])
        # structured image/audio tool output is rejected with 400
        for bad_output in (
            {
                "content_items": [
                    {"type": "output_image", "image_url": "data:image/png;base64,AAAA"}
                ]
            },
            {
                "content_items": [
                    {"type": "output_audio", "audio_url": "data:audio/wav;base64,AAAA"}
                ]
            },
        ):
            with self.assertRaises(gateway.GatewayError) as cm:
                gateway.prepare_upstream_request(
                    _codex_request(
                        input=[
                            {
                                "type": "function_call_output",
                                "call_id": "call_1",
                                "output": copy.deepcopy(bad_output),
                            }
                        ],
                        tools=[_function_tool("read_file")],
                    ),
                    model="deepseek-v4-flash",
                    effort="high",
                    api_key=TEST_API_KEY,
                )
            self.assertEqual(cm.exception.status, 400)
            self.assertNotIn(TEST_API_KEY, str(cm.exception))

    def test_reasoning_replay(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        clock = [1000.0]
        tokens = iter([f"tok{i}" for i in range(20)])
        store = gateway.ReasoningStore(clock=lambda: clock[0], token_factory=lambda: next(tokens))
        chat_state = {
            "transport": "chat_completions",
            "payload": {"reasoning_content": PRIVATE_REASONING},
        }
        msgs_state = {
            "transport": "anthropic_messages",
            "payload": {
                "thinking": [{"type": "thinking", "thinking": PRIVATE_REASONING}],
                "signature": "sig-123",
            },
        }
        resp_state = {
            "transport": "responses",
            "payload": {"encrypted_content": RESPONSES_OPAQUE},
        }
        chat_handle = store.save(chat_state)
        msgs_handle = store.save(msgs_state)
        resp_handle = store.save(resp_state)
        for handle in (chat_handle, msgs_handle, resp_handle):
            self.assertTrue(handle.startswith("ocg1:"))
            self.assertGreater(len(handle.split(":")[1]), 0)
        self.assertNotIn(PRIVATE_REASONING, chat_handle + msgs_handle)
        self.assertNotIn(RESPONSES_OPAQUE, resp_handle)
        self.assertNotIn(PRIVATE_REASONING, repr(store))
        self.assertNotIn(PRIVATE_REASONING, str(store))
        # save/get deep copy
        self.assertEqual(store.get(chat_handle), chat_state)
        restored = store.get(chat_handle)
        restored["payload"]["reasoning_content"] = "mutated"
        self.assertEqual(store.get(chat_handle), chat_state)
        follow_input = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "using tool now"}],
            },
            {"type": "reasoning", "encrypted_content": chat_handle, "summary": []},
        ]
        # chat follow-up replays reasoning_content, never the handle
        chat_upstream = gateway.prepare_upstream_request(
            _codex_request(
                model="deepseek-v4-flash",
                input=copy.deepcopy(follow_input),
                tools=[_function_tool()],
            ),
            model="deepseek-v4-flash",
            effort="high",
            api_key=TEST_API_KEY,
            reasoning_store=store,
        )
        chat_reasoning = [
            m.get("reasoning_content")
            for m in chat_upstream.body["messages"]
            if m.get("reasoning_content") is not None
        ]
        self.assertEqual(chat_reasoning, [PRIVATE_REASONING])
        self.assertNotIn(chat_handle, json.dumps(chat_upstream.body))
        self.assertNotIn(PRIVATE_REASONING, repr(chat_upstream))
        # anthropic follow-up replays thinking blocks with the signature
        msgs_upstream = gateway.prepare_upstream_request(
            _codex_request(
                model="minimax-m3",
                input=copy.deepcopy(
                    [
                        follow_input[0],
                        {"type": "reasoning", "encrypted_content": msgs_handle, "summary": []},
                    ]
                ),
                tools=[_function_tool()],
            ),
            model="minimax-m3",
            effort="high",
            api_key=TEST_API_KEY,
            reasoning_store=store,
        )
        blocks = [
            block
            for message in msgs_upstream.body["messages"]
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "thinking"
        ]
        self.assertTrue(blocks)
        self.assertIn(PRIVATE_REASONING, json.dumps(blocks[0]))
        self.assertIn("sig-123", json.dumps(blocks[0]))
        self.assertNotIn(msgs_handle, json.dumps(msgs_upstream.body))
        # responses follow-up restores the original opaque encrypted content
        resp_upstream = gateway.prepare_upstream_request(
            _codex_request(
                model="gpt-5.6-luna",
                input=copy.deepcopy(
                    [
                        follow_input[0],
                        {"type": "reasoning", "encrypted_content": resp_handle, "summary": []},
                    ]
                ),
            ),
            model="gpt-5.6-luna",
            effort="max",
            api_key=TEST_API_KEY,
            reasoning_store=store,
        )
        reasoning_items = [
            item
            for item in resp_upstream.body.get("input", [])
            if isinstance(item, dict) and item.get("type") == "reasoning"
        ]
        self.assertEqual(len(reasoning_items), 1)
        self.assertEqual(reasoning_items[0].get("encrypted_content"), RESPONSES_OPAQUE)
        self.assertNotIn(resp_handle, json.dumps(resp_upstream.body))
        self.assertNotIn(PRIVATE_REASONING, json.dumps(resp_upstream.body))
        # cross-transport replay is rejected with 409
        with self.assertRaises(gateway.GatewayError) as cm:
            gateway.prepare_upstream_request(
                _codex_request(
                    model="deepseek-v4-flash",
                    input=copy.deepcopy(
                        [
                            follow_input[0],
                            {"type": "reasoning", "encrypted_content": msgs_handle, "summary": []},
                        ]
                    ),
                    tools=[_function_tool()],
                ),
                model="deepseek-v4-flash",
                effort="high",
                api_key=TEST_API_KEY,
                reasoning_store=store,
            )
        self.assertEqual(cm.exception.code, "reasoning_state_transport_mismatch")
        self.assertEqual(cm.exception.status, 409)
        self.assertNotIn(PRIVATE_REASONING, str(cm.exception))
        self.assertNotIn(TEST_API_KEY, str(cm.exception))

    def test_chat_reasoning_first_tool_history(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        store = gateway.ReasoningStore()
        chat_handle = store.save(
            {
                "transport": "chat_completions",
                "payload": {"reasoning_content": PRIVATE_REASONING},
            }
        )
        request = _codex_request(
            model="deepseek-v4-flash",
            instructions="Keep reasoning-first tool history.",
            input=[
                {
                    "type": "reasoning",
                    "encrypted_content": chat_handle,
                    "summary": [],
                },
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": '{"path":"a.txt"}',
                    "call_id": "call_1",
                },
                {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "input": PATCH_RAW,
                    "call_id": "call_c1",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": '{"ok": true}',
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_c1",
                    "output": '{"applied": true}',
                },
            ],
            tools=[_function_tool("read_file"), _custom_tool("apply_patch")],
        )
        before = copy.deepcopy(request)
        upstream = gateway.prepare_upstream_request(
            request,
            model="deepseek-v4-flash",
            effort="high",
            api_key=TEST_API_KEY,
            reasoning_store=store,
        )
        self.assertEqual(request, before)
        self.assertNotIn(chat_handle, json.dumps(upstream.body))
        messages = [
            message
            for message in upstream.body["messages"]
            if message.get("role") != "system"
        ]
        assistant = messages[0]
        self.assertEqual(assistant.get("reasoning_content"), PRIVATE_REASONING)
        self.assertEqual(
            [call.get("id") for call in assistant.get("tool_calls", [])],
            ["call_1", "call_c1"],
        )
        self.assertEqual(
            [call["function"]["name"] for call in assistant.get("tool_calls", [])],
            ["read_file", "apply_patch"],
        )
        self.assertEqual(
            assistant["tool_calls"][0]["function"]["arguments"],
            '{"path":"a.txt"}',
        )
        self.assertEqual(
            json.loads(assistant["tool_calls"][1]["function"]["arguments"]),
            {"input": PATCH_RAW},
        )
        tool_messages = messages[1:3]
        self.assertEqual(
            [message["tool_call_id"] for message in tool_messages],
            ["call_1", "call_c1"],
        )
        self.assertEqual(
            [message["content"] for message in tool_messages],
            ['{"ok": true}', '{"applied": true}'],
        )

    def test_rejects_non_string_custom_history_input(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        request = _codex_request(
            input=[
                {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "input": {"patch": "bad"},
                    "call_id": "call_c1",
                }
            ],
            tools=[_custom_tool("apply_patch")],
        )
        with self.assertRaises(gateway.GatewayError) as cm:
            gateway.prepare_upstream_request(
                request,
                model="deepseek-v4-flash",
                effort="high",
                api_key=TEST_API_KEY,
            )
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(cm.exception.code, "invalid_request")
        self.assertNotIn(TEST_API_KEY, str(cm.exception))

    def test_messages_reasoning_first_tool_history(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        store = gateway.ReasoningStore()
        msgs_handle = store.save(
            {
                "transport": "anthropic_messages",
                "payload": {
                    "thinking": [
                        {"type": "thinking", "thinking": PRIVATE_REASONING}
                    ],
                    "signature": "sig",
                },
            }
        )
        tool_input = [
            {
                "type": "reasoning",
                "encrypted_content": msgs_handle,
                "summary": [],
            },
            {
                "type": "function_call",
                "name": "read_file",
                "arguments": '{"path":"a.txt"}',
                "call_id": "call_1",
            },
            {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": PATCH_RAW,
                "call_id": "call_c1",
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": '{"ok": true}',
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_c1",
                "output": '{"applied": true}',
            },
        ]
        for parallel in (True, False):
            with self.subTest(parallel_tool_calls=parallel):
                request = _codex_request(
                    model="qwen3.8-max",
                    instructions="Keep messages tool history.",
                    input=copy.deepcopy(tool_input),
                    tools=[
                        _function_tool("read_file"),
                        _custom_tool("apply_patch"),
                    ],
                    parallel_tool_calls=parallel,
                    reasoning={"effort": "max"},
                )
                before = copy.deepcopy(request)
                upstream = gateway.prepare_upstream_request(
                    request,
                    model="qwen3.8-max",
                    effort="max",
                    api_key=TEST_API_KEY,
                    reasoning_store=store,
                )
                self.assertEqual(request, before)
                self.assertNotIn(msgs_handle, json.dumps(upstream.body))
                messages = upstream.body["messages"]
                assistant = messages[0]
                self.assertEqual(
                    [block.get("type") for block in assistant["content"]],
                    ["thinking", "tool_use", "tool_use"],
                )
                thinking = assistant["content"][0]
                self.assertEqual(thinking.get("thinking"), PRIVATE_REASONING)
                self.assertEqual(thinking.get("signature"), "sig")
                tool_uses = assistant["content"][1:]
                self.assertEqual(
                    [tool_use.get("id") for tool_use in tool_uses],
                    ["call_1", "call_c1"],
                )
                self.assertEqual(
                    [tool_use.get("name") for tool_use in tool_uses],
                    ["read_file", "apply_patch"],
                )
                self.assertEqual(tool_uses[0].get("input"), {"path": "a.txt"})
                self.assertEqual(
                    tool_uses[1].get("input"),
                    {"input": PATCH_RAW},
                )
                result_message = messages[1]
                self.assertEqual(result_message["role"], "user")
                results = result_message["content"]
                self.assertEqual(
                    [block.get("type") for block in results],
                    ["tool_result", "tool_result"],
                )
                self.assertEqual(
                    [block.get("tool_use_id") for block in results],
                    ["call_1", "call_c1"],
                )
                self.assertEqual(
                    [block.get("content") for block in results],
                    ['{"ok": true}', '{"applied": true}'],
                )
                self.assertEqual(
                    upstream.body["tool_choice"],
                    {
                        "type": "auto",
                        "disable_parallel_tool_use": not parallel,
                    },
                )

    def test_responses_reasoning_options(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        request = _codex_request(
            model="gpt-5.6-luna",
            reasoning={"effort": "low", "summary": "auto"},
        )
        before = copy.deepcopy(request)
        upstream = gateway.prepare_upstream_request(
            request,
            model="gpt-5.6-luna",
            effort="high",
            api_key=TEST_API_KEY,
        )
        self.assertEqual(request, before)
        self.assertEqual(
            upstream.body["reasoning"],
            {"effort": "high", "summary": "auto"},
        )
        self.assertEqual(upstream.body["tools"], request["tools"])
        self.assertEqual(upstream.body["input"], request["input"])
        self.assertEqual(
            upstream.body["parallel_tool_calls"],
            request["parallel_tool_calls"],
        )
        with self.assertRaises(gateway.GatewayError) as cm:
            gateway.prepare_upstream_request(
                _codex_request(model="gpt-5.6-luna", reasoning="high"),
                model="gpt-5.6-luna",
                effort="high",
                api_key=TEST_API_KEY,
            )
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(cm.exception.code, "invalid_request")
        self.assertNotIn(TEST_API_KEY, str(cm.exception))

    def test_chat_interleaved_tool_rounds(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        request = _codex_request(
            model="deepseek-v4-flash",
            instructions="Keep interleaved tool rounds.",
            input=[
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": '{"path":"a.txt"}',
                    "call_id": "c1",
                },
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": "one",
                },
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": '{"path":"b.txt"}',
                    "call_id": "c2",
                },
                {
                    "type": "function_call_output",
                    "call_id": "c2",
                    "output": "two",
                },
            ],
            tools=[_function_tool("read_file")],
        )
        before = copy.deepcopy(request)
        upstream = gateway.prepare_upstream_request(
            request,
            model="deepseek-v4-flash",
            effort="high",
            api_key=TEST_API_KEY,
        )
        self.assertEqual(request, before)
        messages = [
            message
            for message in upstream.body["messages"]
            if message.get("role") != "system"
        ]
        self.assertEqual(
            [message["role"] for message in messages],
            ["assistant", "tool", "assistant", "tool"],
        )
        assistant_messages = [
            message for message in messages if message["role"] == "assistant"
        ]
        self.assertEqual(
            [len(message["tool_calls"]) for message in assistant_messages],
            [1, 1],
        )
        self.assertEqual(
            [message["tool_calls"][0]["id"] for message in assistant_messages],
            ["c1", "c2"],
        )
        self.assertEqual(
            [
                message["tool_calls"][0]["function"]["arguments"]
                for message in assistant_messages
            ],
            ['{"path":"a.txt"}', '{"path":"b.txt"}'],
        )
        tool_messages = [message for message in messages if message["role"] == "tool"]
        self.assertEqual(
            [message["tool_call_id"] for message in tool_messages],
            ["c1", "c2"],
        )
        self.assertEqual(
            [message["content"] for message in tool_messages],
            ["one", "two"],
        )

    def test_messages_interleaved_tool_rounds(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        request = _codex_request(
            model="qwen3.8-max",
            instructions="Keep interleaved tool rounds.",
            input=[
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": '{"path":"a.txt"}',
                    "call_id": "c1",
                },
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": "one",
                },
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": '{"path":"b.txt"}',
                    "call_id": "c2",
                },
                {
                    "type": "function_call_output",
                    "call_id": "c2",
                    "output": "two",
                },
            ],
            tools=[_function_tool("read_file")],
            parallel_tool_calls=True,
            reasoning={"effort": "max"},
        )
        before = copy.deepcopy(request)
        upstream = gateway.prepare_upstream_request(
            request,
            model="qwen3.8-max",
            effort="max",
            api_key=TEST_API_KEY,
        )
        self.assertEqual(request, before)
        messages = upstream.body["messages"]
        self.assertEqual(
            [message["role"] for message in messages],
            ["assistant", "user", "assistant", "user"],
        )
        assistant_messages = [
            message for message in messages if message["role"] == "assistant"
        ]
        self.assertEqual(
            [
                [
                    block["id"]
                    for block in message["content"]
                    if block.get("type") == "tool_use"
                ]
                for message in assistant_messages
            ],
            [["c1"], ["c2"]],
        )
        self.assertEqual(
            [
                [
                    block["input"]
                    for block in message["content"]
                    if block.get("type") == "tool_use"
                ]
                for message in assistant_messages
            ],
            [[{"path": "a.txt"}], [{"path": "b.txt"}]],
        )
        user_messages = [message for message in messages if message["role"] == "user"]
        self.assertEqual(
            [
                [
                    block["tool_use_id"]
                    for block in message["content"]
                    if block.get("type") == "tool_result"
                ]
                for message in user_messages
            ],
            [["c1"], ["c2"]],
        )
        self.assertEqual(
            [
                [
                    block["content"]
                    for block in message["content"]
                    if block.get("type") == "tool_result"
                ]
                for message in user_messages
            ],
            [["one"], ["two"]],
        )


    def test_developer_messages_fold_into_transport_instructions(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        cases = (
            ("deepseek-v4-flash", "chat_completions"),
            ("qwen3.8-max", "anthropic_messages"),
            ("gpt-5.6-luna", "responses"),
        )
        for model_id, transport in cases:
            with self.subTest(model=model_id, transport=transport):
                request = _codex_request(
                    model=model_id,
                    instructions="base",
                    input=[
                        {
                            "type": "message",
                            "role": "developer",
                            "content": "dev one",
                        },
                        {"type": "message", "role": "developer", "content": ""},
                        {
                            "type": "message",
                            "role": "developer",
                            "content": [{"type": "input_text", "text": "dev two"}],
                        },
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hello"}],
                        },
                    ],
                )
                before = copy.deepcopy(request)
                upstream = gateway.prepare_upstream_request(
                    request, model=model_id, effort="max", api_key=TEST_API_KEY
                )
                body = upstream.body
                expected = "base\n\ndev one\n\ndev two"
                if transport == "chat_completions":
                    self.assertEqual(
                        body["messages"][0],
                        {"role": "system", "content": expected},
                    )
                    roles = [item.get("role") for item in body["messages"][1:]]
                elif transport == "anthropic_messages":
                    self.assertEqual(body["system"], expected)
                    roles = [item.get("role") for item in body["messages"]]
                else:
                    self.assertEqual(body["instructions"], expected)
                    roles = [
                        item.get("role")
                        for item in body["input"]
                        if item.get("type") == "message"
                    ]
                self.assertNotIn("developer", roles)
                self.assertIn("user", roles)
                self.assertEqual(request, before)
        boundary = _codex_request(
            model="deepseek-v4-flash",
            instructions="",
            input=[
                {
                    "type": "message",
                    "role": "developer",
                    "content": "only developer",
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            ],
        )
        boundary_before = copy.deepcopy(boundary)
        boundary_upstream = gateway.prepare_upstream_request(
            boundary, model="deepseek-v4-flash", effort="max", api_key=TEST_API_KEY
        )
        self.assertEqual(
            boundary_upstream.body["messages"][0],
            {"role": "system", "content": "only developer"},
        )
        self.assertEqual(boundary, boundary_before)
        bad_inputs = (
            [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [
                        {"type": "input_image", "image_url": "https://invalid.example"}
                    ],
                }
            ],
            [{"type": "message", "role": "system", "content": "nope"}],
        )
        for bad_input in bad_inputs:
            with self.subTest(bad_input=bad_input):
                with self.assertRaises(gateway.GatewayError) as cm:
                    gateway.prepare_upstream_request(
                        _codex_request(model="deepseek-v4-flash", input=bad_input),
                        model="deepseek-v4-flash",
                        effort="max",
                        api_key=TEST_API_KEY,
                    )
                self.assertEqual(cm.exception.code, "unsupported_input")
                self.assertEqual(cm.exception.status, 400)


class StreamTranslationContractTests(unittest.TestCase):
    """R4: upstream stream conversion to Codex-consumable SSE events."""

    def _events(self, gateway, fixture, transport, custom_tool_names=(), reasoning_store=None):
        return list(
            gateway.translate_upstream_stream(
                iter(fixture),
                transport=transport,
                custom_tool_names=custom_tool_names,
                reasoning_store=reasoning_store,
            )
        )

    def _payloads(self, events, event_name):
        return [json.loads(event.data) for event in events if event.event == event_name]

    def _indices(self, events, event_name, predicate=None):
        return [
            i
            for i, event in enumerate(events)
            if event.event == event_name
            and (predicate is None or predicate(json.loads(event.data)))
        ]

    def _assert_envelopes_and_types(self, events):
        self.assertEqual(events[0].event, "response.created")
        self.assertEqual(events[1].event, "response.in_progress")
        self.assertEqual(events[-1].event, "response.completed")
        for event in events:
            self.assertEqual(json.loads(event.data).get("type"), event.event)

    def _assert_created_completed(self, events):
        self.assertEqual(events[0].event, "response.created")
        self.assertEqual(events[-1].event, "response.completed")
        completed = json.loads(events[-1].data)
        self.assertEqual(completed.get("type"), "response.completed")
        self.assertIsInstance(completed.get("response"), dict)
        self.assertIsInstance(completed["response"].get("id"), str)
        self.assertGreater(len(completed["response"]["id"]), 0)
        return completed

    def _assert_no_leak(self, events):
        for event in events:
            self.assertNotIn(PRIVATE_REASONING, json.dumps(event.data))
            self.assertNotIn(TEST_API_KEY, json.dumps(event.data))
            self.assertNotIn(PRIVATE_REASONING, repr(event))
            self.assertNotIn(TEST_API_KEY, repr(event))

    def test_chat_stream(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        clock = [1000.0]
        tokens = iter(f"tok{i}" for i in range(1000))
        store = gateway.ReasoningStore(clock=lambda: clock[0], token_factory=lambda: next(tokens))
        custom_args_json = json.dumps({"input": PATCH_RAW})
        custom_args_a = custom_args_json[: len(custom_args_json) // 2]
        custom_args_b = custom_args_json[len(custom_args_json) // 2:]
        fixture = [
            {"choices": [{"delta": {"role": "assistant", "reasoning_content": PRIVATE_REASONING}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " world"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "read_file", "arguments": '{"path":'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.txt"}'}}]}}
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call_2",
                                    "function": {
                                        "name": "apply_patch",
                                        "arguments": custom_args_a,
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 1, "function": {"arguments": custom_args_b}}
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}},
        ]
        events = self._events(
            gateway,
            fixture,
            transport="chat_completions",
            custom_tool_names=("apply_patch",),
            reasoning_store=store,
        )
        self._assert_envelopes_and_types(events)
        completed = self._assert_created_completed(events)
        self.assertEqual((completed.get("response") or {}).get("usage", {}).get("total_tokens"), 19)
        names = [e.event for e in events]
        # private reasoning is stored under an opaque local handle, never as delta text
        self.assertNotIn("response.reasoning_text.delta", names)
        reasoning_added = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") == "reasoning"
        ]
        self.assertEqual(len(reasoning_added), 1)
        handle = reasoning_added[0]["encrypted_content"]
        self.assertTrue(handle.startswith("ocg1:"))
        self.assertNotIn(PRIVATE_REASONING, handle)
        restored = store.get(handle)
        self.assertEqual(restored["transport"], "chat_completions")
        self.assertEqual(restored["payload"]["reasoning_content"], PRIVATE_REASONING)
        # text output
        self.assertIn("response.output_text.delta", names)
        text_deltas = self._payloads(events, "response.output_text.delta")
        self.assertTrue(any("Hello" in json.dumps(d) for d in text_deltas))
        self.assertTrue(any(" world" in json.dumps(d) for d in text_deltas))
        # ordinary function call uses function_call items and argument deltas
        fc_added = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") == "function_call"
        ]
        self.assertEqual(len(fc_added), 1)
        fc_item = fc_added[0]
        self.assertEqual(fc_item["name"], "read_file")
        self.assertEqual(fc_item["call_id"], "call_1")
        fc_done = [
            p["item"]
            for p in self._payloads(events, "response.output_item.done")
            if p["item"].get("type") == "function_call"
        ]
        self.assertEqual(fc_done[0]["arguments"], '{"path":"a.txt"}')
        self.assertEqual(fc_done[0]["id"], fc_item["id"])
        self.assertIn("response.function_call_arguments.delta", names)
        self.assertIn("response.function_call_arguments.done", names)
        # custom tool call uses custom_tool_call items and raw input is unwrapped
        ct_added = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") == "custom_tool_call"
        ]
        self.assertEqual(len(ct_added), 1)
        ct_item = ct_added[0]
        self.assertEqual(ct_item["name"], "apply_patch")
        self.assertEqual(ct_item["call_id"], "call_2")
        ct_done = [
            p["item"]
            for p in self._payloads(events, "response.output_item.done")
            if p["item"].get("type") == "custom_tool_call"
        ]
        self.assertEqual(ct_done[0]["input"], PATCH_RAW)
        self.assertEqual(ct_done[0]["id"], ct_item["id"])
        ct_deltas = self._payloads(events, "response.custom_tool_call_input.delta")
        self.assertEqual(
            "".join(d.get("delta", "") for d in ct_deltas),
            PATCH_RAW,
        )
        self.assertIn("response.custom_tool_call_input.done", names)
        # parallel calls keep upstream index order
        tool_items = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") in ("function_call", "custom_tool_call")
        ]
        self.assertEqual(
            [(item["type"], item["call_id"]) for item in tool_items],
            [("function_call", "call_1"), ("custom_tool_call", "call_2")],
        )
        # message/tool items are added before their deltas and done events
        fc_added_idx = self._indices(
            events, "response.output_item.added", lambda p: p["item"].get("type") == "function_call"
        )[0]
        fc_delta_idx = self._indices(events, "response.function_call_arguments.delta")[0]
        fc_done_idx = self._indices(
            events, "response.output_item.done", lambda p: p["item"].get("type") == "function_call"
        )[0]
        self.assertLess(fc_added_idx, fc_delta_idx)
        self.assertLess(fc_delta_idx, fc_done_idx)
        ct_added_idx = self._indices(
            events, "response.output_item.added", lambda p: p["item"].get("type") == "custom_tool_call"
        )[0]
        ct_delta_idx = self._indices(events, "response.custom_tool_call_input.delta")[0]
        ct_done_idx = self._indices(
            events, "response.output_item.done", lambda p: p["item"].get("type") == "custom_tool_call"
        )[0]
        self.assertLess(ct_added_idx, ct_delta_idx)
        self.assertLess(ct_delta_idx, ct_done_idx)
        msg_added_idx = self._indices(
            events, "response.output_item.added", lambda p: p["item"].get("type") == "message"
        )[0]
        self.assertLess(msg_added_idx, names.index("response.output_text.delta"))
        self._assert_no_leak(events)
        raw = gateway.encode_sse(events[0])
        self.assertIsInstance(raw, bytes)
        text = raw.decode("utf-8")
        lines = text.split("\n")
        self.assertTrue(text.startswith("event: response.created\n"))
        self.assertTrue(text.endswith("\n\n"))
        self.assertTrue(any(line.startswith("data: ") for line in lines))
        data_line = next(line for line in lines if line.startswith("data: "))
        parsed_event = json.loads(events[0].data)
        self.assertEqual(json.loads(data_line[len("data: "):]), parsed_event)
        self.assertEqual(parsed_event.get("type"), "response.created")
        self.assertEqual(lines[0], "event: response.created")

    def test_chat_function_argument_fragment_deltas(self):
        # R1: each ordinary function_call arguments fragment must become its
        # own function_call_arguments.delta instead of one aggregated delta.
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        fixture = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":',
                                    },
                                },
                                {
                                    "index": 1,
                                    "id": "c2",
                                    "function": {
                                        "name": "shell_command",
                                        "arguments": '{"command":',
                                    },
                                },
                                {
                                    "index": 2,
                                    "function": {"arguments": '{"late":'},
                                },
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 1, "function": {"arguments": '"dir"}'}},
                                {"index": 0, "function": {"arguments": '"a"}'}},
                                {
                                    "index": 2,
                                    "id": "c3",
                                    "function": {
                                        "name": "late_tool",
                                        "arguments": '"yes"}',
                                    },
                                },
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = self._events(gateway, fixture, transport="chat_completions")
        expected = {
            "c1": ('{"path":', '"a"}', '{"path":"a"}'),
            "c2": ('{"command":', '"dir"}', '{"command":"dir"}'),
            "c3": ('{"late":', '"yes"}', '{"late":"yes"}'),
        }
        added = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") == "function_call"
        ]
        self.assertEqual([item["call_id"] for item in added], ["c1", "c2", "c3"])
        for call_id, (frag_a, frag_b, full) in expected.items():
            item = next(item for item in added if item["call_id"] == call_id)
            delta_indices = self._indices(
                events,
                "response.function_call_arguments.delta",
                lambda p, cid=call_id: p.get("call_id") == cid,
            )
            self.assertEqual(len(delta_indices), 2)
            self.assertEqual(
                [json.loads(events[i].data)["delta"] for i in delta_indices],
                [frag_a, frag_b],
            )
            self.assertTrue(
                all(
                    json.loads(events[i].data)["item_id"] == item["id"]
                    for i in delta_indices
                )
            )
            added_idx = self._indices(
                events,
                "response.output_item.added",
                lambda p, cid=call_id: (
                    p["item"].get("type") == "function_call"
                    and p["item"].get("call_id") == cid
                ),
            )[0]
            self.assertLess(added_idx, min(delta_indices))
            done_idx = self._indices(
                events,
                "response.function_call_arguments.done",
                lambda p, cid=call_id: p.get("call_id") == cid,
            )[0]
            self.assertLess(max(delta_indices), done_idx)
            done = json.loads(events[done_idx].data)
            self.assertEqual(done["item_id"], item["id"])
            self.assertEqual(done["call_id"], call_id)
            self.assertEqual(done["arguments"], full)
            item_done_idx = self._indices(
                events,
                "response.output_item.done",
                lambda p, cid=call_id: (
                    p["item"].get("type") == "function_call"
                    and p["item"].get("call_id") == cid
                ),
            )[0]
            item_done = json.loads(events[item_done_idx].data)["item"]
            self.assertEqual(item_done["id"], item["id"])
            self.assertEqual(item_done["call_id"], call_id)
            self.assertEqual(item_done["arguments"], full)

    def test_chat_coexisting_delta_fields(self):
        # R1: reasoning, content, and finish_reason in the same delta are
        # consumed independently instead of the reasoning branch short-circuiting.
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        clock = [1000.0]
        tokens = iter(f"tok{i}" for i in range(1000))
        store = gateway.ReasoningStore(
            clock=lambda: clock[0], token_factory=lambda: next(tokens)
        )
        fixture = [
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": PRIVATE_REASONING,
                            "content": "hello",
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        ]
        try:
            events = self._events(
                gateway,
                fixture,
                transport="chat_completions",
                reasoning_store=store,
            )
        except gateway.GatewayError as err:
            self.fail(f"coexisting delta fields raised GatewayError code={err.code}")
        names = [event.event for event in events]
        self.assertEqual(
            names,
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.output_item.done",
                "response.output_item.added",
                "response.output_text.delta",
                "response.output_text.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        reasoning_added = [
            payload["item"]
            for payload in self._payloads(events, "response.output_item.added")
            if payload["item"].get("type") == "reasoning"
        ]
        self.assertEqual(len(reasoning_added), 1)
        handle = reasoning_added[0]["encrypted_content"]
        self.assertTrue(handle.startswith("ocg1:"))
        self.assertNotIn(PRIVATE_REASONING, handle)
        for event in events:
            self.assertNotIn(PRIVATE_REASONING, json.dumps(event.data))
        text_deltas = [
            payload.get("delta")
            for payload in self._payloads(events, "response.output_text.delta")
        ]
        self.assertEqual(text_deltas, ["hello"])

    def test_chat_usage_with_empty_choices(self):
        # R2: a final usage chunk that also carries choices=[] still contributes
        # its usage instead of being dropped because the choices key is present.
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        fixture = [
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        ]
        events = self._events(gateway, fixture, transport="chat_completions")
        completed = self._assert_created_completed(events)
        self.assertEqual(
            completed["response"].get("usage"),
            {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        )

    def test_chat_null_usage_is_ignored(self):
        # R1: an explicit usage=null is equivalent to a missing usage field.
        # It must not abort the stream or contribute tokens, and a choices-less
        # chunk with only usage=null must remain malformed.
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        store = gateway.ReasoningStore()
        fixture = [
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": PRIVATE_REASONING,
                            "content": None,
                            "role": "assistant",
                        },
                        "finish_reason": None,
                    }
                ],
                "usage": None,
            },
            {
                "choices": [
                    {
                        "delta": {"content": "hello", "reasoning_content": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": None,
            },
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        ]
        events = self._events(
            gateway, fixture, transport="chat_completions", reasoning_store=store
        )
        self._assert_no_leak(events)
        completed = self._assert_created_completed(events)
        self.assertEqual(
            completed["response"].get("usage"),
            {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        )
        text_deltas = self._payloads(events, "response.output_text.delta")
        self.assertEqual([payload["delta"] for payload in text_deltas], ["hello"])
        with self.assertRaises(gateway.GatewayError) as cm:
            self._events(gateway, [{"usage": None}], transport="chat_completions")
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(cm.exception.code, "malformed_chunk")

    def test_chat_non_object_usage_is_rejected(self):
        # R1: only non-null usage objects may contribute; any other non-null
        # usage value is still rejected as a malformed chunk.
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        for bad_usage in (False, 0, "", []):
            with self.subTest(usage=bad_usage):
                fixture = [
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": bad_usage,
                    }
                ]
                with self.assertRaises(gateway.GatewayError) as cm:
                    self._events(gateway, fixture, transport="chat_completions")
                self.assertEqual(cm.exception.status, 400)
                self.assertEqual(cm.exception.code, "malformed_chunk")

    def test_messages_stream(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        clock = [1000.0]
        tokens = iter(f"tok{i}" for i in range(1000))
        store = gateway.ReasoningStore(clock=lambda: clock[0], token_factory=lambda: next(tokens))
        custom_args_json = json.dumps({"input": PATCH_RAW})
        custom_args_a = custom_args_json[: len(custom_args_json) // 2]
        custom_args_b = custom_args_json[len(custom_args_json) // 2:]
        fixture = [
            {
                "type": "message_start",
                "message": {"id": "msg_1", "usage": {"input_tokens": 10, "output_tokens": 0}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "thinking",
                    "thinking": PRIVATE_REASONING,
                    "signature": "sig-123",
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": PRIVATE_REASONING},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "Hello"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": " world"},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '"a.txt"}'},
            },
            {"type": "content_block_stop", "index": 2},
            {
                "type": "content_block_start",
                "index": 3,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_2",
                    "name": "apply_patch",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 3,
                "delta": {"type": "input_json_delta", "partial_json": custom_args_a},
            },
            {
                "type": "content_block_delta",
                "index": 3,
                "delta": {"type": "input_json_delta", "partial_json": custom_args_b},
            },
            {"type": "content_block_stop", "index": 3},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 8, "input_tokens": 10},
            },
            {"type": "message_stop"},
        ]
        events = self._events(
            gateway,
            fixture,
            transport="anthropic_messages",
            custom_tool_names=("apply_patch",),
            reasoning_store=store,
        )
        self._assert_envelopes_and_types(events)
        completed = self._assert_created_completed(events)
        self.assertEqual((completed.get("response") or {}).get("usage", {}).get("output_tokens"), 8)
        names = [e.event for e in events]
        # private thinking is stored under an opaque local handle, never as delta text
        self.assertNotIn("response.reasoning_text.delta", names)
        reasoning_added = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") == "reasoning"
        ]
        self.assertEqual(len(reasoning_added), 1)
        handle = reasoning_added[0]["encrypted_content"]
        self.assertTrue(handle.startswith("ocg1:"))
        self.assertNotIn(PRIVATE_REASONING, handle)
        restored = store.get(handle)
        self.assertEqual(restored["transport"], "anthropic_messages")
        self.assertIn(PRIVATE_REASONING, json.dumps(restored["payload"]))
        self.assertIn("sig-123", json.dumps(restored["payload"]))
        # text output
        self.assertIn("response.output_text.delta", names)
        text_deltas = self._payloads(events, "response.output_text.delta")
        self.assertTrue(any("Hello" in json.dumps(d) for d in text_deltas))
        self.assertTrue(any(" world" in json.dumps(d) for d in text_deltas))
        # ordinary tool_use becomes a function_call item
        fc_added = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") == "function_call"
        ]
        self.assertEqual(len(fc_added), 1)
        fc_item = fc_added[0]
        self.assertEqual(fc_item["name"], "read_file")
        self.assertEqual(fc_item["call_id"], "toolu_1")
        fc_done = [
            p["item"]
            for p in self._payloads(events, "response.output_item.done")
            if p["item"].get("type") == "function_call"
        ]
        self.assertEqual(fc_done[0]["arguments"], '{"path":"a.txt"}')
        self.assertEqual(fc_done[0]["id"], fc_item["id"])
        self.assertIn("response.function_call_arguments.delta", names)
        self.assertIn("response.function_call_arguments.done", names)
        # custom tool_use becomes a custom_tool_call item and raw input is unwrapped
        ct_added = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") == "custom_tool_call"
        ]
        self.assertEqual(len(ct_added), 1)
        ct_item = ct_added[0]
        self.assertEqual(ct_item["name"], "apply_patch")
        self.assertEqual(ct_item["call_id"], "toolu_2")
        ct_done = [
            p["item"]
            for p in self._payloads(events, "response.output_item.done")
            if p["item"].get("type") == "custom_tool_call"
        ]
        self.assertEqual(ct_done[0]["input"], PATCH_RAW)
        self.assertEqual(ct_done[0]["id"], ct_item["id"])
        ct_deltas = self._payloads(events, "response.custom_tool_call_input.delta")
        self.assertEqual(
            "".join(d.get("delta", "") for d in ct_deltas),
            PATCH_RAW,
        )
        self.assertIn("response.custom_tool_call_input.done", names)
        # parallel calls keep upstream index order
        tool_items = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") in ("function_call", "custom_tool_call")
        ]
        self.assertEqual(
            [(item["type"], item["call_id"]) for item in tool_items],
            [("function_call", "toolu_1"), ("custom_tool_call", "toolu_2")],
        )
        # items are added before their deltas and done events
        fc_added_idx = self._indices(
            events, "response.output_item.added", lambda p: p["item"].get("type") == "function_call"
        )[0]
        fc_delta_idx = self._indices(events, "response.function_call_arguments.delta")[0]
        fc_done_idx = self._indices(
            events, "response.output_item.done", lambda p: p["item"].get("type") == "function_call"
        )[0]
        self.assertLess(fc_added_idx, fc_delta_idx)
        self.assertLess(fc_delta_idx, fc_done_idx)
        ct_added_idx = self._indices(
            events, "response.output_item.added", lambda p: p["item"].get("type") == "custom_tool_call"
        )[0]
        ct_delta_idx = self._indices(events, "response.custom_tool_call_input.delta")[0]
        ct_done_idx = self._indices(
            events, "response.output_item.done", lambda p: p["item"].get("type") == "custom_tool_call"
        )[0]
        self.assertLess(ct_added_idx, ct_delta_idx)
        self.assertLess(ct_delta_idx, ct_done_idx)
        msg_added_idx = self._indices(
            events, "response.output_item.added", lambda p: p["item"].get("type") == "message"
        )[0]
        self.assertLess(msg_added_idx, names.index("response.output_text.delta"))
        self._assert_no_leak(events)

    def test_messages_ping_initial_content_signature_and_partial_usage(self):
        # R1: legal ping is ignored, thinking/text block_start initial content
        # precedes its deltas, signature_delta appends in arrival order, and
        # message_delta usage only updates the fields the upstream provided.
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        clock = [1000.0]
        tokens = iter(f"tok{i}" for i in range(1000))
        store = gateway.ReasoningStore(clock=lambda: clock[0], token_factory=lambda: next(tokens))
        fixture = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            },
            {"type": "ping"},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "prefix", "signature": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "suffix"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig-a"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig-b"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": "Hi"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "!"},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 8},
            },
            {"type": "ping"},
            {"type": "message_stop"},
        ]
        try:
            events = self._events(
                gateway,
                fixture,
                transport="anthropic_messages",
                reasoning_store=store,
            )
        except gateway.GatewayError as err:
            self.fail(f"unexpected GatewayError code={err.code}")
        self.assertEqual(events[0].event, "response.created")
        self.assertEqual(events[1].event, "response.in_progress")
        completed = self._assert_created_completed(events)
        names = [event.event for event in events]
        self.assertNotIn("response.failed", names)
        self.assertFalse(any("unsupported" in name for name in names))
        for event in events:
            self.assertNotIn("ping", json.dumps(event.data))
        reasoning_added = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") == "reasoning"
        ]
        self.assertEqual(len(reasoning_added), 1)
        handle = reasoning_added[0]["encrypted_content"]
        self.assertTrue(handle.startswith("ocg1:"))
        self.assertNotIn("prefixsuffix", handle)
        self.assertNotIn("sig-a", handle)
        self.assertNotIn("sig-b", handle)
        restored = store.get(handle)
        self.assertEqual(restored["transport"], "anthropic_messages")
        self.assertEqual(restored["payload"]["thinking"][0]["thinking"], "prefixsuffix")
        self.assertEqual(restored["payload"]["signature"], "sig-asig-b")
        text_deltas = [
            json.loads(event.data).get("delta")
            for event in events
            if event.event == "response.output_text.delta"
        ]
        self.assertEqual(text_deltas, ["Hi", "!"])
        done_texts = [
            json.loads(event.data).get("text")
            for event in events
            if event.event == "response.output_text.done"
        ]
        self.assertEqual(done_texts, ["Hi!"])
        self.assertEqual(
            completed["response"].get("usage"),
            {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18},
        )
        for event in events:
            data = json.dumps(event.data)
            self.assertNotIn("prefixsuffix", data)
            self.assertNotIn("sig-a", data)
            self.assertNotIn("sig-b", data)
            self.assertNotIn("prefixsuffix", repr(event))
            self.assertNotIn("sig-a", repr(event))
            self.assertNotIn("sig-b", repr(event))

    def test_messages_function_argument_fragment_deltas(self):
        # R2: each ordinary tool_use input_json_delta fragment must become its
        # own function_call_arguments.delta instead of one aggregated delta.
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        fixture = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "c1",
                    "name": "read_file",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '"a"}'},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 0, "input_tokens": 0},
            },
            {"type": "message_stop"},
        ]
        events = self._events(gateway, fixture, transport="anthropic_messages")
        added = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") == "function_call"
        ]
        self.assertEqual(len(added), 1)
        item = added[0]
        self.assertEqual(item["call_id"], "c1")
        self.assertEqual(item["name"], "read_file")
        delta_indices = self._indices(
            events,
            "response.function_call_arguments.delta",
            lambda p: p.get("call_id") == "c1",
        )
        self.assertEqual(len(delta_indices), 2)
        self.assertEqual(
            [json.loads(events[i].data)["delta"] for i in delta_indices],
            ['{"path":', '"a"}'],
        )
        self.assertTrue(
            all(json.loads(events[i].data)["item_id"] == item["id"] for i in delta_indices)
        )
        added_idx = self._indices(
            events,
            "response.output_item.added",
            lambda p: p["item"].get("type") == "function_call",
        )[0]
        self.assertLess(added_idx, min(delta_indices))
        done_idx = self._indices(
            events,
            "response.function_call_arguments.done",
            lambda p: p.get("call_id") == "c1",
        )[0]
        self.assertLess(max(delta_indices), done_idx)
        done = json.loads(events[done_idx].data)
        self.assertEqual(done["item_id"], item["id"])
        self.assertEqual(done["call_id"], "c1")
        self.assertEqual(done["arguments"], '{"path":"a"}')
        item_done_idx = self._indices(
            events,
            "response.output_item.done",
            lambda p: p["item"].get("type") == "function_call",
        )[0]
        item_done = json.loads(events[item_done_idx].data)["item"]
        self.assertEqual(item_done["id"], item["id"])
        self.assertEqual(item_done["call_id"], "c1")
        self.assertEqual(item_done["arguments"], '{"path":"a"}')

    def test_responses_passthrough(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        clock = [1000.0]
        tokens = iter(f"tok{i}" for i in range(1000))
        store = gateway.ReasoningStore(clock=lambda: clock[0], token_factory=lambda: next(tokens))
        fixture = [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.in_progress", "response": {"id": "resp_1"}},
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [],
                    "encrypted_content": RESPONSES_OPAQUE,
                },
            },
            {
                "type": "response.reasoning_text.delta",
                "item_id": "rs_1",
                "content_index": 0,
                "delta": PRIVATE_REASONING,
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [],
                    "encrypted_content": RESPONSES_OPAQUE,
                },
            },
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                },
            },
            {"type": "response.output_text.delta", "item_id": "msg_1", "content_index": 0, "delta": "Hello"},
            {"type": "response.output_text.delta", "item_id": "msg_1", "content_index": 0, "delta": " world"},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello world"}],
                },
            },
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "custom_tool_call",
                    "id": "ct_1",
                    "call_id": "call_1",
                    "name": "apply_patch",
                    "input": "",
                },
            },
            {
                "type": "response.custom_tool_call_input.delta",
                "item_id": "ct_1",
                "call_id": "call_1",
                "delta": "*** Be",
            },
            {
                "type": "response.custom_tool_call_input.delta",
                "item_id": "ct_1",
                "call_id": "call_1",
                "delta": "gin Patch",
            },
            {
                "type": "response.custom_tool_call_input.done",
                "item_id": "ct_1",
                "call_id": "call_1",
                "input": PATCH_RAW,
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "custom_tool_call",
                    "id": "ct_1",
                    "call_id": "call_1",
                    "name": "apply_patch",
                    "input": PATCH_RAW,
                },
            },
            {
                "type": "response.completed",
                "response": {"id": "resp_1", "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}},
            },
        ]
        events = self._events(
            gateway,
            fixture,
            transport="responses",
            custom_tool_names=("apply_patch",),
            reasoning_store=store,
        )
        self._assert_envelopes_and_types(events)
        completed = self._assert_created_completed(events)
        self.assertEqual(completed["response"]["id"], "resp_1")
        self.assertEqual((completed.get("response") or {}).get("usage", {}).get("total_tokens"), 19)
        names = [e.event for e in events]
        # raw/private reasoning delta is suppressed; opaque payload is handled locally
        self.assertNotIn("response.reasoning_text.delta", names)
        reasoning_added = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") == "reasoning"
        ]
        self.assertEqual(len(reasoning_added), 1)
        self.assertEqual(reasoning_added[0]["id"], "rs_1")
        handle = reasoning_added[0]["encrypted_content"]
        self.assertTrue(handle.startswith("ocg1:"))
        self.assertNotIn(PRIVATE_REASONING, handle)
        self.assertNotIn(RESPONSES_OPAQUE, handle)
        restored = store.get(handle)
        self.assertEqual(restored["transport"], "responses")
        self.assertEqual(restored["payload"]["encrypted_content"], RESPONSES_OPAQUE)
        reasoning_done = [
            p["item"]
            for p in self._payloads(events, "response.output_item.done")
            if p["item"].get("type") == "reasoning"
        ]
        self.assertEqual(reasoning_done[0]["encrypted_content"], handle)
        # legal passthrough events survive
        self.assertIn("response.custom_tool_call_input.done", names)
        text_deltas = self._payloads(events, "response.output_text.delta")
        self.assertTrue(any("Hello" in json.dumps(d) for d in text_deltas))
        self.assertTrue(any(" world" in json.dumps(d) for d in text_deltas))
        msg_added_idx = self._indices(
            events, "response.output_item.added", lambda p: p["item"].get("type") == "message"
        )[0]
        self.assertLess(msg_added_idx, names.index("response.output_text.delta"))
        ct_added = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") == "custom_tool_call"
        ]
        self.assertEqual(len(ct_added), 1)
        self.assertEqual(ct_added[0]["name"], "apply_patch")
        self.assertEqual(ct_added[0]["call_id"], "call_1")
        ct_done = [
            p["item"]
            for p in self._payloads(events, "response.output_item.done")
            if p["item"].get("type") == "custom_tool_call"
        ]
        self.assertEqual(ct_done[0]["input"], PATCH_RAW)
        self.assertEqual(ct_done[0]["id"], "ct_1")
        ct_deltas = self._payloads(events, "response.custom_tool_call_input.delta")
        self.assertTrue(any("*** Be" in json.dumps(d) for d in ct_deltas))
        self.assertTrue(any("gin Patch" in json.dumps(d) for d in ct_deltas))
        ct_added_idx = self._indices(
            events, "response.output_item.added", lambda p: p["item"].get("type") == "custom_tool_call"
        )[0]
        ct_delta_idx = self._indices(events, "response.custom_tool_call_input.delta")[0]
        ct_done_idx = self._indices(
            events, "response.output_item.done", lambda p: p["item"].get("type") == "custom_tool_call"
        )[0]
        self.assertLess(ct_added_idx, ct_delta_idx)
        self.assertLess(ct_delta_idx, ct_done_idx)
        # upstream opaque encrypted content never reaches the SSE plaintext
        for event in events:
            self.assertNotIn(RESPONSES_OPAQUE, json.dumps(event.data))
        self._assert_no_leak(events)

    def test_responses_reasoning_plaintext_is_never_forwarded(self):
        # R1: no response.reasoning_text.* event may reach the SSE stream, and
        # reasoning items must drop raw content/reasoning_text while keeping
        # their summary and one reusable ocg1 handle.
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        clock = [1000.0]
        tokens = iter(f"tok{i}" for i in range(1000))
        store = gateway.ReasoningStore(clock=lambda: clock[0], token_factory=lambda: next(tokens))
        fixture = [
            {"type": "response.created", "response": {"id": "resp_priv"}},
            {"type": "response.in_progress", "response": {"id": "resp_priv"}},
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "reasoning",
                    "id": "rs_priv",
                    "summary": [{"type": "summary_text", "text": "safe summary"}],
                    "encrypted_content": RESPONSES_OPAQUE,
                    "content": [{"type": "reasoning_text", "text": "private-item-added"}],
                    "reasoning_text": "private-field-added",
                },
            },
            {
                "type": "response.reasoning_text.delta",
                "item_id": "rs_priv",
                "content_index": 0,
                "delta": "private-delta",
            },
            {
                "type": "response.reasoning_text.done",
                "item_id": "rs_priv",
                "content_index": 0,
                "text": "private-done",
            },
            {
                "type": "response.reasoning_summary_text.delta",
                "item_id": "rs_priv",
                "content_index": 0,
                "delta": "safe-summary-delta",
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "reasoning",
                    "id": "rs_priv",
                    "summary": [{"type": "summary_text", "text": "safe summary"}],
                    "encrypted_content": RESPONSES_OPAQUE,
                    "content": [{"type": "reasoning_text", "text": "private-item-done"}],
                    "reasoning_text": "private-field-done",
                },
            },
            {"type": "response.completed", "response": {"id": "resp_priv"}},
        ]
        events = self._events(
            gateway,
            fixture,
            transport="responses",
            reasoning_store=store,
        )
        names = [e.event for e in events]
        leaked = [name for name in names if name.startswith("response.reasoning_text.")]
        self.assertFalse(leaked, f"reasoning_text events leaked: {leaked}")
        # ADR-1: response.reasoning_summary_text.* stays passthrough.
        summary_deltas = self._payloads(events, "response.reasoning_summary_text.delta")
        self.assertEqual(
            [d.get("delta") for d in summary_deltas],
            ["safe-summary-delta"],
        )
        # ADR-2/INV-2: added and done reasoning items share one ocg1 handle.
        reasoning_added = [
            p["item"]
            for p in self._payloads(events, "response.output_item.added")
            if p["item"].get("type") == "reasoning"
        ]
        reasoning_done = [
            p["item"]
            for p in self._payloads(events, "response.output_item.done")
            if p["item"].get("type") == "reasoning"
        ]
        self.assertEqual(len(reasoning_added), 1)
        self.assertEqual(len(reasoning_done), 1)
        for item in reasoning_added + reasoning_done:
            self.assertNotIn("content", item)
            self.assertNotIn("reasoning_text", item)
            self.assertEqual(
                item["summary"],
                [{"type": "summary_text", "text": "safe summary"}],
            )
        handle = reasoning_added[0]["encrypted_content"]
        self.assertTrue(handle.startswith("ocg1:"))
        self.assertEqual(reasoning_done[0]["encrypted_content"], handle)
        restored = store.get(handle)
        self.assertEqual(restored["transport"], "responses")
        self.assertEqual(restored["payload"]["encrypted_content"], RESPONSES_OPAQUE)
        # INV-1: private plaintext and the upstream opaque value never leak.
        private_strings = (
            "private-item-added",
            "private-field-added",
            "private-delta",
            "private-done",
            "private-item-done",
            "private-field-done",
            RESPONSES_OPAQUE,
        )
        for event in events:
            data = json.dumps(event.data)
            for private in private_strings:
                self.assertNotIn(private, data)
                self.assertNotIn(private, repr(event))
        # INV-2: created/in_progress lead and completed closes the stream.
        self.assertEqual(events[0].event, "response.created")
        self.assertEqual(events[1].event, "response.in_progress")
        self.assertEqual(events[-1].event, "response.completed")

    def test_stream_error(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        fixtures = {
            "chat_malformed": ("chat_completions", [{"bad": "shape"}]),
            "chat_truncated": ("chat_completions", [{"choices": [{"delta": {"content": "Hi"}}]}]),
            "responses_failed": (
                "responses",
                [
                    {
                        "type": "response.failed",
                        "response": {
                            "id": "resp_1",
                            "error": {"code": "boom", "message": "upstream failed"},
                        },
                    }
                ],
            ),
            "messages_truncated": (
                "anthropic_messages",
                [{"type": "message_start", "message": {"id": "msg_1"}}],
            ),
        }
        for name, (transport, fixture) in fixtures.items():
            with self.subTest(name=name):
                try:
                    events = list(
                        gateway.translate_upstream_stream(
                            iter(fixture),
                            transport=transport,
                            custom_tool_names=(),
                            reasoning_store=None,
                        )
                    )
                except gateway.GatewayError as err:
                    self.assertNotIn(TEST_API_KEY, str(err))
                    self.assertNotIn(PRIVATE_REASONING, str(err))
                    self.assertIsInstance(err.status, int)
                    self.assertGreaterEqual(err.status, 400)
                else:
                    failed = [e for e in events if e.event == "response.failed"]
                    self.assertTrue(failed, f"no structured failure for {name}")
                    self.assertNotIn(TEST_API_KEY, json.dumps(failed[0].data))
                    self.assertNotIn(PRIVATE_REASONING, json.dumps(failed[0].data))


class ReasoningStoreContractTests(unittest.TestCase):
    """R5: in-memory reasoning state store with LRU, TTL, and opaque handles."""

    def test_lru_ttl_opaque(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        clock = [1000.0]
        tokens = iter(f"tok{i}" for i in range(10000))
        store = gateway.ReasoningStore(clock=lambda: clock[0], token_factory=lambda: next(tokens))
        state = {
            "transport": "chat_completions",
            "payload": {"reasoning_content": PRIVATE_REASONING, "signature": "sig-123"},
        }
        handles = [store.save(state) for _ in range(2049)]
        self.assertTrue(all(handle.startswith("ocg1:") for handle in handles))
        self.assertTrue(all(PRIVATE_REASONING not in handle for handle in handles))
        self.assertNotIn(PRIVATE_REASONING, repr(store))
        self.assertNotIn(PRIVATE_REASONING, str(store))
        self.assertEqual(store.get(handles[-1]), state)
        # LRU: the oldest entry is evicted after 2049 saves
        with self.assertRaises(gateway.GatewayError) as cm:
            store.get(handles[0])
        self.assertEqual(cm.exception.code, "reasoning_state_expired")
        self.assertEqual(cm.exception.status, 409)
        self.assertNotIn(PRIVATE_REASONING, str(cm.exception))
        # TTL boundary: exactly 7200 seconds later the newest entry expires
        clock[0] += 7200.0
        with self.assertRaises(gateway.GatewayError) as cm:
            store.get(handles[-1])
        self.assertEqual(cm.exception.code, "reasoning_state_expired")
        self.assertEqual(cm.exception.status, 409)
        # a fresh entry at the same clock time is still live
        fresh = store.save(state)
        self.assertEqual(store.get(fresh), state)

    def test_missing_state(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        store = gateway.ReasoningStore(clock=lambda: 1000.0, token_factory=lambda: "tok0")
        for handle in ("ocg1:never-saved", "ocg1:", "not-a-handle"):
            with self.subTest(handle=handle):
                with self.assertRaises(gateway.GatewayError) as cm:
                    store.get(handle)
                self.assertEqual(cm.exception.code, "reasoning_state_expired")
                self.assertEqual(cm.exception.status, 409)
                self.assertNotIn(PRIVATE_REASONING, str(cm.exception))
                self.assertNotIn(TEST_API_KEY, str(cm.exception))
        # stores are instance-local: no global registry and no disk persistence
        other = gateway.ReasoningStore(clock=lambda: 1000.0, token_factory=lambda: "tok9")
        saved = store.save(
            {
                "transport": "chat_completions",
                "payload": {"reasoning_content": PRIVATE_REASONING},
            }
        )
        with self.assertRaises(gateway.GatewayError):
            other.get(saved)


class UnsupportedContractTests(unittest.TestCase):
    """R6: visual/audio inputs and hosted/unknown tools are rejected, not dropped."""

    def test_rejects_visual_and_hosted_tools(self):
        gateway = _load_production_module("opencode_gateway", _GATEWAY_PATH)
        cases = {
            "input_image": _codex_request(
                input=[{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]
            ),
            "input_audio": _codex_request(
                input=[{"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}]
            ),
            "web_search": _codex_request(tools=[{"type": "web_search"}]),
            "image_generation": _codex_request(tools=[{"type": "image_generation"}]),
            "computer_use": _codex_request(tools=[{"type": "computer_use"}]),
            "local_shell": _codex_request(tools=[{"type": "local_shell"}]),
            "unknown_item": _codex_request(input=[{"type": "mystery_item"}]),
            "unknown_tool": _codex_request(tools=[{"type": "mystery_tool"}]),
        }
        for name, request in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(gateway.GatewayError) as cm:
                    gateway.prepare_upstream_request(
                        request,
                        model="deepseek-v4-flash",
                        effort="high",
                        api_key=TEST_API_KEY,
                    )
                self.assertEqual(cm.exception.status, 400)
                self.assertIsInstance(cm.exception.code, str)
                self.assertGreater(len(cm.exception.code), 0)
                self.assertNotIn(TEST_API_KEY, str(cm.exception))
                self.assertNotIn(PRIVATE_REASONING, str(cm.exception))
        # regression: text, shell, exec_command, MCP function and custom tools remain allowed
        allowed = _codex_request(
            model="deepseek-v4-flash",
            input=[{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            tools=[
                _function_tool("read_file"),
                _function_tool("mcp__fs__read"),
                _function_tool("shell_command"),
                _function_tool("exec_command"),
                _custom_tool("apply_patch"),
            ],
        )
        upstream = gateway.prepare_upstream_request(
            allowed, model="deepseek-v4-flash", effort="high", api_key=TEST_API_KEY
        )
        self.assertEqual(upstream.transport, "chat_completions")
        body_tools = json.dumps(upstream.body["tools"])
        self.assertIn("read_file", body_tools)
        self.assertIn("mcp__fs__read", body_tools)
        self.assertIn("shell_command", body_tools)
        self.assertIn("exec_command", body_tools)
        self.assertIn("apply_patch", body_tools)


if __name__ == "__main__":
    unittest.main()
