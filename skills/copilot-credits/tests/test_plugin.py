"""Offline tests for the copilot-credits extension (stdlib unittest only).

Run:            python3 tests/test_plugin.py
Regenerate:     python3 tests/test_plugin.py --write-examples

Everything is fake: fetch functions, monotonic/wall clocks (starting at 0),
get_settings, HTTPSConnection, sockets and the host plugin API. Real network
access is blocked for the whole process by an offline guard.
"""
from __future__ import annotations

import datetime as _dt
import http.client
import json
import os
import socket
import ssl
import sys
import threading
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXAMPLES_DIR = os.path.join(ROOT, "examples")
sys.path.insert(0, ROOT)

try:  # the host ships starlette; a minimal stand-in keeps the tests portable
    import starlette.responses  # noqa: F401
except ImportError:  # pragma: no cover - only without starlette installed
    _stub = types.ModuleType("starlette.responses")

    class JSONResponse:  # type: ignore[no-redef]
        def __init__(self, content, status_code=200, headers=None):
            self.status_code = status_code
            self.body = json.dumps(content).encode("utf-8")
            self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    _stub.JSONResponse = JSONResponse
    _pkg = types.ModuleType("starlette")
    _pkg.responses = _stub
    sys.modules["starlette"] = _pkg
    sys.modules["starlette.responses"] = _stub


# ---------------------------------------------------------------- offline guard

def _no_network(*_a, **_k):
    raise AssertionError("network access attempted in an offline test")


socket.create_connection = _no_network
socket.getaddrinfo = _no_network
socket.socket.connect = _no_network  # type: ignore[assignment]
socket.socket.connect_ex = _no_network  # type: ignore[assignment]

import plugin as P  # noqa: E402

# ---------------------------------------------------------------- fixtures

BASE = "https://api.business.githubcopilot.com"
TOKEN_A = "ghu_TESTSECRET_AAAAAAAAAAAAAAAAAAAAAAAA"
TOKEN_B = "ghu_TESTSECRET_BBBBBBBBBBBBBBBBBBBBBBBB"
EXC_TEXT = "EXCEPTION_SECRET_TEXT_should_never_leak"
SECRETS = (TOKEN_A, TOKEN_B, "TESTSECRET", EXC_TEXT)

FIXTURE = {
    "login": "sample-user", "copilot_plan": "business",
    "quota_reset_date_utc": "2030-02-01T00:00:00.000Z", "token_based_billing": True,
    "quota_snapshots": {
        "chat": {"unlimited": True, "percent_remaining": 100.0, "entitlement": 0},
        "premium_interactions": {
            "credits_used": 3100, "percent_remaining": 24.2, "quota_remaining": 2500.5,
            "entitlement": 10000, "unlimited": False, "overage_permitted": True,
            "token_based_billing": True,
        },
    },
}
WALL_BASE = _dt.datetime(2030, 1, 2, 3, 0, 0, tzinfo=_dt.timezone.utc).timestamp()


def body(data=None, **premium_overrides):
    data = json.loads(json.dumps(FIXTURE if data is None else data))
    prem = data.get("quota_snapshots", {}).get("premium_interactions")
    for key, value in premium_overrides.items():
        if value is _DELETE:
            prem.pop(key, None)
        else:
            prem[key] = value
    return json.dumps(data).encode("utf-8")


_DELETE = object()
OK = (200, None, body())


def iso(ts):
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    def __init__(self, start=0.0):
        self.t = float(start)

    def __call__(self):
        return self.t


class Settings:
    """Mimics host get_settings: only requested, currently granted keys."""

    def __init__(self, **values):
        self.values = dict(values)
        self.requests = []
        self.lock = threading.Lock()

    def __call__(self, keys):
        with self.lock:
            self.requests.append(list(keys))
            return {k: self.values[k] for k in keys if k in self.values}

    @property
    def count(self):
        with self.lock:
            return len(self.requests)


def granted(token=TOKEN_A, base=BASE):
    return Settings(OPENAI_COMPATIBLE_API_KEY=token, OPENAI_COMPATIBLE_BASE_URL=base)


class FakeFetch:
    """Scripted fetch. Each step: (status, retry_after, body) tuple or an exception."""

    def __init__(self, *steps, hook=None, gate=None):
        self.steps = list(steps)
        self.tokens = []
        self.hook = hook
        self.gate = gate
        self.unexpected = 0
        self.lock = threading.Lock()

    def __call__(self, token):
        with self.lock:
            self.tokens.append(token)
            if not self.steps:
                self.unexpected += 1
                raise RuntimeError("unexpected fetch " + EXC_TEXT)
            step = self.steps.pop(0)
        if self.gate is not None:
            assert self.gate.wait(10), "gate never opened"
        if self.hook is not None:
            self.hook()
        if isinstance(step, BaseException):
            raise step
        return step

    @property
    def count(self):
        with self.lock:
            return len(self.tokens)


