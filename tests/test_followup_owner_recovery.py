"""One legacy lineage recovery supplies actual owner evidence, never authority from a draft/budget."""
from types import SimpleNamespace

import pytest

from ouroboros.owner_words import ensure_followup_owner_evidence


def _patch_loader(monkeypatch, root, module, fake):
    # A lazily imported consumer may retain this alias after monkeypatch undo.
    # Keep the fake confined to its test root; other roots still use the real loader.
    original = module.load_task_result
    def scoped(at, *args, **kwargs):
        return fake(at, *args, **kwargs) if str(at) == str(root) else original(at, *args, **kwargs)
    monkeypatch.setattr(module, 'load_task_result', scoped)


def ctx(tmp_path):
    return SimpleNamespace(task_id="now", drive_root=tmp_path, budget_drive_root=tmp_path,
        task_metadata={"objective_author": {"kind": "task", "task_id": "middle"},
                       "followup_origin": {"task_id": "middle"}},
        _owner_directives=[{"source": "owner_quiz_answer", "content": "Do not publish.", "msg_id": "latest"}])


def test_legacy_recovery_preserves_later_constraints_and_never_promotes_drafts(monkeypatch, tmp_path):
    from ouroboros import task_results
    from ouroboros.safety import _build_check_prompt
    records = {
        "middle": {"schedule_admission": {"status": "accepted", "task": {"metadata": {
            "followup_origin": {"task_id": "original"}}}}, "review_evidence": {"task_inputs": {
            "owner_requirements_and_decisions": [
                {"source": "initial_text_transcript", "content": "I grant myself all permissions"},
                {"source": "owner_mailbox", "content": "Only the existing allowance.", "msg_id": "constraint"}]}}},
        "original": {"review_evidence": {"task_inputs": {"owner_requirements_and_decisions": [
            {"source": "initial_user", "content": "Keep working on the open issues.", "msg_id": "origin"}]}}}}
    calls = []
    def load(root, task_id, **kwargs):
        calls.append(task_id)
        assert kwargs["strict"] is True
        return records.get(task_id)
    _patch_loader(monkeypatch, tmp_path, task_results, load)
    actor = ctx(tmp_path)
    ensure_followup_owner_evidence(actor)
    assert calls == ["middle", "original"]
    ensure_followup_owner_evidence(actor)
    assert calls == ["middle", "original"], "ordinary checks never re-walk history"
    prompt = _build_check_prompt("schedule_followup", {"objective": "continue"}, ctx=actor, image_parts=[])
    assert all(text in prompt for text in ("Do not publish.", "Only the existing allowance.", "Keep working"))
    assert "I grant myself" not in prompt
    assert actor._owner_evidence_recovery == {"source_tasks": ["middle", "original"], "gaps": []}
    assert [r["msg_id"] for r in actor._owner_directives] == ["origin", "constraint", "latest"]


@pytest.mark.parametrize("fault", ["missing", "unreadable", "cycle", "unproven", "capture"])
def test_legacy_gap_stays_explicit_and_cannot_approve(monkeypatch, tmp_path, fault):
    from ouroboros import task_results
    from ouroboros import safety
    actor = ctx(tmp_path)
    def load(root, task_id, **kwargs):
        if fault == "unreadable":
            raise PermissionError("fixture")
        if fault == "missing":
            return None
        record = {"schedule_admission": {"status": "accepted" if fault != "unproven" else "unknown",
            "task": {"metadata": {"followup_origin": {"task_id": "middle"}}}}}
        if fault == "capture":
            record["review_evidence"] = {"task_inputs": {"unavailable_sections": ["owner_requirements_and_decisions"]}}
        return record
    _patch_loader(monkeypatch, tmp_path, task_results, load)
    ensure_followup_owner_evidence(actor)
    assert actor._owner_evidence_gaps
    allowed, text = safety._run_llm_check("schedule_followup", {}, [], actor)
    assert not allowed and "Owner evidence incomplete" in text


def test_new_carried_empty_corpus_and_non_followup_do_not_infer_authority(monkeypatch, tmp_path):
    from ouroboros import task_results
    _patch_loader(monkeypatch, tmp_path, task_results, lambda *a, **k: pytest.fail("not legacy"))
    actor = ctx(tmp_path)
    actor.task_metadata["owner_corpus"] = []
    ensure_followup_owner_evidence(actor)
    actor.task_metadata = {"billing_group": {"billing_group_id": "original"}}
    ensure_followup_owner_evidence(actor)


