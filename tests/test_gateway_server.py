"""RED behavior contract tests for opencode_gateway_server.py (DS-20260811-13 REV-2).

The production server module is intentionally absent in this phase.  Every test
starts by calling ``self._server()``; when the module is missing the test fails
with ``server_missing`` (a unittest FAIL, not an ERROR).  The assertions after
that call are complete and become the GREEN verification contract.

Test-contract decisions made by this RED file (owner: main agent review):
  * ``create_server`` returns an object exposing ``server_address`` (a
    ``(host, port)`` pair), ``serve_forever``, ``shutdown`` and
    ``server_close``; binding to a rejected host/upstream raises ValueError.
  * ``GET /health`` returns exactly ``{"status": "ok"}`` with
    ``Connection: close``.
  * Error responses are JSON.  Upstream/connect failures use
    ``{"code": ..., "message": ...}`` and 429 responses additionally carry
    ``request_id``; the connect-failure code is ``upstream_unavailable``.
  * Request IDs are sanitized to ``^[A-Za-z0-9._-]{1,64}$``; a non-matching
    upstream ID is replaced with the stable value ``sanitized``.
  * Stream failures after a 200 are emitted as an SSE ``response.failed``
    event whose payload mirrors the core envelope
    ``{"type": "response.failed", "response": {"error": {"code": ...}}}``;
    stable codes are ``upstream_stream_truncated`` and
    ``upstream_idle_timeout``.
  * JSONL logs contain exactly ``model``, ``transport``, ``status``,
    ``duration_ms`` and ``request_id``; success logs keep the real model and
    core transport label.

All helpers below serve only the 11 tests in ``GatewayServerTests``.  No extra
test classes are defined.  Only loopback sockets are used; tokens, keys and
prompts are explicit placeholders.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import json
import os
import re
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SKILL_DIR = _REPO_ROOT / "codex-opencode-go-subagent"
_SERVER_PATH = _SKILL_DIR / "scripts" / "opencode_gateway_server.py"

# Explicit placeholders only; no real credentials are used or read.
LOCAL_TOKEN = "red-local-token-placeholder"
API_KEY = "red-api-key-placeholder"
PROMPT = "red-prompt-placeholder"
SECRET_MARKER = "red-upstream-secret-placeholder"
TOOL_ARGS_MARKER = "red-tool-args-placeholder"
TOOL_NAME = "read_file"

MAX_REQUEST_BYTES = 33554432
SAFE_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SANITIZED_REQUEST_ID = "sanitized"
CONNECT_ERROR_CODE = "upstream_unavailable"
FAILURE_CODE_TRUNCATED = "upstream_stream_truncated"
FAILURE_CODE_IDLE = "upstream_idle_timeout"
PRODUCTION_BASE = "https://opencode.ai/zen/go/v1"


def _load_server_module():
    """Lazily load the production server module; None when it is missing."""
    if not _SERVER_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        "opencode_gateway_server_red", _SERVER_PATH
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sse_bytes(pairs):
    """Build raw SSE bytes from (event_name_or_None, payload_dict) pairs."""
    parts = []
    for event_name, payload in pairs:
        data = json.dumps(payload, separators=(",", ":"))
        if event_name:
            parts.append(f"event: {event_name}\ndata: {data}\n\n")
        else:
            parts.append(f"data: {data}\n\n")
    return "".join(parts).encode("utf-8")


def _parse_sse(body):
    """Parse an SSE body into [(event_name_or_None, payload_dict), ...]."""
    events = []
    for block in body.decode("utf-8").split("\n\n"):
        if not block.strip():
            continue
        event_name = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if data_lines:
            events.append((event_name, json.loads("\n".join(data_lines))))
    return events


def _raw_request(port, method, path, headers, body=b"", timeout=5.0):
    """Send one raw HTTP/1.1 request to 127.0.0.1 and return a response triple.

    Returns (status, lowercased_headers, body_bytes).  The response is read
    until EOF (server uses Connection: close) or until Content-Length is met.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        lines = [f"{method} {path} HTTP/1.1"]
        for name, value in headers.items():
            lines.append(f"{name}: {value}")
        request_bytes = ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")
        sock.sendall(request_bytes + body)
        raw = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
            header_end = raw.find(b"\r\n\r\n")
            if header_end != -1:
                match = re.search(
                    rb"(?im)^Content-Length:\s*(\d+)", raw[:header_end]
                )
                if match and len(raw) >= header_end + 4 + int(match.group(1)):
                    break
    finally:
        sock.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    status_line, _, header_block = head.decode("iso-8859-1").partition("\r\n")
    status = int(status_line.split(" ", 2)[1])
    response_headers = {}
    for line in header_block.split("\r\n"):
        if line and ":" in line:
            name, value = line.split(":", 1)
            response_headers[name.strip().lower()] = value.strip()
    return status, response_headers, payload


