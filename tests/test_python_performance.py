"""Causal work bounds and ownership controls for real synthetic layout operations."""
import asyncio
from collections import Counter
import copy
import plistlib

import pytest

from fake_phone import app, folder, widget
from homescreen import organize, preview, ui
from homescreen.device import FileSource
from homescreen.model import Grid, Layout, LayoutError
from homescreen.session import Session
from test_integrity import MemorySource
from test_editor_transactions import request


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


@pytest.mark.parametrize("size, count", [(24, 24), (1000, 90), (2000, 90)])
def test_batch_folder_move_resolves_library_ownership_once(size, count):
    lay = Layout([[], [folder("Destination", [app("kept", "Kept")])]],
                 library=[app(f"lib{i}", str(i)) for i in range(size)])
    for node in lay.nodes.values():
        node.id = CountedID(node.id)
    lay.nodes = {node.id: node for node in lay.nodes.values()}
    library = lay.library
    selected = list(reversed(library[-count:]))
    kept = library[:-count]
    baseline = copy.deepcopy(lay.baseline())
    CountedID.comparisons = CountedID.hashes = 0
    lay.move_many([node.id for node in selected], "folder:1")
    work = CountedID.comparisons, CountedID.hashes
    assert work[0] < 8 * size + 400
    assert work[1] < 12 * (size + count) + 400
    assert lay.library is library and lay.library == kept
    children = [node for page in lay.get("folder:1").children for node in page]
    assert children[0].id == "kept"
    assert all(actual is original for actual, original in zip(children[1:], selected))
    assert len(children) == count + 1 and not lay.validate()
    assert lay.baseline() == baseline


@pytest.mark.parametrize("count", [4, 120, 240])
def test_folder_sort_builds_top_level_membership_once(count):
    folders = [folder(str(i), [app(f"kid{i}-{j}", str(3 - j)) for j in range(3)])
               for i in range(count)]
    for i, raw in enumerate(folders):
        raw.update(listUniqueIdentifiers=[f"page-{i}"],
                   listMetadata={f"page-{i}": {"opaque": [i]}})
    lay = Layout([[], *[folders[i:i + 24] for i in range(0, count, 24)]], library=[])
    for node in lay.nodes.values():
        node.id = CountedID(node.id)
    lay.nodes = {node.id: node for node in lay.nodes.values()}
    baseline = copy.deepcopy(lay.baseline())
    CountedID.comparisons = CountedID.hashes = 0
    organize.sort_folder_contents(lay)
    hashes = CountedID.hashes
    assert hashes <= 3 * count
    for i, node in enumerate(n for n in lay.nodes.values() if n.kind == "folder"):
        assert [child.name for page in node.children for child in page] == ["1", "2", "3"]
        assert node.raw["listUniqueIdentifiers"] == [f"page-{i}"]
        assert node.raw["listMetadata"] == {f"page-{i}": {"opaque": [i]}}
    assert not lay.validate() and lay.baseline() == baseline


@pytest.mark.parametrize("target_owner", ["dock", "page", "bank"])
@pytest.mark.parametrize("reverse", [False, True])
def test_batch_folder_move_preserves_mixed_owners_and_page_metadata(target_owner, reverse):
    destination = {**folder("Destination", []), "uniqueIdentifier": "destination",
                   "iconLists": [[app("kept", "Kept"), app("target0", "Target")],
                                 [app("target1", "Target")], []],
                   "listUniqueIdentifiers": ["dst0", "dst1", "dst2"],
                   "listMetadata": {key: {"opaque": [key]} for key in ("dst0", "dst1", "dst2")}}
    origin = {**folder("Origin", []), "uniqueIdentifier": "origin",
              "iconLists": [[app("first", "First")], [], [app("third", "Third"), app("stay", "Stay")]],
              "listUniqueIdentifiers": ["src0", "src1", "src2"],
              "listMetadata": {key: {"opaque": [key]} for key in ("src0", "src1", "src2")}}
    bank_folder = {**folder("Bank", [app("bankchild", "Child")]), "uniqueIdentifier": "bank"}
    alias = app("same", "Same")
    raw = [[app("dock", "Dock"), origin],
           [app("page", "Page"), alias, alias, app("bank", "Bank"), bank_folder]]
    (raw[0] if target_owner == "dock" else raw[1]).append(destination)
    lay = Layout(raw, library=[app("library", "Library"), app("libstay", "Stay")],
                 pins={"page": 23, "same": 20})
    lay.move("bank", "bank")
    lay.move("folder:bank", "bank")
    if target_owner == "bank":
        lay.move("folder:destination", "bank")
    duplicate = next(n.id for n in lay.nodes.values() if n.bundle_id == "same" and n.id != "same")
    ids = ["library", "target1", "first", "dock", duplicate, "bankchild", "page", "third", "bank", "target0"]
    if reverse:
        ids.reverse()
    selected = [lay.get(nid) for nid in ids]
    nodes, baseline = dict(lay.nodes), copy.deepcopy(lay.baseline())
    dock, page, library, tray = lay.dock, lay.pages[0], lay.library, lay.bank
    leaves = Counter(lay.leaf_ids())
    lay.move_many([*ids, ids[0]], "folder:destination")
    children = [n for p in lay.get("folder:destination").children for n in p]
    assert children[0].id == "kept" and len(children) == len(selected) + 1
    assert all(a is b for a, b in zip(children[1:], selected))
    assert all(lay.nodes[nid] is node for nid, node in nodes.items())
    assert lay.dock is dock and lay.pages[0] is page and lay.library is library and lay.bank is tray
    assert [n.id for n in library] == ["libstay"] and lay.pins == {"same": 20}
    assert lay.get("same").raw is lay.get(duplicate).raw
    assert lay.get("folder:origin").raw["listUniqueIdentifiers"] == ["src2"]
    assert lay.get("folder:origin").raw["listMetadata"] == {"src2": {"opaque": ["src2"]}}
    assert lay.get("folder:destination").raw["listUniqueIdentifiers"] == ["dst0", "dst1"]
    assert Counter(lay.leaf_ids()) == leaves and lay.baseline() == baseline
    assert lay.validate() == [f"the App Bank still holds {2 if target_owner == 'bank' else 1} item(s); "
                              "place them or send them to the App Library"]


