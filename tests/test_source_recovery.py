import asyncio
import plistlib

import pytest

from fake_phone import app, folder
from homescreen import ui
from homescreen.device import FileSource, disk_to_model
from homescreen.model import Layout, LayoutError
from homescreen.session import Session
from test_integrity import MemorySource


@pytest.mark.parametrize("operation", ["replace", "reorder-identical"])
def test_review_approval_covers_archive_page_ownership(tmp_path, operation):
    path = tmp_path / "archive.plist"
    pages = [["x"]] if operation == "replace" else [["x"], ["x"]]
    ids = ["A"] if operation == "replace" else ["A", "B"]
    path.write_bytes(plistlib.dumps({"iconLists": pages, "ignored": [], "listUniqueIdentifiers": ids,
                                    "listMetadata": {pid: {"opaque": pid} for pid in ids}}))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    token = session.require().token()
    if operation == "replace":
        session.edit(lambda lay: (lay.insert_page(2), lay.move("x", "page:2"), lay.delete_page(1)))
    else:
        session.edit(lambda lay: lay.move_page(1, 2))
    assert ui.view(session)["diff"]["has_changes"]
    with pytest.raises(LayoutError, match="token"):
        asyncio.run(session.apply(token))
    assert not session.backups() and not session._pending_path.exists()
    assert session.undo()
    assert session.require().token() == token and not ui.view(session)["diff"]["has_changes"]
    assert session.redo()
    assert asyncio.run(session.apply(session.require().token()))["ok"]
    written = plistlib.loads(path.read_bytes())
    if operation == "replace":
        assert written["listUniqueIdentifiers"][0] != "A" and "listMetadata" not in written
    else:
        assert written["listUniqueIdentifiers"] == ["B", "A"]


@pytest.mark.parametrize("reorder", [False, True])
def test_legacy_duplicate_wire_pages_require_known_occurrence_ownership(tmp_path, reorder):
    path = tmp_path / "archive.plist"
    raw = {"iconLists": [["x"], ["x"]], "ignored": [], "listUniqueIdentifiers": ["A", "B"],
           "listMetadata": {"A": {"opaque": b"A"}, "B": {"opaque": b"B"}}}
    path.write_bytes(plistlib.dumps(raw))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    if reorder:
        session.edit(lambda lay: lay.move_page(1, 2))
    blob = plistlib.loads(session.draft_path.read_bytes())
    blob.pop("page_origins")
    blob.pop("page_origins_known")
    session.draft_path.write_bytes(plistlib.dumps(blob))
    session = Session(source, session.dir)
    if reorder:
        with pytest.raises(LayoutError, match="page ownership.*read"):
            asyncio.run(session.apply(session.require().token()))
        assert not session.backups() and not session._pending_path.exists()
        assert plistlib.loads(path.read_bytes()) == raw
    else:
        assert asyncio.run(session.apply(session.require().token()))["ok"]


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
    clip.update(displayName="Second", webClipURL="/two", opaque={"bytes": b"configuration"})
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


def test_legacy_fixture_pending_is_preserved_as_unresolved_evidence(tmp_path, monkeypatch):
    raw = {"iconLists": [["a", "b"]]}
    p = tmp_path / "first" / "phone.plist"
    p.parent.mkdir()
    p.write_bytes(plistlib.dumps(raw))
    session = Session(FileSource(p), tmp_path / "data")
    marker = session._pending_path
    old = b'{"device_id":"fixture-phone","backup":"original.plist","status":"pending"}\n'
    marker.write_bytes(old)
    with monkeypatch.context() as patch:
        def full():
            raise OSError("storage unavailable")
        patch.setattr(session, "_save_draft", full)
        with pytest.raises(OSError):
            asyncio.run(session.read())
    assert marker.read_bytes() == old
    asyncio.run(session.read())
    assert not marker.exists()
    preserved = list((session.dir / "unresolved-writes").glob("*.json"))
    assert len(preserved) == 1 and preserved[0].read_bytes() == old
    assert asyncio.run(session.apply(session.require().token()))["ok"]
    second = tmp_path / "second" / "phone.plist"
    second.parent.mkdir()
    second.write_bytes(plistlib.dumps(raw))
    other = Session(FileSource(second), session.dir)
    asyncio.run(other.read())
    assert other.device_id != session.device_id and not other.backups()
    assert preserved[0].read_bytes() == old


