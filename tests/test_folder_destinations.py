import asyncio
import plistlib

import pytest

from homescreen import server, ui
from homescreen.model import Grid, Layout, LayoutError
from homescreen.session import Session
from fake_phone import app, folder, widget
from test_integrity import MemorySource


def apps(count):
    return [app(f"app{i}", f"App {i}") for i in range(count)]


@pytest.mark.parametrize("target", ["library", "folder:1"])
def test_new_folder_rejects_unsupported_target_before_persistence(tmp_path, monkeypatch, target):
    source = MemorySource()
    session = Session(source, tmp_path)
    session.layout = Layout([[], [folder("Existing", apps(1)), app("x", "X")]], library=[])
    monkeypatch.setattr(server, "session", lambda: session)
    before = session.require().snapshot()
    assert not server.create_folder("New", ["x"], target)["ok"]
    assert session.require().snapshot() == before and not session.draft_path.exists()


def test_dock_and_library_owners_checked_for_apply_and_restore(tmp_path):
    source = MemorySource()
    source.state = [[], [app("keep", "Keep"), widget("Small", "small")]]
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    lay = session.require()
    with pytest.raises(LayoutError, match="dock"):
        session.edit(lambda draft: draft.arrange([["keep"]], dock=[lay.pages[0][1].id]))
    invalid = session._backup([[source.state[1][1]], [source.state[1][0]]], None, "invalid")
    with pytest.raises(LayoutError, match="dock"):
        asyncio.run(session.restore(invalid.name))
    assert source.writes == 0 and session.backups() == [invalid.name]
    lib = Layout([[], [app("keep", "Keep")]], library=[folder("Invalid", apps(1))])
    assert any("Library" in problem for problem in lib.validate())


def test_dock_folder_uses_the_loaded_capacity_and_is_counted():
    lay = Layout([[folder("Dock", apps(9))], [app("x", "X")]])
    assert lay.summary()["counts"]["folders"] == 1 and not lay.validate()
    lay.move("x", "folder:1")
    assert any("10" in problem and "9" in problem for problem in lay.validate())
    lay.dock[0].children[0] = [Layout([[], [widget("Weather", "small")]]).pages[0][0]]
    assert any("contains a folder or widget" in problem for problem in lay.validate())


@pytest.mark.parametrize("retire", ["dissolve", "delete", "drop"])
def test_retired_folder_cannot_absorb_another_item(retire):
    lay = Layout([[], [folder("Old", apps(1))], [app("x", "X")]], library=[])
    if retire == "dissolve":
        lay.dissolve_folder("folder:1")
    elif retire == "delete":
        lay.delete_page(1)
    else:
        lay.move("app0", "page:2")
        lay.drop_empty()
    assert not lay.find("Old")
    with pytest.raises(LayoutError):
        lay.move("x", "folder:1")
    assert lay.positions()["x"].startswith("page")


@pytest.mark.parametrize("suffix", [":0", ":-1", ":bad", ":1:2"])
def test_empty_dock_folder_retains_positive_drop_target(suffix):
    lay = Layout([[folder("Dock", apps(1))], [app("x", "X")]])
    lay.move("app0", "page:1")
    lay.drop_empty()
    assert lay.dock[0].children == [[]]
    with pytest.raises(LayoutError):
        lay.move("x", "folder:1" + suffix)
    lay.move("x", "folder:1:1")
    assert lay.dock[0].children[0][0].id == "x"


@pytest.mark.parametrize("capacity", [9, 16])
def test_filing_selections_splits_at_the_loaded_grid(capacity):
    count = capacity * 2 + 2
    lay = Layout([[], apps(count)], grid=Grid(folder=capacity))
    order = [n.id for n in lay.pages[0]]
    fid = lay.create_folder("New", order[:capacity + 1], "page:1")
    assert [len(p) for p in lay.get(fid).children] == [capacity, 1]
    ui._ops()["move_many"](order[capacity + 1:], fid)(lay)
    assert [len(p) for p in lay.get(fid).children] == [capacity, capacity, 2]
    assert [n.id for p in lay.get(fid).children for n in p] == order


def test_same_named_folder_members_have_distinct_diff_endpoints():
    lay = Layout([[], [folder("Team", apps(2)), folder("Team", [app("x", "X")])]])
    lay.swap("app0", "app1")
    assert lay.diff()["items_moved"] == 1
    lay.move("app1", "folder:2", 0)
    move = lay.diff()["moves"]["App 1 [app1]"]
    assert move.split(" -> ")[0] != move.split(" -> ")[1]
    assert "folder:1" in move and "folder:2" in move


