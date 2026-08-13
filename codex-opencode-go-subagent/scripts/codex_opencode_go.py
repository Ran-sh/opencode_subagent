#!/usr/bin/env python3
"""OpenCode Go offline configuration lifecycle manager (DS-20260811-09).

Manages only the opencode-go provider, the OpenCode role/agent, the standalone
model catalog and the manager state under an isolated CODEX_HOME. API keys go
to the system credential store only; no secret is written to any managed file.
"""

from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import http.client
import importlib.util
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # macOS / Linux
    msvcrt = None


PROVIDER = "opencode-go"
ROLE = "OpenCode"
CREDENTIAL_TARGET = "codex-opencode-go-api-key"
PROVIDER_BEGIN = "# BEGIN CODEX-OPENCODE-GO-SUBAGENT PROVIDER"
PROVIDER_END = "# END CODEX-OPENCODE-GO-SUBAGENT PROVIDER"
ROLE_BEGIN = "# BEGIN CODEX-OPENCODE-GO-SUBAGENT ROLE"
ROLE_END = "# END CODEX-OPENCODE-GO-SUBAGENT ROLE"
LEGACY_PROVIDER_BEGIN = "# BEGIN CODEX-DEEPSEEK-SUBAGENT PROVIDER"
LEGACY_PROVIDER_END = "# END CODEX-DEEPSEEK-SUBAGENT PROVIDER"
LEGACY_ROLE_BEGIN = "# BEGIN CODEX-DEEPSEEK-SUBAGENT ROLE"
LEGACY_ROLE_END = "# END CODEX-DEEPSEEK-SUBAGENT ROLE"
LOCK_WAIT_SECONDS = 5.0
STATE_DIR_NAME = "opencode-go-subagent"
CATALOG_NAME = "models-opencode-go.json"
GATEWAY_START_WAIT_SECONDS = 5.0
GATEWAY_STOP_WAIT_SECONDS = 5.0
GATEWAY_POLL_SECONDS = 0.05
GATEWAY_RUNTIME_VERSION = 1
STATE_SCHEMA_VERSION = 2
STATE_FIELDS = frozenset({"version", "local_gateway_token", "port", "model_registry"})
LIVE_TEST_TIMEOUT_SECONDS = 660
LIVE_TEST_LINE_LIMIT = 1024 * 1024
LIVE_TEST_TOTAL_LIMIT = 8 * 1024 * 1024
LIVE_TEST_TEXT_MARKER = "OPENCODE_TEXT_OK"
LIVE_TEST_TOOL_MARKER = "OPENCODE_TOOL_OK"
LIVE_TEST_TOOL_NAME = "opencode_live_probe"
 
 
DESKTOP_CODEX_CANDIDATES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    Path("/Applications/Codex.app/Contents/Resources/codex"),
)
WINDOWS_CODEX_RELATIVE_CANDIDATES = (
    Path("Programs") / "Codex" / "resources" / "codex.exe",
    Path("Programs") / "OpenAI" / "Codex" / "resources" / "codex.exe",
    Path("Codex") / "resources" / "codex.exe",
)
CODEX_VERSION_TIMEOUT_SECONDS = 15
CODEX_MODELS_TIMEOUT_SECONDS = 45
REFRESH_TIMEOUT_SECONDS = 30
REFRESH_USER_AGENT = "codex-opencode-go-subagent/1"
REFRESH_SOURCES = {
    "opencode_go": ("opencode.ai", "/zen/go/v1/models", 1048576),
    "models_dev": ("models.dev", "/api.json", 8388608),
}
 
 
class ManagerError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _fetch_refresh_json(source, connection_factory=None):
    if source not in REFRESH_SOURCES:
        raise ManagerError(
            "refresh_payload_invalid", "模型目录来源无效。")
    host, path, limit = REFRESH_SOURCES[source]
    factory = connection_factory or http.client.HTTPSConnection
    conn = None
    try:
        conn = factory(host, timeout=REFRESH_TIMEOUT_SECONDS)
        conn.request(
            "GET", path, body=None,
            headers={
                "User-Agent": REFRESH_USER_AGENT,
                "Accept": "application/json",
                "Connection": "close",
            },
        )
        response = conn.getresponse()
        status = response.status
        if not isinstance(status, int) or isinstance(status, bool):
            raise ManagerError(
                "refresh_network_failed", "无法下载模型目录。",
                {"source": source})
        if status != 200:
            raise ManagerError(
                "refresh_network_failed", "无法下载模型目录。",
                {"source": source, "status": status})
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            if (not isinstance(content_length, str) or not content_length
                    or any(char not in "0123456789"
                           for char in content_length)):
                raise ManagerError(
                    "refresh_payload_invalid", "模型目录响应无效。",
                    {"source": source})
            if int(content_length) > limit:
                raise ManagerError(
                    "refresh_response_too_large", "模型目录响应过大。",
                    {"source": source})
        body = response.read(limit + 1)
        if not isinstance(body, bytes):
            raise ManagerError(
                "refresh_payload_invalid", "模型目录响应无效。",
                {"source": source})
        if len(body) > limit:
            raise ManagerError(
                "refresh_response_too_large", "模型目录响应过大。",
                {"source": source})
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise ManagerError(
                "refresh_payload_invalid", "模型目录响应无效。",
                {"source": source}) from None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise ManagerError(
                "refresh_payload_invalid", "模型目录响应无效。",
                {"source": source}) from None
        if not isinstance(payload, dict):
            raise ManagerError(
                "refresh_payload_invalid", "模型目录响应无效。",
                {"source": source})
        return payload
    except ManagerError:
        raise
    except (OSError, http.client.HTTPException):
        raise ManagerError(
            "refresh_network_failed", "无法下载模型目录。",
            {"source": source}) from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def fetch_refresh_payloads(connection_factory=None):
    go_payload = _fetch_refresh_json("opencode_go", connection_factory)
    models_dev_payload = _fetch_refresh_json("models_dev", connection_factory)
    return (go_payload, models_dev_payload)


@dataclass(frozen=True)
class Paths:
    home: Path
    config: Path
    catalog: Path
    agent: Path
    state_dir: Path
    manifest: Path
    state: Path
    backups: Path
    lock: Path
    gateway_runtime: Path
    gateway_log: Path


@dataclass(frozen=True)
class Profile:
    model: str
    effort: str


def resolve_paths(codex_home: str | None) -> Paths:
    home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    state_dir = home / STATE_DIR_NAME
    return Paths(
        home=home,
        config=home / "config.toml",
        catalog=home / CATALOG_NAME,
        agent=home / "agents" / f"{ROLE}.toml",
        state_dir=state_dir,
        manifest=state_dir / "manifest.json",
        state=state_dir / "state.json",
        backups=state_dir / "backups",
        lock=state_dir / "operation.lock",
        gateway_runtime=state_dir / "gateway-runtime.json",
        gateway_log=state_dir / "gateway.jsonl",
    )


def result(status: str, **kwargs: Any) -> dict[str, Any]:
    return {"status": status, **kwargs}


def emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(payload.get("status", "unknown"))
    for key, value in payload.items():
        if key != "status":
            print(f"{key}: {value}")


def _models_module():
    module = sys.modules.get("opencode_models")
    if module is not None:
        return module
    path = Path(__file__).resolve().parent / "opencode_models.py"
    spec = importlib.util.spec_from_file_location("opencode_models", str(path))
    if spec is None or spec.loader is None:
        raise ManagerError("models_unavailable", "无法加载 opencode_models.py。")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def get_model(model_id: str, effort: str | None = None, registry=None):
    models = _models_module()
    try:
        return models.get_model(model_id, effort, registry=registry)
    except models.ModelError as exc:
        raise ManagerError(exc.code, exc.message) from exc


def _validated_registry(registry=None):
    models = _models_module()
    try:
        payload = models.registry_snapshot(registry)
        return models.registry_from_snapshot(payload)
    except models.ModelError as exc:
        raise ManagerError("conflict", "模型注册表无效。", {"fields": ["model_registry"]}) from exc


def platform_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt" or sys.platform == "win32":
        return "windows"
    return "unsupported"
 
 
def _windows_bundled_codex_candidates() -> list[Path]:
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        return []
    base = Path(root) / "OpenAI" / "Codex" / "bin"
    try:
        subdirectories = [entry for entry in base.iterdir() if entry.is_dir()]
    except OSError:
        subdirectories = []
    candidates: dict[Path, int] = {}
    for candidate in [base / "codex.exe"] + [entry / "codex.exe" for entry in subdirectories]:
        try:
            if candidate.is_file():
                resolved = candidate.resolve()
                if resolved not in candidates:
                    candidates[resolved] = resolved.stat().st_mtime_ns
        except OSError:
            continue
    return [
        path
        for path, _ in sorted(
            candidates.items(),
            key=lambda item: (-item[1], str(item[0]).casefold(), str(item[0])),
        )
    ]
 
 
