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
