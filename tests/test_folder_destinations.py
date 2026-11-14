import asyncio

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
