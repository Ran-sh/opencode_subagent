#!/usr/bin/env python3
"""RED contract tests for the Codex catalog bridge (DS-20260811-15 REV-2).
Each test FAILs exactly once on the current manager: missing callables, missing
strict template rejection, or capability mismatch. No real Codex, credential,
network, or background process runs."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "codex-opencode-go-subagent"
    / "scripts"
    / "codex_opencode_go.py"
)
MODELS_SCRIPT = SCRIPT.parent / "opencode_models.py"
SECRET_MARKER = "sk-" + "opencode-red-secret-marker"


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


def valid_template() -> dict:
    """Full current-Codex-like record used as the unrelated parent template."""
    return {
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


class CodexCatalogContractTests(unittest.TestCase):
    def _manager(self):
        try:
            return _load_manager()
        except FileNotFoundError:
            self.fail("manager_missing")

    def test_desktop_override_success_missing_and_unusable_are_deterministic(self):
        manager = self._manager()
        find = getattr(manager, "find_desktop_codex", None)
        self.assertTrue(callable(find), "missing callable find_desktop_codex")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_bin = root / "codex.exe"
            codex_bin.write_bytes(b"fake desktop codex\n")
            ok = types.SimpleNamespace(returncode=0, stdout="Codex 0.147.0\n", stderr="")
            with mock.patch.dict(os.environ, {"CODEX_DESKTOP_BIN": str(codex_bin)}), mock.patch.object(
                manager.subprocess, "run", return_value=ok
            ) as run_mock:
                self.assertEqual(find(), str(codex_bin.resolve()))
                self.assertEqual(run_mock.call_args.args[0], [str(codex_bin.resolve()), "--version"])
            bad = types.SimpleNamespace(returncode=1, stdout="", stderr=SECRET_MARKER)
            with mock.patch.dict(os.environ, {"CODEX_DESKTOP_BIN": str(codex_bin)}), mock.patch.object(
                manager.subprocess, "run", return_value=bad
            ):
                with self.assertRaises(manager.ManagerError) as raised:
                    find()
                self.assertEqual(raised.exception.code, "desktop_codex_unusable")
                self.assertNotIn(SECRET_MARKER, str(raised.exception) + str(raised.exception.details))
            missing = root / "missing.exe"
            with mock.patch.dict(os.environ, {"CODEX_DESKTOP_BIN": str(missing)}):
                with self.assertRaises(manager.ManagerError) as raised:
                    find()
                self.assertEqual(raised.exception.code, "desktop_codex_missing")
                self.assertNotIn(SECRET_MARKER, str(raised.exception) + str(raised.exception.details))
    def test_desktop_windows_and_macos_candidates_are_ordered_and_runnable(self):
        manager = self._manager()
        find = getattr(manager, "find_desktop_codex", None)
        self.assertTrue(callable(find), "missing callable find_desktop_codex")
        candidates = getattr(manager, "_windows_bundled_codex_candidates", None)
        self.assertTrue(callable(candidates), "missing callable _windows_bundled_codex_candidates")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_root = root / "OpenAI" / "Codex" / "bin"
            base = bin_root / "codex.exe"
            old = bin_root / "old" / "codex.exe"
            new = bin_root / "new" / "codex.exe"
            tied_a = bin_root / "a" / "codex.exe"
            tied_b = bin_root / "b" / "codex.exe"
            for path, stamp in ((base, 1000), (old, 2000), (new, 3000), (tied_a, 4000), (tied_b, 4000)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
                os.utime(path, (stamp, stamp))
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root)}):
                ordered = candidates()
            self.assertEqual(
                ordered,
                [tied_a.resolve(), tied_b.resolve(), new.resolve(), old.resolve(), base.resolve()],
            )
            macos_candidates = (root / "ChatGPT.app" / "Contents" / "Resources" / "codex", root / "Codex.app" / "Contents" / "Resources" / "codex")
            for path in macos_candidates:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
            ok = types.SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
            with mock.patch.object(manager, "platform_name", return_value="macos"), mock.patch.object(
                manager, "DESKTOP_CODEX_CANDIDATES", macos_candidates
            ), mock.patch.object(manager.subprocess, "run", return_value=ok) as run_mock:
                self.assertEqual(find(), str(macos_candidates[0].resolve()))
                self.assertEqual(run_mock.call_args.args[0][:2], [str(macos_candidates[0].resolve()), "--version"])
            fail = types.SimpleNamespace(returncode=1, stdout="", stderr="bad")
            with mock.patch.object(manager, "platform_name", return_value="macos"), mock.patch.object(
                manager, "DESKTOP_CODEX_CANDIDATES", macos_candidates
            ), mock.patch.object(manager.subprocess, "run", side_effect=[fail, ok]) as run_mock:
                self.assertEqual(find(), str(macos_candidates[1].resolve()))
                self.assertEqual(run_mock.call_count, 2)
    def test_run_codex_models_uses_bundled_utf8_contract_and_sanitizes_errors(self):
        manager = self._manager()
        run = getattr(manager, "run_codex_models", None)
        self.assertTrue(callable(run), "missing callable run_codex_models")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            codex_bin = "C:/fake/codex.exe"
            payload = {"models": [{"slug": "gpt-5.2", "display_name": "GPT-5.2"}]}
            ok = types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
            with mock.patch.object(manager.subprocess, "run", return_value=ok) as run_mock:
                self.assertEqual(run(codex_bin, home), payload)
                self.assertEqual(run_mock.call_args.args[0], [codex_bin, "debug", "models", "--bundled"])
                kwargs = run_mock.call_args.kwargs
                self.assertIs(kwargs.get("capture_output"), True)
                self.assertIs(kwargs.get("text"), True)
                self.assertEqual(kwargs.get("encoding"), "utf-8")
                self.assertEqual(kwargs.get("errors"), "replace")
                self.assertEqual(kwargs.get("timeout"), 45)
                env = kwargs.get("env")
                self.assertIsNot(env, os.environ)
                self.assertEqual(env.get("CODEX_HOME"), str(home))
            failures = (
                types.SimpleNamespace(returncode=2, stdout='{"models": []}', stderr=SECRET_MARKER),
                types.SimpleNamespace(returncode=0, stdout="not json", stderr=SECRET_MARKER),
                types.SimpleNamespace(returncode=0, stdout='{"models": []}', stderr=SECRET_MARKER),
            )
            for fake in failures:
                with mock.patch.object(manager.subprocess, "run", return_value=fake):
                    with self.assertRaises(manager.ManagerError) as raised:
                        run(codex_bin, home)
                    self.assertIn(raised.exception.code, {"codex_catalog_failed", "codex_catalog_invalid"})
                    self.assertNotIn(SECRET_MARKER, str(raised.exception) + str(raised.exception.details))
    def test_load_base_catalog_precedence_and_clean_discovery(self):
        manager = self._manager()
        run = getattr(manager, "run_codex_models", None)
        self.assertTrue(callable(run), "missing callable run_codex_models")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = manager.resolve_paths(str(root))
            configured = root / "configured.json"
            configured_data = {"models": [{"slug": "external-model", "name": "External"}]}
            configured.write_text(json.dumps(configured_data), encoding="utf-8")
            with mock.patch.object(manager.subprocess, "run") as run_mock:
                result = manager.load_base_catalog(paths, f'model_catalog_json = "{configured.as_posix()}"', None)
                self.assertEqual(result, configured_data)
                run_mock.assert_not_called()
            injected = {"models": [{"slug": "injected-model"}]}
            with mock.patch.object(manager.subprocess, "run") as run_mock:
                self.assertEqual(manager.load_base_catalog(paths, "", lambda: injected), injected)
                run_mock.assert_not_called()
            bundled = {"models": [{"slug": "bundled-model"}]}
            with mock.patch.object(manager, "find_desktop_codex", return_value="C:/fake/codex.exe") as find_mock, mock.patch.object(
                manager, "run_codex_models", create=True, return_value=bundled
            ) as run_mock, mock.patch.object(manager.subprocess, "run") as sub_mock:
                self.assertEqual(manager.load_base_catalog(paths, "", None), bundled)
                find_mock.assert_called_once_with()
                run_mock.assert_called_once()
                sub_mock.assert_not_called()

    def test_merge_catalog_rejects_incomplete_or_invalid_templates(self):
        manager = self._manager()
        merge = getattr(manager, "merge_catalog", None)
        self.assertTrue(callable(merge), "missing callable merge_catalog")
        missing_instructions = {
            "models": [
                {
                    "slug": "unrelated-parent",
                    "name": "Unrelated Parent",
                    "input_modalities": ["text"],
                    "shell_type": "shell_command",
                    "apply_patch_tool_type": "freeform",
                    "multi_agent_version": "v9",
                }
            ]
        }
        with self.assertRaises(manager.ManagerError) as raised:
            merge(missing_instructions)
        self.assertEqual(raised.exception.code, "codex_catalog_invalid")
        read_only_template = {
            "models": [
                {
                    "slug": "unrelated-parent",
                    "name": "Unrelated Parent",
                    "input_modalities": ["text"],
                    "shell_type": "read-only",
                    "apply_patch_tool_type": "read-only",
                    "visibility": "internal",
                    "base_instructions": "instructions",
                    "multi_agent_version": "v9",
                }
            ]
        }
        with self.assertRaises(manager.ManagerError) as raised:
            merge(read_only_template)
        self.assertEqual(raised.exception.code, "codex_catalog_invalid")

    def test_merge_catalog_preserves_source_and_overrides_all_capabilities(self):
        manager = self._manager()
        models = _load_opencode_models()
        template = valid_template()
        stale = dict(template)
        stale.update(slug="deepseek-v4-flash", name="Stale DeepSeek V4 Flash", shell_type="read-only", apply_patch_tool_type="read-only", tool_mode="code_mode")
        merged = manager.merge_catalog({"models": [template, stale]})
        preserved = next(item for item in merged["models"] if item["slug"] == "unrelated-parent")
        self.assertEqual(preserved, template)
        slugs = [item["slug"] for item in merged["models"]]
        self.assertEqual(len(slugs), len(models.MODELS) + 1)
        self.assertEqual(slugs.count("deepseek-v4-flash"), 1)
        self.assertEqual(slugs, sorted(slugs))
        by_slug = {item["slug"]: item for item in merged["models"]}
        self.assertEqual(set(by_slug), set(models.MODELS) | {"unrelated-parent"})
        for slug, spec in models.MODELS.items():
            record = by_slug[slug]
            self.assertIn("OpenCode Go", str(record.get("description", "")))
            self.assertEqual(record.get("base_instructions"), template["base_instructions"])
            self.assertEqual(record.get("model_messages"), template["model_messages"])
            self.assertEqual(record.get("truncation_policy"), template["truncation_policy"])
            self.assertEqual(record.get("unknown_future"), {"preserve": True})
            self.assertEqual(record.get("shell_type"), "shell_command")
            self.assertEqual(record.get("apply_patch_tool_type"), "freeform")
            self.assertEqual(record.get("tool_mode"), "direct")
            self.assertEqual(record.get("additional_speed_tiers"), [])
            self.assertEqual(record.get("service_tiers"), [])
            self.assertIsNone(record.get("default_service_tier"))
            self.assertIsNone(record.get("availability_nux"))
            self.assertIsNone(record.get("upgrade"))
            self.assertIs(record.get("supports_reasoning_summary_parameter"), False)
            self.assertEqual(record.get("default_reasoning_summary"), "none")
            self.assertIs(record.get("support_verbosity"), False)
            self.assertIsNone(record.get("default_verbosity"))
            self.assertEqual(record.get("web_search_tool_type"), "text")
            self.assertIs(record.get("supports_parallel_tool_calls"), True)
            self.assertEqual(record.get("context_window"), spec.context_window)
            self.assertEqual(record.get("max_context_window"), spec.context_window)
            self.assertIsNone(record.get("auto_compact_token_limit"))
            self.assertIsNone(record.get("comp_hash"))
            self.assertEqual(record.get("experimental_supported_tools"), [])
            self.assertEqual(record.get("input_modalities"), ["text"])
            self.assertIs(record.get("supports_search_tool"), False)
            self.assertIs(record.get("supports_image_detail_original"), False)
            self.assertIs(record.get("use_responses_lite"), False)
            self.assertEqual(record.get("multi_agent_version"), "v1")
            levels = record.get("supported_reasoning_levels")
            self.assertTrue(isinstance(levels, list) and levels)
            self.assertTrue(all(isinstance(level, dict) and level.get("effort") for level in levels))
            x = record.get("x_opencode_go", {})
            self.assertEqual(x.get("transport"), spec.transport)
            self.assertEqual(x.get("max_output"), spec.max_output)
            self.assertEqual(x.get("source_revision"), spec.source_revision)
            self.assertEqual(x.get("tool_call"), spec.tool_call)

    def test_clean_setup_uses_discovered_catalog_without_secret_or_real_process(self):
        manager = self._manager()
        run = getattr(manager, "run_codex_models", None)
        self.assertTrue(callable(run), "missing callable run_codex_models")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = manager.resolve_paths(str(home))
            bundled = {"models": [valid_template()]}
            with mock.patch.object(manager, "credential_available", return_value=True), mock.patch.object(
                manager, "credential_has_key", return_value=True
            ), mock.patch.object(manager, "store_credential_key") as store_mock, mock.patch.object(
                manager, "find_desktop_codex", return_value="C:/fake/codex.exe"
            ) as find_mock, mock.patch.object(
                manager, "run_codex_models", return_value=bundled
            ) as run_mock, mock.patch.object(manager.subprocess, "run") as sub_mock:
                result = manager.setup(paths, False, False, True, None, catalog_loader=None)
            self.assertEqual(result["status"], "configured")
            find_mock.assert_called_once_with()
            run_mock.assert_called_once()
            sub_mock.assert_not_called()
            store_mock.assert_not_called()
            catalog = json.loads(paths.catalog.read_text(encoding="utf-8"))
            slugs = {item["slug"] for item in catalog["models"]}
            models = _load_opencode_models()
            self.assertEqual(slugs, set(models.MODELS) | {"unrelated-parent"})
            marker_bytes = SECRET_MARKER.encode("utf-8")
            for file_path in sorted(home.rglob("*")):
                if file_path.is_file():
                    self.assertNotIn(marker_bytes, file_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
