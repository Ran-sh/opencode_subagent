#!/usr/bin/env python3
"""RED behavior contract tests for standalone live validation (DS-20260811-22).

The production manager currently exposes no live_test, _live_gateway_request or
_live_probe_profile, and the CLI test parser has no --all-models.  Every test
starts with a callable gate so the current module yields FAIL, never ERROR.
All HTTP behaviour is exercised through fake connections/responses: no real
socket, credential store, upstream call, thread or listening port is used.
The tests only observe the fixed public contracts from DEC-20260811-22-V1 and
never scan production source.
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.test_gateway_manager import _configure_home
from tests.test_manager import _all_files, _load_manager, _snapshot

LOCAL_TOKEN_MARKER = "local-token-red-marker"
UPSTREAM_SECRET_MARKER = "upstream-secret-red-marker"
REVIEWED_MODELS = 18
TEXT_MARKER = "OPENCODE_TEXT_OK"
TOOL_MARKER = "OPENCODE_TOOL_OK"
LIVE_TIMEOUT = 660
LINE_LIMIT = 1024 * 1024
TOTAL_LIMIT = 8 * 1024 * 1024


def _input_texts(payload):
    texts = []
    for item in payload.get("input") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if (isinstance(part, dict)
                        and part.get("type") == "input_text"
                        and isinstance(part.get("text"), str)):
                    texts.append(part["text"])
    return texts


def _sse_event(name, payload, crlf=False):
    ending = b"\r\n" if crlf else b"\n"
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return (b"event: " + name.encode("ascii") + ending
            + b"data: " + data + ending + ending)


class _FakeResponse:
    """Minimal HTTPResponse double: status, getheader, readline, read, close."""

    def __init__(self, status=200, body=b"", content_type="text/event-stream",
                 headers=None):
        self.status = status
        self._body = body
        self._pos = 0
        self._content_type = content_type
        self._headers = dict(headers or {})
        self.closed = False
        self.readline_calls = []
        self.read_calls = []

    def getheader(self, name, default=None):
        lowered = name.lower()
        if lowered == "content-type":
            return self._content_type
        for key, value in self._headers.items():
            if key.lower() == lowered:
                return value
        return default

    def readline(self, limit=None):
        self.readline_calls.append(limit)
        if self._pos >= len(self._body):
            return b""
        end = self._body.find(b"\n", self._pos)
        if limit is None:
            if end == -1:
                end = len(self._body)
            else:
                end += 1
            chunk = self._body[self._pos:end]
            self._pos += len(chunk)
            return chunk
        if end != -1 and end - self._pos + 1 <= limit:
            chunk = self._body[self._pos:end + 1]
            self._pos += len(chunk)
            return chunk
        chunk = self._body[self._pos:self._pos + limit]
        self._pos += len(chunk)
        return chunk

    def read(self, amount=None):
        self.read_calls.append(amount)
        if amount is None:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + amount]
        self._pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class _FakeConnection:
    """Minimal HTTPConnection double recording request/getresponse/close."""

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.request_calls = []
        self.closed = False
        self.response = None
        self.request_error = None
        self.getresponse_error = None

    def request(self, method, url, body=None, headers=None):
        self.request_calls.append((method, url, body, headers))
        if self.request_error is not None:
            raise self.request_error
        return None

    def getresponse(self):
        if self.getresponse_error is not None:
            raise self.getresponse_error
        if self.response is None:
            raise AssertionError("no fake response installed")
        return self.response

    def close(self):
        self.closed = True


class _FakeFactory:
    """Factory matching the frozen call: factory(host, port, timeout=660)."""

    def __init__(self, response=None, request_error=None,
                 getresponse_error=None):
        self.calls = []
        self.connections = []
        self.response = response
        self.request_error = request_error
        self.getresponse_error = getresponse_error

    def __call__(self, host, port, timeout):
        self.calls.append((host, port, timeout))
        connection = _FakeConnection(host, port, timeout)
        connection.response = self.response
        connection.request_error = self.request_error
        connection.getresponse_error = self.getresponse_error
        self.connections.append(connection)
        return connection


class LiveManagerRedTests(unittest.TestCase):
    def _manager(self):
        try:
            return _load_manager()
        except FileNotFoundError:
            self.fail("manager_missing")

    def _require_manager(self):
        manager = self._manager()
        for name in ("live_test", "_live_gateway_request",
                     "_live_probe_profile"):
            if not callable(getattr(manager, name, None)):
                self.fail(
                    "RED: manager.%s is missing (DS-20260811-22 REV-2)" % name)
        return manager

    def _assert_safe_failure(self, context, secrets=()):
        self.assertEqual(context.exception.code, "live_test_failed")
        serialized = str(context.exception) + json.dumps(
            context.exception.details, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, serialized)
        self.assertTrue(
            set(context.exception.details).issubset(
                {"model", "phase", "status", "code"}),
            "failure details must stay on the allowlist")

    def test_live_test_default_profile_single_starter_single_runner_zero_managed_write(self):
        manager = self._require_manager()
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            before = _snapshot(paths.home)
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            starter_calls = []

            def fake_starter(target):
                starter_calls.append(target)
                return {"status": "ok", "running": True, "changed": True,
                        "port": state["port"]}

            runner_calls = []

            def fake_runner(gateway_state, spec, effort):
                runner_calls.append((spec.id, effort, gateway_state["port"]))
                return {"model": spec.id, "effort": effort,
                        "transport": spec.transport, "text_ok": True,
                        "tool_ok": True, "usage": 0}

            payload = manager.live_test(paths, starter=fake_starter,
                                        profile_runner=fake_runner)
            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("status"), "ok")
            self.assertIs(payload.get("all_models"), False)
            self.assertEqual(payload.get("tested"), 1)
            self.assertEqual(payload.get("active_profile"),
                             {"model": manifest["selected_model"],
                              "effort": manifest["selected_effort"]})
            self.assertIs(payload.get("gateway_started"), True)
            self.assertEqual(starter_calls, [paths])
            self.assertEqual(len(runner_calls), 1)
            self.assertEqual(runner_calls[0][0], manifest["selected_model"])
            self.assertEqual(runner_calls[0][1], manifest["selected_effort"])
            self.assertEqual(runner_calls[0][2], state["port"])
            results = payload.get("results")
            self.assertIsInstance(results, list)
            self.assertEqual(len(results), 1)
            for key in ("model", "effort", "transport", "text_ok",
                        "tool_ok", "usage"):
                self.assertIn(key, results[0])
            self.assertIs(results[0]["text_ok"], True)
            self.assertIs(results[0]["tool_ok"], True)
            self.assertNotIn(state["local_gateway_token"],
                             json.dumps(payload, ensure_ascii=False))
            self.assertEqual(_snapshot(paths.home), before)

    def test_live_test_all_models_exact_registry_order_and_default_effort(self):
        manager = self._require_manager()
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            registry = manager.read_model_registry(paths)
            expected = [(model_id, spec.default_effort)
                        for model_id, spec in registry.items()]
            self.assertEqual(len(expected), REVIEWED_MODELS)
            self.assertTrue(all(spec.status == "active"
                                for spec in registry.values()))
            starter_calls = []

            def fake_starter(target):
                starter_calls.append(target)
                return {"status": "ok", "running": True, "changed": False,
                        "port": 0}

            runner_records = []

            def fake_runner(gateway_state, spec, effort):
                runner_records.append((spec.id, effort))
                return {"model": spec.id, "effort": effort,
                        "transport": spec.transport, "text_ok": True,
                        "tool_ok": True, "usage": 0}

            payload = manager.live_test(paths, all_models=True,
                                        starter=fake_starter,
                                        profile_runner=fake_runner)
            self.assertEqual(payload.get("status"), "ok")
            self.assertIs(payload.get("all_models"), True)
            self.assertEqual(payload.get("tested"), REVIEWED_MODELS)
            self.assertEqual(len(starter_calls), 1)
            self.assertEqual(runner_records, expected)
            results = payload.get("results")
            self.assertIsInstance(results, list)
            self.assertEqual([item.get("model") for item in results],
                             [model_id for model_id, _ in expected])

    def test_live_test_prechecks_and_runner_failure_are_safe_and_fail_fast(self):
        manager = self._require_manager()

        with self.subTest(case="all_models_non_bool"):
            with tempfile.TemporaryDirectory() as directory:
                paths = _configure_home(manager, Path(directory))
                starter = mock.Mock()
                runner = mock.Mock()
                with self.assertRaises(manager.ManagerError):
                    manager.live_test(paths, all_models="yes",
                                      starter=starter, profile_runner=runner)
                starter.assert_not_called()
                runner.assert_not_called()

        with self.subTest(case="disabled"):
            with tempfile.TemporaryDirectory() as directory:
                paths = _configure_home(manager, Path(directory))
                with mock.patch.object(manager, "_gateway_probe",
                                       return_value=False), mock.patch.object(
                        manager, "credential_has_key", return_value=True):
                    manager.disable(paths)
                before = _snapshot(paths.home)
                starter = mock.Mock()
                runner = mock.Mock()
                with self.assertRaises(manager.ManagerError):
                    manager.live_test(paths, starter=starter,
                                      profile_runner=runner)
                starter.assert_not_called()
                runner.assert_not_called()
                self.assertEqual(_snapshot(paths.home), before)

        with self.subTest(case="unavailable_reviewed_model"):
            with tempfile.TemporaryDirectory() as directory:
                paths = _configure_home(manager, Path(directory))
                state_payload = json.loads(
                    paths.state.read_text(encoding="utf-8"))
                records = state_payload["model_registry"]["models"]
                target = records[0]["id"]
                for record in records:
                    if record["id"] == target:
                        record["status"] = "unavailable"
                state_bytes = (json.dumps(
                    state_payload, ensure_ascii=False, indent=2) + "\n"
                ).encode()
                paths.state.write_bytes(state_bytes)
                manifest = json.loads(
                    paths.manifest.read_text(encoding="utf-8"))
                manifest["state_sha256"] = hashlib.sha256(
                    state_bytes).hexdigest()
                manifest_bytes = (json.dumps(
                    manifest, ensure_ascii=False, indent=2) + "\n").encode()
                paths.manifest.write_bytes(manifest_bytes)
                before = _snapshot(paths.home)
                starter = mock.Mock()
                runner = mock.Mock()
                with self.assertRaises(manager.ManagerError):
                    manager.live_test(paths, all_models=True,
                                      starter=starter, profile_runner=runner)
                starter.assert_not_called()
                runner.assert_not_called()
                self.assertEqual(_snapshot(paths.home), before)

        with self.subTest(case="runner_failure_fail_fast"):
            with tempfile.TemporaryDirectory() as directory:
                paths = _configure_home(manager, Path(directory))
                before = _snapshot(paths.home)
                starter = mock.Mock(return_value={
                    "status": "ok", "running": True, "changed": True,
                    "port": 0})
                runner_calls = []

                def failing_runner(gateway_state, spec, effort):
                    runner_calls.append(spec.id)
                    raise RuntimeError(UPSTREAM_SECRET_MARKER)

                with self.assertRaises(manager.ManagerError) as cm:
                    manager.live_test(paths, all_models=True,
                                      starter=starter,
                                      profile_runner=failing_runner)
                self.assertEqual(cm.exception.code, "live_test_failed")
                serialized = str(cm.exception) + json.dumps(
                    cm.exception.details, ensure_ascii=False)
                self.assertNotIn(UPSTREAM_SECRET_MARKER, serialized)
                self.assertNotIn(LOCAL_TOKEN_MARKER, serialized)
                starter.assert_called_once()
                self.assertEqual(len(runner_calls), 1)
                self.assertEqual(_snapshot(paths.home), before)

    def test_live_gateway_request_parses_fragmented_sse_completed_text_tool_usage_and_headers(self):
        manager = self._require_manager()
        gateway_state = {"version": 2, "port": 44123,
                         "local_gateway_token": LOCAL_TOKEN_MARKER}
        request = {
            "model": "model-red-test",
            "input": [{"role": "user",
                       "content": [{"type": "input_text",
                                    "text": TEXT_MARKER}]}],
            "stream": True,
        }
        done_call = {"type": "response.output_item.done",
                     "item": {"type": "function_call",
                              "name": "opencode_live_probe",
                              "arguments": '{"value":"%s"}' % TOOL_MARKER}}
        completed = {"type": "response.completed",
                     "response": {"id": "r1", "status": "completed",
                                  "usage": {"input_tokens": 1,
                                            "output_tokens": 2,
                                            "total_tokens": 3}}}
        fragment = (b"event: response.output_text.delta\r\n"
                    b'data: {"type":"response.output_text.delta",\r\n'
                    b'data: "delta":{"text":"' + TEXT_MARKER.encode("ascii")
                    + b'"}}\n\n')
        body = (b": sse-comment\r\n\r\n"
                + _sse_event("response.created",
                             {"type": "response.created"})
                + fragment
                + _sse_event("response.output_item.done", done_call, crlf=True)
                + _sse_event("response.in_progress",
                             {"type": "response.in_progress"})
                + _sse_event("response.completed", completed, crlf=True))
        response = _FakeResponse(status=200, body=body,
                                 content_type="text/event-stream")
        factory = _FakeFactory(response=response)
        result = manager._live_gateway_request(
            gateway_state, request, "model-red-test", "text",
            connection_factory=factory)
        self.assertEqual(
            factory.calls,
            [("127.0.0.1", gateway_state["port"], LIVE_TIMEOUT)])
        self.assertEqual(len(factory.connections), 1)
        connection = factory.connections[0]
        self.assertEqual(len(connection.request_calls), 1)
        method, url, raw_body, headers = connection.request_calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "/v1/responses")
        self.assertIsInstance(raw_body, bytes)
        body_payload = json.loads(raw_body.decode("utf-8"))
        self.assertEqual(body_payload, request)
        self.assertNotIn("phase", body_payload)
        self.assertEqual(headers.get("Authorization"),
                         "Bearer " + LOCAL_TOKEN_MARKER)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(headers.get("Accept"), "text/event-stream")
        self.assertEqual(headers.get("Connection"), "close")
        self.assertNotIn(LOCAL_TOKEN_MARKER, raw_body.decode("utf-8"))
        self.assertEqual(set(result),
                         {"output_text", "function_calls", "usage"})
        self.assertEqual(result["output_text"], TEXT_MARKER)
        function_calls = result["function_calls"]
        self.assertEqual(len(function_calls), 1)
        self.assertEqual(function_calls[0]["name"], "opencode_live_probe")
        self.assertEqual(function_calls[0]["arguments"],
                         '{"value":"%s"}' % TOOL_MARKER)
        self.assertEqual(result["usage"],
                         {"input_tokens": 1, "output_tokens": 2,
                          "total_tokens": 3})
        for value in result["usage"].values():
            self.assertIsInstance(value, int)
            self.assertGreaterEqual(value, 0)
        self.assertNotIn(LOCAL_TOKEN_MARKER, json.dumps(result))
        self.assertTrue(connection.closed)

    def test_live_gateway_request_rejects_http_media_failed_and_truncated_secret_free(self):
        manager = self._require_manager()
        gateway_state = {"version": 2, "port": 44123,
                         "local_gateway_token": LOCAL_TOKEN_MARKER}
        request = {"model": "model-red-test",
                   "input": [{"role": "user",
                              "content": [{"type": "input_text",
                                           "text": TEXT_MARKER}]}],
                   "stream": True}
        cases = [
            ("http_500",
             _FakeResponse(status=500,
                           body=b'{"code":"boom","message":"boom"}',
                           content_type="application/json"),
             None, "boom"),
            ("media_not_event_stream",
             _FakeResponse(status=200, body=b"data: {}\n\n",
                           content_type="application/json"),
             None, "boom"),
            ("response_failed",
             _FakeResponse(
                 status=200,
                 body=_sse_event(
                     "response.failed",
                     {"type": "response.failed",
                      "response": {"id": "",
                                   "error": {"code": "upstream_stream_failed",
                                             "message":
                                                 UPSTREAM_SECRET_MARKER}}}),
                 content_type="text/event-stream"),
             None, UPSTREAM_SECRET_MARKER),
            ("truncated_no_completed",
             _FakeResponse(
                 status=200,
                 body=_sse_event("response.created",
                                 {"type": "response.created"}),
                 content_type="text/event-stream"),
             None, None),
            ("socket_error",
             _FakeResponse(status=200, body=b"",
                           content_type="text/event-stream"),
             OSError(UPSTREAM_SECRET_MARKER), UPSTREAM_SECRET_MARKER),
        ]
        for label, response, request_error, forbidden in cases:
            with self.subTest(case=label):
                factory = _FakeFactory(response=response,
                                       request_error=request_error)
                with self.assertRaises(manager.ManagerError) as cm:
                    manager._live_gateway_request(
                        gateway_state, request, "model-red-test", "text",
                        connection_factory=factory)
                self._assert_safe_failure(cm, (LOCAL_TOKEN_MARKER,))
                if forbidden is not None:
                    serialized = str(cm.exception) + json.dumps(
                        cm.exception.details, ensure_ascii=False)
                    self.assertNotIn(forbidden, serialized)
                self.assertTrue(factory.connections[0].closed)

        with self.subTest(case="request_serialization_exception"):
            circular = {}
            circular["self"] = circular
            serialization_factory = mock.Mock()
            try:
                manager._live_gateway_request(
                    gateway_state, circular, "model-red-test", "text",
                    connection_factory=serialization_factory)
            except manager.ManagerError as cm:
                self._assert_safe_failure(mock.Mock(exception=cm), (LOCAL_TOKEN_MARKER,))
                serialization_factory.assert_not_called()
            except Exception as exc:
                self.fail(
                    "RED: unsanitized request serialization exception type "
                    + type(exc).__name__)

        with self.subTest(case="factory_runtime_exception"):
            def failing_factory(host, port, timeout):
                raise RuntimeError(UPSTREAM_SECRET_MARKER)

            try:
                manager._live_gateway_request(
                    gateway_state, request, "model-red-test", "text",
                    connection_factory=failing_factory)
            except manager.ManagerError as cm:
                self._assert_safe_failure(
                    mock.Mock(exception=cm), (LOCAL_TOKEN_MARKER, UPSTREAM_SECRET_MARKER))
            except Exception as exc:
                self.fail(
                    "RED: unsanitized factory exception type "
                    + type(exc).__name__)

    def test_live_gateway_request_rejects_utf8_json_line_and_total_limits(self):
        manager = self._require_manager()
        gateway_state = {"version": 2, "port": 44123,
                         "local_gateway_token": LOCAL_TOKEN_MARKER}
        request = {"model": "model-red-test", "input": [], "stream": True}
        too_long_line = b"data: " + b"a" * (LINE_LIMIT + 1) + b"\n\n"
        filler = _sse_event("response.created", {"type": "response.created"})
        too_large_total = filler * (TOTAL_LIMIT // len(filler) + 1)
        cases = (
            ("invalid_utf8", b"data: \xff\xfe\n\n"),
            ("invalid_json", b"data: not-json\n\n"),
            ("line_over_1mib", too_long_line),
            ("total_over_8mib", too_large_total),
        )
        for label, body in cases:
            with self.subTest(case=label):
                response = _FakeResponse(status=200, body=body,
                                         content_type="text/event-stream")
                factory = _FakeFactory(response=response)
                with self.assertRaises(manager.ManagerError) as cm:
                    manager._live_gateway_request(
                        gateway_state, request, "model-red-test", "tool",
                        connection_factory=factory)
                self._assert_safe_failure(cm, (LOCAL_TOKEN_MARKER,))
                self.assertNotIn("a" * 16, str(cm.exception))
                self.assertTrue(factory.connections[0].closed)

    def test_live_probe_profile_builds_exact_text_and_tool_requests_for_nondefault_effort(self):
        manager = self._require_manager()
        gateway_state = {"version": 2, "port": 44123,
                         "local_gateway_token": LOCAL_TOKEN_MARKER}
        spec = manager.get_model("deepseek-v4-pro", "high")
        calls = []

        def fake_requester(state, payload, model, phase):
            calls.append((state, payload, model, phase))
            if phase == "text":
                return {"output_text": TEXT_MARKER, "function_calls": [],
                        "usage": {"total_tokens": 1}}
            return {"output_text": "", "function_calls": [
                {"name": "opencode_live_probe",
                 "arguments": '{"value":"%s"}' % TOOL_MARKER}],
                "usage": {"total_tokens": 4}}

        result = manager._live_probe_profile(
            gateway_state, spec, "high", requester=fake_requester)
        self.assertEqual(len(calls), 2)
        self.assertEqual([item[3] for item in calls], ["text", "tool"])
        self.assertEqual(calls[0][2], spec.id)
        self.assertEqual(calls[1][2], spec.id)
        self.assertEqual(calls[0][0], gateway_state)
        self.assertEqual(calls[1][0], gateway_state)
        text_payload, tool_payload = calls[0][1], calls[1][1]
        for payload in (text_payload, tool_payload):
            self.assertEqual(payload.get("model"), spec.id)
            self.assertIs(payload.get("stream"), True)
            self.assertEqual(payload.get("reasoning"), {"effort": "high"})
            self.assertNotIn("api_key", payload)
            self.assertNotIn("x-api-key", payload)
            self.assertNotIn(LOCAL_TOKEN_MARKER,
                             json.dumps(payload, ensure_ascii=False))
        self.assertIn(TEXT_MARKER, _input_texts(text_payload))
        self.assertNotIn("tools", text_payload)
        self.assertNotIn("tool_choice", text_payload)
        self.assertEqual(tool_payload.get("tool_choice"), "auto")
        tools = tool_payload.get("tools")
        self.assertIsInstance(tools, list)
        self.assertEqual(len(tools), 1)
        tool = tools[0]
        self.assertEqual(tool.get("type"), "function")
        self.assertEqual(tool.get("name"), "opencode_live_probe")
        self.assertIsInstance(tool.get("description"), str)
        parameters = tool.get("parameters")
        self.assertIsInstance(parameters, dict)
        self.assertEqual(parameters.get("type"), "object")
        self.assertIn("value", parameters.get("properties") or {})
        self.assertIs(parameters.get("additionalProperties"), False)
        self.assertTrue(_input_texts(tool_payload))
        self.assertEqual(result.get("model"), spec.id)
        self.assertEqual(result.get("effort"), "high")
        self.assertEqual(result.get("transport"), spec.transport)
        self.assertIs(result.get("text_ok"), True)
        self.assertIs(result.get("tool_ok"), True)
        self.assertEqual(result.get("usage"), 5)

    def test_live_probe_profile_omits_default_reasoning_and_rejects_bad_text(self):
        manager = self._require_manager()
        gateway_state = {"version": 2, "port": 44123,
                         "local_gateway_token": LOCAL_TOKEN_MARKER}
        spec = manager.get_model("glm-5.1", "default")

        with self.subTest(case="pseudo_default_omits_reasoning"):
            calls = []

            def good_requester(state, payload, model, phase):
                calls.append(payload)
                if phase == "text":
                    return {"output_text": TEXT_MARKER, "function_calls": [],
                            "usage": {"total_tokens": 2}}
                return {"output_text": "", "function_calls": [
                    {"name": "opencode_live_probe",
                     "arguments": '{"value":"%s"}' % TOOL_MARKER}],
                    "usage": {"total_tokens": 5}}

            result = manager._live_probe_profile(
                gateway_state, spec, "default", requester=good_requester)
            self.assertEqual(len(calls), 2)
            for payload in calls:
                self.assertNotIn("reasoning", payload)
            self.assertIs(result.get("text_ok"), True)
            self.assertIs(result.get("tool_ok"), True)
            self.assertEqual(result.get("usage"), 7)

        with self.subTest(case="bad_text_marker_fails_before_tool"):
            phases = []

            def bad_requester(state, payload, model, phase):
                phases.append(phase)
                if phase == "text":
                    return {"output_text": "unexpected response text",
                            "function_calls": [],
                            "usage": {"total_tokens": 0}}
                return {"output_text": "", "function_calls": [],
                        "usage": {"total_tokens": 0}}

            with self.assertRaises(manager.ManagerError) as cm:
                manager._live_probe_profile(
                    gateway_state, spec, "default",
                    requester=bad_requester)
            self.assertEqual(cm.exception.code, "live_test_failed")
            self.assertEqual(phases, ["text"])
            serialized = str(cm.exception) + json.dumps(
                cm.exception.details, ensure_ascii=False)
            self.assertNotIn("unexpected response text", serialized)
            self.assertNotIn(LOCAL_TOKEN_MARKER, serialized)

    def test_live_probe_profile_rejects_bad_tool_variants_secret_free(self):
        manager = self._require_manager()
        gateway_state = {"version": 2, "port": 44123,
                         "local_gateway_token": LOCAL_TOKEN_MARKER}
        spec = manager.get_model("deepseek-v4-flash", "max")
        good_args = '{"value":"%s"}' % TOOL_MARKER
        variants = {
            "wrong_name": [{"name": "opencode_live_probe_other",
                            "arguments": good_args}],
            "custom_tool_call": [{"type": "custom_tool_call",
                                  "name": "opencode_live_probe",
                                  "arguments": good_args}],
            "invalid_arguments_json": [{"name": "opencode_live_probe",
                                        "arguments": "not-json"}],
            "wrong_value": [{"name": "opencode_live_probe",
                             "arguments": '{"value":"OPENCODE_BAD"}'}],
            "only_delta_no_done": [],
        }
        for label, function_calls in variants.items():
            with self.subTest(variant=label):

                def fake_requester(state, payload, model, phase):
                    if phase == "text":
                        return {"output_text": TEXT_MARKER,
                                "function_calls": [],
                                "usage": {"total_tokens": 0}}
                    return {"output_text": "",
                            "function_calls": function_calls,
                            "usage": {"total_tokens": 0}}

                with self.assertRaises(manager.ManagerError) as cm:
                    manager._live_probe_profile(
                        gateway_state, spec, "max",
                        requester=fake_requester)
                self._assert_safe_failure(cm, (LOCAL_TOKEN_MARKER,))
                serialized = str(cm.exception) + json.dumps(
                    cm.exception.details, ensure_ascii=False)
                self.assertNotIn(TOOL_MARKER, serialized)
                self.assertNotIn(good_args, serialized)
                self.assertNotIn("not-json", serialized)
                self.assertNotIn(UPSTREAM_SECRET_MARKER, serialized)

    def test_cli_test_all_models_dispatch_and_empty_home_zero_write(self):
        manager = self._require_manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            seen = []

            def fake_live_test(paths, all_models=False, starter=None,
                               profile_runner=None):
                seen.append(all_models)
                return {"status": "ok", "all_models": all_models,
                        "tested": 0, "results": []}

            with mock.patch.object(manager, "live_test",
                                   side_effect=fake_live_test):
                with redirect_stdout(io.StringIO()) as stdout_false:
                    rc_false = manager.main(
                        ["test", "--json", "--codex-home", str(home)])
                with redirect_stdout(io.StringIO()) as stdout_true:
                    rc_true = manager.main(
                        ["test", "--all-models", "--json",
                         "--codex-home", str(home)])
            self.assertEqual(rc_false, 0)
            self.assertEqual(rc_true, 0)
            self.assertEqual(seen, [False, True])
            payload_false = json.loads(stdout_false.getvalue())
            payload_true = json.loads(stdout_true.getvalue())
            self.assertEqual(payload_false.get("status"), "ok")
            self.assertIs(payload_false.get("all_models"), False)
            self.assertEqual(payload_true.get("status"), "ok")
            self.assertIs(payload_true.get("all_models"), True)
            self.assertEqual(_all_files(home), [])

            for argv in (["test"], ["test", "--all-models"]):
                with self.subTest(argv=argv), redirect_stdout(
                        io.StringIO()) as stdout_empty:
                    rc_empty = manager.main(
                        argv + ["--json", "--codex-home", str(home)])
                self.assertEqual(rc_empty, 2)
                payload_empty = json.loads(stdout_empty.getvalue())
                self.assertEqual(payload_empty.get("status"),
                                 "gateway_unavailable")
            self.assertEqual(_all_files(home), [])


if __name__ == "__main__":
    unittest.main()
