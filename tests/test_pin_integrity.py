"""Wire placement, readback and backup regressions using synthetic archive transport."""

import asyncio
import copy
import plistlib

import pytest

from fake_phone import app, folder, widget
from homescreen.device import to_root_archive
from homescreen.model import LayoutError
from homescreen.session import Session, placed_structure
from test_grid_serialization import ArchiveSource, metadata


def run(operation):
    return asyncio.run(operation)


def loaded(tmp_path, state, cols=4, rows=6, pins=None):
    source = ArchiveSource(cols, rows, "dictionary")
    source.raw = to_root_archive(state, [], pins)
    session = Session(source, tmp_path / "data")
    run(session.read())
    return session, source


def apply(session):
    return run(session.apply(session.require().token()))


def folder_state(identified=True):
    item = folder("Tools", [app("example.a", "A"), app("example.b", "B")])
    if identified:
        item["uniqueIdentifier"] = "folder-id"
    return [[], [item]]


@pytest.mark.parametrize("cols,rows,cell", [(4, 6, 23), (5, 7, 34)])
def test_first_folder_pin_survives_payload_readback_reload_and_restore(tmp_path, cols, rows, cell):
    session, source = loaded(tmp_path, folder_state(False), cols, rows)
    session.edit(lambda lay: lay.pin("folder:1", 1, cell))
    session = Session(source, session.dir)
    assert session.require().pins == {"folder:1": cell}
    assert apply(session)["status"] == "verified"
    wire_folder = source.payloads[-1]["iconLists"][0][0]
    uid = wire_folder["uniqueIdentifier"]
    assert metadata(source.payloads[-1])["fixedLocations"] == {uid: cell}
    assert source.pins == {uid: cell}
    session = Session(source, session.dir)
    assert session.require().pins == {f"folder:{uid}": cell}
    session.edit(lambda lay: lay.pin(f"folder:{uid}", 1, 0))
    second = apply(session)
    backup = plistlib.loads((session._dev_dir("backups") / second["backup"]).read_bytes())
    assert backup["state"][1][0]["uniqueIdentifier"] == uid
    assert backup["pins"] == [{uid: cell}]
    assert run(session.restore(second["backup"]))["status"] == "verified"
    assert source.pins == {uid: cell}
    assert source.raw["iconLists"][0][0]["iconLists"] == [["example.a", "example.b"]]


def test_backup_prepares_unidentified_folder_from_remembered_pin(tmp_path):
    session, source = loaded(tmp_path, folder_state(False))
    # USB-like reads omit pin metadata; the session remembers the previous placement.
    source.raw = folder_state(False)
    session._gaps_path.write_text('{"folder:Tools": 23}')
    run(session.read())
    result = apply(session)
    backup = plistlib.loads((session._dev_dir("backups") / result["backup"]).read_bytes())
    uid = backup["state"][1][0]["uniqueIdentifier"]
    assert backup["pins"] == [{uid: 23}]
    assert run(session.restore(result["backup"]))["status"] == "verified"
    assert source.pins == {uid: 23}


@pytest.mark.parametrize("operation", ["apply", "restore"])
@pytest.mark.parametrize("cols,rows,cell", [(4, 6, 23), (5, 7, 34)])
@pytest.mark.parametrize("wrong", [False, True])
def test_folder_readback_checks_coordinates(tmp_path, operation, cols, rows, cell, wrong):
    session, source = loaded(tmp_path, folder_state(), cols, rows)
    session.edit(lambda lay: lay.pin("folder:folder-id", 1, cell))
    saved = session._backup(session.require().to_state(), [], "synthetic", [{"folder-id": cell}])
    original_write = source.set_icon_state

    async def write(payload):
        await original_write(payload)
        if wrong:
            metadata(source.raw)["fixedLocations"]["folder-id"] = 0

    source.set_icon_state = write
    result = apply(session) if operation == "apply" else run(session.restore(saved.name))
    assert result["status"] == ("unknown" if wrong else "verified")
    assert session._pending_path.exists() is wrong


@pytest.mark.parametrize("operation", ["apply", "restore"])
@pytest.mark.parametrize("change", ["none", "children", "name", "missing-child", "pages", "dock-order", "identifier"])
def test_dock_folder_readback_compares_semantics_and_order(tmp_path, operation, change):
    item = folder_state()[1][0]
    item["iconLists"].append([app("example.c", "C")])
    state = [[item, app("example.d", "D")], [app("example.e", "E")]]
    session, source = loaded(tmp_path, state)
    saved = session._backup(session.require().to_state(), [], "synthetic")
    original_write = source.set_icon_state

    async def write(payload):
        await original_write(payload)
        dock = source.raw["buttonBar"]
        if change == "children":
            dock[0]["iconLists"][0].reverse()
        elif change == "name":
            dock[0]["displayName"] = "Changed"
        elif change == "missing-child":
            dock[0]["iconLists"][0].pop()
        elif change == "pages":
            dock[0]["iconLists"].reverse()
        elif change == "dock-order":
            dock.reverse()
        elif change == "identifier":
            dock[0]["uniqueIdentifier"] = "regenerated-folder-id"

    source.set_icon_state = write
    result = apply(session) if operation == "apply" else run(session.restore(saved.name))
    matches = change in ("none", "identifier")
    assert result["status"] == ("verified" if matches else "unknown")
    assert session._pending_path.exists() is not matches


