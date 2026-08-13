"""Frozen 19-model catalog for the OpenCode Go protocol core (DP-20260811-03-V1).

Pure data/validation module: no HTTP, credentials, configuration or state.
"""

from dataclasses import asdict, dataclass, fields, replace


class ModelError(Exception):
    """Structured, secret-free model validation error."""

    def __init__(self, code, status, message):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    transport: str
    efforts: tuple
    default_effort: str
    tool_call: bool
    context_window: int
    max_output: int
    status: str
    source_revision: str


MODELS = {
    "deepseek-v4-flash": ModelSpec(
        "deepseek-v4-flash", "DeepSeek V4 Flash", "chat_completions",
        ("low", "high", "max"), "max", True, 1000000, 384000, "active", "2026-08-13",
    ),
    "deepseek-v4-pro": ModelSpec(
        "deepseek-v4-pro", "DeepSeek V4 Pro", "chat_completions",
        ("high", "max"), "max", True, 1000000, 384000, "active", "2026-08-13",
    ),
    "glm-5.1": ModelSpec(
        "glm-5.1", "GLM-5.1", "chat_completions",
        ("default",), "default", True, 202752, 32768, "active", "2026-08-13",
    ),
    "glm-5.2": ModelSpec(
        "glm-5.2", "GLM-5.2", "chat_completions",
        ("high", "max"), "max", True, 1000000, 131072, "active", "2026-08-13",
    ),
    "gpt-5.6-luna": ModelSpec(
        "gpt-5.6-luna", "GPT 5.6 Luna", "responses",
        ("none", "low", "medium", "high", "xhigh", "max"), "max", True,
        1050000, 128000, "active", "2026-08-13",
    ),
    "grok-4.5": ModelSpec(
        "grok-4.5", "Grok 4.5", "chat_completions",
        ("low", "medium", "high"), "high", True, 500000, 500000, "active", "2026-08-13",
    ),
    "hy3": ModelSpec(
        "hy3", "Hy3", "chat_completions",
        ("none", "low", "high"), "high", True, 256000, 64000, "active", "2026-08-13",
    ),
    "kimi-k2.6": ModelSpec(
        "kimi-k2.6", "Kimi K2.6", "chat_completions",
        ("default",), "default", True, 262144, 65536, "active", "2026-08-13",
    ),
    "kimi-k2.7-code": ModelSpec(
        "kimi-k2.7-code", "Kimi K2.7 Code", "chat_completions",
        ("default",), "default", True, 262144, 262144, "active", "2026-08-13",
    ),
    "kimi-k3": ModelSpec(
        "kimi-k3", "Kimi K3", "chat_completions",
        ("max",), "max", True, 1048576, 131072, "active", "2026-08-13",
    ),
    "mimo-v2.5": ModelSpec(
        "mimo-v2.5", "MiMo V2.5", "chat_completions",
        ("default",), "default", True, 1000000, 128000, "active", "2026-08-13",
    ),
    "mimo-v2.5-pro": ModelSpec(
        "mimo-v2.5-pro", "MiMo V2.5 Pro", "chat_completions",
        ("default",), "default", True, 1048576, 128000, "active", "2026-08-13",
    ),
    "minimax-m2.5": ModelSpec(
        "minimax-m2.5", "MiniMax M2.5", "anthropic_messages",
        ("default",), "default", True, 204800, 65536, "active", "2026-08-13",
    ),
    "minimax-m2.7": ModelSpec(
        "minimax-m2.7", "MiniMax M2.7", "anthropic_messages",
        ("default",), "default", True, 204800, 131072, "active", "2026-08-13",
    ),
    "minimax-m3": ModelSpec(
        "minimax-m3", "MiniMax M3", "anthropic_messages",
        ("none", "high"), "high", True, 1000000, 131072, "active", "2026-08-13",
    ),
    "qwen3.6-plus": ModelSpec(
        "qwen3.6-plus", "Qwen3.6 Plus", "anthropic_messages",
        ("none", "high", "max"), "max", True, 1000000, 65536, "active", "2026-08-13",
    ),
    "qwen3.7-max": ModelSpec(
        "qwen3.7-max", "Qwen3.7 Max", "anthropic_messages",
        ("none", "high", "max"), "max", True, 1000000, 65536, "active", "2026-08-13",
    ),
    "qwen3.7-plus": ModelSpec(
        "qwen3.7-plus", "Qwen3.7 Plus", "anthropic_messages",
        ("none", "high", "max"), "max", True, 1000000, 65536, "active", "2026-08-13",
    ),
    "qwen3.8-max": ModelSpec(
        "qwen3.8-max", "Qwen3.8 Max", "anthropic_messages",
        ("none", "high", "max"), "max", True, 1000000, 131072, "active", "2026-08-13",
    ),
}

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_EFFORT = "max"

