"""Causal work bounds and ownership controls for real synthetic layout operations."""
import asyncio
from collections import Counter
import copy

import pytest

from fake_phone import app, folder, widget
from homescreen import organize, preview, ui
from homescreen.device import FileSource
from homescreen.model import Grid, Layout, LayoutError
from homescreen.session import Session
from test_integrity import MemorySource


@pytest.mark.parametrize("show_original", [True, False])
def test_preview_materializes_one_diff_and_one_original(tmp_path, monkeypatch, show_original):
    session = Session(FileSource(tmp_path / "unused.plist"), tmp_path / "data")
    session.layout = Layout([[], [app("a", "<A>"), app("b", "B"),
                                 folder("Work & More", [app("c", "C")]), widget("Weather", "small")]], library=[])
    session.layout.swap("a", "b")
    calls = Counter()
    original, diff = Layout.original, Layout.diff
    def counted_original(lay):
        calls["original"] += 1
        return original(lay)
    def counted_diff(lay, *args, **kwargs):
        calls["diff"] += 1
        return diff(lay, *args, **kwargs)
    monkeypatch.setattr(Layout, "original", counted_original)
    monkeypatch.setattr(Layout, "diff", counted_diff)
    async def icon(identifier):
        calls["icon"] += 1
        return b"fixture-png"
    monkeypatch.setattr(session, "icon", icon)
    before = session.layout.snapshot()
    path = asyncio.run(preview.render(session, tmp_path / "preview.html", show_original))
    html = path.read_text()
    assert "&lt;A&gt;" in html and "Work &amp; More" in html and "1 items moved" in html
    assert ("Last read · Page 1" in html) == show_original
    assert session.layout.snapshot() == before
    assert calls == {"original": 1, "diff": 1, "icon": 3}


def test_uniform_packing_avoids_geometry_and_keeps_first_page_anchors(monkeypatch):
    state = [[], [app("kept", "Kept")],
             [folder("Folder", [app("child", "Child")]), *[app(f"a{i}", str(i)) for i in range(50)]]]
    lay = Layout(state, pins={"kept": 23, "a0": 22})
    place, calls = Layout.place, []
    def counted(self, nodes):
        calls.append(tuple(n.id for n in nodes))
        return place(self, nodes)
    monkeypatch.setattr(Layout, "place", counted)
    organize.alphabetical(lay, keep_first_page=True)
    assert [len(p) for p in lay.pages] == [1, 24, 24, 3]
    assert lay.pins == {"kept": 23}
    assert lay.page_origins == [0, 1, -1, -1]
    assert [n.name for n in lay.pages[1]][:3] == ["Folder", "0", "1"]
    assert calls == []


@pytest.mark.parametrize("columns", [4, 5])
def test_mixed_packing_retains_geometry_then_returns_to_uniform_path(monkeypatch, columns):
    lay = Layout([[], [widget("Weather", "small"), *[app(f"a{i}", str(i)) for i in range(60)]]],
                 grid=Grid(cols=columns, rows=6))
    place, calls = Layout.place, []
    def counted(self, nodes):
        calls.append(tuple(n.kind for n in nodes))
        return place(self, nodes)
    monkeypatch.setattr(Layout, "place", counted)
    organize.pack(lay, list(lay.pages[0]))
    assert calls and all("widget" in kinds for kinds in calls)
    assert [len(p) for p in lay.pages] == ([21, 24, 16] if columns == 4 else [27, 30, 4])
    assert all(not place(lay, page)[1] for page in lay.pages)
    assert not lay.validate()


def test_packing_rechecks_geometry_when_widget_joins_a_uniform_page(monkeypatch):
    lay = Layout([[], [*[app(f"a{i}", str(i)) for i in range(20)], widget("Weather", "small"),
                       *[app(f"b{i}", str(i)) for i in range(24)]]])
    place, calls = Layout.place, []
    def counted(self, nodes):
        calls.append(tuple(n.kind for n in nodes))
        return place(self, nodes)
    monkeypatch.setattr(Layout, "place", counted)
    organize.pack(lay, list(lay.pages[0]))
    assert [len(page) for page in lay.pages] == [20, 21, 4]
    assert calls[0] == (*(["app"] * 20), "widget")
    assert all("widget" in kinds for kinds in calls)
    assert all(not place(lay, page)[1] for page in lay.pages)