def test_unidentified_widget_pin_rejects_atomically_but_sequential_moves_work(tmp_path):
    item = widget("Weather", "small")
    del item["uniqueIdentifier"]
    item["displayIdentifier"] = "weather-widget"
    session, source = loaded(tmp_path, [[], [item, app("example.a", "A")]])
    session.edit(lambda lay: lay.move("example.a", "page:1", index=0))
    session.undo()  # preserve a nonempty redo stack through the rejected operation
    before, history = session.draft_path.read_bytes(), session.history()
    with pytest.raises(LayoutError, match="widget.*identifier|identifier.*widget"):
        session.edit(lambda lay: lay.pin("widget:weather-widget", 2, 16))
    assert session.draft_path.read_bytes() == before
    assert session.history() == history
    assert len(session.require().pages) == 1
    assert session.require().pins == {}
    assert source.payloads == []
    session.edit(lambda lay: lay.move("widget:weather-widget", "page:1", index=1))
    assert apply(session)["status"] == "verified"
    assert source.raw["iconLists"][0][1] == item


@pytest.mark.parametrize("identity", ["uniqueIdentifier", "userSelectedElementIdentifier", "remembered"])
@pytest.mark.parametrize("cols,rows,cell", [(4, 6, 18), (5, 7, 28)])
def test_widget_pins_preserve_configuration_and_resolve_known_identity(tmp_path, identity, cols, rows, cell):
    item = widget("Weather", "small")
    del item["uniqueIdentifier"]
    item.update(displayIdentifier="weather-widget", configuration=b"opaque", futureSetting={"enabled": True})
    item["userSelectedElementIdentifier" if identity == "remembered" else identity] = "widget-wire-id"
    session, source = loaded(tmp_path, [[], [item]], cols, rows)
    if identity == "remembered":
        del source.raw["iconLists"][0][0]["userSelectedElementIdentifier"]
        run(session.read())
    node_id = session.require().pages[0][0].id
    session.edit(lambda lay: lay.pin(node_id, 1, cell))
    assert apply(session)["status"] == "verified"
    assert source.pins == {"widget-wire-id": cell}
    assert source.raw["iconLists"][0][0] == item


def test_widget_configuration_change_in_readback_is_unknown(tmp_path):
    item = widget("Weather", "small")
    item["futureSetting"] = {"enabled": True}
    session, source = loaded(tmp_path, [[], [item]])
    session.edit(lambda lay: lay.pin(lay.pages[0][0].id, 1, 18))
    original_write = source.set_icon_state

    async def write(payload):
        await original_write(payload)
        source.raw["iconLists"][0][0]["futureSetting"]["enabled"] = False

    source.set_icon_state = write
    assert apply(session)["status"] == "unknown"
    assert session._pending_path.exists()


@pytest.mark.parametrize("cell,new_cols,new_rows,want", [(34, 5, 7, 34), (13, 4, 6, 11), (18, 4, 6, 15)])
def test_restore_maps_saved_row_and_column_to_loaded_grid(tmp_path, cell, new_cols, new_rows, want):
    session, source = loaded(tmp_path, [[], [app("example.a", "A")]], 5, 7)
    backup = session._backup(session.require().to_state(), [], "synthetic", [{"example.a": cell}])
    blob = plistlib.loads(backup.read_bytes())
    assert blob["provenance"]["grid"]["cols"] == 5
    assert blob["provenance"]["grid"]["rows"] == 7
    source.cols, source.rows = new_cols, new_rows
    run(session.read())
    assert run(session.restore(backup.name))["status"] == "verified"
    assert metadata(source.payloads[-1]) == {"fixedLocations": {"example.a": want},
                                           "fixedLocationsGridColumns": new_cols,
                                           "fixedLocationsGridRows": new_rows}
    assert source.pins == {"example.a": want}


@pytest.mark.parametrize("case", ["row", "column", "outside-old", "negative", "footprint", "unknown-key", "wrong-page",
                                 "legacy-pinned", "missing-columns", "missing-rows", "empty-grid"])
