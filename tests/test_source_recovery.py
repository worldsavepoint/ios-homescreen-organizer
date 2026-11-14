import asyncio
import plistlib

import pytest

from fake_phone import app, folder
from homescreen.device import FileSource, disk_to_model
from homescreen.model import Layout, LayoutError
from homescreen.session import Session
from test_integrity import MemorySource


@pytest.mark.parametrize(("state", "library"), [
    ([{}, []], []), ([[], "BAD"], []), ([[], [True]], "BAD"),
    ([[], [{"listType": "folder", "iconLists": ["BAD"]}]], []),
    ([[], [{"listType": "folder", "iconLists": {}}]], []),
])
def test_malformed_read_preserves_saved_draft_and_history(tmp_path, state, library):
    source = MemorySource()
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    session.edit(lambda lay: lay.move("b", "dock"))
    before = session.draft_path.read_bytes(), session.history(), session.require().token()

    async def malformed():
        return state, library

    source.read = malformed
    with pytest.raises(LayoutError):
        asyncio.run(session.read())
    assert before == (session.draft_path.read_bytes(), session.history(), session.require().token())
    assert Layout([[], [True, 3, "item"]]).to_state() == [[], [True, 3, "item"]]


@pytest.mark.parametrize("bad", [
    {"buttonBar": "BAD"}, {"iconLists": "BAD"}, {"iconLists": [{"x": 1}]},
    {"ignored": "BAD"}, {"iconLists": [[{"listType": "folder", "iconLists": ["BAD"]}]]},
])
def test_archive_containers_are_validated_before_conversion(bad):
    raw = {"buttonBar": [], "iconLists": [["app", False]], "ignored": [], **bad}
    with pytest.raises(LayoutError):
        disk_to_model(raw)


@pytest.mark.parametrize("bad", [b"bplist00truncated", plistlib.dumps({}), plistlib.dumps([])])
def test_explicit_read_recovers_corrupt_saved_draft_without_losing_failed_recovery(tmp_path, bad):
    source = MemorySource()
    path = tmp_path / "draft.plist"
    path.write_bytes(bad)
    session = Session(source, tmp_path)
    with pytest.raises(LayoutError, match="read"):
        session.require()
    source.unreadable = True
    with pytest.raises(ConnectionError):
        asyncio.run(session.read())
    assert path.read_bytes() == bad
    source.unreadable = False
    asyncio.run(session.read())
    assert session.require().pages[0][0].id == "a"
    assert Session(source, tmp_path).require().token() == session.require().token()


def test_hidden_clip_uses_latest_explicit_observation_after_reopen(tmp_path):
    path = tmp_path / "fixture.plist"
    clip = {"displayIdentifier": "clip", "displayName": "First", "webClipURL": "/one"}
    raw = {"buttonBar": [], "iconLists": [[clip, "other"]], "ignored": []}
    path.write_bytes(plistlib.dumps(raw))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    clip.update(displayName="Second", webClipURL="/two")
    path.write_bytes(plistlib.dumps(raw))
    asyncio.run(session.read())
    raw["iconLists"][0].remove(clip)
    path.write_bytes(plistlib.dumps(raw))
    reopened = Session(source, session.dir)
    asyncio.run(reopened.read())
    assert reopened.require().library[0].raw == clip


@pytest.mark.parametrize("pins", [False, True])
def test_dictionary_recovery_preserves_original_page_records(tmp_path, pins):
    path = tmp_path / "archive.plist"
    first = {"opaque": {"value": b"one"}}
    if pins:
        first["fixedLocations"] = {"a": 4}
    last = {"opaque": {"value": b"two"}}
    raw = {"buttonBar": [], "iconLists": [["a", "b"], ["c"]], "ignored": [],
           "listUniqueIdentifiers": ["first", "last"], "listMetadata": {"first": first, "last": last},
           "unrelated": b"original"}
    path.write_bytes(plistlib.dumps(raw))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda lay: (lay.move("b", "dock"), lay.delete_page(2)))
    result = asyncio.run(session.apply(session.require().token()))
    assert result["ok"]
    changed = plistlib.loads(path.read_bytes())
    assert changed["listMetadata"]["first"]["opaque"] == first["opaque"]
    backup = plistlib.loads((session._dev_dir("backups") / result["backup"]).read_bytes())
    assert backup["source_archive"] == raw
    changed["unrelated"] = b"updated"
    path.write_bytes(plistlib.dumps(changed))
    assert asyncio.run(session.restore(result["backup"]))["ok"]
    actual = plistlib.loads(path.read_bytes())
    assert actual["listUniqueIdentifiers"] == ["first", "last"]
    assert actual["listMetadata"]["first"]["opaque"] == first["opaque"]
    assert actual["listMetadata"]["last"] == last
    assert actual["unrelated"] == b"updated"


@pytest.mark.parametrize("kind", ["clip", "folder", "app"])
@pytest.mark.parametrize("read_number", [1, 2, 3])
def test_dictionary_carried_fields_are_checked_through_last_observation(tmp_path, kind, read_number):
    path = tmp_path / "archive.plist"
    owner = {"clip": {"displayIdentifier": "clip", "webClipURL": "/before", "displayName": "Clip"},
             "app": app("a", "A"), "folder": folder("Folder", [app("inside", "Inside")])}[kind]
    owner["exported"] = {"value": b"old"}
    raw = {"iconLists": [[owner, "moving", "keep"]], "ignored": []}
    path.write_bytes(plistlib.dumps(raw))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda lay: lay.move("moving", "dock"))
    original = source.read
    count = 0

    async def read():
        nonlocal count
        count += 1
        if count == read_number:
            changed = plistlib.loads(path.read_bytes())
            changed["iconLists"][0][0]["exported"] = {"value": b"new"}
            if kind == "clip":
                changed["iconLists"][0][0].update(webClipURL="/changed", displayName="Changed")
            path.write_bytes(plistlib.dumps(changed))
        return await original()

    source.read = read
    if read_number < 3:
        with pytest.raises(LayoutError, match="changed"):
            asyncio.run(session.apply(session.require().token()))
        assert not session.backups() and not session._pending_path.exists()
    else:
        result = asyncio.run(session.apply(session.require().token()))
        assert not result["ok"] and session._pending_path.exists()
    assert plistlib.loads(path.read_bytes())["iconLists"][0][0]["exported"] == {"value": b"new"}


def test_readback_requires_the_retained_opaque_page_fields(tmp_path):
    path = tmp_path / "archive.plist"
    raw = {"iconLists": [["a", "b"]], "listUniqueIdentifiers": ["p"], "listMetadata": {"p": {"unknown": b"data"}}}
    path.write_bytes(plistlib.dumps(raw))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda lay: lay.move("a", "dock"))
    send = source.write

    async def write(*args):
        await send(*args)
        changed = plistlib.loads(path.read_bytes())
        changed["listMetadata"]["p"].pop("unknown")
        path.write_bytes(plistlib.dumps(changed))

    source.write = write
    result = asyncio.run(session.apply(session.require().token()))
    assert not result["ok"] and session._pending_path.exists()
    assert result["backup"] in session.backups()