def test_packing_empty_suffix_preserves_only_the_kept_page(monkeypatch):
    lay = Layout([[], [app("a", "A")], []], pins={"a": 23})
    def fail(*args):
        pytest.fail("empty packing invoked geometry")
    monkeypatch.setattr(Layout, "place", fail)
    organize.pack(lay, [], start_page=1)
    assert [[n.id for n in page] for page in lay.pages] == [["a"]]
    assert lay.pins == {"a": 23} and lay.page_origins == [0]


class CountedID(str):
    comparisons = 0
    hashes = 0
    def __eq__(self, other):
        type(self).comparisons += 1
        return str.__eq__(self, other)
    def __hash__(self):
        type(self).hashes += 1
        return str.__hash__(self)


def test_arrange_counts_and_membership_scale_linearly_with_omitted_library():
    lay = Layout([[], [app(f"a{i}", str(i)) for i in range(24)]],
                 library=[app(f"lib{i}", str(i)) for i in range(1032)],
                 bank=[app(f"bank{i}", str(i)) for i in range(100)])
    for node in lay.nodes.values():
        node.id = CountedID(node.id)
    lay.nodes = {n.id: n for n in lay.nodes.values()}
    pages = [[CountedID(n.id) for n in reversed(lay.pages[0])]]
    CountedID.comparisons = CountedID.hashes = 0
    lay.arrange(pages)
    assert [n.id for n in lay.pages[0]] == pages[0]
    assert len(lay.library) == 1032
    assert len(lay.bank) == 100
    # A repeated full-list count does more than one million comparisons.
    assert CountedID.comparisons < 12000
    assert CountedID.hashes < 20000


def test_arrange_diagnostics_and_bank_membership_preserve_occurrences():
    lay = Layout([[], [app("a", "A"), app("a", "A"), app("b", "B")]], library=[], bank=[app("c", "C"), app("d", "D")])
    ids = [n.id for n in lay.pages[0]]
    before = lay.snapshot()
    with pytest.raises(LayoutError) as error:
        lay.arrange([[ids[1], ids[1], "z", "y"]])
    assert str(error.value) == (f"listed twice: {ids[1:2]}; left out (iOS would put these back randomly): {sorted([ids[0], 'b'])}; "
                                "not top-level items (inside a folder, or unknown): ['y', 'z']")
    assert lay.snapshot() == before
    lay.arrange([[*ids, "c"]])
    assert [n.id for n in lay.bank] == ["d"]
    assert [n.id for n in lay.pages[0]] == [*ids, "c"]


def test_history_does_not_copy_shared_baseline_before_its_constructor(tmp_path, monkeypatch):
    session = Session(MemorySource(), tmp_path)
    asyncio.run(session.read())
    session.edit(lambda lay: lay.swap("a", "b"))
    baseline = session._undo[-1]["source"]
    real = copy.deepcopy
    encounters = Counter()
    def counted(value, memo=None, _nil=[]):
        if value is baseline:
            encounters["source"] += 1
        if isinstance(value, dict) and value.get("source") is baseline:
            encounters["whole"] += 1
        return real(value, memo) if memo is not None else real(value)
    # Count explicit deepcopy calls on the target entry and its source baseline.
    monkeypatch.setattr(copy, "deepcopy", counted)
    assert session.undo()
    assert session.require().pages[0][0].id == "a"
    assert encounters == {"source": 1}


