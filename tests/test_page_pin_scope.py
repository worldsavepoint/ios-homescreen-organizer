"""Duplicate icons retain their own page's evidence, intent and remembered frame."""

import copy
import json

import pytest

from fake_phone import app, folder
from homescreen.device import to_root_archive
from homescreen.model import Grid, LayoutError
from homescreen.session import Session
from test_live_frame import FrameSource, external_traps, reviewed
from test_pin_integrity import apply, run


def duplicate_session(tmp_path, pins=None, reported=True):
    source = FrameSource(reported)
    source.metrics = None
    source.raw = to_root_archive([[], [app("example.a", "A")], [app("example.a", "A")]], [], pins)
    session = Session(source, tmp_path / "data")
    run(session.read())
    return session, source


def page_cells(session):
    lay = session.require()
    return [[lay.pins.get(n.id) for n in page] for page in lay.pages]


def test_observed_duplicate_bundle_pin_only_belongs_to_exporting_page(tmp_path):
    session, source = duplicate_session(tmp_path, [{"example.a": 23}, {}])
    assert page_cells(session) == [[23], [None]]
    assert apply(session)["status"] == "verified"
    ids = source.raw["listUniqueIdentifiers"]
    assert ids[1] not in source.raw["listMetadata"]


@pytest.mark.parametrize("operation", ["apply", "restore"])
@pytest.mark.parametrize("missing", [False, True])
def test_duplicate_bundle_requests_keep_each_page_anchor(tmp_path, operation, missing):
    session, source = duplicate_session(tmp_path)
    for page, cell in [(1, 0), (2, 23)]:
        session.edit(lambda lay: lay.pin(lay.pages[page - 1][0].id, page, cell))
    session._remember_gaps(session.require())
    backup = session._backup(session.require().to_state(), [], "synthetic", [{"example.a": 0}, {"example.a": 23}])
    before = reviewed(session)
    original = source.set_icon_state

    async def write(payload):
        await original(payload)
        if missing:
            del source.raw["listMetadata"][source.raw["listUniqueIdentifiers"][0]]

    source.set_icon_state = write
    result = apply(session) if operation == "apply" else run(session.restore(backup.name))
    assert result["status"] == ("unknown" if missing else "verified")
    assert session._pending_path.exists() is missing
    if missing:
        assert reviewed(session) == before
    else:
        assert page_cells(session) == [[0], [23]]
        session = Session(source, session.dir)
        run(session.read())
        assert page_cells(session) == [[0], [23]]


@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_regenerated_colliding_folder_wire_ids_do_not_hide_wrong_page_cell(tmp_path, operation):
    session, source = duplicate_session(tmp_path)
    first = folder("Tools", [app("example.a", "A")])
    second = folder("Tools", [app("example.b", "B")])
    first["uniqueIdentifier"], second["uniqueIdentifier"] = "first", "second"
    source.raw = to_root_archive([[], [first], [second]], [])
    run(session.read())
    session.edit(lambda lay: lay.pin("folder:first", 1, 5))
    session.edit(lambda lay: lay.pin("folder:second", 2, 5))
    session._remember_gaps(session.require())
    backup = session._backup(session.require().to_state(), [], "synthetic", [{"first": 5}, {"second": 5}])
    before = reviewed(session)
    original = source.set_icon_state

    async def write(payload):
        await original(payload)
        for page, cell in [(0, 1), (1, 5)]:
            source.raw["iconLists"][page][0]["uniqueIdentifier"] = "shared"
            source.raw["listMetadata"][source.raw["listUniqueIdentifiers"][page]]["fixedLocations"] = {"shared": cell}

    source.set_icon_state = write
    result = apply(session) if operation == "apply" else run(session.restore(backup.name))
    assert result["status"] == "unknown"
    assert session._pending_path.exists()
    assert reviewed(session) == before


