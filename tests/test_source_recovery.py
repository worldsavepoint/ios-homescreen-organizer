import asyncio
import plistlib

import pytest

from fake_phone import app, folder
from homescreen import ui
from homescreen.device import FileSource, NAME_MARK, disk_to_model
from homescreen.model import Layout, LayoutError
from homescreen.session import Session
from test_integrity import MemorySource


@pytest.mark.parametrize("dictionary,default,rename", [
    (False, "Utilities", None), (True, None, None), (True, None, "Folder"), (True, "Utilities", "Utilities"),
])
def test_folder_names_keep_wire_presence_after_reopen_and_apply(tmp_path, dictionary, default, rename):
    path = tmp_path / "source.plist"
    raw = {"listType": "folder", "iconLists": [[app("child", "Child")]]}
    if default:
        raw["defaultDisplayName"] = default
    page = [raw, app("moving", "Moving")]
    source = {"buttonBar": [], "iconLists": [page], "ignored": []} if dictionary else [[], page]
    path.write_bytes(plistlib.dumps(source))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    item = session.require().pages[0][0]
    assert item.name == (default or "Folder")
    if rename is not None:
        session.edit(lambda lay: lay.rename_folder(item.id, rename))
    session.edit(lambda lay: lay.move("moving", "dock"))
    restarted = Session(FileSource(path), session.dir)
    assert asyncio.run(restarted.apply(restarted.require().token()))["ok"]
    saved = plistlib.loads(path.read_bytes())
    written = (saved["iconLists"][0] if dictionary else saved[1])[0]
    assert written == (raw if rename is None else {**raw, "displayName": rename})


@pytest.mark.parametrize("saved_name,explicit", [("Utilities", None), ("Chosen", "Chosen")])
def test_legacy_draft_name_marker_keeps_an_explicit_rename(saved_name, explicit):
    raw = {"listType": "folder", "iconLists": [[]], "defaultDisplayName": "Utilities",
           "displayName": saved_name, "__nameFromDefault__": True}
    layout = Layout([[], [raw]])
    assert layout.pages[0][0].name == saved_name
    emitted = layout.to_state()[1][0]
    assert "__nameFromDefault__" not in emitted
    assert emitted.get("displayName") == explicit and ("displayName" in emitted) == (explicit is not None)


@pytest.mark.parametrize("damage", ["none", "empty-page", "default-name"])
def test_readback_preserves_unidentified_folder_subpages_and_implicit_name(tmp_path, damage):
    path = tmp_path / "archive.plist"
    wire = {"listType": "folder", "defaultDisplayName": "Tools", "iconLists": [[], ["inside"]]}
    path.write_bytes(plistlib.dumps({"iconLists": [[wire, "move"]], "ignored": []}))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda layout: layout.move("move", "dock"))
    write = source.write
    async def persist(*args, **kwargs):
        await write(*args, **kwargs)
        archive = plistlib.loads(path.read_bytes())
        folder = archive["iconLists"][0][0]
        if damage == "empty-page":
            folder["iconLists"] = folder["iconLists"][1:]
        elif damage == "default-name":
            folder["displayName"] = folder["defaultDisplayName"]
        path.write_bytes(plistlib.dumps(archive))
    source.write = persist
    result = asyncio.run(session.apply(session.require().token()))
    assert result["ok"] is (damage == "none")
    assert session._pending_path.exists() is (damage != "none")
    assert result["backup"] in session.backups()


@pytest.mark.parametrize("operation", ["apply", "restore"])
@pytest.mark.parametrize("change", ["keep", "remove", "type"])
def test_verification_checks_today_from_the_live_write(tmp_path, operation, change):
    path = tmp_path / "source.plist"
    path.write_bytes(plistlib.dumps({"iconLists": [["a", "b"]], "ignored": [], "today": ["original"]}))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda layout: layout.move("b", "dock"))
    if operation == "restore":
        saved = asyncio.run(session.apply(session.require().token()))["backup"]
    live = plistlib.loads(path.read_bytes())
    live["today"] = [{"value": 1}]
    path.write_bytes(plistlib.dumps(live))
    write = source.write
    async def persist(*args, **kwargs):
        await write(*args, **kwargs)
        archive = plistlib.loads(path.read_bytes())
        assert archive["today"] == [{"value": 1}] and type(archive["today"][0]["value"]) is int
        if change == "remove":
            del archive["today"]
        if change == "type":
            archive["today"][0]["value"] = True
        path.write_bytes(plistlib.dumps(archive))
    source.write = persist
    result = asyncio.run(session.restore(saved) if operation == "restore" else session.apply(session.require().token()))
    assert result["ok"] is (change == "keep") and result["backup"] in session.backups()
    if change != "keep":
        assert result["status"] == "unknown" and "Today View" in result["error"]
        assert result["recovery"] == "read" and session._pending_path.exists()


