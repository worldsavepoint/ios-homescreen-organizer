"""Shared drafts, source observations and recovery through synthetic transports."""

import json
import os
import plistlib
import socket

import pytest

import homescreen.session as persistence
from fake_phone import app, folder, widget
from homescreen.device import FileSource, PhoneSource, SimSource, to_root_archive
from homescreen.model import Grid, LayoutError
from homescreen.session import Session
from test_grid_serialization import metadata
from test_integrity import MemorySource
from test_live_frame import FrameSource, external_traps, reviewed
from test_pin_integrity import apply, run
from test_transaction_identity import change_read, paired_folders


@pytest.fixture(autouse=True)
def source_traps(monkeypatch, external_traps):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected external source operation")
    for owner, names in [(socket, ["getaddrinfo"]), (os, ["posix_spawn", "posix_spawnp"]),
                         (PhoneSource, ["_ld", "_call", "read_raw", "installed_apps", "memory_reports", "icon_png"]),
                         (SimSource, ["apps", "_springboard_pid", "_screen_lit", "icon_png"])]:
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)


def loaded(path, source=None):
    source = source or FrameSource()
    session = Session(source, path)
    run(session.read())
    return session, source


@pytest.mark.parametrize("action", ["edit", "edited", "undo", "redo", "apply", "restore", "reset"])
def test_stale_client_cannot_replace_a_committed_draft(tmp_path, action):
    first, source = loaded(tmp_path)
    pin = lambda lay: lay.pin("example.a", 1, 5)
    first.edit(pin)
    first.edit(lambda lay: lay.pin("example.a", 1, 7))
    first.undo()
    other = Session(source, first.dir)
    other.edit(lambda lay: lay.pin("example.a", 1, 12))
    before = reviewed(first)
    backup = first._backup(first.require().to_state(), [], "previous")
    actions = {"edit": lambda: first.edit(pin), "apply": lambda: apply(first),
               "restore": lambda: run(first.restore(backup.name))}
    with pytest.raises(LayoutError, match="draft.*changed"):
        (actions[action] if action in actions else getattr(first, action))()
    assert reviewed(first) == before
    assert Session(source, first.dir).require().pins == {"example.a": 12}
    assert source.payloads == [] and first.backups() == [backup.name]
    run(first.read())
    first.edit(pin)
    assert Session(source, first.dir).require().pins == {"example.a": 5}


def test_token_from_another_selected_device_cannot_apply(tmp_path):
    a, _ = loaded(tmp_path / "a", MemorySource())
    source = MemorySource()
    source.udid += "-second"
    b, _ = loaded(tmp_path / "b", source)
    with pytest.raises(LayoutError, match="token"):
        run(b.apply(a.require().diff()["apply_token"]))
    assert not source.writes
    assert run(b.apply(b.require().diff()["apply_token"]))["ok"]


@pytest.mark.parametrize("failure", ["draft", "clear"])
def test_adoption_rolls_back_both_local_files_and_history(tmp_path, monkeypatch, failure):
    s, src = loaded(tmp_path, FrameSource(False))
    s.edit(lambda l: l.pin("example.a", 1, 2))
    assert apply(s)["ok"]
    s.edit(lambda l: l.pin("example.a", 1, 9))
    before = reviewed(s)
    original = s.require()

    def fail():
        raise OSError("storage unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(s, "_save_draft" if failure == "draft" else "_clear_pending", fail)
        result = apply(s)
        assert not result["ok"] and result["readback"] == "matched"
        assert result["local_adoption"] == "failed" and result["recovery"] == "read"
        assert result["backup"] in s.backups()
    assert s.require() is original and reviewed(s) == before
    assert Session(src, s.dir)._snap() == before[0]
    assert s._pending_path.exists()
    run(s.read())
    assert not s._pending_path.exists()


def test_partial_marker_data_never_occupies_final_name(tmp_path, monkeypatch):
    s, src = loaded(tmp_path)
    fdopen = os.fdopen

    class Short:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, data):
            if b'"status": "pending"' in data:
                self.stream.write(data[:1])
                raise OSError("storage unavailable")
            return self.stream.write(data)

        def __getattr__(self, name):
            return getattr(self.stream, name)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fdopen", lambda *a, **kw: Short(fdopen(*a, **kw)))
        with pytest.raises(OSError):
            apply(s)
    assert not s._pending_path.exists() and not src.payloads
    run(Session(src, s.dir).read())


