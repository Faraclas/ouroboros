"""Public real-SDK max fidelity and synthetic rejection grammars; no network."""
import asyncio
import copy
import inspect
import json
from pathlib import Path
import socket
import uuid

import openai
import pytest

from ouroboros import config, copilot_responses as codec, net_transport as nt, pricing
from ouroboros import request_wire_contract as wire
from ouroboros import request_wire_recovery as recovery
from ouroboros import usage_accounting as ua
from ouroboros.llm import LLMClient
from ouroboros.observability import read_blob_ref


@pytest.fixture
def public_wire(monkeypatch, tmp_path):
    """Keep production constructors/accounting/codec, substituting only HTTP I/O."""
    source = Path(__file__).resolve().parents[1] / "ouroboros"
    assert Path(codec.__file__).resolve().parent == source
    assert Path(nt.__file__).resolve().parent == source
    assert Path(inspect.getfile(LLMClient)).resolve().parent == source
    target = dict(provider="openai-compatible", resolved_model="gpt-6.1-sol",
                  usage_model="openai-compatible/gpt-6.1-sol", api_key="offline-fixture",
                  base_url="https://api.business.githubcopilot.com",
                  supports_openrouter_extensions=False)
    hm = nt._openai_httpx_module()
    sent = []
    state = {"error": None}
    task_id = "copilot-effort-" + uuid.uuid4().hex
    root = Path(config.DATA_DIR)  # pytest's already-isolated, coherently rebound roots

    def no_network(*args, **kwargs):
        pytest.fail("unexpected socket outside the mocked SDK transport")

    def no_learning(**kwargs):
        pytest.fail("translated Responses must never teach Chat compatibility")

    def handle(self, request):
        body = json.loads(request.read())
        sent.append({"path": request.url.path, "body": body})
        if state["error"] and body.get("reasoning", {}).get("effort") == "max":
            return hm.Response(400, json={"error": state["error"]})
        return hm.Response(200, json=dict(
            id="effort-fixture", model=target["resolved_model"], status="completed",
            output=[dict(type="message", role="assistant", status="completed",
                         content=[dict(type="output_text", text="OK")])],
            usage=dict(input_tokens=12, output_tokens=1, total_tokens=13)))

    async def ahandle(self, request):
        return handle(self, request)

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(LLMClient, "_resolve_remote_target", lambda self, model: copy.deepcopy(target))
    monkeypatch.setattr(ua, "_reservation_cost", lambda request: 0.0)
    monkeypatch.setattr(pricing, "estimate_cost_optional", lambda *a, **k: None)
    monkeypatch.setattr(wire, "canonical_wire_evidence_root", lambda: tmp_path)
    monkeypatch.setattr(recovery, "observe_wire_semantics", no_learning)
    monkeypatch.setattr(hm.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(hm.AsyncHTTPTransport, "handle_async_request", ahandle)

    def invoke(asynchronous, stream):
        client = LLMClient()
        call = dict(messages=[dict(role="system", content="Fixture policy"),
                              dict(role="user", content="Offline effort fixture")],
                    model="openai-compatible::gpt-6.1-sol", reasoning_effort="max",
                    max_tokens=1024, stream=stream, wait_for_resources=False)
        scope = ua.UsageScope(drive_root=root, task_id=task_id,
                              root_task_id="offline-copilot-effort", category="verification",
                              source="offline_mock_fixture", non_task_operation=True)
        with ua.usage_scope(scope), recovery.request_wire_call_scope():
            if asynchronous:
                async def run():
                    try:
                        return await client.chat_async(**call)
                    finally:
                        for remote in client._async_remote_clients.values():
                            await remote.close()
                return asyncio.run(run())
            try:
                return client.chat(**call)
            finally:
                for remote in client._remote_clients.values():
                    remote.close()

    return invoke, sent, state, root, task_id


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sdk-stream"])
def test_public_max_reaches_native_http_and_private_custody(public_wire, asynchronous, stream):
    invoke, sent, _, root, task_id = public_wire
    message, usage = invoke(asynchronous, stream)
    assert message["content"] == "OK" and len(sent) == 1
    assert sent[0]["path"] == "/responses"
    assert sent[0]["body"]["reasoning"] == {"effort": "max"}
    fact = usage["request_wire"]
    observed = fact["observed_http"]
    assert fact["candidate_boundary"] == fact["applied_effort_source"] == "sdk_input"
    assert fact["requested_effort"] == fact["applied_effort"] == "max"
    assert fact["reported_effort"] is None and fact["reported_effort_source"] is None
    assert observed["api_surface"] == "responses" and observed["effort"] == "max"
    assert observed["attempt_id"] == fact["attempt_id"]
    assert observed["sdk_candidate_sha256"] == fact["candidate_sha256"]
    assert fact["compatibility_learning"] == "not_performed_translated_surface"
    assert observed["delivery"] == "not_attested"
    manifests = list((root / "observability" / "calls" / task_id).glob("*_http_*.json"))
    assert len(manifests) == 2
    retained = []
    for path in manifests:
        manifest = json.loads(path.read_text())
        assert manifest["attempt_id"] == fact["attempt_id"]
        assert manifest["full_payload_custody"] == "private_unredacted_cas"
        retained.append(read_blob_ref(root, manifest["full_payload_ref"]))
    outgoing = next(item for item in retained if item["kind"] == "request_hook")
    assert outgoing["request_body"] == sent[0]["body"]
    assert {item["kind"] for item in retained} == {"request_hook", "native_response"}
    assert all("headers" not in item for item in retained)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sdk-stream"])
@pytest.mark.parametrize("param,expected", [("reasoning.effort", ["max"]),
                                           ("reasoning_effort", ["max", "xhigh"])])
def test_public_max_rejection_uses_existing_grammar_without_learning(
    public_wire, asynchronous, stream, param, expected,
):
    """Hypothetical grammars, NOT observed Copilot rejection/service guarantees."""
    invoke, sent, state, _, _ = public_wire
    state["error"] = dict(type="invalid_request_error", code="invalid_value", param=param,
                          message="Unsupported value 'max'. Supported values are: "
                                  "'low', 'medium', 'high', 'xhigh'.")
    # Separate logical calls must start with max again; no translated acceptance
    # teaches a profile that silently narrows the next call.
    for _ in range(2):
        before = len(sent)
        if param == "reasoning.effort":
            with pytest.raises(openai.BadRequestError) as error:
                invoke(asynchronous, stream)
            assert error.value.status_code == 400
            assert error.value.body["param"] == param
        else:
            message, usage = invoke(asynchronous, stream)
            assert message["content"] == "OK"
            fact = usage["request_wire"]
            assert fact["reason_code"] == "provider_prescribed_value"
            assert fact["applied_effort"] == "xhigh"
            assert fact["applied_effort_source"] == "sdk_input"
            assert fact["observed_http"]["effort"] == "xhigh"
            assert fact["compatibility_learning"] == "not_performed_translated_surface"
        assert [item["body"]["reasoning"]["effort"] for item in sent[before:]] == expected
        assert all(item["path"] == "/responses" for item in sent[before:])