def test_restore_rejects_unrepresentable_pins_before_any_write_or_backup(tmp_path, case):
    item = widget("Weather", "small") if case == "footprint" else app("example.a", "A")
    key = "W-Weather-small" if case == "footprint" else "example.a"
    cells = {"row": 34, "column": 4, "outside-old": 35, "negative": -1, "footprint": 23}
    pins = [{key: cells.get(case, 0)}]
    if case == "unknown-key":
        pins = [{"absent": 0}]
    if case == "wrong-page":
        pins = [{}, {key: 0}]
    session, source = loaded(tmp_path, [[], [item]], 5, 7)
    backup = session._backup(session.require().to_state(), [], "synthetic", pins)
    if case == "legacy-pinned":
        blob = plistlib.loads(backup.read_bytes())
        blob["provenance"].pop("grid", None)
        backup.write_bytes(plistlib.dumps(blob))
    elif case in ("missing-columns", "missing-rows", "empty-grid"):
        blob = plistlib.loads(backup.read_bytes())
        if case == "empty-grid":
            blob["provenance"]["grid"] = {}
        else:
            blob["provenance"]["grid"].pop("cols" if case == "missing-columns" else "rows")
        backup.write_bytes(plistlib.dumps(blob))
    source.cols, source.rows = 4, 6
    run(session.read())
    session.edit(lambda lay: lay.move(lay.pages[0][0].id, "page:1"))
    before = copy.deepcopy(session._snap())
    persisted, history, backups = session.draft_path.read_bytes(), session.history(), session.backups()
    with pytest.raises(LayoutError, match="pin|grid|fit|page"):
        run(session.restore(backup.name))
    assert source.payloads == []
    assert session._snap() == before
    assert session.draft_path.read_bytes() == persisted
    assert session.history() == history
    assert session.backups() == backups
    assert not session._pending_path.exists()


def test_trusted_legacy_backup_without_pins_still_restores(tmp_path):
    session, source = loaded(tmp_path, [[], [app("example.a", "A")]])
    backup = session._backup(session.require().to_state(), [], "synthetic")
    blob = plistlib.loads(backup.read_bytes())
    blob["provenance"].pop("grid", None)
    backup.write_bytes(plistlib.dumps(blob))
    assert run(session.restore(backup.name))["status"] == "verified"


def test_comparison_does_not_normalize_invalid_requested_pin_to_success():
    with pytest.raises(LayoutError, match="pin|fit|grid"):
        placed_structure([[], [app("example.a", "A")]], [], {"example.a": 34})


@pytest.mark.parametrize("observed", [34, -1])
def test_invalid_observed_pin_cannot_verify_requested_first_cell(tmp_path, observed):
    session, source = loaded(tmp_path, [[], [app("example.a", "A")]])
    session.edit(lambda lay: lay.pin("example.a", 1, 0))
    original_write = source.set_icon_state

    async def write(payload):
        await original_write(payload)
        metadata(source.raw)["fixedLocations"]["example.a"] = observed

    source.set_icon_state = write
    result = apply(session)
    assert result["status"] == "unknown"
    assert session._pending_path.exists()


def test_folder_preparation_does_not_mutate_draft_when_prewrite_is_rejected(tmp_path):
    session, source = loaded(tmp_path, folder_state(False))
    session.edit(lambda lay: lay.pin("folder:1", 1, 23))
    before, history = session.draft_path.read_bytes(), session.history()
    snap = copy.deepcopy(session._snap())
    source.raw["iconLists"][0][0]["displayName"] = "Changed externally"
    with pytest.raises(LayoutError, match="changed"):
        apply(session)
    assert session._snap() == snap
    assert session.draft_path.read_bytes() == before
    assert session.history() == history
    reloaded = Session(source, session.dir)
    assert reloaded.require().pins == {"folder:1": 23}
    assert reloaded.require().pages[0][0].id == "folder:1"
    assert source.payloads == []


class USBArchiveSource(ArchiveSource):
    """Serialize every write, but omit gap metadata and folder identity on every read."""

    def __init__(self, cols=4, rows=6, identifiers="omitted"):
        super().__init__(cols, rows, "dictionary")
        self.identifiers = identifiers
        self.read_count = 0
        self.reverse_folders = False

    async def read(self):
        state, library = await super().read()
        state = copy.deepcopy(state)
        self.pins = None
        self.read_count += 1
        for page in state:
            for index, item in enumerate(page):
                if item.get("listType") == "folder":
                    item.pop("uniqueIdentifier", None)
                    if self.identifiers == "regenerated":
                        item["uniqueIdentifier"] = f"returned-{self.read_count}-{index}"
        if self.reverse_folders and self.payloads:
            state[1].reverse()
        return state, library


