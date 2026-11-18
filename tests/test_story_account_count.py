"""The story capture counts stored account records without interpreting every account a second time.

``store.accounts()`` yields one interpreted entry per stored account record (rejected drafts included), so the
count read from the raw records must equal it in every state the common story can be in.
"""
from __future__ import annotations

from ouroboros.chronicle_store import ChronicleStore
from tests import _memory_inventory_shared as shared
from tests.test_common_story import G2_TEXT, G_TEXT, _rooms, _snapshot, _story


def _counts(root):
    blocks = _snapshot(root).legacy_blocks
    return blocks["accounts"], blocks["accounts_shown"], len(ChronicleStore(root).accounts())


def test_account_count_matches_interpreted_count_in_every_state(tmp_path):
    rooms, alpha, beta, a1, b1, h1 = _rooms(tmp_path)
    store = ChronicleStore(tmp_path)
    assert _counts(tmp_path) == (0, 0, 0)
    g1 = store.publish_account(room_id="1", text=G_TEXT, sources=[a1, b1], author=shared.MIND).record["id"]
    assert _counts(tmp_path) == (1, 0, 1)
    assert store.select_account(g1, replaces=[a1], author=shared.MIND, reason="shown").ok
    assert _counts(tmp_path) == (1, 1, 1)
    store.correct(a1, "I counted twice, not once.", shared.MIND, expected_sequence=store.room_head(alpha))
    assert _counts(tmp_path) == (1, 1, 1)
    g2 = store.publish_account(room_id="1", text=G2_TEXT, sources=[a1, b1], author=shared.MIND).record["id"]
    assert store.select_account(g2, replaces=[g1], author=shared.MIND, reason="nested").ok
    assert store.select_account(g2, replaces=[g1], shown=False, author=shared.MIND, reason="withdrawn").ok
    count, shown, interpreted = _counts(tmp_path)
    assert count == interpreted == 2 and shown <= count


def test_story_capture_does_not_interpret_accounts_a_second_time(tmp_path, monkeypatch):
    rooms, alpha, beta, a1, b1, h1 = _rooms(tmp_path)
    store = ChronicleStore(tmp_path)
    store.publish_account(room_id="1", text=G_TEXT, sources=[a1, b1], author=shared.MIND)
    calls = []
    original = ChronicleStore.accounts

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(ChronicleStore, "accounts", counting)
    story = _story(tmp_path)
    assert len(calls) == 1  # the story path (account_entries) only; the count no longer interprets again
    assert "My accounts across rooms: 1 written" in story
