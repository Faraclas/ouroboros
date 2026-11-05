"""Copilot option intent through real SDK/public paths; all HTTP is mocked.

Temperature omission is an explicitly OPEN compatibility concern, not proof
that the service rejects or ignores a supplied value. Only description is fixed.
"""
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
from ouroboros import request_wire_contract as wire, request_wire_recovery as recovery
from ouroboros import usage_accounting as ua
from ouroboros.llm import LLMClient
from ouroboros.observability import read_blob_ref


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}
DESCRIPTION = "Return the stated decision — not its opposite."


def schema_format(description):
    value = {"type": "json_schema", "json_schema": {
        "name": "fixture", "schema": copy.deepcopy(SCHEMA), "strict": False}}
    if description is not None:
        value["json_schema"]["description"] = description
    return value


@pytest.mark.parametrize("description", [None, "", DESCRIPTION])
def test_codec_preserves_optional_schema_description_without_mutating_input(description):
    payload = {"model": "gpt-6.1-sol", "messages": [],
               "response_format": schema_format(description)}
    before = copy.deepcopy(payload)
    expected = {"type": "json_schema", "name": "fixture", "schema": SCHEMA, "strict": False}
    if description is not None:
        expected["description"] = description
    assert codec._copilot_chat_to_responses_payload(payload)["text"]["format"] == expected
    assert payload == before


@pytest.fixture
def option_wire(monkeypatch, tmp_path):
    """Real SDK/codec/accounting; synthetic routing/pricing and mocked HTTP."""
    source = Path(__file__).resolve().parents[1] / "ouroboros"
    assert Path(codec.__file__).resolve().parent == source
    assert Path(inspect.getfile(LLMClient)).resolve().parent == source
    target = dict(provider="openai-compatible", resolved_model="gpt-6.1-sol",
                  usage_model="openai-compatible/gpt-6.1-sol", api_key="fixture",
                  base_url="https://api.business.githubcopilot.com",
                  supports_openrouter_extensions=False)
    hm = nt._openai_httpx_module()
    sent, state = [], {"error": None}
    def no_network(*args, **kwargs):
        pytest.fail("unexpected real socket in offline option-fidelity test")
    def no_learning(**kwargs):
        pytest.fail("translated Responses must not teach Chat compatibility")
    def handle(self, request):
        sent.append({"path": request.url.path, "body": json.loads(request.read())})
        if state["error"]:
            return hm.Response(400, json={"error": state["error"]})
        return hm.Response(200, json=dict(
            id="format-fixture", model=target["resolved_model"], status="completed",
            output=[dict(type="message", role="assistant", status="completed",
                         content=[dict(type="output_text", text='{"ok":true}')])],
            usage=dict(input_tokens=12, output_tokens=4, total_tokens=16)))
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
    root = Path(config.DATA_DIR)
    task_id = "option-fidelity-" + uuid.uuid4().hex
    scope = ua.UsageScope(drive_root=root, task_id=task_id,
                          root_task_id="offline-option-fidelity", category="verification",
                          source="offline_mock_fixture", non_task_operation=True)

    def invoke(*, asynchronous=False, stream=False, no_proxy=False, **options):
        client = LLMClient()
        call = dict(messages=[dict(role="user", content="Offline option fixture")],
                    model="openai-compatible::gpt-6.1-sol", reasoning_effort="high",
                    max_tokens=1024, stream=stream, no_proxy=no_proxy,
                    wait_for_resources=False, **options)
        with ua.usage_scope(scope), recovery.request_wire_call_scope():
            if asynchronous:
                async def run():
                    try:
                        if "response_format" not in options:
                            return await client.chat_async(**call)
                        # chat_async has no response_format argument. Exercise its
                        # existing shared builder/SDK lane without extending that API.
                        kwargs = client._build_remote_kwargs(
                            target, call["messages"], "high", 1024, "auto", None, None,
                            response_format=options["response_format"], stream=stream)
                        remote = client._get_async_remote_client(target)
                        result = await client._create_chat_completion_with_retries_async(
                            remote.chat.completions.create, kwargs, target)
                        return client._normalize_remote_response(
                            result.model_dump(), target, skip_cost_fetch=True, wire_completion=result)
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