def codex_version_text(codex_bin: str) -> str:
    proc = subprocess.run(
        [codex_bin, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CODEX_VERSION_TIMEOUT_SECONDS,
    )
    text = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode != 0 or not text:
        raise ManagerError("codex_version_unknown", "无法读取 Codex 桌面内置运行时版本。")
    return text
 
 
def _codex_runnable(codex_bin: str) -> bool:
    try:
        return bool(codex_version_text(codex_bin))
    except (ManagerError, OSError, subprocess.TimeoutExpired):
        return False
 
 
def find_desktop_codex() -> str:
    configured = os.environ.get("CODEX_DESKTOP_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_file():
            raise ManagerError(
                "desktop_codex_missing",
                f"CODEX_DESKTOP_BIN 指向的文件不存在：{candidate}",
            )
        resolved = str(candidate.resolve())
        if _codex_runnable(resolved):
            return resolved
        raise ManagerError(
            "desktop_codex_unusable",
            f"CODEX_DESKTOP_BIN 指向的文件无法运行：{candidate}",
        )
    if platform_name() == "macos":
        for candidate in DESKTOP_CODEX_CANDIDATES:
            try:
                if candidate.is_file() and _codex_runnable(str(candidate.resolve())):
                    return str(candidate.resolve())
            except OSError:
                continue
    if platform_name() == "windows":
        candidates = _windows_bundled_codex_candidates()
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            root = os.environ.get(variable)
            if root:
                candidates.extend(
                    Path(root) / relative for relative in WINDOWS_CODEX_RELATIVE_CANDIDATES
                )
        for candidate in candidates:
            try:
                if candidate.is_file() and _codex_runnable(str(candidate.resolve())):
                    return str(candidate.resolve())
            except OSError:
                continue
        discovered = shutil.which("codex.exe") or shutil.which("codex")
        if discovered and _codex_runnable(discovered):
            return discovered
    raise ManagerError(
        "desktop_codex_missing",
        "没有找到可运行的 Codex 桌面内置运行时；请先安装或启动桌面应用，或设置 CODEX_DESKTOP_BIN。",
    )
 
 
def credential_account() -> str:
    return getpass.getuser()


def credential_backend() -> str | None:
    current = platform_name()
    if current == "macos" and Path("/usr/bin/security").is_file():
        return "macos-keychain"
    if current == "windows":
        return "windows-credential-manager"
    return None


def credential_available() -> bool:
    return credential_backend() is not None


def _macos_read_credential() -> str | None:
    proc = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            credential_account(),
            "-s",
            CREDENTIAL_TARGET,
            "-w",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _macos_store_credential(secret: str) -> None:
    proc = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            credential_account(),
            "-s",
            CREDENTIAL_TARGET,
            "-w",
            secret,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise ManagerError("credential_write_failed", "无法把 API Key 写入 macOS Keychain。")


def _macos_remove_credential() -> bool:
    proc = subprocess.run(
        [
            "/usr/bin/security",
            "delete-generic-password",
            "-a",
            credential_account(),
            "-s",
            CREDENTIAL_TARGET,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def _windows_credential_api():
    import ctypes
    from ctypes import wintypes

    class CredentialW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CredentialW)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(CredentialW), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    return ctypes, CredentialW, advapi32


def _windows_read_credential() -> str | None:
    ctypes, credential_type, advapi32 = _windows_credential_api()
    credential = ctypes.POINTER(credential_type)()
    if not advapi32.CredReadW(CREDENTIAL_TARGET, 1, 0, ctypes.byref(credential)):
        error = ctypes.get_last_error()
        if error == 1168:
            return None
        raise ManagerError(
            "credential_read_failed",
            f"无法读取 Windows Credential Manager（错误 {error}）。",
        )
    try:
        raw = ctypes.string_at(
            credential.contents.CredentialBlob,
            credential.contents.CredentialBlobSize,
        )
        return raw.decode("utf-8")
    finally:
        advapi32.CredFree(credential)


def _windows_store_credential(secret: str) -> None:
    ctypes, credential_type, advapi32 = _windows_credential_api()
    raw = secret.encode("utf-8")
    blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    credential = credential_type()
    credential.Flags = 0
    credential.Type = 1
    credential.TargetName = CREDENTIAL_TARGET
    credential.CredentialBlobSize = len(raw)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = 2
    credential.UserName = credential_account()
    if not advapi32.CredWriteW(ctypes.byref(credential), 0):
        error = ctypes.get_last_error()
        raise ManagerError(
            "credential_write_failed",
            f"无法把 API Key 写入 Windows Credential Manager（错误 {error}）。",
        )


def _windows_remove_credential() -> bool:
    ctypes, _, advapi32 = _windows_credential_api()
    if advapi32.CredDeleteW(CREDENTIAL_TARGET, 1, 0):
        return True
    error = ctypes.get_last_error()
    if error == 1168:
        return False
    raise ManagerError(
        "credential_delete_failed",
        f"无法从 Windows Credential Manager 删除 API Key（错误 {error}）。",
    )


def read_credential_key() -> str | None:
    backend = credential_backend()
    if backend == "macos-keychain":
        return _macos_read_credential()
    if backend == "windows-credential-manager":
        return _windows_read_credential()
    raise ManagerError("unsupported_platform", "当前只支持 macOS 和 Windows 系统凭据库。")


def credential_has_key() -> bool:
    if not credential_available():
        return False
    return read_credential_key() is not None


def store_credential_key(secret: str) -> None:
    if not secret:
        raise ManagerError("invalid_api_key", "API Key 不能为空。")
    backend = credential_backend()
    if backend == "macos-keychain":
        _macos_store_credential(secret)
        return
    if backend == "windows-credential-manager":
        _windows_store_credential(secret)
        return
    raise ManagerError("unsupported_platform", "当前只支持 macOS 和 Windows 系统凭据库。")


def remove_credential_key() -> bool:
    if not credential_available() or not credential_has_key():
        return False
    if credential_backend() == "macos-keychain":
        return _macos_remove_credential()
    return _windows_remove_credential()


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_string_array(values: list[str]) -> str:
    return "[" + ", ".join(toml_string(value) for value in values) + "]"


def parse_toml_text(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManagerError("invalid_config", f"TOML 无法解析：{exc}") from exc


def remove_marked_block(text: str, begin: str, end: str) -> str:
    pattern = re.compile(
        rf"\n?{re.escape(begin)}.*?{re.escape(end)}\n?",
        flags=re.DOTALL,
    )
    return pattern.sub("\n", text).rstrip() + ("\n" if text else "")


def remove_managed_blocks(text: str) -> str:
    text = remove_marked_block(text, PROVIDER_BEGIN, PROVIDER_END)
    return remove_marked_block(text, ROLE_BEGIN, ROLE_END)


def remove_legacy_blocks(text: str) -> str:
    text = remove_marked_block(text, LEGACY_PROVIDER_BEGIN, LEGACY_PROVIDER_END)
    return remove_marked_block(text, LEGACY_ROLE_BEGIN, LEGACY_ROLE_END)


def toml_table_header(table: str) -> re.Pattern[str]:
    tokens = [
        rf"(?:{re.escape(part)}|\"{re.escape(part)}\"|'{re.escape(part)}')"
        for part in table.split(".")
    ]
    return re.compile(r"^\[\s*" + r"\s*\.\s*".join(tokens) + r"\s*\]\s*(?:#.*)?$")


def _is_table_or_sub(header: str, table: str) -> bool:
    if not (header.startswith("[") and header.endswith("]")):
        return False
    inner = header[1:-1].strip()
    parts = [part.strip().strip('"').strip("'") for part in inner.split(".")]
    expected = table.split(".")
    return parts[: len(expected)] == expected


def remove_table_and_subtables(text: str, table: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_table_or_sub(line.strip(), table):
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("["):
                index += 1
            continue
        out.append(line)
        index += 1
    return "\n".join(out).rstrip() + ("\n" if text else "")


def top_level_key(text: str, key: str) -> str | None:
    value = parse_toml_text(text).get(key)
    return value if isinstance(value, str) else None


def set_top_level_key(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    assignment = f"{key} = {toml_string(value)}"
    first_table = next((i for i, line in enumerate(lines) if line.strip().startswith("[")), len(lines))
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(first_table):
        if key_pattern.match(lines[index]):
            lines[index] = assignment
            return "\n".join(lines).rstrip() + "\n"
    lines.insert(first_table, assignment)
    if first_table and lines[first_table - 1].strip():
        lines.insert(first_table + 1, "")
    return "\n".join(lines).rstrip() + "\n"


def remove_top_level_key_if_value(text: str, key: str, expected: str) -> str:
    lines = text.splitlines()
    first_table = next((i for i, line in enumerate(lines) if line.strip().startswith("[")), len(lines))
    kept: list[str] = []
    for index, line in enumerate(lines):
        matches = False
        if index < first_table:
            try:
                matches = tomllib.loads(line).get(key) == expected
            except tomllib.TOMLDecodeError:
                matches = False
        if not matches:
            kept.append(line)
    return "\n".join(kept).rstrip() + "\n"


def set_table_bool(text: str, table: str, key: str, value: bool) -> str:
    lines = text.splitlines()
    assignment = f"{key} = {'true' if value else 'false'}"
    header = toml_table_header(table)
    start = next((index for index, line in enumerate(lines) if header.match(line.strip())), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend((f"[{table}]", assignment))
        return "\n".join(lines).rstrip() + "\n"
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].strip().startswith("[")),
        len(lines),
    )
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(start + 1, end):
        if key_pattern.match(lines[index]):
            lines[index] = assignment
            return "\n".join(lines).rstrip() + "\n"
    lines.insert(end, assignment)
    return "\n".join(lines).rstrip() + "\n"


def remove_table_bool_if_value(text: str, table: str, key: str, expected: bool) -> str:
    lines = text.splitlines()
    header = toml_table_header(table)
    start = next((index for index, line in enumerate(lines) if header.match(line.strip())), None)
    if start is None:
        return text
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].strip().startswith("[")),
        len(lines),
    )
    expected_text = "true" if expected else "false"
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*{expected_text}\s*(?:#.*)?$")
    kept = [
        line
        for index, line in enumerate(lines)
        if not (start < index < end and pattern.match(line))
    ]
    return "\n".join(kept).rstrip() + "\n"


def detect_legacy_config(text: str) -> bool:
    if not text.strip():
        return False
    if LEGACY_PROVIDER_BEGIN in text and LEGACY_PROVIDER_END in text:
        return True
    if LEGACY_ROLE_BEGIN in text and LEGACY_ROLE_END in text:
        return True
    try:
        parsed = parse_toml_text(text)
    except ManagerError:
        return False
    if (parsed.get("model_providers") or {}).get("deepseek"):
        return True
    if (parsed.get("agents") or {}).get("DeepSeek"):
        return True
    return False


def expected_provider_auth(home: str) -> dict[str, Any]:
    return {
        "command": sys.executable,
        "args": [
            str(Path(__file__).resolve()),
            "_gateway-token",
            "--codex-home",
            home,
        ],
        "timeout_ms": 30000,
        "refresh_interval_ms": 5000,
    }


def managed_provider_block(paths: Paths, port: int) -> str:
    auth = expected_provider_auth(str(paths.home))
    return (
        f"{PROVIDER_BEGIN}\n"
        f"[model_providers.{PROVIDER}]\n"
        'name = "OpenCode Go"\n'
        f'base_url = "http://127.0.0.1:{port}/v1"\n'
        'wire_api = "responses"\n'
        "\n"
        f"[model_providers.{PROVIDER}.auth]\n"
        f"command = {toml_string(auth['command'])}\n"
        f"args = {toml_string_array(auth['args'])}\n"
        f"timeout_ms = {auth['timeout_ms']}\n"
        f"refresh_interval_ms = {auth['refresh_interval_ms']}\n"
        f"{PROVIDER_END}\n"
    )


def managed_role_block(paths: Paths) -> str:
    return (
        f"{ROLE_BEGIN}\n"
        f"[agents.{ROLE}]\n"
        'description = "OpenCode Go native subagent for bounded coding tasks. '
        'Text and tools only: shell, apply_patch, function and MCP. '
        'No image, video, screenshot or web browsing; the parent agent inspects '
        'visual inputs and passes text facts."\n'
        f"config_file = {toml_string(str(paths.agent))}\n"
        f"{ROLE_END}\n"
    )


def expected_agent_text(model: str, effort: str) -> str:
    lines = [
        f'name = "{ROLE}"',
        'description = "OpenCode Go native subagent for bounded coding tasks. '
        'Text and tools only: shell, apply_patch, function and MCP. '
        'No image, video, screenshot or web browsing; the parent agent inspects '
        'visual inputs and passes text facts."',
        f'model = "{model}"',
        f'model_provider = "{PROVIDER}"',
    ]
    if effort != "default":
        lines.append(f'model_reasoning_effort = "{effort}"')
    lines.append("developer_instructions = \"\"\"")
    lines.append("You are a focused OpenCode Go subagent running inside Codex.")
    lines.append("")
    lines.append(
        "Complete the bounded task assigned by the parent agent, use available tools "
        "when needed, and return a concise evidence-based result."
    )
    lines.append(
        "Treat the parent agent's task packet as an execution contract, not an "
        "invitation to redesign it."
    )
    lines.append(
        "Do not make or change requirements, architecture, safety, environment, or "
        "release decisions. If the packet is ambiguous, conflicts with code or rules, "
        "lacks required facts, or requires broader scope, stop and return the evidence "
        "and applicable STOP-* reason."
    )
    lines.append(
        "PREFLIGHT-only and DELIVERY-only are read-only. Before any write, verify "
        "TASK_ID, REVISION, PHASE, WRITE_AUTHORIZATION, BASELINE_SHA256, WRITE_SET, "
        "FROZEN_SET, and BUDGET."
    )
    lines.append(
        "Modify only explicitly authorized files and anchors. Preserve pre-existing "
        "user changes. Run only listed commands, obey READ-ONLY/MAY-WRITE and declared "
        "outputs. Do not refactor unrelated code, install dependencies, change the "
        "environment, commit, push, or publish unless the exact task packet explicitly "
        "authorizes it."
    )
    lines.append(
        "For every write phase, return a complete unified diff, command-by-command "
        "test evidence, a structured risk list, and a traceability matrix. If any "
        "required evidence is missing, return PARTIAL_STOP with "
        "STOP-DELIVERY-INCOMPLETE."
    )
    lines.append(
        "The parent agent owns independent acceptance and the final response. Never "
        "claim the user's task is complete."
    )
    lines.append(
        "You are text-only. Do not claim to inspect images, videos, screenshots, or "
        "other visual inputs. If visual evidence is required and the parent did not "
        "provide a textual description, report that limitation clearly."
    )
    lines.append(
        "Do not spawn additional subagents unless the user or parent explicitly asks "
        "for nested delegation."
    )
    lines.append('"""')
    return "\n".join(lines) + "\n"


def profile_json(profile: Profile) -> dict[str, str]:
    return {"model": profile.model, "effort": profile.effort}


def default_profile() -> Profile:
    return Profile("deepseek-v4-flash", "max")


def _catalog_record(spec) -> dict[str, Any]:
    efforts = tuple(spec.efforts)
    if spec.default_effort == "default" or "default" in efforts:
        default_level = "none"
        levels = [
            {
                "effort": "none",
                "description": "Default reasoning level; agent omits model_reasoning_effort",
            }
        ]
    else:
        default_level = spec.default_effort
        levels = [{"effort": item, "description": f"{item} reasoning effort"} for item in efforts]
    return {
        "slug": spec.id,
        "display_name": spec.name,
        "description": f"{spec.name} OpenCode Go subagent (text and tools only).",
        "default_reasoning_level": default_level,
        "supported_reasoning_levels": levels,
        "shell_type": "shell_command",
        "apply_patch_tool_type": "freeform",
        "tool_mode": "direct",
        "additional_speed_tiers": [],
        "service_tiers": [],
        "default_service_tier": None,
        "availability_nux": None,
        "upgrade": None,
        "supports_reasoning_summary_parameter": False,
        "default_reasoning_summary": "none",
        "support_verbosity": False,
        "default_verbosity": None,
        "web_search_tool_type": "text",
        "supports_parallel_tool_calls": True,
        "context_window": spec.context_window,
        "max_context_window": spec.context_window,
        "auto_compact_token_limit": None,
        "comp_hash": None,
        "experimental_supported_tools": [],
        "input_modalities": ["text"],
        "supports_search_tool": False,
        "supports_image_detail_original": False,
        "use_responses_lite": False,
        "multi_agent_version": "v1",
        "x_opencode_go": {
            "transport": spec.transport,
            "max_output": spec.max_output,
            "source_revision": spec.source_revision,
            "tool_call": spec.tool_call,
        },
    }


def merge_catalog(base: dict[str, Any], registry=None) -> dict[str, Any]:
    def template_from_base(items: list[Any]) -> dict[str, Any] | None:
        for item in items:
            if not isinstance(item, dict):
                continue
            modalities = item.get("input_modalities")
            if not (isinstance(modalities, list) and "text" in modalities):
                continue
            if item.get("shell_type") != "shell_command":
                continue
            if item.get("apply_patch_tool_type") != "freeform":
                continue
            base_instructions = item.get("base_instructions")
            if isinstance(base_instructions, str) and base_instructions.strip():
                return copy.deepcopy(item)
            model_messages = item.get("model_messages")
            if (
                isinstance(model_messages, dict)
                and isinstance(model_messages.get("instructions_template"), str)
                and model_messages["instructions_template"].strip()
            ):
                return copy.deepcopy(item)
        return None

    models = _models_module()
    registry = _validated_registry(registry)
    canonical = set(models.MODELS)
    base_models = base.get("models") if isinstance(base, dict) else None
    if not isinstance(base_models, list):
        base_models = []
    kept = [
        copy.deepcopy(item)
        for item in base_models
        if isinstance(item, dict) and item.get("slug") not in canonical
    ]
    template = template_from_base(base_models)
    if template is None:
        raise ManagerError(
            "codex_catalog_invalid",
            "当前 Codex 内置模型目录中没有可用的工具模板。",
        )
    for slug, spec in registry.items():
        if spec.status != "active":
            continue
        record = copy.deepcopy(template)
        record.update(_catalog_record(spec))
        kept.append(record)
    kept.sort(key=lambda item: item.get("slug", ""))
    return {"models": kept}


def run_codex_models(codex_bin: str, home: Path) -> dict[str, Any]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    try:
        proc = subprocess.run(
            [codex_bin, "debug", "models", "--bundled"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CODEX_MODELS_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagerError("codex_catalog_failed", "无法读取 Codex 内置模型目录。") from exc
    if proc.returncode != 0:
        raise ManagerError("codex_catalog_failed", "Codex 读取内置模型目录失败。")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ManagerError("codex_catalog_invalid", "Codex 内置模型目录输出无效。") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("models"), list)
        or not payload["models"]
    ):
        raise ManagerError("codex_catalog_invalid", "Codex 内置模型目录不完整。")
    return payload


def load_base_catalog(paths: Paths, config_text: str, catalog_loader=None) -> dict[str, Any]:
    if catalog_loader is not None:
        data = catalog_loader()
        if (
            isinstance(data, dict)
            and isinstance(data.get("models"), list)
            and data["models"]
        ):
            return data
        raise ManagerError("codex_catalog_invalid", "模型目录 loader 返回了无效结构。")
    parsed = parse_toml_text(config_text) if config_text.strip() else {}
    configured_path = parsed.get("model_catalog_json")
    if isinstance(configured_path, str):
        candidate = Path(configured_path).expanduser()
        if candidate.is_file():
            try:
                data = json.loads(read_utf8_text(candidate))
                if isinstance(data.get("models"), list) and data["models"]:
                    return data
            except (OSError, json.JSONDecodeError):
                pass
    codex_bin = find_desktop_codex()
    return run_codex_models(codex_bin, paths.home)
    raise ManagerError("codex_catalog_failed", "无法读取 Codex 模型目录；需要可注入 catalog_loader。")


def new_state(registry=None) -> dict[str, Any]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return {
        "version": STATE_SCHEMA_VERSION,
        "local_gateway_token": secrets.token_urlsafe(32),
        "port": int(port),
        "model_registry": _models_module().registry_snapshot(_validated_registry(registry)),
    }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_utf8_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def read_manifest(paths: Paths) -> dict[str, Any]:
    if not paths.manifest.is_file():
        return {}
    try:
        payload = json.loads(read_utf8_text(paths.manifest))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_manifest(paths: Paths, payload: dict[str, Any]) -> None:
    atomic_write(paths.manifest, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())


def resolve_managed_profile(manifest: dict[str, Any], paths: Paths, registry=None) -> Profile | None:
    if not manifest:
        return None
    model = manifest.get("selected_model")
    effort = manifest.get("selected_effort")
    if not isinstance(model, str) or not isinstance(effort, str):
        return None
    if registry is None:
        registry = read_model_registry(paths)
    try:
        get_model(model, effort, registry=registry)
    except ManagerError:
        return None
    return Profile(model, effort)


def _manifest_enabled(manifest: dict[str, Any]) -> bool:
    enabled = manifest.get("enabled")
    if enabled is None:
        return True
    if not isinstance(enabled, bool):
        raise ManagerError("conflict", "管理记录中的 enabled 字段无效。", {"fields": ["enabled"]})
    return enabled


def _verify_managed_hashes(paths: Paths, manifest: dict[str, Any]) -> None:
    enabled = _manifest_enabled(manifest)
    drift: list[str] = []
    if enabled:
        pairs = (
            ("config_sha256", paths.config),
            ("catalog_sha256", paths.catalog),
            ("agent_sha256", paths.agent),
            ("state_sha256", paths.state),
        )
    else:
        pairs = (
            ("config_sha256", paths.config),
            ("catalog_sha256", paths.catalog),
            ("state_sha256", paths.state),
        )
    for key, path in pairs:
        expected = manifest.get(key)
        if not isinstance(expected, str) or not path.is_file() or sha256_bytes(path.read_bytes()) != expected:
            drift.append(str(path))
    if not enabled:
        agent_expected = manifest.get("agent_sha256")
        if agent_expected is not None or paths.agent.is_file():
            drift.append(str(paths.agent))
    if drift:
        raise ManagerError("conflict", "受管文件已被修改，拒绝操作。", {"paths": drift})


def _static_verify(paths: Paths, profile: Profile) -> None:
    if not paths.config.is_file():
        raise ManagerError("not_configured", "config.toml 缺失。")
    config_text = read_utf8_text(paths.config)
    parsed = parse_toml_text(config_text)
    provider = (parsed.get("model_providers") or {}).get(PROVIDER)
    role = (parsed.get("agents") or {}).get(ROLE)
    if not provider:
        raise ManagerError("not_configured", "provider 未注册。")
    if not role:
        raise ManagerError("not_configured", "role 未注册。")
    catalog_value = parsed.get("model_catalog_json")
    if not isinstance(catalog_value, str) or Path(catalog_value).expanduser().resolve() != paths.catalog:
        raise ManagerError("not_configured", "模型目录未选中。")
    manifest = read_manifest(paths)
    expected = {
        "config_sha256": paths.config,
        "catalog_sha256": paths.catalog,
        "agent_sha256": paths.agent,
        "state_sha256": paths.state,
    }
    for key, path in expected.items():
        expected_hash = manifest.get(key)
        if not isinstance(expected_hash, str) or not path.is_file() or sha256_bytes(path.read_bytes()) != expected_hash:
            raise ManagerError("not_configured", f"受管文件校验失败：{path.name}")
    registry = read_model_registry(paths)
    get_model(profile.model, profile.effort, registry=registry)
    if not paths.agent.is_file() or read_utf8_text(paths.agent) != expected_agent_text(profile.model, profile.effort):
        raise ManagerError("not_configured", "agent 文件与目标 Profile 不一致。")


def make_backup(paths: Paths) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = paths.backups / stamp
    backup.mkdir(parents=True, exist_ok=False)
    for source in (paths.config, paths.catalog, paths.agent, paths.manifest, paths.state):
        if source.is_file():
            shutil.copy2(source, backup / source.name)
    return backup


def restore_backup(paths: Paths, backup: Path) -> None:
    for target in (paths.config, paths.catalog, paths.agent, paths.manifest, paths.state):
        source = backup / target.name
        if source.is_file():
            atomic_write(target, source.read_bytes(), mode=0o644 if target == paths.agent else 0o600)
        elif target.is_file():
            target.unlink()


def try_acquire_file_lock(lock_file) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    if msvcrt is not None:
        lock_file.seek(0)
        if lock_file.read(1) == "":
            lock_file.seek(0)
            lock_file.write("\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    raise ManagerError("unsupported_platform", "当前平台没有可用的文件锁实现。")


def release_file_lock(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def operation_lock(paths: Paths, timeout_seconds: float = LOCK_WAIT_SECONDS):
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    with paths.lock.open("a+") as lock_file:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if try_acquire_file_lock(lock_file):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagerError(
                    "operation_in_progress",
                    "另一个配置操作仍在进行，请稍后重试。",
                )
            time.sleep(min(0.1, remaining))
        try:
            yield
        finally:
            release_file_lock(lock_file)


def _gateway_server_module():
    module = sys.modules.get("opencode_gateway_server")
    if module is not None:
        return module
    path = Path(__file__).resolve().parent / "opencode_gateway_server.py"
    spec = importlib.util.spec_from_file_location("opencode_gateway_server", str(path))
    if spec is None or spec.loader is None:
        raise ManagerError("gateway_server_unavailable", "无法加载网关服务器模块。")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parse_state_payload(payload):
    models = _models_module()
    if not isinstance(payload, dict) or set(payload) != STATE_FIELDS:
        raise ManagerError("conflict", "托管状态文件无效。", {"fields": ["state"]})
    version = payload.get("version")
    token = payload.get("local_gateway_token")
    port = payload.get("port")
    fields: list[str] = []
    if version != STATE_SCHEMA_VERSION or isinstance(version, bool):
        fields.append("version")
    if not (
        isinstance(token, str)
        and token
        and 20 <= len(token) <= 512
        and token.isascii()
        and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in token)
    ):
        fields.append("local_gateway_token")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        fields.append("port")
    if fields:
        raise ManagerError("conflict", "托管状态文件字段无效。", {"fields": fields})
    try:
        registry = models.registry_from_snapshot(payload.get("model_registry"))
    except models.ModelError as exc:
        raise ManagerError("conflict", "托管状态文件无效。", {"fields": ["model_registry"]}) from exc
    return {
        "version": version,
        "local_gateway_token": token,
        "port": port,
        "model_registry": registry,
    }


def _read_managed_state(paths: Paths, manifest=None) -> dict[str, Any]:
    if manifest is None:
        manifest = read_manifest(paths)
    if not manifest:
        raise ManagerError("not_managed", "没有找到本工具的管理记录，拒绝读取网关状态。")
    _verify_managed_hashes(paths, manifest)
    if not paths.state.is_file():
        raise ManagerError("conflict", "托管状态文件缺失。", {"fields": ["state"]})
    try:
        payload = json.loads(read_utf8_text(paths.state))
    except (OSError, json.JSONDecodeError):
        raise ManagerError("conflict", "托管状态文件无效。", {"fields": ["state"]}) from None
    return _parse_state_payload(payload)


def read_gateway_state(paths: Paths) -> dict[str, Any]:
    state = _read_managed_state(paths)
    return {
        "version": state["version"],
        "local_gateway_token": state["local_gateway_token"],
        "port": state["port"],
    }


def read_model_registry(paths: Paths, manifest=None) -> dict[str, Any]:
    state = _read_managed_state(paths, manifest)
    return dict(state["model_registry"])


def _raise_live_failure(model, phase, status=None, code=None):
    details: dict[str, Any] = {}
    if isinstance(model, str) and model:
        details["model"] = model
    if phase in ("text", "tool", "gateway", "profile"):
        details["phase"] = phase
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        details["status"] = status
    if (isinstance(code, str) and code and len(code) <= 64
            and all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in code)):
        details["code"] = code
    raise ManagerError("live_test_failed", "live test failed", details)


def _usage_total(usage):
    if isinstance(usage, dict):
        value = usage.get("total_tokens")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _live_gateway_request(gateway_state, request, model, phase, connection_factory=None):
    if not isinstance(gateway_state, dict):
        _raise_live_failure(model, phase)
    port = gateway_state.get("port")
    token = gateway_state.get("local_gateway_token")
    if (not isinstance(port, int) or isinstance(port, bool)
            or not (1 <= port <= 65535)
            or not isinstance(token, str) or not token
            or not isinstance(request, dict)
            or not isinstance(model, str) or not model
            or phase not in ("text", "tool")):
        _raise_live_failure(model, phase)
    factory = connection_factory if connection_factory is not None else http.client.HTTPConnection
    conn = None
    output_parts: list[str] = []
    function_calls: list[dict[str, str]] = []
    usage: dict[str, int] = {}
    pending_event = None
    pending_data: list[str] = []
    completed = False

    def dispatch_event():
        nonlocal pending_event, pending_data, completed
        if pending_event is None and not pending_data:
            return False
        payload = json.loads("\n".join(pending_data))
        if not isinstance(payload, dict):
            _raise_live_failure(model, phase)
        event_name = pending_event
        payload_type = payload.get("type")
        if event_name is None:
            event_name = payload_type if isinstance(payload_type, str) else None
        elif isinstance(payload_type, str) and payload_type != event_name:
            _raise_live_failure(model, phase)
        if event_name is not None:
            if event_name == "response.output_text.delta":
                delta = payload.get("delta")
                if isinstance(delta, str):
                    output_parts.append(delta)
                elif isinstance(delta, dict) and isinstance(delta.get("text"), str):
                    output_parts.append(delta["text"])
            elif event_name == "response.output_item.done":
                item = payload.get("item")
                if (isinstance(item, dict)
                        and item.get("type") == "function_call"
                        and isinstance(item.get("name"), str)
                        and isinstance(item.get("arguments"), str)):
                    function_calls.append({
                        "name": item["name"],
                        "arguments": item["arguments"],
                    })
            elif event_name == "response.failed":
                failed_response = payload.get("response")
                safe_code = None
                if isinstance(failed_response, dict):
                    failed_error = failed_response.get("error")
                    if isinstance(failed_error, dict):
                        safe_code = failed_error.get("code")
                _raise_live_failure(model, phase, code=safe_code)
            elif event_name == "response.completed":
                completed_response = payload.get("response")
                if isinstance(completed_response, dict):
                    response_usage = completed_response.get("usage")
                    if isinstance(response_usage, dict):
                        for key in ("input_tokens", "output_tokens", "total_tokens"):
                            value = response_usage.get(key)
                            if (isinstance(value, int)
                                    and not isinstance(value, bool)
                                    and value >= 0):
                                usage[key] = value
                completed = True
        pending_event = None
        pending_data = []
        return completed

    try:
        body = json.dumps(request, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        conn = factory("127.0.0.1", port, timeout=LIVE_TEST_TIMEOUT_SECONDS)
        conn.request(
            "POST",
            "/v1/responses",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": "Bearer " + token,
                "Connection": "close",
            },
        )
        response = conn.getresponse()
        status = response.status
        if status != 200:
            response.read(4097)
            _raise_live_failure(model, phase, status=status)
        content_type = response.getheader("Content-Type") or ""
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "text/event-stream":
            _raise_live_failure(model, phase, status=status)
        total_bytes = 0
        while True:
            raw = response.readline(LIVE_TEST_LINE_LIMIT + 1)
            if raw == b"":
                break
            total_bytes += len(raw)
            if total_bytes > LIVE_TEST_TOTAL_LIMIT:
                _raise_live_failure(model, phase)
            if len(raw) > LIVE_TEST_LINE_LIMIT:
                _raise_live_failure(model, phase)
            if raw.endswith(b"\r\n"):
                line = raw[:-2]
            elif raw.endswith(b"\n"):
                line = raw[:-1]
            elif raw.endswith(b"\r"):
                line = raw[:-1]
            else:
                line = raw
            if not line:
                if dispatch_event():
                    break
                continue
            if line.startswith(b":"):
                continue
            if line.startswith(b"event:"):
                pending_event = line[len(b"event:"):].strip().decode("utf-8")
                continue
            if line.startswith(b"data:"):
                value = line[len(b"data:"):]
                if value.startswith(b" "):
                    value = value[1:]
                pending_data.append(value.decode("utf-8"))
                continue
        dispatch_event()
        if not completed:
            _raise_live_failure(model, phase)
        return {
            "output_text": "".join(output_parts),
            "function_calls": function_calls,
            "usage": usage,
        }
    except ManagerError:
        raise
    except Exception:
        _raise_live_failure(model, phase)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _live_probe_profile(gateway_state, spec, effort, requester=None):
    requester = requester if requester is not None else _live_gateway_request
    text_payload = {
        "model": spec.id,
        "stream": True,
        "store": False,
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": LIVE_TEST_TEXT_MARKER,
            }],
        }],
    }
    tool_payload = {
        "model": spec.id,
        "stream": True,
        "store": False,
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": "Call " + LIVE_TEST_TOOL_NAME + " with value "
                        + LIVE_TEST_TOOL_MARKER
                        + ". Do not reply with text.",
            }],
        }],
        "tools": [{
            "type": "function",
            "name": LIVE_TEST_TOOL_NAME,
            "description": "Live validation probe tool.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "string",
                              "enum": [LIVE_TEST_TOOL_MARKER]},
                },
                "required": ["value"],
                "additionalProperties": False,
            },
        }],
        "tool_choice": "auto",
    }
    if effort != "default":
        text_payload["reasoning"] = {"effort": effort}
        tool_payload["reasoning"] = {"effort": effort}
    try:
        text_result = requester(gateway_state, text_payload, spec.id, "text")
    except ManagerError:
        raise
    except Exception:
        _raise_live_failure(spec.id, "text")
    text_ok = bool(
        isinstance(text_result, dict)
        and isinstance(text_result.get("output_text"), str)
        and LIVE_TEST_TEXT_MARKER in text_result["output_text"])
    if not text_ok:
        _raise_live_failure(spec.id, "text")
    try:
        tool_result = requester(gateway_state, tool_payload, spec.id, "tool")
    except ManagerError:
        raise
    except Exception:
        _raise_live_failure(spec.id, "tool")
    tool_ok = False
    if isinstance(tool_result, dict):
        for call in tool_result.get("function_calls") or []:
            if not isinstance(call, dict):
                continue
            call_type = call.get("type")
            if call_type not in (None, "function_call"):
                continue
            if call.get("name") != LIVE_TEST_TOOL_NAME:
                continue
            arguments = call.get("arguments")
            if not isinstance(arguments, str):
                continue
            try:
                parsed = json.loads(arguments)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(parsed, dict) and parsed.get("value") == LIVE_TEST_TOOL_MARKER:
                tool_ok = True
                break
    if not tool_ok:
        _raise_live_failure(spec.id, "tool")
    return {
        "model": spec.id,
        "effort": effort,
        "transport": spec.transport,
        "text_ok": True,
        "tool_ok": True,
        "usage": _usage_total(text_result.get("usage"))
                 + _usage_total(tool_result.get("usage")),
    }


