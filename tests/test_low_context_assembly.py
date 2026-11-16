"""Low residency and exact host-objective references, independent of provider shape."""

import copy
import hashlib

import pytest

from ouroboros.main_context_authority import project_main_task_authority
from ouroboros.tool_policy import (
    LOW_INITIAL_SCHEMA_NAMES, NANO_SCHEMA_META_NAMES, initial_tool_schemas,
    request_tool_schema_selection,
)
from tests.test_nano_tool_policy import Registry, _names
from types import SimpleNamespace


@pytest.mark.parametrize("prefix,suffix", [("", "\n"), ("", "\n\nExpected output: verified"),
                                           ("[WORK ORDER]\n", "\n[END]")])
def test_exact_objective_range_survives_assignment_framing(prefix, suffix):
    objective = "Keep all governing words. Ελληνικά 🐍\nDo the repair."
    task = {"text": prefix + objective + suffix,
            "task_contract": {"objective": objective},
            "origin_message_ref": {"chat_id": 1}, "origin_message_text": objective}
    before = copy.deepcopy(task)
    projection = project_main_task_authority(task)
    ref = projection["task_contract"]["objective"]
    start, end = ref["assignment_range"]
    assert task["text"][start:end] == objective
    assert ref["chars"] == len(objective)
    assert ref["sha256"] == hashlib.sha256(objective.encode()).hexdigest()
    assert projection["task_authority_origin"]["text"] == objective, "human source is not deduplicated"
    assert task == before


@pytest.mark.parametrize("objective", ["different", "", None, {"nested": "text"}, " task "])
def test_objective_without_exact_assignment_occurrence_is_kept(objective):
    assert project_main_task_authority({"text": "task", "task_contract": {"objective": objective}})[
        "task_contract"]["objective"] == objective


def test_low_actor_can_replace_then_extend_its_view_without_changing_capabilities():
    registry, ctx = Registry(), SimpleNamespace()
    current = initial_tool_schemas(registry, "low")
    assert set(_names(current)) == LOW_INITIAL_SCHEMA_NAMES & set(_names(registry.schemas()))
    request = request_tool_schema_selection(ctx, registry, ["write_file", "blocked", "missing"],
                                           context_mode="low", current_schemas=current)
    assert request.status == "pending"
    assert set(request.selection.chosen) == NANO_SCHEMA_META_NAMES | {"write_file"}
    assert request.selection.unavailable == {"blocked": "disabled by this task's contract"}
    assert request.selection.unknown == ("missing",)
    added = request_tool_schema_selection(ctx, registry, ["read_file"], context_mode="low",
                                         current_schemas=current, extend=True)
    assert set(added.selection.chosen) == NANO_SCHEMA_META_NAMES | {"read_file", "write_file"}
    assert "read_file" in _names(current) and "write_file" not in _names(current)
    assert "write_file" in _names(registry.schemas())


from tests.test_main_authored_context import main_loop, call


@pytest.mark.parametrize("mode", ["max", "low", "nano"])
def test_lean_load_replace_and_dispatch_reach_the_next_real_loop_send(main_loop, monkeypatch, mode):
    from tests.test_context_fit_integration import _plan
    f = main_loop
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", mode)
    f.ctx.context_fit_plan = _plan(preferred=mode, window=900_000)
    f.messages = f.ctx.context_fit_plan.messages_for(mode)
    answer, _, _ = f.run([
        call("enable_tools", {"tools": "write_file"}, "enable"),
        call("write_file", {"path": "made.txt", "content": "preserved"}, "write"),
        call("compact_context", {"working_note": "I wrote made.txt and retained its result.",
                                 "keep_unit_ids": [], "schema_names": ["read_file"]}, "compact"),
        {"content": "done"},
    ])
    assert answer == "done"
    first, extended, _third, replaced = [set(_names(row["tools"])) for row in f.inputs]
    assert "write_file" not in first
    assert first | {"write_file"} == extended
    assert replaced == NANO_SCHEMA_META_NAMES | {"read_file"}
    assert f.ctx._context_view_receipt["status"] == "applied"
    assert (f.ctx.repo_dir / "made.txt").read_text() == "preserved"


def test_nonresident_dispatch_and_both_family_history_builders(tmp_path, monkeypatch):
    from ouroboros.llm import LLMClient
    from ouroboros.tools.registry import ToolRegistry
    monkeypatch.setenv("MCP_ENABLED", "false")
    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    registry._ctx.repo_dir.mkdir()
    schemas = initial_tool_schemas(registry, "low")
    assert "write_file" not in _names(schemas)
    output = registry.execute("write_file", {"path": "evidence.txt", "content": "actual dispatch"})
    assert (registry._ctx.repo_dir / "evidence.txt").read_text() == "actual dispatch"
    messages = [{"role": "system", "content": "Keep these instructions exact."},
                {"role": "user", "content": "Write evidence."},
                call("write_file", {"path": "evidence.txt", "content": "actual dispatch"}, "old-call"),
                {"role": "tool", "tool_call_id": "old-call", "content": output},
                {"role": "user", "content": "Continue."}]
    before = copy.deepcopy(messages)
    client = LLMClient()
    anthropic = client._build_remote_candidate(
        {"provider": "anthropic", "resolved_model": "claude-test"}, messages,
        "medium", 65536, "auto", None, schemas)
    uses = [b for m in anthropic["messages"] for b in m["content"] if b["type"] == "tool_use"]
    assert uses[0]["name"] == "write_file" and uses[0]["id"] == "old-call"
    assert "write_file" not in {t["name"] for t in anthropic["tools"]}
    openai = client._build_remote_kwargs(
        {"provider": "openai-compatible", "resolved_model": "test"}, messages,
        "medium", 65536, "auto", None, schemas, skip_capability_fetch=True)
    replay = [m for m in openai["messages"] if m.get("tool_calls")]
    assert replay[0]["tool_calls"][0]["function"]["name"] == "write_file"
    assert "write_file" not in _names(openai["tools"])
    assert messages == before
    assert anthropic["max_tokens"] == openai["max_tokens"] == 65536