@pytest.mark.parametrize("failed", [True, False])
@pytest.mark.parametrize("change", ["pin", "move-pin", "unpin"])
def test_changed_unreported_positions_need_a_successful_write(tmp_path, failed, change):
    s, src = loaded(tmp_path, FrameSource(False))
    if change != "pin":
        s.edit(lambda l: l.pin("example.a", 1, 3))
        assert apply(s)["ok"]
    s.edit(lambda l: l.unpin("example.a") if change == "unpin" else l.pin("example.a", 1, 21))
    before = reviewed(s)
    if failed:
        async def reject(payload):
            raise ConnectionError("send failed before delivery")
        src.set_icon_state = reject
    assert apply(s)["ok"] is (not failed)
    assert s._pending_path.exists() is failed
    if failed:
        assert reviewed(s) == before


def odd_app():
    return {**app("example.ride", "Ride"), "displayIdentifier": "wire-ride", "iconType": "app"}


@pytest.mark.parametrize("at", [1, 2, None])
def test_archive_pins_and_prewrite_guards_use_serialized_app_id(tmp_path, at):
    src = FrameSource()
    src.raw = [[], [odd_app()]]
    s, _ = loaded(tmp_path, src)
    s.edit(lambda l: l.pin("example.ride", 1, 7))
    change_read(src, at, lambda raw: raw[1][0].update(displayIdentifier="replacement"))
    if at:
        with pytest.raises(LayoutError, match="changed"):
            apply(s)
        assert not src.payloads and not s.backups()
    else:
        assert apply(s)["ok"]
        assert src.raw["iconLists"] == [["wire-ride"]]
        assert metadata(src.raw)["fixedLocations"] == {"wire-ride": 7}
        assert s.require().pins == {"example.ride": 7}
        s = Session(src, s.dir)
        run(s.read())
        assert s.require().pins == {"example.ride": 7}
        assert apply(s)["ok"]


@pytest.mark.parametrize("value", [True, 1.0])
@pytest.mark.parametrize("flat", [True, False])
def test_corrected_cell_type_invalidates_saved_observation(tmp_path, value, flat):
    src = FrameSource()
    src.raw = to_root_archive([[], [app("a", "A")]], [], [{"a": 1}], Grid(5, 7))
    metadata(src.raw)["fixedLocations"]["a"] = value
    if flat:
        read = src.read

        async def read_flat():
            result = await read()
            src.pins = dict(src.pins)
            return result
        src.read = read_flat
    s, _ = loaded(tmp_path, src)
    assert s.require().pins == {}
    s = Session(src, s.dir)
    metadata(src.raw)["fixedLocations"]["a"] = 1
    with pytest.raises(LayoutError, match="changed|pin"):
        apply(s)
    assert not src.payloads and not s.backups()


@pytest.mark.parametrize("at", [1, 2, None])
@pytest.mark.parametrize("change", ["page-ids", "owner"])
def test_prewrite_checks_page_and_resolved_pin_owner(tmp_path, change, at):
    src = FrameSource()
    if change == "page-ids":
        src.raw = to_root_archive([[], [app("a", "A")], [app("a", "A")]], [])
        src.raw["listUniqueIdentifiers"] = ["first", "second"]
        modify = lambda raw: raw["listUniqueIdentifiers"].reverse()
    else:
        pages = [[{**folder(name, [app(key, key)]), "uniqueIdentifier": key}
                  for name, key in [("Work", "alpha"), ("Other", "beta")]]]
        src.raw = to_root_archive([[], *pages], [], [{"alpha": 9}], Grid(5, 7))

        def modify(raw):
            a, b = raw["iconLists"][0]
            a["uniqueIdentifier"], b["uniqueIdentifier"] = b["uniqueIdentifier"], a["uniqueIdentifier"]
    s, _ = loaded(tmp_path, src)
    if change == "page-ids":
        assert src.pins is None
        s.edit(lambda l: l.pin("a", 1, 5))
    s = Session(src, s.dir)
    change_read(src, at, modify)
    if at:
        with pytest.raises(LayoutError, match="changed"):
            apply(s)
        assert not src.payloads and not s.backups()
    else:
        assert apply(s)["ok"]


