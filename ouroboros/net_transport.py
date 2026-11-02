"""Shared httpx transport construction for remote LLM clients.

Extracted from ``llm.py`` (size-ratchet byte budget, same precedent as
``loop_transport.py``): one factory owns the TCP-keepalive socket options
for every remote httpx client class, so a NAT/VPN mapping silently dropped
during a long silent reasoning stretch is detected by kernel probes within
minutes instead of hanging until the transport read timeout. Linux and
Darwin both get the idle/interval/count tuning where CPython exports the
constants, with values from ``config.py`` and Darwin's idle threshold set via
``TCP_KEEPALIVE`` (``platform_layer.tcp_keepalive_socket_options``); proxy-routed
installs (the cached and web-search clients skip the explicit transport, because
httpx env-proxy mounts require it absent), the Anthropic-native ``requests`` lane and
every other platform, Windows included (``SO_KEEPALIVE`` only), keep their
current behaviour — a disclosed residual.

The same module owns the trust bundle: ``extra_ca_bundle`` merges the owner's
``OUROBOROS_EXTRA_CA_BUNDLE`` PEM over certifi once and every first-party
client (httpx transports here, the Anthropic ``requests`` lane, the GigaChat
SDK, catalog and probe clients) verifies against that one path.

It also owns how an openai SDK client authenticates: ``openai_client_class``
gives a route the routing leaf declared keyless the SDK's keyless form.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Optional, Tuple


class ExtraCaBundleError(RuntimeError):
    """``OUROBOROS_EXTRA_CA_BUNDLE`` names a file that cannot serve as a trust anchor."""


_EXTRA_CA_BUNDLE_KEY = "OUROBOROS_EXTRA_CA_BUNDLE"
_merged_bundle_cache: Dict[Tuple[str, str, int, int], str] = {}
_ssl_context_cache: Dict[str, Any] = {}


def extra_ca_bundle() -> Optional[str]:
    """Path of the trust bundle every first-party HTTP client verifies against, or None.

    ``OUROBOROS_EXTRA_CA_BUNDLE`` names a PEM file with the CA certificates the
    default bundle lacks (a TLS-inspecting corporate proxy, a national CA such
    as the one behind GigaChat). httpx, requests and the GigaChat SDK each take
    ONE bundle path and treat it as the whole trust list, so the owner's file is
    merged with certifi into a content-addressed
    ``<data>/state/extra-ca-bundle/<digest>.pem`` and that path is returned: a
    changed owner file yields a new path, so every cache keyed on the path — the
    SSL context below and the provider clients — rotates with it; siblings older
    than a day are pruned (a task still holding an earlier setting keeps its file).
    Unset returns None and every client is built exactly as before the setting
    existed. An unreadable or non-PEM file raises ``ExtraCaBundleError``: a
    silent fall-back to certifi would reproduce the very TLS failure the owner
    set the key to cure.
    """
    from ouroboros.settings_integrity import runtime_setting

    raw = str(runtime_setting(_EXTRA_CA_BUNDLE_KEY, "") or "").strip()
    if not raw:
        return None
    import os
    import pathlib

    from ouroboros.config import DATA_DIR

    extra = pathlib.Path(raw).expanduser()
    bundle_dir = pathlib.Path(DATA_DIR) / "state" / "extra-ca-bundle"
    try:
        stat = extra.stat()
    except OSError as exc:
        raise ExtraCaBundleError(f"{_EXTRA_CA_BUNDLE_KEY} is not readable: {extra} ({exc})") from exc
    key = (str(extra), str(bundle_dir), stat.st_mtime_ns, stat.st_size)
    cached = _merged_bundle_cache.get(key)
    if cached and os.path.isfile(cached):
        return cached
    import ssl

    import certifi

    try:
        extra_bytes = extra.read_bytes()
    except OSError as exc:
        raise ExtraCaBundleError(f"{_EXTRA_CA_BUNDLE_KEY} is not readable: {extra} ({exc})") from exc
    try:
        # Load the owner's file on its own first: a malformed PEM must surface as
        # this typed error at the setting, not as an SSLError inside some client.
        ssl.create_default_context().load_verify_locations(cadata=extra_bytes.decode("ascii", "replace"))
    except (ssl.SSLError, ValueError) as exc:
        raise ExtraCaBundleError(f"{_EXTRA_CA_BUNDLE_KEY} holds no loadable PEM certificate: {extra} ({exc})") from exc

    import hashlib
    import time

    from ouroboros.utils import write_bytes_atomic

    base = pathlib.Path(certifi.where()).read_bytes()
    merged = base.rstrip(b"\n") + b"\n" + extra_bytes.rstrip(b"\n") + b"\n"
    digest = hashlib.sha256(merged).hexdigest()[:12]
    target = bundle_dir / f"{digest}.pem"
    try:
        if not target.is_file():
            bundle_dir.mkdir(parents=True, exist_ok=True)
            write_bytes_atomic(target, merged)
        cutoff = time.time() - 86400
        for stale in bundle_dir.glob("*.pem"):
            if stale != target:
                try:
                    if stale.stat().st_mtime < cutoff:
                        stale.unlink()
                except OSError:
                    pass  # a sibling another process is materializing or has just pruned
    except OSError as exc:
        raise ExtraCaBundleError(f"cannot write the merged trust bundle {target}: {exc}") from exc
    _merged_bundle_cache[key] = str(target)
    return str(target)


def trust_ssl_context():
    """The ``ssl.SSLContext`` over the merged bundle, or None without the setting.

    httpx deprecates a path-valued ``verify``; one context per merged bundle is
    built here and shared by every httpx client (a context is read-only after
    construction, so sharing across clients and threads is safe).
    """
    bundle = extra_ca_bundle()
    if bundle is None:
        return None
    context = _ssl_context_cache.get(bundle)
    if context is None:
        import ssl

        context = ssl.create_default_context(cafile=bundle)
        _ssl_context_cache[bundle] = context
    return context


def verify_kwargs() -> Dict[str, Any]:
    """``{"verify": <SSLContext>}`` for an httpx constructor, ``{}`` without the setting."""
    context = trust_ssl_context()
    return {"verify": context} if context is not None else {}


def requests_verify_kwargs() -> Dict[str, Any]:
    """``{"verify": <bundle path>}`` for a ``requests`` call, ``{}`` without the setting."""
    bundle = extra_ca_bundle()
    return {"verify": bundle} if bundle else {}


def copilot_compatible_headers(base_url: Any, api_key: Any = "") -> Dict[str, str]:
    """Return GitHub Copilot API headers when ``base_url`` targets ``githubcopilot.com``."""
    if "githubcopilot.com" not in str(base_url or "").lower():
        return {}
    key = str(api_key or "").strip()
    return {
        "Editor-Version": "vscode/1.104.1",
        "User-Agent": "HermesAgent/1.0",
        "Copilot-Integration-Id": "copilot-developer-cli" if key.startswith("github_pat_") else "vscode-chat",
        "Openai-Intent": "conversation-edits",
        "x-initiator": "agent",
    }


_COPILOT_RESPONSES_MODEL_PREFIXES = ("gpt-5.3", "gpt-5.4", "gpt-5.5", "gpt-5.6", "gpt-6", "mai-code")
_COPILOT_EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
    "ultra": "xhigh",
}


def _copilot_uses_responses_api(model: str) -> bool:
    m = str(model or "").strip().lower()
    return m.startswith(_COPILOT_RESPONSES_MODEL_PREFIXES)


def _copilot_chat_to_responses_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    model = str(payload.get("model") or "")
    out: Dict[str, Any] = {"model": model}
    input_items = []
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        raw_content = msg.get("content")
        content_val: Any = ""
        if isinstance(raw_content, str):
            content_val = raw_content
        elif isinstance(raw_content, list):
            has_image = any(isinstance(b, dict) and b.get("type") == "image_url" for b in raw_content)
            if not has_image:
                content_val = "".join(
                    b if isinstance(b, str) else str(b.get("text") or "")
                    for b in raw_content
                    if isinstance(b, (str, dict))
                )
            else:
                blocks = []
                text_type = "output_text" if role == "assistant" else "input_text"
                for b in raw_content:
                    if isinstance(b, str):
                        blocks.append({"type": text_type, "text": b})
                    elif isinstance(b, dict):
                        btype = b.get("type")
                        if btype == "text":
                            blocks.append({"type": text_type, "text": str(b.get("text") or "")})
                        elif btype == "image_url":
                            img = b.get("image_url")
                            url_str = img.get("url") if isinstance(img, dict) else str(img or "")
                            if url_str:
                                blocks.append({"type": "input_image", "image_url": url_str})
                content_val = blocks
        if role in ("system", "developer", "user"):
            input_items.append({"role": role, "content": content_val})
        elif role == "assistant":
            if content_val:
                input_items.append({"role": "assistant", "content": content_val})
            for idx, tc in enumerate(msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                input_items.append({
                    "type": "function_call",
                    "call_id": str(tc.get("id") or f"call_{idx}"),
                    "name": str(fn.get("name") or ""),
                    "arguments": str(fn.get("arguments") or "{}"),
                })
        elif role == "tool":
            out_str = content_val if isinstance(content_val, str) else json.dumps(content_val)
            input_items.append({
                "type": "function_call_output",
                "call_id": str(msg.get("tool_call_id") or "call_0"),
                "output": out_str,
            })
    out["input"] = input_items

    raw_tools = payload.get("tools")
    if isinstance(raw_tools, list) and raw_tools:
        resp_tools = []
        for t in raw_tools:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                fn = t["function"]
                resp_tools.append({
                    "type": "function",
                    "name": str(fn.get("name") or ""),
                    "description": str(fn.get("description") or ""),
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                    "strict": bool(fn.get("strict", False)),
                })
            else:
                resp_tools.append(t)
        if resp_tools:
            out["tools"] = resp_tools

    tc = payload.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") == "function" and isinstance(tc.get("function"), dict):
        out["tool_choice"] = {"type": "function", "name": str(tc["function"].get("name") or "")}
    elif tc is not None:
        out["tool_choice"] = tc

    max_tok = payload.get("max_completion_tokens") or payload.get("max_tokens")
    if isinstance(max_tok, int) and max_tok > 0:
        out["max_output_tokens"] = max(16, max_tok)

    eff = payload.get("reasoning_effort")
    if not eff and isinstance(payload.get("reasoning"), dict):
        eff = payload["reasoning"].get("effort")
    mapped_eff = _COPILOT_EFFORT_MAP.get(str(eff or "").strip().lower())
    if mapped_eff:
        out["reasoning"] = {"effort": mapped_eff}

    rf = payload.get("response_format")
    if isinstance(rf, dict) and rf.get("type"):
        if rf["type"] == "json_object":
            out["text"] = {"format": {"type": "json_object"}}
        elif rf["type"] == "json_schema" and isinstance(rf.get("json_schema"), dict):
            js = rf["json_schema"]
            out["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": str(js.get("name") or "response"),
                    "schema": js.get("schema") or {},
                    "strict": bool(js.get("strict", False)),
                }
            }
    return out


def _copilot_responses_to_chat_completion(rjson: Dict[str, Any], requested_model: str) -> Dict[str, Any]:
    import time

    text_parts = []
    reasoning_parts = []
    tool_calls = []
    for idx, item in enumerate(rjson.get("output") or []):
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    text_parts.append(str(c.get("text") or ""))
        elif itype == "function_call":
            tool_calls.append({
                "id": str(item.get("call_id") or item.get("id") or f"call_{idx}"),
                "type": "function",
                "function": {
                    "name": str(item.get("name") or ""),
                    "arguments": str(item.get("arguments") or "{}"),
                },
            })
        elif itype == "reasoning":
            for s in item.get("summary") or []:
                if isinstance(s, dict) and s.get("text"):
                    reasoning_parts.append(str(s["text"]))

    content_text = "".join(text_parts) if text_parts else None
    if content_text is None and not tool_calls:
        content_text = ""
    message: Dict[str, Any] = {"role": "assistant", "content": content_text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_parts:
        message["reasoning_content"] = "\n".join(reasoning_parts)

    finish_reason = "tool_calls" if tool_calls else ("length" if rjson.get("status") == "incomplete" else "stop")
    u = rjson.get("usage") if isinstance(rjson.get("usage"), dict) else {}
    in_tok = int(u.get("input_tokens") or 0)
    out_tok = int(u.get("output_tokens") or 0)
    tot_tok = int(u.get("total_tokens") or (in_tok + out_tok))
    in_det = u.get("input_tokens_details") if isinstance(u.get("input_tokens_details"), dict) else {}
    out_det = u.get("output_tokens_details") if isinstance(u.get("output_tokens_details"), dict) else {}

    return {
        "id": str(rjson.get("id") or "chatcmpl-copilot-responses"),
        "object": "chat.completion",
        "created": int(rjson.get("created_at") or time.time()),
        "model": str(rjson.get("model") or requested_model),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": in_tok,
            "completion_tokens": out_tok,
            "total_tokens": tot_tok,
            "prompt_tokens_details": {"cached_tokens": int(in_det.get("cached_tokens") or 0)},
            "completion_tokens_details": {"reasoning_tokens": int(out_det.get("reasoning_tokens") or 0)},
        },
    }


def _copilot_chat_completion_to_sse(chat_comp: Dict[str, Any]) -> bytes:
    import json

    choice = (chat_comp.get("choices") or [{}])[0]
    msg = dict(choice.get("message") or {"role": "assistant", "content": ""})
    if "tool_calls" in msg:
        msg["tool_calls"] = [
            {**tc, "index": idx} for idx, tc in enumerate(msg["tool_calls"])
        ]
    chunk = {
        "id": chat_comp.get("id"),
        "object": "chat.completion.chunk",
        "created": chat_comp.get("created"),
        "model": chat_comp.get("model"),
        "choices": [{
            "index": 0,
            "delta": msg,
            "finish_reason": choice.get("finish_reason") or "stop",
        }],
        "usage": chat_comp.get("usage"),
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode("utf-8")


def _wrap_copilot_responses_transport(base_transport: Any, httpx_mod: Any, async_client: bool) -> Any:
    import json

    def _prepare(request: Any):
        if request.method != "POST" or "githubcopilot.com" not in request.url.host.lower():
            return None
        if not request.url.path.endswith("/chat/completions"):
            return None
        try:
            body_bytes = request.read()
            payload = json.loads(body_bytes)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        model = str(payload.get("model") or "")
        if not _copilot_uses_responses_api(model):
            return None
        was_stream = bool(payload.get("stream"))
        new_payload = _copilot_chat_to_responses_payload(payload)
        new_bytes = json.dumps(new_payload).encode("utf-8")
        new_path = request.url.path[:-len("/chat/completions")] + "/responses"
        new_url = request.url.copy_with(path=new_path)
        headers = dict(request.headers)
        headers["content-length"] = str(len(new_bytes))
        headers["accept"] = "application/json"
        new_req = httpx_mod.Request(
            method="POST",
            url=new_url,
            headers=headers,
            content=new_bytes,
            extensions=request.extensions,
        )
        return new_req, model, was_stream

    def _convert_response(resp: Any, raw_bytes: bytes, request: Any, model: str, was_stream: bool):
        if resp.status_code != 200:
            return httpx_mod.Response(
                status_code=resp.status_code,
                headers=resp.headers,
                content=raw_bytes,
                request=request,
            )
        rjson = json.loads(raw_bytes)
        chat_comp = _copilot_responses_to_chat_completion(rjson, model)
        if was_stream:
            sse_bytes = _copilot_chat_completion_to_sse(chat_comp)
            return httpx_mod.Response(
                status_code=200,
                headers={"content-type": "text/event-stream; charset=utf-8"},
                content=sse_bytes,
                request=request,
            )
        out_bytes = json.dumps(chat_comp).encode("utf-8")
        return httpx_mod.Response(
            status_code=200,
            headers={"content-type": "application/json"},
            content=out_bytes,
            request=request,
        )

    if async_client:
        if not hasattr(base_transport, "handle_async_request"):
            return base_transport
        orig_async = base_transport.handle_async_request

        async def _handle_async_request(request: Any):
            prep = _prepare(request)
            if prep is None:
                return await orig_async(request)
            new_req, model, was_stream = prep
            resp = await orig_async(new_req)
            raw_bytes = await resp.aread()
            await resp.aclose()
            return _convert_response(resp, raw_bytes, request, model, was_stream)

        base_transport.handle_async_request = _handle_async_request
        return base_transport

    if not hasattr(base_transport, "handle_request"):
        return base_transport
    orig_sync = base_transport.handle_request

    def _handle_request(request: Any):
        prep = _prepare(request)
        if prep is None:
            return orig_sync(request)
        new_req, model, was_stream = prep
        resp = orig_sync(new_req)
        raw_bytes = resp.read()
        resp.close()
        return _convert_response(resp, raw_bytes, request, model, was_stream)

    base_transport.handle_request = _handle_request
    return base_transport


def remote_httpx_transport(
    async_client: bool = False,
    *,
    trust_env: bool = True,
    limits: Optional[Any] = None,
    httpx_mod: Optional[Any] = None,
):
    """Build the shared keepalive (Async)HTTPTransport.

    Socket options live on the transport (httpx ignores ``socket_options``
    on the Client itself). ``trust_env`` must be forwarded here too: httpx
    uses an explicit transport as-is, so ``Client(trust_env=False)`` alone
    never reaches ``create_ssl_context`` — the no-proxy clients pass
    ``trust_env=False`` to keep SSL_CERT_FILE/SSL_CERT_DIR env isolation.
    """
    if httpx_mod is None:
        import httpx as httpx_mod

    from ouroboros.platform_layer import tcp_keepalive_socket_options

    kwargs: Dict[str, Any] = {
        "socket_options": tcp_keepalive_socket_options(),
        "trust_env": trust_env,
    }
    kwargs.update(verify_kwargs())
    if limits is not None:
        kwargs["limits"] = limits
    transport_cls = httpx_mod.AsyncHTTPTransport if async_client else httpx_mod.HTTPTransport
    try:
        transport = transport_cls(**kwargs)
    except TypeError:
        # httpx < 0.25 has no ``socket_options`` parameter; the dependency pin
        # is a bare ``httpx``, so such a venv is legal. Rebuild without the
        # keepalive tuning (silently absent) rather than killing every remote
        # call at client construction — same guard shape as the openai
        # Default-client getattr fallback below.
        kwargs.pop("socket_options", None)
        transport = transport_cls(**kwargs)
    return _wrap_copilot_responses_transport(transport, httpx_mod, async_client)


def _sdk_pool_limits(httpx_mod: Optional[Any] = None):
    """openai-SDK-equivalent pool limits for long-lived clients.

    An explicit transport ignores the Client-level limits, silently
    downgrading the SDK's 1000/100 pool to the httpx 100/20 defaults — and a
    self-inflicted PoolTimeout would then read as a transport outage.
    """
    if httpx_mod is None:
        import httpx as httpx_mod

    return httpx_mod.Limits(
        max_connections=1000, max_keepalive_connections=100, keepalive_expiry=5.0
    )


def env_proxies_configured() -> bool:
    """True when any proxy httpx would honor is configured.

    Mirrors httpx (``get_environment_proxies`` builds its mounts from
    ``urllib.request.getproxies()``): that includes not only the
    HTTP(S)_PROXY/ALL_PROXY env vars but also macOS SystemConfiguration and
    the Windows registry, so a system-proxy install (no env vars; a typical
    GUI-launched macOS app) is detected too. httpx honors those proxies only
    when no explicit transport is passed, so attaching the keepalive
    transport there would silently break the install's only working egress.
    A lone ``no``/NO_PROXY entry does not count.
    """
    import urllib.request

    proxies = urllib.request.getproxies()
    return any(scheme in ("http", "https", "all") for scheme in proxies)


def keepalive_http_client(async_client: bool = False):
    """openai Default(Async)HttpxClient on the keepalive transport, or None.

    None on proxy-routed installs without a trust bundle: they keep the SDK
    default construction so httpx env-proxy mounts survive (disclosed residual:
    no TCP-keepalive tuning there); with a bundle the Default client carries it
    as ``verify`` and still mounts the env proxies.
    """
    proxied = env_proxies_configured()
    bundle = extra_ca_bundle()
    if proxied and bundle is None:
        return None
    import openai

    cls = getattr(
        openai,
        "DefaultAsyncHttpxClient" if async_client else "DefaultHttpxClient",
        None,
    )
    if cls is None:
        # An SDK build without the Default client classes falls back to SDK
        # default construction (no keepalive tuning) rather than failing the
        # LLM client construction over a tuning concern.
        return None
    sdk_httpx = _openai_httpx_module()
    # Proxy-routed installs keep SDK default construction (env proxy mounts
    # survive only without an explicit transport) and receive the trust bundle
    # through ``verify``; everyone else gets it on the keepalive transport.
    kwargs: Dict[str, Any] = (
        verify_kwargs() if proxied
        else {"transport": remote_httpx_transport(async_client, limits=_sdk_pool_limits(sdk_httpx), httpx_mod=sdk_httpx)}
    )
    try:
        return cls(**kwargs)
    except TypeError:
        # A future SDK generation whose Default client rejects the transport
        # object falls back to SDK default construction the same way.
        return None


_keyless_client_classes: Dict[Any, Any] = {}


def openai_client_class(target: Dict[str, Any], *, async_client: bool = False):
    """The openai SDK client class for ``target``: the SDK's own, or its keyless form.

    Only the route declares an endpoint keyless (``target["keyless"]``: the dedicated
    OpenAI-compatible endpoint saved without a key, "Leave empty for no auth"); a key on
    the target always wins, and every other target gets the SDK class unchanged. openai 2.x
    refuses a blank key twice before any HTTP: construction raises "Missing credentials"
    and each request "Could not resolve authentication method". The keyless form waives
    the construction check and sends the SDK's own explicit ``Authorization`` omission
    with every request, so no credential from any source (a key, the SDK's environment,
    default headers) reaches the endpoint, and no placeholder key exists anywhere.
    """
    import openai

    base = openai.AsyncOpenAI if async_client else openai.OpenAI
    if not target.get("keyless") or str(target.get("api_key") or ""):
        return base
    keyless = _keyless_client_classes.get(base)
    if keyless is None:
        keyless = _keyless_client_classes[base] = _keyless_client_class(base, async_client)
    return keyless


def _keyless_client_class(base, async_client: bool):
    import inspect

    from openai import Omit

    # openai<2 has no construction check to waive: it already accepts a blank key.
    waiver = ({"_enforce_credentials": False}
              if "_enforce_credentials" in inspect.signature(base.__init__).parameters else {})

    def omit_authorization(options):
        headers = options.headers if isinstance(options.headers, Mapping) else {}
        options.headers = {**headers, "Authorization": Omit()}
        return options

    class Keyless(base):
        def __init__(self, **kwargs):  # ``copy()``/``with_options()`` rebuild through here too
            super().__init__(**{**kwargs, "api_key": "", **waiver})

        def _build_headers(self, options, *, retries_taken=0):
            headers = super()._build_headers(options, retries_taken=retries_taken)
            # SDK dictionaries merge case-sensitively; the final Headers object does not.
            # Strip every spelling, including defaults or per-request overrides.
            headers.pop("Authorization", None)
            return headers

    if async_client:
        class KeylessAsync(Keyless):
            async def _prepare_options(self, options):
                return omit_authorization(await super()._prepare_options(options))
        client = KeylessAsync
    else:
        class KeylessSync(Keyless):
            def _prepare_options(self, options):
                return omit_authorization(super()._prepare_options(options))
        client = KeylessSync

    client.__name__ = client.__qualname__ = f"Keyless{base.__name__}"
    return client


def _openai_httpx_module():
    import importlib
    import httpx
    import openai

    if (
        getattr(httpx.Client, "__module__", "").split(".")[0] != "httpx"
        or getattr(httpx.AsyncClient, "__module__", "").split(".")[0] != "httpx"
        or getattr(httpx.HTTPTransport, "__module__", "").split(".")[0] != "httpx"
        or getattr(httpx.AsyncHTTPTransport, "__module__", "").split(".")[0] != "httpx"
    ):
        return httpx
    cls = getattr(openai, "DefaultHttpxClient", None)
    bases = getattr(cls, "__bases__", None)
    if bases:
        pkg = bases[0].__module__.split(".")[0]
        if pkg in ("httpx", "httpx2"):
            return importlib.import_module(pkg)
    return httpx


def make_no_proxy_client(target: Dict[str, Any], timeout: Any) -> Tuple[Any, Any]:
    """Per-call OpenAI client fully isolated from proxy/SSL environment.

    ``timeout`` is the caller's explicit per-call bound (an ``httpx.Timeout``
    or a float); no default is applied here.
    """
    httpx_mod = _openai_httpx_module()

    http_client = httpx_mod.Client(
        trust_env=False,
        mounts={},
        timeout=timeout,
        transport=remote_httpx_transport(trust_env=False, httpx_mod=httpx_mod),
    )
    oa_client = openai_client_class(target)(
        api_key=str(target.get("api_key") or ""),
        base_url=str(target.get("base_url") or ""),
        default_headers=dict(target.get("default_headers") or {}),
        http_client=http_client,
        max_retries=0,
    )
    return oa_client, http_client


def make_no_proxy_async_client(target: Dict[str, Any], timeout: Any) -> Tuple[Any, Any]:
    """Async variant of :func:`make_no_proxy_client`.

    ``timeout`` is the caller's explicit per-call bound, as above.
    """
    httpx_mod = _openai_httpx_module()

    http_client = httpx_mod.AsyncClient(
        trust_env=False,
        mounts={},
        timeout=timeout,
        transport=remote_httpx_transport(async_client=True, trust_env=False, httpx_mod=httpx_mod),
    )
    oa_client = openai_client_class(target, async_client=True)(
        api_key=str(target.get("api_key") or ""),
        base_url=str(target.get("base_url") or ""),
        default_headers=dict(target.get("default_headers") or {}),
        http_client=http_client,
        max_retries=0,
    )
    return oa_client, http_client


def web_search_openai_client(
    *, api_key: str, base_url: Optional[str], timeout: Optional[float] = None,
    default_headers: Optional[Dict[str, str]] = None,
):
    """Web-search OpenAI client (Q16 coverage) on the keepalive transport."""
    from openai import OpenAI

    kwargs: Dict[str, Any] = {"api_key": api_key, "max_retries": 0}
    if base_url:
        kwargs["base_url"] = base_url
    if timeout is not None:
        kwargs["timeout"] = float(timeout)
    if default_headers:
        kwargs["default_headers"] = dict(default_headers)
    http_client = keepalive_http_client()
    if http_client is not None:
        kwargs["http_client"] = http_client
    return OpenAI(**kwargs)
