"""RED behavior contract tests for the fixed HTTPS refresh downloader
(DS-20260811-20 REV-2).

The tests prove the downloader API decided in DEC-20260811-20-V1 is not
implemented yet.  They load codex_opencode_go.py dynamically, use only the
standard library, open no network/files/credentials/subprocess, and fail with
a fixed assertion message when the target callable is missing so the current
production module yields FAIL (not ERROR).
"""

from __future__ import annotations

import http.client
import json
import unittest
from unittest import mock

from tests.test_manager import _load_manager

GO_HOST = "opencode.ai"
GO_PATH = "/zen/go/v1/models"
GO_LIMIT = 1048576
MODELS_DEV_HOST = "models.dev"
MODELS_DEV_PATH = "/api.json"
MODELS_DEV_LIMIT = 8388608
TIMEOUT_SECONDS = 30
USER_AGENT = "codex-opencode-go-subagent/1"
SECRET_MARKER = "red-secret-placeholder-20260811"


def _exact_json_object(size):
    overhead = len(b'{"pad":""}')
    if size < overhead:
        raise AssertionError("size too small for an exact JSON object")
    padding = b"a" * (size - overhead)
    return b'{"pad":"' + padding + b'"}'


class _FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self._body = body
        self._headers = dict(headers or {})
        self._read_calls = []
        self._closed = False

    def getheader(self, name, default=None):
        lowered = name.lower()
        for key, value in self._headers.items():
            if key.lower() == lowered:
                return value
        return default

    def read(self, amount=None):
        self._read_calls.append(amount)
        if amount is None:
            return self._body
        return self._body[:amount]

    def close(self):
        self._closed = True


class _FakeConnection:
    def __init__(self, host, timeout, request_error=None):
        self.host = host
        self.timeout = timeout
        self.request_calls = []
        self.closed = False
        self.response = None
        self._request_error = request_error

    def request(self, method, url, body=None, headers=None):
        self.request_calls.append((method, url, body, headers))
        if self._request_error is not None:
            raise self._request_error
        return None

    def getresponse(self):
        if self.response is None:
            raise AssertionError("no fake response installed for host")
        return self.response

    def close(self):
        self.closed = True


class _FakeFactory:
    def __init__(self, responses=None, request_errors=None):
        self.calls = []
        self.connections = []
        self._responses = dict(responses or {})
        self._request_errors = dict(request_errors or {})

    def __call__(self, host, timeout):
        self.calls.append((host, timeout))
        connection = _FakeConnection(
            host, timeout, self._request_errors.get(host))
        connection.response = self._responses.get(host)
        self.connections.append(connection)
        return connection