@pytest.mark.parametrize("value", [4.9, True, 0, -2, "four"])
def test_malformed_present_metrics_cannot_approve_a_write(tmp_path, value):
    src = FrameSource()
    src.metrics = {"homeScreenIconColumns": 4, "homeScreenIconRows": 6}
    s, _ = loaded(tmp_path, src)
    src.metrics["homeScreenIconColumns"] = value
    with pytest.raises(LayoutError, match="grid"):
        apply(s)
    assert not src.payloads and not s.backups()


def test_opaque_widget_icon_lists_survive_root_serialization(tmp_path):
    src = FrameSource()
    contents = [[{"opaque": b"preserve"}]]
    src.raw = [[], [{**widget("Sample", "small"), "iconLists": contents}]]
    s, _ = loaded(tmp_path, src)
    assert apply(s)["ok"]
    assert src.raw["iconLists"][0][0]["iconLists"] == contents


@pytest.mark.parametrize("replacement", ["during-write", "later"])
def test_remembered_instance_survives_intervening_write(tmp_path, replacement):
    s, src = paired_folders(tmp_path)
    src.reported = False
    run(s.read())
    s.edit(lambda l: l.pin("folder:alpha", 1, 17))
    assert apply(s)["ok"]
    src.raw["iconLists"][0].pop()
    run(s.read())
    assert s.require().pins == {"folder:alpha": 17}
    s = Session(src, s.dir)
    before = reviewed(s)

    def replace(raw):
        raw["iconLists"][0][0]["uniqueIdentifier"] = "beta"
    if replacement == "during-write":
        change_read(src, 3, replace)
        assert not apply(s)["ok"] and reviewed(s) == before
    else:
        assert apply(s)["ok"]
        replace(src.raw)
        run(s.read())
        assert s.require().pins == {}


@pytest.mark.parametrize("source_kind", ["phone", "file"])
@pytest.mark.parametrize("has_library", [False, True])
def test_list_restore_checks_target_library_before_backup(tmp_path, source_kind, has_library):
    state = [[], [app("a", "A")]]
    if source_kind == "file":
        path = tmp_path / "icons.plist"
        path.write_bytes(plistlib.dumps(state))
        src = FileSource(path)
    else:
        src = FrameSource()
        src.raw, src.write_mode = state, "list"
    s, _ = loaded(tmp_path / "data", src)
    backup = s._backup(state, [app("b", "B")] if has_library else [], "previous")
    if has_library:
        with pytest.raises(LayoutError, match="App Library"):
            run(s.restore(backup.name))
        assert s.backups() == [backup.name] and not s._pending_path.exists()
    else:
        assert run(s.restore(backup.name))["ok"]


def test_legacy_unpinned_restore_removes_live_metadata(tmp_path):
    s, src = loaded(tmp_path)
    backup = s._backup(s.require().to_state(), [], "legacy")
    s.edit(lambda l: l.pin("example.a", 1, 15))
    assert apply(s)["ok"]
    assert run(s.restore(backup.name))["ok"]
    assert not src.raw.get("listMetadata") and not s.require().pins