def test_legacy_draft_gap_capability_requires_its_own_observation(tmp_path):
    path = tmp_path / "source.plist"
    path.write_bytes(plistlib.dumps({"iconLists": [["a"]], "ignored": []}))
    source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    assert session.gaps_readable and Session(FileSource(path), session.dir).gaps_readable
    draft = plistlib.loads(session.draft_path.read_bytes())
    for field in ("source_placement", "source_pins_reported", "source_pin_map"):
        draft.pop(field, None)
    session.draft_path.write_bytes(plistlib.dumps(draft))
    assert not Session(source, session.dir).gaps_readable


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


@pytest.mark.parametrize(("key", "value", "valid"), [
    ("DeviceName", b"bad", False), ("ProductType", 42, False), ("ProductVersion", ["17"], False),
    ("extra", b"bad", False), ("DeviceName", "Synthetic", True), ("ProductType", None, True),
])
def test_stored_device_info_keeps_the_json_recovery_view_available(tmp_path, key, value, valid):
    source = MemorySource()
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    body = plistlib.loads(session.draft_path.read_bytes())
    body["info"][key] = value
    damaged = plistlib.dumps(body, fmt=plistlib.FMT_BINARY)
    session.draft_path.write_bytes(damaged)
    session = Session(source, tmp_path)
    viewed = ui.view(session)
    assert ui.JSONResponse(viewed).status_code == 200
    assert viewed["loaded"] is valid
    assert session.draft_path.read_bytes() == damaged
    if not valid:
        assert "read" in viewed["draft_error"]
        asyncio.run(session.read())
        assert ui.JSONResponse(ui.view(session)).status_code == 200 and session.require()


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


@pytest.mark.parametrize("members", [
    '"device_id":"fixture-phone","backup":"other/previous.plist","backup":"previous.plist"',
    '"device_id":"fixture-phone","backup":"previous.plist","backup":"other/previous.plist"',
    '"device_id":"foreign","device_id":"fixture-phone","backup":"previous.plist"',
    '"device_id":"fixture-phone","device_id":"foreign","backup":"previous.plist"',
])
def test_duplicate_legacy_marker_members_retain_the_raw_write_gate(tmp_path, members):
    path = tmp_path / "phone.plist"
    path.write_bytes(plistlib.dumps([[], [app("a", "A")]]))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    marker = ("{" + members + ',"status":"pending"}\n').encode()
    session._pending_path.write_bytes(marker)
    draft = session.draft_path.read_bytes()
    with pytest.raises(LayoutError, match="repeated|duplicate"):
        asyncio.run(session.read())
    assert session._pending_path.read_bytes() == marker and session.draft_path.read_bytes() == draft
    assert not list((session.dir / "unresolved-writes").glob("*.json"))
    with pytest.raises(LayoutError, match="pending|unknown"):
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


@pytest.mark.parametrize("damage", ["duplicate-ids", "typed-ids", "record-owner", "frame-type", "pin-value", "valid"])
def test_historical_raw_backup_validates_page_evidence_before_restore(tmp_path, damage):
    path = tmp_path / "archive.plist"
    raw = {"iconLists": [["a"], ["b"]], "ignored": [], "listUniqueIdentifiers": ["A", "B"],
           "listMetadata": {"A": {"fixedLocations": {"a": 3}, "fixedLocationsGridColumns": 4,
                                   "fixedLocationsGridRows": 6}, "B": {"opaque": b"B"}}}
    path.write_bytes(plistlib.dumps(raw))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    backup = session._backup(session.require().to_state(), [], "older", [{"a": 3}, {}],
                             observed=session.require(), archive=raw)
    body = plistlib.loads(backup.read_bytes())
    body["provenance"].pop("archive_pins_reported")
    archive = body["source_archive"]
    if damage == "duplicate-ids":
        archive["listUniqueIdentifiers"] = ["A", "A"]
    elif damage == "typed-ids":
        archive["listUniqueIdentifiers"] = [False, True]
    elif damage == "record-owner":
        archive["listMetadata"]["unrelated"] = {"opaque": b"data"}
    elif damage == "frame-type":
        archive["listMetadata"]["A"]["fixedLocationsGridColumns"] = True
    elif damage == "pin-value":
        archive["listMetadata"]["A"]["fixedLocations"]["a"] = 2
    backup.write_bytes(plistlib.dumps(body))
    before = path.read_bytes(), session.backups()
    if damage == "valid":
        assert asyncio.run(session.restore(backup.name))["ok"]
    else:
        with pytest.raises(LayoutError):
            asyncio.run(session.restore(backup.name))
        assert (path.read_bytes(), session.backups()) == before
        assert not session._pending_path.exists()