# ---------------------------------------------------------------- fake transport

class FakeSock:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class FakeResponse:
    def __init__(self, status, headers=None, chunks=(), read_exc=None):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.chunks = [bytes(c) for c in chunks]
        self.read_exc = read_exc
        self.reads = 0

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read(self, n):
        self.reads += 1
        if self.read_exc is not None:
            raise self.read_exc
        if not self.chunks:
            return b""
        head = self.chunks[0]
        out, rest = head[:n], head[n:]
        if rest:
            self.chunks[0] = rest
        else:
            self.chunks.pop(0)
        return out


class ConnFactory:
    """Stands in for http.client.HTTPSConnection; records every connection."""

    def __init__(self, response=None, *, connect_exc=None, request_exc=None, getresponse_exc=None,
                 clock=None, connect_cost=0.0, request_cost=0.0, getresponse_cost=0.0):
        self.response = response
        self.connect_exc, self.request_exc, self.getresponse_exc = connect_exc, request_exc, getresponse_exc
        self.clock = clock
        self.costs = (connect_cost, request_cost, getresponse_cost)
        self.conns = []

    def __call__(self, host, port, timeout=None, context=None):
        conn = FakeConn(self, host, port, timeout, context)
        self.conns.append(conn)
        return conn


class FakeConn:
    def __init__(self, factory, host, port, timeout, context):
        self.f = factory
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.sock = None
        self.requests = []
        self.connected = 0
        self.closed = False

    def _spend(self, idx):
        if self.f.clock is not None:
            self.f.clock.t += self.f.costs[idx]

    def connect(self):
        self.connected += 1
        self._spend(0)
        if self.f.connect_exc is not None:
            raise self.f.connect_exc
        self.sock = FakeSock()

    def request(self, method, path, headers=None):
        self.requests.append((method, path, dict(headers or {})))
        self._spend(1)
        if self.f.request_exc is not None:
            raise self.f.request_exc

    def getresponse(self):
        self._spend(2)
        if self.f.getresponse_exc is not None:
            raise self.f.getresponse_exc
        return self.f.response

    def close(self):
        self.closed = True


def transport_fetch(factory, clock=None):
    clock = clock if clock is not None else Clock(0.0)
    return lambda token: P.https_get(token, connection_factory=factory, monotonic=clock)


# ---------------------------------------------------------------- base case

class Base(unittest.TestCase):
    def setUp(self):
        self.mono = Clock(0.0)
        self.wall = Clock(0.0)
        self.logged = []

    def service(self, settings, fetch):
        self.settings, self.fetch = settings, fetch
        return P.CreditsService(lambda: P.read_credential(settings), fetch,
                                wall=self.wall, monotonic=self.mono, log=self.logged.append)

    def tick(self, seconds):
        self.mono.t += seconds
        self.wall.t += seconds

    def get(self, svc):
        payload = svc.get_usage()
        check_payload(self, payload)
        return payload

    def assertUnavailable(self, payload, code, http_status=None):
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["error"]["code"], code)
        self.assertEqual(payload["error"]["http_status"], http_status)
        for key in ("observed_at", "account", "premium", "reset_at", "nominal_usd"):
            self.assertIsNone(payload[key], key)

    def tearDown(self):
        fetch = getattr(self, "fetch", None)
        if isinstance(fetch, FakeFetch):
            self.assertEqual(fetch.unexpected, 0, "unexpected upstream fetch")
        for line in self.logged:
            self.assertIn(line, P.ERROR_MESSAGES)  # log carries the code only


def check_payload(tc, payload):
    """Contract invariants (API.md) + no secret/exception text in the JSON."""
    text = json.dumps(payload)
    for secret in SECRETS:
        tc.assertNotIn(secret, text)
    tc.assertEqual(set(payload), {"schema_version", "status", "source", "observed_at", "last_attempt_at",
                                  "next_refresh_after", "error", "account", "premium", "reset_at",
                                  "nominal_usd"})
    tc.assertEqual(payload["schema_version"], 1)
    tc.assertEqual(payload["source"], "github.copilot_internal.user")
    status = payload["status"]
    tc.assertIn(status, ("fresh", "stale", "unavailable"))
    err = payload["error"]
    if err is not None:
        tc.assertEqual(set(err), {"code", "message", "http_status", "retry_after_s"})
        tc.assertEqual(err["message"], P.ERROR_MESSAGES[err["code"]])
    if status == "unavailable":
        tc.assertIsNotNone(err)
        for key in ("observed_at", "account", "premium", "reset_at", "nominal_usd"):
            tc.assertIsNone(payload[key])
    else:
        tc.assertIsNotNone(payload["observed_at"])
        tc.assertIsNotNone(payload["account"])
        prem = payload["premium"]
        tc.assertIsNotNone(prem)
        if prem["unlimited"] is True:
            tc.assertIsNone(prem["used_percentage"])
            tc.assertIsNone(prem["percent_remaining"])
        else:
            tc.assertIsInstance(prem["used_percentage"], (int, float))
            tc.assertNotIsInstance(prem["used_percentage"], bool)
            tc.assertTrue(0 <= prem["used_percentage"] <= 100)
        if payload["nominal_usd"] is not None:
            tc.assertIs(prem["token_based_billing"], True)
        if status == "stale":
            tc.assertIsNotNone(err)
        else:
            tc.assertIsNone(err)


