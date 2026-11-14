"""SUSPICIOUS outgoing speech is sent with its warning; only DANGEROUS speech is held (owner decision)."""
from types import SimpleNamespace

from ouroboros import safety


def _ctx(tmp_path):
    return SimpleNamespace(task_id="t1", root_task_id="r-speech", task_metadata={}, drive_root=tmp_path,
                           budget_drive_root=tmp_path, task_contract={}, messages=[], event_queue=None)


def _assess(status):
    return safety._Assessment(status != "DANGEROUS", "⚠️ verdict " + status, status=status,
                              verdicts=[{"stage": "first", "status": status, "reason": "test"}])


def _check(tmp_path, monkeypatch, status):
    monkeypatch.setattr(safety, "get_safety_mode", lambda *_a, **_k: "full")
    monkeypatch.setattr(safety, "_run_llm_check", lambda *a, **k: _assess(status))
    ctx = _ctx(tmp_path)
    try:
        return safety.check_safety("place_reply", {"destination": "d", "text": "hi"}, None, ctx)
    finally:
        safety.clear_safety_work("r-speech")


def test_suspicious_speech_sends_with_warning(tmp_path, monkeypatch):
    allowed, message = _check(tmp_path, monkeypatch, "SUSPICIOUS")
    assert allowed is True and "SUSPICIOUS" in message


def test_dangerous_speech_is_still_held(tmp_path, monkeypatch):
    allowed, message = _check(tmp_path, monkeypatch, "DANGEROUS")
    assert allowed is False and "Message not sent" in message