@pytest.mark.parametrize("damage", ["xml", "grid", "pin", "bank", "placement"])
def test_invalid_draft_fields_allow_recovery_without_adopting_damaged_state(tmp_path, damage):
    source = MemorySource()
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    blob = plistlib.loads(session.draft_path.read_bytes())
    if damage == "grid":
        blob["grid"]["rows"] = "six"
    elif damage == "pin":
        blob["pins"] = {"a": "bad"}
    elif damage == "bank":
        blob["bank"] = {}
    elif damage == "placement":
        blob["source_placement"] = "bad"
    damaged = b'<?xml version="1.0"?><plist><dict><key>name' if damage == "xml" else plistlib.dumps(blob)
    session.draft_path.write_bytes(damaged)
    reopened = Session(source, tmp_path)
    assert reopened.layout is None and reopened._draft_error
    assert reopened.draft_path.read_bytes() == damaged
    asyncio.run(reopened.read())
    assert reopened.require().pages[0][0].id == "a"


def test_selected_session_refuses_other_identity_before_draft_adoption(tmp_path):
    source = MemorySource()
    session = Session(source, tmp_path, device="selected")
    with pytest.raises(LayoutError, match="identity"):
        asyncio.run(session.read())
    assert not session.draft_path.exists() and session.layout is None


@pytest.mark.parametrize("operation", ["delete", "move", "insert"])
def test_opaque_page_owners_follow_surviving_pages_after_draft_reload(tmp_path, operation):
    path = tmp_path / "archive.plist"
    raw = {"iconLists": [["a"], ["b"], ["c"]], "ignored": [],
           "listUniqueIdentifiers": ["A", "B", "C"], "listMetadata": {k: {"opaque": k} for k in "ABC"}}
    path.write_bytes(plistlib.dumps(raw))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    if operation == "delete":
        session.edit(lambda lay: lay.delete_page(2))
        owners = ["A", "C"]
    elif operation == "move":
        session.edit(lambda lay: lay.move_page(3, 1))
        owners = ["C", "A", "B"]
    else:
        session.edit(lambda lay: (lay.insert_page(2), lay.move("b", "page:2"), lay.delete_page(3)))
        owners = ["A", None, "C"]
    session = Session(source, session.dir)
    r = asyncio.run(session.apply(session.require().token()))
    assert r["ok"]
    written = plistlib.loads(path.read_bytes())
    assert [written["listMetadata"].get(pid, {}).get("opaque") for pid in written["listUniqueIdentifiers"]] == owners
    assert asyncio.run(session.restore(r["backup"]))["ok"]
    assert plistlib.loads(path.read_bytes())["listUniqueIdentifiers"] == list("ABC")


@pytest.mark.parametrize("regenerate", [False, True])
def test_known_dictionary_page_ids_must_match_on_readback(tmp_path, regenerate):
    path = tmp_path / "archive.plist"
    raw = {"iconLists": [["a"], ["b"]], "listUniqueIdentifiers": ["one", "two"],
           "listMetadata": {} if regenerate else {"one": {"color": "red"}, "two": {"color": "blue"}}}
    path.write_bytes(plistlib.dumps(raw))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    send = source.write
    async def write(*args, **kw):
        await send(*args, **kw)
        content = plistlib.loads(path.read_bytes())
        content["listUniqueIdentifiers"] = ["x", "y"] if regenerate else ["two", "one"]
        path.write_bytes(plistlib.dumps(content))
    source.write = write
    r = asyncio.run(session.apply(session.require().token()))
    assert not r["ok"] and session._pending_path.exists()


@pytest.mark.parametrize("at", [1, 2, 3])
def test_carried_folder_subpage_id_is_checked_at_each_observation(tmp_path, at):
    path = tmp_path / "archive.plist"
    field = {**folder("Work", [app("inside", "Inside")]), "listUniqueIdentifiers": ["alpha"]}
    path.write_bytes(plistlib.dumps({"iconLists": [[field, "spare", "moving"]], "ignored": []}))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda l: l.move("moving", "dock"))
    read = source.read
    calls = 0
    async def changed():
        nonlocal calls
        calls += 1
        if calls == at:
            raw = plistlib.loads(path.read_bytes())
            raw["iconLists"][0][0]["listUniqueIdentifiers"] = ["beta"]
            path.write_bytes(plistlib.dumps(raw))
        return await read()
    source.read = changed
    if at == 3:
        assert not asyncio.run(session.apply(session.require().token()))["ok"]
    else:
        with pytest.raises(LayoutError, match="changed"):
            asyncio.run(session.apply(session.require().token()))
        assert not session.backups()