def test_historical_raw_backup_without_reported_pins_can_keep_remembered_positions(tmp_path):
    path = tmp_path / "archive.plist"
    raw = {"iconLists": [["a", "b"]], "ignored": [], "listUniqueIdentifiers": ["A"]}
    path.write_bytes(plistlib.dumps(raw))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    backup = session._backup(session.require().to_state(), [], "older", [{"a": 4}], archive=raw)
    body = plistlib.loads(backup.read_bytes())
    body["provenance"].pop("archive_pins_reported")
    backup.write_bytes(plistlib.dumps(body))
    assert asyncio.run(session.restore(backup.name))["ok"]
    assert plistlib.loads(path.read_bytes())["listMetadata"]["A"]["fixedLocations"] == {"a": 4}


@pytest.mark.parametrize("draft_kind", ["fresh", "recorded-omission", "unmarked"])
def test_old_list_draft_omitted_wire_name(tmp_path, draft_kind):
    path = tmp_path / "list.plist"
    folder = {"listType": "folder", "defaultDisplayName": "Utilities", "iconLists": [[app("inside", "Inside")]]}
    path.write_bytes(plistlib.dumps([[], [folder, app("move", "Move")]]))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    if draft_kind != "fresh":
        # cde1d8eb Layout._wrap's list fallback was 'folder', and _unwrap always inserted it.
        draft = plistlib.loads(session.draft_path.read_bytes())
        if draft_kind == "unmarked":
            del draft["saved_folder_fields"]
        draft["state"][1][0]["displayName"] = "folder"
        session.draft_path.write_bytes(plistlib.dumps(draft))
    saved, source = session.draft_path.read_bytes(), path.read_bytes()
    reopened = Session(FileSource(path), session.dir)
    if draft_kind == "unmarked":
        with pytest.raises(LayoutError, match="folder name.*read"):
            reopened.require()
        assert reopened.draft_path.read_bytes() == saved and path.read_bytes() == source
        assert not reopened.backups() and not reopened._pending_path.exists()
        return
    name = reopened.require().pages[0][0].name
    reopened.edit(lambda lay: lay.move("move", "dock"))
    result = asyncio.run(reopened.apply(reopened.require().token()))
    wire = plistlib.loads(path.read_bytes())[1][0]
    assert result["ok"] and wire == folder and name == "Utilities"


@pytest.mark.parametrize("old", [False, True])
def test_old_dictionary_draft_preserves_untouched_subpage_and_metadata(tmp_path, old):
    path = tmp_path / "layout.plist"
    folder = {"displayName": "Work", "listType": "folder", "uniqueIdentifier": "work",
              "iconLists": [[], ["inside"]], "listUniqueIdentifiers": ["empty", "used"],
              "listMetadata": {"empty": {"opaque": "quiet"}, "used": {"opaque": "active"}}}
    path.write_bytes(plistlib.dumps({"buttonBar": [], "iconLists": [[folder, "move"]], "ignored": []}))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    if old:
        # The old _unwrap filtered both children and listUniqueIdentifiers; it kept listMetadata.
        draft = plistlib.loads(session.draft_path.read_bytes())
        draft.pop("saved_folder_fields", None)
        item = draft["state"][1][0]
        item["iconLists"] = item["iconLists"][1:]
        item["listUniqueIdentifiers"] = item["listUniqueIdentifiers"][1:]
        session.draft_path.write_bytes(plistlib.dumps(draft))
    reopened = Session(FileSource(path), session.dir)
    reopened.edit(lambda lay: lay.move("move", "dock"))
    result = asyncio.run(reopened.apply(reopened.require().token()))
    wire = plistlib.loads(path.read_bytes())["iconLists"][0][0]
    backup = plistlib.loads((reopened._dev_dir("backups") / result["backup"]).read_bytes())
    restore = asyncio.run(reopened.restore(result["backup"]))
    assert backup["source_archive"]["iconLists"][0][0] == folder
    assert result["ok"] and restore["ok"]
    assert wire == folder


