"""Local mementos retain node ownership independently of wire serialization order."""

import copy
import plistlib

import pytest

from fake_phone import app, folder, widget
from homescreen.device import to_root_archive
from homescreen.model import LayoutError
from homescreen.session import Session
from test_live_frame import FrameSource, external_traps, reviewed
from test_pin_integrity import apply, run


def folder_session(tmp_path, same_name=False):
    source = FrameSource(False)
    source.raw = to_root_archive([[], [folder("Tools" if same_name else "A", [app("example.a", "A")]),
                                      folder("Tools" if same_name else "B", [app("example.b", "B")]),
                                      app("example.c", "C")]], [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    return session, source


def contents(lay):
    return {n.id: tuple(c.bundle_id for p in n.children for c in p)
            for n in lay.nodes.values() if n.kind == "folder"}


@pytest.mark.parametrize("same_name", [False, True])
@pytest.mark.parametrize("restore", ["reload", "redo"])
def test_folder_pin_and_source_identity_survive_reordered_serialization(tmp_path, same_name, restore):
    session, source = folder_session(tmp_path, same_name)
    session.edit(lambda lay: lay.pin("folder:1", 1, 34))
    # A source pin belongs to the original A independently of draft reorder and rename.
    session.require().source_pins = {"folder:1": 0}
    session.edit(lambda lay: lay.move("folder:2", "page:1", index=0))
    session.edit(lambda lay: lay.rename_folder("folder:1", "Renamed"))
    if restore == "reload":
        session = Session(source, session.dir)
    else:
        assert session.undo()
        assert session.redo()
    lay = session.require()
    assert contents(lay)["folder:1"] == ("example.a",)
    assert lay.get("folder:1").name == "Renamed"
    assert lay.pins == {"folder:1": 34}
    assert contents(lay.original())["folder:1"] == ("example.a",)
    assert lay.original().pins == {"folder:1": 0}
    assert apply(session)["status"] == "verified"
    pinned = [n for p in session.require().pages for n in p if n.id in session.require().pins]
    assert [n.children[0][0].bundle_id for n in pinned] == ["example.a"]


@pytest.mark.parametrize("destination", ["bank", "page:2", "folder:2"])
def test_item_ids_survive_bank_page_and_folder_moves(tmp_path, destination):
    session, source = folder_session(tmp_path)
    item = "example.a" if destination == "folder:2" else "folder:1"
    session.edit(lambda lay: lay.move(item, destination))
    expected = {k: v for k, v in session.require().containers().items() if v}
    session = Session(source, session.dir)
    assert {k: v for k, v in session.require().containers().items() if v} == expected
    assert contents(session.require())["folder:1"] == (() if destination == "folder:2" else ("example.a",))


@pytest.mark.parametrize("restore", ["reload", "redo"])
def test_new_allocation_cannot_reuse_restored_folder_or_widget_id(tmp_path, restore):
    session, source = folder_session(tmp_path)
    # A generated widget id and a banked folder must also be reserved by the snapshot.
    item = widget("Weather", "small")
    del item["uniqueIdentifier"]
    source.raw["iconLists"][0].append(item)
    run(session.read())
    session.edit(lambda lay: lay.move("folder:1", "bank"))
    session.edit(lambda lay: lay.move("folder:2", "page:1", index=0))
    session.edit(lambda lay: lay.move("widget:1", "bank"))
    if restore == "reload":
        session = Session(source, session.dir)
    else:
        session.undo()
        session.redo()
    new_id = session.edit(lambda lay: lay.create_folder("New", ["example.c"], "page:1"))
    assert new_id not in {"folder:1", "folder:2"}
    assert contents(session.require())["folder:1"] == ("example.a",)
    assert contents(session.require())["folder:2"] == ("example.b",)
    generated = session.require()._wrap(copy.deepcopy(item), "bank")
    assert generated.id != "widget:1"
    assert session.require().get("widget:1") in session.require().bank


def test_rejected_edit_and_apply_keep_reordered_draft_identity_and_history(tmp_path):
    session, source = folder_session(tmp_path)
    session.edit(lambda lay: lay.pin("folder:1", 1, 34))
    session.edit(lambda lay: lay.move("folder:2", "page:1", index=0))
    before = reviewed(session)

    def rejected(lay):
        lay.rename_folder("folder:1", "Temporary")
        lay.create_folder("Temporary", ["example.c"], "page:1")
        raise LayoutError("reject synthetic edit")

    with pytest.raises(LayoutError, match="reject"):
        session.edit(rejected)
    assert reviewed(session) == before
    assert contents(session.require())["folder:1"] == ("example.a",)
    source.metrics = None
    with pytest.raises(LayoutError, match="grid"):
        apply(session)
    assert reviewed(session) == before
    assert source.payloads == []


@pytest.mark.parametrize("stable", [False, True])
def test_legacy_draft_migrates_unambiguous_identity_or_allows_recovery_read(tmp_path, stable):
    session, source = folder_session(tmp_path)
    if stable:
        source.raw["iconLists"][0][0]["uniqueIdentifier"] = "stable-a"
        run(session.read())
    node_id = "folder:stable-a" if stable else "folder:1"
    session.edit(lambda lay: lay.pin(node_id, 1, 34))
    session.edit(lambda lay: lay.move("folder:1" if stable else "folder:2", "page:1", index=0))
    blob = plistlib.loads(session.draft_path.read_bytes())
    blob.pop("identity", None)
    blob.pop("source_identity", None)
    session.draft_path.write_bytes(plistlib.dumps(blob))
    saved = session.draft_path.read_bytes()
    session = Session(source, session.dir)
    if stable:
        assert session.require().pins == {node_id: 34}
        assert contents(session.require())[node_id] == ("example.a",)
        assert apply(session)["status"] == "verified"
    else:
        with pytest.raises(LayoutError, match="identity.*read|identity.*Read"):
            session.require()
        assert session.draft_path.read_bytes() == saved
        run(session.read())
        assert session.require().pins == {}
        assert apply(session)["status"] == "verified"


def test_legacy_duplicate_app_identity_cannot_transfer_between_pages(tmp_path):
    source = FrameSource(False)
    source.raw = to_root_archive([[], [app("example.a", "A")], [app("example.a", "A")]], [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("example.a", 1, 34))
    session.edit(lambda lay: lay.move_page(1, 2))
    blob = plistlib.loads(session.draft_path.read_bytes())
    blob.pop("identity", None)
    blob.pop("source_identity", None)
    session.draft_path.write_bytes(plistlib.dumps(blob))
    session = Session(source, session.dir)
    with pytest.raises(LayoutError, match="identity.*read|identity.*Read"):
        session.require()
    run(session.read())
    assert apply(session)["status"] == "verified"


def test_rejected_edit_preserves_raw_folder_fields_after_rename(tmp_path):
    session, source = folder_session(tmp_path)
    session.edit(lambda lay: lay.rename_folder("folder:1", "Renamed"))
    before = {nid: copy.deepcopy(node.raw) for nid, node in session.require().nodes.items()}

    def rejected(lay):
        lay.move("folder:2", "page:1", index=0)
        raise LayoutError("reject edit")

    with pytest.raises(LayoutError, match="reject"):
        session.edit(rejected)
    assert {nid: node.raw for nid, node in session.require().nodes.items()} == before


def test_rejected_apply_preserves_source_raw_ids_with_remembered_folder_pin(tmp_path):
    session, source = folder_session(tmp_path)
    session.edit(lambda lay: lay.pin("folder:1", 1, 34))
    session._remember_gaps(session.require())
    run(session.read())
    before = reviewed(session)
    source.metrics = None
    with pytest.raises(LayoutError, match="grid"):
        apply(session)
    assert reviewed(session) == before
    assert "uniqueIdentifier" not in session.require().source[1][0]


def test_legacy_numeric_wire_id_cannot_claim_a_reordered_folders_generated_id(tmp_path):
    session, source = folder_session(tmp_path)
    source.raw["iconLists"][0][1]["uniqueIdentifier"] = "1"
    run(session.read())
    session.edit(lambda lay: lay.pin("folder:1", 1, 34))  # UUID-less A was first on Read.
    session.edit(lambda lay: lay.move("folder:2", "page:1", index=0))
    blob = plistlib.loads(session.draft_path.read_bytes())
    del blob["identity"], blob["source_identity"]
    session.draft_path.write_bytes(plistlib.dumps(blob))
    saved = session.draft_path.read_bytes()
    session = Session(source, session.dir)
    with pytest.raises(LayoutError, match="identity.*read"):
        session.require()
    assert session.draft_path.read_bytes() == saved
    run(session.read())
    assert session.require().pins == {}