# ---------------------------------------------------------------- parsing

class ParseTests(Base):
    def test_fresh_fixture_parse(self):
        svc = self.service(granted(), FakeFetch(OK))
        p = self.get(svc)
        self.assertEqual(p["status"], "fresh")
        self.assertIsNone(p["error"])
        self.assertEqual(p["account"], {"login": "sample-user", "plan": "business"})
        self.assertEqual(p["premium"], {
            "used_percentage": 75.8, "percent_remaining": 24.2, "entitlement": 10000,
            "quota_remaining": 2500.5, "credits_used": 3100, "unlimited": False,
            "overage_permitted": True, "token_based_billing": True})
        self.assertEqual(p["reset_at"], "2030-02-01T00:00:00Z")
        self.assertEqual(p["nominal_usd"], {"basis": "1 credit = $0.01 consumption value; not an invoice",
                                            "entitlement": 100.0, "quota_remaining": 25.005})
        self.assertEqual(p["observed_at"], iso(0))
        self.assertEqual(p["last_attempt_at"], iso(0))
        self.assertEqual(p["next_refresh_after"], iso(60))
        self.assertEqual(self.fetch.tokens, [TOKEN_A])

    def test_unlimited_has_null_percentages(self):
        svc = self.service(granted(), FakeFetch((200, None, body(unlimited=True, percent_remaining=100.0))))
        p = self.get(svc)
        self.assertEqual(p["status"], "fresh")
        self.assertTrue(p["premium"]["unlimited"])
        self.assertIsNone(p["premium"]["used_percentage"])
        self.assertIsNone(p["premium"]["percent_remaining"])

    def test_unrecognized_responses(self):
        no_bucket = json.loads(json.dumps(FIXTURE))
        del no_bucket["quota_snapshots"]["premium_interactions"]
        no_snapshots = {k: v for k, v in FIXTURE.items() if k != "quota_snapshots"}
        cases = {
            "missing bucket": json.dumps(no_bucket).encode(),
            "missing snapshots": json.dumps(no_snapshots).encode(),
            "top-level list": b"[1, 2]",
            "unlimited missing": body(unlimited=_DELETE),
            "unlimited str": body(unlimited="false"),
            "percent bool": body(percent_remaining=True),
            "percent str": body(percent_remaining="24.2"),
            "percent NaN": body(percent_remaining=float("nan")),
            "percent Infinity": body(percent_remaining=float("inf")),
            "percent missing": body(percent_remaining=_DELETE),
            "percent > 100": body(percent_remaining=100.5),
            "percent negative": body(percent_remaining=-0.1),
            "entitlement 0": body(entitlement=0),
        }
        for name, raw in cases.items():
            with self.subTest(name):
                svc = self.service(granted(), FakeFetch((200, None, raw)))
                p = self.get(svc)
                self.assertUnavailable(p, "unrecognized_response", 200)

    def test_bad_json(self):
        for raw in (b"{not json", b"\xff\xfe\x00", b""):
            with self.subTest(raw=raw):
                svc = self.service(granted(), FakeFetch((200, None, raw)))
                self.assertUnavailable(self.get(svc), "bad_json", 200)

    def test_sanitized_projection(self):
        data = json.loads(json.dumps(FIXTURE))
        data["login"] = "<script>" + TOKEN_A
        data["copilot_plan"] = "biz plan with spaces"
        data["access_token"] = TOKEN_A
        data["quota_snapshots"]["premium_interactions"]["secret"] = EXC_TEXT
        data["quota_reset_date_utc"] = "not a date " + EXC_TEXT
        svc = self.service(granted(), FakeFetch((200, None, json.dumps(data).encode())))
        p = self.get(svc)
        self.assertEqual(p["account"], {"login": None, "plan": None})
        self.assertIsNone(p["reset_at"])
        self.assertNotIn("secret", p["premium"])

    def test_no_nominal_without_token_billing(self):
        data = json.loads(json.dumps(FIXTURE))
        data["token_based_billing"] = False
        data["quota_snapshots"]["premium_interactions"]["token_based_billing"] = False
        svc = self.service(granted(), FakeFetch((200, None, json.dumps(data).encode())))
        p = self.get(svc)
        self.assertIsNone(p["nominal_usd"])
        self.assertEqual(p["premium"]["used_percentage"], 75.8)


