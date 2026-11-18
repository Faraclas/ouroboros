"""A directory export keeps native non-UTF8 member names instead of failing the whole export."""
import json
import os
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from ouroboros import artifacts
from ouroboros.artifact_directories import archive_member_name


def _native_tree(tmp_path):
    source = tmp_path / "evidence"
    source.mkdir()
    raw = os.fsencode(source) + b"/bad\xff-name.txt"
    with open(raw, "wb") as handle:
        handle.write(b"native bytes")
    (source / "plain.txt").write_text("plain")
    return source


def test_surrogate_names_export_with_exact_native_name_in_manifest(tmp_path):
    source = _native_tree(tmp_path)
    ctx = SimpleNamespace(drive_root=tmp_path / "data", task_id="native")
    records = artifacts.copy_directory_to_task_artifacts(ctx, source)
    text = Path(records[0]["path"]).read_text(encoding="utf-8")  # valid UTF-8 document
    manifest = json.loads(text)
    rows = {row["path"]: row for row in manifest["files"]}
    native = "bad\udcff-name.txt"
    assert set(rows) == {native, "plain.txt"}
    assert os.fsencode(native) == b"bad\xff-name.txt"
    assert rows[native]["archive_name"] == "bad%ff-name.txt"
    assert "archive_name" not in rows["plain.txt"]
    with zipfile.ZipFile(records[1]["path"]) as archive:
        assert set(archive.namelist()) == {"bad%ff-name.txt", "plain.txt"}
        data = archive.read(rows[native]["archive_name"])
    assert (len(data), sha256(data).hexdigest()) == (rows[native]["size"], rows[native]["sha256"])
    assert data == b"native bytes"
    assert (source / native).read_bytes() == b"native bytes"  # original untouched


def test_archive_names_are_unchanged_for_ordinary_unicode():
    assert archive_member_name("caf\u00e9/\u4e2d.txt") == "caf\u00e9/\u4e2d.txt"


def test_escaped_name_colliding_with_a_real_member_is_refused(tmp_path):
    source = _native_tree(tmp_path)
    (source / "bad%ff-name.txt").write_text("literal")
    ctx = SimpleNamespace(drive_root=tmp_path / "data", task_id="collide")
    with pytest.raises(OSError, match="collides"):
        artifacts.copy_directory_to_task_artifacts(ctx, source)
    assert not list((tmp_path / "data").rglob("*.zip"))


def test_workspace_file_output_reader_resolves_escaped_archive_member(tmp_path):
    import shutil
    from ouroboros.workspace_file_outputs import _archive_files

    source = _native_tree(tmp_path)
    ctx = SimpleNamespace(drive_root=tmp_path / "data", task_id="wfo")
    records = artifacts.copy_directory_to_task_artifacts(ctx, source, kind="workspace_file_outputs")
    cap = tmp_path / "cap"
    cap.mkdir()
    for record in records:
        shutil.copy2(record["path"], cap / record["name"])
    files = _archive_files(records, cap)
    native = "bad\udcff-name.txt"
    assert set(files) == {native, "plain.txt"}
    assert files[native]["member"] == "bad%ff-name.txt"
    with zipfile.ZipFile(files[native]["archive_path"]) as archive:
        assert archive.read(files[native]["member"]) == b"native bytes"
