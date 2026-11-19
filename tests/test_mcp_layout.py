"""MCP layout operations use synthetic loaded drafts and temporary storage."""

import asyncio

import pytest

from homescreen import server, ui
from fake_phone import app, folder, widget
from test_integrity import MemorySource
from homescreen.device import FileSource
from homescreen.model import Grid, Layout
from homescreen.session import Session


@pytest.fixture
def draft(tmp_path, monkeypatch):
    def load(cols=4, rows=6, version="27"):
        grid = Grid(cols=cols, rows=rows, dock=6 if cols == 5 else 4,
                    folder=16 if cols == 5 else 9)
        session = Session(FileSource(tmp_path / "unused-source.plist"), tmp_path / "data")
        session.device_id = "synthetic"
        session.info = {"ProductVersion": version}
        session.grid = grid
        session.layout = Layout([[], [{"bundleIdentifier": "com.example.a", "displayName": "A"}]],
                                library=[], grid=grid)
        monkeypatch.setattr(server, "session", lambda: session)
        return session

    return load


def call(tool, **arguments):
    result = asyncio.run(server.mcp.call_tool(tool, arguments)).model_dump(by_alias=True)
    assert not result.get("isError"), result
    return result["structuredContent"]


@pytest.mark.parametrize("tool", ["show_layout", "find_apps", "diff"])
def test_inspection_reads_another_sessions_current_draft(tmp_path, monkeypatch, tool):
    source = MemorySource()
    session = Session(source, tmp_path)
    monkeypatch.setattr(server, "session", lambda: session)
    call("read_phone")
    other = Session(source, tmp_path)
    other.edit(lambda layout: layout.move("b", "dock"))
    assert not call("move", item_id="a", target="dock")["ok"]
    if tool == "show_layout":
        assert [node["id"] for node in call(tool)["dock"]] == ["b"]
    elif tool == "find_apps":
        assert call(tool, query="b")["result"][0]["position"] == "dock #1"
    else:
        assert call(tool)["apply_token"] == other.require().token()
    assert session.binding == other.binding


def test_review_details_for_parked_children_widgets_and_folder_names(tmp_path, monkeypatch):
    source = MemorySource()
    source.state = [[], [folder("Work", [app("a", "A")]), app("b", "B")], [widget("Weather", "small")]]
    session = Session(source, tmp_path)
    monkeypatch.setattr(server, "session", lambda: session)
    call("read_phone")
    lay = session.require()
    group, weather = lay.pages[0][0].id, lay.pages[1][0].id
    assert call("move", item_id=group, target="bank")["ok"]
    assert call("move", item_id="b", target=group)["ok"]
    assert call("rename_folder", folder_id=group, name="Today")["ok"]
    assert call("move", item_id=weather, target="page:1")["ok"]
    assert call("find_apps", query="b")["result"][0]["position"] == "folder 'Today' p1 #2"
    diff = call("diff")
    assert diff["items_moved"] == 1
    detail = diff["move_details"]["B [b]"]
    assert detail["from"] == {"kind": "page", "page": 1, "index": 2}
    assert detail["to"] == {"kind": "folder", "name": "Today", "page": 1, "index": 2}
    changes = {row["id"]: row for row in diff["placement_changes"]}
    assert changes[group]["reason"] == "renamed" and changes[group]["before_name"] == "Work"
    assert changes[group]["from"]["page"] == 1 and changes[group]["to"] == {"kind": "bank"}
    assert changes[weather]["from"]["page"] == 2 and changes[weather]["to"]["page"] == 1
    assert ui.view(session)["diff"]["placement_changes"] == diff["placement_changes"]


@pytest.mark.parametrize("tool,arguments", [
    ("create_folder", {"name": "", "item_ids": ["a"], "target": "page:1"}),
    ("rename_folder", {"name": "", "folder_id": "folder:1"}),
    ("move", {"item_id": "a", "target": "page:two"}), ("move", {"item_id": "a", "target": "page:0"}),
])
def test_invalid_folder_names_and_page_targets_preserve_saved_draft(tmp_path, monkeypatch, tool, arguments):
    source = MemorySource()
    source.state = [[], [folder("Work", [app("b", "B")]), app("a", "A")]]
    session = Session(source, tmp_path)
    monkeypatch.setattr(server, "session", lambda: session)
    call("read_phone")
    before = session.draft_path.read_bytes(), session.binding, session.history()
    result = call(tool, **arguments)
    assert not result["ok"] and ("number" if tool == "move" else "name") in result["error"]
    assert (session.draft_path.read_bytes(), session.binding, session.history()) == before


