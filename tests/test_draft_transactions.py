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

    def short_file(path, data):
        if path.parent == memory.parent:
            path.write_bytes(b"{")
            raise fault()
        return exclusive(path, data)

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

    def isolated_failure(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise fault()
        return sync(path)

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
    unlink, sync = Path.unlink, session_module._sync_directory

    def fail_unlink(path, *args, **kwargs):
        if path == marker and failure == "unlink":
            raise fault()
        return unlink(path, *args, **kwargs)

    def fail_sync(path):
        if path == marker.parent and not marker.exists() and failure == "directory-sync":
            raise fault()
        return sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_unlink)
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
    unlink = Path.unlink
    failed = []

    def fail(path, *args, **kwargs):
        if path.name.startswith(".draft.plist.") and path.suffix == ".previous":
            failed.append(path)
            raise OSError(errno.EIO, "synthetic cleanup failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        if action == "edit":
            session.edit(lambda lay: lay.pin("example.a", 1, 9))
        else:
            assert getattr(session, action)()
    assert failed
    expected = {} if action == "undo" else {"example.a": 9 if action == "edit" else 2}
    assert session.require().pins == Session(source, session.dir).require().pins == expected
    assert session.history() == ({"undo": 0, "redo": 2} if action == "undo" else {"undo": 2, "redo": 0})