@pytest.mark.parametrize("operation", ["grow", "shrink-last", "shrink-first"])
def test_folder_page_owners_survive_intentional_pagination(tmp_path, operation):
    path = tmp_path / "archive.plist"
    field = {"listType": "folder", "displayName": "Work", "uniqueIdentifier": "owner",
             "iconLists": [["inside"], ["last"]], "listUniqueIdentifiers": ["first", "last"],
             "listMetadata": {"first": {"opaque": b"first"}, "last": {"opaque": b"last"}}}
    if operation == "grow":
        field["iconLists"].pop()
        field["listUniqueIdentifiers"].pop()
        field["listMetadata"].pop("last")
    path.write_bytes(plistlib.dumps({"iconLists": [[field, "moving", "keep"]], "ignored": []}))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    if operation == "grow":
        session.edit(lambda lay: lay.move("moving", "folder:owner:2"))
    else:
        session.edit(lambda lay: lay.move("last" if operation == "shrink-last" else "inside", "dock"))
    reviewed = session.require().to_state()[1][0]
    ids = reviewed["listUniqueIdentifiers"]
    if operation == "grow":
        assert len(ids) == 2 and ids[0] == "first" and ids[1] != "first"
    else:
        assert ids == (["first"] if operation == "shrink-last" else ["last"])
        assert reviewed["listMetadata"] == {ids[0]: {"opaque": ids[0].encode()}}
    reopened = Session(source, session.dir)
    assert reopened.require().to_state()[1][0] == reviewed
    result = asyncio.run(reopened.apply(reopened.require().token()))
    assert result["ok"] and not reopened._pending_path.exists()
    written = plistlib.loads(path.read_bytes())["iconLists"][0][0]
    assert written["listUniqueIdentifiers"] == ids
    assert written["listMetadata"] == reviewed["listMetadata"]
    assert written["iconLists"] == {"grow": [["inside"], ["moving"]], "shrink-last": [["inside"]],
                                     "shrink-first": [["last"]]}[operation]


def test_explicit_archive_library_readback_cannot_claim_clip_from_memory(tmp_path):
    path = tmp_path / "archive.plist"
    clip = {"displayIdentifier": "clip", "webClipURL": "/clip", "displayName": "Clip"}
    path.write_bytes(plistlib.dumps({"iconLists": [[clip, "keep"]], "ignored": []}))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda l: l.move("clip", "library"))
    send = source.write
    async def discard(*args, **kw):
        await send(*args, **kw)
        raw = plistlib.loads(path.read_bytes())
        raw["ignored"] = []
        path.write_bytes(plistlib.dumps(raw))
    source.write = discard
    result = asyncio.run(session.apply(session.require().token()))
    assert not result["ok"] and session._pending_path.exists()


@pytest.mark.parametrize("at", [0, 1, 2])
@pytest.mark.parametrize("draft", ["live", "reopened", "legacy"])
def test_prewrite_compares_reported_library_clips_before_memory_enrichment(tmp_path, at, draft):
    path = tmp_path / "archive.plist"
    clip = {"displayIdentifier": "clip", "webClipURL": "/clip", "displayName": "Clip", "opaque": b"data"}
    path.write_bytes(plistlib.dumps({"iconLists": [["moving", "keep"]], "ignored": [clip]}))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda lay: lay.move("moving", "dock"))
    if draft == "legacy":
        blob = plistlib.loads(session.draft_path.read_bytes())
        blob.pop("source_reported_clips", None)
        session.draft_path.write_bytes(plistlib.dumps(blob))
    if draft != "live":
        session = Session(source, session.dir)
    before = session.draft_path.read_bytes(), session.history()
    read = source.read
    calls = 0
    async def changed():
        nonlocal calls
        calls += 1
        if calls == at:
            raw = plistlib.loads(path.read_bytes())
            raw["ignored"] = []
            path.write_bytes(plistlib.dumps(raw))
        return await read()
    source.read = changed
    if at:
        with pytest.raises(LayoutError, match="changed"):
            asyncio.run(session.apply(session.require().token()))
        assert not session.backups() and not session._pending_path.exists()
        assert (session.draft_path.read_bytes(), session.history()) == before
        assert plistlib.loads(path.read_bytes())["ignored"] == []
    else:
        assert asyncio.run(session.apply(session.require().token()))["ok"]
        assert plistlib.loads(path.read_bytes())["ignored"] == [clip]