SNAPSHOT_SCHEMA_VERSION = 1
_ALLOWED_EFFORTS = frozenset({"default", "none", "low", "medium", "high", "xhigh", "max"})
_ALLOWED_STATUSES = frozenset({"active", "unavailable"})
_QWEN_REASONING_IDS = frozenset({"qwen3.6-plus", "qwen3.7-max", "qwen3.7-plus", "qwen3.8-max"})
_TRANSPORT_PACKAGES = {
    "responses": "@ai-sdk/openai",
    "anthropic_messages": "@ai-sdk/anthropic",
    "chat_completions": None,
}
_CHAT_COMPATIBLE_PACKAGE = "@ai-sdk/openai-compatible"
_MODEL_SPEC_FIELDS = frozenset(field.name for field in fields(ModelSpec))
_SNAPSHOT_MSG = "invalid model snapshot"
_REFRESH_MSG = "invalid refresh payload"


def registry_snapshot(registry=None):
    source = MODELS if registry is None else registry
    if not isinstance(source, dict):
        raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
    for model_id, spec in source.items():
        if not isinstance(model_id, str) or not model_id or not isinstance(spec, ModelSpec):
            raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
    records = []
    for model_id in sorted(source):
        spec = source[model_id]
        record = asdict(spec)
        record["efforts"] = list(record["efforts"])
        records.append(record)
    payload = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "models": records}
    registry_from_snapshot(payload)
    return payload


def registry_from_snapshot(payload):
    if not isinstance(payload, dict) or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
    records = payload.get("models")
    if not isinstance(records, list) or len(records) != len(MODELS):
        raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
    seen = set()
    result = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != _MODEL_SPEC_FIELDS:
            raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
        spec = _validate_record(record)
        if spec.id in seen:
            raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
        seen.add(spec.id)
        result[spec.id] = spec
    if seen != set(MODELS):
        raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
    return dict(sorted(result.items()))


def _validate_record(record):
    model_id = record.get("id")
    name = record.get("name")
    if (not isinstance(model_id, str) or not model_id
            or not isinstance(name, str) or not name):
        raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
    transport = record.get("transport")
    builtin = MODELS.get(model_id)
    if builtin is None or transport != builtin.transport:
        raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
    efforts = _validate_efforts(record.get("efforts"))
    default_effort = record.get("default_effort")
    if not isinstance(default_effort, str) or default_effort not in efforts:
        raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
    context_window = record.get("context_window")
    max_output = record.get("max_output")
    if (not isinstance(record.get("tool_call"), bool)
            or not isinstance(context_window, int) or isinstance(context_window, bool)
            or context_window <= 0
            or not isinstance(max_output, int) or isinstance(max_output, bool)
            or max_output <= 0):
        raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
    status = record.get("status")
    source_revision = record.get("source_revision")
    if status not in _ALLOWED_STATUSES or not isinstance(source_revision, str) or not source_revision:
        raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
    return ModelSpec(
        model_id, name, transport, efforts, default_effort,
        record.get("tool_call"), context_window, max_output, status,
        source_revision,
    )