@pytest.mark.parametrize("owner_mode", ["max", "low", "nano"])
@pytest.mark.parametrize("shape", ["main", "root", "wake", "child", "metadata-child"])
def test_fresh_task_memory_and_schemas_share_the_role_mode(tmp_path, monkeypatch, owner_mode, shape):
    from ouroboros import context
    from ouroboros.tools.registry import ToolRegistry
    from tests.test_doc_context import _make_env_and_memory
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", owner_mode)
    monkeypatch.setenv("MCP_ENABLED", "false")
    env, memory = _make_env_and_memory(tmp_path / "assembly")
    task = {"id": "fresh", "type": "task", "text": "Keep the governing words."}
    if shape == "main":
        task.update(type="chat", chat_id=1)
    elif shape == "wake":
        task["type"] = "consciousness"
    elif shape == "child":
        task.update(parent_task_id="parent", delegation_role="subagent",
                    subagent_id="openai-compatible::claude-opus-5.5/high")
    elif shape == "metadata-child":
        task["metadata"] = {"parent_task_id": "parent", "delegation_role": "subagent"}
    expected = "low" if "child" in shape else owner_mode
    registry = ToolRegistry(repo_dir=env.repo_dir, drive_root=env.drive_root)
    schemas = initial_tool_schemas(registry, context.initial_context_mode(task))
    messages, facts = context.build_llm_messages(env, memory, task, ctx=registry._ctx,
                                                tool_schemas=schemas, tool_registry=registry)
    assert facts["context_fit"]["preferred_mode"] == expected
    assert facts["context_fit"]["initial_mode"] == expected
    assert messages[-1]["content"] == task["text"]
    assert set(_names(schemas)) == set(_names(initial_tool_schemas(registry, expected)))
    assert len(schemas) <= len(LOW_INITIAL_SCHEMA_NAMES)


def test_low_project_and_main_keep_distinct_room_horizons(tmp_path, monkeypatch):
    from tests._memory_view_context import world, blocks
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", "low")
    env, memory, rooms = world(tmp_path)
    project_task = {"id": "bound", "chat_id": 1}
    a, story, project, facts = blocks(env, memory, project_task)
    assert facts["context_fit"]["preferred_mode"] == "low"
    assert "## My story" not in story
    assert "Cross-room story" in project
    _a, explicit_story, _project, _facts = blocks(
        env, memory, {**project_task, "memory_view": {"story": True}})
    assert "## My story" in explicit_story
    assert f"## This room (Project Alpha [chat_id={rooms['alpha']}])" in project
    assert "## Live rooms" not in project
    assert "memory_read(room_id='*', start=0)" in project
    assert "memory_read(room_id=..., rows=true)" in project
    _a, main_story, main, _facts = blocks(env, memory, {"id": "main", "chat_id": 1})
    assert "## My story" in main_story
    assert "## This room (Main)" in main and "## Live rooms" in main
    assert f"memory_read(room_id='{rooms['alpha']}', rows=true)" in main


@pytest.mark.parametrize("journal,workpad", [(True, True), (False, True), (True, False), (False, False),
                                             (None, True), (True, None), (None, None)])
def test_project_overview_and_scratchpad_pointer_require_both_local_blocks(tmp_path, monkeypatch, journal, workpad):
    from tests._memory_view_context import world, blocks
    from ouroboros import project_facts
    from ouroboros.tools import project_journal
    from ouroboros.tools.knowledge import _knowledge_write
    from ouroboros.tools.registry import ToolContext

    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", "max")
    env, memory, _rooms = world(tmp_path)
    monkeypatch.setattr("ouroboros.config.DATA_DIR", memory.drive_root)
    assert "✅" in _knowledge_write(ToolContext(repo_dir=env.repo_dir, drive_root=memory.drive_root,
        task_id="overview-fixture"), "overview", "AUTHORED_GLOBAL_OVERVIEW")
    # Exercise the production loader with independently empty/available local sources.
    local = tmp_path / "local-workpad.md"
    if workpad is not None:
        local.write_text("LOCAL_WORKING_MEMORY" if workpad else "   ")
    monkeypatch.setattr(project_facts, "project_workpad_path", lambda pid: local)
    monkeypatch.setattr(project_journal, "journal_tail_digest", lambda pid: "LOCAL_JOURNAL" if journal else "")
    _a, _b, project, _facts = blocks(env, memory, {"id": "bound", "chat_id": 1})
    if journal and workpad:
        assert "## Shared understanding (compact pointer" in project
        assert "knowledge_read(topic='overview', scope='global')" in project
        assert "AUTHORED_GLOBAL_OVERVIEW" not in project
        assert "this project's workpad and journal are loaded above" in project
    else:
        assert "## Shared understanding\n" in project
        assert "## Shared understanding (compact pointer" not in project
        assert "this project's workpad and journal are loaded above" not in project
        assert "## Scratchpad" in project
        assert "AUTHORED_GLOBAL_OVERVIEW" in project