@pytest.mark.parametrize("unsupported", ["pin", "leading-widget", None])
def test_restore_uses_ios16_live_placement_capability(tmp_path, unsupported):
    src = FrameSource(False)
    async def info():
        return {"UniqueDeviceID": "synthetic", "ProductVersion": "16.0"}
    src.info = info
    items = [widget("Weather", "small"), {**folder("Work", [app("a", "A")]), "uniqueIdentifier": "work"}]
    src.raw = to_root_archive([[], items], [])
    s, _ = loaded(tmp_path, src)
    state = s.require().to_state()
    if unsupported == "leading-widget":
        state[1].reverse()
    backup = s._backup(state, [], "previous", [{"work": 23}] if unsupported == "pin" else None)
    if unsupported:
        with pytest.raises(LayoutError, match="iOS 16"):
            run(s.restore(backup.name))
        assert not src.payloads and s.backups() == [backup.name] and not s._pending_path.exists()
    else:
        assert run(s.restore(backup.name))["ok"]


@pytest.mark.parametrize("conflicting", [False, True])
def test_archive_frame_supplies_fixture_metrics(tmp_path, conflicting):
    path = tmp_path / "icons.plist"
    archive = to_root_archive([[], [app("a", "A")], [app("b", "B")]], [], [{"a": 34}, {"b": 33}], Grid(5, 7))
    if conflicting:
        metadata(archive)["fixedLocationsGridColumns"] = 4
    path.write_bytes(plistlib.dumps(archive))
    s = Session(FileSource(path), tmp_path / "session")
    if conflicting:
        with pytest.raises(LayoutError, match="grid|dimensions"):
            run(s.read())
    else:
        run(s.read())
        assert s.grid == Grid(5, 7) and s.require().pins == {"a": 34, "b": 33}
        assert apply(s)["ok"]


@pytest.mark.parametrize("changed", [None, "body", "frame", "arbitrary"])
def test_only_observed_widget_backup_shape_can_restore_through_overflow(tmp_path, changed):
    src = FrameSource()
    src.raw = to_root_archive([[], [widget("weather", "large"), widget("calendar", "large")]], [])
    s, _ = loaded(tmp_path, src)
    backup = s._dev_dir("backups") / apply(s)["backup"]
    if changed == "body":
        blob = plistlib.loads(backup.read_bytes())
        blob["state"][1][0]["displayName"] = "Other"
        backup.write_bytes(plistlib.dumps(blob))
    if changed == "frame":
        src.metrics["homeScreenIconRows"] = 6
        run(s.read())
    if changed == "arbitrary":
        backup = s._backup(s.require().to_state(), [], "arbitrary")
    if changed:
        with pytest.raises(LayoutError, match="grid|room|footprint"):
            run(s.restore(backup.name))
        assert len(src.payloads) == 1
    else:
        assert run(Session(src, s.dir).restore(backup.name))["ok"]


def test_read_replaces_malformed_saved_gap_pages(tmp_path):
    s, _ = loaded(tmp_path, FrameSource(False))
    s._gaps_path.write_text(json.dumps({"version": 2, "grid": s.grid.to_dict(), "pages": [[]]}))
    run(s.read())
    assert s.require().pins == {} and json.loads(s._gaps_path.read_text())["pages"] == [{}]
    assert apply(s)["ok"]


def test_history_keeps_one_isolated_source_baseline(tmp_path):
    s, _ = loaded(tmp_path)
    for cell in [1, 3, 5]:
        s.edit(lambda l: l.pin("example.a", 1, cell))
    baselines = {id(step["source"]) for step in s._undo if "source" in step}
    assert len(baselines) <= 1 and id(s.require().source) not in baselines
    assert s.undo() and s.require().pins == {"example.a": 3}
    assert s.undo() and s.require().pins == {"example.a": 1}