@pytest.mark.parametrize("retired,expected", [([], ["A", "B", "C"]), (["A", "B"], ["C"]), (["A"], None)])
def test_old_folder_page_metadata_distinguishes_retirement_from_filtering(tmp_path, retired, expected):
    path = tmp_path / "layout.plist"
    folder = {"listType": "folder", "displayName": "Work", "uniqueIdentifier": "work",
              "iconLists": [[], [], ["inside"]], "listUniqueIdentifiers": ["A", "B", "C"],
              "listMetadata": {k: {"opaque": k} for k in ("A", "B", "C")}}
    path.write_bytes(plistlib.dumps({"iconLists": [[folder, "move", "stay"]], "ignored": []}))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda layout: layout.move("folder:work", "dock"))
    draft = plistlib.loads(session.draft_path.read_bytes())
    draft.pop("saved_folder_fields", None)
    item = draft["state"][0][0]
    item["iconLists"] = item["iconLists"][2:]
    item["listUniqueIdentifiers"] = ["C"]
    for key in retired:
        del item["listMetadata"][key]
    saved = plistlib.dumps(draft)
    session.draft_path.write_bytes(saved)
    reopened = Session(FileSource(path), session.dir)
    if expected is None:
        original = path.read_bytes()
        with pytest.raises(LayoutError, match="folder.*read"):
            asyncio.run(reopened.apply(reopened.require().token()))
        assert reopened.draft_path.read_bytes() == saved and path.read_bytes() == original
        assert not reopened.backups() and not reopened._pending_path.exists()
    else:
        reopened.edit(lambda layout: layout.move("move", "dock"))
        assert asyncio.run(reopened.apply(reopened.require().token()))["ok"]
        written = plistlib.loads(path.read_bytes())["buttonBar"][0]
        assert written["listUniqueIdentifiers"] == expected
        assert set(written["listMetadata"]) == set(expected)


@pytest.mark.parametrize("intent", ["fresh-explicit", "historical-ambiguous", "historical-rename"])
def test_saved_folder_name_preserves_explicit_and_ambiguous_intent(tmp_path, intent):
    path = tmp_path / "list.plist"
    folder = {"listType": "folder", "defaultDisplayName": "folder", "iconLists": [[app("child", "Child")]]}
    path.write_bytes(plistlib.dumps([[], [folder, app("move", "Move")]]))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    target = "Chosen" if intent == "historical-rename" else "folder"
    session.edit(lambda layout: layout.rename_folder(layout.pages[0][0].id, target))
    if intent != "fresh-explicit":
        draft = plistlib.loads(session.draft_path.read_bytes())
        draft.pop("saved_folder_fields", None)
        session.draft_path.write_bytes(plistlib.dumps(draft))
    saved = session.draft_path.read_bytes()
    reopened = Session(FileSource(path), session.dir)
    if intent == "historical-ambiguous":
        with pytest.raises(LayoutError, match="folder name.*read"):
            reopened.require()
        assert reopened.draft_path.read_bytes() == saved and not reopened.backups()
        asyncio.run(reopened.read())
        assert "displayName" not in reopened.require().to_state()[1][0]
    else:
        reopened.edit(lambda layout: layout.move("move", "dock"))
        assert asyncio.run(reopened.apply(reopened.require().token()))["ok"]
        assert plistlib.loads(path.read_bytes())[1][0]["displayName"] == target