def _read_gateway_runtime(paths: Paths) -> dict[str, Any]:
    try:
        payload = json.loads(read_utf8_text(paths.gateway_runtime))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    version = payload.get("version")
    pid = payload.get("pid")
    port = payload.get("port")
    started_at = payload.get("started_at")
    if version != GATEWAY_RUNTIME_VERSION or isinstance(version, bool):
        return {}
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {}
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        return {}
    if not isinstance(started_at, str) or not started_at:
        return {}
    return {"version": version, "pid": pid, "port": port, "started_at": started_at}


def _remove_gateway_runtime(paths: Paths, owner_pid: int | None = None) -> None:
    try:
        if not paths.gateway_runtime.is_file():
            return
        if owner_pid is not None:
            runtime = _read_gateway_runtime(paths)
            if not runtime or runtime.get("pid") != owner_pid:
                return
        paths.gateway_runtime.unlink()
    except FileNotFoundError:
        pass


def _gateway_probe(port: int, local_token: str, timeout: float = 1.0) -> bool:
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request(
            "POST",
            "/v1/responses",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + local_token,
                "Connection": "close",
            },
        )
        response = conn.getresponse()
        body = response.read(4097)
        if response.status != 400 or len(body) > 4096:
            return False
        payload = json.loads(body.decode("utf-8"))
        return isinstance(payload, dict) and payload.get("code") == "invalid_model"
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _gateway_shutdown(port: int, local_token: str, timeout: float = 2.0) -> bool:
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request(
            "POST",
            "/shutdown",
            body=b"",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + local_token,
                "Content-Length": "0",
                "Connection": "close",
            },
        )
        response = conn.getresponse()
        body = response.read(4097)
        if response.status != 200 or len(body) > 4096:
            return False
        payload = json.loads(body.decode("utf-8"))
        return isinstance(payload, dict) and payload.get("status") == "stopping"
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _spawn_gateway_process(paths: Paths):
    current = platform_name()
    script = Path(__file__) if Path(__file__).is_absolute() else Path(__file__).resolve()
    argv = [sys.executable, str(script), "_gateway-serve", "--codex-home", str(paths.home)]
    if current == "windows":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        creationflags = subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    if current == "macos":
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    raise ManagerError("unsupported", "当前平台不支持后台网关进程。")


