#!/usr/bin/env python3
"""RED contract tests for codex_opencode_go (DS-20260811-09 REV-1).

The production manager is intentionally absent during RED; every test loads it
lazily so a missing manager produces a clean FAIL ("manager_missing") instead of
a module-import ERROR. Tests observe only the fixed public interface, temporary
files, CLI JSON and mocked credentials.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "codex-opencode-go-subagent"
    / "scripts"
    / "codex_opencode_go.py"
)
MODELS_SCRIPT = SCRIPT.parent / "opencode_models.py"
SECRET_MARKER = "sk-" + "opencode-super-secret-marker"
PROVIDER_BEGIN = "# BEGIN CODEX-OPENCODE-GO-SUBAGENT PROVIDER"
PROVIDER_END = "# END CODEX-OPENCODE-GO-SUBAGENT PROVIDER"
ROLE_BEGIN = "# BEGIN CODEX-OPENCODE-GO-SUBAGENT ROLE"
ROLE_END = "# END CODEX-OPENCODE-GO-SUBAGENT ROLE"
LEGACY_PROVIDER_BEGIN = "# BEGIN CODEX-DEEPSEEK-SUBAGENT PROVIDER"
LEGACY_PROVIDER_END = "# END CODEX-DEEPSEEK-SUBAGENT PROVIDER"
LEGACY_ROLE_BEGIN = "# BEGIN CODEX-DEEPSEEK-SUBAGENT ROLE"
LEGACY_ROLE_END = "# END CODEX-DEEPSEEK-SUBAGENT ROLE"


def _load_manager():
    path = Path(SCRIPT)
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location("codex_opencode_go", str(path))
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_opencode_models():
    path = Path(MODELS_SCRIPT)
    spec = importlib.util.spec_from_file_location("opencode_models", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"frozen module missing: {path}")
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
                    "display_name": "Unrelated Parent",
                    "description": "Unrelated parent full template",
                    "base_instructions": "You are the unrelated parent template with tools.",
                    "model_messages": {
                        "instructions_template": "You are the unrelated parent template with tools.",
                        "instructions_variables": None,
                    },
                    "default_reasoning_level": None,
                    "supported_reasoning_levels": [{"effort": "medium", "description": "Medium reasoning"}],
                    "shell_type": "shell_command",
                    "visibility": "list",
                    "supported_in_api": True,
                    "priority": 1,
                    "additional_speed_tiers": ["fast"],
                    "service_tiers": [],
                    "default_service_tier": None,
                    "availability_nux": {"message": "Try the template."},
                    "upgrade": {"model": "next-model", "migration_markdown": "migrate"},
                    "include_skills_usage_instructions": True,
                    "include_plugin_usage_instructions": True,
                    "supports_reasoning_summary_parameter": True,
                    "default_reasoning_summary": "auto",
                    "support_verbosity": True,
                    "default_verbosity": "low",
                    "apply_patch_tool_type": "freeform",
                    "web_search_tool_type": "text_and_image",
                    "truncation_policy": {"mode": "tokens", "limit": 100000},
                    "supports_parallel_tool_calls": False,
                    "supports_image_detail_original": True,
                    "context_window": 999,
                    "max_context_window": 999,
                    "auto_compact_token_limit": 800,
                    "comp_hash": "template-comp-hash",
                    "effective_context_window_percent": 95,
                    "experimental_supported_tools": ["template-tool"],
                    "input_modalities": ["text"],
                    "supports_search_tool": True,
                    "use_responses_lite": False,
                    "tool_mode": "code_mode",
                    "multi_agent_version": "v9",
                    "unknown_future": {"preserve": True},
                }
            ]
        }

    return loader


def _snapshot(root: Path):
    entries = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            entries[rel] = ("dir", None)
        elif path.is_file():
            entries[rel] = ("file", path.read_bytes())
    return entries


def _all_files(root: Path):
    return [path for path in sorted(root.rglob("*")) if path.is_file()]


class ManagerContractTests(unittest.TestCase):
    def _manager(self):
        try:
            return _load_manager()
        except FileNotFoundError:
            self.fail("manager_missing")

    def test_profile_validation_is_zero_write(self):
        manager = self._manager()
        sentinel = b'model = "gpt-sentinel"\n[features]\nmulti_agent = true\n'
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            paths.config.parent.mkdir(parents=True, exist_ok=True)
            paths.config.write_bytes(sentinel)
            before = _snapshot(home)
            with self.assertRaises(manager.ManagerError) as raised:
                manager.set_profile(paths, "deepseek-v4-flash", "medium", True)
            self.assertEqual(raised.exception.code, "invalid_effort")
            self.assertEqual(paths.config.read_bytes(), sentinel)
            self.assertEqual(_snapshot(home), before)
            self.assertFalse((paths.state_dir / "backups").exists())

    def test_setup_default_is_transactional_and_secret_free(self):
        manager = self._manager()
        stored = []

        def fake_store(secret):
            stored.append(secret)

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            stdin_text = io.StringIO(SECRET_MARKER + "\n")
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=False
            ), mock.patch.object(manager, "store_credential_key", side_effect=fake_store):
                result = manager.setup(
                    paths, True, False, True, stdin_text, catalog_loader=_catalog_loader()
                )
            self.assertEqual(result["status"], "configured")
            self.assertTrue(result.get("new_task_required"))
            self.assertEqual(stored, [SECRET_MARKER])
            config_text = paths.config.read_text(encoding="utf-8")
            self.assertIn(PROVIDER_BEGIN, config_text)
            self.assertIn(PROVIDER_END, config_text)
            self.assertIn(ROLE_BEGIN, config_text)
            self.assertIn(ROLE_END, config_text)
            parsed = manager.parse_toml_text(config_text)
            self.assertIsNotNone((parsed.get("model_providers") or {}).get("opencode-go"))
            self.assertIsNotNone((parsed.get("agents") or {}).get("OpenCode"))
            agent_text = paths.agent.read_text(encoding="utf-8")
            self.assertIn('name = "OpenCode"', agent_text)
            self.assertIn('model = "deepseek-v4-flash"', agent_text)
            self.assertIn('model_provider = "opencode-go"', agent_text)
            catalog = json.loads(paths.catalog.read_text(encoding="utf-8"))
            slugs = {item["slug"] for item in catalog["models"]}
            models = _load_opencode_models()
            self.assertEqual(len(slugs), len(models.MODELS) + 1)
            self.assertTrue(set(models.MODELS).issubset(slugs))
            self.assertIn("unrelated-parent", slugs)
            preserved = next(
                item for item in catalog["models"] if item["slug"] == "unrelated-parent"
            )
            self.assertEqual(preserved["name"], "Unrelated Parent")
            self.assertEqual(preserved["multi_agent_version"], "v9")
            self.assertTrue(paths.manifest.is_file())
            self.assertTrue((paths.state_dir / "state.json").is_file())
            self.assertNotIn(SECRET_MARKER, json.dumps(result, ensure_ascii=False))
            marker_bytes = SECRET_MARKER.encode("utf-8")
            for file_path in _all_files(home):
                self.assertNotIn(marker_bytes, file_path.read_bytes())

    def test_setup_is_idempotent_without_backup(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ):
                first = manager.setup(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
            managed = (
                paths.config,
                paths.catalog,
                paths.agent,
                paths.manifest,
                paths.state_dir / "state.json",
            )
            before_bytes = {p: p.read_bytes() for p in managed}
            backups_dir = paths.state_dir / "backups"
            backups_before = _snapshot(backups_dir) if backups_dir.exists() else {}
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ):
                second = manager.setup(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
            self.assertEqual(first["status"], "configured")
            self.assertEqual(second["status"], "configured")
            self.assertFalse(second.get("changed", False))
            for p, data in before_bytes.items():
                self.assertEqual(p.read_bytes(), data)
            backups_after = _snapshot(backups_dir) if backups_dir.exists() else {}
            self.assertEqual(backups_after, backups_before)

    def test_setup_requires_explicit_migration_with_zero_write(self):
        manager = self._manager()
        legacy = (
            'model = "gpt-parent"\n'
            f"{LEGACY_PROVIDER_BEGIN}\n"
            "[model_providers.deepseek]\n"
            'name = "DeepSeek"\n'
            'wire_api = "responses"\n'
            f"{LEGACY_PROVIDER_END}\n"
            f"{LEGACY_ROLE_BEGIN}\n"
            "[agents.DeepSeek]\n"
            'description = "legacy role"\n'
            f"{LEGACY_ROLE_END}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            paths.config.parent.mkdir(parents=True, exist_ok=True)
            paths.config.write_text(legacy, encoding="utf-8")
            before = _snapshot(home)
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ), mock.patch.object(manager, "store_credential_key") as store:
                with self.assertRaises(manager.ManagerError) as raised:
                    manager.setup(
                        paths,
                        True,
                        False,
                        True,
                        io.StringIO(SECRET_MARKER + "\n"),
                        catalog_loader=_catalog_loader(),
                    )
            self.assertEqual(raised.exception.code, "migration_required")
            self.assertEqual(_snapshot(home), before)
            store.assert_not_called()
            self.assertFalse((paths.state_dir / "backups").exists())

    def test_explicit_migration_preserves_legacy_disk_and_credential(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            legacy_catalog = home / "models-with-deepseek.json"
            legacy_agent = home / "agents" / "DeepSeek.toml"
            legacy_state = home / "codex-deepseek-subagent"
            config_text = (
                'model = "gpt-parent"\n'
                f'model_catalog_json = "{legacy_catalog.as_posix()}"\n'
                f"{LEGACY_PROVIDER_BEGIN}\n"
                "[model_providers.deepseek]\n"
                'name = "DeepSeek"\n'
                'wire_api = "responses"\n'
                f"{LEGACY_PROVIDER_END}\n"
                f"{LEGACY_ROLE_BEGIN}\n"
                "[agents.DeepSeek]\n"
                'description = "legacy role"\n'
                f'config_file = "{legacy_agent.as_posix()}"\n'
                f"{LEGACY_ROLE_END}\n"
            )
            paths.config.parent.mkdir(parents=True, exist_ok=True)
            paths.config.write_text(config_text, encoding="utf-8")
            legacy_agent.parent.mkdir(parents=True, exist_ok=True)
            legacy_agent.write_text(
                'name = "DeepSeek"\nmodel = "deepseek-v4-flash"\n', encoding="utf-8"
            )
            legacy_state.mkdir(parents=True, exist_ok=True)
            (legacy_state / "manifest.json").write_text('{"schema_version": 4}\n', encoding="utf-8")
            backup_dir = legacy_state / "backups" / "old"
            backup_dir.mkdir(parents=True, exist_ok=True)
            (backup_dir / "keep.txt").write_bytes(b"legacy backup bytes")
            legacy_catalog.write_bytes(b'{"models": [{"slug": "deepseek-v4-flash"}]}\n')
            legacy_files = {
                p: p.read_bytes()
                for p in (
                    legacy_agent,
                    legacy_state / "manifest.json",
                    backup_dir / "keep.txt",
                    legacy_catalog,
                )
            }
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ), mock.patch.object(manager, "store_credential_key") as store:
                result = manager.setup(
                    paths, True, True, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
            self.assertEqual(result["status"], "configured")
            new_config = paths.config.read_text(encoding="utf-8")
            self.assertNotIn(LEGACY_PROVIDER_BEGIN, new_config)
            self.assertNotIn(LEGACY_ROLE_BEGIN, new_config)
            self.assertNotIn("[model_providers.deepseek]", new_config)
            self.assertNotIn("[agents.DeepSeek]", new_config)
            self.assertIn(PROVIDER_BEGIN, new_config)
            self.assertIn(ROLE_BEGIN, new_config)
            parsed = manager.parse_toml_text(new_config)
            self.assertEqual(Path(parsed["model_catalog_json"]).resolve(), paths.catalog.resolve())
            for p, data in legacy_files.items():
                self.assertEqual(p.read_bytes(), data)
            store.assert_not_called()
            self.assertTrue(paths.agent.is_file())

    def test_transaction_failure_restores_every_path(self):
        manager = self._manager()
        sentinel = b'# user config\nmodel = "gpt-parent"\n'
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            paths.config.parent.mkdir(parents=True, exist_ok=True)
            paths.config.write_bytes(sentinel)
            real_atomic_write = manager.atomic_write
            failed = False

            def flaky_write(path, data, mode=0o600):
                nonlocal failed
                if path == paths.agent and not failed:
                    failed = True
                    raise OSError("injected atomic failure")
                return real_atomic_write(path, data, mode)

            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ), mock.patch.object(manager, "atomic_write", side_effect=flaky_write):
                with self.assertRaises(OSError):
                    manager.setup(
                        paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                    )
            self.assertEqual(paths.config.read_bytes(), sentinel)
            for path in (paths.catalog, paths.agent, paths.manifest, paths.state_dir / "state.json"):
                self.assertFalse(path.exists())

    def test_new_credential_is_removed_when_setup_rolls_back(self):
        manager = self._manager()
        credentials = {}

        def fake_store(secret):
            credentials["key"] = secret

        def fake_remove():
            return credentials.pop("key", None) is not None

        def fake_has():
            return "key" in credentials

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            real_atomic_write = manager.atomic_write
            failed = False

            def flaky_write(path, data, mode=0o600):
                nonlocal failed
                if path == paths.agent and not failed:
                    failed = True
                    raise OSError("injected atomic failure")
                return real_atomic_write(path, data, mode)

            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", side_effect=fake_has
            ), mock.patch.object(manager, "store_credential_key", side_effect=fake_store), mock.patch.object(
                manager, "remove_credential_key", side_effect=fake_remove
            ) as remove_mock, mock.patch.object(manager, "atomic_write", side_effect=flaky_write):
                with self.assertRaises(OSError):
                    manager.setup(
                        paths,
                        True,
                        False,
                        True,
                        io.StringIO(SECRET_MARKER + "\n"),
                        catalog_loader=_catalog_loader(),
                    )
            self.assertEqual(credentials, {})
            remove_mock.assert_called_once()

    def test_profile_set_switches_and_invalid_combination_zero_write(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ):
                manager.setup(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
            managed = (
                paths.config,
                paths.catalog,
                paths.agent,
                paths.manifest,
                paths.state_dir / "state.json",
            )
            result = manager.set_profile(paths, "deepseek-v4-pro", "max", True)
            self.assertEqual(result["status"], "configured")
            self.assertTrue(result["changed"])
            self.assertTrue(result.get("new_task_required"))
            self.assertIsInstance(result.get("backup"), str)
            self.assertTrue(Path(result["backup"]).is_dir())
            agent_text = paths.agent.read_text(encoding="utf-8")
            self.assertIn('model = "deepseek-v4-pro"', agent_text)
            self.assertIn('model_reasoning_effort = "max"', agent_text)
            catalog = json.loads(paths.catalog.read_text(encoding="utf-8"))
            slugs = {item["slug"] for item in catalog["models"]}
            self.assertIn("deepseek-v4-pro", slugs)
            self.assertIn("deepseek-v4-flash", slugs)
            self.assertIn("unrelated-parent", slugs)
            switched = {p: p.read_bytes() for p in managed}
            backups_dir = paths.state_dir / "backups"
            backups_switched = _snapshot(backups_dir) if backups_dir.exists() else {}
            with self.assertRaises(manager.ManagerError) as raised:
                manager.set_profile(paths, "deepseek-v4-pro", "none", True)
            self.assertEqual(raised.exception.code, "invalid_effort")
            for p, data in switched.items():
                self.assertEqual(p.read_bytes(), data)
            backups_after = _snapshot(backups_dir) if backups_dir.exists() else {}
            self.assertEqual(backups_after, backups_switched)

    def test_profile_same_value_zero_write(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ):
                manager.setup(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
            managed = (
                paths.config,
                paths.catalog,
                paths.agent,
                paths.manifest,
                paths.state_dir / "state.json",
            )
            before = {p: p.read_bytes() for p in managed}
            backups_dir = paths.state_dir / "backups"
            backups_before = _snapshot(backups_dir) if backups_dir.exists() else {}
            result = manager.set_profile(paths, "deepseek-v4-flash", "max", True)
            self.assertFalse(result.get("changed", False))
            self.assertTrue(result.get("backup") is None or "backup" not in result)
            for p, data in before.items():
                self.assertEqual(p.read_bytes(), data)
            backups_after = _snapshot(backups_dir) if backups_dir.exists() else {}
            self.assertEqual(backups_after, backups_before)

    def test_manifest_drift_returns_conflict(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ):
                manager.setup(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
            drifted = paths.catalog.read_bytes() + b"\n# user drift\n"
            paths.catalog.write_bytes(drifted)
            with self.assertRaises(manager.ManagerError) as raised:
                manager.set_profile(paths, "deepseek-v4-pro", "max", True)
            self.assertEqual(raised.exception.code, "conflict")
            self.assertEqual(paths.catalog.read_bytes(), drifted)
            status = manager.status(paths)
            self.assertEqual(status["status"], "conflict")

    def test_disable_preserves_provider_catalog_state_and_credential(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ), mock.patch.object(manager, "remove_credential_key") as remove_mock:
                manager.setup(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
                result = manager.disable(paths)
            self.assertEqual(result["status"], "disabled")
            self.assertTrue(result.get("changed"))
            config_text = paths.config.read_text(encoding="utf-8")
            self.assertNotIn(ROLE_BEGIN, config_text)
            self.assertNotIn(ROLE_END, config_text)
            self.assertNotIn("[agents.OpenCode]", config_text)
            self.assertIn(PROVIDER_BEGIN, config_text)
            self.assertIn(PROVIDER_END, config_text)
            self.assertIn("[model_providers.opencode-go]", config_text)
            self.assertTrue(paths.catalog.is_file())
            self.assertTrue(paths.manifest.is_file())
            self.assertTrue((paths.state_dir / "state.json").is_file())
            self.assertFalse(paths.agent.exists())
            remove_mock.assert_not_called()

    def test_uninstall_preserves_or_explicitly_removes_credential(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ), mock.patch.object(manager, "remove_credential_key") as remove_mock:
                manager.setup(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
                result = manager.uninstall(paths, False)
            self.assertEqual(result["status"], "uninstalled")
            config_text = paths.config.read_text(encoding="utf-8")
            self.assertNotIn(PROVIDER_BEGIN, config_text)
            self.assertNotIn(ROLE_BEGIN, config_text)
            self.assertNotIn("[model_providers.opencode-go]", config_text)
            parsed = manager.parse_toml_text(config_text)
            self.assertNotIn("model_catalog_json", parsed)
            self.assertFalse(paths.catalog.exists())
            self.assertFalse(paths.manifest.exists())
            self.assertFalse((paths.state_dir / "state.json").exists())
            remove_mock.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ), mock.patch.object(manager, "remove_credential_key") as remove_mock:
                manager.setup(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
                manager.uninstall(paths, True)
            remove_mock.assert_called_once()

    def test_models_list_has_exact_18_and_stable_fields(self):
        manager = self._manager()
        models = _load_opencode_models()
        payload = manager.models_list()
        items = payload.get("models", []) if isinstance(payload, dict) else payload
        self.assertIsInstance(items, list)
        self.assertEqual(len(items), len(models.MODELS))
        by_slug = {item["slug"]: item for item in items}
        self.assertEqual(set(by_slug), set(models.MODELS))
        required = {
            "slug",
            "display_name",
            "description",
            "default_reasoning_level",
            "supported_reasoning_levels",
            "context_window",
            "max_context_window",
            "input_modalities",
            "supports_search_tool",
            "supports_image_detail_original",
            "multi_agent_version",
            "x_opencode_go",
            "shell_type",
            "apply_patch_tool_type",
            "additional_speed_tiers",
            "service_tiers",
            "default_service_tier",
            "availability_nux",
            "upgrade",
            "supports_reasoning_summary_parameter",
            "default_reasoning_summary",
            "support_verbosity",
            "default_verbosity",
            "web_search_tool_type",
            "supports_parallel_tool_calls",
            "auto_compact_token_limit",
            "comp_hash",
            "experimental_supported_tools",
            "use_responses_lite",
            "tool_mode",
        }
        for item in items:
            self.assertTrue(required.issubset(item))
            self.assertEqual(item["input_modalities"], ["text"])
            self.assertIs(item["supports_search_tool"], False)
            self.assertEqual(item["multi_agent_version"], "v1")
            self.assertIsInstance(item["context_window"], int)
            self.assertIsInstance(item["max_context_window"], int)
            self.assertIsInstance(item["supported_reasoning_levels"], list)
            self.assertTrue(item["supported_reasoning_levels"])
            self.assertTrue(item.get("description"))
            self.assertEqual(item.get("shell_type"), "shell_command")
            self.assertEqual(item.get("apply_patch_tool_type"), "freeform")
            self.assertEqual(item.get("additional_speed_tiers"), [])
            self.assertEqual(item.get("service_tiers"), [])
            self.assertIsNone(item.get("default_service_tier"))
            self.assertIsNone(item.get("availability_nux"))
            self.assertIsNone(item.get("upgrade"))
            self.assertIs(item.get("supports_reasoning_summary_parameter"), False)
            self.assertEqual(item.get("default_reasoning_summary"), "none")
            self.assertIs(item.get("support_verbosity"), False)
            self.assertIsNone(item.get("default_verbosity"))
            self.assertEqual(item.get("web_search_tool_type"), "text")
            self.assertIs(item.get("supports_parallel_tool_calls"), True)
            self.assertIsNone(item.get("auto_compact_token_limit"))
            self.assertIsNone(item.get("comp_hash"))
            self.assertEqual(item.get("experimental_supported_tools"), [])
            self.assertIs(item.get("use_responses_lite"), False)
            self.assertEqual(item.get("tool_mode"), "direct")
            for key in ("transport", "max_output", "source_revision", "tool_call"):
                self.assertIn(key, item["x_opencode_go"])

    def test_cli_parses_deferred_refresh_gateway_and_test(self):
        manager = self._manager()
        commands = (
            (["gateway", "status"], "gateway_unavailable"),
            (["gateway", "start"], "gateway_unavailable"),
            (["gateway", "stop"], "gateway_unavailable"),
            (["models", "refresh"], "not_managed"),
            (["test"], "gateway_unavailable"),
        )
        for command, expected_status in commands:
            with self.subTest(command=command, expected_status=expected_status), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                output = io.StringIO()
                with redirect_stdout(output):
                    manager.main(command + ["--json", "--codex-home", str(home)])
                payload = json.loads(output.getvalue())
                self.assertIn("status", payload)
                self.assertEqual(payload["status"], expected_status)
                self.assertEqual(_all_files(home), [])

    def test_fresh_status_is_unconfigured_without_drift_or_write(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            before = _snapshot(home)
            result = manager.status(paths)
            self.assertEqual(result["status"], "unconfigured")
            self.assertEqual(result.get("drift"), [])
            self.assertEqual(_snapshot(home), before)

    def test_disable_state_is_idempotent_and_repairable(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ), mock.patch.object(manager, "remove_credential_key") as remove_mock:
                configured = manager.setup(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
                first = manager.disable(paths)
            self.assertEqual(first["status"], "disabled")
            self.assertTrue(first.get("changed"))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ):
                try:
                    second = manager.disable(paths)
                except manager.ManagerError as exc:
                    self.fail(f"repeated disable must be idempotent, got code={exc.code}")
                self.assertFalse(second.get("changed", True))
                status = manager.status(paths)
                self.assertEqual(status["status"], "disabled")
                self.assertEqual(status["drift"], [])
                config_text = paths.config.read_text(encoding="utf-8")
                self.assertIn(PROVIDER_BEGIN, config_text)
                self.assertIn(PROVIDER_END, config_text)
                self.assertIn("[model_providers.opencode-go]", config_text)
                self.assertNotIn(ROLE_BEGIN, config_text)
                self.assertNotIn(ROLE_END, config_text)
                self.assertNotIn("[agents.OpenCode]", config_text)
                self.assertTrue(paths.catalog.is_file())
                self.assertTrue(paths.manifest.is_file())
                self.assertTrue((paths.state_dir / "state.json").is_file())
                self.assertFalse(paths.agent.exists())
                remove_mock.assert_not_called()
                repaired = manager.repair(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
                self.assertEqual(repaired["status"], "configured")
                self.assertEqual(repaired.get("active_profile"), configured.get("active_profile"))

    def test_uninstall_succeeds_directly_from_disabled(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ), mock.patch.object(manager, "remove_credential_key") as remove_mock:
                manager.setup(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
                manager.disable(paths)
                try:
                    result = manager.uninstall(paths, False)
                except manager.ManagerError as exc:
                    self.fail(f"uninstall from disabled must succeed, got code={exc.code}")
                self.assertEqual(result["status"], "uninstalled")
                self.assertTrue(result.get("changed"))
                config_text = paths.config.read_text(encoding="utf-8")
                self.assertNotIn(PROVIDER_BEGIN, config_text)
                self.assertNotIn(PROVIDER_END, config_text)
                self.assertNotIn(ROLE_BEGIN, config_text)
                self.assertNotIn(ROLE_END, config_text)
                self.assertNotIn("[model_providers.opencode-go]", config_text)
                parsed = manager.parse_toml_text(config_text)
                self.assertNotIn("model_catalog_json", parsed)
                self.assertFalse(paths.catalog.exists())
                self.assertFalse(paths.manifest.exists())
                self.assertFalse((paths.state_dir / "state.json").exists())
                remove_mock.assert_not_called()

    def test_disable_failure_restores_preoperation_managed_files(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ):
                manager.setup(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
                managed = (
                    paths.config,
                    paths.catalog,
                    paths.agent,
                    paths.manifest,
                    paths.state_dir / "state.json",
                )
                before = {path: path.read_bytes() for path in managed}
                real_atomic_write = manager.atomic_write
                failed = False

                def flaky_write(path, data, mode=0o600):
                    nonlocal failed
                    if path == paths.manifest and not failed:
                        failed = True
                        raise OSError("injected manifest write failure")
                    return real_atomic_write(path, data, mode)

                with mock.patch.object(manager, "atomic_write", side_effect=flaky_write):
                    with self.assertRaises(OSError):
                        manager.disable(paths)
                self.assertEqual({path: path.read_bytes() for path in managed}, before)

    def test_repair_rejects_unrecognized_managed_drift(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ):
                manager.setup(
                    paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                )
            managed = (
                paths.config,
                paths.catalog,
                paths.agent,
                paths.manifest,
                paths.state_dir / "state.json",
            )
            backups_dir = paths.state_dir / "backups"
            backups_before = _snapshot(backups_dir) if backups_dir.exists() else {}
            paths.catalog.write_bytes(paths.catalog.read_bytes() + b"\n# user drift\n")
            before_repair = {path: path.read_bytes() for path in managed}
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ):
                with self.assertRaises(manager.ManagerError) as raised:
                    manager.repair(
                        paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader()
                    )
            self.assertEqual(raised.exception.code, "conflict")
            for path, data in before_repair.items():
                self.assertEqual(path.read_bytes(), data)
            backups_after = _snapshot(backups_dir) if backups_dir.exists() else {}
            self.assertEqual(backups_after, backups_before)

    def test_setup_rejects_drift_before_storing_new_credential(self):
        manager = self._manager()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ):
                manager.setup(paths, True, False, True, io.StringIO("ignored\n"), catalog_loader=_catalog_loader())
            managed = (
                paths.config,
                paths.catalog,
                paths.agent,
                paths.manifest,
                paths.state_dir / "state.json",
            )
            backups_dir = paths.state_dir / "backups"
            backups_before = _snapshot(backups_dir) if backups_dir.exists() else {}
            paths.catalog.write_bytes(paths.catalog.read_bytes() + b"\n# user drift\n")
            before = {path: path.read_bytes() for path in managed}
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=False
            ), mock.patch.object(manager, "store_credential_key") as store, mock.patch.object(
                manager, "remove_credential_key"
            ) as remove:
                with self.assertRaises(manager.ManagerError) as raised:
                    manager.setup(
                        paths, True, False, True, io.StringIO(SECRET_MARKER + "\n"), catalog_loader=_catalog_loader()
                    )
            self.assertEqual(raised.exception.code, "conflict")
            store.assert_not_called()
            remove.assert_not_called()
            for path, data in before.items():
                self.assertEqual(path.read_bytes(), data)
            backups_after = _snapshot(backups_dir) if backups_dir.exists() else {}
            self.assertEqual(backups_after, backups_before)
            marker_bytes = SECRET_MARKER.encode("utf-8")
            for file_path in _all_files(home):
                self.assertNotIn(marker_bytes, file_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
