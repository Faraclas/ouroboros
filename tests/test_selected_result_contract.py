"""Registered chosen reads are not unsolicited stdout; no provider calls."""
import json
from pathlib import Path

import pytest

from ouroboros.loop_tool_execution import StatefulToolExecutor, handle_tool_calls
from ouroboros.tool_result_delivery import RESULT_VIEW_MARKER
from tests._tool_result_delivery_shared import measured_fit
from tests.test_work_item_safety_dispatch import board_registry  # real disposable plugin


def deliver(registry, tmp_path, calls, *, window=None, target=3000):
    messages = [{"role": "user", "content": "read the selected sources" + " history" * 3000}]
    trace = {"tool_calls": []}
    logs = tmp_path / "delivery-logs"
    logs.mkdir(exist_ok=True)
    executor = StatefulToolExecutor()
    try:
        tool_calls = [{"id": f"selected-{i}", "type": "function",
                       "function": {"name": name, "arguments": json.dumps(args)}}
                      for i, (name, args) in enumerate(calls)]
        messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
        errors = handle_tool_calls(tool_calls, registry, logs, registry._ctx.task_id,
            executor, messages, trace, lambda *_: None,
            fit_candidate=measured_fit(window=window, target=target, reserve=1000), tool_schemas=[])
    finally:
        executor.shutdown(wait=True)
    assert errors == 0, [(r["tool"], r["result"]) for r in trace["tool_calls"] if r.get("is_error")]
    return messages, trace


@pytest.mark.parametrize("end", [None, 18000])
def test_registered_knowledge_default_and_end_only_are_chosen(board_registry, tmp_path, end):
    registry, _ = board_registry
    path = tmp_path / "memory/knowledge/selected.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: note\nsummary: selected\n---\n" + "Exact selected knowledge.\n" * 1000)
    args = {"topic": "selected", "scope": "global"}
    if end is not None:
        args["end_char"] = end
    expected = registry.execute("knowledge_read", args)
    assert "Exact selected knowledge" in expected
    messages, _ = deliver(registry, tmp_path, [("knowledge_read", args)])
    assert messages[-1]["content"] == expected


def test_registered_extension_exact_item_default_is_chosen(board_registry, tmp_path):
    registry, names = board_registry
    saved = json.loads(registry.execute(names["capture"], {"project_id": "p", "title": "Selected",
        "description": "Exact selected item.\n" * 1000, "request_id": "selected-create"}))
    assert saved["ok"]
    args = {"item_id": saved["item"]["id"]}
    expected = registry.execute(names["get"], args)
    messages, _ = deliver(registry, tmp_path, [(names["get"], args)])
    assert messages[-1]["content"] == expected


def test_registered_file_default_page_is_chosen(board_registry, tmp_path):
    registry, _ = board_registry
    path = tmp_path / "selected.txt"
    path.write_text("Selected file page.\n" * 1000)
    args = {"path": str(path)}
    expected = registry.execute("read_file", args)
    assert "Selected file page" in expected
    messages, _ = deliver(registry, tmp_path, [("read_file", args)])
    assert messages[-1]["content"].startswith(expected)
    assert RESULT_VIEW_MARKER not in messages[-1]["content"]


def probe(registry, *, name="selection_probe", policy=None, body=None, alias_for=""):
    from ouroboros.tools.tool_catalog import ToolEntry
    from ouroboros.tool_capabilities import RESULT_VIEW_PARAMS
    schema = {"name": name, "description": "Disposable selection control", "parameters": {
        "type": "object", "properties": {"span": {"type": "integer"}, "limit": {"type": "integer"}, **RESULT_VIEW_PARAMS}}}
    text = body or "Selected probe head\n" + "unchanged evidence\n" * 2000 + "Selected probe tail"
    registry.register(ToolEntry(name, schema, lambda _ctx, **_kw: text,
        safety_policy="skip", alias_for=alias_for, result_selection=policy or {}))
    return text


