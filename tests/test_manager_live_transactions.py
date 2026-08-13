#!/usr/bin/env python3
"""RED behavior contract tests for default live validation transactions
(DS-20260812-23 REV-2).

The production manager does not yet expose _gateway_start_locked or
_validate_profile_locked, and setup/repair/set_profile still skip live
validation by default while returning fixed skipped_live_test=True.  Every
test starts with a unified callable gate on both locked helpers, so the
current module yields FAIL (never ERROR) with the first missing helper
reported.  Tests use temporary homes, mocked credentials/probes/processes,
tracked writes and an in-process lock tracker; no real socket, network,
credential store, gateway child or paid call is used.  They prove
transaction ordering, single-lock, cleanup, skip, idempotent and rollback
contracts only, never real gateway behaviour.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from tests.test_gateway_manager import _catalog_loader, _configure_home
from tests.test_manager import _load_manager, _snapshot

SECRET_MARKER = "live-validator-secret-marker"
LOCAL_TOKEN_MARKER = "live-validator-token-marker"
MANAGED_ORDER = ("state", "catalog", "agent", "config", "manifest")


def _require_live_helpers(testcase, manager):
    for name in ("_gateway_start_locked", "_validate_profile_locked"):
        if not callable(getattr(manager, name, None)):
            testcase.fail("RED: manager.%s missing (DS-20260812-23 REV-2)" % name)


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


def _managed_bytes(paths):
    return {
        "state": paths.state.read_bytes() if paths.state.is_file() else None,
        "catalog": paths.catalog.read_bytes() if paths.catalog.is_file() else None,
        "agent": paths.agent.read_bytes() if paths.agent.is_file() else None,
        "config": paths.config.read_bytes() if paths.config.is_file() else None,
        "manifest": paths.manifest.read_bytes() if paths.manifest.is_file() else None,
    }


def _backups_snapshot(paths):
    return _snapshot(paths.backups) if paths.backups.is_dir() else {}


def _assert_secret_free(testcase, payload, secret=SECRET_MARKER):
    testcase.assertNotIn(secret, json.dumps(payload, ensure_ascii=False))


def _assert_error_secret_free(testcase, exc, secret=SECRET_MARKER):
    serialized = str(exc) + json.dumps(exc.details or {}, ensure_ascii=False)
    testcase.assertNotIn(secret, serialized)


class LiveValidationTransactionRedTests(unittest.TestCase):
    """Seven RED behaviour tests for default live validation transactions."""

    def _manager(self):
        try:
            return _load_manager()
        except FileNotFoundError:
            self.fail("manager_missing")

    def test_gateway_start_locked_helper_extraction_and_public_single_lock(self):
        manager = self._manager()
        _require_live_helpers(self, manager)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            state = manager.read_gateway_state(paths)

            # Direct locked helper with an already-running gateway: no
            # credential lookup and no process spawn; returns the existing
            # public result dict.
            with mock.patch.object(manager, "_gateway_probe", return_value=True) as probe, \
                    mock.patch.object(manager, "credential_has_key") as cred, \
                    mock.patch.object(manager, "_spawn_gateway_process") as spawn, \
                    mock.patch.object(
                        manager, "_safe_gateway_runtime_fields", return_value=None
                    ), \
                        manager.operation_lock(paths):
                payload = manager._gateway_start_locked(paths, state, 5.0)
            self.assertEqual(
                payload,
                {"status": "ok", "running": True, "changed": False,
                 "port": state["port"]},
            )
            probe.assert_called_once_with(
                state["port"], state["local_gateway_token"], timeout=1.0
            )
            cred.assert_not_called()
            spawn.assert_not_called()

            # Public gateway_start: reads state, delegates under exactly one
            # operation lock; the helper never re-acquires the lock itself.
            lock_wrapper, lock_state = _lock_tracker(manager)
            calls = []

            def delegated(call_paths, call_state, wait):
                calls.append((call_paths, call_state, wait))
                self.assertEqual(lock_state["depth"], 1)
                return {"status": "ok", "running": True, "changed": False,
                        "port": state["port"]}

            with mock.patch.object(manager, "operation_lock", side_effect=lock_wrapper), \
                    mock.patch.object(
                        manager, "_gateway_start_locked", side_effect=delegated
                    ):
                public = manager.gateway_start(paths, wait_seconds=5.0)
            self.assertEqual(
                public,
                {"status": "ok", "running": True, "changed": False,
                 "port": state["port"]},
            )
            self.assertEqual(calls, [(paths, state, 5.0)])
            self.assertEqual(lock_state["max"], 1)

    def test_validate_profile_locked_override_and_gateway_cleanup_semantics(self):
        manager = self._manager()
        _require_live_helpers(self, manager)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            state_data = manager._read_managed_state(paths)
            spec = manager.get_model(
                "deepseek-v4-flash", "max", registry=state_data["model_registry"]
            )
            profile = manager.Profile("deepseek-v4-flash", "max")
            expected_safe = {
                "model": "deepseek-v4-flash",
                "effort": "max",
                "transport": spec.transport,
                "text_ok": True,
                "tool_ok": True,
                "usage": 0,
            }

            # Override success: validator receives model/effort exactly once,
            # nothing starts or probes, and the validator's raw output is never
            # adopted (a fixed allowlisted safe dict is returned).
            start = mock.Mock()
            stop = mock.Mock()
            probe = mock.Mock()
            validator_calls = []

            def validator(model, effort):
                validator_calls.append((model, effort))
                return {"raw": SECRET_MARKER, "ok": True}

            with mock.patch.object(manager, "_gateway_start_locked", start), \
                    mock.patch.object(manager, "_gateway_stop_locked", stop), \
                    mock.patch.object(manager, "_live_probe_profile", probe), \
                    manager.operation_lock(paths):
                result = manager._validate_profile_locked(
                    paths, state_data, profile, validator=validator)
            self.assertEqual(validator_calls, [("deepseek-v4-flash", "max")])
            self.assertEqual(result, expected_safe)
            _assert_secret_free(self, result)
            start.assert_not_called()
            stop.assert_not_called()
            probe.assert_not_called()

            # Override exception: fixed safe live_test_failed, no secret.
            start = mock.Mock()
            stop = mock.Mock()
            probe = mock.Mock()

            def exploding(model, effort):
                raise RuntimeError(SECRET_MARKER)

            with mock.patch.object(manager, "_gateway_start_locked", start), \
                    mock.patch.object(manager, "_gateway_stop_locked", stop), \
                    mock.patch.object(manager, "_live_probe_profile", probe), \
                    manager.operation_lock(paths):
                with self.assertRaises(manager.ManagerError) as cm:
                    manager._validate_profile_locked(
                        paths, state_data, profile, validator=exploding)
            self.assertEqual(cm.exception.code, "live_test_failed")
            _assert_error_secret_free(self, cm.exception)
            start.assert_not_called()
            stop.assert_not_called()
            probe.assert_not_called()

        # Default path: a freshly started gateway that fails probing is stopped
        # exactly once under the same single operation lock; an already-running
        # gateway is never stopped; stop-cleanup failures become a fixed
        # live_test_cleanup_failed error.
        for case in ("runtime_error", "manager_error"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as directory:
                    paths = _configure_home(manager, Path(directory))
                    state_data = manager._read_managed_state(paths)
                    gateway_expected = {
                        "version": state_data["version"],
                        "local_gateway_token": state_data["local_gateway_token"],
                        "port": state_data["port"],
                    }
                    lock_wrapper, lock_state = _lock_tracker(manager)
                    start = mock.Mock(return_value={
                        "status": "ok", "running": True, "changed": True,
                        "port": gateway_expected["port"],
                    })
                    stop = mock.Mock(return_value=(
                        {"status": "ok", "running": False, "changed": True},
                        True,
                    ))

                    def failing_probe(gateway_state, spec_obj, effort):
                        if case == "runtime_error":
                            raise RuntimeError(SECRET_MARKER)
                        raise manager.ManagerError("live_test_failed", SECRET_MARKER)

                    with mock.patch.object(
                        manager, "operation_lock", side_effect=lock_wrapper
                    ), mock.patch.object(
                        manager, "read_gateway_state", return_value=gateway_expected
                    ), mock.patch.object(manager, "_gateway_start_locked", start), \
                            mock.patch.object(
                                manager, "_live_probe_profile",
                                side_effect=failing_probe,
                            ), mock.patch.object(manager, "_gateway_stop_locked", stop):
                        with manager.operation_lock(paths):
                            with self.assertRaises(manager.ManagerError) as cm:
                                manager._validate_profile_locked(
                                    paths,
                                    state_data,
                                    manager.Profile("deepseek-v4-flash", "max"),
                                )
                    self.assertEqual(cm.exception.code, "live_test_failed")
                    _assert_error_secret_free(self, cm.exception)
                    self.assertEqual(start.call_count, 1)
                    self.assertEqual(start.call_args.args[:2], (paths, gateway_expected))
                    self.assertTrue(start.call_args.args[2] > 0)
                    stop.assert_called_once_with(
                        paths, gateway_expected, manager.GATEWAY_STOP_WAIT_SECONDS)
                    self.assertEqual(lock_state["max"], 1)

        with self.subTest(case="already_running_no_stop"):
            with tempfile.TemporaryDirectory() as directory:
                paths = _configure_home(manager, Path(directory))
                state_data = manager._read_managed_state(paths)
                gateway_expected = {
                    "version": state_data["version"],
                    "local_gateway_token": state_data["local_gateway_token"],
                    "port": state_data["port"],
                }
                start = mock.Mock(return_value={
                    "status": "ok", "running": True, "changed": False,
                    "port": gateway_expected["port"],
                })
                stop = mock.Mock()

                def failing_probe(gateway_state, spec_obj, effort):
                    raise RuntimeError(SECRET_MARKER)

                with mock.patch.object(
                    manager, "read_gateway_state", return_value=gateway_expected
                ), mock.patch.object(manager, "_gateway_start_locked", start), \
                        mock.patch.object(
                            manager, "_live_probe_profile",
                            side_effect=failing_probe,
                        ), mock.patch.object(manager, "_gateway_stop_locked", stop), \
                                manager.operation_lock(paths):
                    with self.assertRaises(manager.ManagerError) as cm:
                        manager._validate_profile_locked(
                            paths,
                            state_data,
                            manager.Profile("deepseek-v4-flash", "max"),
                        )
                self.assertEqual(cm.exception.code, "live_test_failed")
                _assert_error_secret_free(self, cm.exception)
                self.assertEqual(start.call_count, 1)
                stop.assert_not_called()

        with self.subTest(case="stop_cleanup_failure"):
            with tempfile.TemporaryDirectory() as directory:
                paths = _configure_home(manager, Path(directory))
                state_data = manager._read_managed_state(paths)
                gateway_expected = {
                    "version": state_data["version"],
                    "local_gateway_token": state_data["local_gateway_token"],
                    "port": state_data["port"],
                }
                start = mock.Mock(return_value={
                    "status": "ok", "running": True, "changed": True,
                    "port": gateway_expected["port"],
                })
                stop = mock.Mock(
                    side_effect=manager.ManagerError(
                        "gateway_stop_failed", "stop cleanup " + SECRET_MARKER
                    )
                )

                def failing_probe(gateway_state, spec_obj, effort):
                    raise RuntimeError(SECRET_MARKER)

                with mock.patch.object(
                    manager, "read_gateway_state", return_value=gateway_expected
                ), mock.patch.object(manager, "_gateway_start_locked", start), \
                        mock.patch.object(
                            manager, "_live_probe_profile",
                            side_effect=failing_probe,
                        ), mock.patch.object(manager, "_gateway_stop_locked", stop), \
                                manager.operation_lock(paths):
                    with self.assertRaises(manager.ManagerError) as cm:
                        manager._validate_profile_locked(
                            paths,
                            state_data,
                            manager.Profile("deepseek-v4-flash", "max"),
                        )
                self.assertEqual(cm.exception.code, "live_test_cleanup_failed")
                _assert_error_secret_free(self, cm.exception)
                self.assertEqual(start.call_count, 1)
                stop.assert_called_once_with(
                    paths, gateway_expected, manager.GATEWAY_STOP_WAIT_SECONDS)

    def test_setup_default_validates_after_all_writes_under_single_lock(self):
        manager = self._manager()
        _require_live_helpers(self, manager)
        with tempfile.TemporaryDirectory() as directory:
            paths = manager.resolve_paths(str(Path(directory)))
            lock_wrapper, lock_state = _lock_tracker(manager)
            real_atomic_write = manager.atomic_write
            labels = {
                paths.state: "state",
                paths.catalog: "catalog",
                paths.agent: "agent",
                paths.config: "config",
                paths.manifest: "manifest",
            }
            order = []
            validator_calls = []

            def tracked_write(path, data, mode=0o600):
                order.append(labels.get(path, path.name))
                return real_atomic_write(path, data, mode)

            def validator(model, effort):
                validator_calls.append((model, effort))
                order.append("validate")
                self.assertEqual(lock_state["depth"], 1)
                for p in (paths.state, paths.catalog, paths.agent,
                          paths.config, paths.manifest):
                    self.assertTrue(p.is_file(), p)
                manager._static_verify(
                    paths, manager.Profile("deepseek-v4-flash", "max")
                )
                return {"skipped_live_test": True, "secret": SECRET_MARKER}

            with mock.patch.object(
                manager, "operation_lock", side_effect=lock_wrapper
            ), mock.patch.object(manager, "credential_available", return_value=True), \
                    mock.patch.object(manager, "credential_has_key", return_value=True), \
                    mock.patch.object(manager, "store_credential_key") as store, \
                    mock.patch.object(manager, "atomic_write", side_effect=tracked_write):
                result = manager.setup(
                    paths, catalog_loader=_catalog_loader(), validator=validator)
            self.assertEqual(result.get("status"), "configured")
            self.assertIs(result.get("changed"), True)
            self.assertIs(result.get("skipped_live_test"), False)
            self.assertIs(result.get("new_task_required"), True)
            self.assertTrue(result.get("backup"))
            self.assertEqual(validator_calls, [("deepseek-v4-flash", "max")])
            self.assertEqual(
                order,
                ["state", "catalog", "agent", "config", "manifest", "validate"],
            )
            self.assertEqual(lock_state["max"], 1)
            store.assert_not_called()
            _assert_secret_free(self, result)
            for p in (paths.state, paths.catalog, paths.agent,
                      paths.config, paths.manifest):
                self.assertNotIn(SECRET_MARKER.encode(), p.read_bytes())

    def test_setup_live_failure_restores_managed_files_and_new_credential(self):
        manager = self._manager()
        _require_live_helpers(self, manager)
        with tempfile.TemporaryDirectory() as directory:
            paths = manager.resolve_paths(str(Path(directory)))
            before = _managed_bytes(paths)
            self.assertEqual(before, {name: None for name in MANAGED_ORDER})
            stored = []
            removed = []
            real_atomic_write = manager.atomic_write
            order = []

            def tracked_write(path, data, mode=0o600):
                order.append(path.name)
                return real_atomic_write(path, data, mode)

            def validator(model, effort):
                order.append("validate")
                raise RuntimeError(SECRET_MARKER)

            with mock.patch.object(manager, "credential_available", return_value=True), \
                    mock.patch.object(manager, "credential_has_key", return_value=False), \
                    mock.patch.object(
                        manager, "store_credential_key", side_effect=stored.append
                    ), mock.patch.object(
                        manager, "remove_credential_key",
                        side_effect=lambda: removed.append(True),
                    ), mock.patch.object(manager, "atomic_write", side_effect=tracked_write):
                with self.assertRaises(manager.ManagerError) as cm:
                    manager.setup(
                        paths,
                        api_key_stdin=True,
                        stdin=io.StringIO(SECRET_MARKER + "\n"),
                        catalog_loader=_catalog_loader(),
                        validator=validator,
                    )
            self.assertEqual(stored, [SECRET_MARKER])
            self.assertEqual(len(removed), 1)
            self.assertEqual(cm.exception.code, "live_test_failed")
            _assert_error_secret_free(self, cm.exception)
            self.assertEqual(_managed_bytes(paths), before)
            for p in (paths.state, paths.catalog, paths.agent,
                      paths.config, paths.manifest):
                self.assertFalse(p.exists(), p)
            self.assertEqual(order[-1], "validate")

    def test_setup_idempotent_and_repair_validate_without_backup_while_skip_skips(self):
        manager = self._manager()
        _require_live_helpers(self, manager)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            managed_before = _managed_bytes(paths)
            backups_before = _backups_snapshot(paths)
            calls = []

            def validator(model, effort):
                calls.append((model, effort))
                return {"ignored": True}

            def exploding(model, effort):
                raise RuntimeError(SECRET_MARKER)

            store = mock.Mock()
            remove = mock.Mock()
            with mock.patch.object(manager, "credential_available", return_value=True), \
                    mock.patch.object(manager, "credential_has_key", return_value=True), \
                    mock.patch.object(manager, "store_credential_key", store), \
                    mock.patch.object(manager, "remove_credential_key", remove):
                result = manager.setup(
                    paths, catalog_loader=_catalog_loader(), validator=validator)
                self.assertEqual(result.get("status"), "configured")
                self.assertIs(result.get("changed"), False)
                self.assertIs(result.get("skipped_live_test"), False)
                self.assertIsNone(result.get("backup"))
                self.assertEqual(calls, [("deepseek-v4-flash", "max")])
                self.assertEqual(_managed_bytes(paths), managed_before)
                self.assertEqual(_backups_snapshot(paths), backups_before)

                calls.clear()
                repaired = manager.repair(
                    paths, catalog_loader=_catalog_loader(), validator=validator)
                self.assertEqual(repaired.get("status"), "configured")
                self.assertIs(repaired.get("changed"), False)
                self.assertIs(repaired.get("skipped_live_test"), False)
                self.assertIsNone(repaired.get("backup"))
                self.assertEqual(calls, [("deepseek-v4-flash", "max")])
                self.assertEqual(_managed_bytes(paths), managed_before)
                self.assertEqual(_backups_snapshot(paths), backups_before)

                calls.clear()
                skipped_setup = manager.setup(
                    paths, skip_live_test=True,
                    catalog_loader=_catalog_loader(), validator=exploding)
                self.assertIs(skipped_setup.get("changed"), False)
                self.assertIs(skipped_setup.get("skipped_live_test"), True)
                self.assertEqual(calls, [])
                self.assertEqual(_managed_bytes(paths), managed_before)
                self.assertEqual(_backups_snapshot(paths), backups_before)

                skipped_repair = manager.repair(
                    paths, skip_live_test=True,
                    catalog_loader=_catalog_loader(), validator=exploding)
                self.assertIs(skipped_repair.get("changed"), False)
                self.assertIs(skipped_repair.get("skipped_live_test"), True)
                self.assertEqual(calls, [])
                self.assertEqual(_managed_bytes(paths), managed_before)
                self.assertEqual(_backups_snapshot(paths), backups_before)

                store.assert_not_called()
                remove.assert_not_called()

    def test_profile_changed_validates_after_write_and_rolls_back_on_failure(self):
        manager = self._manager()
        _require_live_helpers(self, manager)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            lock_wrapper, lock_state = _lock_tracker(manager)
            calls = []

            def validator(model, effort):
                calls.append((model, effort))
                self.assertEqual(lock_state["depth"], 1)
                manifest = manager.read_manifest(paths)
                self.assertEqual(manifest["selected_model"], "deepseek-v4-pro")
                self.assertEqual(manifest["selected_effort"], "max")
                self.assertEqual(
                    paths.agent.read_bytes(),
                    manager.expected_agent_text("deepseek-v4-pro", "max").encode(),
                )
                return {"raw": SECRET_MARKER}

            with mock.patch.object(
                manager, "operation_lock", side_effect=lock_wrapper
            ):
                result = manager.set_profile(
                    paths, "deepseek-v4-pro", "max", False, validator=validator)
            self.assertEqual(result.get("status"), "configured")
            self.assertIs(result.get("changed"), True)
            self.assertIs(result.get("skipped_live_test"), False)
            self.assertIs(result.get("restart_required"), True)
            self.assertIs(result.get("new_task_required"), True)
            self.assertTrue(result.get("backup"))
            self.assertEqual(
                result.get("previous_profile"),
                {"model": "deepseek-v4-flash", "effort": "max"},
            )
            self.assertEqual(
                result.get("active_profile"),
                {"model": "deepseek-v4-pro", "effort": "max"},
            )
            self.assertEqual(calls, [("deepseek-v4-pro", "max")])
            self.assertEqual(lock_state["max"], 1)
            _assert_secret_free(self, result)
            for p in (paths.state, paths.catalog, paths.agent,
                      paths.config, paths.manifest):
                self.assertNotIn(SECRET_MARKER.encode(), p.read_bytes())

        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            managed_before = _managed_bytes(paths)
            backups_before = _backups_snapshot(paths)
            calls = []

            def failing(model, effort):
                calls.append((model, effort))
                manifest = manager.read_manifest(paths)
                self.assertEqual(manifest["selected_model"], "deepseek-v4-pro")
                raise RuntimeError(SECRET_MARKER)

            with self.assertRaises(manager.ManagerError) as cm:
                manager.set_profile(
                    paths, "deepseek-v4-pro", "max", False, validator=failing)
            self.assertEqual(cm.exception.code, "live_test_failed")
            _assert_error_secret_free(self, cm.exception)
            self.assertEqual(calls, [("deepseek-v4-pro", "max")])
            self.assertEqual(_managed_bytes(paths), managed_before)
            manifest = manager.read_manifest(paths)
            self.assertEqual(manifest["selected_model"], "deepseek-v4-flash")
            self.assertEqual(manifest["selected_effort"], "max")
            self.assertEqual(
                manager.resolve_managed_profile(manifest, paths),
                manager.Profile("deepseek-v4-flash", "max"),
            )
            self.assertGreaterEqual(
                set(_backups_snapshot(paths)), set(backups_before))

    def test_profile_same_value_validates_without_backup_skip_and_invalid_zero_write(self):
        manager = self._manager()
        _require_live_helpers(self, manager)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure_home(manager, Path(directory))
            managed_before = _managed_bytes(paths)
            backups_before = _backups_snapshot(paths)
            calls = []

            def validator(model, effort):
                calls.append((model, effort))
                return {"raw": SECRET_MARKER}

            same = manager.set_profile(
                paths, "deepseek-v4-flash", "max", False, validator=validator)
            self.assertEqual(same.get("status"), "configured")
            self.assertIs(same.get("changed"), False)
            self.assertIs(same.get("skipped_live_test"), False)
            self.assertTrue(same.get("backup") is None or "backup" not in same)
            self.assertEqual(calls, [("deepseek-v4-flash", "max")])
            self.assertEqual(_managed_bytes(paths), managed_before)
            self.assertEqual(_backups_snapshot(paths), backups_before)

            calls.clear()

            def exploding(model, effort):
                raise RuntimeError(SECRET_MARKER)

            skipped = manager.set_profile(
                paths, "deepseek-v4-flash", "max", True, validator=exploding)
            self.assertIs(skipped.get("changed"), False)
            self.assertIs(skipped.get("skipped_live_test"), True)
            self.assertEqual(calls, [])
            self.assertEqual(_managed_bytes(paths), managed_before)
            self.assertEqual(_backups_snapshot(paths), backups_before)

            with self.assertRaises(manager.ManagerError) as cm:
                manager.set_profile(
                    paths, "deepseek-v4-pro", "none", False, validator=exploding)
            self.assertEqual(cm.exception.code, "invalid_effort")
            self.assertEqual(calls, [])
            self.assertEqual(_managed_bytes(paths), managed_before)
            self.assertEqual(_backups_snapshot(paths), backups_before)

            drifted = b'{"drifted": true}\n'
            paths.catalog.write_bytes(drifted)
            managed_drifted = _managed_bytes(paths)
            with self.assertRaises(manager.ManagerError) as cm:
                manager.set_profile(
                    paths, "deepseek-v4-pro", "max", False, validator=exploding)
            self.assertEqual(cm.exception.code, "conflict")
            self.assertEqual(calls, [])
            self.assertEqual(paths.catalog.read_bytes(), drifted)
            self.assertEqual(_managed_bytes(paths), managed_drifted)
            self.assertEqual(_backups_snapshot(paths), backups_before)