def _connect_fails(port):
    """True when a fresh loopback connect to the port is refused/reset."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return False
    except OSError:
        return True


class _FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw_length = self.headers.get("Content-Length")
        length = int(raw_length) if raw_length else 0
        body = self.rfile.read(length) if length else b""
        record = {
            "method": "POST",
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        }
        server = self.server
        with server.records_lock:
            server.records.append(record)
        script = server.scripts.get(self.path)
        if callable(script):
            script(self, record)
        elif script is not None:
            status, headers, payload = script
            self._reply(status, headers, payload)
        else:
            self._reply(404, {}, b'{"error":"not found"}')

    def _reply(self, status, headers, payload):
        try:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            if payload:
                self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class _FakeUpstreamServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler):
        super().__init__(addr, handler)
        self.records = []
        self.records_lock = threading.Lock()
        self.scripts = {}
        self.block_event = None
        self.release_event = None


@contextlib.contextmanager
def _fake_upstream(scripts, block_event=None, release_event=None):
    """Run a loopback fake upstream that records POSTs and scripts responses."""
    server = _FakeUpstreamServer(("127.0.0.1", 0), _FakeUpstreamHandler)
    server.scripts = scripts
    server.block_event = block_event
    server.release_event = release_event
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@contextlib.contextmanager
def _local_server(
    module,
    upstream_base,
    log_path=None,
    max_concurrent=8,
    connect_timeout=30.0,
    stream_idle_timeout=650.0,
):
    """Create and run the local gateway on loopback port 0; yields (server, port, thread)."""
    server = module.create_server(
        "127.0.0.1",
        0,
        LOCAL_TOKEN,
        API_KEY,
        upstream_base=upstream_base,
        log_path=log_path,
        max_concurrent=max_concurrent,
        max_request_bytes=MAX_REQUEST_BYTES,
        connect_timeout=connect_timeout,
        stream_idle_timeout=stream_idle_timeout,
        allow_test_http=True,
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, port, thread
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        server.server_close()
        thread.join(timeout=2.0)


def _chat_fixture():
    return [
        (None, {"choices": [{"delta": {"role": "assistant", "content": "Hello"}}]}),
        (None, {"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        (None, {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}),
    ]


def _responses_fixture():
    return [
        ("response.created", {"type": "response.created", "response": {"id": "resp_1"}}),
        ("response.in_progress", {"type": "response.in_progress", "response": {"id": "resp_1"}}),
        (
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                },
            },
        ),
        (
            "response.output_text.delta",
            {"type": "response.output_text.delta", "item_id": "msg_1", "content_index": 0, "delta": "Hello"},
        ),
        (
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello"}],
                },
            },
        ),
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {"id": "resp_1", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}},
            },
        ),
    ]


def _messages_fixture():
    return [
        (None, {"type": "message_start", "message": {"id": "msg_1"}}),
        (None, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        (None, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}),
        (None, {"type": "content_block_stop", "index": 0}),
        (None, {"type": "message_stop"}),
    ]


def _local_request(model, effort, tools=None):
    """A minimal text-only Codex Responses request accepted by the frozen core."""
    if tools is None:
        tools = [
            {
                "type": "function",
                "name": TOOL_NAME,
                "description": f"Read {TOOL_ARGS_MARKER}.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ]
    return {
        "model": model,
        "instructions": "RED test instructions.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": PROMPT}],
            }
        ],
        "tools": tools,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"effort": effort},
        "store": False,
        "stream": True,
        "include": [],
    }


def _inference_headers(payload):
    return {
        "Authorization": "Bearer " + LOCAL_TOKEN,
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
    }


def _namespace_chat_sse(proxy):
    return [
        (
            None,
            {
                "choices": [
                    {
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_ns",
                                    "function": {"name": proxy, "arguments": '{"code":'},
                                }
                            ],
                        }
                    }
                ]
            },
        ),
        (
            None,
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"1+1"}'}}
                            ]
                        }
                    }
                ]
            },
        ),
        (None, {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        (
            None,
            {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        ),
    ]


class GatewayServerTests(unittest.TestCase):
    """Exactly 11 RED behavior tests for the missing local HTTP/SSE gateway."""

    def _server(self):
        module = _load_server_module()
        if module is None:
            self.fail("server_missing")
        return module

    def test_rejects_non_loopback_bind_and_redacts_runtime_repr(self):
        # R1: bind must stay on 127.0.0.1 and secrets must stay out of repr.
        module = self._server()
        for host in ("0.0.0.0", "::", "localhost", "127.0.0.2"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    module.create_server(
                        host, 0, LOCAL_TOKEN, API_KEY, allow_test_http=True
                    )
        # Production (allow_test_http=False) must never accept an http upstream.
        with self.assertRaises(ValueError):
            module.create_server(
                "127.0.0.1",
                0,
                LOCAL_TOKEN,
                API_KEY,
                upstream_base="http://127.0.0.1:9",
                allow_test_http=False,
            )
        # Production upstream must be the exact fixed base URL.
        for base in (
            "https://opencode.ai/not-the-go-base",
            "https://opencode.ai/zen/go/v1/extra",
            "https://opencode.ai:443/zen/go/v1",
        ):
            with self.subTest(upstream_base=base):
                with self.assertRaises(ValueError):
                    module.create_server(
                        "127.0.0.1",
                        0,
                        LOCAL_TOKEN,
                        API_KEY,
                        upstream_base=base,
                        allow_test_http=False,
                    )
        server = module.create_server(
            "127.0.0.1",
            0,
            LOCAL_TOKEN,
            API_KEY,
            upstream_base="http://127.0.0.1:9",
            allow_test_http=True,
        )
        try:
            self.assertGreater(server.server_address[1], 0)
            self.assertNotIn(LOCAL_TOKEN, repr(server))
            self.assertNotIn(API_KEY, repr(server))
        finally:
            server.shutdown()
            server.server_close()
        # ADR-3/ADR-4 fixed defaults must be exact.
        params = inspect.signature(module.create_server).parameters
        self.assertEqual(params["upstream_base"].default, PRODUCTION_BASE)
        self.assertIsNone(params["log_path"].default)
        self.assertEqual(params["max_concurrent"].default, 8)
        self.assertEqual(params["max_request_bytes"].default, 33554432)
        self.assertEqual(params["connect_timeout"].default, 30.0)
        self.assertEqual(params["stream_idle_timeout"].default, 650.0)
        self.assertFalse(params["allow_test_http"].default)

    def test_health_is_public_method_and_route_safe(self):
        # R1: GET /health is public; other methods/routes are rejected.
        module = self._server()
        with _fake_upstream({}) as (fake, upstream_port):
            with _local_server(module, f"http://127.0.0.1:{upstream_port}") as (
                srv,
                port,
                _thread,
            ):
                status, headers, body = _raw_request(port, "GET", "/health", {})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"status": "ok"})
                self.assertEqual(headers.get("connection", "").lower(), "close")
                status2, _, _ = _raw_request(
                    port,
                    "GET",
                    "/health",
                    {"Authorization": "Bearer " + LOCAL_TOKEN},
                )
                self.assertEqual(status2, 200)
                status3, _, _ = _raw_request(port, "GET", "/v1/responses", {})
                self.assertEqual(status3, 404)
                for method in ("PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "TRACE"):
                    with self.subTest(method=method):
                        method_status, method_headers, _ = _raw_request(
                            port, method, "/health", {}
                        )
                        self.assertEqual(method_status, 405)
                        self.assertTrue(
                            method_headers.get("content-type", "").startswith(
                                "application/json"
                            )
                        )
                        self.assertEqual(
                            method_headers.get("connection", "").lower(), "close"
                        )
                self.assertEqual(len(fake.records), 0)

    def test_inference_and_shutdown_require_exact_bearer(self):
        # R1: exact local bearer token; absent/wrong/altered tokens are 401.
        module = self._server()
        with _fake_upstream({}) as (fake, upstream_port):
            with _local_server(module, f"http://127.0.0.1:{upstream_port}") as (
                srv,
                port,
                _thread,
            ):
                payload = json.dumps(_local_request("deepseek-v4-flash", "max")).encode("utf-8")
                cases = [
                    ("absent", {}),
                    ("wrong", {"Authorization": "Bearer wrong-local-token"}),
                    ("case_altered", {"Authorization": "Bearer " + LOCAL_TOKEN.upper()}),
                    ("whitespace_altered", {"Authorization": "Bearer " + LOCAL_TOKEN + " "}),
                ]
                for name, extra in cases:
                    with self.subTest(case=name):
                        headers = {
                            "Content-Type": "application/json",
                            "Content-Length": str(len(payload)),
                        }
                        headers.update(extra)
                        status, _, _ = _raw_request(
                            port, "POST", "/v1/responses", headers, body=payload
                        )
                        self.assertEqual(status, 401)
                self.assertEqual(len(fake.records), 0)
                # Unauthenticated shutdown must not stop the server.
                status_auth_off, _, _ = _raw_request(
                    port, "POST", "/shutdown", {"Content-Length": "0"}
                )
                self.assertEqual(status_auth_off, 401)
                status_wrong, _, _ = _raw_request(
                    port,
                    "POST",
                    "/shutdown",
                    {"Authorization": "Bearer wrong-local-token", "Content-Length": "0"},
                )
                self.assertEqual(status_wrong, 401)
                health_status, _, _ = _raw_request(port, "GET", "/health", {})
                self.assertEqual(health_status, 200)

    def test_request_body_validation_happens_before_upstream(self):
        # R2: invalid framing/length/media/JSON is rejected before upstream.
        module = self._server()
        with _fake_upstream({}) as (fake, upstream_port):
            with _local_server(module, f"http://127.0.0.1:{upstream_port}") as (
                srv,
                port,
                _thread,
            ):
                base = {"Authorization": "Bearer " + LOCAL_TOKEN}
                valid_body = b'{"model":"deepseek-v4-flash","input":[],"tools":[]}'
                cases = [
                    ("transfer_encoding", {"Transfer-Encoding": "chunked"}, b"0\r\n\r\n", 400),
                    ("missing_length", {}, b'{"model":"deepseek-v4-flash"}', 411),
                    ("negative_length", {"Content-Length": "-1"}, b"{}", 411),
                    ("noninteger_length", {"Content-Length": "abc"}, b"{}", 400),
                    ("oversized", {"Content-Length": str(MAX_REQUEST_BYTES + 1)}, b"{}", 413),
                    (
                        "wrong_media",
                        {"Content-Type": "text/plain", "Content-Length": str(len(valid_body))},
                        valid_body,
                        415,
                    ),
                    (
                        "invalid_json",
                        {"Content-Type": "application/json", "Content-Length": "8"},
                        b"{not-json",
                        400,
                    ),
                ]
                for name, extra, body, expected in cases:
                    with self.subTest(case=name):
                        headers = dict(base)
                        headers.update(extra)
                        status, _, _ = _raw_request(
                            port, "POST", "/v1/responses", headers, body=body
                        )
                        self.assertEqual(status, expected)
                self.assertEqual(len(fake.records), 0)

    def test_three_transports_stream_end_to_end(self):
        # R2/R3: chat/responses/messages SSE end-to-end through the server.
        module = self._server()
        cases = [
            ("chat", "deepseek-v4-flash", "max", "/chat/completions", _chat_fixture()),
            ("responses", "gpt-5.6-luna", "high", "/responses", _responses_fixture()),
            ("messages", "qwen3.8-max", "max", "/messages", _messages_fixture()),
        ]
        for name, model, effort, path, fixture in cases:
            with self.subTest(transport=name):
                script = {
                    path: (200, {"Content-Type": "text/event-stream"}, _sse_bytes(fixture))
                }
                with _fake_upstream(script) as (fake, upstream_port):
                    with _local_server(
                        module, f"http://127.0.0.1:{upstream_port}"
                    ) as (srv, port, _thread):
                        payload = json.dumps(_local_request(model, effort)).encode("utf-8")
                        status, headers, body = _raw_request(
                            port,
                            "POST",
                            "/v1/responses",
                            _inference_headers(payload),
                            body=payload,
                            timeout=10.0,
                        )
                        self.assertEqual(status, 200)
                        self.assertTrue(
                            headers.get("content-type", "").startswith("text/event-stream")
                        )
                        events = _parse_sse(body)
                        event_names = [e for e, _ in events]
                        self.assertIn("response.created", event_names)
                        self.assertIn("response.completed", event_names)
                        self.assertIn("Hello", json.dumps(events))
                        self.assertEqual(len(fake.records), 1)
                        record = fake.records[0]
                        self.assertEqual(record["method"], "POST")
                        self.assertEqual(record["path"], path)
                        headers_lc = record["headers"]
                        upstream_text = record["body"].decode("utf-8")
                        if name == "messages":
                            self.assertEqual(headers_lc.get("x-api-key"), API_KEY)
                            self.assertEqual(headers_lc.get("anthropic-version"), "2023-06-01")
                            self.assertNotIn("authorization", headers_lc)
                        else:
                            self.assertEqual(headers_lc.get("authorization"), "Bearer " + API_KEY)
                            self.assertNotIn("x-api-key", headers_lc)
                        upstream_body = json.loads(upstream_text)
                        self.assertEqual(upstream_body["model"], model)
                        if name == "chat":
                            self.assertEqual(upstream_body["reasoning_effort"], "max")
                        elif name == "responses":
                            self.assertEqual(upstream_body["reasoning"]["effort"], "high")
                        else:
                            self.assertIn("thinking", upstream_body)
                        # Tool name survives the frozen conversion; no local token leaks.
                        self.assertIn(TOOL_NAME, upstream_text)
                        self.assertNotIn(LOCAL_TOKEN, upstream_text)
                        self.assertNotIn(LOCAL_TOKEN, json.dumps(headers_lc))

    def test_namespace_tool_round_trip_end_to_end(self):
        # R2: the server must carry the per-request namespace map into stream
        # translation; a proxy tool call returns as namespace + original child.
        module = self._server()
        ns_tool = {
            "type": "namespace",
            "name": "mcp__node_repl",
            "description": "Node REPL",
            "tools": [
                {
                    "type": "function",
                    "name": "js",
                    "description": "Run JS",
                    "parameters": {
                        "type": "object",
                        "properties": {"code": {"type": "string"}},
                        "required": ["code"],
                    },
                    "strict": False,
                }
            ],
        }
        tools = [
            {
                "type": "function",
                "name": TOOL_NAME,
                "description": f"Read {TOOL_ARGS_MARKER}.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            ns_tool,
        ]

        def script(handler, record):
            upstream_body = json.loads(record["body"])
            flat_names = [
                tool["function"]["name"]
                for tool in upstream_body["tools"]
                if tool.get("type") == "function"
            ]
            handler.server.namespace_observed = {
                "has_namespace_def": any(
                    tool.get("type") == "namespace" for tool in upstream_body["tools"]
                ),
                "flat_names": flat_names,
            }
            proxy = next(name for name in flat_names if name.startswith("ocg_"))
            handler._reply(
                200,
                {"Content-Type": "text/event-stream"},
                _sse_bytes(_namespace_chat_sse(proxy)),
            )

        with _fake_upstream({"/chat/completions": script}) as (fake, upstream_port):
            with _local_server(module, f"http://127.0.0.1:{upstream_port}") as (
                srv,
                port,
                _thread,
            ):
                payload = json.dumps(
                    _local_request("deepseek-v4-flash", "max", tools=tools)
                ).encode("utf-8")
                status, headers, body = _raw_request(
                    port,
                    "POST",
                    "/v1/responses",
                    _inference_headers(payload),
                    body=payload,
                    timeout=10.0,
                )
                self.assertEqual(status, 200)
                self.assertTrue(
                    headers.get("content-type", "").startswith("text/event-stream")
                )
                self.assertEqual(len(fake.records), 1)
                self.assertFalse(fake.namespace_observed["has_namespace_def"])
                self.assertNotIn("js", fake.namespace_observed["flat_names"])
                events = _parse_sse(body)
                event_names = [name for name, _ in events]
                self.assertIn("response.created", event_names)
                self.assertIn("response.completed", event_names)
                added = [
                    payload_dict["item"]
                    for name, payload_dict in events
                    if name == "response.output_item.added"
                    and payload_dict["item"].get("type") == "function_call"
                ]
                done = [
                    payload_dict["item"]
                    for name, payload_dict in events
                    if name == "response.output_item.done"
                    and payload_dict["item"].get("type") == "function_call"
                ]
                self.assertEqual(len(added), 1)
                self.assertEqual(len(done), 1)
                for item in added + done:
                    self.assertEqual(item["name"], "js")
                    self.assertEqual(item.get("namespace"), "mcp__node_repl")
                    self.assertEqual(item["call_id"], "call_ns")
                self.assertEqual(done[0]["arguments"], '{"code":"1+1"}')
                self.assertNotIn(LOCAL_TOKEN, json.dumps(events))
                self.assertNotIn(API_KEY, json.dumps(events))
                self.assertNotIn(PROMPT, json.dumps(events))

    def test_concurrency_cap_returns_429(self):
        # R1/R2: max_concurrent=1; second inference is 429 before upstream.
        module = self._server()
        blocked = threading.Event()
        release = threading.Event()

        def blocking_script(handler, record):
            blocked.set()
            release.wait(5.0)
            handler._reply(
                200,
                {"Content-Type": "text/event-stream"},
                _sse_bytes(_responses_fixture()),
            )

        with _fake_upstream(
            {"/responses": blocking_script}, block_event=blocked, release_event=release
        ) as (fake, upstream_port):
            with _local_server(
                module, f"http://127.0.0.1:{upstream_port}", max_concurrent=1
            ) as (srv, port, _thread):
                payload = json.dumps(_local_request("gpt-5.6-luna", "high")).encode("utf-8")
                first_result = {}

                def first_call():
                    first_result["value"] = _raw_request(
                        port,
                        "POST",
                        "/v1/responses",
                        _inference_headers(payload),
                        body=payload,
                        timeout=10.0,
                    )

                first_thread = threading.Thread(target=first_call, daemon=True)
                first_thread.start()
                self.assertTrue(blocked.wait(5.0))
                status2, headers2, body2 = _raw_request(
                    port,
                    "POST",
                    "/v1/responses",
                    _inference_headers(payload),
                    body=payload,
                    timeout=10.0,
                )
                self.assertEqual(status2, 429)
                self.assertEqual(headers2.get("retry-after"), "1")
                serialized2 = json.dumps(json.loads(body2))
                self.assertNotIn(LOCAL_TOKEN, serialized2)
                self.assertNotIn(API_KEY, serialized2)
                self.assertEqual(len(fake.records), 1)
                release.set()
                first_thread.join(timeout=10.0)
                self.assertFalse(first_thread.is_alive())
                status1, _, body1 = first_result["value"]
                self.assertEqual(status1, 200)
                self.assertIn(
                    "response.completed", [e for e, _ in _parse_sse(body1)]
                )

    def test_upstream_429_preserves_retry_after_and_sanitized_request_id(self):
        # R3: 429 + Retry-After preserved; request ID sanitized before local JSON.
        module = self._server()
        cases = [
            ("safe", "req_AbC123.xyz", "req_AbC123.xyz", "3", True),
            ("invalid_id", "bad/id!@#", SANITIZED_REQUEST_ID, "3", True),
            ("invalid_retry", "req_AbC123.xyz", "req_AbC123.xyz", SECRET_MARKER, False),
        ]
        for name, upstream_id, expected_id, retry_value, expect_retry in cases:
            with self.subTest(case=name):
                script = {
                    "/responses": (
                        429,
                        {"Retry-After": retry_value, "x-request-id": upstream_id},
                        json.dumps({"error": {"message": SECRET_MARKER}}).encode("utf-8"),
                    )
                }
                with _fake_upstream(script) as (fake, upstream_port):
                    with _local_server(
                        module, f"http://127.0.0.1:{upstream_port}"
                    ) as (srv, port, _thread):
                        payload = json.dumps(_local_request("gpt-5.6-luna", "high")).encode("utf-8")
                        status, headers, body = _raw_request(
                            port,
                            "POST",
                            "/v1/responses",
                            _inference_headers(payload),
                            body=payload,
                            timeout=10.0,
                        )
                        self.assertEqual(status, 429)
                        if expect_retry:
                            self.assertEqual(headers.get("retry-after"), "3")
                        else:
                            self.assertNotIn("retry-after", headers)
                        body_obj = json.loads(body)
                        self.assertEqual(body_obj.get("request_id"), expected_id)
                        serialized = json.dumps(body_obj)
                        self.assertNotIn(SECRET_MARKER, serialized)
                        self.assertNotIn(LOCAL_TOKEN, serialized)
                        self.assertNotIn(API_KEY, serialized)
                        self.assertNotIn(PROMPT, serialized)
                        if not expect_retry:
                            serialized_headers = json.dumps(dict(headers))
                            for marker in (SECRET_MARKER, LOCAL_TOKEN, API_KEY, PROMPT):
                                self.assertNotIn(marker, serialized_headers)

    def test_connect_error_is_sanitized_json(self):
        # R3: connection failure becomes 502 JSON with a stable code, no secrets.
        module = self._server()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        unused_port = probe.getsockname()[1]
        probe.close()
        with _local_server(
            module,
            f"http://127.0.0.1:{unused_port}",
            connect_timeout=0.2,
        ) as (srv, port, _thread):
            payload = json.dumps(_local_request("deepseek-v4-flash", "max")).encode("utf-8")
            status, _, body = _raw_request(
                port,
                "POST",
                "/v1/responses",
                _inference_headers(payload),
                body=payload,
                timeout=10.0,
            )
            self.assertEqual(status, 502)
            body_obj = json.loads(body)
            self.assertEqual(body_obj.get("code"), CONNECT_ERROR_CODE)
            serialized = json.dumps(body_obj)
            self.assertNotIn(SECRET_MARKER, serialized)
            self.assertNotIn(LOCAL_TOKEN, serialized)
            self.assertNotIn(API_KEY, serialized)
            self.assertNotIn(PROMPT, serialized)

    def test_truncated_or_idle_stream_emits_response_failed(self):
        # R3: truncated/idle upstream streams become response.failed SSE.
        module = self._server()
        truncated_pairs = [
            ("response.created", {"type": "response.created", "response": {"id": "resp_t"}}),
            ("response.in_progress", {"type": "response.in_progress", "response": {"id": "resp_t"}}),
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "item_id": "msg_t", "content_index": 0, "delta": "Hello"},
            ),
        ]
        with self.subTest(case="truncated"):
            script = {
                "/responses": (
                    200,
                    {"Content-Type": "text/event-stream"},
                    _sse_bytes(truncated_pairs),
                )
            }
            with _fake_upstream(script) as (fake, upstream_port):
                with _local_server(
                    module, f"http://127.0.0.1:{upstream_port}"
                ) as (srv, port, _thread):
                    payload = json.dumps(_local_request("gpt-5.6-luna", "high")).encode("utf-8")
                    status, _, body = _raw_request(
                        port,
                        "POST",
                        "/v1/responses",
                        _inference_headers(payload),
                        body=payload,
                        timeout=10.0,
                    )
                    self.assertEqual(status, 200)
                    events = _parse_sse(body)
                    failed = [p for e, p in events if e == "response.failed"]
                    self.assertTrue(failed)
                    self.assertEqual(
                        failed[-1].get("response", {}).get("error", {}).get("code"),
                        FAILURE_CODE_TRUNCATED,
                    )
                    serialized = json.dumps(events)
                    self.assertNotIn(SECRET_MARKER, serialized)
                    self.assertNotIn(LOCAL_TOKEN, serialized)
                    self.assertNotIn(API_KEY, serialized)
                    self.assertNotIn(PROMPT, serialized)
        with self.subTest(case="idle"):
            release = threading.Event()

            def idle_script(handler, record):
                first = _sse_bytes(
                    [("response.created", {"type": "response.created", "response": {"id": "resp_i"}})]
                )
                handler.send_response(200)
                handler.send_header("Content-Type", "text/event-stream")
                handler.send_header("Connection", "close")
                handler.end_headers()
                handler.wfile.write(first)
                handler.wfile.flush()
                release.wait(1.0)

            with _fake_upstream(
                {"/responses": idle_script}, release_event=release
            ) as (fake, upstream_port):
                with _local_server(
                    module,
                    f"http://127.0.0.1:{upstream_port}",
                    stream_idle_timeout=0.2,
                ) as (srv, port, _thread):
                    payload = json.dumps(_local_request("gpt-5.6-luna", "high")).encode("utf-8")
                    status, _, body = _raw_request(
                        port,
                        "POST",
                        "/v1/responses",
                        _inference_headers(payload),
                        body=payload,
                        timeout=10.0,
                    )
                    self.assertEqual(status, 200)
                    events = _parse_sse(body)
                    failed = [p for e, p in events if e == "response.failed"]
                    self.assertTrue(failed)
                    self.assertEqual(
                        failed[-1].get("response", {}).get("error", {}).get("code"),
                        FAILURE_CODE_IDLE,
                    )
                    serialized = json.dumps(events)
                    self.assertNotIn(SECRET_MARKER, serialized)
                    self.assertNotIn(LOCAL_TOKEN, serialized)
                    self.assertNotIn(API_KEY, serialized)
                    self.assertNotIn(PROMPT, serialized)
            release.set()

    def test_safe_log_has_exact_allowlisted_fields(self):
        # R1/R3: JSONL log records contain exactly the five allowlisted keys.
        module = self._server()
        fd, log_path = tempfile.mkstemp(prefix="red-gateway-log-", suffix=".jsonl")
        os.close(fd)
        try:
            script = {
                "/responses": (
                    200,
                    {"Content-Type": "text/event-stream"},
                    _sse_bytes(_responses_fixture()),
                )
            }
            with _fake_upstream(script) as (fake, upstream_port):
                with _local_server(
                    module,
                    f"http://127.0.0.1:{upstream_port}",
                    log_path=log_path,
                ) as (srv, port, _thread):
                    payload = json.dumps(_local_request("gpt-5.6-luna", "high")).encode("utf-8")
                    ok_status, _, _ = _raw_request(
                        port,
                        "POST",
                        "/v1/responses",
                        _inference_headers(payload),
                        body=payload,
                        timeout=10.0,
                    )
                    self.assertEqual(ok_status, 200)
                    bad_status, _, _ = _raw_request(
                        port,
                        "POST",
                        "/v1/responses",
                        {
                            "Authorization": "Bearer " + LOCAL_TOKEN,
                            "Content-Type": "application/json",
                            "Content-Length": "8",
                        },
                        body=b"{not-json",
                        timeout=10.0,
                    )
                    self.assertEqual(bad_status, 400)
            records = []
            with open(log_path, "r", encoding="utf-8") as log_file:
                for line in log_file:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
            self.assertGreaterEqual(len(records), 2)
            statuses = set()
            for record in records:
                self.assertEqual(
                    set(record.keys()),
                    {"model", "transport", "status", "duration_ms", "request_id"},
                )
                self.assertIsInstance(record["model"], str)
                self.assertIsInstance(record["transport"], str)
                self.assertIsInstance(record["duration_ms"], (int, float))
                self.assertGreaterEqual(record["duration_ms"], 0)
                self.assertIsNotNone(SAFE_REQUEST_ID_PATTERN.match(record["request_id"]))
                statuses.add(record["status"])
                serialized = json.dumps(record)
                self.assertNotIn(PROMPT, serialized)
                self.assertNotIn(TOOL_ARGS_MARKER, serialized)
                self.assertNotIn(API_KEY, serialized)
                self.assertNotIn(LOCAL_TOKEN, serialized)
                self.assertNotIn("prompt_tokens", serialized)
                self.assertNotIn("completion_tokens", serialized)
                self.assertNotIn("reasoning", serialized)
            self.assertIn(200, statuses)
            self.assertIn(400, statuses)
            success_records = [r for r in records if r["status"] == 200]
            self.assertTrue(success_records)
            self.assertEqual(success_records[-1]["model"], "gpt-5.6-luna")
            self.assertEqual(success_records[-1]["transport"], "responses")
        finally:
            try:
                os.unlink(log_path)
            except OSError:
                pass

    def test_authenticated_shutdown_stops_server(self):
        # R1: authenticated /shutdown returns safe JSON and stops the server.
        module = self._server()
        with _fake_upstream({}) as (fake, upstream_port):
            with _local_server(
                module, f"http://127.0.0.1:{upstream_port}"
            ) as (srv, port, thread):
                status, _, body = _raw_request(
                    port,
                    "POST",
                    "/shutdown",
                    {"Authorization": "Bearer " + LOCAL_TOKEN, "Content-Length": "0"},
                    timeout=10.0,
                )
                self.assertEqual(status, 200)
                serialized = json.dumps(json.loads(body))
                self.assertNotIn(LOCAL_TOKEN, serialized)
                self.assertNotIn(API_KEY, serialized)
                thread.join(timeout=2.0)
                self.assertFalse(thread.is_alive())
        self.assertTrue(_connect_fails(port))


if __name__ == "__main__":
    unittest.main(verbosity=2)
