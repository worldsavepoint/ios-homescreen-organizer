"""Failure and concurrency at the saved draft, history and recovery boundaries."""

import asyncio
import errno
import json
import plistlib
from pathlib import Path

import pytest

import homescreen.session as session_module
from homescreen.model import LayoutError
from homescreen.session import Session
from test_draft_identity import folder_session
from test_live_frame import FrameSource, external_traps, reviewed
from test_pin_integrity import apply, run


@pytest.mark.parametrize("next_stamp", ["20261001-120000", "20261001-110000"])
def test_rapid_reads_retain_history_and_failed_read_restores_pruned_snapshot(tmp_path, monkeypatch, next_stamp):
    from test_integrity import MemorySource

    source = MemorySource()
    session = Session(source, tmp_path / "data")
    monkeypatch.setattr(session_module, "_stamp", lambda: "20261001-120000")
    for i in range(10):
        source.state[1][0]["displayName"] = str(i)
        run(session.read())
    directory = session._dev_dir("reads")
    before = {p.name: p.read_bytes() for p in sorted(directory.glob("*.plist"))}
    assert [plistlib.loads(data)["state"][1][0]["displayName"] for data in before.values()] == list(map(str, range(10)))
    monkeypatch.setattr(session_module, "_stamp", lambda: next_stamp)
    source.state[1][0]["displayName"] = "latest"
    with monkeypatch.context() as patch:
        patch.setattr(session, "_save_draft", lambda: (_ for _ in ()).throw(fault()))
        with pytest.raises(OSError):
            run(session.read())
    assert {p.name: p.read_bytes() for p in directory.glob("*.plist")} == before
    run(session.read())
    owners = [plistlib.loads(p.read_bytes())["state"][1][0]["displayName"] for p in sorted(directory.glob("*.plist"))]
    assert owners == [*map(str, range(1, 10)), "latest"]


