"""Persistence regressions under concurrent, interrupted, and invalid writes."""

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from a11y_computer_use import safety
from a11y_computer_use.schema import TypeText


APP = "test.app"
NOW = 1_783_080_000.0


def _rows(directory: Path) -> list[dict]:
    return [json.loads(line) for path in directory.glob("*.jsonl")
            for line in path.read_text().splitlines()]


def _process_writes(directory: str, worker: int, count: int) -> None:
    """Top-level entry point so spawn works on Linux, macOS, and Windows."""
    root = Path(directory)
    store = safety.PermissionStore(root / "permissions.json")
    audit = safety.AuditLog(root / "audit", max_file_bytes=8192,
                            max_record_bytes=4096, max_files=64)
    for index in range(count):
        store.set_tier(f"worker-{worker}-{index}", safety.Tier.CLICK)
        audit.record({"worker": worker, "index": index, "data": "x" * 1024})


def _interrupted_audit_writer(directory: str) -> None:
    """Exit without cleanup while holding the same lock as normal appends."""
    path = Path(directory)
    with safety._file_lock(path / ".audit.lock"):
        fd = os.open(path / "2026-07-03.jsonl", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        os.write(fd, b'{"interrupted":"unfinished')
        os._exit(0)


def test_processes_merge_permission_updates_and_keep_audit_rows_intact(tmp_path: Path) -> None:
    workers, count = 4, 24
    with ProcessPoolExecutor(max_workers=workers,
                             mp_context=multiprocessing.get_context("spawn")) as executor:
        futures = [executor.submit(_process_writes, str(tmp_path), worker, count)
                   for worker in range(workers)]
        for future in futures:
            future.result(timeout=30)
    permissions = json.loads((tmp_path / "permissions.json").read_text())["apps"]
    assert len(permissions) == workers * count
    rows = _rows(tmp_path / "audit")
    assert {(row["worker"], row["index"]) for row in rows} == {
        (worker, index) for worker in range(workers) for index in range(count)
    }
    assert len(rows) == workers * count
    assert all(row["data"] == "x" * 1024 for row in rows)
    assert all(path.stat().st_size <= 8192 for path in (tmp_path / "audit").glob("*.jsonl"))


@pytest.mark.parametrize("invalid", [
    '{"apps":',
    '{"apps":{"test.app":{"tier":"invalid"}}}',
    '{"apps":{"test.app":{"tier":"full"}},"deny":"test.app"}',
    '{"apps":{"test.app":{"tier":"full"}},"allow":[null]}',
    '["test.app"]',
])
def test_invalid_external_policy_never_revives_a_cached_grant(tmp_path: Path, invalid: str) -> None:
    path = tmp_path / "permissions.json"
    store = safety.PermissionStore(path)
    store.set_tier(APP, safety.Tier.FULL)
    path.write_text(invalid)
    for _ in range(3):
        decision = safety.check_action(TypeText("hello"), APP, store=store)
        assert decision.verdict is safety.Verdict.DENY
        assert "invalid or unreadable" in decision.reason
        assert store.get_tier(APP) is None
    with pytest.raises(ValueError, match="repair"):
        store.set_tier("another.app", safety.Tier.FULL)
    assert path.read_text() == invalid
    path.write_text(json.dumps({"apps": {APP: {"tier": "read"}}}))
    assert store.get_tier(APP) is safety.Tier.READ


def test_unreadable_policy_denies_instead_of_reusing_a_grant(tmp_path: Path, monkeypatch) -> None:
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier(APP, safety.Tier.FULL)

    def unreadable(self: Path, *args, **kwargs):
        raise PermissionError("policy is unreadable")

    # Force refresh; stat failures and read failures must both fail closed.
    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", unreadable)
        patch.setattr(Path, "open", unreadable)
        decision = safety.check_action(TypeText("hello"), APP, store=store)
        assert decision.verdict is safety.Verdict.DENY
        assert decision.granted is None
    assert store.get_tier(APP) is safety.Tier.FULL


def test_invalid_policy_stays_denied_without_reparsing_until_it_changes(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "permissions.json"
    store = safety.PermissionStore(path)
    store.set_tier(APP, safety.Tier.FULL)
    path.write_text('{"apps":')
    original_load = json.load
    loads = 0

    def counted_load(fh):
        nonlocal loads
        loads += 1
        return original_load(fh)

    monkeypatch.setattr(json, "load", counted_load)
    for _ in range(20):
        assert safety.check_action(TypeText("hello"), APP, store=store).verdict is safety.Verdict.DENY
    assert loads == 1
    path.write_text(json.dumps({"apps": {APP: {"tier": "full"}}}))
    assert safety.check_action(TypeText("hello"), APP, store=store).allowed
    assert loads == 2


@pytest.mark.parametrize("contents", [
    '{"apps":',
    '{"apps":{"test.app":{"tier":"read"}}}',
])
def test_policy_cache_uses_consistent_metadata_when_stat_and_fstat_differ(
    tmp_path: Path, monkeypatch, contents: str,
) -> None:
    path = tmp_path / "permissions.json"
    path.write_text(contents)
    original_fstat, original_load = os.fstat, json.load
    loads = 0

    def different_fstat(fd: int):
        stat = original_fstat(fd)
        # Reproduce Python 3.12 on Windows: path stat exposes birthtime as
        # ctime, while fstat exposes change time for the very same file.
        return SimpleNamespace(
            st_dev=stat.st_dev, st_ino=stat.st_ino, st_size=stat.st_size,
            st_mtime_ns=stat.st_mtime_ns, st_ctime_ns=stat.st_ctime_ns + 1_000_000_000,
        )

    def counted_load(fh):
        nonlocal loads
        loads += 1
        return original_load(fh)

    monkeypatch.setattr(os, "fstat", different_fstat)
    monkeypatch.setattr(json, "load", counted_load)
    store = safety.PermissionStore(path)
    for _ in range(20):
        assert not safety.check_action(TypeText("hello"), APP, store=store).allowed
    assert loads == 1
    path.write_text(json.dumps({"apps": {APP: {"tier": "full"}}}))
    assert safety.check_action(TypeText("hello"), APP, store=store).allowed
    assert loads == 2


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_atomic_replace_retries_brief_windows_reader_conflicts(
    tmp_path: Path, monkeypatch, winerror: int,
) -> None:
    path = tmp_path / "permissions.json"
    store = safety.PermissionStore(path)
    store.set_tier(APP, safety.Tier.READ)
    original_replace = os.replace
    attempts = 0

    def reader_blocks_replace(source, target) -> None:
        nonlocal attempts
        attempts += 1
        assert json.loads(path.read_text())["apps"][APP]["tier"] == "read"
        if attempts < 3:
            error = PermissionError(errno.EACCES, "reader has file open")
            error.winerror = winerror
            raise error
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", reader_blocks_replace)
    store.set_tier(APP, safety.Tier.FULL)
    assert attempts == 3
    assert json.loads(path.read_text())["apps"][APP]["tier"] == "full"
    assert store.get_tier(APP) is safety.Tier.FULL
    assert list(tmp_path.glob(".permissions.json.*")) == []


def test_blocked_windows_replace_has_deadline_and_preserves_old_grant(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "permissions.json"
    store = safety.PermissionStore(path)
    store.set_tier(APP, safety.Tier.READ)
    before = path.read_bytes()
    elapsed = 0.0
    attempts = 0

    def permanently_blocked_replace(source, target) -> None:
        nonlocal attempts
        attempts += 1
        error = PermissionError(errno.EACCES, "replacement remains blocked")
        error.winerror = 5
        raise error

    def advance_time(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(os, "replace", permanently_blocked_replace)
    monkeypatch.setattr(safety.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(safety.time, "sleep", advance_time)
    with pytest.raises(PermissionError, match="remains blocked"):
        store.set_tier(APP, safety.Tier.FULL)
    assert elapsed == pytest.approx(5.0)  # the bounded wait, safety._replace_file
    assert 2 <= attempts <= 502  # one attempt per 10 ms sleep, plus the first
    assert path.read_bytes() == before
    assert store.get_tier(APP) is safety.Tier.READ
    assert list(tmp_path.glob(".permissions.json.*")) == []


def test_failed_atomic_replace_preserves_old_file_and_in_memory_policy(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "permissions.json"
    store = safety.PermissionStore(path)
    store.set_tier(APP, safety.Tier.READ)
    before = path.read_bytes()

    def failed_replace(source, target) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", failed_replace)
        with pytest.raises(OSError, match="disk full"):
            store.set_tier(APP, safety.Tier.FULL)
    assert path.read_bytes() == before
    assert store.get_tier(APP) is safety.Tier.READ
    assert list(tmp_path.glob(".permissions.json.*")) == []


def test_atomic_grants_never_expose_partial_json_to_readers(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    store = safety.PermissionStore(path)
    store.set_tier(APP, safety.Tier.READ)
    ready = threading.Event()
    done = threading.Event()

    def reader() -> int:
        reads = 0
        ready.set()
        while not done.is_set():
            try:
                contents = path.read_text()
            except PermissionError as exc:
                if os.name != "nt" or exc.errno != errno.EACCES:
                    raise
                # Windows may deny a new read while replacement is pending.
                # This is temporary unavailability, not partially written JSON.
                done.wait(0.001)
                continue
            assert json.loads(contents)["apps"][APP]["tier"] in {"read", "full"}
            reads += 1
        return reads

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(reader)
        assert ready.wait(timeout=5)
        try:
            for index in range(40):
                store.set_tier(APP, safety.Tier.READ if index % 2 else safety.Tier.FULL)
        finally:
            done.set()
        assert future.result(timeout=5) > 0


def test_replacement_with_same_size_and_mtime_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    store = safety.PermissionStore(path)
    store.set_tier(APP, safety.Tier.FULL)
    original = path.stat()
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(path.read_bytes().replace(b'"full"', b'"read"'))
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(replacement, path)
    assert path.stat().st_size == original.st_size
    assert store.get_tier(APP) is safety.Tier.READ


def test_policy_changed_while_reading_is_not_cached_as_current(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "permissions.json"
    store = safety.PermissionStore(path)
    store.set_tier(APP, safety.Tier.FULL)
    original_load = json.load

    def revoke_while_loading(fh):
        data = original_load(fh)
        path.write_text(json.dumps({"apps": {APP: {"tier": "read"}}}))
        return data

    with monkeypatch.context() as patch:
        patch.setattr(json, "load", revoke_while_loading)
        store._load()
    decision = safety.check_action(TypeText("hello"), APP, store=store)
    assert decision.verdict is safety.Verdict.DENY
    assert decision.granted is safety.Tier.READ


def test_audit_retention_bounds_disk_and_preserves_unrelated_files(tmp_path: Path) -> None:
    audit = safety.AuditLog(tmp_path, now=lambda: NOW, max_file_bytes=2048,
                            max_record_bytes=1024, max_files=3)
    unrelated = tmp_path / "imported-data.jsonl"
    unrelated.write_text('{"keep":true}\n')
    for index in range(30):
        audit.record({"index": index, "data": "x" * 700})
    files = [path for path in tmp_path.glob("*.jsonl") if path != unrelated]
    assert len(files) == 3
    assert sum(path.stat().st_size for path in files) <= 3 * 2048
    rows = [json.loads(line) for path in files for line in path.read_text().splitlines()]
    assert max(row["index"] for row in rows) == 29
    assert len({row["index"] for row in rows}) == len(rows)
    assert unrelated.read_text() == '{"keep":true}\n'


def test_audit_startup_preserves_oversized_legacy_files_until_normal_eviction(tmp_path: Path) -> None:
    legacy = tmp_path / "2020-01-01.jsonl"
    legacy.write_text(json.dumps({"legacy": "x" * 8192}) + "\n")
    audit = safety.AuditLog(tmp_path, max_file_bytes=2048, max_record_bytes=1024, max_files=3)
    audit.record({"current": True})
    assert legacy.exists()
    assert any(row.get("current") is True for row in _rows(tmp_path))


def test_audit_summarizes_oversized_records_after_redaction(tmp_path: Path) -> None:
    audit = safety.AuditLog(tmp_path, max_file_bytes=2048, max_record_bytes=1024)
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier(APP, safety.Tier.FULL)
    action = TypeText("secret" * 1000)
    decision = safety.check_action(action, APP, store=store)
    path = audit.record_action(action, app=APP, decision=decision, result="ok", secure=True)
    assert "secret" not in path.read_text()
    assert _rows(tmp_path)[0]["params"]["text"] == safety.REDACTED
    audit.record_action(action, app=APP, decision=decision, result="ok")
    summary = _rows(tmp_path)[-1]
    assert summary["params"]["_truncated"] is True
    assert summary["params"]["_original_bytes"] > 1024
    assert summary["decision"]["verdict"] == "allow"
    assert summary["action"] == "typetext" and summary["result"] == "ok"
    assert all(len(line) + 1 <= 1024 for line in path.read_bytes().splitlines())


def test_threads_share_one_audit_instance_without_losing_rows(tmp_path: Path) -> None:
    audit = safety.AuditLog(tmp_path)
    barrier = threading.Barrier(8)

    def write(worker: int) -> None:
        barrier.wait(timeout=5)
        for index in range(40):
            audit.record({"worker": worker, "index": index, "data": "x" * 2048})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(8)))
    rows = _rows(tmp_path)
    assert len(rows) == 320
    assert len({(row["worker"], row["index"]) for row in rows}) == 320


def test_escaped_identifiers_and_numeric_timestamp_strings_cannot_bypass_record_cap(tmp_path: Path) -> None:
    audit = safety.AuditLog(tmp_path, max_file_bytes=2048, max_record_bytes=1024)
    path = audit.record({
        "ts": "0" * 5000 + str(NOW), "app": "\ud800" * 1000,
        "action": "\0" * 1000, "result": "\0" * 1000,
    })
    assert path.stat().st_size <= 1024
    row = json.loads(path.read_text())
    assert row["ts"] == NOW
    assert row.get("audit_truncated") or row.get("params", {}).get("_truncated")


def test_interrupted_process_releases_lock_and_partial_audit_tail_is_repaired(tmp_path: Path) -> None:
    audit = safety.AuditLog(tmp_path, now=lambda: NOW)
    path = audit.record({"index": 1})
    assert path.name == "2026-07-03.jsonl"
    process = multiprocessing.get_context("spawn").Process(
        target=_interrupted_audit_writer, args=(str(tmp_path),)
    )
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("interrupted writer hung")
    assert process.exitcode == 0
    assert path.read_bytes().endswith(b"unfinished")
    audit.record({"index": 2})
    assert [row["index"] for row in _rows(tmp_path)] == [1, 2]


def test_partial_write_failure_rolls_back_before_the_next_record(tmp_path: Path, monkeypatch) -> None:
    audit = safety.AuditLog(tmp_path)
    path = audit.record({"index": 1})
    original_write = os.write
    calls = 0

    def fail_after_partial(fd: int, data) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(fd, data[:8])
        raise OSError(errno.ENOSPC, "disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", fail_after_partial)
        with pytest.raises(OSError, match="disk full"):
            audit.record({"index": 2})
    assert len(path.read_text().splitlines()) == 1
    audit.record({"index": 3})
    assert [row["index"] for row in _rows(tmp_path)] == [1, 3]


def test_lock_wait_has_a_deadline_and_does_not_leak_locks(tmp_path: Path) -> None:
    path = tmp_path / "file.lock"
    with safety._file_lock(path):
        with pytest.raises(TimeoutError, match="timed out"):
            with safety._file_lock(path, timeout=0.02):
                pytest.fail("second writer acquired an already-held lock")
    with safety._file_lock(path, timeout=0.02):
        pass


def test_sync_audit_fsyncs_before_returning(tmp_path: Path, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(os, "fsync", calls.append)
    safety.AuditLog(tmp_path, sync=True).record({"ok": True})
    assert len(calls) == (1 if os.name == "nt" else 2)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_permission_and_audit_files_are_private_by_default(tmp_path: Path) -> None:
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier(APP, safety.Tier.READ)
    path = safety.AuditLog(tmp_path / "audit").record({"ok": True})
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kwargs", [
    {"max_files": 0}, {"max_files": True}, {"max_record_bytes": 256},
    {"max_file_bytes": 1024}, {"lock_timeout": 0}, {"lock_timeout": float("nan")},
])
def test_audit_rejects_unbounded_or_inconsistent_limits(tmp_path: Path, kwargs: dict) -> None:
    with pytest.raises(ValueError):
        safety.AuditLog(tmp_path, **kwargs)


def test_lock_byte_init_tolerates_another_process_holding_the_byte(tmp_path: Path, monkeypatch) -> None:
    """Windows: the first opener writes and locks byte 0; a second opener's write
    into that locked byte fails with EACCES, which must not abort the lock."""
    fd = os.open(tmp_path / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        def locked_range_write(_fd, _data):
            raise PermissionError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(safety.os, "write", locked_range_write)
        safety._ensure_lock_byte(fd)  # no raise: the byte exists, the lock loop will wait

        def disk_full(_fd, _data):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(safety.os, "write", disk_full)
        with pytest.raises(OSError, match="No space"):
            safety._ensure_lock_byte(fd)
    finally:
        os.close(fd)


def test_lock_byte_init_writes_once_then_leaves_the_file_alone(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        safety._ensure_lock_byte(fd)
        safety._ensure_lock_byte(fd)
    finally:
        os.close(fd)
    assert path.read_bytes() == b"\0"