def test_unreported_duplicate_pins_survive_adoption_reload_read_and_restore(tmp_path):
    session, source = duplicate_session(tmp_path, reported=False)
    for page, cell in [(1, 0), (2, 23)]:
        session.edit(lambda lay: lay.pin(lay.pages[page - 1][0].id, page, cell))
    assert apply(session)["status"] == "verified"
    assert page_cells(session) == [[0], [23]]
    session = Session(source, session.dir)
    run(session.read())
    assert page_cells(session) == [[0], [23]]
    session.edit(lambda lay: lay.pin(lay.pages[1][0].id, 2, 10))
    result = apply(session)
    assert result["status"] == "verified"
    assert run(session.restore(result["backup"]))["status"] == "verified"
    assert page_cells(session) == [[0], [23]]


@pytest.mark.parametrize("cell,want", [(13, 11), (34, None)])
def test_remembered_frame_preserves_coordinates_or_normalizes_nonfitting_page(tmp_path, cell, want):
    source = FrameSource(False)
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("example.a", 1, cell))
    assert apply(session)["status"] == "verified"
    source.metrics = None
    run(session.read())
    assert session.grid == Grid()
    assert page_cells(session) == [[want]]


def test_legacy_global_memory_does_not_guess_between_duplicate_pages(tmp_path):
    session, source = duplicate_session(tmp_path, reported=False)
    session._gaps_path.write_text(json.dumps({"example.a": 23}))
    run(session.read())
    assert page_cells(session) == [[None], [None]]


@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_same_page_ambiguous_wire_pin_rejects_without_mutation(tmp_path, operation):
    session, source = duplicate_session(tmp_path)
    source.raw = to_root_archive([[], [app("example.a", "A"), app("example.a", "A")]], [])
    run(session.read())
    session.edit(lambda lay: lay.pin(lay.pages[0][0].id, 1, 23))
    backup = session._backup(session.require().to_state(), [], "synthetic", [{"example.a": 23}])
    before, backups = reviewed(session), session.backups()
    with pytest.raises(LayoutError, match="ambiguous|identify|pin"):
        apply(session) if operation == "apply" else run(session.restore(backup.name))
    assert reviewed(session) == before
    assert session.backups() == backups
    assert source.payloads == []
    assert not session._pending_path.exists()


def test_legacy_reported_map_cannot_drop_a_natural_source_anchor(tmp_path):
    source = FrameSource()
    original = source.read
    drop = False

    async def read():
        state, library = await original()
        source.pins = {} if drop else {"example.a": 0}
        return state, library

    source.read = read
    session = Session(source, tmp_path / "data")
    run(session.read())
    before = reviewed(session)
    drop = True
    with pytest.raises(LayoutError, match="gaps changed"):
        apply(session)
    assert source.payloads == []
    assert reviewed(session) == before
    assert not session._pending_path.exists()


def test_unreported_semantic_mismatch_preserves_reviewed_draft_and_memory(tmp_path):
    session, source = duplicate_session(tmp_path, reported=False)
    session.edit(lambda lay: lay.pin(lay.pages[0][0].id, 1, 0))
    session._remember_gaps(session.require())
    before = reviewed(session)
    original = source.set_icon_state

    async def write(payload):
        await original(payload)
        source.raw["iconLists"][1].append("example.unexpected")

    source.set_icon_state = write
    assert apply(session)["status"] == "unknown"
    assert session._pending_path.exists()
    assert reviewed(session) == before


@pytest.mark.parametrize("other_folder", [False, True])
def test_legacy_folder_content_memory_with_enumerated_child(tmp_path, other_folder):
    source = FrameSource(False)
    repeated = app("example.a", "A")
    page = [repeated, folder("Tools", [repeated])]
    if other_folder:
        page += [app("example.b", "B"), folder("Tools", [app("example.b", "B")])]
    source.raw = to_root_archive([[], page], [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    # The old global key encoded the duplicate child's enumeration, not its bundle.
    session._gaps_path.write_text(json.dumps({'folder-content:["Tools",[["app:1"]]]': 23}))
    run(session.read())
    assert session.require().pins == ({} if other_folder else {"folder:1": 23})
    if not other_folder:
        assert apply(session)["status"] == "verified"
        assert json.loads(session._gaps_path.read_text())["pages"] == [
            {'folder-content:["Tools",[["example.a"]]]': 23}]