def assert_private_http(usage, root, task_id, expected):
    fact = usage["request_wire"]
    observed = fact["observed_http"]
    assert fact["candidate_boundary"] == "sdk_input"
    assert fact["compatibility_learning"] == "not_performed_translated_surface"
    assert observed["api_surface"] == "responses" and observed["delivery"] == "not_attested"
    assert observed["sdk_candidate_sha256"] == fact["candidate_sha256"]
    assert observed["attempt_id"] == fact["attempt_id"]
    assert fact["reported_effort"] is None
    manifests = list((root / "observability" / "calls" / task_id).glob("*_http_*.json"))
    assert len(manifests) == 2
    retained = [read_blob_ref(root, json.loads(path.read_text())["full_payload_ref"]) for path in manifests]
    assert {item["kind"] for item in retained} == {"request_hook", "native_response"}
    assert next(item["request_body"] for item in retained if item["kind"] == "request_hook") == expected
    assert all("headers" not in item for item in retained)


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sdk-stream"])
@pytest.mark.parametrize("no_proxy", [False, True], ids=["cached", "no-proxy"])
@pytest.mark.parametrize("description", [None, "", DESCRIPTION])
def test_public_schema_description_reaches_http_and_private_custody(option_wire, stream, no_proxy, description):
    invoke, sent, _, root, task_id = option_wire
    original = schema_format(description)
    before = copy.deepcopy(original)
    message, usage = invoke(response_format=original, stream=stream, no_proxy=no_proxy)
    assert json.loads(message["content"]) == {"ok": True}
    assert len(sent) == 1 and original == before and sent[0]["path"] == "/responses"
    assert sent[0]["body"]["text"]["format"] == {"type": "json_schema", **before["json_schema"]}
    assert_private_http(usage, root, task_id, sent[0]["body"])


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("description", [None, "", DESCRIPTION])
def test_async_sdk_builder_preserves_description(option_wire, stream, description):
    invoke, sent, _, root, task_id = option_wire
    message, usage = invoke(asynchronous=True, response_format=schema_format(description), stream=stream)
    assert json.loads(message["content"]) == {"ok": True} and len(sent) == 1
    assert sent[0]["body"]["text"]["format"] == {"type": "json_schema", **schema_format(description)["json_schema"]}
    assert_private_http(usage, root, task_id, sent[0]["body"])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("options", [{}, {"default_temperature": .2}, {"temperature": 0},
                                    {"temperature": .7, "default_temperature": .2}])
def test_characterizes_current_temperature_omission(option_wire, asynchronous, stream, options):
    """Present behaviour only; revise with an evidence-backed sampling repair."""
    invoke, sent, _, root, task_id = option_wire
    message, usage = invoke(asynchronous=asynchronous, stream=stream, **options)
    assert json.loads(message["content"]) == {"ok": True} and len(sent) == 1
    assert "temperature" not in sent[0]["body"]
    assert_private_http(usage, root, task_id, sent[0]["body"])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_nested_description_rejection_is_not_silently_removed(option_wire, asynchronous, stream):
    """Hypothetical native rejection grammar, not measured provider behaviour."""
    invoke, sent, state, _, _ = option_wire
    state["error"] = dict(type="invalid_request_error", code="unsupported_parameter",
                          param="text.format.description", message="Unsupported parameter: text.format.description")
    with pytest.raises(openai.BadRequestError) as error:
        invoke(asynchronous=asynchronous, stream=stream, response_format=schema_format(DESCRIPTION))
    assert error.value.status_code == 400
    assert len(sent) == 1 and sent[0]["body"]["text"]["format"]["description"] == DESCRIPTION


@pytest.mark.parametrize("host,model", [("https://provider.test", "gpt-6.1-sol"),
                                        ("https://api.business.githubcopilot.com", "claude-opus-5.5")])
def test_nontranslated_requests_keep_canonical_schema(host, model):
    hm = nt._openai_httpx_module()
    payload = dict(model=model, messages=[], temperature=0, response_format=schema_format(DESCRIPTION))
    request = hm.Request("POST", host + "/chat/completions", json=payload)
    assert codec._prepare_request(request, hm) is None
    assert json.loads(request.content) == payload


def test_json_object_format_stays_unchanged():
    body = codec._copilot_chat_to_responses_payload({"response_format": {"type": "json_object"}})
    assert body["text"] == {"format": {"type": "json_object"}}
