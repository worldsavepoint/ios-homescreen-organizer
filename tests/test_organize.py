"""Organization preserves loaded-grid placement and complete filing results."""
import asyncio
import json

import pytest
from starlette.requests import Request

from fake_phone import app, widget
from homescreen import server, ui
from homescreen.session import Session
from homescreen.model import LayoutError
from test_integrity import MemorySource


class OrganizeSource(MemorySource):
    def __init__(self, columns=4):
        super().__init__()
        self.columns = columns

    async def info(self):
        return {**await super().info(), "icon_metrics": {
            "homeScreenIconColumns": self.columns, "homeScreenIconRows": 6}}


def call(name, **arguments):
    result = asyncio.run(server.mcp.call_tool(name, arguments)).model_dump(by_alias=True)
    assert not result.get("isError"), result
    return result["structuredContent"]


@pytest.mark.parametrize("columns", [4, 5])
@pytest.mark.parametrize("operation", ["alpha", "color", "http"])
def test_sort_widgets_preserves_geometric_pages_and_reopened_apply(tmp_path, monkeypatch, columns, operation):
    source = OrganizeSource(columns)
    widgets = [widget(f"Small-{i}", "small") for i in range(7)]
    for item in widgets:
        item["elements"][0]["configuration"] = {"opaque": b"\0\1", "name": item["displayName"]}
    source.state = [[], widgets[:6], widgets[6:]]
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    before = session.require().snapshot()
    monkeypatch.setattr(server, "session", lambda: session)
    if operation == "color":
        async def colors():
            return {}
        monkeypatch.setattr(session, "icon_colors", colors)
        result = call("sort_by_color", keep_first_page=False)
    elif operation == "http":
        monkeypatch.setattr(ui, "session", lambda: session)
        body = json.dumps({"binding": session.binding, "op": "sort_alpha", "args": {"keep_first_page": False}}).encode()
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        request = Request({"type": "http", "method": "POST", "path": "/api/op", "query_string": b"",
                           "headers": [(b"host", b"127.0.0.1"), (b"x-token", ui.TOKEN.encode())]}, receive)
        response = asyncio.run(ui.post_op(request))
        assert response.status_code == 200
        result = {"problems": json.loads(response.body)["diff"]["problems"]}
    else:
        result = call("sort_alphabetically", keep_first_page=False)
    assert result["problems"] == []
    assert [len(page) for page in session.require().pages] == [6, 1]
    assert all(not session.require().place(page)[1] for page in session.require().pages)
    sorted_state = session.require().snapshot()
    assert session.undo() and session.require().snapshot() == before
    assert session.redo() and session.require().snapshot() == sorted_state
    reopened = Session(source, session.dir)
    assert reopened.require().snapshot() == sorted_state
    assert asyncio.run(reopened.apply(reopened.require().token()))["ok"]
    assert source.writes == 1
    assert [node.raw for page in reopened.require().pages for node in page] == widgets


@pytest.mark.parametrize("destinations", [("Work", "Personal"), ("Work", "work")])
def test_filing_feedback_accumulates_apps_for_the_actual_folder(tmp_path, monkeypatch, destinations):
    source = OrganizeSource()
    source.state = [[], [app(name.lower(), name) for name in "ABCD"]]
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    before = session.require().snapshot()
    monkeypatch.setattr(server, "session", lambda: session)
    result = call("file_into_folders", groups={destinations[0]: ["a", "b"], destinations[1]: ["c", "d"]}, keep_first_page=False)
    assert result["ok"] and result["problems"] == []
    assert result["count"] == 4
    assert sorted(name for names in result["filed"].values() for name in names) == list("ABCD")
    assert sorted(session.require().leaf_ids()) == list("abcd")
    assert session.undo() and session.require().snapshot() == before


@pytest.mark.parametrize("ids,count", [(["a"], 0), (["a", "a"], 0), (["a", "a", "b"], 2), (["a", "b"], 2)])
def test_filing_counts_distinct_local_items(tmp_path, monkeypatch, ids, count):
    source = OrganizeSource()
    source.state = [[], [app("a", "A"), app("b", "B")]]
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    monkeypatch.setattr(server, "session", lambda: session)
    result = call("file_into_folders", groups={"Work": ids}, keep_first_page=False)
    assert result["ok"] and result["count"] == count
    assert result["filed"] == ({"Work": ["A", "B"]} if count else {})
    assert sorted(session.require().leaf_ids()) == ["a", "b"]


def test_filing_keeps_separate_occurrences_of_same_bundle(tmp_path, monkeypatch):
    source = OrganizeSource()
    source.state = [[], [app("a", "A"), app("a", "A")]]
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    ids = [node.id for node in session.require().pages[0]]
    monkeypatch.setattr(server, "session", lambda: session)
    result = call("file_into_folders", groups={"Work": ids}, keep_first_page=False)
    assert result["count"] == 2 and result["filed"] == {"Work": ["A", "A"]}
    assert sorted(session.require().leaf_ids()) == sorted(ids)


@pytest.mark.parametrize("keep_first", [False, True])
def test_usage_keeps_widget_page_index_identity_and_metadata(tmp_path, monkeypatch, keep_first):
    from homescreen.device import to_root_archive
    from test_grid_serialization import ArchiveSource
    pages = [[app("a", "A")], [app("b", "B")], [widget("Calendar", "small")]]
    if keep_first:
        pages.insert(0, [app("first", "First")])
    source = ArchiveSource(4, 6, "dictionary")
    source.raw = to_root_archive([[], *pages], [])
    names = [f"page-{i}" for i in range(1, len(pages) + 1)]
    metadata = {name: {"opaque": name.encode()} for name in names}
    source.raw.update(listUniqueIdentifiers=names, listMetadata=metadata)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    before = session.require().snapshot()
    session.usage = {"ok": True, "days": 2, "opens": {"a": 20, "b": 10}}
    monkeypatch.setattr(server, "session", lambda: session)
    result = call("optimize_by_usage", keep_first_page=keep_first)
    assert result["ok"] and result["problems"] == []
    assert len(session.require().pages) == len(pages)
    assert session.require().pages[-1][0].kind == "widget"
    assert session.require().page_origins == list(range(len(pages)))
    after = session.require().snapshot()
    assert Session(source, session.dir).require().snapshot() == after
    assert session.undo() and session.require().snapshot() == before
    assert session.redo() and session.require().snapshot() == after
    assert asyncio.run(session.apply(session.require().token()))["ok"]
    assert source.raw["listUniqueIdentifiers"] == names
    assert source.raw["listMetadata"] == metadata


def test_widget_bank_and_pin_keep_configuration_and_supported_destinations(tmp_path):
    source = OrganizeSource()
    item = widget("Weather", "small")
    item["elements"][0]["configuration"] = {"opaque": b"\0\1"}
    source.state = [[], [item], [app("a", "A")]]
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    node = next(node for node in session.require().nodes.values() if node.kind == "widget")
    session.edit(lambda lay: lay.move(node.id, "bank"))
    assert session.require().bank[0].raw == item
    with pytest.raises(LayoutError):
        session.edit(lambda lay: lay.move(node.id, "library"))
    session.edit(lambda lay: lay.pin(node.id, 2, 9))
    assert session.require().place(session.require().pages[1])[0][node.id] == (2, 1, 2, 2)
    assert session.require().get(node.id).raw == item
    assert session.undo() and session.require().bank[0].id == node.id
    assert session.redo() and Session(source, session.dir).require().get(node.id).raw == item
