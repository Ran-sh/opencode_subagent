#!/usr/bin/env python3
"""RED contract tests for gateway lifecycle management (DS-20260811-14 REV-3).

The production manager currently exposes only placeholders for gateway
lifecycle: no read_gateway_state, no _gateway_probe/_gateway_shutdown, no
gateway_status/start/stop, no _spawn_gateway_process, no gateway_serve, no
working _gateway-token helper, and disable/uninstall never stop the gateway.
Every test therefore FAILs (never ERRORs) against a missing interface or a
placeholder behaviour.  Tests observe only the fixed public contract using
temporary files, loopback sockets, mocked credentials and mocked process
spawning.  No real credential store, upstream network, background child or
PID termination is used.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "codex-opencode-go-subagent" / "scripts" / "codex_opencode_go.py"
SERVER_SCRIPT = (
    REPO_ROOT / "codex-opencode-go-subagent" / "scripts" / "opencode_gateway_server.py"
)
SECRET_MARKER = "sk-" + "opencode-red-test-secret-marker"
LOCAL_TOKEN = "red-test-local-token-00000000000000000000"


def _load_manager():
    sys.modules.pop("codex_opencode_go", None)
    spec = importlib.util.spec_from_file_location("codex_opencode_go", str(SCRIPT))
    if spec is None or spec.loader is None:
        raise FileNotFoundError(SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_server():
    spec = importlib.util.spec_from_file_location(
        "opencode_gateway_server", str(SERVER_SCRIPT)
    )
    if spec is None or spec.loader is None:
        raise FileNotFoundError(SERVER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _catalog_loader():
    def loader():
        return {
            "models": [
                {
                    "slug": "unrelated-parent",
                    "name": "Unrelated Parent",
                    "multi_agent_version": "v9",
                    "context_window": 999,
                    "input_modalities": ["text"],
                    "shell_type": "shell_command",
                    "apply_patch_tool_type": "freeform",
                    "base_instructions": "Gateway lifecycle test template instructions.",
                }
            ]
        }

    return loader


def _snapshot(root):
    entries = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            entries[rel] = ("dir", None)
        elif path.is_file():
            entries[rel] = ("file", path.read_bytes())
    return entries


def _all_files(root):
    return [path for path in sorted(root.rglob("*")) if path.is_file()]


def _require_callable(testcase, module, name):
    testcase.assertTrue(
        callable(getattr(module, name, None)), f"missing callable {name}"
    )


def _configure_home(manager, directory):
    paths = manager.resolve_paths(str(directory))
    with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
        manager, "credential_has_key", return_value=True
    ), mock.patch.object(manager, "read_credential_key", return_value=SECRET_MARKER), mock.patch.object(
        manager, "store_credential_key"
    ):
        result = manager.setup(
            paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
        )
    if result.get("status") != "configured":
        raise AssertionError(f"setup did not configure: {result}")
    return paths


_POPEN_POSITIONAL = {
    "args": 0,
    "bufsize": 1,
    "executable": 2,
    "stdin": 3,
    "stdout": 4,
    "stderr": 5,
    "preexec_fn": 6,
    "close_fds": 7,
    "shell": 8,
    "cwd": 9,
    "env": 10,
    "universal_newlines": 11,
    "startupinfo": 12,
    "creationflags": 13,
    "restore_signals": 14,
    "start_new_session": 15,
}


def _popen_value(call, name, default=None):
    if name in call.kwargs:
        return call.kwargs[name]
    index = _POPEN_POSITIONAL.get(name)
    if index is not None and len(call.args) > index:
        return call.args[index]
    return default


class _FakeServer:
    def __init__(self, serve_behavior=None, **kwargs):
        self.serve_behavior = serve_behavior
        self.host = kwargs.get("host")
        self.port = kwargs.get("port")
        self.local_token = kwargs.get("local_token")
        self.api_key = kwargs.get("api_key")
        self.log_path = kwargs.get("log_path")
        self.closed = False
        self.serve_forever_calls = 0

    def serve_forever(self, poll_interval=0.5):
        self.serve_forever_calls += 1
        if self.serve_behavior is not None:
            self.serve_behavior(self)

    def server_close(self):
        self.closed = True


class _FakeServerFactory:
    def __init__(self, serve_behavior=None):
        self.serve_behavior = serve_behavior
        self.server = None
        self.host = None
        self.port = None
        self.local_token = None
        self.api_key = None
        self.log_path = None
        self.kwargs = None

    def __call__(self, host, port, local_token, api_key, log_path=None, **kwargs):
        self.host = host
        self.port = port
        self.local_token = local_token
        self.api_key = api_key
        self.log_path = log_path
        self.kwargs = kwargs
        self.server = _FakeServer(
            serve_behavior=self.serve_behavior,
            host=host,
            port=port,
            local_token=local_token,
            api_key=api_key,
            log_path=log_path,
        )
        return self.server


class _FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.terminate = mock.Mock()
        self.kill = mock.Mock()
        self.poll = mock.Mock(return_value=None)
        self.wait = mock.Mock(return_value=0)


class GatewayLifecycleContractTests(unittest.TestCase):
    """Exactly ten RED behavior tests for the missing gateway lifecycle."""

    def _manager(self):
        return _load_manager()

    def test_fresh_gateway_commands_are_zero_write(self):
        manager = self._manager()
        for name in ("gateway_status", "gateway_start", "gateway_stop", "read_gateway_state"):
            _require_callable(self, manager, name)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            before = _snapshot(home)
            for command in ("status", "start", "stop"):
                with self.subTest(command=command):
                    payload = getattr(manager, "gateway_" + command)(paths)
                    self.assertEqual(payload.get("status"), "gateway_unavailable")
            with self.assertRaises(manager.ManagerError) as cm:
                manager.read_gateway_state(paths)
            self.assertEqual(cm.exception.code, "not_managed")
            self.assertEqual(_all_files(home), [])
            self.assertEqual(_snapshot(home), before)

    def test_authenticated_probe_and_shutdown_use_frozen_server(self):
        manager = self._manager()
        server_module = _load_server()
        for name in ("_gateway_probe", "_gateway_shutdown"):
            _require_callable(self, manager, name)
        server = None
        thread = None
        try:
            server = server_module.create_server(
                "127.0.0.1",
                0,
                LOCAL_TOKEN,
                SECRET_MARKER,
                upstream_base="http://127.0.0.1:9",
                allow_test_http=True,
            )
            port = server.server_address[1]
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            thread.start()
            ready = False
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if manager._gateway_probe(port, LOCAL_TOKEN, timeout=1.0):
                    ready = True
                    break
                time.sleep(0.05)
            self.assertTrue(ready, "authenticated probe never became ready")
            self.assertFalse(
                manager._gateway_probe(port, "wrong-" + LOCAL_TOKEN, timeout=1.0)
            )
            self.assertTrue(manager._gateway_shutdown(port, LOCAL_TOKEN, timeout=2.0))
            thread.join(timeout=5.0)
            self.assertFalse(thread.is_alive(), "server thread did not stop after shutdown")
        finally:
            if server is not None:
                try:
                    server.shutdown()
                except Exception:
                    pass
                server.server_close()
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)

    def test_read_gateway_state_and_status_are_strict_and_secret_free(self):
        manager = self._manager()
        for field in ("gateway_runtime", "gateway_log"):
            self.assertIn(field, manager.Paths.__dataclass_fields__)
        for name in ("read_gateway_state", "gateway_status"):
            _require_callable(self, manager, name)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertIn("version", state)
            self.assertIn("local_gateway_token", state)
            self.assertIn("port", state)
            parsed = manager.read_gateway_state(paths)
            self.assertEqual(parsed["version"], state["version"])
            self.assertEqual(parsed["local_gateway_token"], state["local_gateway_token"])
            self.assertEqual(parsed["port"], state["port"])
            paths.gateway_runtime.parent.mkdir(parents=True, exist_ok=True)
            paths.gateway_runtime.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pid": 123456,
                        "port": state["port"],
                        "started_at": "2026-08-11T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            try:
                os.chmod(paths.gateway_runtime, 0o600)
            except OSError:
                pass
            with mock.patch.object(manager, "_gateway_probe", return_value=False) as probe_mock:
                status_payload = manager.gateway_status(paths)
            serialized = json.dumps(status_payload, ensure_ascii=False)
            self.assertNotIn(state["local_gateway_token"], serialized)
            self.assertNotIn(SECRET_MARKER, serialized)
            self.assertIs(status_payload.get("running"), False)
            probe_mock.assert_called_once()
            paths.state.write_text(
                paths.state.read_text(encoding="utf-8") + "\n", encoding="utf-8"
            )
            after_tamper = _snapshot(paths.home)
            for fn_name in ("read_gateway_state", "gateway_status"):
                with self.subTest(fn=fn_name):
                    with self.assertRaises(manager.ManagerError) as cm:
                        getattr(manager, fn_name)(paths)
                    self.assertEqual(cm.exception.code, "conflict")
            self.assertEqual(_snapshot(paths.home), after_tamper)

    def test_spawn_command_is_hidden_and_secret_free(self):
        manager = self._manager()
        _require_callable(self, manager, "_spawn_gateway_process")
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            fake = _FakeProcess(pid=424242)
            with mock.patch.object(manager.subprocess, "Popen", return_value=fake) as popen_mock:
                if os.name == "nt":
                    result = manager._spawn_gateway_process(paths)
                    self.assertIs(result, fake)
                    popen_mock.assert_called_once()
                    call = popen_mock.call_args
                    argv = _popen_value(call, "args")
                    self.assertEqual(
                        argv,
                        [
                            sys.executable,
                            manager.__file__,
                            "_gateway-serve",
                            "--codex-home",
                            str(paths.home),
                        ],
                    )
                    joined = " ".join(argv)
                    self.assertNotIn(LOCAL_TOKEN, joined)
                    self.assertNotIn(SECRET_MARKER, joined)
                    self.assertNotIn(state["local_gateway_token"], joined)
                    self.assertIs(_popen_value(call, "stdin"), manager.subprocess.DEVNULL)
                    self.assertIs(_popen_value(call, "stdout"), manager.subprocess.DEVNULL)
                    self.assertIs(_popen_value(call, "stderr"), manager.subprocess.DEVNULL)
                    self.assertIs(_popen_value(call, "close_fds"), True)
                    self.assertIsNone(_popen_value(call, "env"))
                    create_no_window = getattr(manager.subprocess, "CREATE_NO_WINDOW", None)
                    if create_no_window is not None:
                        self.assertTrue(
                            _popen_value(call, "creationflags", 0) & create_no_window
                        )
                    startupinfo = _popen_value(call, "startupinfo")
                    if startupinfo is not None:
                        startf_use_show_window = getattr(
                            manager.subprocess, "STARTF_USESHOWWINDOW", 1
                        )
                        self.assertTrue(startupinfo.dwFlags & startf_use_show_window)
                        self.assertEqual(
                            startupinfo.wShowWindow,
                            getattr(manager.subprocess, "SW_HIDE", 0),
                        )
                elif sys.platform == "darwin":
                    result = manager._spawn_gateway_process(paths)
                    self.assertIs(result, fake)
                    popen_mock.assert_called_once()
                    call = popen_mock.call_args
                    argv = _popen_value(call, "args")
                    self.assertEqual(
                        argv,
                        [
                            sys.executable,
                            manager.__file__,
                            "_gateway-serve",
                            "--codex-home",
                            str(paths.home),
                        ],
                    )
                    joined = " ".join(argv)
                    self.assertNotIn(LOCAL_TOKEN, joined)
                    self.assertNotIn(SECRET_MARKER, joined)
                    self.assertNotIn(state["local_gateway_token"], joined)
                    self.assertIs(_popen_value(call, "stdin"), manager.subprocess.DEVNULL)
                    self.assertIs(_popen_value(call, "stdout"), manager.subprocess.DEVNULL)
                    self.assertIs(_popen_value(call, "stderr"), manager.subprocess.DEVNULL)
                    self.assertIs(_popen_value(call, "close_fds"), True)
                    self.assertIsNone(_popen_value(call, "env"))
                    self.assertIs(_popen_value(call, "start_new_session"), True)
                else:
                    with self.assertRaises(manager.ManagerError) as cm:
                        manager._spawn_gateway_process(paths)
                    self.assertEqual(cm.exception.code, "unsupported")

    def test_gateway_start_is_idempotent_and_failure_sanitized(self):
        manager = self._manager()
        for name in ("gateway_start", "_gateway_probe", "_spawn_gateway_process"):
            _require_callable(self, manager, name)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            fake = _FakeProcess(pid=31337)
            with mock.patch.object(manager, "credential_has_key", return_value=True), mock.patch.object(
                manager, "_gateway_probe", side_effect=[False, True, True]
            ), mock.patch.object(manager, "_spawn_gateway_process", return_value=fake) as spawn_mock:
                first = manager.gateway_start(paths, wait_seconds=5.0)
                self.assertIn("status", first)
                self.assertIs(first.get("running"), True)
                self.assertIs(first.get("changed"), True)
                second = manager.gateway_start(paths, wait_seconds=5.0)
                self.assertIs(second.get("running"), True)
                self.assertIs(second.get("changed"), False)
                spawn_mock.assert_called_once()
                fake.terminate.assert_not_called()
                fake.kill.assert_not_called()
            before = _snapshot(paths.home)
            with mock.patch.object(manager, "credential_has_key", return_value=True), mock.patch.object(
                manager, "_gateway_probe", return_value=False
            ), mock.patch.object(
                manager, "_spawn_gateway_process", side_effect=OSError(SECRET_MARKER)
            ):
                with self.assertRaises(manager.ManagerError) as cm:
                    manager.gateway_start(paths, wait_seconds=0.2)
            self.assertEqual(cm.exception.code, "gateway_start_failed")
            self.assertNotIn(SECRET_MARKER, str(cm.exception))
            details = json.dumps(cm.exception.details or {}, ensure_ascii=False)
            self.assertNotIn(SECRET_MARKER, details)
            self.assertEqual(_snapshot(paths.home), before)
            self.assertFalse(paths.gateway_runtime.exists())
            timeout_fake = _FakeProcess(pid=51515)
            timeout_fake.wait.side_effect = subprocess.TimeoutExpired(
                cmd="gateway", timeout=1.0
            )
            with mock.patch.object(manager, "credential_has_key", return_value=True), mock.patch.object(
                manager, "_gateway_probe", return_value=False
            ), mock.patch.object(
                manager, "_spawn_gateway_process", return_value=timeout_fake
            ), mock.patch.object(manager.os, "kill") as kill_mock:
                with self.assertRaises(manager.ManagerError) as cm:
                    manager.gateway_start(paths, wait_seconds=0.01)
            self.assertEqual(cm.exception.code, "gateway_start_failed")
            timeout_fake.terminate.assert_called_once()
            timeout_fake.wait.assert_called_once_with(timeout=1.0)
            kill_mock.assert_not_called()
            timeout_fake.kill.assert_called_once()

    def test_gateway_stop_uses_authenticated_http_not_persisted_pid(self):
        manager = self._manager()
        for name in ("gateway_stop", "_gateway_probe", "_gateway_shutdown"):
            _require_callable(self, manager, name)

        def write_runtime(paths, state, pid):
            paths.gateway_runtime.parent.mkdir(parents=True, exist_ok=True)
            paths.gateway_runtime.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pid": pid,
                        "port": state["port"],
                        "started_at": "2026-08-11T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            try:
                os.chmod(paths.gateway_runtime, 0o600)
            except OSError:
                pass

        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            write_runtime(paths, state, 999991)
            with mock.patch.object(
                manager, "_gateway_probe", side_effect=[True, False]
            ), mock.patch.object(manager, "_gateway_shutdown", return_value=True) as shutdown_mock, mock.patch.object(
                manager.os, "kill"
            ) as kill_mock, mock.patch.object(manager.subprocess, "Popen") as popen_mock:
                stopped = manager.gateway_stop(paths, wait_seconds=5.0)
            self.assertIs(stopped.get("running"), False)
            self.assertIs(stopped.get("changed"), True)
            self.assertFalse(paths.gateway_runtime.exists())
            shutdown_mock.assert_called_once()
            kill_mock.assert_not_called()
            popen_mock.assert_not_called()
            write_runtime(paths, state, 999992)
            with mock.patch.object(
                manager, "_gateway_probe", return_value=False
            ), mock.patch.object(manager, "_gateway_shutdown") as shutdown_mock2, mock.patch.object(
                manager.os, "kill"
            ) as kill_mock2:
                stale = manager.gateway_stop(paths, wait_seconds=1.0)
            self.assertFalse(paths.gateway_runtime.exists())
            shutdown_mock2.assert_not_called()
            kill_mock2.assert_not_called()
            write_runtime(paths, state, 999993)
            before_failure = _snapshot(paths.home)
            with mock.patch.object(
                manager, "_gateway_probe", return_value=True
            ), mock.patch.object(manager, "_gateway_shutdown", return_value=False), mock.patch.object(
                manager.os, "kill"
            ) as kill_mock3:
                with self.assertRaises(manager.ManagerError) as cm:
                    manager.gateway_stop(paths, wait_seconds=0.2)
            self.assertEqual(cm.exception.code, "gateway_stop_failed")
            self.assertTrue(paths.gateway_runtime.is_file())
            kill_mock3.assert_not_called()
            self.assertEqual(_snapshot(paths.home), before_failure)

    def test_gateway_serve_reads_secret_in_memory_and_cleans_own_runtime(self):
        manager = self._manager()
        _require_callable(self, manager, "gateway_serve")
        for field in ("gateway_runtime", "gateway_log"):
            self.assertIn(field, manager.Paths.__dataclass_fields__)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            token = state["local_gateway_token"]
            port = state["port"]

            def on_serve(fake_server):
                runtime = json.loads(paths.gateway_runtime.read_text(encoding="utf-8"))
                self.assertEqual(set(runtime.keys()), {"version", "pid", "port", "started_at"})
                self.assertNotIn(SECRET_MARKER, json.dumps(runtime))
                self.assertNotIn(token, json.dumps(runtime))

            factory = _FakeServerFactory(serve_behavior=on_serve)
            with mock.patch.object(manager, "read_credential_key", return_value=SECRET_MARKER) as read_mock:
                exit_code = manager.gateway_serve(paths, server_factory=factory)
            self.assertEqual(exit_code, 0)
            self.assertEqual(factory.host, "127.0.0.1")
            self.assertEqual(factory.port, port)
            self.assertEqual(factory.local_token, token)
            self.assertEqual(factory.api_key, SECRET_MARKER)
            self.assertEqual(factory.log_path, str(paths.gateway_log))
            read_mock.assert_called_once()
            self.assertTrue(factory.server.closed)
            self.assertFalse(paths.gateway_runtime.exists())
            foreign_pid = os.getpid() + 987654321

            def rewrite_foreign(fake_server):
                paths.gateway_runtime.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "pid": foreign_pid,
                            "port": port,
                            "started_at": "2026-08-11T00:00:00Z",
                        }
                    ),
                    encoding="utf-8",
                )

            factory2 = _FakeServerFactory(serve_behavior=rewrite_foreign)
            with mock.patch.object(manager, "read_credential_key", return_value=SECRET_MARKER):
                exit_code2 = manager.gateway_serve(paths, server_factory=factory2)
            self.assertEqual(exit_code2, 0)
            self.assertTrue(paths.gateway_runtime.is_file())
            self.assertEqual(
                json.loads(paths.gateway_runtime.read_text(encoding="utf-8"))["pid"],
                foreign_pid,
            )
            secrets_found = []
            for path in sorted(paths.home.rglob("*")):
                if path.is_file() and SECRET_MARKER.encode("utf-8") in path.read_bytes():
                    secrets_found.append(str(path))
            self.assertEqual(secrets_found, [])

    def test_gateway_token_cli_outputs_only_token_and_sanitizes_failure(self):
        manager = self._manager()
        _require_callable(self, manager, "ensure_gateway")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = _configure_home(manager, home)
            old_token = json.loads(paths.state.read_text(encoding="utf-8"))[
                "local_gateway_token"
            ]
            new_token = "red-test-refreshed-token-" + "0" * 20
            self.assertNotEqual(new_token, old_token)

            def refresh_token(paths_arg, **kwargs):
                state = json.loads(paths_arg.state.read_text(encoding="utf-8"))
                state["local_gateway_token"] = new_token
                state_bytes = (
                    json.dumps(state, ensure_ascii=False, indent=2) + "\n"
                ).encode()
                paths_arg.state.write_bytes(state_bytes)
                manifest = manager.read_manifest(paths_arg)
                manifest["state_sha256"] = manager.sha256_bytes(state_bytes)
                manager.write_manifest(paths_arg, manifest)
                return {"running": True}

            with mock.patch.object(manager, "gateway_start", side_effect=refresh_token):
                fresh_token = manager.ensure_gateway(paths)
            self.assertEqual(fresh_token, new_token)
            with mock.patch.object(
                manager, "ensure_gateway", create=True, return_value=LOCAL_TOKEN
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = manager.main(
                        ["_gateway-token", "--codex-home", str(home)]
                    )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), LOCAL_TOKEN)
            self.assertEqual(len(stdout.getvalue().split()), 1)
            self.assertEqual(stderr.getvalue(), "")
            with mock.patch.object(
                manager,
                "ensure_gateway",
                create=True,
                side_effect=manager.ManagerError("gateway_start_failed", SECRET_MARKER),
            ):
                stdout2 = io.StringIO()
                stderr2 = io.StringIO()
                with redirect_stdout(stdout2), redirect_stderr(stderr2):
                    exit_code2 = manager.main(
                        ["_gateway-token", "--codex-home", str(home)]
                    )
            self.assertEqual(exit_code2, 2)
            self.assertEqual(stdout2.getvalue(), "")
            self.assertIn("gateway_token_failed", stderr2.getvalue())
            self.assertIn("gateway_start_failed", stderr2.getvalue())
            self.assertNotIn(SECRET_MARKER, stderr2.getvalue())
            self.assertNotIn(LOCAL_TOKEN, stderr2.getvalue())

    def test_public_gateway_cli_dispatches_without_writes_when_fresh(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = manager.main(
                    ["gateway", "status", "--json", "--codex-home", str(home)]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertEqual(payload.get("status"), "gateway_unavailable")
            for command in ("start", "stop"):
                with self.subTest(command=command, mode="fresh"):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = manager.main(
                            ["gateway", command, "--json", "--codex-home", str(home)]
                        )
                    payload = json.loads(output.getvalue())
                    self.assertEqual(exit_code, 2)
                    self.assertEqual(payload.get("status"), "gateway_unavailable")
            self.assertEqual(_all_files(home), [])
            paths = _configure_home(manager, home)
            fakes = {
                "status": {"status": "ok", "running": False},
                "start": {"status": "ok", "running": True, "changed": True},
                "stop": {"status": "ok", "running": False, "changed": True},
            }
            with mock.patch.object(
                manager, "gateway_status", create=True, return_value=fakes["status"]
            ) as status_mock, mock.patch.object(
                manager, "gateway_start", create=True, return_value=fakes["start"]
            ) as start_mock, mock.patch.object(
                manager, "gateway_stop", create=True, return_value=fakes["stop"]
            ) as stop_mock:
                for command, fn_mock in (
                    ("status", status_mock),
                    ("start", start_mock),
                    ("stop", stop_mock),
                ):
                    with self.subTest(command=command, mode="configured"):
                        output2 = io.StringIO()
                        with redirect_stdout(output2):
                            exit_code = manager.main(
                                ["gateway", command, "--json", "--codex-home", str(home)]
                            )
                        payload2 = json.loads(output2.getvalue())
                        self.assertEqual(exit_code, 0)
                        self.assertEqual(payload2["status"], fakes[command]["status"])
                        fn_mock.assert_called_once()
                        serialized = json.dumps(payload2, ensure_ascii=False)
                        self.assertNotIn(SECRET_MARKER, serialized)
                        self.assertNotIn(LOCAL_TOKEN, serialized)

    def test_disable_and_uninstall_stop_first_and_abort_on_failure(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            order = []

            def stop_side_effect(p):
                self.assertTrue(paths.agent.is_file(), "stop must precede agent removal")
                self.assertTrue(paths.state.is_file(), "stop must precede state removal")
                order.append("disable-stop")

            with mock.patch.object(
                manager, "gateway_stop", create=True, side_effect=stop_side_effect
            ) as stop_mock, mock.patch.object(manager, "credential_has_key", return_value=True):
                disabled = manager.disable(paths)
            self.assertEqual(disabled.get("status"), "disabled")
            self.assertFalse(paths.agent.exists())
            self.assertEqual(order, ["disable-stop"])
            stop_mock.assert_called_once()
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            order2 = []
            with mock.patch.object(
                manager,
                "gateway_stop",
                create=True,
                side_effect=lambda p: order2.append("uninstall-stop"),
            ) as stop_mock2, mock.patch.object(
                manager, "remove_credential_key", return_value=False
            ) as remove_mock2, mock.patch.object(manager, "credential_has_key", return_value=True):
                uninstalled = manager.uninstall(paths, remove_credential=False)
            self.assertEqual(uninstalled.get("status"), "uninstalled")
            self.assertFalse(paths.state.exists())
            self.assertFalse(paths.manifest.exists())
            self.assertEqual(order2, ["uninstall-stop"])
            stop_mock2.assert_called_once()
            remove_mock2.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            with mock.patch.object(
                manager, "gateway_stop", create=True
            ) as stop_mock3, mock.patch.object(
                manager, "remove_credential_key", return_value=True
            ) as remove_mock3, mock.patch.object(manager, "credential_has_key", return_value=True):
                uninstalled = manager.uninstall(paths, remove_credential=True)
            self.assertEqual(uninstalled.get("status"), "uninstalled")
            self.assertFalse(paths.state.exists())
            stop_mock3.assert_called_once()
            remove_mock3.assert_called_once()
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            before = _snapshot(paths.home)
            with mock.patch.object(
                manager,
                "gateway_stop",
                create=True,
                side_effect=manager.ManagerError("gateway_stop_failed", "boom"),
            ), mock.patch.object(manager, "credential_has_key", return_value=True), mock.patch.object(
                manager, "remove_credential_key"
            ) as remove_mock4:
                with self.assertRaises(manager.ManagerError) as cm:
                    manager.disable(paths)
            self.assertEqual(cm.exception.code, "gateway_stop_failed")
            self.assertEqual(_snapshot(paths.home), before)
            remove_mock4.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            before = _snapshot(paths.home)
            with mock.patch.object(
                manager,
                "gateway_stop",
                create=True,
                side_effect=manager.ManagerError(
                    "gateway_stop_failed", "uninstall stop failed"
                ),
            ), mock.patch.object(manager, "credential_has_key", return_value=True), mock.patch.object(
                manager, "remove_credential_key"
            ) as remove_mock5:
                with self.assertRaises(manager.ManagerError) as cm:
                    manager.uninstall(paths, remove_credential=True)
            self.assertEqual(cm.exception.code, "gateway_stop_failed")
            self.assertEqual(_snapshot(paths.home), before)
            remove_mock5.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
