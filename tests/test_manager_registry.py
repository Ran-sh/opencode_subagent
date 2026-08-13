#!/usr/bin/env python3
"""RED contract tests for the persisted managed state v2 model registry (DS-20260811-18).

The production manager does not yet persist or consume a model registry snapshot:
clean setup writes a version-1 state, merge_catalog/profile_list/models_list/set_profile
read the built-in MODELS, and there is no read_model_registry reader.  These tests
observe only the fixed external contracts of DEC-20260811-18-V1 and must FAIL (not
ERROR) on the current production code.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_manager as fixtures

SECRET_MARKER = "registry-secret-marker-never-serialize"


def _require_callable(testcase, target, name):
    fn = getattr(target, name, None)
    if not callable(fn):
        testcase.fail(f"missing callable {name}")
    return fn


def _registry_variant(models):
    registry = {}
    for model_id, spec in models.MODELS.items():
        registry[model_id] = dataclasses.replace(spec)
    registry["hy3"] = dataclasses.replace(registry["hy3"], status="unavailable")
    flash = registry["deepseek-v4-flash"]
    registry["deepseek-v4-flash"] = dataclasses.replace(
        flash, context_window=777777, max_output=222222, source_revision="2026-08-12"
    )
    return registry


def _configure(manager, home):
    paths = manager.resolve_paths(str(home))
    with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
        manager, "credential_has_key", return_value=True
    ):
        manager.setup(paths, False, False, True, None, catalog_loader=fixtures._catalog_loader())
    return paths


def _install_registry(manager, paths, models, registry):
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    state["version"] = 2
    state["model_registry"] = models.registry_snapshot(registry)
    state_bytes = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
    paths.state.write_bytes(state_bytes)
    manifest = manager.read_manifest(paths)
    manifest["state_sha256"] = manager.sha256_bytes(state_bytes)
    manager.write_manifest(paths, manifest)
    return state_bytes


class ManagerRegistryContractTests(unittest.TestCase):
    def test_clean_setup_persists_version2_registry_and_readers_keep_contract(self):
        manager = fixtures._load_manager()
        models = fixtures._load_opencode_models()
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure(manager, Path(directory))
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertEqual(set(state), {"version", "local_gateway_token", "port", "model_registry"})
            self.assertEqual(state["version"], 2)
            self.assertIsInstance(state["port"], int)
            self.assertIsInstance(state["local_gateway_token"], str)
            snapshot = state["model_registry"]
            self.assertEqual(snapshot.get("schema_version"), 1)
            records = snapshot.get("models")
            self.assertIsInstance(records, list)
            self.assertEqual(len(records), len(models.MODELS))
            ids = [record.get("id") for record in records]
            self.assertEqual(ids, sorted(ids))
            parsed = models.registry_from_snapshot(snapshot)
            expected = models.registry_from_snapshot(models.registry_snapshot(models.MODELS))
            self.assertEqual(parsed, expected)
            for model_id, spec in models.MODELS.items():
                self.assertEqual(dataclasses.asdict(parsed[model_id]), dataclasses.asdict(spec))
            reader = _require_callable(self, manager, "read_model_registry")
            persisted = reader(paths)
            self.assertEqual(set(persisted), set(models.MODELS))
            self.assertEqual(persisted, parsed)
            gateway = manager.read_gateway_state(paths)
            self.assertEqual(set(gateway), {"version", "local_gateway_token", "port"})
            self.assertEqual(gateway["version"], state["version"])
            token = state["local_gateway_token"]
            self.assertNotIn(token, json.dumps(snapshot, ensure_ascii=False))
            self.assertNotIn(SECRET_MARKER, json.dumps(snapshot, ensure_ascii=False))
            for payload in (
                manager.gateway_status(paths),
                manager.models_list(),
                manager.profile_list(),
            ):
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(token, serialized)
                self.assertNotIn(SECRET_MARKER, serialized)

    def test_merge_catalog_uses_registry_and_excludes_unavailable(self):
        manager = fixtures._load_manager()
        models = fixtures._load_opencode_models()
        registry = _registry_variant(models)
        base = fixtures._catalog_loader()()
        try:
            merged = manager.merge_catalog(base, registry=registry)
        except TypeError as exc:
            self.fail(f"merge_catalog does not accept a registry yet: {exc}")
        slugs = [item["slug"] for item in merged["models"]]
        expected_slugs = sorted(set(models.MODELS) - {"hy3"} | {"unrelated-parent"})
        self.assertEqual(slugs, expected_slugs)
        self.assertEqual(len(set(slugs)), len(slugs))
        unrelated = next(item for item in merged["models"] if item["slug"] == "unrelated-parent")
        self.assertEqual(unrelated, base["models"][0])
        flash = next(item for item in merged["models"] if item["slug"] == "deepseek-v4-flash")
        self.assertEqual(flash["context_window"], 777777)
        self.assertEqual(flash["max_context_window"], 777777)
        self.assertEqual(flash["x_opencode_go"]["max_output"], 222222)
        self.assertEqual(flash["x_opencode_go"]["source_revision"], "2026-08-12")
        self.assertEqual(models.MODELS["deepseek-v4-flash"].context_window, 1000000)
        self.assertEqual(models.MODELS["deepseek-v4-flash"].max_output, 384000)
        self.assertEqual(models.MODELS["hy3"].status, "active")

    def test_repair_preserves_registry_and_rebuilds_catalog(self):
        manager = fixtures._load_manager()
        models = fixtures._load_opencode_models()
        registry = _registry_variant(models)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure(manager, Path(directory))
            state_bytes = _install_registry(manager, paths, models, registry)
            token = json.loads(state_bytes)["local_gateway_token"]
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ):
                result = manager.repair(
                    paths, False, False, True, None, catalog_loader=fixtures._catalog_loader()
                )
            self.assertEqual(result.get("status"), "configured")
            self.assertIs(result.get("changed"), True)
            self.assertEqual(paths.state.read_bytes(), state_bytes)
            catalog = json.loads(paths.catalog.read_text(encoding="utf-8"))
            slugs = [item["slug"] for item in catalog["models"]]
            self.assertNotIn("hy3", slugs)
            flash = next(item for item in catalog["models"] if item["slug"] == "deepseek-v4-flash")
            self.assertEqual(flash["context_window"], 777777)
            self.assertEqual(flash["x_opencode_go"]["max_output"], 222222)
            self.assertEqual(flash["x_opencode_go"]["source_revision"], "2026-08-12")
            manifest = manager.read_manifest(paths)
            self.assertEqual(manifest["state_sha256"], manager.sha256_bytes(state_bytes))
            for payload in (result, catalog, manifest):
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(token, serialized)
                self.assertNotIn(SECRET_MARKER, serialized)

    def test_models_and_profiles_use_persisted_status_but_no_paths_use_builtin(self):
        manager = fixtures._load_manager()
        models = fixtures._load_opencode_models()
        registry = _registry_variant(models)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure(manager, Path(directory))
            state_bytes = _install_registry(manager, paths, models, registry)
            token = json.loads(state_bytes)["local_gateway_token"]
            try:
                models_payload = manager.models_list(paths)
            except TypeError as exc:
                self.fail(f"models_list does not accept paths yet: {exc}")
            try:
                profiles_payload = manager.profile_list(paths)
            except TypeError as exc:
                self.fail(f"profile_list does not accept paths yet: {exc}")
            items = models_payload["models"]
            self.assertEqual(len(items), len(models.MODELS))
            self.assertEqual({item["slug"] for item in items}, set(models.MODELS))
            hy3 = next(item for item in items if item["slug"] == "hy3")
            self.assertEqual(hy3.get("status"), "unavailable")
            flash = next(item for item in items if item["slug"] == "deepseek-v4-flash")
            self.assertEqual(flash.get("status"), "active")
            self.assertEqual(flash["context_window"], 777777)
            self.assertEqual(flash["x_opencode_go"]["max_output"], 222222)
            self.assertEqual(flash["x_opencode_go"]["source_revision"], "2026-08-12")
            profiles = profiles_payload["profiles"]
            profile_models = {profile["model"] for profile in profiles}
            self.assertNotIn("hy3", profile_models)
            expected_efforts = {
                model_id: set(spec.efforts)
                for model_id, spec in registry.items()
                if spec.status == "active"
            }
            actual_efforts = {}
            for profile in profiles:
                actual_efforts.setdefault(profile["model"], set()).add(profile["effort"])
            self.assertEqual(actual_efforts, expected_efforts)
            builtin_models = manager.models_list()
            self.assertEqual(len(builtin_models["models"]), len(models.MODELS))
            builtin_profiles = manager.profile_list()
            self.assertIn("hy3", {profile["model"] for profile in builtin_profiles["profiles"]})
            serialized = json.dumps([models_payload, profiles_payload], ensure_ascii=False)
            self.assertNotIn(token, serialized)
            self.assertNotIn(SECRET_MARKER, serialized)

    def test_set_profile_rejects_unavailable_with_zero_write(self):
        manager = fixtures._load_manager()
        models = fixtures._load_opencode_models()
        registry = _registry_variant(models)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure(manager, Path(directory))
            _install_registry(manager, paths, models, registry)
            before = fixtures._snapshot(paths.home)
            backups_before = fixtures._snapshot(paths.backups) if paths.backups.is_dir() else {}
            with self.assertRaises(manager.ManagerError) as raised:
                manager.set_profile(paths, "hy3", "high", True)
            self.assertEqual(raised.exception.code, "model_unavailable")
            after = fixtures._snapshot(paths.home)
            backups_after = fixtures._snapshot(paths.backups) if paths.backups.is_dir() else {}
            self.assertEqual(after, before)
            self.assertEqual(backups_after, backups_before)

    def test_invalid_registry_state_is_conflict_and_zero_write(self):
        manager = fixtures._load_manager()
        models = fixtures._load_opencode_models()
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure(manager, Path(directory))
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            state["version"] = 2
            bad_snapshot = models.registry_snapshot()
            bad_snapshot["models"][0]["efforts"] = [{}]
            state["model_registry"] = bad_snapshot
            state_bytes = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
            paths.state.write_bytes(state_bytes)
            manifest = manager.read_manifest(paths)
            manifest["state_sha256"] = manager.sha256_bytes(state_bytes)
            manager.write_manifest(paths, manifest)
            token = state["local_gateway_token"]
            before = fixtures._snapshot(paths.home)
            backups_before = fixtures._snapshot(paths.backups) if paths.backups.is_dir() else {}
            reader = _require_callable(self, manager, "read_model_registry")
            with self.assertRaises(manager.ManagerError) as raised:
                reader(paths)
            self.assertEqual(raised.exception.code, "conflict")
            fields = raised.exception.details.get("fields", [])
            self.assertIn("model_registry", fields)
            status_payload = manager.status(paths)
            self.assertEqual(status_payload.get("status"), "conflict")
            checks = status_payload.get("checks") or {}
            self.assertIs(checks.get("model_registry_valid"), False)
            with self.assertRaises(manager.ManagerError) as raised:
                manager.set_profile(paths, "deepseek-v4-flash", "max", True)
            self.assertEqual(raised.exception.code, "conflict")
            after = fixtures._snapshot(paths.home)
            backups_after = fixtures._snapshot(paths.backups) if paths.backups.is_dir() else {}
            self.assertEqual(after, before)
            self.assertEqual(backups_after, backups_before)
            serialized = json.dumps(status_payload, ensure_ascii=False)
            self.assertNotIn(token, serialized)
            self.assertNotIn(SECRET_MARKER, serialized)

    # R1: exact legacy 18-model managed snapshot upgrades in memory with zero writes.
    def test_exact_legacy_catalog_is_upgraded_in_memory_without_managed_writes(self):
        manager = fixtures._load_manager()
        models = fixtures._load_opencode_models()
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure(manager, Path(directory))
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            snapshot = state["model_registry"]
            self.assertEqual(len(snapshot["models"]), 19)
            legacy_records = [
                record for record in snapshot["models"]
                if record["id"] != "minimax-m2.5"]
            self.assertEqual(len(legacy_records), 18)
            state["model_registry"] = dict(snapshot, models=legacy_records)
            state_bytes = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
            paths.state.write_bytes(state_bytes)
            manifest = manager.read_manifest(paths)
            manifest["state_sha256"] = manager.sha256_bytes(state_bytes)
            manager.write_manifest(paths, manifest)
            before = fixtures._snapshot(paths.home)
            backups_before = fixtures._snapshot(paths.backups) if paths.backups.is_dir() else {}
            with mock.patch.object(manager, "credential_has_key", return_value=True):
                registry = manager.read_model_registry(paths)
                status_payload = manager.status(paths)
                gateway = manager.read_gateway_state(paths)
            self.assertEqual(len(registry), 19)
            self.assertEqual(set(registry), set(models.MODELS))
            self.assertEqual(registry["minimax-m2.5"], models.MODELS["minimax-m2.5"])
            self.assertEqual(status_payload.get("status"), "configured")
            self.assertEqual(
                status_payload.get("active_profile"),
                {"model": "deepseek-v4-flash", "effort": "max"})
            checks = status_payload.get("checks") or {}
            self.assertIs(checks.get("model_registry_valid"), True)
            self.assertEqual(gateway["version"], state["version"])
            self.assertEqual(gateway["port"], state["port"])
            after = fixtures._snapshot(paths.home)
            backups_after = fixtures._snapshot(paths.backups) if paths.backups.is_dir() else {}
            self.assertEqual(after, before)
            self.assertEqual(backups_after, backups_before)
            serialized = json.dumps(
                [
                    registry,
                    status_payload,
                    {"version": gateway["version"], "port": gateway["port"]},
                ],
                ensure_ascii=False)
            self.assertNotIn(SECRET_MARKER, serialized)
        with tempfile.TemporaryDirectory() as directory:
            paths = _configure(manager, Path(directory))
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            snapshot = state["model_registry"]
            missing_hy3 = [
                record for record in snapshot["models"] if record["id"] != "hy3"]
            self.assertEqual(len(missing_hy3), 18)
            self.assertIn(
                "minimax-m2.5", {record["id"] for record in missing_hy3})
            state["model_registry"] = dict(snapshot, models=missing_hy3)
            state_bytes = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
            paths.state.write_bytes(state_bytes)
            manifest = manager.read_manifest(paths)
            manifest["state_sha256"] = manager.sha256_bytes(state_bytes)
            manager.write_manifest(paths, manifest)
            before = fixtures._snapshot(paths.home)
            backups_before = fixtures._snapshot(paths.backups) if paths.backups.is_dir() else {}
            with self.assertRaises(manager.ManagerError) as raised:
                manager.read_model_registry(paths)
            self.assertEqual(raised.exception.code, "conflict")
            self.assertIn(
                "model_registry", raised.exception.details.get("fields", []))
            status_payload = manager.status(paths)
            self.assertEqual(status_payload.get("status"), "conflict")
            checks = status_payload.get("checks") or {}
            self.assertIs(checks.get("model_registry_valid"), False)
            after = fixtures._snapshot(paths.home)
            backups_after = fixtures._snapshot(paths.backups) if paths.backups.is_dir() else {}
            self.assertEqual(after, before)
            self.assertEqual(backups_after, backups_before)
