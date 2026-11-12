"""Structured room masking must never consume JSONL record delimiters."""
from hashlib import sha256
import json

import pytest

from ouroboros import dialogue_store
from ouroboros.dialogue_evidence import read_room_source


@pytest.mark.parametrize("text", [
    'https://example.invalid/?token=synthetic123',
    'https://example.invalid/?token=synthetic123&mode=read',
    'café "quoted" \\path\nSecond line\u2028next\u2029paragraph\u0085end',
    'export CUSTOM_PASSWORD=synthetic123',
    'token=synthetic123"',
    'Ordinary first line\nexport CUSTOM_PASSWORD=synthetic123\nKeep this line',
    'password="synthetic123"',
])
def test_room_masks_values_before_jsonl_framing(tmp_path, text):
    original = {"direction": "in", "chat_id": 1, "text": text,
                "client_message_id": "row-one", "ts": "2026-09-01T10:00:00Z"}
    dialogue_store.append(tmp_path, "chat", original)
    dialogue_store.append(tmp_path, "chat", {"direction": "out", "chat_id": 1,
        "text": 'Keep "every" \\word\n雪', "ts": "2026-09-01T10:01:00Z"})
    source = read_room_source(tmp_path, 1)
    records = [json.loads(line) for line in source["text"].split("\n")[:-1]]
    assert records[1:] == source["rows"] and len(records) == 3
    assert records[0]["rows"] == len(source["rows"]) == 2
    assert source["rows"][0]["client_message_id"] == "row-one"
    assert [r["author"] for r in source["rows"]] == ["User", "Ouroboros"]
    assert [r["source_ordinal"] for r in source["rows"]] == [1, 2]
    assert source["rows"][1]["text"] == 'Keep "every" \\word\n雪'
    assert source["bytes"] == len(source["text"].encode("utf-8"))
    assert source["sha256"] == sha256(source["text"].encode("utf-8")).hexdigest()
    assert source["secrets_redacted"] == ("synthetic123" in text)
    assert "synthetic123" not in source["text"]
    if "synthetic123" not in text:
        assert source["rows"][0]["text"] == text
    assert records[0]["coverage"]["chat"]["gaps"] == source["coverage"]["chat"]["gaps"]


def test_short_and_default_string_values_are_masked_before_serialization(tmp_path, monkeypatch):
    from ouroboros.memory import Memory

    class TextValue:
        def __str__(self):
            return "https://example.invalid/?token=synthetic123"

    dialogue_store.append(tmp_path, "chat", {"direction": "in", "chat_id": 1,
        "text": "Keep the source"})
    original = Memory.read_chat_generations

    def metadata(memory, **kwargs):
        rows, coverage = original(memory, **kwargs)
        return [{**r, "actions": {"url": TextValue(), "password": "abc"}} for r in rows], coverage

    monkeypatch.setattr(Memory, "read_chat_generations", metadata)
    source = read_room_source(tmp_path, 1)
    assert "synthetic123" not in source["text"]
    assert source["rows"][0]["actions"]["password"] != "abc"
    assert source["rows"][0]["text"] == "Keep the source"
    assert json.loads(source["text"].split("\n")[1]) == source["rows"][0]


def test_nested_secret_values_keep_metadata_and_original_store(tmp_path):
    metadata = {"password": 'short"\\secret', "nested": [
        {"authorization": "Bearer synthetic-auth", "keep": 'café "yes" \\path'},
        {"url": "https://example.invalid/?api_key=synthetic-query"}],
        "count": 0, "enabled": False, "empty": None}
    dialogue_store.append(tmp_path, "chat", {"direction": "in", "chat_id": 1,
        "text": "Keep the whole row", "actions": metadata})
    source = read_room_source(tmp_path, 1)
    safe = source["rows"][0]["actions"]
    assert safe["password"] != metadata["password"]
    assert safe["nested"][0]["authorization"] != metadata["nested"][0]["authorization"]
    assert safe["nested"][0]["keep"] == metadata["nested"][0]["keep"]
    assert {k: safe[k] for k in ("count", "enabled", "empty")} == {k: metadata[k] for k in ("count", "enabled", "empty")}
    assert "synthetic-query" not in source["text"] and "synthetic-auth" not in source["text"]
    assert source["secrets_redacted"]
    stored = [entry.row for entry in dialogue_store.iter_rows(tmp_path, "chat")]
    # The projection does not rewrite the canonical conversation.
    assert stored[0]["actions"] == metadata


@pytest.mark.parametrize("selector", ["", "::lines=2-2"])
def test_chat_attachment_keeps_already_masked_jsonl(tmp_path, selector):
    from ouroboros.dialogue_evidence import chat_evidence_reader
    from ouroboros.tools.plan_evidence import resolve_evidence

    dialogue_store.append(tmp_path, "chat", {"direction": "in", "chat_id": 1,
        "text": 'https://example.invalid/?token=synthetic123'})
    source = read_room_source(tmp_path, 1)
    manifest = resolve_evidence(["chat:1" + selector], active_root=tmp_path,
        allowed_roots=[], resolve_chat=chat_evidence_reader(tmp_path))
    [attached] = manifest["attached"]
    expected = source["text"].split("\n")[1] + "\n" if selector else source["text"]
    assert attached["text"] == expected
    assert attached["secrets_redacted"]
    assert attached["attached_bytes"] == len(expected.encode())
    for line in expected.split("\n"):
        if line:
            json.loads(line)