def _safe_gateway_runtime_fields(paths: Paths, state: dict[str, Any]) -> dict[str, Any] | None:
    runtime = _read_gateway_runtime(paths)
    if runtime and runtime.get("port") == state["port"]:
        return {"pid": runtime["pid"], "started_at": runtime["started_at"]}
    return None


def _terminate_process_handle(handle) -> None:
    try:
        handle.terminate()
    except Exception:
        try:
            handle.kill()
        except Exception:
            pass
        return
    try:
        handle.wait(timeout=1.0)
    except Exception:
        try:
            handle.kill()
        except Exception:
            pass


def gateway_status(paths: Paths) -> dict[str, Any]:
    try:
        state = read_gateway_state(paths)
    except ManagerError as exc:
        if exc.code == "not_managed":
            return result("gateway_unavailable", running=False)
        raise
    running = _gateway_probe(state["port"], state["local_gateway_token"], timeout=1.0)
    payload = result("ok", running=running, port=state["port"])
    safe = _safe_gateway_runtime_fields(paths, state)
    if safe is not None:
        payload.update(safe)
    return payload


def _gateway_start_locked(paths: Paths, state: dict[str, Any], wait_seconds: float) -> dict[str, Any]:
    if _gateway_probe(state["port"], state["local_gateway_token"], timeout=1.0):
        payload = result("ok", running=True, changed=False, port=state["port"])
        safe = _safe_gateway_runtime_fields(paths, state)
        if safe is not None:
            payload.update(safe)
        return payload
    if not credential_has_key():
        raise ManagerError("credential_missing", "未找到 API Key。")
    try:
        handle = _spawn_gateway_process(paths)
    except Exception:
        raise ManagerError("gateway_start_failed", "无法启动本地网关。") from None
    deadline = time.monotonic() + float(wait_seconds)
    while True:
        if _gateway_probe(state["port"], state["local_gateway_token"], timeout=1.0):
            payload = result("ok", running=True, changed=True, port=state["port"])
            safe = _safe_gateway_runtime_fields(paths, state)
            if safe is not None:
                payload.update(safe)
            else:
                payload["pid"] = getattr(handle, "pid", None)
            return payload
        if time.monotonic() >= deadline:
            _terminate_process_handle(handle)
            _remove_gateway_runtime(paths, owner_pid=getattr(handle, "pid", None))
            raise ManagerError("gateway_start_failed", "无法启动本地网关。") from None
        time.sleep(GATEWAY_POLL_SECONDS)


