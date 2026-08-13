"""Local standard-library HTTP/SSE gateway for the OpenCode Go subagent.

Task DS-20260811-13 REV-3 GREEN.  This module is the sole HTTP/upstream owner:
it validates requests, enforces the bounded concurrency cap, calls the frozen
protocol core (opencode_gateway.py / opencode_models.py), streams translated
Codex Responses SSE to the local client, and writes allowlisted JSONL logs.

Security invariants (INV-1/2/3): bind only 127.0.0.1; exact local Bearer token
via hmac.compare_digest; reject framing/media/JSON/model/effort/tool/modality
errors before any upstream connection; secrets stay memory-only, never appear
in repr/log output, and the local token is never forwarded upstream.

Python 3.11+ standard library only.
"""

from __future__ import annotations

import email.utils
import hmac
import http.client
import importlib.util
import json
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PRODUCTION_BASE = "https://opencode.ai/zen/go/v1"
DEFAULT_MAX_CONCURRENT = 8
DEFAULT_MAX_REQUEST_BYTES = 33554432
DEFAULT_CONNECT_TIMEOUT = 30.0
DEFAULT_STREAM_IDLE_TIMEOUT = 650.0

SANITIZED_REQUEST_ID = "sanitized"
_SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_PRODUCTION_HOST = "opencode.ai"
_LOOPBACK_HOST = "127.0.0.1"

FAILURE_CODE_TRUNCATED = "upstream_stream_truncated"
FAILURE_CODE_IDLE = "upstream_idle_timeout"
FAILURE_CODE_STREAM = "upstream_stream_failed"
CONNECT_ERROR_CODE = "upstream_unavailable"