def _validate_efforts(efforts):
    if not isinstance(efforts, (list, tuple)) or not efforts:
        raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
    values = []
    for effort in efforts:
        if not isinstance(effort, str) or not effort or effort not in _ALLOWED_EFFORTS:
            raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
        values.append(effort)
    if len(set(values)) != len(values):
        raise ModelError("snapshot_invalid", 400, _SNAPSHOT_MSG)
    return tuple(values)


def reconcile_registry(go_payload, models_dev_payload, current=None,
                       selected_model=None, selected_effort=None):
    current = registry_from_snapshot(registry_snapshot(current))
    if selected_effort is not None and selected_model is None:
        raise ModelError("refresh_payload_invalid", 400, _REFRESH_MSG)
    if selected_model is not None:
        get_model(selected_model, selected_effort, registry=current)
    go_ids = _parse_go_payload(go_payload)
    metadata_map = _parse_models_dev_payload(models_dev_payload)
    canonical = sorted(MODELS)
    if selected_model is not None and selected_model not in go_ids:
        raise ModelError("selected_model_missing", 409, "selected model is missing")
    conflicts = []
    registry = {}
    for model_id in canonical:
        spec = current[model_id]
        if model_id not in go_ids:
            registry[model_id] = replace(spec, status="unavailable")
            continue
        meta = metadata_map.get(model_id)
        if meta is None:
            conflict, safe = "metadata_missing", None
        else:
            conflict, safe = _check_metadata(
                spec, meta, selected_effort if model_id == selected_model else None)
        if conflict is not None:
            conflicts.append({"model": model_id, "code": conflict})
            registry[model_id] = replace(spec, status="active")
        else:
            context_window, max_output, efforts, source_revision = safe
            registry[model_id] = replace(
                spec, status="active", context_window=context_window,
                max_output=max_output, efforts=efforts,
                source_revision=source_revision)
    conflicts.sort(key=lambda entry: (entry["model"], entry["code"]))
    updated_models = [
        model_id for model_id in canonical if registry[model_id] != current[model_id]]
    return registry, {
        "unknown_go_models": sorted(go_ids - set(MODELS)),
        "unknown_metadata_models": sorted(set(metadata_map) - set(MODELS)),
        "missing_go_models": sorted(set(MODELS) - go_ids),
        "conflicts": conflicts,
        "updated_models": updated_models,
    }


def _parse_go_payload(go_payload):
    if not isinstance(go_payload, dict) or go_payload.get("object") != "list":
        raise ModelError("refresh_payload_invalid", 400, _REFRESH_MSG)
    data = go_payload.get("data")
    if not isinstance(data, list):
        raise ModelError("refresh_payload_invalid", 400, _REFRESH_MSG)
    ids = []
    for item in data:
        model_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(model_id, str) or not model_id:
            raise ModelError("refresh_payload_invalid", 400, _REFRESH_MSG)
        ids.append(model_id)
    if len(set(ids)) != len(ids):
        raise ModelError("refresh_payload_invalid", 400, _REFRESH_MSG)
    return set(ids)


def _parse_models_dev_payload(models_dev_payload):
    if not isinstance(models_dev_payload, dict):
        raise ModelError("refresh_payload_invalid", 400, _REFRESH_MSG)
    provider = models_dev_payload.get("opencode-go")
    if not isinstance(provider, dict) or not isinstance(provider.get("models"), dict):
        raise ModelError("refresh_payload_invalid", 400, _REFRESH_MSG)
    result = {}
    for key, value in provider["models"].items():
        if not isinstance(key, str) or not key or not isinstance(value, dict):
            raise ModelError("refresh_payload_invalid", 400, _REFRESH_MSG)
        value_id = value.get("id")
        if value_id is not None and value_id != key:
            raise ModelError("refresh_payload_invalid", 400, _REFRESH_MSG)
        result[key] = value
    return result