def test_legacy_draft_and_flat_memory_count_owners_once(tmp_path, monkeypatch):
    src = FrameSource(False)
    src.raw = to_root_archive([[], *[[app(f"a{p}-{i}", "A") for i in range(12)] for p in range(8)]], [])
    s, _ = loaded(tmp_path, src)
    def pin_all(lay):
        for p, page in enumerate(lay.pages, 1):
            for i, node in enumerate(page):
                lay.pin(node.id, p, i)
    s.edit(pin_all)
    blob = plistlib.loads(s.draft_path.read_bytes())
    del blob["identity"], blob["source_identity"]
    s.draft_path.write_bytes(plistlib.dumps(blob))
    calls = []
    original = persistence.pin_key
    def count(node):
        calls.append(node.id)
        return original(node)
    monkeypatch.setattr(persistence, "pin_key", count)
    assert len(Session(src, s.dir).require().pins) == 96
    assert len(calls) <= 4 * 96
    calls.clear()
    s._gaps_path.write_text('{"a0-1": 3}')
    run(s.read())
    assert s.require().pins == {"a0-1": 3} and len(calls) <= 2 * 96


def test_pending_publication_refuses_an_existing_complete_record(tmp_path, monkeypatch):
    s, src = loaded(tmp_path)
    marker = s._pending_path
    original_backup = s._backup
    existing = b'{"device_id":"synthetic","status":"pending","backup":"other"}'

    def publish_competing_marker(*args, **kwargs):
        backup = original_backup(*args, **kwargs)
        marker.write_bytes(existing)
        return backup

    monkeypatch.setattr(s, "_backup", publish_competing_marker)
    with pytest.raises(FileExistsError):
        apply(s)
    assert marker.read_bytes() == existing and not src.payloads


def test_byte_count_short_write_cannot_publish_a_marker(tmp_path, monkeypatch):
    s, src = loaded(tmp_path)
    fdopen = os.fdopen
    def opened(*args, **kwargs):
        stream = fdopen(*args, **kwargs)
        write = stream.write
        def short(data):
            if b'"status": "pending"' in data:
                return write(data[:1])
            return write(data)
        stream.write = short
        return stream

    with monkeypatch.context() as patch:
        patch.setattr(os, "fdopen", opened)
        with pytest.raises(OSError):
            apply(s)
    assert not s._pending_path.exists() and not src.payloads


def test_identical_folder_pin_stays_at_its_observed_index_before_write(tmp_path):
    s, src = paired_folders(tmp_path)
    src.raw.update(to_root_archive(s.require().to_state(), [], [{"alpha": 9}], s.grid))
    run(s.read())
    src.raw["iconLists"][0].reverse()
    with pytest.raises(LayoutError, match="changed"):
        apply(s)
    assert not src.payloads and not s.backups()


def test_disk_archive_keeps_nondefault_grid_after_last_pin_removed(tmp_path):
    path = tmp_path / "icons.plist"
    path.write_bytes(plistlib.dumps(to_root_archive([[], [app("a", "A")]], [], [{"a": 34}], Grid(5, 7))))
    s, src = loaded(tmp_path / "session", FileSource(path))
    s.edit(lambda lay: lay.unpin("a"))
    assert apply(s)["ok"]
    s = Session(src, s.dir)
    run(s.read())
    assert s.grid == Grid(5, 7) and not s.require().pins


@pytest.mark.parametrize("major", ["16", "27"])
def test_restore_uses_version_from_live_prewrite_info(tmp_path, major):
    src = FrameSource(False)
    s, _ = loaded(tmp_path, src)
    backup = s._backup(s.require().to_state(), [], "previous", [{"example.a": 23}])
    info = src.info
    async def version():
        return {**await info(), "ProductVersion": major}
    src.info = version
    if major == "16":
        with pytest.raises(LayoutError, match="iOS 16"):
            run(s.restore(backup.name))
        assert not src.payloads
    else:
        assert run(s.restore(backup.name))["ok"]