@pytest.mark.parametrize("twins", [False, True])
def test_historical_moved_folder_needs_a_unique_owner_without_local_ids(tmp_path, twins):
    path = tmp_path / "list.plist"
    def unnamed(child):
        return {"listType": "folder", "defaultDisplayName": "Tools", "iconLists": [[app(child, child)]]}
    path.write_bytes(plistlib.dumps([[], [unnamed("a"), unnamed("a" if twins else "b"), app("stay", "Stay")]]))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda layout: layout.move(layout.pages[0][0].id, "dock"))
    draft = plistlib.loads(session.draft_path.read_bytes())
    for key in ("identity", "source_identity", "saved_folder_fields"):
        draft.pop(key, None)
    draft["state"][0][0]["displayName"] = "folder"
    saved = plistlib.dumps(draft)
    session.draft_path.write_bytes(saved)
    reopened = Session(FileSource(path), session.dir)
    with pytest.raises(LayoutError, match="ownership.*read" if twins else "folder name.*read"):
        reopened.require()
    assert reopened.draft_path.read_bytes() == saved and not reopened.backups()


@pytest.mark.parametrize("representation", ["marked-dictionary", "dictionary", "list"])
@pytest.mark.parametrize("chosen", ["Chosen", "folder"])
def test_unmarked_old_draft_retains_an_explicit_lowercase_rename(tmp_path, representation, chosen):
    path = tmp_path / "source.plist"
    f = {"listType": "folder", "defaultDisplayName": "Utilities", "iconLists": [[{"displayIdentifier": "inside"}]]}
    apps = [f, {"displayIdentifier": "move"}, {"displayIdentifier": "stay"}]
    archive = {"buttonBar": [], "iconLists": [apps], "ignored": []} if representation != "list" else [[], apps]
    path.write_bytes(plistlib.dumps(archive))
    s = Session(FileSource(path), tmp_path / "data")
    asyncio.run(s.read())
    def edited(l):
        l.rename_folder(l.pages[0][0].id, chosen)
        l.move("move", "dock")
    s.edit(edited)
    blob = plistlib.loads(s.draft_path.read_bytes())
    del blob["saved_folder_fields"]
    if representation == "marked-dictionary":
        blob.pop("source_placement", None)
        # cde1's dictionary reader carried the synthetic marker in both raw projections;
        # its independent source explicitly records the actual prior default.
        blob["source"][1][0].update(displayName="Utilities", **{NAME_MARK: True})
        blob["state"][1][0][NAME_MARK] = True
    stored = plistlib.dumps(blob)
    s.draft_path.write_bytes(stored)
    reopened = Session(FileSource(path), s.dir)
    if representation == "list" and chosen == "folder":
        with pytest.raises(LayoutError, match="folder name.*read"):
            reopened.require()
        assert reopened.draft_path.read_bytes() == stored and path.read_bytes() == plistlib.dumps(archive)
        assert not reopened.backups() and not reopened._pending_path.exists()
        return
    shown = reopened.require().pages[0][0].name
    result = asyncio.run(reopened.apply(reopened.require().token()))
    written = plistlib.loads(path.read_bytes())
    actual = written["iconLists"][0][0] if representation != "list" else written[1][0]
    assert result["ok"] and actual.get("displayName") == chosen and shown == chosen


@pytest.mark.parametrize("metadata,recorded", [(True, False), (False, False), (False, True)])
def test_intentional_empty_page_retirement_survives_old_draft(tmp_path, metadata, recorded):
    path = tmp_path / "source.plist"
    f = {"listType": "folder", "displayName": "Filed", "uniqueIdentifier": "f",
         "iconLists": [[], ["inside"]], "listUniqueIdentifiers": ["empty", "used"]}
    if metadata:
        f["listMetadata"] = {"empty": {"owner": "discarded"}, "used": {"owner": "retained"}}
    archive = {"buttonBar": [], "iconLists": [[f, "move", "stay"]], "ignored": []}
    source_bytes = plistlib.dumps(archive)
    path.write_bytes(source_bytes)
    s = Session(FileSource(path), tmp_path / "data")
    asyncio.run(s.read())
    def edit(l):
        l.drop_empty()
        l.move("move", "dock")
    s.edit(edit)
    blob = plistlib.loads(s.draft_path.read_bytes())
    assert blob["state"][1][0]["listUniqueIdentifiers"] == ["used"]
    if not recorded:
        del blob["saved_folder_fields"]
    stored = plistlib.dumps(blob)
    s.draft_path.write_bytes(stored)
    reopened = Session(FileSource(path), s.dir)
    if not metadata and not recorded:
        with pytest.raises(LayoutError, match="folder pages.*read"):
            reopened.require()
        assert reopened.draft_path.read_bytes() == stored and path.read_bytes() == source_bytes
        assert not reopened.backups() and not reopened._pending_path.exists()
        return
    result = asyncio.run(reopened.apply(reopened.require().token()))
    written = plistlib.loads(path.read_bytes())["iconLists"][0][0]["listUniqueIdentifiers"]
    assert result["ok"] and written == ["used"]