@pytest.mark.parametrize("source_kind", ["memory", "file"])
def test_bulk_folder_edits_retain_history_bindings_and_verified_writes(tmp_path, source_kind):
    state = [[], [folder("Destination", [app("c", "C")]), app("a", "A"), app("b", "B")]]
    if source_kind == "memory":
        source = MemorySource()
        source.state = copy.deepcopy(state)
    else:
        path = tmp_path / "fixture.plist"
        path.write_bytes(plistlib.dumps(state))
        source = FileSource(path)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    original = session.require().snapshot()
    initial_binding = session.binding
    session.edit(lambda lay: lay.move_many(["b", "a", "b"], "folder:1"), expected=initial_binding)
    moved = session.require().snapshot()
    session.edit(organize.sort_folder_contents, expected=session.binding)
    sorted_snapshot = session.require().snapshot()
    assert [n.id for n in session.require().get("folder:1").children[0]] == ["a", "b", "c"]
    assert session.binding != initial_binding
    assert session.undo(expected=session.binding) and session.require().snapshot() == moved
    assert session.undo(expected=session.binding) and session.require().snapshot() == original
    assert session.redo(expected=session.binding) and session.redo(expected=session.binding)
    assert session.require().snapshot() == sorted_snapshot
    for ids, target, message in [(["a", "missing"], "folder:1", "unknown id"),
                                 (["a", "folder:1"], "folder:1", "can't go"),
                                 (["a"], "folder:missing", "unknown id")]:
        before = copy.deepcopy((session.require().snapshot(), session._undo, session._redo,
                                session.binding, session.draft_path.read_bytes()))
        with pytest.raises(LayoutError, match=message):
            session.edit(lambda lay: lay.move_many(ids, target), expected=session.binding)
        assert (session.require().snapshot(), session._undo, session._redo,
                session.binding, session.draft_path.read_bytes()) == before
    reopened = Session(source, session.dir)
    assert reopened.require().snapshot() == sorted_snapshot and reopened.binding == session.binding
    applied = asyncio.run(reopened.apply(reopened.require().token(), expected=reopened.binding))
    assert applied["ok"] and applied["status"] == "verified"
    binding = reopened.binding
    review = reopened.review_backup(applied["backup"], binding)
    restored = asyncio.run(reopened.restore(applied["backup"], expected=binding, review=review))
    assert restored["ok"] and restored["status"] == "verified"
    assert reopened.require().to_state() == state


def test_batch_folder_move_keeps_sequential_failure_for_unplaced_selected_nodes():
    lay = Layout([[], [folder("Destination", [app("kept", "Kept")]), app("a", "A"), app("b", "B")]],
                 pins={"a": 23})
    lay.pages[0].remove(lay.get("b"))
    with pytest.raises(LayoutError, match="no item with id 'b'"):
        lay.move_many(["a", "b"], "folder:1")
    assert [n.id for n in lay.pages[0]] == ["folder:1"] and lay.pins == {}
    assert [n.id for n in lay.get("folder:1").children[0]] == ["kept"]


def test_folder_sort_leaves_bank_folder_order_and_metadata_untouched():
    lay = Layout([[folder("Dock", [app("b", "B"), app("a", "A")])],
                  [folder("Bank", [app("z", "Z"), app("y", "Y")])]])
    lay.move("folder:2", "bank")
    bank = copy.deepcopy(lay.get("folder:2").raw)
    organize.sort_folder_contents(lay)
    assert [n.id for n in lay.dock[0].children[0]] == ["a", "b"]
    assert [n.id for n in lay.bank[0].children[0]] == ["z", "y"]
    assert lay.bank[0].raw == bank