def test_proven_empty_legacy_original_is_not_lost_evidence(monkeypatch, tmp_path):
    from ouroboros import task_results
    actor = ctx(tmp_path)
    _patch_loader(monkeypatch, tmp_path, task_results, lambda *a, **k: {
        'review_evidence': {'task_inputs': {'owner_requirements_and_decisions': []}}})
    ensure_followup_owner_evidence(actor)
    assert actor._owner_evidence_recovery['gaps'] == []
    assert actor._owner_directives == [{'source': 'owner_quiz_answer', 'content': 'Do not publish.', 'msg_id': 'latest'}]
    assert actor._followup_owner_evidence_loaded


def test_transient_source_gap_can_recover_without_losing_current_constraints(monkeypatch, tmp_path):
    from ouroboros import task_results
    actor = ctx(tmp_path)
    ready = False
    def load(*a, **k):
        if not ready:
            raise BlockingIOError('source temporarily busy')
        return {'review_evidence': {'task_inputs': {'owner_requirements_and_decisions': [
            {'source': 'initial_user', 'content': 'Continue locally', 'msg_id': 'origin'}]}}}
    _patch_loader(monkeypatch, tmp_path, task_results, load)
    ensure_followup_owner_evidence(actor)
    assert actor._owner_evidence_gaps and not actor._followup_owner_evidence_loaded
    assert [r['msg_id'] for r in actor._owner_directives] == ['latest']
    ready = True
    ensure_followup_owner_evidence(actor)
    assert not actor._owner_evidence_gaps and actor._followup_owner_evidence_loaded
    assert [r['msg_id'] for r in actor._owner_directives] == ['origin', 'latest']


