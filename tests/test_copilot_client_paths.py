"""Real SDK mount-selection and host-only HTTP observation regressions (no network)."""
import asyncio
import copy
import json
from types import SimpleNamespace

import certifi
import pytest

from ouroboros import net_transport as nt, llm_observability as obs
from ouroboros.llm import LLMClient
from ouroboros import request_wire_recovery as recovery
from ouroboros.request_wire_contract import physical_candidate_sha256
from ouroboros.usage_accounting import PhysicalAttemptCapture


def target(host="api.business.githubcopilot.com", model="gpt-6.1-sol"):
    return dict(provider="openai-compatible", resolved_model=model, usage_model="openai-compatible/" + model,
                api_key="fixture", base_url="https://" + host, supports_openrouter_extensions=False)


def native():
    return dict(id="r1", model="gpt-6.1-sol", status="completed", output=[
        dict(type="message", status="completed", content=[dict(type="output_text", text="OK")])])


def chat():
    return dict(id="c1", model="other", choices=[dict(index=0, finish_reason="stop",
                message=dict(role="assistant", content="OK"))], usage={})


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("urllib.request.getproxies", lambda: {})
    monkeypatch.setattr(obs, "retain_http_observation", lambda *a, **k: {})


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("proxy_mode", ["direct", "proxy", "no_proxy"])
@pytest.mark.parametrize("extra_ca", [False, True])
def test_production_cached_mount_selection(monkeypatch, clean_env, asynchronous, proxy_mode, extra_ca):
    hm = nt._openai_httpx_module()
    proxy = proxy_mode != "direct"
    import importlib
    # httpx imports urllib's callable by value; the first SDK import can occur
    # under clean_env's empty stub. Patch that actual imported alias as well.
    proxy_facts = {"https": "http://127.0.0.1:8899", **(
        {"no": "api.business.githubcopilot.com"} if proxy_mode == "no_proxy" else {})} if proxy else {}
    monkeypatch.setattr(importlib.import_module(hm.__name__ + "._utils"), "getproxies", lambda: proxy_facts)
    if proxy:
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8899")
        if proxy_mode == "no_proxy":
            monkeypatch.setenv("NO_PROXY", "api.business.githubcopilot.com")
        monkeypatch.setattr("urllib.request.getproxies", lambda: {
            "https": "http://127.0.0.1:8899", **({"no": "api.business.githubcopilot.com"} if proxy_mode == "no_proxy" else {})})
    monkeypatch.setattr(nt, "extra_ca_bundle", lambda: certifi.where() if extra_ca else None)
    seen = []
    selected_proxy = []
    real_init = hm.HTTPTransport.__init__
    real_async_init = hm.AsyncHTTPTransport.__init__
    def init(self, *args, **kwargs):
        selected_proxy.append(bool(kwargs.get("proxy")))
        real_init(self, *args, **kwargs)
    def ainit(self, *args, **kwargs):
        selected_proxy.append(bool(kwargs.get("proxy")))
        real_async_init(self, *args, **kwargs)
    def handle(self, request):
        # Observe the actual selected transport, not just wrapper output.
        import importlib
        core = importlib.import_module(type(self._pool).__module__.split(".")[0])
        is_proxy = isinstance(self._pool, core.HTTPProxy)
        seen.append((request, is_proxy))
        return hm.Response(200, json=native())
    async def ahandle(self, request):
        import importlib
        core = importlib.import_module(type(self._pool).__module__.split(".")[0])
        is_proxy = isinstance(self._pool, core.AsyncHTTPProxy)
        seen.append((request, is_proxy))
        return hm.Response(200, json=native())
    monkeypatch.setattr(hm.HTTPTransport, "__init__", init)
    monkeypatch.setattr(hm.AsyncHTTPTransport, "__init__", ainit)
    monkeypatch.setattr(hm.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(hm.AsyncHTTPTransport, "handle_async_request", ahandle)
    args = dict(model="gpt-6.1-sol", messages=[dict(role="user", content="fixture")], reasoning_effort="high")
    assert nt.env_proxies_configured() == proxy
    if asynchronous:
        async def run():
            llm = LLMClient()
            remote = llm._get_async_remote_client(target())
            try:
                assert await remote.chat.completions.create(**args)
            finally:
                await remote.close()
        asyncio.run(run())
    else:
        with LLMClient._new_remote_client(target()) as remote:
            assert bool(remote._client._mounts) == proxy, (remote._client.trust_env, selected_proxy)
            assert remote.chat.completions.create(**args)
    assert len(seen) == 1
    request, via_proxy = seen[0]
    assert request.url.path == "/responses"
    assert json.loads(request.content)["reasoning"] == {"effort": "high"}
    assert via_proxy == (proxy_mode == "proxy")
    assert selected_proxy and any(selected_proxy) == proxy


@pytest.mark.parametrize("kind", ["no_proxy", "search", "missing_defaults", "reject_defaults", "keyless"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_all_first_party_constructors(monkeypatch, clean_env, kind, asynchronous):
    import openai
    hm = nt._openai_httpx_module()
    seen = []
    def handle(self, request):
        seen.append(request)
        return hm.Response(200, json=native())
    async def ahandle(self, request):
        return handle(self, request)
    monkeypatch.setattr(hm.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(hm.AsyncHTTPTransport, "handle_async_request", ahandle)
    if kind == "missing_defaults":
        monkeypatch.delattr(openai, "DefaultHttpxClient")
        monkeypatch.delattr(openai, "DefaultAsyncHttpxClient")
    elif kind == "reject_defaults":
        def reject(**kwargs):
            raise TypeError("fixture: default transport not supported")
        monkeypatch.setattr(openai, "DefaultHttpxClient", reject)
        monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", reject)
    t = target()
    if kind == "keyless":
        t.update(api_key="", keyless=True, default_headers={"Authorization": "must-not-send"})
    args = dict(model=t["resolved_model"], messages=[dict(role="user", content="fixture")])
    if asynchronous:
        async def run():
            if kind == "no_proxy":
                remote, _ = nt.make_no_proxy_async_client(t, 11)
            elif kind == "search":
                # The shared web-search constructor is synchronous only.
                return
            else:
                remote = LLMClient()._get_async_remote_client(t)
            try:
                await remote.chat.completions.create(**args)
            finally:
                await remote.close()
        asyncio.run(run())
        if kind == "search":
            assert not seen
            return
    else:
        if kind == "no_proxy":
            remote, _ = nt.make_no_proxy_client(t, 11)
        elif kind == "search":
            remote = nt.web_search_openai_client(api_key="fixture", base_url=t["base_url"])
        else:
            remote = LLMClient._new_remote_client(t)
        with remote:
            remote.chat.completions.create(**args)
    assert len(seen) == 1 and seen[0].url.path == "/responses"
    assert ("authorization" not in seen[0].headers) == (kind == "keyless")


@pytest.mark.parametrize("host,model,translate", [
    ("githubcopilot.com", "gpt-6.1-sol", True), ("api.githubcopilot.com", "gpt-6.1-sol", True),
    ("new-region.githubcopilot.com", "gpt-6.1-sol", True),
    ("api.business.githubcopilot.com", "claude-opus-5.5", False),
    ("githubcopilot.com.attacker.test", "gpt-6.1-sol", False), ("provider.test", "gpt-6.1-sol", False)])
def test_domain_and_model_boundary(clean_env, host, model, translate):
    import openai
    hm = nt._openai_httpx_module()
    seen = []
    def handle(request):
        seen.append(request)
        return hm.Response(200, json=native() if translate else chat())
    with openai.OpenAI(api_key="fixture", base_url="https://" + host, max_retries=0,
                       http_client=hm.Client(transport=hm.MockTransport(handle))) as remote:
        nt.attach_copilot_client(remote)
        nt.attach_copilot_client(remote)  # same client and with_options share its http owner
        remote.with_options(timeout=17).chat.completions.create(
            model=model, messages=[dict(role="user", content="fixture")], reasoning_effort="max")
    assert len(seen) == 1
    assert seen[0].url.path == ("/responses" if translate else "/chat/completions")
    assert seen[0].extensions["timeout"]["read"] == 17
    body = json.loads(seen[0].content)
    if translate:
        assert body["reasoning"] == {"effort": "max"} and "reasoning_effort" not in body
    else:
        assert body["reasoning_effort"] == "max" and "reasoning" not in body


def test_redirect_auth_metadata_and_observation_failure(monkeypatch, clean_env):
    import openai
    hm = nt._openai_httpx_module()
    seen = []
    def broken(*a, **k):
        raise OSError("fixture observability unavailable")
    monkeypatch.setattr(obs, "http_projection", broken)
    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            return hm.Response(307, headers={"location": "/v2/responses"})
        return hm.Response(200, json=native(), extensions={"fixture": "retained"}, headers={"x-request-id": "native-id"})
    http = hm.Client(transport=hm.MockTransport(handle), follow_redirects=True)
    with openai.OpenAI(api_key="fixture", base_url=target()["base_url"], max_retries=0, http_client=http) as remote:
        nt.attach_copilot_client(remote)
        with obs.http_observation_scope():
            raw = http.send(http.build_request("POST", target()["base_url"] + "/chat/completions",
                json={"model": "gpt-6.1-sol", "messages": []}, headers={"authorization": "Bearer fixture"}))
            assert json.loads(raw.content)["choices"][0]["message"]["content"] == "OK"
            assert raw.request.url.path == "/v2/responses" and len(raw.history) == 1
            assert raw.extensions["fixture"] == "retained" and raw.headers["x-request-id"] == "native-id"
    assert len(seen) == 2 and all(r.headers["authorization"] == "Bearer fixture" for r in seen)


@pytest.mark.parametrize("projection", [None, {"attempt_id": "stale", "sdk_candidate_sha256": "wrong"}])
def test_missing_or_mismatched_http_evidence_never_teaches_chat(monkeypatch, projection):
    t = target()
    payload = dict(model=t["resolved_model"], messages=[dict(role="user", content="fixture")], reasoning_effort="minimal")
    capture = PhysicalAttemptCapture(attempt_id="attempt1", model=t["usage_model"], provider=t["provider"], state="settled",
        candidate_measurement_kind="canonical_json_v1", candidate_raw_sha256=physical_candidate_sha256(payload),
        candidate_manifest_ref={"path": "/private/fixture.json", "call_id": "attempt1", "sha256": "a" * 64})
    monkeypatch.setattr(recovery, "observe_wire_semantics", lambda **kw: pytest.fail("Responses must not teach Chat"))
    with recovery.request_wire_call_scope():
        recovery.prepare_wire_payload_for_send(t, payload, api_surface="chat.completions")
        recovery.note_wire_send_succeeded(capture)
        usage = {"http_projection": projection} if projection is not None else {}
        recovery.finalize_wire_response({"role": "assistant", "content": "OK"}, usage)
    fact = usage["request_wire"]
    assert fact["candidate_boundary"] == "sdk_input"
    assert fact["compatibility_learning"] == "not_performed_translated_surface"
    assert fact["observed_http_gap"] == "matching_observation_unavailable"


def test_provider_projection_cannot_impersonate_host():
    import openai
    forged = dict(attempt_id="known", sdk_candidate_sha256="known", api_surface="responses")
    body = chat()
    body.update(created=1, object="chat.completion")
    body["usage"].update(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    body.update(_ouroboros_http_projection=forged, _http_projection=forged)
    body["usage"].update(http_projection=forged, _http_projection=forged)
    sdk = openai.types.chat.ChatCompletion.model_validate(body)
    _, usage = LLMClient()._normalize_remote_response(sdk.model_dump(), target("provider.test"),
        skip_cost_fetch=True, wire_completion=sdk)
    assert "http_projection" not in usage and "_http_projection" not in usage
    # Host attachment is out of band and does not change model_dump/source bytes.
    before = copy.deepcopy(sdk.model_dump())
    with obs.http_observation_scope():
        obs._HTTP_OBSERVATIONS.get().append(forged)
        obs.bind_http_observations(sdk)
    assert sdk.model_dump() == before
    assert obs.response_http_projection(sdk) == forged


def test_concurrent_sender_observations_are_isolated(monkeypatch):
    monkeypatch.setattr(obs, "http_projection", lambda request: {"attempt_id": request})
    async def run():
        ready = asyncio.Event()
        async def one(identity):
            with obs.http_observation_scope():
                obs.note_http_projection(identity)
                ready.set()
                await ready.wait()
                await asyncio.sleep(0)
                reply = obs.bind_http_observations(SimpleNamespace())
                return obs.response_http_projection(reply)
        return await asyncio.gather(one("a"), one("b"))
    assert asyncio.run(run()) == [{"attempt_id": "a"}, {"attempt_id": "b"}]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("effort,native_effort", [
    ("minimal", "low"), ("low", "low"), ("medium", "medium"), ("high", "high"),
    ("xhigh", "xhigh"), ("max", "max"), ("ultra", "xhigh"), ("none", None)])
def test_accounted_consumer_keeps_sdk_and_http_boundaries(tmp_path, monkeypatch, clean_env, asynchronous, stream, effort, native_effort):
    import openai
    from ouroboros import usage_accounting as ua
    import ouroboros.request_wire_contract as wire
    import ouroboros.pricing as pricing
    from tests._usage_store_testing import ledger_rows
    # Restore the real retention function: all observations must be materialized.
    # clean_env patches the function, not its implementation reference.
    monkeypatch.undo()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(ua, "_reservation_cost", lambda request: 1.0)
    monkeypatch.setattr(pricing, "estimate_cost_optional", lambda *a, **k: None)
    monkeypatch.setattr(wire, "canonical_wire_evidence_root", lambda: tmp_path)
    monkeypatch.setattr(recovery, "observe_wire_semantics", lambda **kw: pytest.fail("translated acceptance teaches no Chat profile"))
    hm = nt._openai_httpx_module()
    sent = []
    def handler(request):
        sent.append(request)
        return hm.Response(200, json=native())
    client = LLMClient()
    t = target()
    payload = dict(model=t["resolved_model"], messages=[dict(role="user", content="fixture")],
                   reasoning_effort=effort, max_tokens=1024, stream=stream)
    scope = ua.UsageScope(drive_root=tmp_path, task_id="boundary-proof", root_task_id="boundary-proof")
    if asynchronous:
        async def run():
            http = hm.AsyncClient(transport=hm.MockTransport(handler))
            async with openai.AsyncOpenAI(api_key="fixture", base_url=t["base_url"], max_retries=0, http_client=http) as remote:
                nt.attach_copilot_client(remote)
                with ua.usage_scope(scope), recovery.request_wire_call_scope():
                    result = await client._create_chat_completion_with_retries_async(remote.chat.completions.create, payload, t)
                    return client._normalize_remote_response(result.model_dump(), t, skip_cost_fetch=True, wire_completion=result)
        message, usage = asyncio.run(run())
    else:
        http = hm.Client(transport=hm.MockTransport(handler))
        with openai.OpenAI(api_key="fixture", base_url=t["base_url"], max_retries=0, http_client=http) as remote:
            nt.attach_copilot_client(remote)
            with ua.usage_scope(scope), recovery.request_wire_call_scope():
                result = client._create_chat_completion_with_retries(remote.chat.completions.create, payload, t)
                message, usage = client._normalize_remote_response(result.model_dump(), t, skip_cost_fetch=True, wire_completion=result)
    assert message["content"] == "OK" and len(sent) == 1
    fact = usage["request_wire"]
    observed = fact["observed_http"]
    assert fact["candidate_boundary"] == "sdk_input"
    assert fact["applied_effort_source"] == "sdk_input"
    assert fact["reported_effort"] is None and fact["reported_effort_source"] is None
    assert fact["applied_effort"] == effort and observed["effort"] == native_effort
    assert fact["candidate_sha256"] != observed["payload_sha256"]
    assert observed["sdk_candidate_sha256"] == fact["candidate_sha256"]
    assert observed["attempt_id"] == fact["attempt_id"]
    assert observed["api_surface"] == "responses" and observed["delivery"] == "not_attested"
    assert fact["compatibility_learning"] == "not_performed_translated_surface"
    rows = ledger_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["state"] == "settled"
    # Open the actual private manifests and content-addressed values.
    manifests = list((tmp_path / "observability" / "calls" / "boundary-proof").glob("*_http_*.json"))
    assert len(manifests) == 2
    retained = []
    for path in manifests:
        manifest = json.loads(path.read_text())
        assert manifest["attempt_id"] == fact["attempt_id"]
        assert manifest["full_payload_custody"] == "private_unredacted_cas"
        ref = manifest["full_payload_ref"]
        from ouroboros.observability import read_blob_ref
        retained.append(read_blob_ref(tmp_path, ref))
    assert {row["kind"] for row in retained} == {"request_hook", "native_response"}
    outgoing = next(row for row in retained if row["kind"] == "request_hook")
    incoming = next(row for row in retained if row["kind"] == "native_response")
    assert outgoing["request_body"] == json.loads(sent[0].content)
    assert json.loads(incoming["decoded_body"]) == native()
    assert all("headers" not in row for row in retained)


@pytest.mark.parametrize("blocked_kind", ["request_hook", "native_response"])
@pytest.mark.parametrize("cancel", [False, True])
def test_async_persistence_is_offloop_and_joined(monkeypatch, blocked_kind, cancel):
    import threading
    import openai
    from ouroboros import usage_accounting as ua
    hm = nt._openai_httpx_module()
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    seen = []
    capture = PhysicalAttemptCapture(attempt_id="joined-owner", model="fixture", provider="openai-compatible", state="dispatched", candidate_measurement_kind="canonical_json_v1")
    def retain(kind, request, **kwargs):
        assert ua.last_physical_attempt_capture().attempt_id == "joined-owner"
        if kind == blocked_kind:
            entered.set()
            assert release.wait(5), "test controller did not release persistence"
            completed.set()
        return {"manifest_ref": {"path": "fixture"}}
    monkeypatch.setattr(obs, "retain_http_observation", retain)
    def handler(request):
        seen.append(request)
        return hm.Response(200, json=native())
    async def run():
        ua.adopt_physical_attempt_capture(capture)
        http = hm.AsyncClient(transport=hm.MockTransport(handler))
        async with openai.AsyncOpenAI(api_key="fixture", base_url=target()["base_url"], max_retries=0, http_client=http) as remote:
            nt.attach_copilot_client(remote)
            task = asyncio.create_task(remote.chat.completions.create(model="gpt-6.1-sol", messages=[]))
            try:
                # This loop must run while the persistence worker is blocked.
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await asyncio.sleep(.001)
                if cancel:
                    task.cancel()
                    await asyncio.sleep(.01)
                    assert not task.done(), "cancellation abandoned an owned writer"
                assert not completed.is_set()
                release.set()
                if cancel:
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    assert (await task).choices[0].message.content == "OK"
                assert completed.is_set()
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
    asyncio.run(run())
    assert len(seen) == (0 if cancel and blocked_kind == "request_hook" else 1)