@pytest.mark.parametrize("identifiers", ["omitted", "regenerated"])
@pytest.mark.parametrize("cols,rows,cell", [(4, 6, 23), (5, 7, 34)])
def test_usb_folder_pin_survives_apply_reload_read_and_restore(tmp_path, identifiers, cols, rows, cell):
    source = USBArchiveSource(cols, rows, identifiers)
    source.raw = to_root_archive(folder_state(False), [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin(lay.pages[0][0].id, 1, cell))
    result = apply(session)
    assert result["status"] == "verified"
    assert not session._pending_path.exists()
    outgoing = source.payloads[-1]
    uid = outgoing["iconLists"][0][0]["uniqueIdentifier"]
    assert metadata(outgoing)["fixedLocations"] == {uid: cell}
    assert source.pins is None
    assert session.require().summary()["pages"]["1"][0]["placed_at"] == {"row": rows, "column": cols}
    assert ("uniqueIdentifier" in session.require().pages[0][0].raw) is (identifiers == "regenerated")
    session = Session(source, session.dir)
    assert session.require().summary()["pages"]["1"][0]["placed_at"] == {"row": rows, "column": cols}
    run(session.read())
    assert session.require().summary()["pages"]["1"][0]["placed_at"] == {"row": rows, "column": cols}
    session.edit(lambda lay: lay.pin(lay.pages[0][0].id, 1, 0))
    second = apply(session)
    assert second["status"] == "verified"
    assert run(session.restore(second["backup"]))["status"] == "verified"
    run(session.read())
    assert session.require().summary()["pages"]["1"][0]["placed_at"] == {"row": rows, "column": cols}
    assert source.raw["iconLists"][0][0]["iconLists"] == [["example.a", "example.b"]]


def test_usb_same_name_folders_keep_distinct_content_positions(tmp_path):
    source = USBArchiveSource()
    source.raw = to_root_archive([[], [folder("Tools", [app("example.a", "A")]),
                                      folder("Tools", [app("example.b", "B")])]], [])
    source.reverse_folders = True
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("folder:1", 1, 23))
    session.edit(lambda lay: lay.pin("folder:2", 1, 18))
    assert apply(session)["status"] == "verified"
    session = Session(source, session.dir)
    run(session.read())
    contents_to_cell = {node.children[0][0].id: session.require().pins[node.id]
                        for node in session.require().pages[0]}
    assert contents_to_cell == {"example.a": 23, "example.b": 18}
    assert apply(session)["status"] == "verified"


@pytest.mark.parametrize("change", ["name", "children"])
def test_usb_changed_folder_semantics_remain_unknown(tmp_path, change):
    source = USBArchiveSource()
    source.raw = to_root_archive(folder_state(False), [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("folder:1", 1, 23))
    original_write = source.set_icon_state

    async def write(payload):
        await original_write(payload)
        item = source.raw["iconLists"][0][0]
        if change == "name":
            item["displayName"] = "Changed"
        else:
            item["iconLists"][0].reverse()

    source.set_icon_state = write
    assert apply(session)["status"] == "unknown"
    assert session._pending_path.exists()
    assert not session._gaps_path.exists()


@pytest.mark.parametrize("same_name", [False, True])
def test_usb_legacy_folder_memory_only_migrates_unambiguous_names(tmp_path, same_name):
    source = USBArchiveSource()
    state = folder_state(False)
    if same_name:
        state.append([folder("Tools", [app("example.c", "C")])])
    source.raw = to_root_archive(state, [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    session._gaps_path.write_text('{"folder:Tools": 23}')
    run(session.read())
    if same_name:
        assert session.require().pins == {}
    else:
        assert session.require().pins == {"folder:1": 23}
        assert apply(session)["status"] == "verified"
        assert '"folder:Tools"' not in session._gaps_path.read_text()
        run(session.read())
        assert session.require().pins == {"folder:1": 23}


def test_usb_indistinguishable_folders_do_not_guess_a_pin_owner(tmp_path):
    source = USBArchiveSource()
    source.raw = to_root_archive([[], [folder("Empty", []), folder("Empty", [])]], [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("folder:1", 1, 23))
    assert apply(session)["status"] == "unknown"
    assert session._pending_path.exists()
    assert not session._gaps_path.exists()


@pytest.mark.parametrize("reported", [{}, {"absent-folder-id": 23}])
def test_reported_folder_pins_never_use_usb_semantic_fallback(tmp_path, reported):
    source = USBArchiveSource()
    source.raw = to_root_archive(folder_state(False), [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("folder:1", 1, 23))
    original_read = source.read

    async def read():
        state, library = await original_read()
        if source.payloads:
            source.pins = reported
        return state, library

    source.read = read
    assert apply(session)["status"] == "unknown"
    assert session._pending_path.exists()
    assert not session._gaps_path.exists()
