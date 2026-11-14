"""MCP layout operations use synthetic loaded drafts and temporary storage."""

import asyncio

import pytest

from homescreen import server
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


def call(name, **arguments):
    result = asyncio.run(server.mcp.call_tool(name, arguments)).model_dump(by_alias=True)
    assert not result.get("isError"), result
    return result["structuredContent"]


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
