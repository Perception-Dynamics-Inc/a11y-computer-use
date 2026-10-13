"""Real filesystem regressions for the opaque Thunar view fallback (#481)."""

import json
import sys
from types import SimpleNamespace
import pytest

from a11y_computer_use import files, safety
from a11y_computer_use.schema import ComputerUseError


@pytest.fixture
def helper(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    audit = safety.AuditLog(tmp_path / "audit")
    return files.FileOperations(str(root), audit), root


def records(helper):
    return [json.loads(line) for path in helper.audit.dir_path.glob("*.jsonl")
            for line in path.read_text().splitlines()]


def unsupported_platform(ops, root):
    """Other platforms must refuse every verb without changing files."""
    if sys.platform == "linux":
        return False
    for action in ("list", "mkdir", "rename", "move"):
        with pytest.raises(ComputerUseError, match="requires Linux"):
            ops.run(action, str(root / "source"), str(root / "dest"))
    assert not (root / "source").exists() and not (root / "dest").exists()
    return True


def test_unsupported_platform_is_refused(helper, monkeypatch):
    ops, root = helper
    monkeypatch.setattr(files, "sys", SimpleNamespace(platform="win32"))
    for action in ("list", "mkdir", "rename", "move"):
        with pytest.raises(ComputerUseError, match="requires Linux"):
            ops.run(action, str(root / "source"), str(root / "dest"))
    assert list(root.iterdir()) == []
    assert [entry["result"] for entry in records(ops)] == ["attempt", "refused"] * 4


def test_named_file_task_without_ui_input(helper):
    ops, root = helper
    if unsupported_platform(ops, root):
        return
    source = root / "α report.txt"
    source.write_text("keep the contents")
    assert ops.run("list", str(root))["entries"] == [source.name]
    folder = root / "archive"
    assert ops.run("mkdir", str(folder))["verified"]
    renamed = root / "final.txt"
    assert ops.run("rename", str(source), str(renamed))["verified"]
    moved = folder / renamed.name
    assert ops.run("move", str(renamed), str(moved))["verified"]
    assert moved.read_text() == "keep the contents"
    assert not source.exists() and not renamed.exists()
    entries = records(ops)
    assert [e["result"] for e in entries] == ["attempt", "ok"] * 4
    assert entries[-1]["evidence"]["destination"] == str(moved)


def test_directory_move(helper):
    ops, root = helper
    if unsupported_platform(ops, root):
        return
    source = root / "folder"
    source.mkdir()
    (source / "item").write_text("data")
    dest = root / "renamed"
    assert ops.run("rename", str(source), str(dest))["verified"]
    assert (dest / "item").read_text() == "data"


@pytest.mark.parametrize("target", ["outside", "root", "dotdot", "relative"])
def test_reject_paths_outside_allowlist(helper, target):
    ops, root = helper
    if unsupported_platform(ops, root):
        return
    path = {"outside": str(root.parent / "outside"), "root": str(root),
            "dotdot": str(root / ".." / "outside"), "relative": "relative"}[target]
    with pytest.raises(ValueError):
        ops.run("mkdir", path)
    assert not (root.parent / "outside").exists()
    assert records(ops)[-1]["result"] == "refused"


@pytest.mark.parametrize("action", ["mkdir", "move", "list"])
def test_symlink_parent_cannot_escape(helper, action):
    ops, root = helper
    if unsupported_platform(ops, root):
        return
    outside = root.parent / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    source = root / "item"
    source.write_text("original")
    with pytest.raises(ComputerUseError):
        if action == "move":
            ops.run(action, str(source), str(root / "link" / "item"))
        else:
            ops.run(action, str(root / "link" if action == "list" else root / "link" / "item"))
    assert source.read_text() == "original"
    assert list(outside.iterdir()) == []
    assert records(ops)[-1]["result"] == "error"


def test_symlink_source_refused(helper):
    ops, root = helper
    if unsupported_platform(ops, root):
        return
    (root / "link").symlink_to(root.parent / "secret")
    with pytest.raises(ValueError, match="not symlinks"):
        ops.run("move", str(root / "link"), str(root / "new"))
    assert (root / "link").is_symlink()


@pytest.mark.parametrize("destination_type", ["file", "directory", "symlink"])
def test_no_overwrite(helper, destination_type):
    ops, root = helper
    if unsupported_platform(ops, root):
        return
    source, dest = root / "source", root / "dest"
    source.write_text("original")
    if destination_type == "directory":
        dest.mkdir()
    elif destination_type == "symlink":
        dest.symlink_to(root / "missing")
    else:
        dest.write_text("preserved")
    with pytest.raises(ComputerUseError):
        ops.run("move", str(source), str(dest))
    assert source.read_text() == "original"
    if destination_type == "file":
        assert dest.read_text() == "preserved"
    elif destination_type == "directory":
        assert dest.is_dir()
    else:
        assert dest.is_symlink()


def test_failed_or_unverified_move_never_claims_success(helper, monkeypatch):
    ops, root = helper
    if unsupported_platform(ops, root):
        return
    source = root / "source"
    source.write_text("data")
    monkeypatch.setattr(files, "_rename", lambda *args: None)
    with pytest.raises(ComputerUseError):
        ops.run("move", str(source), str(root / "dest"))
    assert source.read_text() == "data"
    assert records(ops)[-1]["result"] == "error"
    assert all(e["result"] != "ok" for e in records(ops))


def test_audit_failure_prevents_mutation(helper, monkeypatch):
    ops, root = helper
    if unsupported_platform(ops, root):
        return
    def fail(_event):
        raise OSError("audit unavailable")
    monkeypatch.setattr(ops.audit, "record", fail)
    with pytest.raises(OSError, match="audit unavailable"):
        ops.run("mkdir", str(root / "folder"))
    assert not (root / "folder").exists()


def test_disabled_by_default(tmp_path):
    ops = files.FileOperations("", safety.AuditLog(tmp_path / "audit"))
    if unsupported_platform(ops, tmp_path):
        return
    with pytest.raises(ValueError, match="disabled"):
        ops.run("mkdir", str(tmp_path / "folder"))
    assert not (tmp_path / "folder").exists()


def test_nested_allowed_root_cannot_be_moved(helper):
    ops, root = helper
    if unsupported_platform(ops, root):
        return
    nested = root / "nested"
    nested.mkdir()
    ops = files.FileOperations(f"{root}:{nested}", ops.audit)
    with pytest.raises(ValueError, match="root itself"):
        ops.run("move", str(nested), str(root / "new"))
    assert nested.is_dir()


def test_rename_does_not_substitute_move(helper):
    ops, root = helper
    if unsupported_platform(ops, root):
        return
    source = root / "source"
    source.write_text("data")
    (root / "folder").mkdir()
    with pytest.raises(ValueError, match="same parent"):
        ops.run("rename", str(source), str(root / "folder" / "dest"))
    assert source.exists()


def test_list_is_bounded(helper):
    ops, root = helper
    if unsupported_platform(ops, root):
        return
    for index in range(201):
        (root / str(index)).touch()
    with pytest.raises(ValueError, match="exceeds 200"):
        ops.run("list", str(root))


def test_missing_source_is_an_honest_error(helper):
    ops, root = helper
    if unsupported_platform(ops, root):
        return
    with pytest.raises(ComputerUseError, match="failed"):
        ops.run("move", str(root / "missing"), str(root / "dest"))
    assert not (root / "dest").exists()
