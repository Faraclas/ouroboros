"""Owner pixels stay native through Safety, not encoded into its text budget."""
import base64
import copy
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from ouroboros import safety
from ouroboros.artifacts import stage_task_attachments
from ouroboros.context import build_user_content
from ouroboros.loop_messages import _initialize_owner_directives, _record_owner_directive
from tests._safety_fixtures import supervisor

_REAL_CALL = safety._safety_model_call


def corpus(tmp_path, ctx):
    # Valid, uncompressed pixels beyond the historical 512k TEXT gate.
    output = io.BytesIO()
    Image.new("RGB", (700, 500), (10, 90, 130)).save(output, format="PNG", compress_level=0)
    path = tmp_path / "owner.png"
    path.write_bytes(output.getvalue())
    manifest = stage_task_attachments(tmp_path, "work", [{"path": str(path)}])
    content = build_user_content({"id": "work", "drive_root": tmp_path,
                                  "text": "Ask both works for status; never stop their tests.",
                                  "attachment_images": manifest})
    _initialize_owner_directives(ctx, [{"role": "user", "content": content}])
    return content


@pytest.mark.parametrize("stage", ["first", "repair", "second"])
@pytest.mark.parametrize("producer", ["initial", "direct", "mailbox", "fallback"])
def test_native_pixels_reach_every_actual_checker_stage(supervisor, monkeypatch, tmp_path, stage, producer):
    from ouroboros import llm_observability as lo, vision_routing as vr
    h = supervisor
    content = corpus(tmp_path, h.ctx)
    if producer in {"direct", "mailbox"}:
        h.ctx._owner_directives = []
        _record_owner_directive(h.ctx, source=producer, content=content, msg_id="owner-image",
                               origin={"quiz_id": "q", "source_task_id": "origin"})
    elif producer == "fallback":
        del h.ctx._owner_directives
    original = copy.deepcopy(getattr(h.ctx, "_owner_directives", None))
    monkeypatch.setattr(h.module, "_safety_model_call", _REAL_CALL)
    monkeypatch.setattr(vr, "own_lane_without_images", lambda *a, **k: "")
    sent = []
    responses = (["not json", '{"status":"SAFE","reason":"checked"}'] if stage == "repair"
                 else ['{"status":"SUSPICIOUS","reason":"verify invitation"}',
                       '{"status":"SAFE","reason":"invitation and pixels checked"}'] if stage == "second"
                 else ['{"status":"SAFE","reason":"checked"}'])
    if stage == "second":
        h.values["OUROBOROS_SUBAGENTS"] = {"enabled": True, "items": [{
            "subagent_id": "second", "recommended_use": "Check flags", "enabled": True, "rechecks_flags": True, "delivery": "packet",
            "route": {"kind": "api_model", "target_id": "test/second"}}]}
    def physical(client, **kwargs):
        sent.append(kwargs)
        return {"content": responses.pop(0)}, {}
    monkeypatch.setattr(lo, "chat_observed", physical)
    ok, detail = safety.check_safety("schedule_followup", {"task_id": "peer", "note": "report status"},
                                   [{"role": "user", "content": content}], h.ctx)
    assert ok, detail
    assert len(sent) == (1 if stage == "first" else 2)
    for send in sent:
        blocks = send["messages"][1]["content"]
        assert len(blocks) == 2 and blocks[1]["type"] == "image_url"
        assert "base64," not in blocks[0]["text"]
        assert "never stop their tests" in blocks[0]["text"]
        assert len(blocks[0]["text"].encode()) < 512000
        assert len(base64.b64decode(blocks[1]["image_url"]["url"].split(",", 1)[1])) > 512000
        assert send["model_role"] in {"safety", "subagent:second"}
    assert getattr(h.ctx, "_owner_directives", None) == original


def test_image_only_and_native_source_projection_preserve_pixels(supervisor):
    from ouroboros.review_evidence_sections import _owner_content_projection
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "Ynl0ZXM="}}
    parts = []
    assert "image ref" in _owner_content_projection([block], image_parts=parts)
    assert parts == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,Ynl0ZXM="}}]
    assert _owner_content_projection([{"type": "unknown", "instruction": "retain"}]) == json.dumps(
        {"type": "unknown", "instruction": "retain"})