def test_reordered_unpinned_widgets_must_fit_before_write(tmp_path):
    source = MemorySource()
    source.state = [[], [widget("Large", "large"), widget("Small", "small"), *apps(3)]]
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    lay = session.require()
    ordered = [n.id for n in lay.pages[0]]
    session.edit(lambda draft: draft.arrange([ordered[2:] + ordered[:2]]))
    with pytest.raises(LayoutError, match="no room"):
        asyncio.run(session.apply(lay.token()))
    assert not session.backups() and source.writes == 0
    session.edit(lambda draft: draft.arrange([ordered[:2] + ordered[2:][::-1]]))
    assert asyncio.run(session.apply(lay.token()))["ok"]


def test_numeric_wire_folder_id_remains_reserved_after_retirement_and_reload(tmp_path):
    source = MemorySource()
    source.state = [[], [{**folder("Old", apps(1)), "uniqueIdentifier": "3"}, app("x", "X")]]
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    session.edit(lambda lay: lay.dissolve_folder("folder:3"))
    reopened = Session(source, tmp_path)
    ids = []
    for i in range(3):
        ids.append(reopened.edit(lambda lay: lay.create_folder("New", ["x"], "page:1")))
        reopened.edit(lambda lay: lay.dissolve_folder(ids[-1]))
    assert "folder:3" not in ids


@pytest.mark.parametrize(("item", "target", "before"), [
    ("x", "folder:1:3", None), ("app0", "folder:1:2", None), ("x", "folder:1:0", "x"),
])
def test_unrepresentable_folder_target_keeps_draft_and_history(tmp_path, item, target, before):
    source = MemorySource()
    source.state = [[], [folder("Work", apps(1)), app("x", "X")]]
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    image = session.draft_path.read_bytes()
    with pytest.raises(LayoutError):
        session.edit(lambda lay: lay.move(item, target, before=before))
    assert image == session.draft_path.read_bytes() and session.history()["undo"] == 0


@pytest.mark.parametrize("replacement", [True, False])
def test_review_distinguishes_replaced_and_literal_folder_names(tmp_path, replacement):
    source = MemorySource()
    source.state = [[], [folder("Team", [app("a", "A")]), folder("Team", [app("b", "B")]),
                         folder("Team [folder:1]", [app("c", "C")])]]
    if replacement:
        source.state[1] = [source.state[1][0], app("other", "Other")]
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    if replacement:
        session.edit(lambda lay: lay.dissolve_folder("folder:1"))
        session.edit(lambda lay: lay.create_folder("Team", ["a"], "page:1"))
    else:
        session.edit(lambda lay: lay.swap("a", "c"))
    reviewed = ui.view(session)
    detail = reviewed["move_details"]["A [a]"]
    assert detail["from"] != detail["to"]
    before, after = reviewed["moves"]["A [a]"].split(" -> ")
    assert before != after


def test_appending_child_of_full_folder_retains_original_subpage_and_localizes_capacity(tmp_path):
    source = MemorySource()
    source.state = [[folder("Dock", apps(9))], [app("x", "X"), app("tail", "Tail")]]
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    session.edit(lambda lay: lay.move_many(["app8"], "folder:1"))
    assert [len(p) for p in session.require().dock[0].children] == [9]
    session.edit(lambda lay: lay.move("x", "folder:1:1"))
    assert ui.view(session)["diff"]["problem_details"][0]["key"] == "validation.folderCapacity"


def test_older_retired_wire_id_stays_reserved_after_upgrade(tmp_path):
    source = MemorySource()
    source.state = [[], [{**folder("Old", apps(1)), "uniqueIdentifier": "1"}, app("x", "X")]]
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    session.edit(lambda lay: lay.dissolve_folder("folder:1"))
    saved = plistlib.loads(session.draft_path.read_bytes())
    for field in ("identity", "source_identity"):
        saved[field]["counters"].pop("folder:", None)
    session.draft_path.write_bytes(plistlib.dumps(saved))
    reopened = Session(source, tmp_path)
    assert reopened.edit(lambda lay: lay.create_folder("New", ["x"], "page:1")) != "folder:1"
    with pytest.raises(LayoutError):
        reopened.edit(lambda lay: lay.move("app0", "folder:1"))