def _load_dependency(name, filename):
    """Load a frozen sibling module by path; never modifies those files."""
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load frozen dependency {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_GATEWAY = _load_dependency("opencode_gateway_server_dep", "opencode_gateway.py")
_MODELS = _load_dependency("opencode_models_server_dep", "opencode_models.py")


def _sanitize_request_id(value):
    """Return a safe upstream request ID or the fixed sanitized value."""
    if isinstance(value, str) and _SAFE_REQUEST_ID_RE.fullmatch(value):
        return value
    return SANITIZED_REQUEST_ID


def _sanitize_retry_after(value):
    """Return a valid Retry-After value, or None when it must be omitted."""
    if not isinstance(value, str) or not value:
        return None
    if len(value) > 128 or not value.isascii():
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    if value.isdigit():
        return value
    try:
        email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    return value


def _parse_upstream_base(upstream_base, allow_test_http):
    """Validate the fixed upstream base; return (scheme, host, port, base_path)."""
    if not isinstance(upstream_base, str) or not upstream_base:
        raise ValueError("upstream_base must be a non-empty string")
    if not allow_test_http and upstream_base != PRODUCTION_BASE:
        raise ValueError(
            "production upstream_base must be exactly " + PRODUCTION_BASE
        )
    parts = urllib.parse.urlsplit(upstream_base)
    if parts.scheme not in ("http", "https"):
        raise ValueError("upstream scheme must be http or https")
    if parts.username is not None or parts.password is not None:
        raise ValueError("upstream userinfo is not allowed")
    if parts.query or parts.fragment:
        raise ValueError("upstream query/fragment are not allowed")
    host = parts.hostname
    if not host:
        raise ValueError("upstream host is missing")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid upstream port") from exc
    if allow_test_http:
        if parts.scheme != "http" or host != _LOOPBACK_HOST or port is None:
            raise ValueError("test upstream must be loopback http://127.0.0.1:<port>")
        if parts.path not in ("", "/"):
            raise ValueError("test upstream must not include a path")
        base_path = ""
    else:
        if parts.scheme != "https" or host != _PRODUCTION_HOST:
            raise ValueError("production upstream must be https://opencode.ai")
        if port not in (None, 443):
            raise ValueError("production upstream port must be 443")
        base_path = parts.path.rstrip("/")
    return parts.scheme, host, port, base_path


def _iter_upstream_chunks(response):
    """Yield JSON payload dicts from an upstream SSE body (data: lines only)."""
    data_lines = []
    while True:
        line = response.readline()
        if not line:
            break
        if line in (b"\r\n", b"\n"):
            if data_lines:
                text = "\n".join(data_lines).strip()
                data_lines = []
                if text == "[DONE]":
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise _GATEWAY.GatewayError(
                        "malformed_chunk", 400, "malformed upstream SSE data"
                    ) from exc
                if isinstance(payload, dict):
                    yield payload
            continue
        lowered = line.lower()
        if lowered.startswith(b"data:"):
            data_lines.append(line[len(b"data:"):].strip().decode("utf-8"))
    if data_lines:
        text = "\n".join(data_lines).strip()
        if text != "[DONE]":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise _GATEWAY.GatewayError(
                    "malformed_chunk", 400, "malformed upstream SSE data"
                ) from exc
            if isinstance(payload, dict):
                yield payload


class GatewayServer(ThreadingHTTPServer):
    """Threading HTTP server with a bounded inference semaphore and safe shutdown."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr,
        handler,
        *,
        local_token,
        api_key,
        upstream_scheme,
        upstream_host,
        upstream_port,
        upstream_base_path,
        log_path,
        max_concurrent,
        max_request_bytes,
        connect_timeout,
        stream_idle_timeout,
        model_registry=None,
        model_registry_snapshot=None,
    ):
        self._local_token = local_token
        self._api_key = api_key
        self._upstream_scheme = upstream_scheme
        self._upstream_host = upstream_host
        self._upstream_port = upstream_port
        self._upstream_base_path = upstream_base_path
        self._log_path = log_path
        self._max_concurrent = max_concurrent
        self._max_request_bytes = max_request_bytes
        self._connect_timeout = connect_timeout
        self._stream_idle_timeout = stream_idle_timeout
        self._model_registry = model_registry
        self._model_registry_snapshot = model_registry_snapshot
        self._semaphore = threading.BoundedSemaphore(max_concurrent)
        self._reasoning_store = _GATEWAY.ReasoningStore()
        self._log_lock = threading.Lock()
        self._serve_lock = threading.Lock()
        self._serving = False
        super().__init__(addr, handler)

    def serve_forever(self, poll_interval=0.5):
        with self._serve_lock:
            self._serving = True
        try:
            super().serve_forever(poll_interval)
        finally:
            with self._serve_lock:
                self._serving = False

    def shutdown(self):
        with self._serve_lock:
            serving = self._serving
        if not serving:
            return
        super().shutdown()

    def __repr__(self):
        host, port = self.server_address
        return f"<GatewayServer host={host} port={port}>"


class _GatewayHandler(BaseHTTPRequestHandler):
    """Local gateway handler: /health, /v1/responses, /shutdown."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.close_connection = True
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"code": "not_found", "message": "not found"})

    def do_POST(self):
        self.close_connection = True
        if self.path == "/v1/responses":
            if not self._authorized():
                self._send_json(
                    401, {"code": "unauthorized", "message": "unauthorized"}
                )
                return
            self._handle_inference()
            return
        if self.path == "/shutdown":
            if not self._authorized():
                self._send_json(
                    401, {"code": "unauthorized", "message": "unauthorized"}
                )
                return
            self._send_json(200, {"status": "stopping"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self._send_json(404, {"code": "not_found", "message": "not found"})

    def _method_not_allowed(self):
        self.close_connection = True
        payload = {"code": "method_not_allowed", "message": "method not allowed"}
        if self.command == "HEAD":
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(405)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            return
        self._send_json(405, payload)

    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_TRACE = _method_not_allowed

    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)
        self.wfile.flush()

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        provided = header[len("Bearer "):]
        return hmac.compare_digest(provided, self.server._local_token)

    def _log_record(self, model, transport, status, request_id, started):
        server = self.server
        if not server._log_path:
            return
        record = {
            "model": model,
            "transport": transport,
            "status": status,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "request_id": request_id,
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with server._log_lock:
            with open(server._log_path, "a", encoding="utf-8") as log_file:
                log_file.write(line)

    def _handle_inference(self):
        server = self.server
        started = time.monotonic()
        request_id = secrets.token_hex(16)
        if not server._semaphore.acquire(blocking=False):
            self._send_json(
                429,
                {"code": "too_many_requests", "message": "concurrency limit reached"},
                {"Retry-After": "1"},
            )
            self._log_record("", "", 429, request_id, started)
            return
        model = ""
        transport = ""
        status = 500
        try:
            def reject(code, http_status):
                self._send_json(
                    http_status, {"code": code, "message": "request rejected"}
                )
                self._log_record(model, transport, http_status, request_id, started)
                return False

            if self.headers.get("Transfer-Encoding") is not None:
                return reject("transfer_encoding_not_supported", 400)
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                return reject("length_required", 411)
            try:
                content_length = int(raw_length)
            except ValueError:
                return reject("invalid_content_length", 400)
            if content_length < 0:
                return reject("negative_content_length", 411)
            if content_length > server._max_request_bytes:
                return reject("request_too_large", 413)
            media = (self.headers.get("Content-Type") or "").strip()
            if not (media == "application/json" or media.startswith("application/json;")):
                return reject("unsupported_media_type", 415)
            raw = self.rfile.read(content_length)
            if len(raw) != content_length:
                return reject("invalid_body_length", 400)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return reject("invalid_utf8", 400)
            try:
                request = json.loads(text)
            except json.JSONDecodeError:
                return reject("invalid_json", 400)
            if not isinstance(request, dict):
                return reject("invalid_request", 400)
            model = request.get("model")
            if not isinstance(model, str) or not model.strip():
                return reject("invalid_model", 400)
            effort = None
            reasoning = request.get("reasoning")
            if reasoning is not None:
                if not isinstance(reasoning, dict):
                    return reject("invalid_reasoning", 400)
                effort = reasoning.get("effort")
                if effort is not None and not isinstance(effort, str):
                    return reject("invalid_effort", 400)
            spec = _MODELS.get_model(model, effort, registry=server._model_registry)
            if effort is None:
                effort = spec.default_effort
            upstream = _GATEWAY.prepare_upstream_request(
                request,
                model,
                effort,
                server._api_key,
                server._reasoning_store,
                model_registry_snapshot=server._model_registry_snapshot,
            )
            transport = upstream.transport
            self._call_upstream(server, upstream, request_id)
            status = self._outcome_status
        except (_GATEWAY.GatewayError, _MODELS.ModelError) as exc:
            status = getattr(exc, "status", 400)
            if not (400 <= status < 600):
                status = 400
            self._send_json(
                status,
                {
                    "code": getattr(exc, "code", "invalid_request"),
                    "message": "request rejected",
                },
            )
        except Exception:
            status = 500
            self._send_json(
                500, {"code": "internal_error", "message": "internal error"}
            )
        finally:
            server._semaphore.release()
        self._log_record(model, transport, status, request_id, started)

    def _call_upstream(self, server, upstream, request_id):
        self._headers_sent = False
        connection_class = (
            http.client.HTTPSConnection
            if server._upstream_scheme == "https"
            else http.client.HTTPConnection
        )
        conn = connection_class(
            server._upstream_host,
            server._upstream_port,
            timeout=server._connect_timeout,
        )
        try:
            body = json.dumps(upstream.body, separators=(",", ":")).encode("utf-8")
            headers = dict(upstream.headers)
            headers.setdefault("Content-Type", "application/json")
            headers["Content-Length"] = str(len(body))
            headers["Connection"] = "close"
            path = server._upstream_base_path + upstream.path
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            if resp.status == 429:
                retry = _sanitize_retry_after(resp.getheader("Retry-After"))
                raw_id = resp.getheader("x-request-id") or resp.getheader("request-id")
                safe_id = _sanitize_request_id(raw_id)
                resp.read()
                extra = {"Retry-After": retry} if retry is not None else None
                self._send_json(
                    429,
                    {
                        "code": "upstream_rate_limited",
                        "message": "upstream rate limited",
                        "request_id": safe_id,
                    },
                    extra,
                )
                self._outcome_status = 429
                return
            if resp.status != 200:
                resp.read()
                status = resp.status if 400 <= resp.status < 600 else 502
                self._send_json(
                    status,
                    {"code": "upstream_error", "message": "upstream request failed"},
                )
                self._outcome_status = status
                return
            fp = getattr(resp, "fp", None)
            raw = getattr(fp, "raw", None)
            stream_sock = getattr(raw, "_sock", None)
            if stream_sock is None:
                stream_sock = raw
            if stream_sock is not None:
                try:
                    stream_sock.settimeout(server._stream_idle_timeout)
                except (AttributeError, OSError):
                    pass
            self._stream_local(conn, resp, upstream)
            self._outcome_status = 200
        except socket.timeout:
            if not self._headers_sent:
                self._send_json(
                    502,
                    {"code": CONNECT_ERROR_CODE, "message": "upstream unavailable"},
                )
            self._outcome_status = 502
        except (OSError, http.client.HTTPException):
            if not self._headers_sent:
                self._send_json(
                    502,
                    {"code": CONNECT_ERROR_CODE, "message": "upstream unavailable"},
                )
            self._outcome_status = 502
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _stream_local(self, conn, resp, upstream):
        server = self.server
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self._headers_sent = True
        try:
            chunks = _iter_upstream_chunks(resp)
            translated = _GATEWAY.translate_upstream_stream(
                chunks,
                transport=upstream.transport,
                custom_tool_names=upstream.custom_tool_names,
                reasoning_store=server._reasoning_store,
            )
            for event in translated:
                raw = _GATEWAY.encode_sse(event)
                self.wfile.write(raw)
                self.wfile.flush()
        except socket.timeout:
            self._emit_failed(FAILURE_CODE_IDLE)
        except _GATEWAY.GatewayError as exc:
            if exc.code == "stream_truncated":
                self._emit_failed(FAILURE_CODE_TRUNCATED)
            else:
                self._emit_failed(FAILURE_CODE_STREAM)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            try:
                self._emit_failed(FAILURE_CODE_STREAM)
            except Exception:
                pass

    def _emit_failed(self, code):
        payload = {
            "type": "response.failed",
            "response": {
                "id": "",
                "error": {"code": code, "message": "upstream stream failed"},
            },
        }
        event = _GATEWAY.SSEEvent(
            "response.failed", json.dumps(payload, separators=(",", ":"))
        )
        self.wfile.write(_GATEWAY.encode_sse(event))
        self.wfile.flush()


def create_server(
    host,
    port,
    local_token,
    api_key,
    upstream_base=PRODUCTION_BASE,
    log_path=None,
    max_concurrent=DEFAULT_MAX_CONCURRENT,
    max_request_bytes=DEFAULT_MAX_REQUEST_BYTES,
    connect_timeout=DEFAULT_CONNECT_TIMEOUT,
    stream_idle_timeout=DEFAULT_STREAM_IDLE_TIMEOUT,
    allow_test_http=False,
    model_registry_snapshot=None,
):
    """Create (and bind) the local gateway; returns a GatewayServer instance."""
    if host != _LOOPBACK_HOST:
        raise ValueError("host must be 127.0.0.1")
    if not isinstance(port, int) or port < 0 or port > 65535:
        raise ValueError("port must be an integer in 0..65535")
    if not isinstance(local_token, str) or not local_token:
        raise ValueError("local_token must be a non-empty string")
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("api_key must be a non-empty string")
    if log_path is not None and not isinstance(log_path, str):
        raise ValueError("log_path must be a string or None")
    if not isinstance(max_concurrent, int) or max_concurrent < 1:
        raise ValueError("max_concurrent must be a positive integer")
    if not isinstance(max_request_bytes, int) or max_request_bytes < 1:
        raise ValueError("max_request_bytes must be a positive integer")
    if not isinstance(connect_timeout, (int, float)) or connect_timeout <= 0:
        raise ValueError("connect_timeout must be positive")
    if not isinstance(stream_idle_timeout, (int, float)) or stream_idle_timeout <= 0:
        raise ValueError("stream_idle_timeout must be positive")
    if not isinstance(allow_test_http, bool):
        raise ValueError("allow_test_http must be a bool")
    if model_registry_snapshot is None:
        try:
            canonical_snapshot = _MODELS.registry_snapshot()
            parsed_registry = _MODELS.registry_from_snapshot(canonical_snapshot)
        except _MODELS.ModelError:
            raise ValueError("invalid model registry snapshot") from None
    else:
        try:
            parsed_registry = _MODELS.registry_from_snapshot(model_registry_snapshot)
            canonical_snapshot = _MODELS.registry_snapshot(parsed_registry)
        except _MODELS.ModelError:
            raise ValueError("invalid model registry snapshot") from None
    scheme, upstream_host, upstream_port, base_path = _parse_upstream_base(
        upstream_base, allow_test_http
    )
    return GatewayServer(
        (_LOOPBACK_HOST, port),
        _GatewayHandler,
        local_token=local_token,
        api_key=api_key,
        upstream_scheme=scheme,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        upstream_base_path=base_path,
        log_path=log_path,
        max_concurrent=max_concurrent,
        max_request_bytes=max_request_bytes,
        connect_timeout=connect_timeout,
        stream_idle_timeout=stream_idle_timeout,
        model_registry=parsed_registry,
        model_registry_snapshot=canonical_snapshot,
    )