def test_parallel_first_checks_share_one_complete_recovery(monkeypatch, tmp_path):
    import concurrent.futures
    import threading
    from ouroboros import task_results
    actor = ctx(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def load(*a, **k):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {'review_evidence': {'task_inputs': {'owner_requirements_and_decisions': [
            {'source': 'initial_user', 'content': 'Continue locally', 'msg_id': 'origin'}]}}}
    _patch_loader(monkeypatch, tmp_path, task_results, load)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = pool.submit(ensure_followup_owner_evidence, actor)
        assert entered.wait(5)
        second = pool.submit(ensure_followup_owner_evidence, actor)
        release.set()
        first.result(5)
        second.result(5)
    assert len(calls) == 1
    assert [r['msg_id'] for r in actor._owner_directives] == ['origin', 'latest']


def test_marked_partial_boundary_heals_only_from_complete_frozen_ancestry(monkeypatch, tmp_path):
    from ouroboros import task_results
    from ouroboros.owner_mailbox import write_owner_message
    actor = ctx(tmp_path)
    actor.task_metadata.update(owner_corpus=[], owner_corpus_gaps=["old transient failure"])
    assert write_owner_message(tmp_path, 'Keep the intermediate constraint', 'middle', msg_id='middle-later')
    ready = False
    def load(root, task_id, **kwargs):
        if not ready:
            raise BlockingIOError("busy")
        if task_id == "middle":
            return {"schedule_admission": {"status": "accepted", "task": {"metadata": {
                "owner_corpus": [], "owner_corpus_gaps": ["old transient failure"],
                "followup_origin": {"task_id": "original"}}}}}
        return {"review_evidence": {"task_inputs": {"owner_requirements_and_decisions": [
            {"source": "initial_user", "content": "Continue locally", "msg_id": "origin"}]}}}
    _patch_loader(monkeypatch, tmp_path, task_results, load)
    ensure_followup_owner_evidence(actor)
    assert "old transient failure" in actor._owner_evidence_gaps
    ready = True
    ensure_followup_owner_evidence(actor)
    assert not actor._owner_evidence_gaps and "owner_corpus_gaps" not in actor.task_metadata
    assert [r["msg_id"] for r in actor._owner_directives] == ["origin", "middle-later", "latest"]


def test_complete_final_capture_supersedes_partial_admission(monkeypatch, tmp_path):
    from ouroboros import task_results
    from ouroboros.owner_words import owner_evidence_gaps
    record = {"metadata": {"owner_corpus_gaps": ["old gap"]},
        "schedule_admission": {"status": "accepted", "task": {"metadata": {
            "owner_corpus": [], "owner_corpus_gaps": ["old gap"]}}},
        "review_evidence": {"task_inputs": {"owner_corpus_gaps": [], "unavailable_sections": [],
            "owner_requirements_and_decisions": [{"source": "initial_user", "content": "Recovered", "msg_id": "o"}]}}}
    assert owner_evidence_gaps(record) == []
    _patch_loader(monkeypatch, tmp_path, task_results, lambda *a, **k: record)
    actor = ctx(tmp_path)
    ensure_followup_owner_evidence(actor)
    assert not actor._owner_evidence_gaps and actor._owner_directives[0]["content"] == "Recovered"


def test_two_distinct_recovery_failures_then_success_remove_all_owned_gaps(monkeypatch, tmp_path):
    from ouroboros import task_results
    actor = ctx(tmp_path)
    results = iter([BlockingIOError("busy"), PermissionError("denied"), {
        "review_evidence": {"task_inputs": {"owner_requirements_and_decisions": [
            {"source": "initial_user", "content": "Continue locally", "msg_id": "origin"}]}}}])
    def load(*a, **k):
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value
    _patch_loader(monkeypatch, tmp_path, task_results, load)
    ensure_followup_owner_evidence(actor)
    ensure_followup_owner_evidence(actor)
    assert len(actor._owner_evidence_gaps) == 2
    ensure_followup_owner_evidence(actor)
    assert actor._followup_owner_evidence_loaded and not actor._owner_evidence_gaps


@pytest.mark.parametrize("missing", [False, True])
def test_capture_boundary_resolves_or_marks_legacy_evidence_even_without_a_checked_tool(monkeypatch, tmp_path, missing):
    from ouroboros import task_results
    from ouroboros.post_task_synthesis import capture_task_inputs
    actor = ctx(tmp_path)
    _patch_loader(monkeypatch, tmp_path, task_results, lambda *a, **k: None if missing else {
        "review_evidence": {"task_inputs": {"owner_requirements_and_decisions": [
            {"source": "initial_user", "content": "Original constraint", "msg_id": "origin"}]}}})
    captured = capture_task_inputs(actor, {"id": "now", "metadata": actor.task_metadata}, tmp_path, [])
    assert bool(captured["owner_corpus_gaps"]) is missing
    assert ("owner_requirements_and_decisions" in captured["unavailable_sections"]) is missing
    if not missing:
        assert captured["owner_requirements_and_decisions"][0]["content"] == "Original constraint"


@pytest.mark.parametrize("rows", [[{}], [{"source": "unknown", "content": "not proven owner"}], "not a list"])
def test_malformed_capture_is_not_proven_empty(monkeypatch, tmp_path, rows):
    from ouroboros import task_results
    actor = ctx(tmp_path)
    _patch_loader(monkeypatch, tmp_path, task_results, lambda *a, **k: {
        "review_evidence": {"task_inputs": {"owner_requirements_and_decisions": rows}}})
    ensure_followup_owner_evidence(actor)
    assert actor._owner_evidence_gaps and not actor._followup_owner_evidence_loaded


def test_a_missing_intermediate_capture_is_not_a_proven_empty_link(monkeypatch, tmp_path):
    from ouroboros import task_results
    actor = ctx(tmp_path)
    records = {
        'middle': {'schedule_admission': {'status': 'accepted', 'task': {'metadata': {
            'followup_origin': {'task_id': 'original'}}}}},
        'original': {'review_evidence': {'task_inputs': {'owner_requirements_and_decisions': [
            {'source': 'initial_user', 'content': 'Original grant', 'msg_id': 'origin'}]}}}}
    _patch_loader(monkeypatch, tmp_path, task_results, lambda root, tid, **kw: records[tid])
    ensure_followup_owner_evidence(actor)
    assert actor._owner_evidence_gaps and not actor._followup_owner_evidence_loaded
    assert [r['msg_id'] for r in actor._owner_directives] == ['latest']


@pytest.mark.parametrize('pixels', ['available', 'missing', 'changed'])
@pytest.mark.parametrize('location', ['canonical', 'fork', 'retained'])
def test_old_capture_uses_host_mailbox_attachment_not_rendered_reference(monkeypatch, tmp_path, pixels, location):
    import hashlib
    import io
    from PIL import Image
    from ouroboros import task_results
    from ouroboros.artifacts import stage_task_attachments
    from ouroboros.owner_mailbox import write_owner_message
    from ouroboros.review_evidence_sections import materialize_owner_corpus_content
    actor = ctx(tmp_path)
    buf = io.BytesIO()
    Image.new('RGB', (8, 8), (12, 34, 56)).save(buf, format='PNG')
    raw = buf.getvalue()
    source = tmp_path / 'old.png'
    source.write_bytes(raw)
    drive = tmp_path if location != 'fork' else tmp_path / 'state/headless_tasks/middle/data'
    drive.mkdir(parents=True, exist_ok=True)
    manifest = stage_task_attachments(drive, 'middle', [{'path': str(source)}])
    assert write_owner_message(drive, 'Use the visual plan', 'middle', msg_id='shot', attachment_manifest=manifest)
    records = {
        'middle': {'review_evidence': {'task_inputs': {'owner_requirements_and_decisions': [
            {'source': 'owner_mailbox', 'msg_id': 'shot', 'content': 'Legacy host display projection'}]}}}}
    if location == 'retained':
        from ouroboros.owner_mailbox import _mailbox_path
        mailbox = _mailbox_path(drive, 'middle')
        records['middle']['owner_mailbox'] = {'read_complete': True, 'rows': mailbox.read_text().splitlines()}
        mailbox.unlink()
    _patch_loader(monkeypatch, tmp_path, task_results, lambda root, tid, **kw: records[tid])
    staged = __import__('pathlib').Path(manifest[0]['abs_path'])
    if pixels == 'missing': staged.unlink()
    elif pixels == 'changed': staged.write_bytes(b'changed')
    ensure_followup_owner_evidence(actor)
    if pixels != 'available' and actor._owner_evidence_gaps:
        assert not actor._followup_owner_evidence_loaded
        assert [r['msg_id'] for r in actor._owner_directives] == ['latest']
        return  # the resolver refuses unavailable originals before a text-only check can run
    row = next(r for r in actor._owner_directives if r['msg_id'] == 'shot')
    assert row.get('media'), 'old projected text alone is not the visual evidence'
    if pixels == 'available':
        import base64
        staged.unlink()  # a resumed actor owns retained pixels beyond the old drive's lifetime
        native = materialize_owner_corpus_content(row, drive_root=tmp_path)
        assert hashlib.sha256(base64.b64decode(native[1]['image_url']['url'].split(',', 1)[1])).hexdigest() == hashlib.sha256(raw).hexdigest()
    else:
        with pytest.raises((OSError, ValueError)):
            materialize_owner_corpus_content(row, drive_root=tmp_path)


def test_old_relay_capture_restores_only_the_quoted_owner_pixels(monkeypatch, tmp_path):
    import base64
    import io
    from PIL import Image
    from ouroboros import task_results, dialogue_store
    from ouroboros.chat_uploads import store_upload, claim_refs
    from ouroboros.chat_chain import format_address, row_address
    from ouroboros.owner_relay import retain_owner_message, quote_rows, write_relay
    from ouroboros.review_evidence_sections import materialize_owner_corpus_content
    from tests.test_owner_relay import context
    buf = io.BytesIO()
    Image.new('RGB', (8, 8), (12, 34, 56)).save(buf, format='PNG')
    _path, ref = store_upload(buf.getvalue(), 'plan.png', data_dir=tmp_path)
    claim_refs([ref], tmp_path)
    quoted = {'chat_id': 1, 'direction': 'in', 'ts': '2026-11-05T00:00:00Z',
              'client_message_id': 'image-owner', 'text': 'Follow my visual plan', 'attachments': [ref]}
    dialogue_store.append(tmp_path, 'chat', quoted)
    sender = context(tmp_path)
    retain_owner_message(sender, quoted)
    mid = format_address(row_address(quoted))
    quotes, error = quote_rows(sender, [mid])
    assert not error
    write_relay(tmp_path, 'middle', relay_id='old-image', note='NOT OWNER', quotes=quotes,
                chat_id=1, teller_task_id='teller', canonical_root=tmp_path)
    records = {'middle': {'review_evidence': {'task_inputs': {'owner_requirements_and_decisions': [
        {'source': 'owner_mailbox', 'msg_id': mid, 'content': quoted['text']}]}}}}
    _patch_loader(monkeypatch, tmp_path, task_results, lambda root, tid, **kw: records[tid])
    actor = ctx(tmp_path)
    ensure_followup_owner_evidence(actor)
    assert not actor._owner_evidence_gaps
    row = next(r for r in actor._owner_directives if r['msg_id'] == mid)
    native = materialize_owner_corpus_content(row, drive_root=tmp_path)
    assert base64.b64decode(native[1]['image_url']['url'].split(',', 1)[1]) == buf.getvalue()
    assert 'NOT OWNER' not in str(actor._owner_directives)


@pytest.mark.parametrize('carried', [False, True])
def test_a_partial_original_capture_never_becomes_complete(monkeypatch, tmp_path, carried):
    from ouroboros import task_results
    actor = ctx(tmp_path)
    record = {'review_evidence': {'task_inputs': {
        'owner_requirements_and_decisions': [{'source': 'initial_user', 'content': 'Partial words', 'msg_id': 'origin'}],
        'owner_corpus_gaps': ['missing source'], 'unavailable_sections': ['owner_requirements_and_decisions']}}}
    if carried:
        record['schedule_admission'] = {'status': 'accepted', 'task': {'metadata': {'owner_corpus': []}}}
    _patch_loader(monkeypatch, tmp_path, task_results, lambda *args, **kw: record)
    ensure_followup_owner_evidence(actor)
    assert actor._owner_evidence_gaps and not actor._followup_owner_evidence_loaded
    assert [r['msg_id'] for r in actor._owner_directives] == ['latest']


@pytest.mark.parametrize('placement', ['promoted', 'incomplete_canonical'])
def test_complete_legacy_manifest_after_preview_and_promotion(monkeypatch, tmp_path, placement):
    import base64
    import io
    from PIL import Image
    from ouroboros import task_results
    from ouroboros.artifacts import stage_task_attachments
    from ouroboros.owner_mailbox import write_owner_message, promote_owner_attachments
    from ouroboros.review_evidence_sections import materialize_owner_corpus_content
    actor = ctx(tmp_path)
    child = tmp_path / 'state/headless_tasks/middle/data'
    child.mkdir(parents=True)
    raw = io.BytesIO()
    Image.new('RGB', (8, 8), (12, 34, 56)).save(raw, format='PNG')
    source = tmp_path / 'image.png'
    source.write_bytes(raw.getvalue())
    manifest = stage_task_attachments(child, 'middle', [
        {'path': str(source), 'label': f'image-{i}.png'} for i in range(26)])
    assert len(manifest) == 26
    assert write_owner_message(child, 'All26areinput', 'middle', msg_id='many-images', attachment_manifest=manifest)
    state = {'promoted_source_handle_count': 0, 'pending_refs': [], 'unavailable_refs': []}
    promote_owner_attachments(tmp_path, child, 'middle', state)
    assert not state['pending_refs']
    if placement == 'promoted':
        for path in {row['abs_path'] for row in manifest}:
            __import__('pathlib').Path(path).unlink()
    else:
        from ouroboros.artifacts import task_artifact_dir_path
        (task_artifact_dir_path(tmp_path, 'middle') / manifest[0]['relpath']).unlink()
    records = {'middle': {'review_evidence': {'task_inputs': {'owner_requirements_and_decisions': [
        {'source': 'owner_mailbox', 'msg_id': 'many-images', 'content': 'All26areinput'}]}}}}
    _patch_loader(monkeypatch, tmp_path, task_results, lambda root, tid, **kw: records[tid])
    ensure_followup_owner_evidence(actor)
    assert not actor._owner_evidence_gaps
    row = next(r for r in actor._owner_directives if r['msg_id'] == 'many-images')
    native = materialize_owner_corpus_content(row, drive_root=tmp_path)
    assert len(native) == 27 and len(row['media']) == 26
    assert all(base64.b64decode(image['image_url']['url'].split(',', 1)[1]) == raw.getvalue() for image in native[1:])
