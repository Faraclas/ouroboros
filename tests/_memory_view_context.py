"""A built request over the shared memory installation: governance, story and changing facts.

The installation is ``tests._memory_inventory_shared.world`` (Main, Projects alpha and beta,
a transport chat, the hidden partition, legacy memory covering the first ten stream rows)
inside the repo/drive pair of ``tests.test_cache_optimization``; ``blocks`` builds the task's
real request with ``context.build_llm_messages``, so the first capture activates the chronicle.
"""
from __future__ import annotations

import pathlib
import dataclasses
import json
import re
from typing import Any, Dict, Tuple

from tests import _memory_inventory_shared as shared


def world(tmp_path: pathlib.Path, **kwargs: Any) -> Tuple[Any, Any, Dict[str, int]]:
    """``(env, memory, rooms)`` with the shared installation on the drive, not yet activated."""
    from tests.test_cache_optimization import _make_env_and_memory

    env, memory = _make_env_and_memory(tmp_path)
    return env, memory, shared.world(memory.drive_root, activate=False, **kwargs)


def blocks(env: Any, memory: Any, task: Dict[str, Any], **kwargs: Any) -> Tuple[str, str, str, Dict[str, Any]]:
    """``(A', B, C, cap_info)``: common governance, identity/story, changing facts; omit optional D."""
    from ouroboros.context import build_llm_messages

    messages, cap = build_llm_messages(env=env, memory=memory, task={"type": "task", "text": "hi", **task}, **kwargs)
    first, *stable, third = (block["text"] for block in messages[0]["content"])
    second = "\n\n".join(text for text in stable if not text.startswith("## DEVELOPMENT.md\n"))
    return first, second, third, cap


def native_fixture_core(core, world_root):
    """Replace fixture-world prefixes in three rendered fields, not arbitrary authored text.

    Real env/disk objects, assignment, supplementary messages, memory-view JSON and
    schemas stay untouched. The world's parent is never replaced: the existing
    foreign-session isolation oracle must remain meaningful.
    """
    roots = {str(world_root), str(pathlib.Path(world_root).resolve())}
    roots |= {json.dumps(root)[1:-1] for root in roots}
    fields = {}
    for name in ('dynamic_head_text', 'dynamic_text', 'semi_stable_text'):
        text = getattr(core, name)
        for root in sorted(roots, key=len, reverse=True):
            text = re.sub(re.escape(root) + r'(?=[/\\"`]|$)', lambda _match: '/synthetic-native-root', text)
        fields[name] = text
    return dataclasses.replace(core, **fields)


def section(text: str, heading: str) -> str:
    """One ``## `` section of a block, from its heading to the next one."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end < 0 else text[start:end]


def room_text(root: pathlib.Path, task: Dict[str, Any] | None = None) -> str:
    """What the next turn of ``task`` (Main by default) reads of its room: the view's room block, no floor."""
    from ouroboros import memory_view as mv

    task = task or {"id": "next-turn", "chat_id": 1}
    snapshot = mv.capture_memory_view(root, task, mv.view_spec_for_task(task, root))
    return mv.render_room(snapshot)
