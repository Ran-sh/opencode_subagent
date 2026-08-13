#!/usr/bin/env python3
"""RED contract tests for the plain registry snapshot runtime link (DS-20260811-19 REV-3).

The production protocol core, gateway server and manager currently have no
``model_registry_snapshot`` parameter.  These tests lock the approved cross-module
handoff: core and server accept an optional plain snapshot (schema_version=1 JSON),
reject unavailable/malformed registries before any upstream connection, keep the
snapshot and secrets out of repr/health/runtime output, and the manager passes the
managed plain snapshot through its server factory.  Missing keywords fail via
``_require_keyword`` (a clean FAIL, never an ERROR) before any production call.
Only explicit placeholders are used; no real credentials are read.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import test_gateway_core as corefx
from tests import test_gateway_manager as managerfx
from tests import test_gateway_server as serverfx
from tests import test_manager as managerbase
from tests import test_manager_registry as registryfx

RUNTIME_API_KEY = "red-registry-api-key-placeholder"
RUNTIME_LOCAL_TOKEN = "red-registry-local-token-placeholder"


def _variant_snapshot(models):
    """Plain schema_version=1 snapshot with hy3 unavailable and flash modified."""
    registry = registryfx._registry_variant(models)
    return models.registry_snapshot(registry)


def _load_core():
    """Load opencode_gateway.py under a unique module identity."""
    return corefx._load_production_module(
        "opencode_gateway_registry_red", corefx._GATEWAY_PATH
    )


def _require_keyword(testcase, fn, name):
    """Fail cleanly when the optional keyword is absent or does not default to None."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        testcase.fail(f"signature unavailable for {name}")
        return
    param = params.get(name)
    if param is None:
        testcase.fail(f"missing optional keyword {name}")
        return
    if param.default is not None:
        testcase.fail(f"optional keyword {name} must default to None")


def _assert_not_leaked(testcase, text, secret, label):
    """Fail with a fixed message only; never echo secrets or full output."""
    if secret and secret in text:
        testcase.fail(f"{label} leaked into observable output")