@pytest.mark.parametrize("metadata", [False, True])
def test_historical_retirement_with_an_appended_page_needs_owner_evidence(tmp_path, metadata):
    path = tmp_path / "layout.plist"
    folder = {"listType": "folder", "displayName": "Filed", "uniqueIdentifier": "f",
              "iconLists": [[], ["inside"]], "listUniqueIdentifiers": ["empty", "used"]}
    if metadata:
        folder["listMetadata"] = {"empty": {"opaque": "old"}, "used": {"opaque": "keep"}}
    path.write_bytes(plistlib.dumps({"iconLists": [[folder, "append", "stay"]], "ignored": []}))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    def rearrange(layout):
        layout.drop_empty()
        layout.move("append", "folder:f:2")
    session.edit(rearrange)
    saved = plistlib.loads(session.draft_path.read_bytes())
    del saved["saved_folder_fields"]
    saved_ids = saved["state"][1][0]["listUniqueIdentifiers"]
    assert len(saved_ids) == 2 and saved_ids[0] == "used" and "empty" not in saved_ids
    stored = plistlib.dumps(saved)
    session.draft_path.write_bytes(stored)
    reopened = Session(FileSource(path), session.dir)
    if metadata:
        assert asyncio.run(reopened.apply(reopened.require().token()))["ok"]
        assert plistlib.loads(path.read_bytes())["iconLists"][0][0]["listUniqueIdentifiers"] == saved_ids
    else:
        before = path.read_bytes()
        with pytest.raises(LayoutError, match="folder page.*read"):
            reopened.require()
        assert path.read_bytes() == before and reopened.draft_path.read_bytes() == stored
        assert not reopened.backups() and not reopened._pending_path.exists()


@pytest.mark.parametrize("evidence", ["metadata", "receipt", "unproved"])
@pytest.mark.parametrize("fill_first", [False, True])
def test_recover_missing_subpages_preserves_the_edited_owner(tmp_path, evidence, fill_first):
    folder = {"listType": "folder", "displayName": "Filed", "uniqueIdentifier": "f",
              "iconLists": [[], [], ["inside"]], "listUniqueIdentifiers": ["first", "gap", "used"]}
    if evidence == "metadata":
        folder["listMetadata"] = {pid: {"owner": pid} for pid in folder["listUniqueIdentifiers"]}
    source = plistlib.dumps({"iconLists": [[folder, "fill", "move", "stay"]], "ignored": []})
    path = tmp_path / "source.plist"
    path.write_bytes(source)
    session = Session(FileSource(path), tmp_path / "store")
    asyncio.run(session.read())

    def edit(layout):
        if fill_first:
            layout.move("fill", "folder:f:1")
        layout.move("move", "dock")
    session.edit(edit)
    token = session.require().token()
    draft = plistlib.loads(session.draft_path.read_bytes())
    receipt = draft["saved_folder_fields"]["folder:f"]
    if evidence != "receipt":
        del draft["saved_folder_fields"]
    entry = draft["state"][1][0]
    # Historical serialization filtered each empty row and its page ID together.
    rows = [(pid, page) for pid, page in zip(entry["listUniqueIdentifiers"], entry["iconLists"]) if page]
    entry["listUniqueIdentifiers"] = [pid for pid, page in rows]
    entry["iconLists"] = [page for pid, page in rows]
    historical = plistlib.dumps(draft)
    session.draft_path.write_bytes(historical)
    reopened = Session(FileSource(path), session.dir)
    if evidence == "unproved":
        with pytest.raises(LayoutError, match="folder pages.*read"):
            asyncio.run(reopened.apply(token))
        assert path.read_bytes() == source and reopened.draft_path.read_bytes() == historical
        assert not reopened.backups() and not reopened._pending_path.exists()
        return
    assert reopened.require().get("folder:f").saved_folder_fields() == receipt
    assert asyncio.run(reopened.apply(token))["status"] == "verified"
    written = plistlib.loads(path.read_bytes())
    expected = {**folder, "iconLists": [["fill"] if fill_first else [], [], ["inside"]]}
    assert written["iconLists"][0][0] == expected and written["buttonBar"] == ["move"]