def _check_metadata(spec, meta, selected_effort):
    provider = meta.get("provider")
    if spec.transport == "chat_completions":
        if provider is not None and not isinstance(provider, dict):
            return "transport_conflict", None
        package = provider.get("npm") if isinstance(provider, dict) else None
        if package is not None and package != _CHAT_COMPATIBLE_PACKAGE:
            return "transport_conflict", None
    elif not isinstance(provider, dict) or provider.get("npm") != _TRANSPORT_PACKAGES[spec.transport]:
        return "transport_conflict", None
    limit = meta.get("limit")
    if not isinstance(limit, dict):
        return "limits_invalid", None
    context_window = limit.get("context")
    max_output = limit.get("output")
    if (not isinstance(context_window, int) or isinstance(context_window, bool)
            or context_window <= 0
            or not isinstance(max_output, int) or isinstance(max_output, bool)
            or max_output <= 0):
        return "limits_invalid", None
    if meta.get("tool_call") is not True:
        return "tool_call_conflict", None
    last_updated = meta.get("last_updated")
    if not isinstance(last_updated, str) or not last_updated:
        return "source_revision_missing", None
    efforts, conflict = _normalize_reasoning(
        spec.id, spec, meta.get("reasoning_options"), selected_effort)
    if conflict is not None:
        return conflict, None
    return None, (context_window, max_output, efforts, last_updated)


def _normalize_reasoning(model_id, prior, options, selected_effort):
    if prior.efforts == ("default",):
        if options is None:
            return ("default",), None
        return None, "reasoning_conflict"
    if model_id == "minimax-m3":
        if (isinstance(options, list) and len(options) == 1
                and isinstance(options[0], dict)
                and options[0].get("type") == "toggle"):
            return ("none", "high"), None
        return None, "reasoning_conflict"
    if model_id in _QWEN_REASONING_IDS:
        if (not isinstance(options, list) or len(options) != 2
                or not all(isinstance(item, dict) for item in options)):
            return None, "reasoning_conflict"
        types = []
        for item in options:
            item_type = item.get("type")
            if not isinstance(item_type, str):
                return None, "reasoning_conflict"
            types.append(item_type)
        if sorted(types) != ["budget_tokens", "toggle"]:
            return None, "reasoning_conflict"
        budget = next((item.get("max") for item in options
                       if item.get("type") == "budget_tokens"), None)
        if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0:
            return ("none", "high", "max"), None
        return None, "reasoning_conflict"
    if (not isinstance(options, list) or len(options) != 1
            or not isinstance(options[0], dict)
            or options[0].get("type") != "effort"):
        return None, "reasoning_conflict"
    values = options[0].get("values")
    if not isinstance(values, list) or not values:
        return None, "reasoning_conflict"
    checked = []
    for value in values:
        if (not isinstance(value, str) or not value
                or value not in _ALLOWED_EFFORTS or value == "default"):
            return None, "reasoning_conflict"
        checked.append(value)
    if len(set(checked)) != len(checked):
        return None, "reasoning_conflict"
    if (prior.default_effort not in checked
            or (selected_effort is not None and selected_effort not in checked)):
        return None, "reasoning_conflict"
    return tuple(checked), None


def get_model(model_id, effort=None, registry=None):
    source = MODELS if registry is None else registry
    if not isinstance(source, dict):
        raise ModelError("snapshot_invalid", 400, "invalid model registry")
    spec = source.get(model_id)
    if spec is None:
        raise ModelError("model_not_found", 400, f"unknown model: {model_id}")
    if spec.status != "active":
        raise ModelError("model_unavailable", 400, "model is unavailable")
    if effort is not None and effort not in spec.efforts:
        raise ModelError("invalid_effort", 400, f"invalid effort {effort!r} for model {model_id}")
    return spec


def validate_profile(model_id, effort, registry=None):
    get_model(model_id, effort, registry=registry)
    return True