# ---------------------------------------------------------------- cache / failure

class CacheTests(Base):
    def test_ttl_no_refetch_then_refetch(self):
        svc = self.service(granted(), FakeFetch(OK, OK))
        first = self.get(svc)
        self.tick(30)
        cached = self.get(svc)
        self.assertEqual(self.fetch.count, 1)
        self.assertEqual(cached["status"], "fresh")
        self.assertEqual(cached["observed_at"], first["observed_at"])
        self.tick(29.9)
        self.get(svc)
        self.assertEqual(self.fetch.count, 1)
        self.tick(0.2)  # 60.1 s after the success
        again = self.get(svc)
        self.assertEqual(self.fetch.count, 2)
        self.assertEqual(again["status"], "fresh")
        self.assertEqual(again["observed_at"], iso(60.1))

    def test_failure_after_success_is_stale_with_backoff(self):
        svc = self.service(granted(), FakeFetch(OK, P.UpstreamError("timeout"), OK))
        good = self.get(svc)
        self.tick(61)
        p = self.get(svc)
        self.assertEqual(p["status"], "stale")
        self.assertEqual(p["error"]["code"], "timeout")
        self.assertEqual(p["account"], good["account"])
        self.assertEqual(p["premium"], good["premium"])
        self.assertEqual(p["observed_at"], iso(0))
        self.assertEqual(p["last_attempt_at"], iso(61))
        self.assertEqual(p["next_refresh_after"], iso(121))
        self.tick(59)  # inside the 60 s failure backoff
        self.assertEqual(self.get(svc)["status"], "stale")
        self.assertEqual(self.fetch.count, 2)
        self.tick(2)
        self.assertEqual(self.get(svc)["status"], "fresh")
        self.assertEqual(self.fetch.count, 3)
        self.assertEqual(self.logged, ["timeout"])

    def test_snapshot_age_at_monotonic_zero(self):
        # Regression: a snapshot taken at monotonic 0 must age (0 is falsy).
        svc = self.service(granted(), FakeFetch(OK))
        self.assertEqual(self.get(svc)["status"], "fresh")
        self.tick(100)
        with svc._lock:
            p = svc._render_locked()
        check_payload(self, p)
        self.assertEqual(p["status"], "stale")
        self.assertEqual(p["error"]["code"], "pending")

    def test_stale_capped_at_24h(self):
        svc = self.service(granted(), FakeFetch(OK, P.UpstreamError("network"), P.UpstreamError("network")))
        self.get(svc)
        self.tick(86000)
        self.assertEqual(self.get(svc)["status"], "stale")
        self.tick(401)  # snapshot is now older than 24 h
        p = self.get(svc)
        self.assertUnavailable(p, "network")
        self.assertEqual(self.fetch.count, 3)

    def test_401_403_drop_snapshot(self):
        for status, code in ((401, "unauthorized"), (403, "forbidden")):
            with self.subTest(status=status):
                self.setUp()
                svc = self.service(granted(), FakeFetch(OK, (status, None, b""), OK))
                self.get(svc)
                self.tick(61)
                p = self.get(svc)
                self.assertUnavailable(p, code, status)
                self.assertEqual(p["next_refresh_after"], iso(61 + 300))
                self.tick(299)
                self.assertUnavailable(self.get(svc), code, status)
                self.assertEqual(self.fetch.count, 2)
                self.tick(2)
                self.assertEqual(self.get(svc)["status"], "fresh")

    def test_403_with_retry_after_uses_clamped_value(self):
        svc = self.service(granted(), FakeFetch((403, "120", b"")))
        p = self.get(svc)
        self.assertUnavailable(p, "forbidden", 403)
        self.assertEqual(p["next_refresh_after"], iso(120))

    def test_404(self):
        svc = self.service(granted(), FakeFetch((404, None, b"")))
        self.assertUnavailable(self.get(svc), "not_found", 404)
        svc = self.service(granted(), FakeFetch(OK, (404, None, b"")))
        self.get(svc)
        self.tick(61)
        p = self.get(svc)
        self.assertEqual(p["status"], "stale")  # 404 keeps the same credential's snapshot
        self.assertEqual(p["error"]["code"], "not_found")

    def test_429_retry_after_clamp(self):
        for raw, backoff, reported in (("5", 60, 5), ("5000", 900, 5000), (None, 300, None),
                                       ("120", 120, 120), ("Wed, 21 Oct 2026 07:28:00 GMT", 300, None)):
            with self.subTest(retry_after=raw):
                self.setUp()
                svc = self.service(granted(), FakeFetch((429, raw, b""), OK))
                p = self.get(svc)
                self.assertUnavailable(p, "rate_limited", 429)
                self.assertEqual(p["error"]["retry_after_s"], reported)
                self.assertEqual(p["next_refresh_after"], iso(backoff))
                self.tick(backoff - 1)
                self.get(svc)
                self.assertEqual(self.fetch.count, 1)
                self.tick(1.5)
                self.assertEqual(self.get(svc)["status"], "fresh")
                self.assertEqual(self.fetch.count, 2)

    def test_redirect_refused_one_request(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.setUp()
                factory = ConnFactory(FakeResponse(status, {"Location": "https://evil.example/x"}))
                svc = self.service(granted(), transport_fetch(factory))
                p = self.get(svc)
                self.assertUnavailable(p, "redirect_refused", status)
                self.assertEqual(len(factory.conns), 1)
                self.assertEqual(len(factory.conns[0].requests), 1)

    def test_http_error(self):
        svc = self.service(granted(), FakeFetch((500, None, b"")))
        self.assertUnavailable(self.get(svc), "http_error", 500)

    def test_transport_failures_via_service(self):
        big = b"x" * (P.MAX_BODY_BYTES + 10)
        cases = {
            "timeout": ConnFactory(getresponse_exc=socket.timeout(EXC_TEXT)),
            "network": ConnFactory(connect_exc=ConnectionRefusedError(EXC_TEXT)),
            "too_large": ConnFactory(FakeResponse(200, {"Content-Length": str(len(big))}, [big])),
        }
        for code, factory in cases.items():
            with self.subTest(code=code):
                self.setUp()
                svc = self.service(granted(), transport_fetch(factory))
                p = self.get(svc)
                self.assertEqual(p["error"]["code"], code)
                self.assertEqual(p["status"], "unavailable")

    def test_unclassified_fetch_exception_is_internal(self):
        svc = self.service(granted(), FakeFetch(RuntimeError(EXC_TEXT + TOKEN_A)))
        self.assertUnavailable(self.get(svc), "internal")


# ---------------------------------------------------------------- credentials

class CredentialTests(Base):
    def test_key_rotation_discards_snapshot(self):
        settings = granted()
        svc = self.service(settings, FakeFetch(OK, P.UpstreamError("network")))
        self.assertEqual(self.get(svc)["status"], "fresh")
        settings.values["OPENAI_COMPATIBLE_API_KEY"] = TOKEN_B
        p = self.get(svc)  # still inside the old TTL, but a new credential
        self.assertUnavailable(p, "network")
        self.assertEqual(self.fetch.tokens, [TOKEN_A, TOKEN_B])

    def test_base_url_change_discards_snapshot(self):
        settings = granted()
        svc = self.service(settings, FakeFetch(OK, P.UpstreamError("timeout")))
        self.get(svc)
        settings.values["OPENAI_COMPATIBLE_BASE_URL"] = "https://api.enterprise.githubcopilot.com"
        self.assertUnavailable(self.get(svc), "timeout")
        self.assertEqual(self.fetch.count, 2)

    def test_key_removal_drops_and_does_not_resurrect(self):
        settings = granted()
        svc = self.service(settings, FakeFetch(OK, P.UpstreamError("network")))
        self.get(svc)
        del settings.values["OPENAI_COMPATIBLE_API_KEY"]
        self.assertUnavailable(self.get(svc), "no_credential")
        self.assertEqual(self.fetch.count, 1)
        settings.values["OPENAI_COMPATIBLE_API_KEY"] = TOKEN_A  # same key back
        self.assertUnavailable(self.get(svc), "network")
        self.assertEqual(self.fetch.count, 2)

    def test_wrong_origins_never_fetch(self):
        bad = ["https://api.openai.com", "http://api.business.githubcopilot.com",
               "https://user:pw@api.business.githubcopilot.com", "https://user@api.business.githubcopilot.com",
               "https://api.business.githubcopilot.com:8443", "https://api.business.githubcopilot.com/?x=1",
               "https://api.business.githubcopilot.com#frag", "https://api.business.githubcopilot.com.evil.com",
               "https://evil.com/api.business.githubcopilot.com", "https://githubcopilot.com",
               "api.business.githubcopilot.com", "", "   ", None, 42]
        for base in bad:
            with self.subTest(base=base):
                settings = granted()
                svc = self.service(settings, FakeFetch(OK))
                self.get(svc)
                settings.values["OPENAI_COMPATIBLE_BASE_URL"] = base
                self.assertUnavailable(self.get(svc), "credential_origin_mismatch")
                settings.values["OPENAI_COMPATIBLE_BASE_URL"] = BASE
                fetch = FakeFetch()
                svc2 = self.service(granted(base=base), fetch)
                self.assertUnavailable(self.get(svc2), "credential_origin_mismatch")
                self.assertEqual(fetch.count, 0)

    def test_accepted_origins(self):
        for host in sorted(P.COPILOT_HOSTS):
            for base in ("https://" + host, "https://" + host + "/", "https://" + host + ":443",
                         "HTTPS://" + host.upper(), "https://" + host + "/v1"):
                with self.subTest(base=base):
                    self.assertEqual(P.copilot_origin(base), "https://" + host)

    def test_legacy_openai_api_key_ignored(self):
        settings = Settings(OPENAI_API_KEY=TOKEN_A, OPENAI_COMPATIBLE_BASE_URL=BASE)
        fetch = FakeFetch()
        svc = self.service(settings, fetch)
        self.assertUnavailable(self.get(svc), "no_credential")
        self.assertEqual(fetch.count, 0)
        for keys in settings.requests:
            self.assertEqual(keys, ["OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL"])

    def test_unusable_tokens_never_fetch(self):
        for token in ("ghu_abc\r\nX-Injected: 1", "ghu_abc\nx", "ghu_abc\rx", "ghu abc", "ghu_\x00abc",
                      "ghu_é", "g" * (P.MAX_TOKEN_CHARS + 1)):
            with self.subTest(token=repr(token[:20])):
                fetch = FakeFetch()
                svc = self.service(granted(token=token), fetch)
                p = self.get(svc)
                self.assertUnavailable(p, "credential_unusable")
                self.assertEqual(fetch.count, 0)
                self.assertNotIn(token, json.dumps(p))

    def test_settings_unavailable(self):
        def boom(_keys):
            raise RuntimeError(EXC_TEXT)
        for getter in (boom, lambda _k: None, lambda _k: {}, lambda _k: [TOKEN_A]):
            fetch = FakeFetch()
            svc = P.CreditsService(lambda g=getter: P.read_credential(g), fetch,
                                   wall=self.wall, monotonic=self.mono)
            self.assertUnavailable(self.get(svc), "no_credential")
            self.assertEqual(fetch.count, 0)

    def test_empty_or_non_string_key(self):
        for token in ("", "   ", None, 123):
            fetch = FakeFetch()
            svc = self.service(granted(token=token), fetch)
            self.assertUnavailable(self.get(svc), "no_credential")
            self.assertEqual(fetch.count, 0)


# ---------------------------------------------------------------- concurrency

def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class ConcurrencyTests(Base):
    def run_threads(self, svc, n):
        results = [None] * n

        def worker(i):
            results[i] = svc.get_usage()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        return threads, results

    def test_ten_threads_single_fetch(self):
        gate = threading.Event()
        settings = granted()
        svc = self.service(settings, FakeFetch(OK, gate=gate))
        threads, results = self.run_threads(svc, 10)
        self.assertTrue(wait_until(lambda: settings.count >= 10))
        time.sleep(0.1)
        gate.set()
        for t in threads:
            t.join(15)
            self.assertFalse(t.is_alive())
        self.assertEqual(self.fetch.count, 1)
        dumped = {json.dumps(r, sort_keys=True) for r in results}
        self.assertEqual(len(dumped), 1)
        for r in results:
            check_payload(self, r)
            self.assertEqual(r["status"], "fresh")

    def test_rotation_during_inflight_discards_leader_result(self):
        gate = threading.Event()
        settings = granted()
        svc = self.service(settings, FakeFetch(OK, P.UpstreamError("network"), gate=gate))
        threads, results = self.run_threads(svc, 2)  # leader + follower on TOKEN_A
        self.assertTrue(wait_until(lambda: settings.count >= 2))
        time.sleep(0.1)
        settings.values["OPENAI_COMPATIBLE_API_KEY"] = TOKEN_B  # rotate while in flight
        gate.set()
        for t in threads:
            t.join(15)
        for r in results:
            check_payload(self, r)
            self.assertUnavailable(r, "credential_changed")
        self.assertEqual(self.fetch.tokens, [TOKEN_A])
        # The discarded TOKEN_A result is never shown for TOKEN_B.
        p = self.get(svc)
        self.assertUnavailable(p, "network")
        self.assertEqual(self.fetch.tokens, [TOKEN_A, TOKEN_B])

    def test_removal_during_inflight_discards_leader_result(self):
        settings = granted()

        def remove():
            settings.values.pop("OPENAI_COMPATIBLE_API_KEY", None)

        svc = self.service(settings, FakeFetch(OK, hook=remove))
        self.assertUnavailable(self.get(svc), "credential_changed")
        self.fetch.hook = None
        settings.values["OPENAI_COMPATIBLE_API_KEY"] = TOKEN_A
        self.fetch.steps.append(P.UpstreamError("timeout"))
        self.assertUnavailable(self.get(svc), "timeout")  # nothing was cached


# ---------------------------------------------------------------- transport

class TransportTests(unittest.TestCase):
    def test_request_shape_and_no_redirect_following(self):
        factory = ConnFactory(FakeResponse(200, {"Content-Length": str(len(body()))}, [body()]))
        status, retry_after, raw = P.https_get(TOKEN_A, connection_factory=factory, monotonic=Clock(0))
        self.assertEqual((status, retry_after, raw), (200, None, body()))
        self.assertEqual(len(factory.conns), 1)
        conn = factory.conns[0]
        self.assertEqual((conn.host, conn.port, conn.timeout), ("api.github.com", 443, 10.0))
        self.assertIsInstance(conn.context, ssl.SSLContext)
        self.assertEqual(len(conn.requests), 1)
        method, path, headers = conn.requests[0]
        self.assertEqual((method, path), ("GET", "/copilot_internal/user"))
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN_A)
        self.assertTrue(conn.closed)

        resp = FakeResponse(302, {"Location": "https://evil.example/steal"}, [b"redirect body"])
        factory = ConnFactory(resp)
        self.assertEqual(P.https_get(TOKEN_A, connection_factory=factory, monotonic=Clock(0)),
                         (302, None, b""))
        self.assertEqual(len(factory.conns), 1)
        self.assertEqual(len(factory.conns[0].requests), 1)
        self.assertEqual(resp.reads, 0)

    def test_default_factory_is_https_connection(self):
        self.assertIs(P.https_get.__kwdefaults__["connection_factory"], http.client.HTTPSConnection)
        self.assertEqual(P.UPSTREAM_HOST, "api.github.com")
        self.assertEqual(P.UPSTREAM_PATH, "/copilot_internal/user")

    def test_retry_after_passthrough(self):
        factory = ConnFactory(FakeResponse(429, {"Retry-After": "120"}))
        self.assertEqual(P.https_get(TOKEN_A, connection_factory=factory, monotonic=Clock(0)),
                         (429, "120", b""))

    def expect(self, code, factory, clock=None):
        with self.assertRaises(P.UpstreamError) as ctx:
            P.https_get(TOKEN_A, connection_factory=factory, monotonic=clock or Clock(0))
        self.assertEqual(ctx.exception.code, code)
        self.assertNotIn(EXC_TEXT, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        return ctx.exception

    def test_too_large_content_length(self):
        resp = FakeResponse(200, {"Content-Length": str(P.MAX_BODY_BYTES + 1)}, [b"{}"])
        self.expect("too_large", ConnFactory(resp))
        self.assertEqual(resp.reads, 0)

    def test_too_large_streamed(self):
        chunks = [b"x" * 65536] * 9  # 576 KiB, no Content-Length
        self.expect("too_large", ConnFactory(FakeResponse(200, {}, chunks)))
        lying = FakeResponse(200, {"Content-Length": "10"}, chunks)
        self.expect("too_large", ConnFactory(lying))

    def test_timeout_and_network(self):
        self.expect("timeout", ConnFactory(connect_exc=TimeoutError(EXC_TEXT)))
        self.expect("timeout", ConnFactory(getresponse_exc=socket.timeout(EXC_TEXT)))
        self.expect("timeout", ConnFactory(FakeResponse(200, {}, read_exc=socket.timeout(EXC_TEXT))))
        self.expect("network", ConnFactory(connect_exc=OSError(EXC_TEXT)))
        self.expect("network", ConnFactory(connect_exc=ssl.SSLError(EXC_TEXT)))
        self.expect("network", ConnFactory(getresponse_exc=http.client.RemoteDisconnected(EXC_TEXT)))
        self.expect("network", ConnFactory(request_exc=BrokenPipeError(EXC_TEXT)))

    def test_remaining_budget_tightening(self):
        clock = Clock(0)
        factory = ConnFactory(FakeResponse(200, {}, [body()]), clock=clock,
                              connect_cost=4.0, request_cost=3.0, getresponse_cost=1.0)
        status, _, raw = P.https_get(TOKEN_A, connection_factory=factory, monotonic=clock)
        self.assertEqual(status, 200)
        timeouts = factory.conns[0].sock.timeouts
        self.assertEqual(timeouts[0], 6.0)  # after connect: 10 - 4
        self.assertEqual(timeouts[1], 3.0)  # before getresponse: 10 - 7
        self.assertTrue(all(t <= 2.0 for t in timeouts[2:]))  # body reads: 10 - 8

    def test_budget_exhausted_after_connect_sends_nothing(self):
        clock = Clock(0)
        factory = ConnFactory(FakeResponse(200, {}, [body()]), clock=clock, connect_cost=10.5)
        self.expect("timeout", factory, clock)
        self.assertEqual(factory.conns[0].requests, [])  # token never sent
        self.assertTrue(factory.conns[0].closed)

    def test_budget_exhausted_before_getresponse(self):
        clock = Clock(0)
        factory = ConnFactory(FakeResponse(200, {}, [body()]), clock=clock, connect_cost=5, request_cost=6)
        self.expect("timeout", factory, clock)


# ---------------------------------------------------------------- host glue

class FakeApi:
    def __init__(self, settings=None):
        self.routes = []
        self.unload = []
        self.logs = []
        self.other = []
        self.settings = settings if settings is not None else {}

    def register_route(self, name, handler=None, methods=None, **kw):
        self.routes.append((name, handler, tuple(methods or ()), kw))

    def on_unload(self, callback):
        self.unload.append(callback)

    def get_settings(self, keys):
        return {k: self.settings[k] for k in keys if k in self.settings}

    def log(self, level, message, **fields):
        self.logs.append((level, message, fields))

    def __getattr__(self, name):  # any other registration is recorded as a failure
        def record(*a, **k):
            self.other.append(name)
        return record


def response_payload(tc, response):
    from starlette.responses import JSONResponse
    tc.assertIsInstance(response, JSONResponse)
    tc.assertEqual(response.status_code, 200)
    tc.assertEqual(response.headers.get("cache-control"), "no-store")
    payload = json.loads(bytes(response.body).decode("utf-8"))
    check_payload(tc, payload)
    return payload


class HostGlueTests(unittest.TestCase):
    def test_register_only_usage_get_and_unload(self):
        api = FakeApi()
        P.register(api)
        self.assertEqual([(r[0], r[2]) for r in api.routes], [("usage", ("GET",))])
        self.assertEqual(len(api.unload), 1)
        self.assertEqual(api.other, [])
        payload = response_payload(self, api.routes[0][1](object()))  # no settings: no fetch
        self.assertEqual(payload["error"]["code"], "no_credential")
        api.unload[0]()  # clears without error

    def test_register_wrong_origin_no_fetch(self):
        api = FakeApi({"OPENAI_COMPATIBLE_API_KEY": TOKEN_A,
                       "OPENAI_COMPATIBLE_BASE_URL": "https://api.openai.com/v1"})
        P.register(api)
        payload = response_payload(self, api.routes[0][1](object()))
        self.assertEqual(payload["error"]["code"], "credential_origin_mismatch")

    def test_handler_sanitizes_service_exceptions(self):
        class Exploding:
            def get_usage(self):
                raise RuntimeError(EXC_TEXT + " " + TOKEN_A)

        response = P.make_usage_handler(Exploding())(object())
        payload = response_payload(self, response)
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["error"]["code"], "internal")
        raw = bytes(response.body).decode("utf-8")
        for secret in SECRETS:
            self.assertNotIn(secret, raw)

    def test_handler_is_sync(self):
        import inspect
        handler = P.make_usage_handler(P.CreditsService(lambda: ("no_credential", None, None)))
        self.assertFalse(inspect.iscoroutinefunction(handler))


