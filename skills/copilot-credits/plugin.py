"""copilot-credits: read-only GitHub Copilot premium credit usage for the owner's header.

One route, ``GET /api/extensions/copilot-credits/usage``, answers a sanitized
JSON projection (schema_version 1, see API.md) of the account's own
``premium_interactions`` quota bucket from the fixed GitHub endpoint
``https://api.github.com/copilot_internal/user``.

Design rules (rationale in README.md):
* The credential is read from current granted settings on EVERY request
  (``OPENAI_COMPATIBLE_API_KEY``) and is only sent when the paired
  ``OPENAI_COMPATIBLE_BASE_URL`` is an exact GitHub Copilot HTTPS origin, so a
  different vendor's key is never sent to GitHub. No legacy/other-key fallback.
* Fixed origin and path, GET only, no redirects (http.client never follows
  them), one attempt per refresh, bounded body size. Time is bounded by a
  socket-level per-operation timeout with remaining-budget tightening (each
  phase's socket timeout is lowered to what is left of the 10 s budget); DNS
  resolution is not bounded by the extension (no extra threads or alarms).
* One shared in-memory cache with single-flight, keyed by an HMAC fingerprint of
  (origin, key) under a per-process random salt. A credential change, removal,
  wrong origin or a 401/403 drops the snapshot immediately: stale data is only
  ever the SAME credential's last good snapshot. The leader re-reads the
  credential before publishing; if it changed while the request was in flight
  the result is discarded and ``credential_changed`` is answered.
* Every answer is HTTP 200 JSON with fixed sanitized messages; exception text,
  tokens and raw provider bodies never leave this module.
* No timers, daemons, tools, companions or page modules.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import socket
import ssl
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

from starlette.responses import JSONResponse  # the host ships starlette

SCHEMA_VERSION = 1
SOURCE = "github.copilot_internal.user"
UPSTREAM_HOST = "api.github.com"
UPSTREAM_PATH = "/copilot_internal/user"
KEY_SETTING = "OPENAI_COMPATIBLE_API_KEY"
BASE_URL_SETTING = "OPENAI_COMPATIBLE_BASE_URL"
# Exact GitHub Copilot model-API hosts whose key is a GitHub credential.
COPILOT_HOSTS = frozenset({
    "api.githubcopilot.com",
    "api.individual.githubcopilot.com",
    "api.business.githubcopilot.com",
    "api.enterprise.githubcopilot.com",
})

FRESH_TTL_S = 60.0
FAILURE_BACKOFF_S = 60.0
AUTH_FAILURE_BACKOFF_S = 300.0
RATE_LIMIT_DEFAULT_S = 300.0
RATE_LIMIT_MIN_S = 60.0
RATE_LIMIT_MAX_S = 900.0
STALE_MAX_AGE_S = 24 * 3600.0
UPSTREAM_DEADLINE_S = 10.0
FOLLOWER_WAIT_S = UPSTREAM_DEADLINE_S + 2.0
MAX_BODY_BYTES = 512 * 1024
MAX_TOKEN_CHARS = 1024
CREDIT_USD = 0.01
NOMINAL_BASIS = "1 credit = $0.01 consumption value; not an invoice"
USER_AGENT = "Viper-copilot-credits/1.0 (read-only)"

ERROR_MESSAGES: Dict[str, str] = {
    "no_credential": "No Copilot credential is available to this extension (missing or not granted).",
    "credential_origin_mismatch": "The configured compatible provider is not a GitHub Copilot origin; no request was sent.",
    "credential_unusable": "The configured credential has an unusable format; no request was sent.",
    "unauthorized": "GitHub rejected the credential (401).",
    "forbidden": "GitHub refused access to Copilot usage (403).",
    "not_found": "GitHub Copilot usage is not available for this account (404).",
    "rate_limited": "GitHub rate-limited the usage read (429).",
    "redirect_refused": "GitHub answered with a redirect; redirects are refused.",
    "http_error": "GitHub answered with an unexpected HTTP status.",
    "timeout": "The usage read timed out.",
    "network": "The usage read failed at the network or TLS layer.",
    "too_large": "The usage response exceeded the size limit and was not parsed.",
    "bad_json": "The usage response was not valid JSON.",
    "unrecognized_response": "The usage response did not contain a coherent premium credit quota.",
    "credential_changed": "The credential changed during the refresh; the next read uses the new one.",
    "pending": "A usage refresh is still in progress.",
    "internal": "The usage read failed inside the extension.",
}

_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_PLAN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII, no whitespace/CR/LF
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")


class UpstreamError(Exception):
    """A classified, already-sanitized failure. Carries no provider text."""

    def __init__(self, code: str, http_status: Optional[int] = None,
                 retry_after_s: Optional[int] = None) -> None:
        super().__init__(code)
        self.code = code if code in ERROR_MESSAGES else "internal"
        self.http_status = http_status
        self.retry_after_s = retry_after_s


# ---------------------------------------------------------------- credential

def copilot_origin(base_url: Any) -> Optional[str]:
    """Return the canonical https origin when base_url is an exact Copilot host."""
    if not isinstance(base_url, str):
        return None
    text = base_url.strip()
    if not text or len(text) > 2048 or any(ch.isspace() for ch in text):
        return None
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment or port not in (None, 443):
        return None
    host = (parts.hostname or "").lower()
    if host not in COPILOT_HOSTS:
        return None
    return "https://" + host


def read_credential(get_settings: Callable[[list], Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (error_code, origin, token) from CURRENT granted settings only."""
    try:
        values = get_settings([KEY_SETTING, BASE_URL_SETTING])
    except Exception:
        return "no_credential", None, None
    if not isinstance(values, dict):
        return "no_credential", None, None
    token = values.get(KEY_SETTING)
    token = token.strip() if isinstance(token, str) else ""
    if not token:
        return "no_credential", None, None
    origin = copilot_origin(values.get(BASE_URL_SETTING))
    if origin is None:
        return "credential_origin_mismatch", None, None
    if len(token) > MAX_TOKEN_CHARS or not _TOKEN_RE.match(token):
        return "credential_unusable", None, None
    return None, origin, token