def test_mixed_same_tool_windows_compact_views_and_unmarked_grammar(board_registry, tmp_path):
    registry, _ = board_registry
    text = probe(registry, policy={"arguments": ["span"]})
    unknown = probe(registry, name="unmarked_probe")
    messages, trace = deliver(registry, tmp_path, [
        ("selection_probe", {"span": 17}), ("selection_probe", {"limit": 17}),
        ("selection_probe", {"span": 17, "view_head_chars": 41, "view_tail_chars": 23}),
        ("unmarked_probe", {"limit": 17})])
    rows = [r for r in messages if r["role"] == "tool"]
    assert rows[0]["content"] == text
    assert all(RESULT_VIEW_MARKER in rows[i]["content"] for i in [1, 2, 3])
    assert text[:41] in rows[2]["content"] and text[-23:] in rows[2]["content"]
    assert unknown not in rows[3]["content"]
    from ouroboros.tool_result_record import read_tool_result_record
    assert [read_tool_result_record(row)["result_selection"]["view"] for row in rows] == [
        [None, None], None, [41, 23], None]


def test_chosen_body_oversize_keeps_exact_source_and_cut_reason(board_registry, tmp_path):
    from ouroboros.artifacts import read_actor_source_bytes
    registry, _ = board_registry
    text = probe(registry, policy={"default": "chosen"}, body="x selected body\n" * 20000)
    messages, trace = deliver(registry, tmp_path, [("selection_probe", {})], window=9000, target=None)
    view = trace["tool_calls"][0]
    assert view["result_partial"] and view["result_source_view"]["cut_reason"] == "chosen_body_exceeds_usable_input"
    assert read_actor_source_bytes(registry._ctx.drive_root, registry._ctx.task_id,
                                   view["result_source_ref"]).decode() == text
    assert RESULT_VIEW_MARKER in messages[-1]["content"]


def test_crowded_physical_frame_keeps_fitting_chosen_body_for_presend_recovery(board_registry, tmp_path):
    registry, _ = board_registry
    text = probe(registry, policy={"default": "chosen"}, body="small chosen body\n" * 300)
    messages, trace = deliver(registry, tmp_path, [("selection_probe", {})], window=5000, target=3000)
    assert messages[-1]["content"] == text
    assert trace["tool_result_delivery"][0]["recovery_required"] is True


def test_checkpoint_selection_survives_registry_change_and_invalid_record_does_not(board_registry, tmp_path):
    from dataclasses import replace
    from ouroboros import context_compaction as cc
    from ouroboros.context_source_view import chosen_read_units
    from ouroboros.tool_result_record import TOOL_RESULT_RECORD_KEY as KEY, read_tool_result_record
    registry, _ = board_registry
    probe(registry, policy={"default": "chosen"})
    messages, _ = deliver(registry, tmp_path, [("selection_probe", {})])
    restored = json.loads(json.dumps(messages))
    # The common continuation restorer must carry this exact occurrence, not
    # classify it using a newly published descriptor or a repeated provider ID.
    from ouroboros.owner_wait import restore_continuation_state
    target = []
    state = {"messages": restored, "trace": {}, "usage": {}, "seen": [],
             "acceptance": {}, "route": {}, "delivery": {}}
    restore_continuation_state(registry, state, target, {}, {}, set())
    registry._entries["selection_probe"] = replace(registry._entries["selection_probe"], result_selection={})
    units = cc.context_units(target, scope="dialogue")
    assert chosen_read_units(target, units)
    assert read_tool_result_record(target[-1])["result_selection"] == {"view": [None, None]}
    target[-1]["content"] += "foreign change"
    assert not chosen_read_units(target, cc.context_units(target, scope="dialogue"))
    target[-1].pop(KEY)
    assert not chosen_read_units(target, cc.context_units(target, scope="dialogue"))


def test_alias_and_handler_override_preserve_declared_selection(board_registry):
    registry, _ = board_registry
    probe(registry, name="canonical_probe", policy={"default": "chosen"})
    probe(registry, name="old_probe", policy={"default": "chosen"}, alias_for="canonical_probe")
    registry.override_handler("old_probe", lambda *_a, **_k: "new body")
    result = registry.execute_result("old_probe", {})
    assert result.text == "new body" and dict(result.result_selection) == {"view": [None, None]}