@pytest.mark.parametrize("origin, target, size", [
    ("library", "bank", 24), ("library", "bank", 1000), ("library", "bank", 2000),
    ("bank", "library", 24), ("bank", "library", 1000), ("bank", "library", 2000),
    ("page", "library", 24), ("page", "library", 240),
])
def test_editor_bulk_simple_destination_resolves_ownership_once(tmp_path, monkeypatch, origin, target, size):
    items = [app(f"item{i}", str(i)) for i in range(size)]
    if origin == "page":
        raw = [[], *[items[i:i + 24] for i in range(0, size, 24)]]
        library, bank = [app("kept", "Kept")], []
        ids = [item["bundleIdentifier"] for page in raw[1:] for item in page[-(9 if size == 240 else 4):]]
    else:
        raw = [[], [folder("Retained", [app("child", "Child")])]]
        library = items if origin == "library" else [app("kept", "Kept")]
        bank = items if origin == "bank" else [app("kept", "Kept")]
        ids = [item["bundleIdentifier"] for item in reversed(items[-min(size, 90):])]
    lay = Layout(raw, library=library + bank)
    library_ids = {item["bundleIdentifier"] for item in library}
    lay.bank[:] = [lay.get(item["bundleIdentifier"]) for item in bank]
    lay.library[:] = [node for node in lay.library if node.id in library_ids]
    for node in lay.nodes.values():
        node.id = CountedID(node.id)
    lay.nodes = {node.id: node for node in lay.nodes.values()}
    session = Session(FileSource(tmp_path / "unused.plist"), tmp_path / "data", device="bulk-fixture")
    session.layout = lay
    lay.approval_device = session.device_id
    session.edited()
    before, binding = lay.snapshot(), session.binding
    selected = [lay.get(nid) for nid in ids]
    containers = lay.dock, *lay.pages, lay.library, lay.bank
    baseline = copy.deepcopy(lay.baseline())
    work = {}
    move_many = Layout.move_many
    def counted(self, *args, **kwargs):
        CountedID.comparisons = CountedID.hashes = 0
        result = move_many(self, *args, **kwargs)
        work.update(comparisons=CountedID.comparisons, hashes=CountedID.hashes)
        return result
    monkeypatch.setattr(Layout, "move_many", counted)
    monkeypatch.setattr(ui, "session", lambda: session)
    response = asyncio.run(ui.post_op(request({"binding": binding, "op": "move_many",
        "args": {"ids": ids, "target": target, "before": None}})))
    assert response.status_code == 200
    assert work["comparisons"] < 8 * size + 400 and work["hashes"] < 15 * (size + len(ids)) + 400
    assert session.binding != binding and session.history() == {"undo": 1, "redo": 0}
    destination = getattr(lay, target)
    assert all(actual is original for actual, original in zip(destination[-len(ids):], selected))
    assert all(a is b for a, b in zip(containers, (lay.dock, *lay.pages, lay.library, lay.bank)))
    assert lay.baseline() == baseline
    assert all(problem.startswith("the App Bank still holds ") for problem in lay.validate())
    after = lay.snapshot()
    assert session.undo(expected=session.binding) and session.require().snapshot() == before
    assert session.redo(expected=session.binding) and session.require().snapshot() == after


@pytest.mark.parametrize("target", ["bank", "library"])
def test_simple_batch_duplicate_ids_keep_sequential_last_occurrence_order(target):
    lay = Layout([[], [app("a", "A"), app("b", "B")]], library=[])
    lay.move_many(["a", "b", "a"], target)
    assert [n.id for n in getattr(lay, target)] == ["b", "a"]


@pytest.mark.parametrize("dock_parent, ids, children", [
    (False, ["folder:1", "a", "b"], []), (False, ["b", "a", "folder:1"], []),
    (True, ["folder:1", "a", "b"], []), (True, ["b", "a", "folder:1"], [[]]),
])
def test_bank_batch_can_move_a_parent_folder_and_its_child(dock_parent, ids, children):
    parent = folder("Parent", [app("a", "A")])
    lay = Layout([[parent, app("b", "B")] if dock_parent else [],
                  ([] if dock_parent else [parent, app("b", "B")]) + [widget("Weather", "small")]],
                 pins={"b": 23})
    owned = [lay.get(nid) for nid in ids]
    lay.move_many(ids, "bank")
    assert all(a is b for a, b in zip(lay.bank, owned)) and len(lay.bank) == 3
    assert lay.get("folder:1").children == children and lay.pins == {}
    assert [n.kind for n in lay.pages[0]] == ["widget"]


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