class RefreshDownloaderRedTests(unittest.TestCase):
    def _require_callable(self, manager, name):
        if not callable(getattr(manager, name, None)):
            self.fail(
                "RED: manager.%s is missing (DS-20260811-20 REV-2)" % name)

    def _assert_request(self, connection, expected_path, expected_limit):
        self.assertTrue(
            len(connection.request_calls) == 1,
            "RED: request must be called exactly once per source")
        method, url, body, headers = connection.request_calls[0]
        self.assertTrue(method == "GET", "RED: request method must be GET")
        self.assertTrue(url == expected_path, "RED: request path mismatch")
        self.assertTrue(body is None, "RED: request body must be None")
        self.assertTrue(isinstance(headers, dict),
                        "RED: request headers must be a dict")
        self.assertTrue(headers.get("User-Agent") == USER_AGENT,
                        "RED: User-Agent mismatch")
        self.assertTrue(headers.get("Accept") == "application/json",
                        "RED: Accept mismatch")
        self.assertTrue(headers.get("Connection") == "close",
                        "RED: Connection header mismatch")
        self.assertTrue(len(headers) == 3,
                        "RED: exactly three headers are required")
        lowered = {key.lower() for key in headers}
        self.assertTrue(
            "authorization" not in lowered and "x-api-key" not in lowered,
            "RED: credential headers are forbidden")
        self.assertTrue(
            connection.response is not None
            and connection.response._read_calls == [expected_limit + 1],
            "RED: body read amount must be limit+1")

    def test_success_contract_order_headers_limits_and_no_credentials(self):
        manager = _load_manager()
        self._require_callable(manager, "fetch_refresh_payloads")
        go_body = _exact_json_object(GO_LIMIT)
        models_dev_body = _exact_json_object(MODELS_DEV_LIMIT)
        responses = {
            GO_HOST: _FakeResponse(status=200, body=go_body),
            MODELS_DEV_HOST: _FakeResponse(status=200, body=models_dev_body),
        }
        factory = _FakeFactory(responses=responses)
        factory_keys = set(vars(factory))
        with mock.patch.object(
                manager, "read_credential_key",
                side_effect=AssertionError(
                    "credential read must not be called")), \
                mock.patch.object(
                    manager, "credential_has_key",
                    side_effect=AssertionError(
                        "credential check must not be called")), \
                mock.patch.object(
                    manager, "store_credential_key",
                    side_effect=AssertionError(
                        "credential store must not be called")):
            go_payload, models_dev_payload = manager.fetch_refresh_payloads(
                factory)
        self.assertTrue(isinstance(go_payload, dict),
                        "RED: go payload must be a dict")
        self.assertTrue(isinstance(models_dev_payload, dict),
                        "RED: models.dev payload must be a dict")
        self.assertTrue(go_payload is not models_dev_payload,
                        "RED: payloads must be independent objects")
        self.assertTrue(
            factory.calls == [
                (GO_HOST, TIMEOUT_SECONDS),
                (MODELS_DEV_HOST, TIMEOUT_SECONDS),
            ],
            "RED: factory host/timeout order mismatch")
        self.assertTrue(len(factory.connections) == 2,
                        "RED: exactly two connections required")
        self._assert_request(
            factory.connections[0], GO_PATH, GO_LIMIT)
        self._assert_request(
            factory.connections[1], MODELS_DEV_PATH, MODELS_DEV_LIMIT)
        self.assertTrue(factory.connections[0].closed,
                        "RED: first connection must close")
        self.assertTrue(factory.connections[1].closed,
                        "RED: second connection must close")
        self.assertTrue(
            responses[GO_HOST]._body == go_body
            and responses[MODELS_DEV_HOST]._body == models_dev_body,
            "RED: input body bytes must not be mutated")
        self.assertTrue(set(vars(factory)) == factory_keys,
                        "RED: factory object must not be mutated")

    def test_exact_limit_succeeds_and_actual_limit_plus_one_rejected(self):
        manager = _load_manager()
        self._require_callable(manager, "_fetch_refresh_json")
        exact_body = _exact_json_object(GO_LIMIT)
        exact_response = _FakeResponse(status=200, body=exact_body)
        exact_factory = _FakeFactory(responses={GO_HOST: exact_response})
        payload = manager._fetch_refresh_json("opencode_go", exact_factory)
        self.assertTrue(isinstance(payload, dict),
                        "RED: exact-limit payload must parse as a dict")
        self.assertTrue(
            exact_factory.calls == [(GO_HOST, TIMEOUT_SECONDS)],
            "RED: exact-limit factory host/timeout mismatch")
        self.assertTrue(
            exact_response._read_calls == [GO_LIMIT + 1],
            "RED: exact-limit read amount must be limit+1")
        self.assertTrue(exact_factory.connections[0].closed,
                        "RED: exact-limit connection must close")

        too_large_body = _exact_json_object(GO_LIMIT + 1)
        too_large_response = _FakeResponse(status=200, body=too_large_body)
        too_large_factory = _FakeFactory(
            responses={GO_HOST: too_large_response})
        with self.assertRaises(manager.ManagerError) as raised:
            manager._fetch_refresh_json("opencode_go", too_large_factory)
        self.assertTrue(
            raised.exception.code == "refresh_response_too_large",
            "RED: over-limit body must map to refresh_response_too_large")
        self.assertTrue(
            too_large_response._read_calls == [GO_LIMIT + 1],
            "RED: over-limit body must be read once at limit+1")
        self.assertTrue(too_large_factory.connections[0].closed,
                        "RED: over-limit connection must close")

    def test_content_length_oversize_and_invalid_rejected_without_body_read(self):
        manager = _load_manager()
        self._require_callable(manager, "_fetch_refresh_json")
        cases = (
            ("oversize", "1048577", "refresh_response_too_large"),
            ("non-decimal", "not-a-number", "refresh_payload_invalid"),
            ("negative", "-1", "refresh_payload_invalid"),
        )
        for label, header_value, expected_code in cases:
            with self.subTest(case=label):
                response = _FakeResponse(
                    status=200,
                    body=b'{"ok": true}',
                    headers={"Content-Length": header_value},
                )
                factory = _FakeFactory(responses={GO_HOST: response})
                with self.assertRaises(manager.ManagerError) as raised:
                    manager._fetch_refresh_json("opencode_go", factory)
                self.assertTrue(
                    raised.exception.code == expected_code,
                    "RED: Content-Length case must map to %s" % expected_code)
                self.assertTrue(
                    response._read_calls == [],
                    "RED: body must not be read for invalid/oversize Content-Length")
                self.assertTrue(factory.connections[0].closed,
                                "RED: connection must close")
                message = str(raised.exception)
                details = json.dumps(
                    raised.exception.details, sort_keys=True)
                self.assertTrue(
                    header_value not in message,
                    "RED: header value must not be echoed in error text")
                self.assertTrue(
                    header_value not in details,
                    "RED: header value must not be echoed in error details")

    def test_network_status_and_protocol_failures_close_and_redact(self):
        manager = _load_manager()
        self._require_callable(manager, "_fetch_refresh_json")

        status_response = _FakeResponse(
            status=429, body=SECRET_MARKER.encode("ascii"))
        status_factory = _FakeFactory(responses={GO_HOST: status_response})
        with self.assertRaises(manager.ManagerError) as raised:
            manager._fetch_refresh_json("opencode_go", status_factory)
        self.assertTrue(
            raised.exception.code == "refresh_network_failed",
            "RED: non-200 status must map to refresh_network_failed")
        self.assertTrue(
            raised.exception.details == {
                "source": "opencode_go", "status": 429},
            "RED: non-200 details must contain only source and status")
        self.assertTrue(
            status_response._read_calls == [],
            "RED: non-200 body must not be read")
        self.assertTrue(status_factory.connections[0].closed,
                        "RED: non-200 connection must close")

        for label, error in (
                ("oserror", OSError(SECRET_MARKER)),
                ("http_exception", http.client.HTTPException(SECRET_MARKER))):
            with self.subTest(case=label):
                factory = _FakeFactory(request_errors={GO_HOST: error})
                with self.assertRaises(manager.ManagerError) as raised:
                    manager._fetch_refresh_json("opencode_go", factory)
                self.assertTrue(
                    raised.exception.code == "refresh_network_failed",
                    "RED: protocol failure must map to refresh_network_failed")
                self.assertTrue(
                    factory.calls == [(GO_HOST, TIMEOUT_SECONDS)],
                    "RED: protocol failure must not retry")
                self.assertTrue(len(factory.connections) == 1,
                                "RED: exactly one connection per source")
                self.assertTrue(factory.connections[0].closed,
                                "RED: failed connection must close")
                message = str(raised.exception)
                details = json.dumps(
                    raised.exception.details, sort_keys=True)
                for forbidden in (SECRET_MARKER, GO_HOST, GO_PATH):
                    self.assertTrue(
                        forbidden not in message,
                        "RED: error text must not leak network details")
                    self.assertTrue(
                        forbidden not in details,
                        "RED: error details must not leak network details")

    def test_invalid_utf8_json_and_top_level_rejected_and_redacted(self):
        manager = _load_manager()
        self._require_callable(manager, "_fetch_refresh_json")
        cases = (
            ("invalid_utf8",
             b"\xff\xfe\x00\x7b\x22a\x22\x3a\x31\x7d",
             None),
            ("invalid_json",
             b'{"broken": ' + SECRET_MARKER.encode("ascii") + b'}',
            SECRET_MARKER),
            ("top_level_list", b"[1, 2, 3]", None),
        )
        for label, body, secret in cases:
            with self.subTest(case=label):
                response = _FakeResponse(status=200, body=body)
                factory = _FakeFactory(responses={GO_HOST: response})
                with self.assertRaises(manager.ManagerError) as raised:
                    manager._fetch_refresh_json("opencode_go", factory)
                self.assertTrue(
                    raised.exception.code == "refresh_payload_invalid",
                    "RED: invalid payload must map to refresh_payload_invalid")
                self.assertTrue(factory.connections[0].closed,
                                "RED: invalid payload connection must close")
                message = str(raised.exception)
                details = json.dumps(
                    raised.exception.details, sort_keys=True)
                if secret is not None:
                    self.assertTrue(
                        secret not in message,
                        "RED: error text must not echo payload secret")
                    self.assertTrue(
                        secret not in details,
                        "RED: error details must not echo payload secret")

    def test_first_source_failure_has_no_retry_or_second_source(self):
        manager = _load_manager()
        self._require_callable(manager, "fetch_refresh_payloads")
        factory = _FakeFactory(
            request_errors={GO_HOST: OSError(SECRET_MARKER)})
        with self.assertRaises(manager.ManagerError) as raised:
            manager.fetch_refresh_payloads(factory)
        self.assertTrue(
            raised.exception.code == "refresh_network_failed",
            "RED: first-source failure must map to refresh_network_failed")
        self.assertTrue(
            factory.calls == [(GO_HOST, TIMEOUT_SECONDS)],
            "RED: second source must not be contacted and no retry allowed")
        self.assertTrue(len(factory.connections) == 1,
                        "RED: exactly one connection must be created")
        self.assertTrue(factory.connections[0].closed,
                        "RED: failed first-source connection must close")
        message = str(raised.exception)
        details = json.dumps(raised.exception.details, sort_keys=True)
        for forbidden in (SECRET_MARKER, GO_HOST, GO_PATH):
            self.assertTrue(
                forbidden not in message,
                "RED: error text must not leak network details")
            self.assertTrue(
                forbidden not in details,
                "RED: error details must not leak network details")