@pytest.mark.parametrize("policy", [{"default": "guess"}, {"arguments": ["undeclared"]}, {"arguments": "span"}])
def test_bad_producer_selection_is_rejected_without_changing_dispatch(board_registry, policy):
    registry, _ = board_registry
    with pytest.raises(ValueError):
        probe(registry, policy=policy)


def test_registered_process_output_is_automatic_even_with_exact_path(board_registry, tmp_path, monkeypatch):
    from ouroboros import safety
    registry, _ = board_registry
    text = "process stdout\n" * 4000
    # Scripted assessment/handler keeps this a local dispatch/delivery control,
    # not a provider request or an actual process-lifecycle certification.
    monkeypatch.setattr(safety, "check_safety", lambda *a, **k: (True, ""))
    def command(_ctx, _resolved_binding=None, **_kw):
        return text
    registry.override_handler("run_command", command)
    messages, _ = deliver(registry, tmp_path, [("run_command", {"cmd": ["/exact/process"]})])
    assert RESULT_VIEW_MARKER in messages[-1]["content"]


def test_real_main_next_send_carries_chosen_tail(main_loop, monkeypatch):
    from tests.test_main_authored_context import call
    from ouroboros.tool_result_record import read_tool_result_record
    f = main_loop
    text = probe(f.registry, policy={"arguments": ["span"]})
    f.run([call("selection_probe", {"span": 23}, "chosen"), {"content": "done"}])
    row = next(row for row in f.inputs[-1]["messages"] if row.get("tool_call_id") == "chosen")
    assert row["content"] == text
    assert read_tool_result_record(row)["result_selection"] == {"view": [None, None]}


from tests.test_main_authored_context import main_loop


@pytest.mark.parametrize("sdk_error", [False, True])
def test_registered_mcp_legacy_window_preserves_selection(board_registry, tmp_path, monkeypatch, sdk_error):
    from ouroboros import mcp_client, safety
    from ouroboros.tools.tool_result import ToolResult
    from tests.test_mcp_client import _settings
    registry, _ = board_registry
    manager = mcp_client.MCPManager()
    async def listing(*_args):
        return [{"name":"read", "description":"window", "input_schema":{
            "type":"object", "properties":{"offset":{"type":"integer"}}}}]
    text = "Exact MCP window.\n" * 4000
    async def answer(*_args):
        return ToolResult(status="error" if sdk_error else "ok", code="MCP_ERROR" if sdk_error else "OK", text=text)
    manager._async_list_tools = listing
    manager._async_call_tool = answer
    manager.reconfigure(_settings({"id":"selected", "enabled":True, "transport":"streamable_http", "url":"https://unused.invalid/mcp"}))
    assert manager.refresh_server("selected")["ok"]
    monkeypatch.setattr(mcp_client, "ensure_configured_from_settings", lambda: None)
    monkeypatch.setattr(mcp_client, "get_manager", lambda: manager)
    monkeypatch.setattr(safety, "check_safety", lambda *a, **k: (True, ""))
    name = mcp_client.make_tool_name("selected", "read")
    selected = registry.execute_result(name, {"offset": 4})
    automatic = registry.execute_result(name, {})
    assert selected.status == automatic.status == ("error" if sdk_error else "ok")
    assert dict(selected.result_selection) == {"view": [None, None]}
    assert dict(automatic.result_selection) == {"view": None}
    from ouroboros.tool_result_delivery import project_tool_result_batch
    rows = [{"tool_call_id":"mcp", "fn_name":name, "tool_args":{"offset":4},
             "result":selected.text, "result_selection":dict(selected.result_selection),
             "tool_result":selected, "is_error":sdk_error}]
    delivered, _ = project_tool_result_batch(rows, [{"role":"user", "content":"history" * 5000}], [],
        drive_root=tmp_path, task_id="mcp-window", fit_candidate=measured_fit(target=3000, reserve=1000), policy="measured_frame")
    assert delivered[0]["result"] == selected.text and text in selected.text