def test_explicit_read_can_adopt_a_remembered_only_archive_clip(tmp_path):
    path = tmp_path / "archive.plist"
    clip = {"displayIdentifier": "clip", "webClipURL": "/clip", "displayName": "Clip", "opaque": b"data"}
    raw = {"iconLists": [["moving", "keep"]], "ignored": [clip]}
    path.write_bytes(plistlib.dumps(raw))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    raw["ignored"] = []
    path.write_bytes(plistlib.dumps(raw))
    asyncio.run(session.read())
    session = Session(source, session.dir)
    session.edit(lambda lay: lay.move("moving", "dock"))
    assert asyncio.run(session.apply(session.require().token()))["ok"]
    assert plistlib.loads(path.read_bytes())["ignored"] == [clip]


def test_failed_explicit_read_rolls_back_learned_cache_and_read_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "fixture.plist"
    source = FileSource(path)
    raw = {"iconLists": [["a"]], "ignored": []}
    path.write_bytes(plistlib.dumps(raw))
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    before = {p.relative_to(session.dir): p.read_bytes() for p in session.dir.rglob("*") if p.is_file()}
    raw["iconLists"][0].append({"displayIdentifier": "clip", "webClipURL": "/changed"})
    path.write_bytes(plistlib.dumps(raw))
    def fail():
        raise OSError("draft unavailable")
    monkeypatch.setattr(session, "_save_draft", fail)
    with pytest.raises(OSError):
        asyncio.run(session.read())
    assert {p.relative_to(session.dir): p.read_bytes() for p in session.dir.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("backup", ["other/saved.plist", "../saved.plist"])
def test_invalid_legacy_marker_keeps_its_gate(tmp_path, backup):
    import json
    path = tmp_path / "fixture.plist"
    path.write_bytes(plistlib.dumps([[], [app("a", "A")]]))
    session = Session(FileSource(path), tmp_path / "data")
    pending = json.dumps({"device_id": "fixture-fixture", "status": "pending", "backup": backup}).encode()
    session._pending_path.write_bytes(pending)
    asyncio.run(session.read())
    assert session._pending_path.read_bytes() == pending
    with pytest.raises(LayoutError, match="pending"):
        asyncio.run(session.apply(session.require().token()))


def test_failed_marker_retirement_unpublishes_new_legacy_evidence(tmp_path, monkeypatch):
    path = tmp_path / "fixture.plist"
    path.write_bytes(plistlib.dumps([[], [app("a", "A")]]))
    session = Session(FileSource(path), tmp_path / "data")
    old = b'{"device_id":"fixture-fixture","backup":"saved.plist","status":"pending"}'
    session._pending_path.write_bytes(old)
    def fail():
        raise OSError("storage unavailable")
    monkeypatch.setattr(session, "_clear_pending", fail)
    with pytest.raises(OSError):
        asyncio.run(session.read())
    assert session._pending_path.read_bytes() == old and not list((session.dir / "unresolved-writes").glob("*.json"))


def test_restore_refuses_unrelated_raw_page_attached_to_the_model(tmp_path):
    path = tmp_path / "archive.plist"
    raw = {"iconLists": [["a"]], "listUniqueIdentifiers": ["p"], "listMetadata": {"p": {"opaque": True}}}
    path.write_bytes(plistlib.dumps(raw))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    approved = asyncio.run(session.apply(session.require().token()))
    backup = session._dev_dir("backups") / approved["backup"]
    content = plistlib.loads(backup.read_bytes())
    content["source_archive"]["iconLists"] = [["other"]]
    backup.write_bytes(plistlib.dumps(content))
    before = path.read_bytes()
    with pytest.raises(LayoutError, match="backup"):
        asyncio.run(session.restore(backup.name))
    assert path.read_bytes() == before and not session._pending_path.exists()