def test_read_history_retries_collision_and_prunes_legacy_names_in_order(tmp_path, monkeypatch):
    from test_integrity import MemorySource

    source = MemorySource()
    session = Session(source, tmp_path / "data")
    monkeypatch.setattr(session_module, "_stamp", lambda: "20261001-120000")
    run(session.read())
    reads = session._dev_dir("reads")
    original, = reads.glob("*.plist")
    original.rename(reads / "20261001-120000.plist")
    link = session_module.os.link
    def collide_once(source_name, target_name, **kwargs):
        if target_name == "20261001-120000~0000000001.plist":
            (reads / target_name).write_bytes(b"intervening")
        return link(source_name, target_name, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(session_module.os, "link", collide_once)
        run(session.read())
    ordered = sorted(reads.glob("*.plist"))
    assert [p.name for p in ordered] == ["20261001-120000.plist", "20261001-120000~0000000001.plist",
                                         "20261001-120000~0000000002.plist"]
    assert ordered[1].read_bytes() == b"intervening"
    for _ in range(8):
        run(session.read())
    assert not ordered[0].exists() and len(list(reads.glob("*.plist"))) == 10


def test_read_snapshot_staging_failure_preserves_directory_and_draft(tmp_path, monkeypatch):
    from test_integrity import MemorySource

    session = Session(MemorySource(), tmp_path / "data")
    run(session.read())
    reads = session._dev_dir("reads")
    before = {p.name: p.read_bytes() for p in reads.iterdir()}
    saved = session.draft_path.read_bytes(), session.binding, session.history()
    sync = session_module._sync_directory
    def fail_snapshot_sync(path, **kwargs):
        if path == reads:
            raise OSError("snapshot sync failed")
        sync(path, **kwargs)
    monkeypatch.setattr(session_module, "_sync_directory", fail_snapshot_sync)
    with pytest.raises(OSError, match="snapshot sync failed"):
        run(session.read())
    assert {p.name: p.read_bytes() for p in reads.iterdir()} == before
    assert (session.draft_path.read_bytes(), session.binding, session.history()) == saved


def test_ambiguous_saved_draft_gates_restore_until_successful_read(tmp_path):
    session, source = folder_session(tmp_path)
    backup = session._backup(session.require().to_state(), [], "synthetic")
    session.edit(lambda lay: lay.pin("folder:1", 1, 34))
    session.edit(lambda lay: lay.move("folder:2", "page:1", index=0))
    blob = plistlib.loads(session.draft_path.read_bytes())
    del blob["identity"], blob["source_identity"]
    original = plistlib.dumps(blob)
    session.draft_path.write_bytes(original)
    session = Session(source, session.dir)
    with pytest.raises(LayoutError, match="identity.*read"):
        run(session.restore(backup.name))
    assert session.draft_path.read_bytes() == original
    assert session.backups() == [backup.name] and source.payloads == []
    assert not session._pending_path.exists()
    run(session.read())
    assert run(session.restore(backup.name))["status"] == "verified"


def fault():
    return OSError(errno.ENOSPC, "synthetic disk full")


@pytest.mark.parametrize("action", ["edit", "undo", "redo"])
def test_failed_draft_persistence_preserves_layout_bytes_and_both_history_stacks(tmp_path, monkeypatch, action):
    source = FrameSource()
    session = Session(source, tmp_path / "data")
    run(session.read())
    pin = lambda cell: session.edit(lambda lay: lay.pin("example.a", 1, cell))
    pin(1)
    pin(2)
    assert session.undo()
    before = reviewed(session)

    def fail():
        raise fault()

    with monkeypatch.context() as patch:
        patch.setattr(session, "_save_draft", fail)
        with pytest.raises(OSError):
            pin(9) if action == "edit" else getattr(session, action)()
    assert reviewed(session) == before
    assert Session(source, session.dir).require().pins == {"example.a": 1}
    assert session.redo() and session.require().pins == {"example.a": 2}
    assert session.undo() and session.undo() and session.require().pins == {}


@pytest.mark.parametrize("operation", ["apply", "restore"])
@pytest.mark.parametrize("action", ["edit", "undo", "redo"])
def test_edits_exclude_active_source_transaction(tmp_path, operation, action):
    source = FrameSource()
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("example.a", 1, 1))
    session.edit(lambda lay: lay.pin("example.a", 1, 2))
    session.undo()
    backup = session._backup(session.require().to_state(), [], "synthetic", [{"example.a": 1}])

    async def scenario():
        writing, resume = asyncio.Event(), asyncio.Event()
        original = source.set_icon_state

        async def write(payload):
            writing.set()
            await resume.wait()
            await original(payload)

        source.set_icon_state = write
        change = session.apply(session.require().token()) if operation == "apply" else session.restore(backup.name)
        task = asyncio.create_task(change)
        await asyncio.wait_for(writing.wait(), 2)
        try:
            with pytest.raises(LayoutError, match="local data lock"):
                session.edit(lambda lay: lay.pin("example.a", 1, 9)) if action == "edit" else getattr(session, action)()
        finally:
            resume.set()
            result = await task
        assert result["status"] == "verified"

    run(scenario())
    session.edit(lambda lay: lay.pin("example.a", 1, 9))
    assert Session(source, session.dir).require().pins == {"example.a": 9}


