#!/usr/bin/env python3
"""RED behavior contract tests for the managed models refresh transaction
(DS-20260811-21 REV-2).

The production manager does not yet expose models_refresh or the private
_gateway_stop_locked helper, so every test FAILs (never ERRORs) on a fixed
missing-callable assertion.  Tests use temporary homes, in-memory fake
payloads and mocked credentials; they touch no network, API key, gateway
process, source-text scanning or mtime.
"""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.test_gateway_manager import _configure_home
from tests.test_manager import _load_manager, _snapshot
from tests.test_models_refresh import (
    _go_payload,
    _metadata_for,
    _models_dev_payload,
)

SECRET_MARKER = "sk-" + "opencode-red-refresh-secret-marker"
REPORT_KEYS = frozenset(
    {
        "unknown_go_models",
        "unknown_metadata_models",
        "missing_go_models",
        "conflicts",
        "updated_models",
    }
)
CHANGED_MANIFEST_KEYS = frozenset({"state_sha256", "catalog_sha256", "backup"})
TRANSACTION_FAILED_MESSAGE = "models refresh failed and managed files were restored"


def _require_callable(testcase, manager, name):
    if not callable(getattr(manager, name, None)):
        testcase.fail(f"RED: manager.{name} missing (DS-20260811-21 REV-2)")


def _changed_payload(models):
    go = _go_payload(models, missing=("hy3",), extra=("unknown-go-red",))
    metadata = _models_dev_payload(models, extra=("unknown-meta-red",))
    metadata["opencode-go"]["models"]["gpt-5.6-luna"] = _metadata_for(
        models, "gpt-5.6-luna", provider={"npm": "@ai-sdk/not-openai"}
    )
    return go, metadata


def _raise_secret():
    raise RuntimeError(SECRET_MARKER)


def _fixed_fetcher(payload):
    return lambda: payload


@contextmanager
def _block_credentials(manager):
    names = (
        "credential_available",
        "credential_has_key",
        "read_credential_key",
        "store_credential_key",
        "remove_credential_key",
    )
    with mock.patch.multiple(
        manager,
        **{
            name: mock.Mock(
                side_effect=AssertionError(
                    f"credential function {name} called during models refresh "
                    "(DS-20260811-21 REV-2)"
                )
            )
            for name in names
        },
    ):
        yield


def _lock_tracker(manager):
    state = {"depth": 0, "max": 0}
    original = manager.operation_lock

    @contextmanager
    def wrapper(paths, timeout_seconds=manager.LOCK_WAIT_SECONDS):
        state["depth"] += 1
        state["max"] = max(state["max"], state["depth"])
        try:
            with original(paths, timeout_seconds=timeout_seconds) as lock:
                yield lock
        finally:
            state["depth"] -= 1

    return wrapper, state


