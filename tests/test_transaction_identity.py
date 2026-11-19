"""Session write approval and source identity across independent observations."""

import json
import plistlib

import pytest

from fake_phone import app, widget
from homescreen.device import FileSource, to_root_archive
from homescreen.model import LayoutError
from homescreen.session import Session
from test_live_frame import FrameSource, external_traps, reviewed
from test_page_pin_scope import duplicate_session
from test_pin_integrity import apply, run


def observed_session(tmp_path, pages):
    source = FrameSource()
    source.raw = to_root_archive([[], *pages], [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    return session, source


def change_read(source, at, change):
    original = source.read_raw
    count = 0

    async def read(version):
        nonlocal count
        count += 1
        if count == at:
            change(source.raw)
        return await original(version)

    source.read_raw = read


@pytest.mark.parametrize("at", [1, 2, None])
def test_duplicate_app_substitution_before_write(tmp_path, at):
    a, b = app("example.a", "A"), app("example.b", "B")
    session, source = observed_session(tmp_path, [[a, b], [a]])
    change_read(source, at, lambda raw: raw["iconLists"][1].__setitem__(0, "example.b"))
    if at is None:
        assert apply(session)["status"] == "verified"
    else:
        with pytest.raises(LayoutError, match="layout changed"):
            apply(session)
        assert source.payloads == [] and session.backups() == []
        assert not session._pending_path.exists()


@pytest.mark.parametrize("substitute", [False, True])
def test_readback_compares_duplicate_library_bundles(tmp_path, substitute):
    a, b, c = (app("example." + x, x.upper()) for x in "abc")
    session, source = observed_session(tmp_path, [[a, b], [a, c]])
    session.edit(lambda lay: lay.move(lay.pages[1][0].id, "library"))
    before = reviewed(session)
    if substitute:
        change_read(source, 3, lambda raw: raw["ignored"].__setitem__(0, "example.b"))
    assert apply(session)["status"] == ("unknown" if substitute else "verified")
    assert source.payloads[0]["ignored"] == ["example.a"]
    assert session._pending_path.exists() == substitute
    if substitute:
        assert reviewed(session) == before


@pytest.mark.parametrize("change", ["page", "grid"])
def test_token_tracks_approved_coordinates(tmp_path, change):
    session, source = duplicate_session(tmp_path, [{"example.a": 23}, {}])
    if change == "grid":
        source.raw = to_root_archive([[], [app("example.a", "A")]], [])
        run(session.read())
        session.edit(lambda lay: lay.pin("example.a", 1, 9))
    token = session.require().diff()["apply_token"]
    if change == "page":
        session.edit(lambda lay: lay.move_page(1, 2))
    else:
        source.metrics = {"homeScreenIconColumns": 5, "homeScreenIconRows": 7}
        run(session.read())
        session.edit(lambda lay: lay.pin("example.a", 1, 9))
    with pytest.raises(LayoutError, match="token"):
        run(session.apply(token))
    assert source.payloads == [] and session.backups() == []
    assert apply(session)["status"] == "verified"


@pytest.mark.parametrize("reported_first", [False, True])
@pytest.mark.parametrize("at", [1, 2])
@pytest.mark.parametrize("reload", [False, True])
def test_export_availability_is_source_evidence(tmp_path, reported_first, at, reload):
    session, source = observed_session(tmp_path, [[app("example.a", "A")]])
    if reported_first:
        source.raw["listMetadata"] = {}
        run(session.read())
    session.edit(lambda lay: lay.pin("example.a", 1, 23))
    if reload:
        session = Session(source, session.dir)
    change_read(source, at, lambda raw: raw.pop("listMetadata") if reported_first else
                raw.update(listMetadata={}))
    with pytest.raises(LayoutError, match="exported gaps changed"):
        apply(session)
    assert source.payloads == [] and session.backups() == []
    assert not session._pending_path.exists()


@pytest.mark.parametrize("ids", [None, ["same", "same"], [1, 2], ["short"]])
@pytest.mark.parametrize("reported", [False, True])
def test_dictionary_write_checks_existing_page_keys(tmp_path, ids, reported):
    session, source = observed_session(tmp_path, [[app("example.a", "A")], [app("example.b", "B")]])
    if ids is not None:
        source.raw["listUniqueIdentifiers"] = ids
    if reported:
        source.raw["listMetadata"] = {}
    run(session.read())
    session.edit(lambda lay: lay.pin("example.a", 1, 0))
    session.edit(lambda lay: lay.pin("example.b", 2, 0))
    if ids is None:
        assert apply(session)["status"] == "verified"
        assert len(source.raw["listMetadata"]) == 2
    else:
        with pytest.raises(LayoutError, match="page identifiers"):
            apply(session)
        assert source.payloads == [] and session.backups() == []
        assert not session._pending_path.exists()


@pytest.mark.parametrize("kind", ["phone", "file"])
@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_list_only_writer_rejects_fixed_positions_before_backup(tmp_path, kind, operation):
    state = [[], [app("example.a", "A")]]
    if kind == "phone":
        source = FrameSource()
        source.raw, source.write_mode = state, "list"
    else:
        path = tmp_path / "list.plist"
        original = plistlib.dumps(state)
        path.write_bytes(original)
        source = FileSource(path)
    session = Session(source, tmp_path / "data")
    run(session.read())
    cell = session.grid.slots - 1
    session.edit(lambda lay: lay.pin("example.a", 1, cell))
    backup = session._backup(state, None, "synthetic", [{"example.a": cell}])
    before, backups = reviewed(session), session.backups()
    with pytest.raises(LayoutError, match="list.*fixed positions"):
        apply(session) if operation == "apply" else run(session.restore(backup.name))
    assert reviewed(session) == before and session.backups() == backups
    assert not session._pending_path.exists()
    assert source.payloads == [] if kind == "phone" else path.read_bytes() == original
    session.edit(lambda lay: lay.unpin("example.a"))
    assert apply(session)["status"] == "verified"


def test_legacy_scalar_map_accepts_widget_named_version(tmp_path):
    item = widget("Weather", "small")
    item["uniqueIdentifier"] = "version"
    session, source = observed_session(tmp_path, [[item, app("example.a", "A")]])
    session._gaps_path.write_text(json.dumps({"version": 0, "example.a": 23}))
    run(session.read())
    assert session.require().pins == {"widget:version": 0, "example.a": 23}
    assert apply(session)["status"] == "verified"


@pytest.mark.parametrize("same_config", [False, True])
def test_widget_recovery_checks_instance_evidence_across_pages(tmp_path, same_config):
    first = {"displayIdentifier": "weather-widget", "gridSize": "small", "configuration": b"one",
             "userSelectedElementIdentifier": "alpha"}
    second = {**first, "configuration": b"one" if same_config else b"two",
              "userSelectedElementIdentifier": "beta"}
    session, source = observed_session(tmp_path, [[first], [second]])
    for page in source.raw["iconLists"]:
        del page[0]["uniqueIdentifier"], page[0]["userSelectedElementIdentifier"]
    source.raw["iconLists"].reverse()
    run(session.read())
    if same_config:
        with pytest.raises(LayoutError, match="wire identifier"):
            session.edit(lambda lay: lay.pin(lay.pages[0][0].id, 1, 0))
    else:
        session.edit(lambda lay: lay.pin(lay.pages[0][0].id, 1, 0))
    assert apply(session)["status"] == "verified"
    found = [(page[0]["configuration"], page[0].get("userSelectedElementIdentifier"))
             for page in source.raw["iconLists"]]
    assert found == ([(b"one", None), (b"one", None)] if same_config else [(b"two", "beta"), (b"one", "alpha")])


def test_display_only_legacy_cache_cannot_identify_a_new_configuration(tmp_path):
    item = {"displayIdentifier": "weather-widget", "gridSize": "small", "configuration": b"new"}
    session, source = observed_session(tmp_path, [[item]])
    session._widget_ids_path.write_text(json.dumps({"weather-widget": "old-element"}))
    run(session.read())
    with pytest.raises(LayoutError, match="wire identifier"):
        session.edit(lambda lay: lay.pin(lay.pages[0][0].id, 1, 0))
    assert apply(session)["status"] == "verified"
    assert source.raw["iconLists"] == [[item]]


def test_changed_opaque_item_in_place_rejects_apply(tmp_path):
    session, source = observed_session(tmp_path, [[]])
    source.raw["iconLists"][0] = [{"extensionData": b"first"}]
    run(session.read())
    source.raw["iconLists"][0] = [{"extensionData": b"second"}]
    with pytest.raises(LayoutError, match="layout changed"):
        apply(session)
    assert not source.payloads


def test_flat_reported_empty_map_cannot_become_unreported(tmp_path):
    session, source = observed_session(tmp_path, [[app("example.a", "A")]])
    original = source.read

    async def read():
        result = await original()
        source.pins = {}
        return result

    source.read = read
    run(session.read())
    session = Session(source, session.dir)
    source.read = original
    with pytest.raises(LayoutError, match="exported gaps changed"):
        apply(session)
    assert source.payloads == [] and not session.backups()


def test_widget_recovery_keeps_a_distinct_known_wire_identity(tmp_path):
    first = {"displayIdentifier": "weather", "gridSize": "small", "configuration": b"one",
             "uniqueIdentifier": "instance", "userSelectedElementIdentifier": "chosen"}
    session, source = observed_session(tmp_path, [[first]])
    del source.raw["iconLists"][0][0]["userSelectedElementIdentifier"]
    run(session.read())
    assert apply(session)["status"] == "verified"
    assert source.raw["iconLists"] == [[first]]


def test_widget_recovery_cannot_claim_another_widgets_observed_element(tmp_path):
    first = {"displayIdentifier": "weather", "gridSize": "small", "configuration": b"one",
             "userSelectedElementIdentifier": "alpha"}
    session, source = observed_session(tmp_path, [[first]])
    del source.raw["iconLists"][0][0]["userSelectedElementIdentifier"]
    source.raw["iconLists"].append([{**first, "configuration": b"two", "uniqueIdentifier": "other"}])
    run(session.read())
    assert apply(session)["status"] == "verified"
    assert "userSelectedElementIdentifier" not in source.raw["iconLists"][0][0]


@pytest.mark.parametrize("damage", ["missing-dimensions", "fraction-version"])
def test_invalid_gap_envelope_cannot_supply_default_coordinates(tmp_path, damage):
    session, source = observed_session(tmp_path, [[app("example.a", "A")]])
    memory = {"version": 2, "grid": session.grid.to_dict(), "pages": [{"example.a": 23}]}
    if damage == "missing-dimensions":
        memory["grid"] = {}
    else:
        memory["version"] = 2.0
    session._gaps_path.write_text(json.dumps(memory))
    run(session.read())
    assert session.require().pins == {}


@pytest.mark.parametrize("change", [False, True])
def test_legacy_missing_observation_compares_available_source_intent(tmp_path, change):
    session, source = observed_session(tmp_path, [[app("example.a", "A")]])
    blob = plistlib.loads(session.draft_path.read_bytes())
    blob.pop("source_pins_reported", None)
    session.draft_path.write_bytes(plistlib.dumps(blob))
    session = Session(source, session.dir)
    source.raw = to_root_archive([[], [app("example.a", "A")]], [],
                                 [{"example.a": 23}] if change else [], session.grid)
    if change:
        with pytest.raises(LayoutError, match="exported gaps changed"):
            apply(session)
        assert source.payloads == []
    else:
        source.raw["listMetadata"] = {}
        assert apply(session)["status"] == "verified"


def paired_folders(tmp_path, same_content=True):
    from fake_phone import folder

    a = app("example.a", "A")
    b = a if same_content else app("example.b", "B")
    return observed_session(tmp_path, [[{**folder("Tools", [a]), "uniqueIdentifier": "alpha"},
                                        {**folder("Tools", [b]), "uniqueIdentifier": "beta"}]])


def test_approval_tracks_which_identical_folder_owns_the_pin(tmp_path):
    session, source = paired_folders(tmp_path)
    session.edit(lambda lay: lay.pin("folder:alpha", 1, 0))
    token = session.require().token()
    session.edit(lambda lay: (lay.unpin("folder:alpha"), lay.pin("folder:beta", 1, 0)))
    with pytest.raises(LayoutError, match="token"):
        run(session.apply(token))
    assert not source.payloads and not session.backups()
    assert apply(session)["status"] == "verified"


@pytest.mark.parametrize("returned,distinct", [("alpha", False), ("beta", False), ("regenerated", False),
                                               ("regenerated", True)])
def test_reported_pin_verifies_the_folder_owner(tmp_path, returned, distinct):
    session, source = paired_folders(tmp_path, same_content=not distinct)
    session.edit(lambda lay: lay.pin("folder:alpha", 1, 0))
    before = reviewed(session)

    def report(raw):
        if returned == "regenerated":
            for item in raw["iconLists"][0]:
                item["uniqueIdentifier"] += "-new"
        page_id = raw["listUniqueIdentifiers"][0]
        raw["listMetadata"][page_id]["fixedLocations"] = {
            "alpha-new" if returned == "regenerated" else returned: 0}

    change_read(source, 3, report)
    matched = returned == "alpha" or distinct
    assert apply(session)["status"] == ("verified" if matched else "unknown")
    assert session._pending_path.exists() != matched
    if not matched:
        assert reviewed(session) == before


@pytest.mark.parametrize("survivor,wire,pin", [
    ("b", False, False), ("b", False, True), ("b", True, False), ("a", True, False), ("a", True, True)])
def test_widget_history_remembers_siblings_without_selected_elements(tmp_path, survivor, wire, pin):
    first = {"displayIdentifier": "weather", "gridSize": "small", "configuration": b"same",
             "uniqueIdentifier": "wire-a", "userSelectedElementIdentifier": "chosen-a"}
    second = {**first, "uniqueIdentifier": "wire-b"}
    del second["userSelectedElementIdentifier"]
    session, source = observed_session(tmp_path, [[first], [second]])
    current = source.raw["iconLists"][0 if survivor == "a" else 1][0]
    current.pop("userSelectedElementIdentifier", None)
    if not wire:
        del current["uniqueIdentifier"]
    source.raw["iconLists"] = [[current]]
    run(session.read())
    session = Session(source, session.dir)
    if pin:
        action = lambda: session.edit(lambda lay: lay.pin(lay.pages[0][0].id, 1, 0))
        if not wire:
            with pytest.raises(LayoutError, match="wire identifier"):
                action()
        else:
            action()
    assert apply(session)["status"] == "verified"
    written = source.payloads[0]["iconLists"][0][0]
    assert written.get("userSelectedElementIdentifier") == ("chosen-a" if survivor == "a" else None)


@pytest.mark.parametrize("normalized", [False, True])
def test_equal_folder_owners_in_unreported_memory(tmp_path, normalized):
    session, source = paired_folders(tmp_path)
    source.reported = False
    run(session.read())
    session.edit(lambda lay: lay.pin("folder:alpha", 1, 5))
    session.edit(lambda lay: lay.pin("folder:beta", 1, 10))
    if normalized:
        def regenerate(raw):
            for node in raw["iconLists"][0]:
                node["uniqueIdentifier"] += "-new"
        change_read(source, 3, regenerate)
    result = apply(session)
    if normalized:
        assert result["status"] == "unknown" and session._pending_path.exists()
    else:
        assert result["status"] == "verified"
        session = Session(source, session.dir)
        source.raw["iconLists"][0].reverse()
        run(session.read())
        assert session.require().pins == {"folder:alpha": 5, "folder:beta": 10}
        assert apply(session)["status"] == "verified"


def test_widget_history_remembers_separate_wire_owners_across_reads(tmp_path):
    first = {"displayIdentifier": "weather", "gridSize": "small", "uniqueIdentifier": "wire-a",
             "userSelectedElementIdentifier": "chosen-a"}
    session, source = observed_session(tmp_path, [[first]])
    current = source.raw["iconLists"][0][0]
    del current["userSelectedElementIdentifier"]
    current["uniqueIdentifier"] = "wire-b"
    run(session.read())
    del current["uniqueIdentifier"]
    run(session.read())
    assert apply(session)["status"] == "verified"
    assert "userSelectedElementIdentifier" not in source.payloads[-1]["iconLists"][0][0]


def test_folder_memory_keeps_known_owner_after_sibling_disappears(tmp_path):
    session, source = paired_folders(tmp_path)
    source.reported = False
    run(session.read())
    session.edit(lambda lay: lay.pin("folder:alpha", 1, 5))
    assert apply(session)["status"] == "verified"
    source.raw["iconLists"][0].pop()
    run(session.read())
    assert session.require().pins == {"folder:alpha": 5}


@pytest.mark.parametrize("wire", [False, True])
def test_older_widget_cache_only_recovers_confirmed_wire(tmp_path, wire):
    item = {"displayIdentifier": "weather", "gridSize": "small", "uniqueIdentifier": "wire-a"}
    session, source = observed_session(tmp_path, [[item]])
    key = Session._widget_key(item)
    wire_key = Session._widget_key(item, "wire-a")
    session._widget_ids_path.write_text(json.dumps({"version": 2, "instances": {key: "chosen", wire_key: "chosen"}}))
    if not wire:
        del source.raw["iconLists"][0][0]["uniqueIdentifier"]
    run(session.read())
    assert apply(session)["status"] == "verified"
    assert source.payloads[0]["iconLists"][0][0].get("userSelectedElementIdentifier") == ("chosen" if wire else None)


def test_legacy_wire_observation_does_not_prove_a_config_only_element(tmp_path):
    item = {"displayIdentifier": "weather", "gridSize": "small", "uniqueIdentifier": "wire-b"}
    session, source = observed_session(tmp_path, [[item]])
    session._widget_ids_path.write_text(json.dumps({"version": 2, "instances": {
        Session._widget_key(item): "chosen-a", Session._widget_key(item, "wire-a"): "chosen-a"}}))
    run(session.read())
    del source.raw["iconLists"][0][0]["uniqueIdentifier"]
    run(session.read())
    assert apply(session)["status"] == "verified"
    assert "userSelectedElementIdentifier" not in source.payloads[0]["iconLists"][0][0]


@pytest.mark.parametrize("original,added", [("alpha", None), (None, "beta"), ("alpha", "beta"),
                                            ("alpha-new", None), (None, None)])
def test_ambiguous_content_memory_never_transfers_to_a_new_folder(tmp_path, original, added):
    from fake_phone import folder

    item = {**folder("Tools", [app("example.a", "A")]), "uniqueIdentifier": "alpha"}
    session, source = observed_session(tmp_path, [[item]])
    source.reported = False
    run(session.read())
    session.edit(lambda lay: lay.pin("folder:alpha", 1, 5))
    assert apply(session)["status"] == "verified"
    exported = source.raw["iconLists"][0][0]
    del exported["uniqueIdentifier"]
    sibling = dict(exported)
    if original:
        exported["uniqueIdentifier"] = original
    if added:
        sibling["uniqueIdentifier"] = added
    source.raw["iconLists"][0].append(sibling)
    session = Session(source, session.dir)
    run(session.read())
    assert session.require().pins == {}
    assert apply(session)["status"] == "verified"
    assert not source.payloads[-1].get("listMetadata")


@pytest.mark.parametrize("intermediate", ["element", "skipped", "other-element", "other-wire", "other-config"])
@pytest.mark.parametrize("pin", [False, True])
def test_partial_widget_exports_resolve_only_the_observed_pair(tmp_path, intermediate, pin):
    first = {"displayIdentifier": "weather", "gridSize": "small", "configuration": b"one",
             "uniqueIdentifier": "wire-a", "userSelectedElementIdentifier": "chosen-a"}
    session, source = observed_session(tmp_path, [[first]])
    current = source.raw["iconLists"][0][0]
    if intermediate != "skipped":
        del current["uniqueIdentifier"]
        if intermediate == "other-element":
            current["userSelectedElementIdentifier"] = "other-choice"
        if intermediate == "other-wire":
            current["uniqueIdentifier"] = "wire-b"
        if intermediate == "other-config":
            current["configuration"] = b"two"
            del current["userSelectedElementIdentifier"]
        run(session.read())
    current.pop("uniqueIdentifier", None)
    current.pop("userSelectedElementIdentifier", None)
    run(session.read())
    session = Session(source, session.dir)
    recover = intermediate in ("element", "skipped")
    if pin:
        def place():
            session.edit(lambda lay: lay.pin(lay.pages[0][0].id, 1, 0))
        if recover:
            place()
        else:
            with pytest.raises(LayoutError, match="wire identifier"):
                place()
    assert apply(session)["status"] == "verified"
    written = source.payloads[-1]["iconLists"][0][0]
    assert written.get("userSelectedElementIdentifier") == ("chosen-a" if recover else None)