def test_short_gap_memory_write_preserves_previous_bytes_and_recovery(tmp_path, monkeypatch):
    source = FrameSource(False)
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("example.a", 1, 1))
    assert apply(session)["status"] == "verified"
    session.edit(lambda lay: lay.pin("example.a", 1, 9))
    before, memory = reviewed(session), session._gaps_path
    exclusive, write_text = session_module._exclusive_file, Path.write_text

    def short_file(path, data, **kwargs):
        if path.parent == memory.parent:
            path.write_bytes(b"{")
            raise fault()
        return exclusive(path, data, **kwargs)

    def short_text(path, data, *args, **kwargs):
        if path == memory:
            path.write_bytes(b"{")
            raise fault()
        return write_text(path, data, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(session_module, "_exclusive_file", short_file)
        patch.setattr(Path, "write_text", short_text)
        result = apply(session)
    assert not result["ok"] and result["readback"] == "matched" and result["recovery"] == "read"
    assert result["backup"] in session.backups()
    assert session._pending_path.exists()
    assert reviewed(session) == before
    run(session.read())
    assert not session._pending_path.exists()


def test_explicit_read_recovers_torn_gap_memory(tmp_path):
    source = FrameSource(False)
    session = Session(source, tmp_path / "data")
    run(session.read())
    session._pending_path.write_text(json.dumps({"device_id": "synthetic", "status": "pending"}))
    session._gaps_path.write_bytes(b"{")
    run(session.read())
    assert session.require().pins == {} and not session._pending_path.exists()
    assert apply(session)["status"] == "verified"


def test_failed_directory_sync_cannot_split_draft_from_history(tmp_path, monkeypatch):
    source = FrameSource()
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("example.a", 1, 1))
    before = reviewed(session)
    sync = session_module._sync_directory
    calls = 0

    def isolated_failure(path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise fault()
        return sync(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(session_module, "_sync_directory", isolated_failure)
        with pytest.raises(OSError):
            session.edit(lambda lay: lay.pin("example.a", 1, 9))
    assert reviewed(session) == before
    assert Session(source, session.dir).require().pins == {"example.a": 1}


def test_failed_recovery_read_preserves_legacy_draft_until_pending_record_is_readable(tmp_path):
    session, source = folder_session(tmp_path)
    session.edit(lambda lay: lay.pin("folder:1", 1, 34))
    blob = plistlib.loads(session.draft_path.read_bytes())
    del blob["identity"], blob["source_identity"]
    original = plistlib.dumps(blob)
    session.draft_path.write_bytes(original)
    session = Session(source, session.dir)
    session._pending_path.write_text("{")
    with pytest.raises(ValueError):
        run(session.read())
    assert session.draft_path.read_bytes() == original
    with pytest.raises(LayoutError, match="identity.*read"):
        session.require()
    session._pending_path.write_text(json.dumps({"device_id": "synthetic", "status": "pending"}))
    run(session.read())
    assert apply(session)["status"] == "verified"


@pytest.mark.parametrize("failure", ["unlink", "directory-sync"])
def test_failed_recovery_preserves_legacy_draft_and_pending_bytes(tmp_path, monkeypatch, failure):
    session, source = folder_session(tmp_path)
    backup = session._backup(session.require().to_state(), [], "synthetic")
    session.edit(lambda lay: lay.pin("folder:1", 1, 34))
    blob = plistlib.loads(session.draft_path.read_bytes())
    del blob["identity"], blob["source_identity"]
    original = plistlib.dumps(blob)
    session.draft_path.write_bytes(original)
    session = Session(source, session.dir)
    marker = session._pending_path
    pending = b'{"device_id": "synthetic", "status": "pending"}\n'
    marker.write_bytes(pending)
    unlink, sync = session_module._unlink_file, session_module._sync_directory

    def fail_unlink(path, *args, **kwargs):
        if path == marker and failure == "unlink":
            raise fault()
        return unlink(path, *args, **kwargs)

    def fail_sync(path, **kwargs):
        if path == marker.parent and not marker.exists() and failure == "directory-sync":
            raise fault()
        return sync(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(session_module, "_unlink_file", fail_unlink)
        patch.setattr(session_module, "_sync_directory", fail_sync)
        with pytest.raises(OSError):
            run(session.read())
    assert session.draft_path.read_bytes() == original and marker.read_bytes() == pending
    for using in (session, Session(source, session.dir)):
        with pytest.raises(LayoutError, match="identity.*read"):
            using.require()
        with pytest.raises(LayoutError, match="pending|identity"):
            run(using.restore(backup.name))
    assert source.payloads == [] and session.backups() == [backup.name]
    run(session.read())
    assert not marker.exists() and run(session.restore(backup.name))["status"] == "verified"


@pytest.mark.parametrize("action", ["edit", "undo", "redo"])
def test_committed_history_survives_previous_link_cleanup_failure(tmp_path, monkeypatch, action):
    source = FrameSource()
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("example.a", 1, 1))
    session.edit(lambda lay: lay.pin("example.a", 1, 2))
    session.undo()
    unlink = session_module._unlink_file
    failed = []

    def fail(path, *args, **kwargs):
        if path.name.startswith(".draft.plist.") and path.suffix == ".previous":
            failed.append(path)
            raise OSError(errno.EIO, "synthetic cleanup failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(session_module, "_unlink_file", fail)
        if action == "edit":
            session.edit(lambda lay: lay.pin("example.a", 1, 9))
        else:
            assert getattr(session, action)()
    assert failed
    expected = {} if action == "undo" else {"example.a": 9 if action == "edit" else 2}
    assert session.require().pins == Session(source, session.dir).require().pins == expected
    assert session.history() == ({"undo": 0, "redo": 2} if action == "undo" else {"undo": 2, "redo": 0})