def test_saved_folder_instance_follows_an_explicit_move_to_another_page(tmp_path):
    s, src = paired_folders(tmp_path)
    src.reported = False
    src.raw["iconLists"].append(["other"])
    run(s.read())
    s.edit(lambda lay: lay.pin("folder:alpha", 1, 8))
    assert apply(s)["ok"]
    src.raw["iconLists"][0].pop()
    run(s.read())
    s.edit(lambda lay: lay.pin("folder:alpha", 2, 17))
    s.edit(lambda lay: lay.drop_empty())
    # Keep a different first page so the folder really has moved to a different index.
    s.edit(lambda lay: lay.insert_page(1))
    s.edit(lambda lay: lay.move("other", "page:1"))
    assert apply(s)["ok"]
    for page in src.raw["iconLists"]:
        for item in page:
            if isinstance(item, dict) and item.get("uniqueIdentifier") == "alpha":
                item["uniqueIdentifier"] = "beta"
    run(s.read())
    assert not s.require().pins


def test_arbitrary_overflow_backup_cannot_copy_and_recompute_its_own_receipt(tmp_path):
    import hashlib

    src = FrameSource()
    src.raw = to_root_archive([[], [widget("weather", "large"), widget("calendar", "large")]], [])
    s, _ = loaded(tmp_path, src)
    genuine = s._dev_dir("backups") / apply(s)["backup"]
    blob = plistlib.loads(genuine.read_bytes())
    blob["state"][1][0]["displayName"] = "Other"
    evidence = {"body": {k: v for k, v in blob.items() if k != "provenance"},
                "device": s.device_id, "grid": s.grid.to_dict(), "widget_pages": [0]}
    blob["provenance"]["source_shape"]["sha256"] = hashlib.sha256(
        plistlib.dumps(evidence, fmt=plistlib.FMT_BINARY)).hexdigest()
    forged = genuine.with_name("different-backup.plist")
    forged.write_bytes(plistlib.dumps(blob, fmt=plistlib.FMT_BINARY))
    with pytest.raises(LayoutError, match="grid|room|footprint"):
        run(s.restore(forged.name))
    assert len(src.payloads) == 1


def test_explicit_read_recovers_corrupt_memory_even_with_reported_pins(tmp_path):
    src = FrameSource()
    src.raw = to_root_archive([[], [app("a", "A")]], [], [{"a": 6}], Grid(5, 7))
    s, _ = loaded(tmp_path, src)
    s._gaps_path.write_bytes(b"{")
    run(s.read())
    assert s.require().pins == {"a": 6} and apply(s)["ok"]


@pytest.mark.parametrize("reported", [True, False])
@pytest.mark.parametrize("at", [1, 2, None])
def test_new_draft_pin_rechecks_a_known_folder_instance(tmp_path, reported, at):
    s, src = paired_folders(tmp_path, same_content=False)
    src.reported = reported
    if reported:
        src.raw["listMetadata"] = {}
    run(s.read())
    s.edit(lambda lay: lay.pin("folder:alpha", 1, 7))

    def swap_ids(raw):
        first, second = raw["iconLists"][0]
        first["uniqueIdentifier"], second["uniqueIdentifier"] = second["uniqueIdentifier"], first["uniqueIdentifier"]
    change_read(src, at, swap_ids)
    if at:
        with pytest.raises(LayoutError, match="changed"):
            apply(s)
        assert not src.payloads and not s.backups()
    else:
        assert apply(s)["ok"]


def test_reported_readback_cannot_substitute_a_previously_ambiguous_folder(tmp_path):
    s, src = paired_folders(tmp_path)
    src.reported = False
    run(s.read())
    s.edit(lambda lay: lay.pin("folder:alpha", 1, 7))
    assert apply(s)["ok"]
    src.raw["iconLists"][0].pop()
    run(s.read())
    before = reviewed(s)

    def replace(raw):
        src.reported = True
        raw["iconLists"][0][0]["uniqueIdentifier"] = "beta"
        metadata(raw)["fixedLocations"] = {"beta": 7}
    change_read(src, 3, replace)
    assert not apply(s)["ok"] and s._pending_path.exists()
    assert reviewed(s) == before