@pytest.mark.parametrize("direction", ["undo", "redo"])
def test_history_nested_fields_baselines_and_failure_stay_private(tmp_path, monkeypatch, direction):
    source = MemorySource()
    source.state = [[], [app("a", "A"), app("b", "B"), folder("Work", [app("kid", "Kid")]), widget("Weather", "small")]]
    source.state[1][-1]["elements"][0]["configuration"] = {"city": "Original"}
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    session.edit(lambda lay: lay.nodes["a"].raw["someFutureKey"]["nested"].append(4))
    session.edit(lambda lay: lay.swap("a", "b"))
    assert session.undo()
    before = (session.require().snapshot(), copy.deepcopy(session._undo), copy.deepcopy(session._redo),
              copy.deepcopy(session._history_baseline), session.binding, session.draft_path.read_bytes())
    def fail():
        raise OSError("fixture failed save")
    with monkeypatch.context() as patch:
        patch.setattr(session, "_save_draft", fail)
        with pytest.raises(OSError, match="fixture failed save"):
            getattr(session, direction)()
    assert (session.require().snapshot(), session._undo, session._redo, session._history_baseline,
            session.binding, session.draft_path.read_bytes()) == before
    assert getattr(session, direction)()
    historical = copy.deepcopy((session._undo, session._redo, session._history_baseline))
    own_baseline = copy.deepcopy(session.require().baseline())
    session.require().nodes["a"].raw["someFutureKey"]["nested"].append(99)
    kid = next(n for n in session.require().nodes.values() if n.id == "kid")
    kid.raw["someFutureKey"]["nested"].clear()
    widget_node = next(n for n in session.require().nodes.values() if n.kind == "widget")
    widget_node.raw["elements"][0]["configuration"]["city"] = "Changed"
    assert (session._undo, session._redo, session._history_baseline) == historical
    assert session.require().baseline() == own_baseline
    session.require().source[1][0]["someFutureKey"]["nested"].append(100)
    assert (session._undo, session._redo, session._history_baseline) == historical


def test_history_restored_bank_folder_children_stay_detached(tmp_path):
    source = MemorySource()
    source.state[1].append(folder("Work", [app("kid", "Kid")]))
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    session.edit(lambda lay: lay.move("folder:1", "bank"))
    session.edit(lambda lay: lay.nodes["kid"].raw["someFutureKey"]["nested"].clear())
    assert session.undo()
    assert session.require().bank[0].children[0][0].raw["someFutureKey"]["nested"] == [1, 2, 3]
    historical = copy.deepcopy((session._undo, session._redo, session._history_baseline))
    session.require().bank[0].children[0][0].raw["someFutureKey"]["nested"].append(4)
    assert (session._undo, session._redo, session._history_baseline) == historical
    assert session.redo()
    assert session.require().bank[0].children[0][0].raw["someFutureKey"]["nested"] == []


@pytest.mark.parametrize("broken_pin", [False, True])
def test_http_view_reuses_each_current_placement_and_its_problems(tmp_path, monkeypatch, broken_pin):
    session = Session(FileSource(tmp_path / "unused.plist"), tmp_path / "data")
    lay = session.layout = Layout([[], [app("a", "A"), app("b", "B")], [app("c", "C")]], pins={"a": 0, "c": 23})
    lay.swap("a", "b")
    if broken_pin:
        lay.pins["a"] = 24
    calls = Counter()
    place = Layout.place
    def counted(self, nodes):
        calls[id(self), id(nodes)] += 1
        return place(self, nodes)
    monkeypatch.setattr(Layout, "place", counted)
    result = ui.view(session)
    assert result["diff"]["problems"] == (["page 1: A can't be pinned at row 7, column 1"] if broken_pin else [])
    assert result["pages"][1][0]["pos"] == [5, 3, 1, 1]
    assert all(calls[id(lay), id(page)] == 1 for page in lay.pages)
    # The changed first page needs independent original placement; page two is reusable.
    assert sum(n for (owner, page), n in calls.items() if owner != id(lay)) == 1


def test_changed_widget_geometry_uses_independent_original_cells(tmp_path):
    session = Session(FileSource(tmp_path / "unused.plist"), tmp_path / "data")
    session.layout = Layout([[], [widget("Weather", "small"), folder("Work", [app("a", "A")])]])
    wid = next(n for n in session.layout.nodes.values() if n.kind == "widget")
    wid.raw["gridSize"] = "large"
    result = ui.view(session)
    change = next(c for c in result["diff"]["placement_changes"] if c["id"] == "folder:1")
    assert change["from"]["row"] == 1 and change["from"]["column"] == 3
    assert change["to"]["row"] == 5 and change["to"]["column"] == 1