def _post_json(port, path, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + RUNTIME_LOCAL_TOKEN,
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    return serverfx._raw_request(port, "POST", path, headers, body)


def _start_server(module, snapshot, upstream_base):
    server = module.create_server(
        "127.0.0.1",
        0,
        RUNTIME_LOCAL_TOKEN,
        RUNTIME_API_KEY,
        upstream_base=upstream_base,
        allow_test_http=True,
        model_registry_snapshot=snapshot,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class RegistrySnapshotRedTests(unittest.TestCase):
    """Four RED behavior contracts for the registry snapshot runtime link."""

    def test_core_uses_plain_snapshot_and_rejects_unavailable(self):
        models = managerbase._load_opencode_models()
        core = _load_core()
        _require_keyword(self, core.prepare_upstream_request, "model_registry_snapshot")
        snapshot = _variant_snapshot(models)
        snapshot_before = copy.deepcopy(snapshot)
        request = corefx._codex_request()
        request_before = copy.deepcopy(request)

        upstream = core.prepare_upstream_request(
            request,
            "deepseek-v4-flash",
            "high",
            RUNTIME_API_KEY,
            model_registry_snapshot=snapshot,
        )
        self.assertEqual(upstream.transport, "chat_completions")
        self.assertEqual(upstream.path, "/chat/completions")
        self.assertEqual(request, request_before)
        self.assertEqual(snapshot, snapshot_before)

        hy3_request = copy.deepcopy(request)
        hy3_request["model"] = "hy3"
        with self.assertRaises(core.GatewayError) as cm:
            core.prepare_upstream_request(
                hy3_request,
                "hy3",
                "high",
                RUNTIME_API_KEY,
                model_registry_snapshot=snapshot,
            )
        self.assertEqual(cm.exception.code, "model_unavailable")
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(snapshot, snapshot_before)

        malformed = copy.deepcopy(snapshot)
        malformed["models"][0]["efforts"] = [{}]
        with self.assertRaises(core.GatewayError) as cm:
            core.prepare_upstream_request(
                request,
                "deepseek-v4-flash",
                "high",
                RUNTIME_API_KEY,
                model_registry_snapshot=malformed,
            )
        self.assertEqual(cm.exception.code, "invalid_registry")
        self.assertEqual(cm.exception.status, 500)
        text = str(cm.exception)
        _assert_not_leaked(self, text, RUNTIME_API_KEY, "api key")
        _assert_not_leaked(self, text, RUNTIME_LOCAL_TOKEN, "local token")
        _assert_not_leaked(self, text, "DeepSeek", "snapshot model name")
        self.assertEqual(request, request_before)

        default_upstream = core.prepare_upstream_request(
            request, "deepseek-v4-flash", "high", RUNTIME_API_KEY
        )
        self.assertEqual(default_upstream.path, "/chat/completions")

    def test_manager_gateway_serve_passes_plain_snapshot_to_factory(self):
        manager = managerbase._load_manager()
        models = manager._models_module()
        with tempfile.TemporaryDirectory() as directory:
            paths = managerfx._configure_home(manager, Path(directory))
            registry = registryfx._registry_variant(models)
            state_bytes_before = registryfx._install_registry(
                manager, paths, models, registry
            )
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            token = state["local_gateway_token"]
            factory = managerfx._FakeServerFactory()
            with mock.patch.object(
                manager, "read_credential_key", return_value=RUNTIME_API_KEY
            ):
                exit_code = manager.gateway_serve(paths, server_factory=factory)
            self.assertEqual(exit_code, 0)
            self.assertEqual(factory.host, "127.0.0.1")
            self.assertEqual(factory.port, state["port"])
            self.assertEqual(factory.local_token, token)
            self.assertEqual(factory.api_key, RUNTIME_API_KEY)
            self.assertEqual(factory.log_path, str(paths.gateway_log))
            self.assertEqual(set(factory.kwargs), {"model_registry_snapshot"})

            snapshot = factory.kwargs["model_registry_snapshot"]
            self.assertEqual(snapshot.get("schema_version"), 1)
            records = snapshot.get("models")
            self.assertIsInstance(records, list)
            self.assertEqual(len(records), len(models.MODELS))
            self.assertEqual(
                [record.get("id") for record in records], sorted(models.MODELS)
            )
            self.assertTrue(all(isinstance(record, dict) for record in records))
            self.assertTrue(
                all(not dataclasses.is_dataclass(record) for record in records)
            )
            hy3_record = next(
                record for record in records if record.get("id") == "hy3"
            )
            self.assertEqual(hy3_record.get("status"), "unavailable")
            serialized = json.dumps(snapshot)
            self.assertTrue(serialized)
            _assert_not_leaked(self, serialized, token, "local token")
            _assert_not_leaked(self, serialized, RUNTIME_API_KEY, "api key")
            self.assertFalse(paths.gateway_runtime.exists())
            self.assertTrue(factory.server.closed)
            self.assertEqual(paths.state.read_bytes(), state_bytes_before)

    def test_server_rejects_malformed_snapshot_before_bind(self):
        module = serverfx._load_server_module()
        if module is None:
            self.fail("server_missing")
        _require_keyword(self, module.create_server, "model_registry_snapshot")
        models = managerbase._load_opencode_models()
        malformed = _variant_snapshot(models)
        malformed["models"][0]["efforts"] = [{}]
        with mock.patch.object(module, "GatewayServer") as gateway_cls:
            with self.assertRaises(ValueError) as cm:
                module.create_server(
                    "127.0.0.1",
                    0,
                    RUNTIME_LOCAL_TOKEN,
                    RUNTIME_API_KEY,
                    upstream_base="http://127.0.0.1:9",
                    allow_test_http=True,
                    model_registry_snapshot=malformed,
                )
            text = str(cm.exception)
            _assert_not_leaked(self, text, RUNTIME_API_KEY, "api key")
            _assert_not_leaked(self, text, RUNTIME_LOCAL_TOKEN, "local token")
            _assert_not_leaked(self, text, "model_registry", "snapshot key")
            _assert_not_leaked(self, text, "DeepSeek", "snapshot model name")
        gateway_cls.assert_not_called()

    def test_server_rejects_unavailable_before_upstream_and_keeps_snapshot_private(self):
        module = serverfx._load_server_module()
        if module is None:
            self.fail("server_missing")
        _require_keyword(self, module.create_server, "model_registry_snapshot")
        models = managerbase._load_opencode_models()
        snapshot = _variant_snapshot(models)
        snapshot_before = copy.deepcopy(snapshot)
        script = {
            "/chat/completions": (
                200,
                {"Content-Type": "text/event-stream"},
                serverfx._sse_bytes(serverfx._chat_fixture()),
            )
        }
        with serverfx._fake_upstream(script) as (fake, upstream_port):
            server, thread = _start_server(
                module, snapshot, f"http://127.0.0.1:{upstream_port}"
            )
            try:
                port = server.server_address[1]
                status, _headers, body = _post_json(
                    port,
                    "/v1/responses",
                    {"model": "hy3", "reasoning": {"effort": "high"}},
                )
                self.assertEqual(status, 400)
                payload = json.loads(body.decode("utf-8"))
                self.assertEqual(payload.get("code"), "model_unavailable")
                self.assertEqual(fake.records, [])
                hy3_text = body.decode("utf-8", "replace")
                _assert_not_leaked(self, hy3_text, RUNTIME_API_KEY, "api key")
                _assert_not_leaked(self, hy3_text, RUNTIME_LOCAL_TOKEN, "local token")
                _assert_not_leaked(self, hy3_text, "model_registry", "snapshot key")
                _assert_not_leaked(self, hy3_text, "DeepSeek", "snapshot model name")

                flash_request = serverfx._local_request("deepseek-v4-flash", "high")
                status, _headers, body = _post_json(
                    port, "/v1/responses", flash_request
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(fake.records), 1)
                self.assertEqual(fake.records[0]["path"], "/chat/completions")
                flash_text = body.decode("utf-8", "replace")
                _assert_not_leaked(self, flash_text, RUNTIME_API_KEY, "api key")
                _assert_not_leaked(self, flash_text, RUNTIME_LOCAL_TOKEN, "local token")
                _assert_not_leaked(self, flash_text, "model_registry", "snapshot key")
                _assert_not_leaked(self, flash_text, "DeepSeek", "snapshot model name")

                health_status, _headers, health_body = serverfx._raw_request(
                    port, "GET", "/health", {}
                )
                self.assertEqual(health_status, 200)
                self.assertEqual(
                    json.loads(health_body.decode("utf-8")), {"status": "ok"}
                )
                health_text = health_body.decode("utf-8", "replace")
                _assert_not_leaked(self, health_text, RUNTIME_API_KEY, "api key")
                _assert_not_leaked(self, health_text, RUNTIME_LOCAL_TOKEN, "local token")
                _assert_not_leaked(self, health_text, "model_registry", "snapshot key")
                _assert_not_leaked(self, health_text, "DeepSeek", "snapshot model name")

                server_repr = repr(server)
                _assert_not_leaked(self, server_repr, RUNTIME_API_KEY, "api key")
                _assert_not_leaked(self, server_repr, RUNTIME_LOCAL_TOKEN, "local token")
                _assert_not_leaked(self, server_repr, "model_registry", "snapshot key")
                _assert_not_leaked(self, server_repr, "DeepSeek", "snapshot model name")
            finally:
                try:
                    server.shutdown()
                except Exception:
                    pass
                server.server_close()
                thread.join(timeout=2.0)
        self.assertEqual(snapshot, snapshot_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