def test_numeric_string_wire_identifier_is_opaque_to_plist_counters(tmp_path):
    source = MemorySource()
    uid = "18446744073709551616"
    source.state = [[], [{**folder("Old", apps(1)), "uniqueIdentifier": uid}, app("x", "X")]]
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    assert Session(source, tmp_path).require().pages[0][0].id == "folder:" + uid
    assert session.edit(lambda lay: lay.create_folder("New", ["x"], "page:1")) != "folder:" + uid


def test_empty_intermediate_folder_page_compacts_during_the_edit(tmp_path):
    source = MemorySource()
    source.state = [[], [folder("Work", apps(1)), app("b", "B"), app("keep", "Keep")]]
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    session.edit(lambda lay: lay.move("b", "folder:1:2"))
    session.edit(lambda lay: lay.move("app0", "page:1"))
    def children(lay):
        return [[n.id for n in page] for page in lay.get("folder:1").children]
    assert children(session.require()) == children(Session(source, tmp_path).require()) == [["b"]]
    session.edit(lambda lay: lay.move_many(["keep"], "folder:1"))
    assert children(session.require()) == [["b", "keep"]]


@pytest.mark.parametrize("selection", [["x", "app7"], ["app7", "x"]])
def test_mixed_selection_uses_the_room_vacated_by_every_selected_child(selection):
    lay = Layout([[folder("Full", apps(9))], [app("x", "X"), app("keep", "Keep")]])
    lay.move_many(selection, "folder:1")
    pages = lay.get("folder:1").children
    assert [len(page) for page in pages] == [9, 1]
    assert [n.id for page in pages for n in page][-2:] == selection


def test_mixed_selection_compacts_the_vacated_final_page_before_appending():
    raw = {**folder("Full", apps(9)), "listUniqueIdentifiers": ["first", "last"]}
    raw["iconLists"].append([app("last", "Last")])
    lay = Layout([[raw], [app("x", "X")]])
    lay.move_many(["last", "app7", "x"], "folder:1")
    assert [len(page) for page in lay.dock[0].children] == [9, 2]
    assert [n.id for page in lay.dock[0].children for n in page][-3:] == ["last", "app7", "x"]
    assert lay.dock[0].raw["listUniqueIdentifiers"] == ["first", "last"]


def test_reappending_a_whole_subpage_retains_its_reviewed_identity():
    raw = {**folder("Full", apps(9)), "listUniqueIdentifiers": ["first"]}
    lay = Layout([[raw], [app("keep", "Keep")]])
    lay.move_many([f"app{i}" for i in range(9)], "folder:1")
    assert lay.to_state()[0][0]["listUniqueIdentifiers"] == ["first"]
    assert not lay.diff()["items_moved"]


def test_exported_folder_subpage_identifiers_follow_compaction_and_extension(tmp_path):
    from homescreen.device import FileSource
    raw = {"iconLists": [[{"listType": "folder", "displayName": "Work",
                           "iconLists": [["a"], ["b"]], "listUniqueIdentifiers": ["first", "second"]}, "x"]],
           "ignored": []}
    path = tmp_path / "fixture.plist"
    path.write_bytes(plistlib.dumps(raw))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda lay: lay.move("a", "dock"))
    session.edit(lambda lay: lay.move("x", "folder:1:2"))
    assert asyncio.run(session.apply(session.require().token()))["ok"]
    field = plistlib.loads(path.read_bytes())["iconLists"][0][0]
    assert field["iconLists"] == [["b"], ["x"]]
    assert field["listUniqueIdentifiers"][0] == "second"
    assert field["listUniqueIdentifiers"][1] not in ("first", "second")


@pytest.mark.parametrize("with_ids", [False, True])
def test_vacated_dock_folder_keeps_only_its_editor_drop_target(tmp_path, with_ids):
    from homescreen.device import FileSource

    saved = {"listType": "folder", "iconLists": [["inside"]]}
    if with_ids:
        saved.update(listUniqueIdentifiers=["old"], listMetadata={"old": {"opaque": 1}})
    path = tmp_path / "archive.plist"
    path.write_bytes(plistlib.dumps({"buttonBar": [saved], "iconLists": [["stay"]], "ignored": []}))
    session = Session(FileSource(path), tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda layout: layout.move("inside", "page:1"))
    reopened = Session(FileSource(path), session.dir)
    assert reopened.require().dock[0].children == [[]]
    assert asyncio.run(reopened.apply(reopened.require().token()))["ok"]
    written = plistlib.loads(path.read_bytes())["buttonBar"][0]
    assert written["iconLists"] == [] and written.get("listUniqueIdentifiers", []) == []
    assert "old" not in written.get("listMetadata", {})
