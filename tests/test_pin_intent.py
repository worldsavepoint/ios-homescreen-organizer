"""An exported natural coordinate must also retain each requested fixed anchor."""

import copy
import json

import pytest

from fake_phone import app, folder
from homescreen.device import to_root_archive
from homescreen.session import Session
from test_grid_serialization import metadata
from test_observed_placement import PhoneArchiveSource
from test_pin_integrity import apply, run


def snapshot(session):
    return (copy.deepcopy(session._snap()), session.draft_path.read_bytes(), session.history(),
            session._gaps_path.read_bytes())


@pytest.mark.parametrize("operation", ["apply", "restore"])
@pytest.mark.parametrize("reported", ["empty-map", "empty-locations", "retained"])
@pytest.mark.parametrize("cell", [0, 1, 5])
def test_exported_map_must_keep_requested_pin_even_at_natural_cell(tmp_path, operation, reported, cell):
    source = PhoneArchiveSource(4, 6, "dictionary")
    items = [app("example.a", "A"), app("example.b", "B")]
    if cell == 1:
        items.reverse()  # A's unpinned natural coordinate is now cell 1.
    source.raw = to_root_archive([[], items], [], [{"example.a": 0}])
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("example.a", 1, cell))
    session._remember_gaps(session.require())
    backup = session._backup(session.require().to_state(), [], "synthetic", [{"example.a": cell}])
    before = snapshot(session)
    original_write = source.set_icon_state

    async def write(payload):
        await original_write(payload)
        if reported == "empty-map":
            source.raw["listMetadata"] = {}
        elif reported == "empty-locations":
            metadata(source.raw)["fixedLocations"] = {}

    source.set_icon_state = write
    result = apply(session) if operation == "apply" else run(session.restore(backup.name))
    assert len(source.payloads) == 1
    if reported == "retained":
        assert result["status"] == "verified"
        assert session.require().pins == {"example.a": cell}
        assert json.loads(session._gaps_path.read_text()) == {
            "version": 2, "grid": {"cols": 4, "rows": 6, "dock": 4, "folder": 9},
            "pages": [{"example.a": cell}]}
        assert not session._pending_path.exists()
    else:
        assert result["status"] == "unknown"
        assert session._pending_path.exists()
        assert source.pins == {}
        assert snapshot(session) == before


@pytest.mark.parametrize("operation", ["apply", "restore"])
@pytest.mark.parametrize("change", ["regenerated", "missing", "contents"])
def test_reported_folder_pin_uses_contents_with_regenerated_identity(tmp_path, operation, change):
    source = PhoneArchiveSource(4, 6, "dictionary")
    item = folder("Tools", [app("example.a", "A"), app("example.b", "B")])
    item["uniqueIdentifier"] = "folder-before"
    source.raw = to_root_archive([[], [item]], [], [{"folder-before": 0}])
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("folder:folder-before", 1, 0))
    session._remember_gaps(session.require())
    backup = session._backup(session.require().to_state(), [], "synthetic", [{"folder-before": 0}])
    before = snapshot(session)
    original_write = source.set_icon_state

    async def write(payload):
        await original_write(payload)
        current = source.raw["iconLists"][0][0]
        current["uniqueIdentifier"] = "folder-after"
        metadata(source.raw)["fixedLocations"] = {} if change == "missing" else {"folder-after": 0}
        if change == "contents":
            current["iconLists"][0].reverse()

    source.set_icon_state = write
    result = apply(session) if operation == "apply" else run(session.restore(backup.name))
    assert result["status"] == ("verified" if change == "regenerated" else "unknown")
    if change == "regenerated":
        assert session.require().pins == {"folder:folder-after": 0}
        assert session._gaps_path.read_bytes() == before[3]
        assert not session._pending_path.exists()
    else:
        assert session._pending_path.exists()
        assert snapshot(session) == before


@pytest.mark.parametrize("reported", ["retained", "missing", "configuration", "identifier"])
def test_single_widget_alias_keeps_explicit_pin_and_strict_configuration(tmp_path, reported):
    source = PhoneArchiveSource(4, 6, "root")
    source.raw = [[], [{"displayName": "Weather", "gridSize": "small", "displayIdentifier": "weather-widget",
                       "userSelectedElementIdentifier": "element-123", "configuration": b"opaque"}]]
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin(lay.pages[0][0].id, 1, 0))
    session._remember_gaps(session.require())
    before = snapshot(session)
    original_write = source.set_icon_state

    async def write(payload):
        await original_write(payload)
        item = source.raw["iconLists"][0][0]
        if reported == "missing":
            metadata(source.raw)["fixedLocations"] = {}
        elif reported == "configuration":
            item["configuration"] = b"different"
        elif reported == "identifier":
            item["uniqueIdentifier"] = "different-id"
            metadata(source.raw)["fixedLocations"] = {"different-id": 0}

    source.set_icon_state = write
    assert apply(session)["status"] == ("verified" if reported == "retained" else "unknown")
    assert source.payloads[-1]["iconLists"][0][0]["uniqueIdentifier"] == "element-123"
    if reported == "retained":
        assert session.require().pins == {"widget:element-123": 0}
        assert session._gaps_path.read_bytes() == before[3]
        assert not session._pending_path.exists()
    else:
        assert session._pending_path.exists()
        assert snapshot(session) == before


def test_additional_observed_canonical_pin_can_preserve_the_same_layout(tmp_path):
    source = PhoneArchiveSource(4, 6, "dictionary")
    source.raw = to_root_archive([[], [app("example.a", "A"), app("example.b", "B")]], [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("example.a", 1, 0))
    original_write = source.set_icon_state

    async def write(payload):
        await original_write(payload)
        metadata(source.raw)["fixedLocations"]["example.b"] = 1

    source.set_icon_state = write
    assert apply(session)["status"] == "verified"
    assert session.require().pins == {"example.a": 0, "example.b": 1}
    assert not session._pending_path.exists()