@pytest.mark.parametrize("cols,rows,row,column", [
    (4, 6, 2, 1),
    (4, 6, 6, 4),
    (5, 6, 2, 1),
    (5, 6, 6, 5),
    (5, 7, 7, 5),
])
def test_place_at_uses_loaded_grid_and_persists_exact_coordinates(draft, cols, rows, row, column):
    session = draft(cols, rows)
    result = call("place_at", item_id="com.example.a", page=1, row=row, column=column)
    assert result == {"ok": True, "problems": []}
    assert call("show_layout")["pages"]["1"][0]["placed_at"] == {"row": row, "column": column}
    reloaded = Session(session.source, session.dir)
    assert reloaded.require().place(reloaded.require().pages[0])[0]["com.example.a"] == (
        row - 1, column - 1, 1, 1)
    assert reloaded.grid == session.grid
    assert session.history() == {"undo": 1, "redo": 0}


@pytest.mark.parametrize("cols,rows", [(4, 6), (5, 7)])
@pytest.mark.parametrize("coordinate", ["zero-row", "negative-row", "last-row+1",
                                       "zero-column", "negative-column", "last-column+1"])
def test_place_at_rejects_out_of_grid_coordinates_atomically(draft, cols, rows, coordinate):
    session = draft(cols, rows)
    row, column = {
        "zero-row": (0, cols), "negative-row": (-1, cols), "last-row+1": (rows + 1, 1),
        "zero-column": (2, 0), "negative-column": (2, -1), "last-column+1": (1, cols + 1),
    }[coordinate]
    before = session.require().summary()
    result = call("place_at", item_id="com.example.a", page=1, row=row, column=column)
    assert result["ok"] is False
    assert result["error"]
    assert session.require().summary() == before
    assert session.require().pins == {}
    assert session.history() == {"undo": 0, "redo": 0}
    assert not session.draft_path.exists()


@pytest.mark.parametrize("cols,rows,expected", [
    (4, 6, {"cols": 4, "rows": 6, "dock": 4, "folder": 9}),
    (5, 7, {"cols": 5, "rows": 7, "dock": 6, "folder": 16}),
])
def test_show_layout_adds_loaded_grid_to_existing_summary(draft, cols, rows, expected):
    session = draft(cols, rows)
    before = session.require().summary()
    result = call("show_layout")
    assert result.pop("grid", None) == expected
    assert result == before


def test_ios16_uses_sequential_order_after_rejected_fixed_position(draft):
    session = draft(version="16.7")
    assert call("place_at", item_id="com.example.a", page=1, row=2, column=1)["ok"] is False
    assert session.require().pins == {}
    assert session.history() == {"undo": 0, "redo": 0}
    assert call("move", item_id="com.example.a", target="page:2")["ok"] is True
    assert session.require().pages[1][0].id == "com.example.a"


def test_place_at_keeps_one_based_page_number_when_creating_next_page(draft):
    session = draft(cols=5)
    assert call("place_at", item_id="com.example.a", page=2, row=2, column=1)["ok"] is True
    assert session.require().pages[0] == []
    assert call("show_layout")["pages"]["2"][0]["placed_at"] == {"row": 2, "column": 1}


@pytest.mark.parametrize("cols,rows", [(4, 6), (5, 7)])
def test_close_gaps_then_arrange_changes_pinned_layout_to_sequential_order(draft, cols, rows):
    session = draft(cols, rows)
    session.layout = Layout([[], [{"bundleIdentifier": "com.example.a", "displayName": "A"},
                                 {"bundleIdentifier": "com.example.b", "displayName": "B"}]],
                            library=[], grid=session.grid)
    ids = ["com.example.a", "com.example.b"]
    assert call("place_at", item_id=ids[0], page=1, row=rows, column=cols)["ok"] is True
    assert call("arrange", pages=[ids])["ok"] is True
    assert session.require().place(session.require().pages[0])[0][ids[0]][:2] == (rows - 1, cols - 1)
    assert call("close_gaps", item_ids=[ids[0]])["ok"] is True
    assert call("arrange", pages=[ids])["ok"] is True
    positions, problems = session.require().place(session.require().pages[0])
    assert not problems
    assert positions[ids[0]][:2] == (0, 0)
    assert positions[ids[1]][:2] == (0, 1)
    assert session.require().pins == {}