# ---------------------------------------------------------------- transport

def https_get(token: str, *, deadline_s: float = UPSTREAM_DEADLINE_S,
              max_bytes: int = MAX_BODY_BYTES,
              connection_factory: Callable[..., Any] = http.client.HTTPSConnection,
              monotonic: Callable[[], float] = time.monotonic) -> Tuple[int, Optional[str], bytes]:
    """One GET of the fixed endpoint. Returns (status, retry_after, body).

    The body is only read for 200 answers. http.client never follows redirects.
    Raises UpstreamError(timeout|network|too_large); never echoes exception text.

    Time bound (honest version): this is a socket-level per-operation timeout
    with remaining-budget tightening. The connection starts with
    ``timeout=deadline_s`` (TCP connect + TLS handshake); after connect, and
    again before ``getresponse`` and before every body read, the socket timeout
    is lowered to the budget still remaining, and an exhausted budget raises
    ``timeout`` between phases. Each blocking socket operation is therefore
    bounded by the remaining budget, but a peer that keeps trickling bytes
    within one operation, multiple resolved addresses, or a slow TLS
    handshake can stretch wall time past ``deadline_s``. DNS resolution
    (getaddrinfo) is not bounded by the extension: no extra threads or alarms.
    """
    start = monotonic()
    conn = None

    def remaining() -> float:
        left = deadline_s - (monotonic() - start)
        if left <= 0:
            raise UpstreamError("timeout")
        return left

    def tighten() -> None:
        left = remaining()
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.settimeout(max(0.05, left))
            except OSError:
                pass

    try:
        conn = connection_factory(UPSTREAM_HOST, 443, timeout=deadline_s,
                                  context=ssl.create_default_context())
        conn.connect()  # explicit, so the following phases can be tightened
        tighten()
        conn.request("GET", UPSTREAM_PATH, headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2026-03-10",
        })
        tighten()
        resp = conn.getresponse()
        status = int(resp.status)
        retry_after = resp.getheader("Retry-After")
        if status != 200:
            return status, retry_after, b""
        length = resp.getheader("Content-Length")
        if length is not None:
            try:
                if int(length) > max_bytes:
                    raise UpstreamError("too_large", http_status=status)
            except ValueError:
                pass
        chunks = []
        total = 0
        while True:
            tighten()
            chunk = resp.read(min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise UpstreamError("too_large", http_status=status)
        if monotonic() - start > deadline_s:
            raise UpstreamError("timeout")
        return status, retry_after, b"".join(chunks)
    except UpstreamError:
        raise
    except (socket.timeout, TimeoutError):
        raise UpstreamError("timeout") from None
    except (ssl.SSLError, OSError, http.client.HTTPException, ValueError):
        raise UpstreamError("network") from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _retry_after_seconds(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text.isdigit() or len(text) > 9:
        return None  # HTTP-date or junk: ignored, default backoff applies
    return int(text)


def classify_status(status: int, retry_after_raw: Optional[str]) -> UpstreamError:
    retry_after = _retry_after_seconds(retry_after_raw)
    if 300 <= status < 400:
        return UpstreamError("redirect_refused", status)
    if status == 401:
        return UpstreamError("unauthorized", status, retry_after)
    if status == 403:
        return UpstreamError("forbidden", status, retry_after)
    if status == 404:
        return UpstreamError("not_found", status, retry_after)
    if status == 429:
        return UpstreamError("rate_limited", status, retry_after)
    return UpstreamError("http_error", status if 100 <= status <= 599 else None, retry_after)


# ---------------------------------------------------------------- parsing

def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return value


def _bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _iso_utc(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not _ISO_RE.match(value.strip()):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # fromisoformat in older Pythons accepts at most 6 fractional digits
    text = re.sub(r"\.(\d{6})\d+", r".\1", text)
    try:
        moment = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None or not (2000 <= moment.year <= 2200):
        return None
    return moment.astimezone(_dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_usage(body: bytes) -> Dict[str, Any]:
    """Project the provider body to the allowlisted snapshot or raise UpstreamError."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise UpstreamError("bad_json", 200) from None
    if not isinstance(data, dict):
        raise UpstreamError("unrecognized_response", 200)
    snapshots = data.get("quota_snapshots")
    premium = snapshots.get("premium_interactions") if isinstance(snapshots, dict) else None
    if not isinstance(premium, dict):
        raise UpstreamError("unrecognized_response", 200)
    unlimited = premium.get("unlimited")
    if not isinstance(unlimited, bool):
        raise UpstreamError("unrecognized_response", 200)
    entitlement = _num(premium.get("entitlement"))
    if entitlement is not None and entitlement < 0:
        entitlement = None
    if unlimited:
        percent_remaining = None
        used_percentage = None
    else:
        percent_remaining = _num(premium.get("percent_remaining"))
        # No clamping: an out-of-range or missing provider percentage is not a
        # coherent quota, so the UI never shows an invented value.
        if percent_remaining is None or not (0 <= percent_remaining <= 100):
            raise UpstreamError("unrecognized_response", 200)
        if entitlement == 0:
            raise UpstreamError("unrecognized_response", 200)
        used_percentage = round(100 - percent_remaining, 6)
    token_based = _bool(premium.get("token_based_billing"))
    if token_based is None:
        token_based = _bool(data.get("token_based_billing"))
    quota_remaining = _num(premium.get("quota_remaining"))
    credits_used = _num(premium.get("credits_used"))
    login = data.get("login")
    plan = data.get("copilot_plan")
    nominal = None
    if token_based is True and (entitlement is not None or quota_remaining is not None):
        nominal = {
            "basis": NOMINAL_BASIS,
            "entitlement": None if entitlement is None else round(entitlement * CREDIT_USD, 4),
            "quota_remaining": None if quota_remaining is None else round(quota_remaining * CREDIT_USD, 4),
        }
    return {
        "account": {
            "login": login if isinstance(login, str) and _LOGIN_RE.match(login) else None,
            "plan": plan if isinstance(plan, str) and _PLAN_RE.match(plan) else None,
        },
        "premium": {
            "used_percentage": used_percentage,
            "percent_remaining": percent_remaining,
            "entitlement": entitlement,
            "quota_remaining": quota_remaining,
            "credits_used": credits_used,
            "unlimited": unlimited,
            "overage_permitted": _bool(premium.get("overage_permitted")),
            "token_based_billing": token_based,
        },
        "reset_at": _iso_utc(data.get("quota_reset_date_utc")),
        "nominal_usd": nominal,
    }


# ---------------------------------------------------------------- service

def _iso_from_wall(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_obj(code: str, http_status: Optional[int] = None,
               retry_after_s: Optional[int] = None) -> Dict[str, Any]:
    code = code if code in ERROR_MESSAGES else "internal"
    return {"code": code, "message": ERROR_MESSAGES[code],
            "http_status": http_status, "retry_after_s": retry_after_s}


def unavailable_payload(code: str, *, last_attempt_at: Optional[str] = None,
                        next_refresh_after: Optional[str] = None,
                        http_status: Optional[int] = None,
                        retry_after_s: Optional[int] = None) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "status": "unavailable", "source": SOURCE,
        "observed_at": None, "last_attempt_at": last_attempt_at,
        "next_refresh_after": next_refresh_after,
        "error": _error_obj(code, http_status, retry_after_s),
        "account": None, "premium": None, "reset_at": None, "nominal_usd": None,
    }


# Failures after which the credential's snapshot must not be shown at all.
_DROP_SNAPSHOT_CODES = frozenset({"unauthorized", "forbidden"})


class CreditsService:
    """Shared cache + single-flight. Handlers run on worker threads, so threading."""

    def __init__(self, get_credential: Callable[[], Tuple[Optional[str], Optional[str], Optional[str]]],
                 fetch: Callable[[str], Tuple[int, Optional[str], bytes]] = https_get, *,
                 wall: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self._get_credential = get_credential
        self._fetch = fetch
        self._wall = wall
        self._mono = monotonic
        self._log = log
        self._salt = os.urandom(32)
        self._lock = threading.Lock()
        self._reset_locked(None)

    # state helpers (call with lock held)
    def _reset_locked(self, fingerprint: Optional[str]) -> None:
        self._fp = fingerprint
        self._generation = getattr(self, "_generation", 0) + 1
        self._snapshot: Optional[Dict[str, Any]] = None
        self._snap_mono: Optional[float] = None
        self._snap_wall: Optional[float] = None
        self._last_attempt_wall: Optional[float] = None
        self._next_mono: Optional[float] = None
        self._error: Optional[Dict[str, Any]] = None
        self._inflight: Optional[threading.Event] = None
        self._inflight_gen = -1

    def clear(self) -> None:
        with self._lock:
            self._reset_locked(None)

    def _fingerprint(self, origin: str, token: str) -> str:
        return hmac.new(self._salt, (origin + "\n" + token).encode("utf-8"), hashlib.sha256).hexdigest()

    def _render_locked(self) -> Dict[str, Any]:
        now_m = self._mono()
        now_w = self._wall()
        next_after = None
        if self._next_mono is not None and self._next_mono > now_m:
            next_after = _iso_from_wall(now_w + (self._next_mono - now_m))
        if self._snapshot is not None and self._snap_mono is not None \
                and now_m - self._snap_mono > STALE_MAX_AGE_S:
            self._snapshot = None
            self._snap_mono = self._snap_wall = None
        if self._snapshot is None:
            err = self._error if self._error is not None else _error_obj("pending")
            payload = unavailable_payload(err["code"], last_attempt_at=_iso_from_wall(self._last_attempt_wall),
                                          next_refresh_after=next_after, http_status=err["http_status"],
                                          retry_after_s=err["retry_after_s"])
            return payload
        # Explicit None check: a monotonic clock value of 0 is valid (and falsy).
        age = (now_m - self._snap_mono) if self._snap_mono is not None else 0.0
        fresh = self._error is None and age <= FRESH_TTL_S + FOLLOWER_WAIT_S
        if fresh:
            error = None
        else:
            error = self._error if self._error is not None else _error_obj("pending")
        snap = json.loads(json.dumps(self._snapshot))  # defensive deep copy
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "fresh" if fresh else "stale",
            "source": SOURCE,
            "observed_at": _iso_from_wall(self._snap_wall),
            "last_attempt_at": _iso_from_wall(self._last_attempt_wall),
            "next_refresh_after": next_after,
            "error": error,
            "account": snap["account"],
            "premium": snap["premium"],
            "reset_at": snap["reset_at"],
            "nominal_usd": snap["nominal_usd"],
        }

    def get_usage(self) -> Dict[str, Any]:
        code, origin, token = self._get_credential()
        if code is not None or origin is None or token is None or not origin or not token:
            with self._lock:
                self._reset_locked(None)  # clear/wrong origin drops everything
            return unavailable_payload(code if code is not None else "no_credential")
        fp = self._fingerprint(origin, token)
        with self._lock:
            if fp != self._fp:
                self._reset_locked(fp)
            if self._next_mono is not None and self._mono() < self._next_mono:
                return self._render_locked()
            if self._inflight is not None and self._inflight_gen == self._generation:
                event, leader = self._inflight, False
            else:
                event, leader = threading.Event(), True
                self._inflight, self._inflight_gen = event, self._generation
            generation = self._generation
        if not leader:
            event.wait(FOLLOWER_WAIT_S)
            with self._lock:
                if self._fp != fp:
                    return unavailable_payload("credential_changed")
                return self._render_locked()
        try:
            outcome: Any = self._refresh(token)
        except Exception:  # never let anything unclassified escape
            outcome = UpstreamError("internal")
        finally:
            token = None  # drop our reference promptly
        # Re-read the CURRENT credential before publishing (outside the lock:
        # get_settings is host code). A rotated, re-pointed or removed
        # credential means this result belongs to a credential that is no
        # longer configured, so it is discarded rather than cached.
        current_fp = self._current_fingerprint()
        with self._lock:
            try:
                if generation != self._generation:
                    return unavailable_payload("credential_changed")
                if current_fp != fp:
                    self._reset_locked(current_fp)
                    return unavailable_payload("credential_changed")
                self._apply_locked(outcome)
                return self._render_locked()
            finally:
                if self._inflight is event:
                    self._inflight = None
                event.set()

    def _current_fingerprint(self) -> Optional[str]:
        try:
            code, origin, token = self._get_credential()
        except Exception:
            return None
        if code is not None or origin is None or token is None or not origin or not token:
            return None
        return self._fingerprint(origin, token)

    def _refresh(self, token: str) -> Any:
        try:
            status, retry_after, body = self._fetch(token)
            if status != 200:
                return classify_status(status, retry_after)
            return parse_usage(body)
        except UpstreamError as error:
            return error

    def _apply_locked(self, outcome: Any) -> None:
        now_m, now_w = self._mono(), self._wall()
        self._last_attempt_wall = now_w
        if isinstance(outcome, dict):
            self._snapshot, self._snap_mono, self._snap_wall = outcome, now_m, now_w
            self._error = None
            self._next_mono = now_m + FRESH_TTL_S
            return
        error: UpstreamError = outcome
        if error.code in _DROP_SNAPSHOT_CODES:
            self._snapshot = None
            self._snap_mono = self._snap_wall = None
        if error.code == "rate_limited" or (error.code == "forbidden" and error.retry_after_s is not None):
            wait = float(error.retry_after_s) if error.retry_after_s is not None else RATE_LIMIT_DEFAULT_S
            backoff = max(RATE_LIMIT_MIN_S, min(RATE_LIMIT_MAX_S, wait))
        elif error.code in _DROP_SNAPSHOT_CODES:
            backoff = AUTH_FAILURE_BACKOFF_S
        else:
            backoff = FAILURE_BACKOFF_S
        self._next_mono = now_m + backoff
        self._error = _error_obj(error.code, error.http_status, error.retry_after_s)
        if self._log is not None:
            try:
                self._log(error.code)
            except Exception:
                pass


# ---------------------------------------------------------------- host glue

def _json_response(payload: Dict[str, Any]) -> Any:
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def make_usage_handler(service: CreditsService) -> Callable[[Any], Any]:
    def usage(_request: Any) -> Any:  # sync: the host runs it on a worker thread
        try:
            payload = service.get_usage()
        except Exception:
            payload = unavailable_payload("internal")
        return _json_response(payload)
    return usage


def register(api: Any) -> None:
    def credential() -> Tuple[Optional[str], Optional[str], Optional[str]]:
        return read_credential(api.get_settings)

    def log(code: str) -> None:
        api.log("warning", "copilot-credits usage refresh failed", code=code)

    service = CreditsService(credential, log=log)
    api.register_route("usage", handler=make_usage_handler(service), methods=("GET",))
    api.on_unload(service.clear)