def test_separate_draft_paths_recheck_shared_device_pin_memory(tmp_path):
    src = FrameSource(False)
    raw_state = [[], [app("example.a", "A")]]
    src.raw = raw_state
    record = src.set_icon_state
    async def write_list_readback(payload):
        await record(payload)
        src.raw = raw_state
    src.set_icon_state = write_list_readback
    shared, _ = loaded(tmp_path, src)
    app_session = Session(src, shared.dir, device="synthetic")
    run(app_session.read())
    shared.edit(lambda lay: lay.pin("example.a", 1, 5))
    app_session.edit(lambda lay: lay.pin("example.a", 1, 9))
    assert apply(app_session)["ok"]
    reviewed_shared = reviewed(shared)
    with pytest.raises(LayoutError, match="changed"):
        apply(shared)
    assert len(src.payloads) == 1 and reviewed(shared) == reviewed_shared
    run(shared.read())
    assert shared.require().pins == {"example.a": 9}


@pytest.mark.parametrize("key_form", ["old-bundle", "wire"])
def test_odd_app_keeps_preupgrade_remembered_bundle_pin(tmp_path, key_form):
    src = FrameSource(False)
    src.raw = [[], [odd_app()]]
    s, _ = loaded(tmp_path, src)
    key = "example.ride" if key_form == "old-bundle" else "wire-ride"
    s._gaps_path.write_text(json.dumps({"version": 2, "grid": s.grid.to_dict(), "pages": [{key: 7}]}))
    run(s.read())
    assert s.require().pins == {"example.ride": 7}
    assert apply(s)["ok"]
    assert metadata(src.raw)["fixedLocations"] == {"wire-ride": 7}


def test_preupgrade_backup_bundle_pin_restores_to_odd_app_wire_identifier(tmp_path):
    src = FrameSource()
    src.raw = [[], [odd_app()]]
    s, _ = loaded(tmp_path, src)
    backup = s._backup(s.require().to_state(), [], "previous", [{"example.ride": 7}])
    assert run(s.restore(backup.name))["ok"]
    assert metadata(src.raw)["fixedLocations"] == {"wire-ride": 7}


@pytest.mark.parametrize("archive_only", [True, False])
def test_disk_reader_requires_trustworthy_geometry_for_missing_metrics(tmp_path, monkeypatch, archive_only):
    from homescreen import device

    path = tmp_path / "IconState.plist"
    raw = to_root_archive([[], [app("a", "A")]], [], [{"a": 34}], Grid(5, 7))
    if not archive_only:
        raw["listMetadata"]["orphan"] = raw["listMetadata"].pop(raw["listUniqueIdentifiers"][0])
    path.write_bytes(plistlib.dumps(raw))
    src = SimSource("synthetic")
    src.dir = tmp_path
    monkeypatch.setattr(src, "apps", lambda: {})
    monkeypatch.setattr(device, "_run", lambda *args: json.dumps({"devices": {
        "example.iOS-27-0": [{"udid": src.udid, "name": "Synthetic", "state": "Booted"}]}}))
    s = Session(src, tmp_path / "session")
    if archive_only:
        run(s.read())
        assert s.grid == Grid(5, 7) and s.require().pins == {"a": 34}
    else:
        with pytest.raises(LayoutError, match="grid|page"):
            run(s.read())


@pytest.mark.parametrize("field", ["version", "grid", "pins"])
def test_restore_refuses_malformed_fitting_backup_metadata(tmp_path, field):
    s, src = loaded(tmp_path)
    backup = s._backup(s.require().to_state(), [], "previous")
    blob = plistlib.loads(backup.read_bytes())
    if field == "version":
        blob["provenance"]["version"] = True
    elif field == "grid":
        blob["provenance"]["grid"]["cols"] = 5.0
    else:
        blob["pins"] = [[]]
    backup.write_bytes(plistlib.dumps(blob))
    with pytest.raises(LayoutError, match="identity|grid|pins"):
        run(s.restore(backup.name))
    assert not src.payloads and s.backups() == [backup.name] and not s._pending_path.exists()