def gateway_start(paths: Paths, wait_seconds: float = GATEWAY_START_WAIT_SECONDS) -> dict[str, Any]:
    try:
        state = read_gateway_state(paths)
    except ManagerError as exc:
        if exc.code == "not_managed":
            return result("gateway_unavailable", running=False)
        raise
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or wait_seconds <= 0:
        raise ManagerError("invalid_argument", "wait_seconds 必须为正数。")
    with operation_lock(paths):
        state = read_gateway_state(paths)
        return _gateway_start_locked(paths, state, float(wait_seconds))


def _gateway_stop_locked(paths: Paths, state: dict[str, Any], wait_seconds: float) -> tuple[dict[str, Any], bool]:
    had_runtime = paths.gateway_runtime.is_file()
    if not _gateway_probe(state["port"], state["local_gateway_token"], timeout=1.0):
        _remove_gateway_runtime(paths)
        return result("ok", running=False, changed=had_runtime), False
    if not _gateway_shutdown(state["port"], state["local_gateway_token"], timeout=2.0):
        raise ManagerError("gateway_stop_failed", "无法停止本地网关。")
    deadline = time.monotonic() + float(wait_seconds)
    while True:
        if not _gateway_probe(state["port"], state["local_gateway_token"], timeout=1.0):
            _remove_gateway_runtime(paths)
            return result("ok", running=False, changed=True), True
        if time.monotonic() >= deadline:
            raise ManagerError("gateway_stop_failed", "无法停止本地网关。")
        time.sleep(GATEWAY_POLL_SECONDS)


def _validate_profile_locked(
    paths: Paths,
    state_data: dict[str, Any],
    profile: Profile,
    validator=None,
) -> dict[str, Any]:
    registry = state_data["model_registry"]
    spec = get_model(profile.model, profile.effort, registry=registry)
    gateway_state = {
        "version": state_data["version"],
        "local_gateway_token": state_data["local_gateway_token"],
        "port": state_data["port"],
    }
    if validator is not None:
        try:
            validator(profile.model, profile.effort)
        except Exception:
            raise ManagerError("live_test_failed", "实时验证失败。") from None
        return {
            "model": profile.model,
            "effort": profile.effort,
            "transport": spec.transport,
            "text_ok": True,
            "tool_ok": True,
            "usage": 0,
        }
    try:
        started = _gateway_start_locked(
            paths, gateway_state, GATEWAY_START_WAIT_SECONDS)
    except Exception:
        raise ManagerError("live_test_failed", "实时验证失败。") from None
    started_new = bool(isinstance(started, dict) and started.get("changed") is True)
    try:
        probe_result = _live_probe_profile(gateway_state, spec, profile.effort)
        usage = probe_result.get("usage") if isinstance(probe_result, dict) else None
        probe_ok = bool(
            isinstance(probe_result, dict)
            and probe_result.get("text_ok") is True
            and probe_result.get("tool_ok") is True
            and not isinstance(usage, bool)
            and isinstance(usage, int)
            and usage >= 0
        )
    except Exception:
        probe_ok = False
        usage = None
    if not probe_ok:
        if started_new:
            try:
                _gateway_stop_locked(paths, gateway_state, GATEWAY_STOP_WAIT_SECONDS)
            except Exception:
                raise ManagerError(
                    "live_test_cleanup_failed", "实时验证清理失败。") from None
        raise ManagerError("live_test_failed", "实时验证失败。") from None
    return {
        "model": profile.model,
        "effort": profile.effort,
        "transport": spec.transport,
        "text_ok": True,
        "tool_ok": True,
        "usage": usage,
    }


def gateway_stop(paths: Paths, wait_seconds: float = GATEWAY_STOP_WAIT_SECONDS) -> dict[str, Any]:
    try:
        state = read_gateway_state(paths)
    except ManagerError as exc:
        if exc.code == "not_managed":
            return result("gateway_unavailable", running=False)
        raise
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or wait_seconds <= 0:
        raise ManagerError("invalid_argument", "wait_seconds 必须为正数。")
    with operation_lock(paths):
        state = read_gateway_state(paths)
        payload, _ = _gateway_stop_locked(paths, state, float(wait_seconds))
        return payload


def ensure_gateway(paths: Paths, wait_seconds: float = GATEWAY_START_WAIT_SECONDS) -> str:
    started = gateway_start(paths, wait_seconds=wait_seconds)
    if started.get("running") is True:
        state = read_gateway_state(paths)
        return state["local_gateway_token"]
    raise ManagerError("gateway_start_failed", "无法启动本地网关。")