class ManagerRefreshTransactionRedTests(unittest.TestCase):
    """Eight RED behavior tests for the missing models refresh transaction."""

    def _manager(self):
        return _load_manager()

    def test_changed_refresh_transaction_order_report_catalog_and_secrets(self):
        manager = self._manager()
        _require_callable(self, manager, "models_refresh")
        models = manager._models_module()
        go, metadata = _changed_payload(models)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            config_before = paths.config.read_bytes()
            agent_before = paths.agent.read_bytes()
            state_before = json.loads(paths.state.read_text(encoding="utf-8"))
            manifest_before = manager.read_manifest(paths)
            token = state_before["local_gateway_token"]
            port = state_before["port"]
            version = state_before["version"]
            builtin_models = copy.deepcopy(models.MODELS)
            payload_before = copy.deepcopy((go, metadata))

            order = []
            lock_wrapper, lock_state = _lock_tracker(manager)
            original_backup = manager.make_backup
            original_atomic_write = manager.atomic_write
            target_labels = {
                "state.json": "state",
                "models-opencode-go.json": "catalog",
                "manifest.json": "manifest",
            }

            def stop_locked(call_paths, call_state, wait):
                self.assertEqual(lock_state["depth"], 1)
                self.assertEqual(
                    set(call_state),
                    {"version", "local_gateway_token", "port"},
                )
                self.assertEqual(wait, manager.GATEWAY_STOP_WAIT_SECONDS)
                order.append("stop")
                return {"status": "ok", "running": False, "changed": True}, True

            def tracked_backup(call_paths):
                order.append("backup")
                return original_backup(call_paths)

            def tracked_atomic_write(path, data, mode=0o600):
                order.append(target_labels.get(path.name, path.name))
                return original_atomic_write(path, data, mode)

            with mock.patch.object(
                manager, "operation_lock", side_effect=lock_wrapper
            ), mock.patch.object(
                manager, "make_backup", side_effect=tracked_backup
            ), mock.patch.object(
                manager, "atomic_write", side_effect=tracked_atomic_write
            ), _block_credentials(manager):
                result = manager.models_refresh(
                    paths, fetcher=lambda: (go, metadata), stop_locked=stop_locked
                )

            self.assertEqual(order, ["stop", "backup", "state", "catalog", "manifest"])
            self.assertEqual(lock_state["max"], 1)
            self.assertEqual(result["status"], "ok")
            self.assertIs(result["changed"], True)
            self.assertEqual(
                result["active_profile"],
                {"model": "deepseek-v4-flash", "effort": "max"},
            )
            self.assertEqual(set(result["report"]), REPORT_KEYS)
            self.assertTrue(result["backup"])
            for key in (
                "gateway_stopped",
                "gateway_restart_required",
                "new_task_required",
            ):
                self.assertIs(result[key], True)

            state_after = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertEqual(state_after["version"], version)
            self.assertEqual(state_after["local_gateway_token"], token)
            self.assertEqual(state_after["port"], port)
            self.assertNotEqual(
                state_after["model_registry"], state_before["model_registry"]
            )
            registry_after = models.registry_from_snapshot(state_after["model_registry"])
            self.assertEqual(len(registry_after), 19)
            self.assertEqual(registry_after["hy3"].status, "unavailable")
            self.assertNotIn("unknown-go-red", registry_after)
            self.assertNotIn("unknown-meta-red", registry_after)

            catalog_after = json.loads(paths.catalog.read_text(encoding="utf-8"))
            slugs = {item.get("slug") for item in catalog_after["models"]}
            self.assertNotIn("hy3", slugs)
            self.assertNotIn("unknown-go-red", slugs)
            self.assertNotIn("unknown-meta-red", slugs)
            self.assertIn("deepseek-v4-flash", slugs)
            gpt_old = builtin_models["gpt-5.6-luna"]
            gpt_new = registry_after["gpt-5.6-luna"]
            self.assertEqual(
                (gpt_new.context_window, gpt_new.max_output),
                (gpt_old.context_window, gpt_old.max_output),
            )
            flash_old = builtin_models["deepseek-v4-flash"]
            flash_new = registry_after["deepseek-v4-flash"]
            self.assertGreater(flash_new.context_window, flash_old.context_window)
            self.assertGreater(flash_new.max_output, flash_old.max_output)

            manifest_after = manager.read_manifest(paths)
            for key in set(manifest_before) | set(manifest_after):
                if key not in CHANGED_MANIFEST_KEYS:
                    self.assertEqual(manifest_after.get(key), manifest_before.get(key), key)
            self.assertNotEqual(
                manifest_after["state_sha256"], manifest_before["state_sha256"]
            )
            self.assertNotEqual(
                manifest_after["catalog_sha256"], manifest_before["catalog_sha256"]
            )
            self.assertTrue(manifest_after["backup"])
            self.assertNotEqual(manifest_after["backup"], manifest_before["backup"])
            self.assertEqual(paths.config.read_bytes(), config_before)
            self.assertEqual(paths.agent.read_bytes(), agent_before)
            self.assertEqual(copy.deepcopy(models.MODELS), builtin_models)
            self.assertEqual(copy.deepcopy((go, metadata)), payload_before)
            for payload_text in (
                json.dumps(result, ensure_ascii=False),
                json.dumps(manifest_after, ensure_ascii=False),
                json.dumps(catalog_after, ensure_ascii=False),
            ):
                self.assertNotIn(token, payload_text)
                self.assertNotIn(SECRET_MARKER, payload_text)

    def test_pre_stop_failures_are_zero_write_and_secret_free(self):
        manager = self._manager()
        _require_callable(self, manager, "models_refresh")
        models = manager._models_module()
        go, metadata = _changed_payload(models)
        go_no_flash = _go_payload(
            models, missing=("deepseek-v4-flash",), extra=("unknown-go-red",)
        )
        cases = (
            (
                "selected_missing",
                _fixed_fetcher((go_no_flash, metadata)),
                "selected_model_missing",
            ),
            ("fetcher_raises", _raise_secret, "refresh_payload_invalid"),
            (
                "bad_shape",
                _fixed_fetcher(
                    ({"object": "not-list"}, {"opencode-go": {"models": {}}})
                ),
                "refresh_payload_invalid",
            ),
            ("catalog_drift", _fixed_fetcher((go, metadata)), "conflict"),
        )
        for name, make_fetcher, expected_code in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    paths = _configure_home(manager, Path(directory))
                    if name == "catalog_drift":
                        paths.catalog.write_bytes(paths.catalog.read_bytes() + b" ")
                    home_before = _snapshot(paths.home)
                    backups_before = _snapshot(paths.backups)
                    stop = mock.Mock()
                    fetcher_calls = []

                    def fetcher():
                        fetcher_calls.append(1)
                        return make_fetcher()

                    with mock.patch.object(
                        manager,
                        "make_backup",
                        side_effect=AssertionError("make_backup called before stop"),
                    ), mock.patch.object(
                        manager,
                        "atomic_write",
                        side_effect=AssertionError("atomic_write called before stop"),
                    ), _block_credentials(manager):
                        with self.assertRaises(manager.ManagerError) as cm:
                            manager.models_refresh(
                                paths, fetcher=fetcher, stop_locked=stop
                            )
                    self.assertEqual(cm.exception.code, expected_code)
                    self.assertNotIn(SECRET_MARKER, str(cm.exception))
                    details = json.dumps(
                        cm.exception.details or {}, ensure_ascii=False
                    )
                    self.assertNotIn(SECRET_MARKER, details)
                    self.assertEqual(_snapshot(paths.home), home_before)
                    self.assertEqual(_snapshot(paths.backups), backups_before)
                    stop.assert_not_called()
                    if name == "catalog_drift":
                        self.assertEqual(fetcher_calls, [])

    def test_conflicts_and_unknowns_are_report_only(self):
        manager = self._manager()
        _require_callable(self, manager, "models_refresh")
        models = manager._models_module()
        go, metadata = _changed_payload(models)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            old_registry = manager.read_model_registry(paths)
            gpt_old = old_registry["gpt-5.6-luna"]

            def stop_locked(call_paths, call_state, wait):
                return {"status": "ok", "running": False, "changed": True}, False

            with _block_credentials(manager):
                result = manager.models_refresh(
                    paths, fetcher=lambda: (go, metadata), stop_locked=stop_locked
                )
            self.assertEqual(result["status"], "ok")
            self.assertIs(result["changed"], True)
            report = result["report"]
            self.assertEqual(report["unknown_go_models"], ["unknown-go-red"])
            self.assertEqual(report["unknown_metadata_models"], ["unknown-meta-red"])
            self.assertIn("hy3", report["missing_go_models"])
            self.assertIn(
                {"model": "gpt-5.6-luna", "code": "transport_conflict"},
                report["conflicts"],
            )
            self.assertTrue(report["updated_models"])
            self.assertEqual(sorted(report["updated_models"]), report["updated_models"])
            self.assertIs(result["gateway_stopped"], False)
            self.assertIs(result["gateway_restart_required"], False)
            self.assertIs(result["new_task_required"], True)
            registry = manager.read_model_registry(paths)
            self.assertEqual(len(registry), 19)
            self.assertNotIn("unknown-go-red", registry)
            self.assertNotIn("unknown-meta-red", registry)
            self.assertEqual(registry["gpt-5.6-luna"], gpt_old)
            self.assertNotEqual(
                registry["deepseek-v4-flash"], old_registry["deepseek-v4-flash"]
            )

    def test_second_identical_refresh_is_zero_write(self):
        manager = self._manager()
        _require_callable(self, manager, "models_refresh")
        models = manager._models_module()
        go, metadata = _changed_payload(models)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))

            def stop_locked(call_paths, call_state, wait):
                return {"status": "ok", "running": False, "changed": True}, True

            with _block_credentials(manager):
                first = manager.models_refresh(
                    paths, fetcher=lambda: (go, metadata), stop_locked=stop_locked
                )
            self.assertIs(first["changed"], True)
            home_before = _snapshot(paths.home)
            backups_before = _snapshot(paths.backups)
            stop = mock.Mock()
            with mock.patch.object(
                manager,
                "make_backup",
                side_effect=AssertionError("make_backup called on unchanged refresh"),
            ), mock.patch.object(
                manager,
                "atomic_write",
                side_effect=AssertionError("atomic_write called on unchanged refresh"),
            ), _block_credentials(manager):
                second = manager.models_refresh(
                    paths, fetcher=lambda: (go, metadata), stop_locked=stop
                )
            self.assertEqual(second["status"], "ok")
            self.assertIs(second["changed"], False)
            self.assertIsNone(second["backup"])
            self.assertIs(second["gateway_stopped"], False)
            self.assertIs(second["gateway_restart_required"], False)
            self.assertIs(second["new_task_required"], False)
            self.assertEqual(second["report"]["updated_models"], [])
            self.assertEqual(_snapshot(paths.home), home_before)
            self.assertEqual(_snapshot(paths.backups), backups_before)
            stop.assert_not_called()

    def test_gateway_stop_failure_precedes_backup_and_writes(self):
        manager = self._manager()
        _require_callable(self, manager, "models_refresh")
        models = manager._models_module()
        go, metadata = _changed_payload(models)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))

            def stop_locked(call_paths, call_state, wait):
                raise manager.ManagerError("gateway_stop_failed", "cannot stop gateway")

            home_before = _snapshot(paths.home)
            backups_before = _snapshot(paths.backups)
            with mock.patch.object(
                manager,
                "make_backup",
                side_effect=AssertionError("make_backup called after stop failure"),
            ), mock.patch.object(
                manager,
                "atomic_write",
                side_effect=AssertionError("atomic_write called after stop failure"),
            ), _block_credentials(manager):
                with self.assertRaises(manager.ManagerError) as cm:
                    manager.models_refresh(
                        paths, fetcher=lambda: (go, metadata), stop_locked=stop_locked
                    )
            self.assertEqual(cm.exception.code, "gateway_stop_failed")
            self.assertEqual(_snapshot(paths.home), home_before)
            self.assertEqual(_snapshot(paths.backups), backups_before)

    def test_atomic_failure_restores_all_managed_bytes_and_keeps_stopped(self):
        manager = self._manager()
        _require_callable(self, manager, "models_refresh")
        models = manager._models_module()
        go, metadata = _changed_payload(models)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            before = {
                name: getattr(paths, name).read_bytes()
                for name in ("config", "catalog", "agent", "manifest", "state")
            }
            backups_before = _snapshot(paths.backups)
            stopped = {"value": False}
            catalog_failed = {"value": False}
            original_atomic_write = manager.atomic_write

            def failing_atomic_write(path, data, mode=0o600):
                if path.name == "models-opencode-go.json" and not catalog_failed["value"]:
                    catalog_failed["value"] = True
                    raise OSError(SECRET_MARKER)
                return original_atomic_write(path, data, mode)

            def stop_locked(call_paths, call_state, wait):
                stopped["value"] = True
                return {"status": "ok", "running": False, "changed": True}, True

            with mock.patch.object(
                manager, "atomic_write", side_effect=failing_atomic_write
            ), mock.patch.object(
                manager,
                "gateway_start",
                side_effect=AssertionError("gateway_start called during refresh"),
            ), mock.patch.object(
                manager,
                "ensure_gateway",
                side_effect=AssertionError("ensure_gateway called during refresh"),
            ), _block_credentials(manager):
                with self.assertRaises(manager.ManagerError) as cm:
                    manager.models_refresh(
                        paths, fetcher=lambda: (go, metadata), stop_locked=stop_locked
                    )
            self.assertEqual(cm.exception.code, "refresh_transaction_failed")
            self.assertEqual(str(cm.exception), TRANSACTION_FAILED_MESSAGE)
            self.assertNotIn(SECRET_MARKER, str(cm.exception))
            details = json.dumps(cm.exception.details or {}, ensure_ascii=False)
            self.assertNotIn(SECRET_MARKER, details)
            self.assertTrue(stopped["value"])
            for name, expected in before.items():
                self.assertEqual(getattr(paths, name).read_bytes(), expected, name)
            self.assertNotEqual(_snapshot(paths.backups), backups_before)
            registry = manager.read_model_registry(paths)
            manager.get_model("deepseek-v4-flash", "max", registry=registry)

    def test_disabled_refresh_preserves_disabled_state(self):
        manager = self._manager()
        _require_callable(self, manager, "models_refresh")
        models = manager._models_module()
        go, metadata = _changed_payload(models)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            with mock.patch.object(
                manager,
                "gateway_stop",
                return_value={"status": "ok", "running": False, "changed": False},
            ), mock.patch.object(manager, "credential_has_key", return_value=True):
                disabled = manager.disable(paths)
            self.assertEqual(disabled["status"], "disabled")
            self.assertFalse(paths.agent.is_file())
            self.assertIs(manager.read_manifest(paths)["enabled"], False)

            def stop_locked(call_paths, call_state, wait):
                return {"status": "ok", "running": False, "changed": True}, False

            with _block_credentials(manager):
                result = manager.models_refresh(
                    paths, fetcher=lambda: (go, metadata), stop_locked=stop_locked
                )
            self.assertEqual(result["status"], "ok")
            self.assertIs(result["changed"], True)
            self.assertIs(result["new_task_required"], True)
            manifest_after = manager.read_manifest(paths)
            self.assertIs(manifest_after["enabled"], False)
            self.assertFalse(paths.agent.is_file())
            parsed = manager.parse_toml_text(paths.config.read_text(encoding="utf-8"))
            self.assertNotIn("OpenCode", parsed.get("agents") or {})
            manager._verify_managed_hashes(paths, manifest_after)
            registry = manager.read_model_registry(paths)
            manager.get_model("deepseek-v4-flash", "max", registry=registry)
            self.assertEqual(manager.status(paths)["status"], "disabled")

    def test_locked_helper_public_gateway_stop_and_cli_json_regressions(self):
        manager = self._manager()
        _require_callable(self, manager, "models_refresh")
        _require_callable(self, manager, "_gateway_stop_locked")
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            state = manager.read_gateway_state(paths)
            lock_wrapper, lock_state = _lock_tracker(manager)
            helper = mock.Mock(
                return_value=(
                    {"status": "ok", "running": False, "changed": True},
                    True,
                )
            )
            with mock.patch.object(
                manager, "operation_lock", side_effect=lock_wrapper
            ), mock.patch.object(manager, "_gateway_stop_locked", helper):
                public = manager.gateway_stop(paths, wait_seconds=3.0)
            self.assertEqual(
                public, {"status": "ok", "running": False, "changed": True}
            )
            self.assertNotIsInstance(public, tuple)
            self.assertEqual(helper.call_count, 1)
            self.assertEqual(helper.call_args.args, (paths, state, 3.0))
            self.assertEqual(helper.call_args.kwargs, {})
            self.assertEqual(lock_state["max"], 1)

            fake_refresh = {
                "status": "ok",
                "changed": False,
                "active_profile": {"model": "deepseek-v4-flash", "effort": "max"},
                "report": {
                    "unknown_go_models": [],
                    "unknown_metadata_models": [],
                    "missing_go_models": [],
                    "conflicts": [],
                    "updated_models": [],
                },
                "backup": None,
                "gateway_stopped": False,
                "gateway_restart_required": False,
                "new_task_required": False,
            }
            refresh_mock = mock.Mock(return_value=fake_refresh)
            out = io.StringIO()
            with mock.patch.object(manager, "models_refresh", refresh_mock), _block_credentials(
                manager
            ), redirect_stdout(out):
                exit_code = manager.main(
                    ["models", "refresh", "--codex-home", str(directory), "--json"]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(out.getvalue()), fake_refresh)
            self.assertEqual(refresh_mock.call_count, 1)
            self.assertEqual(refresh_mock.call_args.args, (paths,))
            self.assertEqual(refresh_mock.call_args.kwargs, {})
            out2 = io.StringIO()
            with mock.patch.object(
                manager, "credential_has_key", return_value=False
            ), redirect_stdout(out2):
                exit_code2 = manager.main(
                    ["test", "--codex-home", str(directory), "--json"]
                )
            self.assertEqual(exit_code2, 2)
            self.assertEqual(
                json.loads(out2.getvalue())["status"], "gateway_unavailable"
            )
            self.assertEqual(refresh_mock.call_count, 1)
