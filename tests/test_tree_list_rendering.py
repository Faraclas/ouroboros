"""Registered tree replies retain traversal meaning while only displayed paths render."""
from __future__ import annotations

import json
import sys

import pytest

from ouroboros.tools import core_file_tools
from ouroboros.tools.registry import ToolRegistry


@pytest.mark.parametrize("include,expected_count", [("*.py", 1), ("", 41)])
def test_registered_tree_renders_only_paths_it_displays(tmp_path, include, expected_count):
    repo, data = tmp_path / "repo", tmp_path / "data"
    repo.mkdir()
    folder = data / "small"
    folder.mkdir(parents=True)
    for index in range(40):
        (folder / f"café-{index:02}.txt").write_text("x", encoding="utf-8")
    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    rendered = []
    previous = sys.getprofile()

    def profile(frame, event, arg):
        if (event == "call" and frame.f_code.co_name == "shown"
                and frame.f_code.co_filename == core_file_tools.__file__):
            rendered.append(frame.f_locals["entry"])

    try:
        sys.setprofile(profile)
        result = registry.execute_result("list_files", {
            "root": "runtime_data", "path": "small", "depth": 2,
            "include": include, "max_scan": 100, "max_entries": 100,
        })
    finally:
        sys.setprofile(previous)
    assert result.status == "ok", result.text
    body = json.loads(result.text)
    assert body["path"] == "runtime_data:small"
    assert body["scanned"] == 40 and body["complete"] is True and body["gaps"] == []
    assert body["entries"] == ([] if include else [f"small/café-{i:02}.txt" for i in range(40)])
    assert len(rendered) == expected_count  # the target, plus only returned files


def test_filtered_tree_keeps_gap_paths_and_fresh_second_call(tmp_path):
    repo, data = tmp_path / "repo", tmp_path / "data"
    repo.mkdir()
    (repo / "nested").mkdir()
    (repo / "nested" / "é.py").write_text("x", encoding="utf-8")
    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    args = {"root": "active_workspace", "depth": 1, "include": "*.py"}
    first = registry.execute_result("list_files", args)
    assert first.status == "ok", first.text
    body = json.loads(first.text)
    assert body["entries"] == [] and body["complete"] is False
    assert body["gaps"][0]["kind"] == "depth_limit"
    assert body["gaps"][0]["paths"] == ["nested/"]
    (repo / "fresh.py").write_text("new", encoding="utf-8")
    second = registry.execute_result("list_files", args)
    assert second.status == "ok", second.text
    assert json.loads(second.text)["entries"] == ["fresh.py"]