def test_corpus_identity_is_path_independent_and_unsupported_pixels_stay_explicit():
    from ouroboros.review_evidence_sections import _owner_content_projection, project_owner_corpus_row, materialize_owner_corpus_content
    image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,Ynl0ZXM='}}
    assert _owner_content_projection([{**image, '_source_path': '/one/image.png'}]) == _owner_content_projection([
        {**image, '_source_path': '/other/image.png'}])
    projected = project_owner_corpus_row({'source': 'owner_mailbox', 'content': [
        {'type': 'text', 'text': 'Keep the actual owner words'},
        {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.invalid/image'}}]})
    assert 'Keep the actual owner words' in projected['content'] and projected['media'][0]['unavailable']
    with pytest.raises(ValueError, match='image evidence unavailable'):
        materialize_owner_corpus_content(projected)


def test_missing_pixels_cannot_approve_even_local_fallback(supervisor, monkeypatch, tmp_path):
    from ouroboros import vision_routing as vr, llm_observability as lo
    h = supervisor
    corpus(tmp_path, h.ctx)
    monkeypatch.setattr(h.module, "_safety_model_call", _REAL_CALL)
    monkeypatch.setattr(h.module, "_resolve_safety_routing", lambda: (True, True, None))
    monkeypatch.setattr(vr, "prepare_messages_for_send", lambda *a, **k: [{"content": [{"type": "text", "text": "no pixels"}]}])
    monkeypatch.setattr(lo, "chat_observed", lambda *a, **k: pytest.fail("must not send an incomplete check"))
    ok, detail = safety.check_safety("schedule_followup", {"note": "hello"}, ctx=h.ctx)
    assert not ok and "image evidence" in detail


@pytest.mark.parametrize("source", ["text", "arguments", "prior", "repair"])
def test_real_text_limits_and_last_directive_still_bind(supervisor, monkeypatch, tmp_path, source):
    h = supervisor
    corpus(tmp_path, h.ctx)
    _record_owner_directive(h.ctx, source="owner_mailbox", content="DO NOT SEND ANYTHING", msg_id="latest")
    args = {"note": "hello"}
    if source == "text":
        _record_owner_directive(h.ctx, source="owner_mailbox", content="X" * 512001)
    elif source == "arguments":
        args["note"] = "X" * 250001
    elif source == "prior":
        safety._PRIOR_VERDICTS["work"] = {"x": {"reason": "X" * 512001}}
    else:
        prompt = safety._build_check_prompt("schedule_followup", args, ctx=h.ctx, image_parts=[])
        monkeypatch.setattr(safety, "_SAFETY_BUNDLE_BYTE_BUDGET", len(prompt.encode()) + 10)
        h.replies.append("not json")
    ok, detail = safety.check_safety("schedule_followup", args, ctx=h.ctx)
    assert not ok and "TOO_LARGE" in detail
    assert len(h.calls) == (1 if source == "repair" else 0)
    if h.calls:
        assert "DO NOT SEND ANYTHING" in h.calls[0]["user_prompt"]


def test_checker_route_reprepare_rejects_lost_images(supervisor, monkeypatch, tmp_path):
    from contextlib import contextmanager
    from ouroboros import model_wait, llm_observability as lo, vision_routing as vr
    h = supervisor
    corpus(tmp_path, h.ctx)
    monkeypatch.setattr(safety, "_safety_model_call", _REAL_CALL)
    callbacks = {}
    @contextmanager
    def registered(role, callback):
        callbacks[role] = callback
        try:
            yield
        finally:
            callbacks.pop(role)
    monkeypatch.setattr(model_wait, "current_model_wait", lambda: SimpleNamespace(register_reprepare=registered))
    monkeypatch.setattr(vr, "own_lane_without_images", lambda *a, **k: "")
    real_prepare = vr.prepare_messages_for_send
    def prepare(messages, *, routing, purpose):
        if routing.use_local:
            return [{"role": "user", "content": [{"type": "text", "text": "local has no pixels"}]}]
        return real_prepare(messages, routing=routing, purpose=purpose)
    monkeypatch.setattr(vr, "prepare_messages_for_send", prepare)
    def switched(client, **kwargs):
        # The existing waiter changed the actual receiving route after initial preparation.
        callbacks["safety"]({**kwargs, "model": "local", "use_local": True})
        pytest.fail("a local text-only SAFE must never be accepted")
    monkeypatch.setattr(lo, "chat_observed", switched)
    ok, detail = safety.check_safety("schedule_followup", {"note": "report"}, ctx=h.ctx)
    assert not ok and "IMAGE_EVIDENCE_UNAVAILABLE" in detail
    assert not callbacks


def test_native_source_survives_actual_anthropic_wire(monkeypatch):
    from ouroboros.llm import LLMClient
    from ouroboros.review_evidence_sections import _owner_content_projection
    raw = io.BytesIO()
    Image.new("RGB", (5, 5)).save(raw, format="PNG")
    parts = []
    _owner_content_projection([{"type": "image", "source": {"type": "base64", "media_type": "image/png",
        "data": base64.b64encode(raw.getvalue()).decode()}}], image_parts=parts)
    client = LLMClient(api_key="test-no-network")
    target = client._resolve_remote_target("anthropic::fixture")
    payload = client._build_remote_candidate(target, [{"role": "user", "content": parts}], "high", 128, "auto", None, None)
    assert payload["messages"][0]["content"][0]["type"] == "image"
    assert base64.b64decode(payload["messages"][0]["content"][0]["source"]["data"]) == raw.getvalue()


@pytest.mark.parametrize('tool', ['tell_task', 'forward_to_worker'])
def test_internal_mail_skips_only_the_model_gate(supervisor, monkeypatch, tmp_path, tool):
    h = supervisor
    corpus(tmp_path, h.ctx)
    h.ctx._owner_evidence_gaps = ['legacy capture unavailable']
    monkeypatch.setattr(safety, '_run_llm_check', lambda *a, **k: pytest.fail('internal mail needs no model authorization'))
    assert safety.check_safety(tool, {'task_id': 'peer'}, ctx=h.ctx) == (True, '')
    assert not h.calls