def gateway_serve(paths: Paths, server_factory=None) -> int:
    state = read_gateway_state(paths)
    registry = read_model_registry(paths)
    snapshot = _models_module().registry_snapshot(registry)
    api_key = read_credential_key()
    if api_key is None:
        raise ManagerError("credential_missing", "未找到 API Key。")
    factory = server_factory if server_factory is not None else _gateway_server_module().create_server
    server = factory(
        "127.0.0.1",
        state["port"],
        state["local_gateway_token"],
        api_key,
        log_path=str(paths.gateway_log),
        model_registry_snapshot=snapshot,
    )
    runtime = {
        "version": GATEWAY_RUNTIME_VERSION,
        "pid": os.getpid(),
        "port": state["port"],
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    runtime_bytes = (json.dumps(runtime, ensure_ascii=False) + "\n").encode()
    try:
        atomic_write(paths.gateway_runtime, runtime_bytes, mode=0o600)
    except Exception:
        server.server_close()
        raise
    try:
        server.serve_forever()
        return 0
    finally:
        server.server_close()
        _remove_gateway_runtime(paths, owner_pid=os.getpid())


def compatible_existing(parsed: dict[str, Any], paths: Paths) -> list[str]:
    provider = (parsed.get("model_providers") or {}).get(PROVIDER)
    if not provider:
        return []
    issues: list[str] = []
    if provider.get("wire_api") != "responses":
        issues.append(f"model_providers.{PROVIDER}.wire_api")
    expected = expected_provider_auth(str(paths.home))
    auth = provider.get("auth")
    if not isinstance(auth, dict):
        issues.append(f"model_providers.{PROVIDER}.auth")
        return issues
    for key, value in expected.items():
        if auth.get(key) != value:
            issues.append(f"model_providers.{PROVIDER}.auth.{key}")
    return issues


def _plan_setup_candidates(
    paths: Paths,
    config_text: str,
    previous_manifest: dict[str, Any],
    profile: Profile,
    base: dict[str, Any],
    migrate_deepseek: bool,
):
    parsed = parse_toml_text(config_text) if config_text.strip() else {}
    if migrate_deepseek:
        unmanaged = remove_legacy_blocks(config_text)
        unmanaged = remove_table_and_subtables(unmanaged, "model_providers.deepseek")
        unmanaged = remove_table_and_subtables(unmanaged, "agents.DeepSeek")
        unmanaged = remove_managed_blocks(unmanaged)
        previous_catalog = parsed.get("model_catalog_json")
        previous_features = (parsed.get("features") or {}).get("multi_agent")
        legacy_removed = True
    else:
        unmanaged = remove_managed_blocks(config_text)
        previous_catalog = parsed.get("model_catalog_json")
        previous_features = (parsed.get("features") or {}).get("multi_agent")
        legacy_removed = False
    unmanaged_parsed = parse_toml_text(unmanaged) if unmanaged.strip() else {}
    conflicts = compatible_existing(unmanaged_parsed, paths)
    if conflicts:
        raise ManagerError("conflict", "发现不兼容的现有 opencode-go 配置。", {"fields": conflicts})

    if paths.state.is_file() and previous_manifest.get("state_sha256") == sha256_bytes(paths.state.read_bytes()):
        state_bytes = paths.state.read_bytes()
        try:
            state_payload = json.loads(state_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ManagerError("conflict", "托管状态文件无效。", {"fields": ["state"]}) from None
        state_data = _parse_state_payload(state_payload)
        registry = state_data["model_registry"]
    else:
        state_data = new_state()
        state_bytes = (json.dumps(state_data, ensure_ascii=False, indent=2) + "\n").encode()
        registry = _models_module().registry_from_snapshot(state_data["model_registry"])
    port = int(state_data["port"])

    catalog = merge_catalog(base, registry=registry)
    catalog_bytes = (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode()
    agent_text = expected_agent_text(profile.model, profile.effort)
    agent_bytes = agent_text.encode()
    parse_toml_text(agent_text)

    parts: list[str] = []
    if unmanaged.strip():
        parts.append(unmanaged.rstrip())
    if not (unmanaged_parsed.get("model_providers") or {}).get(PROVIDER):
        parts.append(managed_provider_block(paths, port).rstrip())
    parts.append(managed_role_block(paths).rstrip())
    new_config = "\n\n".join(parts)
    new_config = set_top_level_key(new_config, "model_catalog_json", str(paths.catalog))
    new_config = set_table_bool(new_config, "features", "multi_agent", True)
    parse_toml_text(new_config)
    new_config_bytes = new_config.encode()

    manifest = {
        "schema_version": 2,
        "selected_model": profile.model,
        "selected_effort": profile.effort,
        "installed_at": datetime.now().isoformat(timespec="seconds"),
        "backup": None,
        "enabled": True,
        "managed_provider_block": True,
        "managed_agent_file": True,
        "managed_catalog_selection": True,
        "managed_features_multi_agent": True,
        "previous_model_catalog_json": previous_catalog if isinstance(previous_catalog, str) else None,
        "previous_features_multi_agent": previous_features if isinstance(previous_features, bool) else None,
        "catalog_preexisted": paths.catalog.is_file(),
        "agent_preexisted": paths.agent.is_file(),
        "legacy_role_block_removed": legacy_removed,
        "adopted_existing": bool(previous_manifest) or paths.catalog.is_file() or paths.agent.is_file(),
        "config_sha256": sha256_bytes(new_config_bytes),
        "catalog_sha256": sha256_bytes(catalog_bytes),
        "agent_sha256": sha256_bytes(agent_bytes),
        "state_sha256": sha256_bytes(state_bytes),
    }

    if previous_manifest:
        for key in (
            "schema_version",
            "previous_model_catalog_json",
            "previous_features_multi_agent",
            "catalog_preexisted",
            "agent_preexisted",
            "managed_provider_block",
            "managed_agent_file",
            "managed_catalog_selection",
            "managed_features_multi_agent",
            "legacy_role_block_removed",
            "adopted_existing",
        ):
            if key in previous_manifest:
                manifest[key] = previous_manifest[key]

    if previous_manifest:
        same_profile = (
            previous_manifest.get("selected_model") == profile.model
            and previous_manifest.get("selected_effort") == profile.effort
        )
        files_ok = all(
            path.is_file() and sha256_bytes(path.read_bytes()) == previous_manifest.get(key)
            for key, path in (
                ("config_sha256", paths.config),
                ("catalog_sha256", paths.catalog),
                ("agent_sha256", paths.agent),
                ("state_sha256", paths.state),
            )
        )
        managed_ok = bool(
            previous_manifest.get("managed_provider_block")
            and previous_manifest.get("managed_agent_file")
            and previous_manifest.get("managed_catalog_selection")
        )
        semantic_ok = False
        if config_text.strip():
            try:
                parsed_now = parse_toml_text(config_text)
                provider_now = (parsed_now.get("model_providers") or {}).get(PROVIDER)
                role_now = (parsed_now.get("agents") or {}).get(ROLE)
                catalog_now = parsed_now.get("model_catalog_json")
                features_now = (parsed_now.get("features") or {}).get("multi_agent")
                semantic_ok = bool(
                    provider_now
                    and role_now
                    and isinstance(catalog_now, str)
                    and Path(catalog_now).expanduser().resolve() == paths.catalog
                    and features_now is True
                    and paths.agent.is_file()
                    and read_utf8_text(paths.agent) == expected_agent_text(profile.model, profile.effort)
                    and paths.state.is_file()
                )
            except ManagerError:
                semantic_ok = False
        catalog_matches = (
            paths.catalog.is_file()
            and sha256_bytes(paths.catalog.read_bytes()) == sha256_bytes(catalog_bytes)
        )
        if same_profile and files_ok and managed_ok and semantic_ok and catalog_matches:
            return None
    return {
        "config": new_config_bytes,
        "catalog": catalog_bytes,
        "agent": agent_bytes,
        "state": state_bytes,
        "manifest": manifest,
    }


def setup(
    paths: Paths,
    api_key_stdin: bool = False,
    migrate_deepseek: bool = False,
    skip_live_test: bool = False,
    stdin=None,
    catalog_loader=None,
    validator=None,
) -> dict[str, Any]:
    config_text = read_utf8_text(paths.config) if paths.config.is_file() else ""
    if detect_legacy_config(config_text) and not migrate_deepseek:
        raise ManagerError(
            "migration_required",
            "检测到旧 DeepSeek 托管配置；需要 --migrate-deepseek 显式迁移。",
        )
    if not credential_available():
        raise ManagerError("unsupported", "当前平台没有可用的系统凭据库。")
    pending_secret: str | None = None
    if not credential_has_key():
        if not api_key_stdin:
            return result("credential_missing", credential="opencode_go_api_key")
        stream = stdin if stdin is not None else sys.stdin
        pending_secret = stream.readline().strip()
        if not pending_secret:
            raise ManagerError("credential_missing", "标准输入中没有 API Key。")

    with operation_lock(paths):
        previous_manifest = read_manifest(paths)
        if previous_manifest:
            _verify_managed_hashes(paths, previous_manifest)
        backup: Path | None = None
        credential_created = False
        try:
            profile = resolve_managed_profile(previous_manifest, paths)
            if profile is None:
                profile = default_profile()
            if pending_secret is not None:
                store_credential_key(pending_secret)
                pending_secret = ""
                credential_created = True
            base = load_base_catalog(paths, config_text, catalog_loader)
            plan = _plan_setup_candidates(paths, config_text, previous_manifest, profile, base, migrate_deepseek)
            if plan is None:
                _static_verify(paths, profile)
                if not skip_live_test:
                    _validate_profile_locked(
                        paths, _read_managed_state(paths), profile, validator)
                return result(
                    "configured",
                    changed=False,
                    active_profile=profile_json(profile),
                    previous_profile=profile_json(profile),
                    new_task_required=False,
                    skipped_live_test=bool(skip_live_test),
                )
            backup = make_backup(paths)
            manifest = plan["manifest"]
            manifest["backup"] = str(backup)
            manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
            atomic_write(paths.state, plan["state"], mode=0o600)
            atomic_write(paths.catalog, plan["catalog"], mode=0o644)
            atomic_write(paths.agent, plan["agent"], mode=0o644)
            atomic_write(paths.config, plan["config"], mode=0o600)
            atomic_write(paths.manifest, manifest_bytes, mode=0o600)
            _static_verify(paths, profile)
            if not skip_live_test:
                _validate_profile_locked(
                    paths, _read_managed_state(paths), profile, validator)
        except Exception:
            if backup is not None:
                restore_backup(paths, backup)
            if credential_created:
                remove_credential_key()
            raise
        return result(
            "configured",
            changed=True,
            active_profile=profile_json(profile),
            previous_profile=profile_json(profile),
            new_task_required=True,
            backup=str(backup),
            skipped_live_test=bool(skip_live_test),
        )


def repair(
    paths: Paths,
    api_key_stdin: bool = False,
    migrate_deepseek: bool = False,
    skip_live_test: bool = False,
    stdin=None,
    catalog_loader=None,
    validator=None,
) -> dict[str, Any]:
    return setup(
        paths, api_key_stdin, migrate_deepseek, skip_live_test, stdin,
        catalog_loader, validator)


def status(paths: Paths) -> dict[str, Any]:
    manifest = read_manifest(paths)
    checks: dict[str, Any] = {}
    config_text = read_utf8_text(paths.config) if paths.config.is_file() else ""
    parsed: dict[str, Any] = {}
    if config_text.strip():
        try:
            parsed = parse_toml_text(config_text)
            checks["config_valid"] = True
        except ManagerError:
            parsed = {}
            checks["config_valid"] = False
    else:
        checks["config_valid"] = False
    provider = (parsed.get("model_providers") or {}).get(PROVIDER)
    role = (parsed.get("agents") or {}).get(ROLE)
    checks["provider_registered"] = bool(provider)
    checks["role_registered"] = bool(role)
    catalog_value = parsed.get("model_catalog_json")
    checks["catalog_selected"] = (
        isinstance(catalog_value, str)
        and Path(catalog_value).expanduser().resolve() == paths.catalog
    )
    drift: list[str] = []
    enabled = True
    if manifest:
        enabled = _manifest_enabled(manifest)
        pairs = (
            ("config_sha256", paths.config),
            ("catalog_sha256", paths.catalog),
            ("agent_sha256", paths.agent),
            ("state_sha256", paths.state),
        )
        if not enabled:
            pairs = (
                ("config_sha256", paths.config),
                ("catalog_sha256", paths.catalog),
                ("state_sha256", paths.state),
            )
        for key, path in pairs:
            expected = manifest.get(key)
            ok = isinstance(expected, str) and path.is_file() and sha256_bytes(path.read_bytes()) == expected
            checks[key] = ok
            if not ok:
                drift.append(str(path))
        if not enabled:
            agent_ok = manifest.get("agent_sha256") is None and not paths.agent.is_file()
            checks["agent_sha256"] = agent_ok
            if not agent_ok:
                drift.append(str(paths.agent))
    active = None
    source = None
    registry = None
    if manifest and not drift:
        try:
            registry = read_model_registry(paths)
            checks["model_registry_valid"] = True
        except ManagerError:
            checks["model_registry_valid"] = False
    if registry is not None:
        profile = resolve_managed_profile(manifest, paths, registry=registry)
    elif manifest and not drift and checks.get("model_registry_valid") is False:
        profile = None
    elif manifest:
        model = manifest.get("selected_model")
        effort = manifest.get("selected_effort")
        if isinstance(model, str) and isinstance(effort, str):
            try:
                get_model(model, effort)
                profile = Profile(model, effort)
            except ManagerError:
                profile = None
        else:
            profile = None
    else:
        profile = None
    if profile is not None:
        active = profile_json(profile)
        source = "manifest"
        checks["agent_content_valid"] = (
            paths.agent.is_file()
            and read_utf8_text(paths.agent) == expected_agent_text(profile.model, profile.effort)
        )
    else:
        checks["agent_content_valid"] = False
    legacy = detect_legacy_config(config_text) if config_text.strip() else False
    payload = {
        "active_profile": active,
        "profile_source": source,
        "credential_backend": credential_backend(),
        "credential_present": None,
        "managed_hashes": {
            key: manifest.get(key)
            for key in ("config_sha256", "catalog_sha256", "agent_sha256", "state_sha256")
        },
        "drift": drift,
        "checks": checks,
    }
    if checks.get("model_registry_valid") is False:
        return result("conflict", **payload)
    if not manifest:
        return result("unconfigured", **payload)
    if drift:
        return result("conflict", **payload)
    if legacy and not provider:
        return result("migration_required", **payload)
    if not enabled:
        disabled = bool(
            checks.get("config_valid")
            and provider
            and not role
            and checks.get("catalog_selected")
            and checks.get("config_sha256")
            and checks.get("catalog_sha256")
            and checks.get("state_sha256")
            and checks.get("agent_sha256")
            and manifest
        )
        if disabled:
            return result("disabled", **payload)
    configured = bool(
        checks.get("config_valid")
        and provider
        and role
        and checks.get("catalog_selected")
        and checks.get("config_sha256")
        and checks.get("catalog_sha256")
        and checks.get("agent_sha256")
        and checks.get("state_sha256")
        and checks.get("agent_content_valid")
        and manifest
    )
    if configured:
        try:
            payload["credential_present"] = credential_has_key()
        except ManagerError:
            payload["credential_present"] = None
        return result("configured", **payload)
    return result("unconfigured", **payload)


def profile_list(paths=None) -> dict[str, Any]:
    registry = None
    if paths is not None:
        manifest = read_manifest(paths)
        if manifest:
            registry = read_model_registry(paths)
    validated = _validated_registry(registry)
    profiles = [
        {"model": model_id, "effort": effort}
        for model_id, spec in validated.items()
        if spec.status == "active"
        for effort in spec.efforts
    ]
    return {
        "status": "ok",
        "profiles": profiles,
        "default_profile": profile_json(default_profile()),
    }


def profile_show(paths: Paths) -> dict[str, Any]:
    return status(paths)


def models_list(paths=None) -> dict[str, Any]:
    registry = None
    if paths is not None:
        manifest = read_manifest(paths)
        if manifest:
            registry = read_model_registry(paths)
    validated = _validated_registry(registry)
    items = []
    for spec in validated.values():
        record = _catalog_record(spec)
        record["status"] = spec.status
        items.append(record)
    return {"status": "ok", "models": items, "count": len(items)}


def live_test(paths, all_models=False, starter=None, profile_runner=None):
    if not isinstance(all_models, bool):
        raise ManagerError("invalid_argument", "all_models must be a bool.")
    manifest = read_manifest(paths)
    if not manifest:
        return result("gateway_unavailable", running=False)
    _verify_managed_hashes(paths, manifest)
    state_data = _read_managed_state(paths, manifest)
    if not _manifest_enabled(manifest):
        raise ManagerError("disabled", "managed tool is disabled.")
    registry = state_data["model_registry"]
    selected = resolve_managed_profile(manifest, paths, registry=registry)
    if selected is None:
        raise ManagerError(
            "conflict", "managed profile is invalid.",
            {"fields": ["selected_profile"]})
    models = _models_module()
    if set(registry) != set(models.MODELS) or len(registry) != len(models.MODELS):
        raise ManagerError(
            "conflict", "model registry is invalid.",
            {"fields": ["model_registry"]})
    if all_models:
        unavailable = sorted(
            model_id for model_id, spec in registry.items()
            if spec.status != "active")
        if unavailable:
            raise ManagerError(
                "model_unavailable", "reviewed models are unavailable.",
                {"models": unavailable})
        profiles = [
            (model_id, spec.default_effort)
            for model_id, spec in registry.items()
        ]
    else:
        get_model(selected.model, selected.effort, registry=registry)
        profiles = [(selected.model, selected.effort)]
    run_starter = starter if starter is not None else gateway_start
    try:
        started = run_starter(paths)
    except ManagerError as exc:
        if exc.code == "credential_missing":
            return result("gateway_unavailable", running=False)
        raise
    except Exception:
        _raise_live_failure(selected.model, "gateway")
    if not isinstance(started, dict) or started.get("running") is not True:
        _raise_live_failure(selected.model, "gateway")
    gateway_state = read_gateway_state(paths)
    run_runner = profile_runner if profile_runner is not None else _live_probe_profile
    records = []
    for model_id, effort in profiles:
        spec = registry[model_id]
        try:
            runner_result = run_runner(gateway_state, spec, effort)
        except ManagerError as exc:
            if exc.code == "live_test_failed":
                _raise_live_failure(
                    model_id, "profile",
                    status=exc.details.get("status"),
                    code=exc.details.get("code"))
            _raise_live_failure(model_id, "profile")
        except Exception:
            _raise_live_failure(model_id, "profile")
        if (not isinstance(runner_result, dict)
                or runner_result.get("text_ok") is not True
                or runner_result.get("tool_ok") is not True):
            _raise_live_failure(model_id, "profile")
        usage = runner_result.get("usage")
        if isinstance(usage, bool) or not isinstance(usage, int) or usage < 0:
            _raise_live_failure(model_id, "profile")
        records.append({
            "model": model_id,
            "effort": effort,
            "transport": spec.transport,
            "text_ok": True,
            "tool_ok": True,
            "usage": usage,
        })
    return result(
        "ok",
        all_models=all_models,
        active_profile=profile_json(selected),
        tested=len(records),
        gateway_started=started.get("changed") is True,
        results=records,
    )


def models_refresh(paths: Paths, fetcher=None, stop_locked=None) -> dict[str, Any]:
    manifest = read_manifest(paths)
    if not manifest:
        raise ManagerError("not_managed", "没有找到本工具的管理记录，拒绝刷新模型目录。")
    _verify_managed_hashes(paths, manifest)
    state_data = _read_managed_state(paths, manifest)
    profile = resolve_managed_profile(manifest, paths, registry=state_data["model_registry"])
    if profile is None:
        raise ManagerError("conflict", "管理记录中的 Profile 无效。", {"fields": ["selected_profile"]})
    source = fetcher if fetcher is not None else fetch_refresh_payloads
    try:
        payloads = source()
    except ManagerError:
        raise
    except Exception:
        raise ManagerError("refresh_payload_invalid", "模型目录响应无效。") from None
    if not isinstance(payloads, tuple) or len(payloads) != 2:
        raise ManagerError("refresh_payload_invalid", "模型目录响应无效。")
    go_payload, models_dev_payload = payloads

    with operation_lock(paths):
        manifest = read_manifest(paths)
        if not manifest:
            raise ManagerError("not_managed", "没有找到本工具的管理记录，拒绝刷新模型目录。")
        _verify_managed_hashes(paths, manifest)
        state_data = _read_managed_state(paths, manifest)
        profile = resolve_managed_profile(manifest, paths, registry=state_data["model_registry"])
        if profile is None:
            raise ManagerError("conflict", "管理记录中的 Profile 无效。", {"fields": ["selected_profile"]})
        old_registry = state_data["model_registry"]
        get_model(profile.model, profile.effort, registry=old_registry)
        models = _models_module()
        try:
            new_registry, report = models.reconcile_registry(
                go_payload,
                models_dev_payload,
                current=old_registry,
                selected_model=profile.model,
                selected_effort=profile.effort,
            )
        except models.ModelError as exc:
            raise ManagerError(exc.code, str(exc)) from exc
        try:
            new_registry_snapshot = models.registry_snapshot(new_registry)
        except models.ModelError as exc:
            raise ManagerError(exc.code, str(exc)) from exc

        candidate_state = {
            "version": state_data["version"],
            "local_gateway_token": state_data["local_gateway_token"],
            "port": state_data["port"],
            "model_registry": new_registry_snapshot,
        }
        state_bytes = (json.dumps(candidate_state, ensure_ascii=False, indent=2) + "\n").encode()
        old_state_bytes = paths.state.read_bytes()

        try:
            current_catalog = json.loads(read_utf8_text(paths.catalog))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ManagerError("conflict", "模型目录无效。", {"fields": ["catalog"]}) from None
        if (
            not isinstance(current_catalog, dict)
            or not isinstance(current_catalog.get("models"), list)
            or not current_catalog["models"]
        ):
            raise ManagerError("conflict", "模型目录无效。", {"fields": ["catalog"]})
        candidate_catalog = merge_catalog(current_catalog, registry=new_registry)
        catalog_bytes = (json.dumps(candidate_catalog, ensure_ascii=False, indent=2) + "\n").encode()
        old_catalog_bytes = paths.catalog.read_bytes()

        active_profile = profile_json(profile)
        changed = state_bytes != old_state_bytes or catalog_bytes != old_catalog_bytes
        if not changed:
            return result(
                "ok",
                changed=False,
                active_profile=active_profile,
                report=report,
                backup=None,
                gateway_stopped=False,
                gateway_restart_required=False,
                new_task_required=False,
            )

        stopper = stop_locked if stop_locked is not None else _gateway_stop_locked
        gateway_state = {
            "version": state_data["version"],
            "local_gateway_token": state_data["local_gateway_token"],
            "port": state_data["port"],
        }
        try:
            _stop_public, was_running = stopper(paths, gateway_state, GATEWAY_STOP_WAIT_SECONDS)
        except ManagerError:
            raise
        except Exception:
            raise ManagerError("gateway_stop_failed", "无法停止本地网关。") from None
        if not isinstance(was_running, bool):
            raise ManagerError("gateway_stop_failed", "无法停止本地网关。")

        backup: Path | None = None
        try:
            backup = make_backup(paths)
            new_manifest = dict(manifest)
            new_manifest["state_sha256"] = sha256_bytes(state_bytes)
            new_manifest["catalog_sha256"] = sha256_bytes(catalog_bytes)
            new_manifest["backup"] = str(backup)
            atomic_write(paths.state, state_bytes, mode=0o600)
            atomic_write(paths.catalog, catalog_bytes, mode=0o644)
            write_manifest(paths, new_manifest)
            _verify_managed_hashes(paths, new_manifest)
            persisted_registry = read_model_registry(paths, new_manifest)
            get_model(profile.model, profile.effort, registry=persisted_registry)
            if _manifest_enabled(new_manifest):
                _static_verify(paths, profile)
            else:
                if paths.agent.is_file():
                    raise ManagerError("conflict", "禁用状态下不应存在 agent 文件。")
        except Exception:
            if backup is None:
                raise ManagerError(
                    "refresh_transaction_failed", "models refresh failed before managed writes"
                ) from None
            try:
                restore_backup(paths, backup)
            except Exception:
                raise ManagerError(
                    "refresh_rollback_failed", "models refresh failed and rollback also failed"
                ) from None
            raise ManagerError(
                "refresh_transaction_failed", "models refresh failed and managed files were restored"
            ) from None
        return result(
            "ok",
            changed=True,
            active_profile=active_profile,
            report=report,
            backup=str(backup),
            gateway_stopped=was_running,
            gateway_restart_required=was_running,
            new_task_required=True,
        )


def set_profile(
    paths: Paths,
    model: str,
    effort: str,
    skip_live_test: bool,
    validator=None,
) -> dict[str, Any]:
    manifest = read_manifest(paths)
    if manifest:
        _verify_managed_hashes(paths, manifest)
        registry = read_model_registry(paths)
        get_model(model, effort, registry=registry)
    else:
        get_model(model, effort)
    with operation_lock(paths):
        manifest = read_manifest(paths)
        if not manifest:
            raise ManagerError("not_managed", "没有找到本工具的管理记录，拒绝修改现有配置。")
        _verify_managed_hashes(paths, manifest)
        registry = read_model_registry(paths)
        get_model(model, effort, registry=registry)
        previous = resolve_managed_profile(manifest, paths, registry=registry)
        if previous is None:
            raise ManagerError("conflict", "管理记录中的 Profile 无效或不可信，拒绝切换。")
        if previous.model == model and previous.effort == effort:
            _static_verify(paths, previous)
            if not skip_live_test:
                _validate_profile_locked(
                    paths, _read_managed_state(paths), previous, validator)
            return result(
                "configured",
                previous_profile=profile_json(previous),
                active_profile=profile_json(previous),
                changed=False,
                backup=None,
                restart_required=False,
                new_task_required=False,
                skipped_live_test=bool(skip_live_test),
            )
        agent_text = expected_agent_text(model, effort)
        agent_bytes = agent_text.encode()
        parse_toml_text(agent_text)
        backup = make_backup(paths)
        new_manifest = dict(manifest)
        new_manifest["selected_model"] = model
        new_manifest["selected_effort"] = effort
        new_manifest["installed_at"] = datetime.now().isoformat(timespec="seconds")
        new_manifest["agent_sha256"] = sha256_bytes(agent_bytes)
        new_manifest["backup"] = str(backup)
        try:
            atomic_write(paths.agent, agent_bytes, mode=0o644)
            write_manifest(paths, new_manifest)
            _static_verify(paths, Profile(model, effort))
            if not skip_live_test:
                _validate_profile_locked(
                    paths,
                    _read_managed_state(paths),
                    Profile(model, effort),
                    validator,
                )
        except Exception:
            restore_backup(paths, backup)
            raise
        return result(
            "configured",
            previous_profile=profile_json(previous),
            active_profile=profile_json(Profile(model, effort)),
            changed=True,
            backup=str(backup),
            restart_required=True,
            new_task_required=True,
            skipped_live_test=bool(skip_live_test),
        )


def disable(paths: Paths) -> dict[str, Any]:
    gateway_stop(paths)
    with operation_lock(paths):
        manifest = read_manifest(paths)
        if not manifest:
            raise ManagerError("not_managed", "没有找到本工具的管理记录，拒绝修改现有配置。")
        _verify_managed_hashes(paths, manifest)
        if not _manifest_enabled(manifest):
            return result(
                "disabled",
                changed=False,
                agent_preserved=not bool(manifest.get("managed_agent_file")),
                credential_preserved=credential_has_key(),
            )
        text = read_utf8_text(paths.config) if paths.config.is_file() else ""
        updated = remove_marked_block(text, ROLE_BEGIN, ROLE_END)
        updated = remove_table_and_subtables(updated, f"agents.{ROLE}")
        parse_toml_text(updated)
        updated_bytes = updated.encode()
        backup = make_backup(paths)
        disabled_manifest = dict(manifest)
        disabled_manifest["enabled"] = False
        disabled_manifest["config_sha256"] = sha256_bytes(updated_bytes)
        disabled_manifest["agent_sha256"] = None
        disabled_manifest["backup"] = str(backup)
        try:
            atomic_write(paths.config, updated_bytes, mode=0o600)
            if manifest.get("managed_agent_file") and paths.agent.is_file():
                paths.agent.unlink()
            write_manifest(paths, disabled_manifest)
        except Exception:
            restore_backup(paths, backup)
            raise
        return result(
            "disabled",
            changed=True,
            agent_preserved=not bool(manifest.get("managed_agent_file")),
            credential_preserved=credential_has_key(),
        )


def uninstall(paths: Paths, remove_credential: bool = False) -> dict[str, Any]:
    gateway_stop(paths)
    with operation_lock(paths):
        manifest = read_manifest(paths)
        if not manifest:
            raise ManagerError("not_managed", "没有找到本工具的管理记录，拒绝修改现有配置。")
        _verify_managed_hashes(paths, manifest)
        backup = make_backup(paths)
        try:
            if paths.config.is_file():
                text = read_utf8_text(paths.config)
                updated = remove_managed_blocks(text)
                updated = remove_table_and_subtables(updated, f"model_providers.{PROVIDER}")
                updated = remove_table_and_subtables(updated, f"agents.{ROLE}")
                if manifest.get("managed_catalog_selection"):
                    previous_catalog = manifest.get("previous_model_catalog_json")
                    if previous_catalog is None:
                        updated = remove_top_level_key_if_value(updated, "model_catalog_json", str(paths.catalog))
                    elif top_level_key(updated, "model_catalog_json") == str(paths.catalog):
                        updated = set_top_level_key(updated, "model_catalog_json", previous_catalog)
                if manifest.get("managed_features_multi_agent"):
                    previous_features = manifest.get("previous_features_multi_agent")
                    if isinstance(previous_features, bool):
                        updated = set_table_bool(updated, "features", "multi_agent", previous_features)
                    else:
                        updated = remove_table_bool_if_value(updated, "features", "multi_agent", True)
                parse_toml_text(updated)
                atomic_write(paths.config, updated.encode(), mode=0o600)
            catalog_removed = False
            agent_removed = False
            if paths.catalog.is_file():
                paths.catalog.unlink()
                catalog_removed = True
            if manifest.get("managed_agent_file") and paths.agent.is_file():
                paths.agent.unlink()
                agent_removed = True
            if paths.manifest.is_file():
                paths.manifest.unlink()
            if paths.state.is_file():
                paths.state.unlink()
            _remove_gateway_runtime(paths)
            if paths.gateway_log.is_file():
                paths.gateway_log.unlink()
        except Exception:
            restore_backup(paths, backup)
            raise
        removed_credential = remove_credential_key() if remove_credential else False
        return result(
            "uninstalled",
            changed=True,
            catalog_removed=catalog_removed,
            agent_removed=agent_removed,
            credential_removed=removed_credential,
        )


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "_gateway-token":
        helper_parser = argparse.ArgumentParser(add_help=False)
        helper_parser.add_argument("--codex-home", required=True)
        try:
            helper_args = helper_parser.parse_args(args_list[1:])
            token = ensure_gateway(resolve_paths(helper_args.codex_home))
            sys.stdout.write(token + "\n")
            return 0
        except ManagerError as exc:
            sys.stderr.write(f"gateway_token_failed:{exc.code}\n")
            return 2
        except Exception:
            sys.stderr.write("gateway_token_failed:failed\n")
            return 1
    if args_list and args_list[0] == "_gateway-serve":
        helper_parser = argparse.ArgumentParser(add_help=False)
        helper_parser.add_argument("--codex-home", required=True)
        try:
            helper_args = helper_parser.parse_args(args_list[1:])
            gateway_serve(resolve_paths(helper_args.codex_home))
            return 0
        except ManagerError as exc:
            sys.stderr.write(f"gateway_serve_failed:{exc.code}\n")
            return 2
        except Exception:
            sys.stderr.write("gateway_serve_failed:failed\n")
            return 1

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--codex-home")
    common.add_argument("--json", action="store_true")

    subparsers.add_parser("status", parents=[common])
    setup_parser = subparsers.add_parser("setup", parents=[common])
    setup_parser.add_argument("--api-key-stdin", action="store_true")
    setup_parser.add_argument("--skip-live-test", action="store_true")
    setup_parser.add_argument("--migrate-deepseek", action="store_true")
    repair_parser = subparsers.add_parser("repair", parents=[common])
    repair_parser.add_argument("--api-key-stdin", action="store_true")
    repair_parser.add_argument("--skip-live-test", action="store_true")
    repair_parser.add_argument("--migrate-deepseek", action="store_true")
    subparsers.add_parser("disable", parents=[common])
    uninstall_parser = subparsers.add_parser("uninstall", parents=[common])
    uninstall_parser.add_argument("--remove-credential", action="store_true")

    profile_parser = subparsers.add_parser("profile", parents=[common])
    profile_subparsers = profile_parser.add_subparsers(dest="profile_command", required=True)
    profile_subparsers.add_parser("list", parents=[common])
    profile_subparsers.add_parser("show", parents=[common])
    profile_set_parser = profile_subparsers.add_parser("set", parents=[common])
    profile_set_parser.add_argument("--model", required=True)
    profile_set_parser.add_argument("--effort", required=True)
    profile_set_parser.add_argument("--skip-live-test", action="store_true")

    models_parser = subparsers.add_parser("models", parents=[common])
    models_subparsers = models_parser.add_subparsers(dest="models_command", required=True)
    models_subparsers.add_parser("list", parents=[common])
    models_subparsers.add_parser("refresh", parents=[common])

    gateway_parser = subparsers.add_parser("gateway", parents=[common])
    gateway_subparsers = gateway_parser.add_subparsers(dest="gateway_command", required=True)
    for name in ("status", "start", "stop"):
        gateway_subparsers.add_parser(name, parents=[common])

    test_parser = subparsers.add_parser("test", parents=[common])
    test_parser.add_argument("--all-models", action="store_true")

    args = parser.parse_args(args_list)
    paths = resolve_paths(args.codex_home)
    try:
        if args.command == "profile":
            if args.profile_command == "list":
                payload = profile_list(paths)
            elif args.profile_command == "show":
                payload = profile_show(paths)
            else:
                payload = set_profile(paths, args.model, args.effort, args.skip_live_test)
        elif args.command == "models":
            if args.models_command == "list":
                payload = models_list(paths)
            else:
                payload = models_refresh(paths)
        elif args.command == "gateway":
            if args.gateway_command == "status":
                payload = gateway_status(paths)
            elif args.gateway_command == "start":
                payload = gateway_start(paths)
            else:
                payload = gateway_stop(paths)
        elif args.command == "test":
            payload = live_test(paths, args.all_models)
        elif args.command == "status":
            payload = status(paths)
        elif args.command == "setup":
            payload = setup(paths, args.api_key_stdin, args.migrate_deepseek, args.skip_live_test, sys.stdin)
        elif args.command == "repair":
            payload = repair(paths, args.api_key_stdin, args.migrate_deepseek, args.skip_live_test, sys.stdin)
        elif args.command == "disable":
            payload = disable(paths)
        else:
            payload = uninstall(paths, getattr(args, "remove_credential", False))
        emit(payload, args.json)
        return 0 if payload["status"] in {
            "configured",
            "disabled",
            "uninstalled",
            "ok",
            "not_implemented",
        } else 2
    except ManagerError as exc:
        emit(result(exc.code, message=str(exc), **exc.details), args.json)
        return 2
    except Exception as exc:
        emit(result("failed", message=f"{type(exc).__name__}: {exc}"), args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
