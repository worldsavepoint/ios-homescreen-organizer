"""Observed archive evidence and restore capacity using in-memory transports only."""

import copy
import plistlib

import pytest

from fake_phone import app, widget
from homescreen.device import FileSource, PhoneSource, to_root_archive
from homescreen.model import LayoutError
from homescreen.session import Session
from test_grid_serialization import ArchiveSource, metadata
from test_pin_integrity import apply, loaded, run


class PhoneArchiveSource(ArchiveSource):
    """Use the production reader and writer; replace only external transport hooks."""

    read = PhoneSource.read

    async def _ld(self):
        raise AssertionError("synthetic transport must never discover a device")


@pytest.mark.parametrize("source_type", [ArchiveSource, PhoneArchiveSource])
@pytest.mark.parametrize("damage", ["negative-fraction", "fraction", "bool", "string", "wrong-page",
                                    "orphan-page", "columns", "rows", "bool-columns", "cell", "empty"])
def test_reported_archive_damage_keeps_write_pending(tmp_path, source_type, damage):
    source = source_type(4, 6, "dictionary")
    source.raw = to_root_archive([[], [app("example.a", "A")], [app("example.b", "B")]], [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    cell = 0 if damage == "negative-fraction" else 1 if damage == "bool" else 5
    session.edit(lambda lay: lay.pin("example.a", 1, cell))
    original_write = source.set_icon_state

    async def write(payload):
        await original_write(payload)
        meta = metadata(source.raw)
        values = {"negative-fraction": -0.5, "fraction": 5.75, "bool": True, "string": "5"}
        if damage in values:
            meta["fixedLocations"]["example.a"] = values[damage]
        elif damage in ("wrong-page", "orphan-page"):
            ids = source.raw["listUniqueIdentifiers"]
            source.raw["listMetadata"][ids[1] if damage == "wrong-page" else "unknown-page"] = (
                source.raw["listMetadata"].pop(ids[0]))
        elif damage == "empty":
            source.raw["listMetadata"] = {}
        elif damage == "cell":
            meta["fixedLocations"]["example.a"] = 0
        else:
            key = "fixedLocationsGridRows" if damage == "rows" else "fixedLocationsGridColumns"
            meta[key] = True if damage == "bool-columns" else 7 if damage == "rows" else 5

    source.set_icon_state = write
    assert apply(session)["status"] == "unknown"
    assert session._pending_path.exists()
    assert not session._gaps_path.exists()


def test_phone_reader_distinguishes_exported_empty_metadata_and_clears_each_read(tmp_path):
    source = PhoneArchiveSource(4, 6, "dictionary")
    state = [[], [app("example.a", "A")]]
    source.raw = to_root_archive(state, [], [{"example.a": 23}])
    run(source.read())
    assert source.pins == {"example.a": 23}
    source.raw["listMetadata"] = {}
    run(source.read())
    assert source.pins == {}
    del source.raw["listMetadata"]
    run(source.read())
    assert source.pins is None
    source.raw = to_root_archive(state, [], [{"example.a": 23}])
    run(source.read())
    source.raw = state
    run(source.read())
    assert source.pins is None
    assert source._last_dict is None
    path = tmp_path / "disk.plist"
    path.write_bytes(plistlib.dumps(to_root_archive(state, [])))
    file_source = FileSource(path)
    run(file_source.read())
    assert file_source.pins == {}


def test_phone_list_readback_retains_unreported_pin_contract(tmp_path):
    source = PhoneArchiveSource(4, 6, "root")
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("com.example.a", 1, 23))
    original_read = source.read_raw

    async def read_raw(version):
        if source.payloads:
            return [[], [app("com.example.a", "A"), app("com.example.b", "B")]]
        return await original_read(version)

    source.read_raw = read_raw
    assert apply(session)["status"] == "verified"
    assert source.pins is None
    assert session.require().pins == {"com.example.a": 23}
    assert not session._pending_path.exists()


def test_malformed_observed_read_is_not_reused_as_prewrite_proof(tmp_path):
    session, source = loaded(tmp_path, [[], [app("example.a", "A")]], pins=[{"example.a": 0}])
    metadata(source.raw)["fixedLocations"]["example.a"] = -0.5
    run(session.read())
    assert session.require().pins == {}
    with pytest.raises(LayoutError, match="pin|grid|metadata"):
        apply(session)
    assert source.payloads == []
    assert session.backups() == []
    assert not session._pending_path.exists()


@pytest.mark.parametrize("when", ["before-apply", "final-read"])
@pytest.mark.parametrize("change", ["columns", "page-id", "fraction"])
def test_observed_frame_changes_block_write_even_after_draft_reload(tmp_path, when, change):
    session, source = loaded(tmp_path, [[], [app("example.a", "A")]], pins=[{"example.a": 0}])
    session = Session(source, session.dir)
    original_read = source.read_raw
    reads = 0

    async def read_raw(version):
        nonlocal reads
        reads += 1
        if reads == (1 if when == "before-apply" else 2):
            if change == "columns":
                metadata(source.raw)["fixedLocationsGridColumns"] = 5
            elif change == "fraction":
                metadata(source.raw)["fixedLocations"]["example.a"] = 0.0
            else:
                old_id = source.raw["listUniqueIdentifiers"][0]
                source.raw["listUniqueIdentifiers"][0] = "different-page-id"
                source.raw["listMetadata"]["different-page-id"] = source.raw["listMetadata"].pop(old_id)
        return await original_read(version)

    source.read_raw = read_raw
    with pytest.raises(LayoutError, match="changed|grid|metadata|pin"):
        apply(session)
    assert source.payloads == []
    assert not session._pending_path.exists()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("fits", [False, True])
def test_restore_widget_uses_footprint_geometry(tmp_path, legacy, fits):
    # The legacy extralarge slot estimate is 32, but its actual footprint is 4x4.
    state = [[], [widget("Weather", "extralarge")]]
    if not fits:
        state[1].append(widget("Calendar", "large"))
    session, source = loaded(tmp_path, state)
    backup = session._backup(session.require().to_state(), [], "synthetic")
    if legacy:
        blob = plistlib.loads(backup.read_bytes())
        del blob["provenance"]["grid"]
        backup.write_bytes(plistlib.dumps(blob))
    if fits:
        assert run(session.restore(backup.name))["status"] == "verified"
        assert session.require().place(session.require().pages[0])[1] == []
    else:
        with pytest.raises(LayoutError, match="room|fit|grid"):
            run(session.restore(backup.name))
        assert source.payloads == []
        assert session.backups() == [backup.name]
        assert not session._pending_path.exists()


@pytest.mark.parametrize("count,pinned_other_page,legacy,new_grid", [
    (30, False, False, (4, 6)), (35, True, False, (4, 6)),
    (30, False, True, (4, 6)), (36, False, False, (5, 7)),
])
def test_restore_rejects_unpinned_overflow_before_side_effects(tmp_path, count, pinned_other_page, legacy, new_grid):
    state = [[], [app(f"example.a{i}", f"A{i}") for i in range(count)]]
    if pinned_other_page:
        state.append([app("example.pinned", "Pinned")])
    session, source = loaded(tmp_path, state, 5, 7)
    pins = [{}, {"example.pinned": 0}] if pinned_other_page else None
    backup = session._backup(session.require().to_state(), [], "synthetic", pins)
    if legacy:
        blob = plistlib.loads(backup.read_bytes())
        del blob["provenance"]["grid"]
        backup.write_bytes(plistlib.dumps(blob))
    source.cols, source.rows = new_grid
    source.raw = to_root_archive([[], [app("example.current", "Current")]], [])
    run(session.read())
    session.edit(lambda lay: lay.move("example.current", "page:1"))
    before = copy.deepcopy(session._snap()), session.draft_path.read_bytes(), session.history(), session.backups()
    with pytest.raises(LayoutError, match="room|slots|fit|grid"):
        run(session.restore(backup.name))
    assert source.payloads == []
    assert (session._snap(), session.draft_path.read_bytes(), session.history(), session.backups()) == before
    assert not session._pending_path.exists()


@pytest.mark.parametrize("legacy,new_grid,count", [(False, (5, 7), 30), (False, (4, 6), 24), (True, (4, 6), 24)])
def test_unpinned_restore_that_fits_loaded_grid_remains_supported(tmp_path, legacy, new_grid, count):
    state = [[], [app(f"example.a{i}", f"A{i}") for i in range(count)]]
    session, source = loaded(tmp_path, state, 5, 7)
    backup = session._backup(session.require().to_state(), [], "synthetic")
    if legacy:
        blob = plistlib.loads(backup.read_bytes())
        del blob["provenance"]["grid"]
        backup.write_bytes(plistlib.dumps(blob))
    source.cols, source.rows = new_grid
    source.raw = to_root_archive([[], [app("example.current", "Current")]], [])
    run(session.read())
    assert run(session.restore(backup.name))["status"] == "verified"
    assert session.require().place(session.require().pages[0])[1] == []


def test_current_source_widget_shape_still_applies_on_same_grid(tmp_path):
    session, source = loaded(tmp_path, [[], [widget("Weather", "large"), widget("Calendar", "large")]])
    assert session.require().validate() == []
    assert apply(session)["status"] == "verified"


@pytest.mark.parametrize("representation", ["root", "dictionary"])
@pytest.mark.parametrize("identity", ["selected", "remembered"])
@pytest.mark.parametrize("change", [None, "configuration", "uniqueIdentifier", "userSelectedElementIdentifier"])
def test_single_widget_wire_alias_preserves_configuration_verification(tmp_path, representation, identity, change):
    item = {"displayName": "Weather", "gridSize": "small", "displayIdentifier": "weather-widget",
            "userSelectedElementIdentifier": "element-123", "configuration": b"opaque"}
    source = PhoneArchiveSource(4, 6, representation)
    source.raw = [[], [item]] if representation == "root" else to_root_archive([[], [item]], [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    if identity == "remembered":
        raw_item = source.raw[1][0] if representation == "root" else source.raw["iconLists"][0][0]
        raw_item.pop("userSelectedElementIdentifier")
        raw_item.pop("uniqueIdentifier", None)
        run(session.read())
    session.edit(lambda lay: lay.pin(lay.pages[0][0].id, 1, 18))
    original_write = source.set_icon_state

    async def write(payload):
        await original_write(payload)
        if change:
            source.raw["iconLists"][0][0][change] = b"changed" if change == "configuration" else "other-id"

    source.set_icon_state = write
    assert apply(session)["status"] == ("unknown" if change else "verified")
    assert session._pending_path.exists() is bool(change)
    assert source.payloads[-1]["iconLists"][0][0]["configuration"] == b"opaque"
    if not change:
        backup = session.backups()[-1]
        assert run(session.restore(backup))["status"] == "verified"
