"""RED behavior contract tests for pure models refresh (DS-20260811-17 REV-2).

Tests prove that the reviewed snapshot / reconciliation / registry-aware
lookup API decided in DEC-20260811-17-V1 is not implemented yet.  They load
opencode_models.py dynamically, use only the standard library, touch no
network/files/credentials/subprocess, and fail with assertion guards instead
of ImportError so the current production module yields FAIL (not ERROR).
"""

from __future__ import annotations

import copy
import dataclasses
import importlib.util
import inspect
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODELS_PATH = _REPO_ROOT / "codex-opencode-go-subagent" / "scripts" / "opencode_models.py"

_MODEL_SPEC_FIELDS = (
    "id", "name", "transport", "efforts", "default_effort",
    "tool_call", "context_window", "max_output", "status", "source_revision",
)

_REPORT_KEYS = frozenset({
    "unknown_go_models", "unknown_metadata_models",
    "missing_go_models", "conflicts", "updated_models",
})

_QWEN_IDS = frozenset({
    "qwen3.6-plus", "qwen3.7-max", "qwen3.7-plus", "qwen3.8-max",
})


def _load_models_module():
    if not _MODELS_PATH.is_file():
        raise AssertionError(f"opencode_models.py missing at {_MODELS_PATH}")
    spec = importlib.util.spec_from_file_location(
        "opencode_models_refresh_under_test", _MODELS_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot create import spec for opencode_models.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _require_callable(module, name):
    if not callable(getattr(module, name, None)):
        raise AssertionError(f"{name} must be callable")


def _require_parameter(fn, name):
    if name not in inspect.signature(fn).parameters:
        raise AssertionError(
            f"{name} must be a parameter of {getattr(fn, '__name__', 'function')}")


def _canonical_ids(module):
    return sorted(module.MODELS)


def _go_payload(module, *, missing=(), extra=("unknown-go-a", "unknown-go-b")):
    ids = sorted((set(_canonical_ids(module)) | set(extra)) - set(missing))
    return {"object": "list", "data": [{"id": model_id} for model_id in ids]}


def _metadata_for(module, model_id, **overrides):
    spec = module.MODELS[model_id]
    meta = {
        "id": model_id,
        "limit": {"context": spec.context_window + 1000, "output": spec.max_output + 500},
        "tool_call": True,
        "last_updated": "2026-08-10",
    }
    if spec.transport == "responses":
        meta["provider"] = {"npm": "@ai-sdk/openai"}
    elif spec.transport == "anthropic_messages":
        meta["provider"] = {"npm": "@ai-sdk/anthropic"}
    if spec.efforts == ("default",):
        meta["reasoning_options"] = None
    elif model_id == "minimax-m3":
        meta["reasoning_options"] = [{"type": "toggle"}]
    elif model_id in _QWEN_IDS:
        meta["reasoning_options"] = [
            {"type": "toggle"},
            {"type": "budget_tokens", "max": 131072},
        ]
    else:
        meta["reasoning_options"] = [
            {"type": "effort", "values": list(spec.efforts)}]
    meta.update(overrides)
    return meta


def _unknown_metadata(model_id):
    return {
        "id": model_id,
        "limit": {"context": 1, "output": 1},
        "tool_call": True,
        "last_updated": "2026-08-10",
        "reasoning_options": None,
    }


def _models_dev_payload(module, ids=None, extra=("unknown-meta-b", "unknown-meta-c")):
    model_ids = set(_canonical_ids(module) if ids is None else ids)
    payload = {}
    for model_id in sorted(model_ids):
        payload[model_id] = _metadata_for(module, model_id)
    for model_id in sorted(set(extra)):
        payload[model_id] = _unknown_metadata(model_id)
    return {"opencode-go": {"models": payload}}


class ModelsRefreshRedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = _load_models_module()

    def _snapshot(self):
        _require_callable(self.models, "registry_snapshot")
        return self.models.registry_snapshot()

    def _restore(self, payload):
        _require_callable(self.models, "registry_from_snapshot")
        return self.models.registry_from_snapshot(payload)

    # R1: snapshot roundtrip, stable order, no shared mutable state, no regression.
    def test_snapshot_round_trip_and_order(self):
        models = self.models
        original = copy.deepcopy(models.MODELS)
        payload = self._snapshot()
        self.assertEqual(payload["schema_version"], 1)
        records = payload["models"]
        self.assertEqual(len(records), 18)
        self.assertEqual(
            [record["id"] for record in records], _canonical_ids(models))
        self.assertEqual(
            [set(record) for record in records],
            [set(_MODEL_SPEC_FIELDS) for _ in records],
        )
        for record, model_id in zip(records, _canonical_ids(models)):
            spec = models.MODELS[model_id]
            self.assertEqual(record["name"], spec.name)
            self.assertEqual(record["transport"], spec.transport)
            self.assertEqual(record["efforts"], list(spec.efforts))
            self.assertEqual(record["default_effort"], spec.default_effort)
            self.assertEqual(record["tool_call"], spec.tool_call)
            self.assertEqual(record["context_window"], spec.context_window)
            self.assertEqual(record["max_output"], spec.max_output)
            self.assertEqual(record["status"], spec.status)
            self.assertEqual(record["source_revision"], spec.source_revision)
        restored = self._restore(payload)
        self.assertIsNot(restored, models.MODELS)
        self.assertEqual(set(restored), set(models.MODELS))
        for model_id, spec in models.MODELS.items():
            other = restored[model_id]
            self.assertIsNot(other, spec)
            self.assertEqual(dataclasses.asdict(other), dataclasses.asdict(spec))
            self.assertIsInstance(other.efforts, tuple)
        payload["models"][0]["efforts"].append("max")
        first_id = payload["models"][0]["id"]
        self.assertEqual(
            restored[first_id].efforts, models.MODELS[first_id].efforts)
        self.assertEqual(models.MODELS, original)
        self.assertEqual(
            models.get_model(models.DEFAULT_MODEL, models.DEFAULT_EFFORT),
            models.MODELS[models.DEFAULT_MODEL],
        )

    # R1: every invalid snapshot dimension raises snapshot_invalid (400).
    def test_snapshot_rejects_schema_ids_transport_status_effort_limits(self):
        models = self.models
        valid = self._snapshot()
        records = valid["models"]

        def mutate(index, **changes):
            payload = copy.deepcopy(valid)
            payload["models"] = copy.deepcopy(records)
            payload["models"][index].update(changes)
            return payload

        cases = [
            ("schema_version=2", {**copy.deepcopy(valid), "schema_version": 2}),
            ("id empty", mutate(0, id="")),
            ("unknown id", mutate(0, id="never-reviewed")),
            ("transport drift", mutate(0, transport="bogus")),
            ("status invalid", mutate(0, status="disabled")),
            ("efforts empty", mutate(0, efforts=[])),
            ("efforts duplicate", mutate(0, efforts=["max", "max"])),
            ("efforts unsupported", mutate(0, efforts=["turbo"])),
            ("efforts unhashable", mutate(0, efforts=[{}])),
            ("default not in efforts", mutate(0, efforts=["none"], default_effort="max")),
            ("context bool", mutate(0, context_window=True)),
            ("max_output zero", mutate(0, max_output=0)),
            ("tool_call non-bool", mutate(0, tool_call="yes")),
            ("source_revision empty", mutate(0, source_revision="")),
        ]
        for label, payload in cases:
            with self.subTest(label=label):
                try:
                    with self.assertRaises(models.ModelError) as ctx:
                        self._restore(payload)
                except Exception as exc:
                    self.fail(f"malformed snapshot escaped as {type(exc).__name__}")
                self.assertEqual(ctx.exception.code, "snapshot_invalid")
                self.assertEqual(ctx.exception.status, 400)
        duplicated = copy.deepcopy(valid)
        duplicated["models"].append(copy.deepcopy(records[0]))
        with self.assertRaises(models.ModelError) as ctx:
            self._restore(duplicated)
        self.assertEqual(ctx.exception.code, "snapshot_invalid")
        missing = copy.deepcopy(valid)
        missing["models"] = [
            record for record in records if record["id"] != _canonical_ids(models)[0]]
        with self.assertRaises(models.ModelError) as ctx:
            self._restore(missing)
        self.assertEqual(ctx.exception.code, "snapshot_invalid")

    # R2/R3: safe updates, four reasoning families, unknown reporting, missing -> unavailable.
    def test_reconcile_updates_safe_fields_reports_unknown_and_marks_missing_unavailable(self):
        models = self.models
        _require_callable(models, "reconcile_registry")
        canonical = _canonical_ids(models)
        go_payload = _go_payload(models)
        md_payload = _models_dev_payload(models)
        current = copy.deepcopy(models.MODELS)
        go_before = copy.deepcopy(go_payload)
        md_before = copy.deepcopy(md_payload)
        registry, report = models.reconcile_registry(
            go_payload, md_payload, current=current)
        self.assertIsNot(registry, current)
        self.assertEqual(set(report), _REPORT_KEYS)
        self.assertEqual(report["unknown_go_models"], ["unknown-go-a", "unknown-go-b"])
        self.assertEqual(
            report["unknown_metadata_models"], ["unknown-meta-b", "unknown-meta-c"])
        self.assertEqual(report["missing_go_models"], [])
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["updated_models"], canonical)
        for model_id in ("unknown-go-a", "unknown-go-b", "unknown-meta-b", "unknown-meta-c"):
            self.assertNotIn(model_id, registry)
        for model_id in canonical:
            spec = registry[model_id]
            prior = models.MODELS[model_id]
            self.assertEqual(spec.status, "active")
            self.assertEqual(spec.context_window, prior.context_window + 1000)
            self.assertEqual(spec.max_output, prior.max_output + 500)
            self.assertEqual(spec.source_revision, "2026-08-10")
            self.assertEqual(spec.transport, prior.transport)
            self.assertEqual(spec.name, prior.name)
            self.assertEqual(spec.default_effort, prior.default_effort)
            self.assertTrue(spec.tool_call)
            if prior.efforts == ("default",):
                self.assertEqual(spec.efforts, ("default",))
            elif model_id == "minimax-m3":
                self.assertEqual(spec.efforts, ("none", "high"))
            elif model_id in _QWEN_IDS:
                self.assertEqual(spec.efforts, ("none", "high", "max"))
            else:
                self.assertEqual(spec.efforts, prior.efforts)
        self.assertEqual(go_payload, go_before)
        self.assertEqual(md_payload, md_before)
        self.assertEqual(current, models.MODELS)
        # Non-selected reviewed model absent from Go becomes unavailable only.
        go_missing = _go_payload(models, missing=("hy3",), extra=())
        md_missing = _models_dev_payload(models, ids=set(canonical) - {"hy3"})
        registry2, report2 = models.reconcile_registry(
            go_missing, md_missing, current=current)
        self.assertEqual(report2["missing_go_models"], ["hy3"])
        self.assertIn("hy3", report2["updated_models"])
        prior = models.MODELS["hy3"]
        self.assertEqual(registry2["hy3"].status, "unavailable")
        self.assertEqual(registry2["hy3"].efforts, prior.efforts)
        self.assertEqual(registry2["hy3"].context_window, prior.context_window)
        self.assertEqual(registry2["hy3"].max_output, prior.max_output)
        self.assertEqual(registry2["hy3"].source_revision, prior.source_revision)

    # R2: selected missing is fatal; malformed payloads and effort misuse are rejected.
    def test_reconcile_selected_missing_is_fatal(self):
        models = self.models
        _require_callable(models, "reconcile_registry")
        current = copy.deepcopy(models.MODELS)
        selected = "deepseek-v4-flash"
        go_missing = _go_payload(models, missing=(selected,), extra=())
        md_full = _models_dev_payload(models)
        go_before = copy.deepcopy(go_missing)
        with self.assertRaises(models.ModelError) as ctx:
            models.reconcile_registry(
                go_missing, md_full, current=current,
                selected_model=selected, selected_effort=models.DEFAULT_EFFORT)
        self.assertEqual(ctx.exception.code, "selected_model_missing")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(go_missing, go_before)
        self.assertEqual(current, models.MODELS)
        with self.assertRaises(models.ModelError) as ctx:
            models.reconcile_registry(
                go_missing, md_full, current=current, selected_model="not-reviewed")
        self.assertEqual(ctx.exception.code, "model_not_found")
        with self.assertRaises(models.ModelError) as ctx:
            models.reconcile_registry(
                _go_payload(models), md_full, current=current, selected_effort="max")
        self.assertEqual(ctx.exception.code, "refresh_payload_invalid")
        self.assertEqual(ctx.exception.status, 400)
        malformed = [
            ("go not dict", "not-a-dict", md_full),
            ("go object wrong", {"object": "model", "data": []}, md_full),
            ("go data not list", {"object": "list", "data": {}}, md_full),
            ("go duplicate ids", {"object": "list", "data": [{"id": "x"}, {"id": "x"}]}, md_full),
            ("md not dict", _go_payload(models), []),
            ("md missing provider", _go_payload(models), {}),
            ("md models not dict", _go_payload(models), {"opencode-go": {"models": []}}),
            ("md key id mismatch", _go_payload(models), {"opencode-go": {"models": {"a": {"id": "b"}}}}),
        ]
        for label, bad_go, bad_md in malformed:
            with self.subTest(label=label):
                with self.assertRaises(models.ModelError) as ctx:
                    models.reconcile_registry(bad_go, bad_md, current=current)
                self.assertEqual(ctx.exception.code, "refresh_payload_invalid")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(current, models.MODELS)

    # R3: fixed-order conflicts preserve prior fields and are sorted; inputs stay untouched.
    def test_reconcile_conflicts_preserve_previous_fields_and_are_sorted(self):
        models = self.models
        _require_callable(models, "reconcile_registry")
        current = copy.deepcopy(models.MODELS)

        def reconcile_with(metadata_overrides):
            go = _go_payload(models, extra=())
            md = _models_dev_payload(models, extra=())
            for model_id, overrides in metadata_overrides.items():
                md["opencode-go"]["models"][model_id].update(overrides)
            return models.reconcile_registry(go, md, current=current)

        conflict_cases = [
            ("transport_conflict", "gpt-5.6-luna", {"provider": {"npm": "@ai-sdk/anthropic"}}),
            ("limits_invalid", "glm-5.2", {"limit": {"context": True, "output": 1}}),
            ("tool_call_conflict", "hy3", {"tool_call": False}),
            ("source_revision_missing", "kimi-k3", {"last_updated": ""}),
            ("reasoning_conflict", "minimax-m2.7", {"reasoning_options": [{"type": "effort", "values": ["max"]}]}),
            ("reasoning_conflict", "deepseek-v4-flash", {"reasoning_options": [{"type": "effort", "values": ["none", "medium"]}]}),
            ("reasoning_conflict", "grok-4.5", {"reasoning_options": [{"type": "effort", "values": ["high", "high"]}]}),
            ("reasoning_conflict", "minimax-m3", {"reasoning_options": [{"type": "toggle"}, {"type": "toggle"}]}),
            ("reasoning_conflict", "qwen3.7-max", {"reasoning_options": [{"type": "toggle"}, {"type": "budget_tokens", "max": 0}]}),
            ("reasoning_conflict", "qwen3.8-max", {"reasoning_options": [{"type": None}, {"type": "toggle"}]}),
        ]
        for code, model_id, overrides in conflict_cases:
            with self.subTest(code=code, model=model_id):
                try:
                    registry, report = reconcile_with({model_id: overrides})
                except Exception as exc:
                    self.fail(f"malformed metadata escaped as {type(exc).__name__}")
                self.assertEqual(report["conflicts"], [{"model": model_id, "code": code}])
                spec = registry[model_id]
                prior = models.MODELS[model_id]
                self.assertEqual(spec.status, "active")
                self.assertEqual(spec.transport, prior.transport)
                self.assertEqual(spec.name, prior.name)
                self.assertEqual(spec.default_effort, prior.default_effort)
                self.assertEqual(spec.tool_call, prior.tool_call)
                self.assertEqual(spec.context_window, prior.context_window)
                self.assertEqual(spec.max_output, prior.max_output)
                self.assertEqual(spec.efforts, prior.efforts)
                self.assertEqual(spec.source_revision, prior.source_revision)
        registry, report = reconcile_with({
            "gpt-5.6-luna": {
                "provider": {"npm": "@ai-sdk/anthropic"},
                "limit": {"context": True, "output": 1},
            },
        })
        self.assertEqual(
            report["conflicts"], [{"model": "gpt-5.6-luna", "code": "transport_conflict"}])
        registry, report = reconcile_with({
            "hy3": {"tool_call": False},
            "gpt-5.6-luna": {"provider": {"npm": "@ai-sdk/anthropic"}},
        })
        self.assertEqual(
            report["conflicts"],
            [
                {"model": "gpt-5.6-luna", "code": "transport_conflict"},
                {"model": "hy3", "code": "tool_call_conflict"},
            ],
        )
        go = _go_payload(models, extra=())
        md = _models_dev_payload(models, extra=())
        md["opencode-go"]["models"]["grok-4.5"]["reasoning_options"] = [
            {"type": "effort", "values": ["none", "medium"]}]
        registry, report = models.reconcile_registry(
            go, md, current=current,
            selected_model="grok-4.5", selected_effort="high")
        self.assertEqual(
            report["conflicts"], [{"model": "grok-4.5", "code": "reasoning_conflict"}])
        self.assertEqual(registry["grok-4.5"].efforts, models.MODELS["grok-4.5"].efforts)
        self.assertEqual(current, models.MODELS)

    # R4: registry-aware lookup keeps default behavior and rejects unavailable models.
    def test_registry_aware_get_model_rejects_unavailable_and_keeps_default_behavior(self):
        models = self.models
        _require_parameter(models.get_model, "registry")
        _require_parameter(models.validate_profile, "registry")
        registry = copy.deepcopy(models.MODELS)
        registry["hy3"] = dataclasses.replace(registry["hy3"], status="unavailable")
        with self.assertRaises(models.ModelError) as ctx:
            models.get_model("hy3", registry=registry)
        self.assertEqual(ctx.exception.code, "model_unavailable")
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(models.ModelError) as ctx:
            models.get_model("hy3", "high", registry=registry)
        self.assertEqual(ctx.exception.code, "model_unavailable")
        with self.assertRaises(models.ModelError) as ctx:
            models.get_model("no-such-model", registry=registry)
        self.assertEqual(ctx.exception.code, "model_not_found")
        with self.assertRaises(models.ModelError) as ctx:
            models.get_model("glm-5.1", "max", registry=registry)
        self.assertEqual(ctx.exception.code, "invalid_effort")
        with self.assertRaises(models.ModelError) as ctx:
            models.validate_profile("hy3", "high", registry=registry)
        self.assertEqual(ctx.exception.code, "model_unavailable")
        self.assertTrue(models.validate_profile(models.DEFAULT_MODEL, models.DEFAULT_EFFORT))
        self.assertEqual(
            models.get_model(models.DEFAULT_MODEL, models.DEFAULT_EFFORT, None),
            models.MODELS[models.DEFAULT_MODEL],
        )
        self.assertEqual(models.get_model("glm-5.1"), models.MODELS["glm-5.1"])