# ---------------------------------------------------------------- examples

def build_examples():
    """Deterministic example payloads from the 2030-01-02 synthetic fixture."""
    out = {}
    mono, wall = Clock(0.0), Clock(WALL_BASE)

    def service(fetch):
        return P.CreditsService(lambda: P.read_credential(granted()), fetch, wall=wall, monotonic=mono)

    out["fresh"] = service(FakeFetch(OK)).get_usage()
    svc = service(FakeFetch(OK, P.UpstreamError("timeout")))
    svc.get_usage()
    mono.t += 300
    wall.t += 300
    out["stale"] = svc.get_usage()
    mono.t, wall.t = 0.0, WALL_BASE
    out["unavailable"] = service(FakeFetch((401, None, b""))).get_usage()
    return out


def example_text(payload):
    return json.dumps(payload, indent=2) + "\n"


def write_examples():
    os.makedirs(EXAMPLES_DIR, exist_ok=True)
    for name, payload in build_examples().items():
        path = os.path.join(EXAMPLES_DIR, name + ".json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(example_text(payload))
        print("wrote", os.path.relpath(path, ROOT))


class ExampleTests(unittest.TestCase):
    def test_examples_are_current(self):
        for name, payload in build_examples().items():
            with self.subTest(name=name):
                check_payload(self, payload)
                self.assertEqual(payload["status"], name)
                path = os.path.join(EXAMPLES_DIR, name + ".json")
                self.assertTrue(os.path.exists(path), path + " missing: run --write-examples")
                with open(path, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), example_text(payload),
                                     name + ".json is out of date: run --write-examples")

    def test_fresh_example_values(self):
        fresh = build_examples()["fresh"]
        self.assertEqual(fresh["premium"]["used_percentage"], 75.8)
        self.assertEqual(fresh["nominal_usd"]["entitlement"], 100.0)
        self.assertEqual(fresh["nominal_usd"]["quota_remaining"], 25.005)
        self.assertEqual(fresh["reset_at"], "2030-02-01T00:00:00Z")
        self.assertEqual(fresh["observed_at"], "2030-01-02T03:00:00Z")


if __name__ == "__main__":
    if "--write-examples" in sys.argv:
        write_examples()
        sys.exit(0)
    unittest.main(verbosity=1)
