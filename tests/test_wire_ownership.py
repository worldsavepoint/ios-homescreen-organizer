"""Owner changes between draft, source reads, archive writes and remembered placement."""

import json
import plistlib

import pytest

from fake_phone import app, folder
from homescreen import device
from homescreen.device import FileSource, SimSource, to_root_archive
from homescreen.model import Grid, LayoutError
from homescreen.session import Session
from test_grid_serialization import ArchiveSource, metadata
from test_live_frame import FrameSource, external_traps, reviewed
from test_pin_integrity import apply, run
from test_reviewed_session import loaded, source_traps
from test_transaction_identity import change_read, paired_folders


def test_repeated_clip_owners_keep_the_unobserved_typed_occurrence(tmp_path):
    clip = {"iconType": "webClip", "displayIdentifier": "shared", "webClipURL": "/one"}
    first, second = {**clip, "setting": True}, {**clip, "setting": 1}
    path = tmp_path / "archive.plist"
    archive = {"iconLists": [[first, second, "keep"]], "ignored": []}
    path.write_bytes(plistlib.dumps(archive))
    session = Session(FileSource(path), tmp_path / "data")
    run(session.read())
    archive["iconLists"] = [[second, "keep"]]
    path.write_bytes(plistlib.dumps(archive))
    run(session.read())
    session.edit(lambda layout: layout.move("keep", "dock"))
    assert apply(session)["ok"]
    wire = plistlib.loads(path.read_bytes())
    assert type(wire["iconLists"][0][0]["setting"]) is int
    assert len(wire["ignored"]) == 1 and wire["ignored"][0]["setting"] is True


def test_legacy_json_clip_memory_excludes_explicit_apps_and_preserves_url_clips(tmp_path):
    path = tmp_path / "archive.plist"
    path.write_bytes(plistlib.dumps({"iconLists": [["keep"]], "ignored": []}))
    session = Session(FileSource(path), tmp_path / "data")
    run(session.read())
    clip = {"displayIdentifier": "clip", "webClipURL": "/example"}
    ordinary = {"iconType": "app", "displayIdentifier": "sample.webclipper", "webClipURL": "/help"}
    session._webclips_path.with_suffix(".json").write_text(json.dumps({"old-one": clip, "old-two": ordinary}))
    run(session.read())
    assert [node.raw for node in session.require().library] == [clip]


def test_explicit_webclip_with_exported_empty_icon_lists(tmp_path):
    clip = {"iconType": "webClip", "displayIdentifier": "clip", "iconLists": []}
    path = tmp_path / "archive.plist"
    path.write_bytes(plistlib.dumps({"iconLists": [[clip, "keep"]], "ignored": []}))
    session = Session(FileSource(path), tmp_path / "data")
    run(session.read())
    assert session.require().get("clip").kind == "webclip"
    path.write_bytes(plistlib.dumps({"iconLists": [["keep"]], "ignored": []}))
    run(session.read())
    assert [node.raw for node in session.require().library] == [clip]


@pytest.mark.parametrize("legacy", [False, True])
def test_remembered_clips_keep_display_owners_across_reads(tmp_path, legacy):
    clips = [{"iconType": "webClip", "bundleIdentifier": "shared", "displayIdentifier": owner,
              "webClipURL": "/" + owner, "opaque": owner.encode()} for owner in ("one", "two")]
    path = tmp_path / "fixture.plist"
    archive = {"buttonBar": [], "iconLists": [[*clips, "keep"]], "ignored": []}
    path.write_bytes(plistlib.dumps(archive))
    s = Session(FileSource(path), tmp_path / "data")
    run(s.read())
    if legacy:
        s._webclips_path.write_bytes(plistlib.dumps(dict(zip(("shared", "webclip:1"), clips))))
    archive["iconLists"] = [[clips[1], "keep"]]
    path.write_bytes(plistlib.dumps(archive))
    run(s.read())
    s.edit(lambda layout: layout.move("shared", "dock"))
    assert apply(s)["ok"]
    written = plistlib.loads(path.read_bytes())
    assert written["buttonBar"] == [clips[1]] and written["ignored"] == [clips[0]]


@pytest.mark.parametrize("empty", [False, True])
def test_unrelated_move_preserves_folder_pages_through_recovery(tmp_path, empty):
    pages = [[], ["inside"]] if empty else [["inside"]]
    owners = ["empty", "used"] if empty else ["used"]
    folder = {"listType": "folder", "defaultDisplayName": "Utilities", "iconLists": pages,
              "listUniqueIdentifiers": owners, "listMetadata": {owner: {"payload": owner} for owner in owners}}
    path = tmp_path / "device.plist"
    path.write_bytes(plistlib.dumps({"buttonBar": [], "iconLists": [[folder, "moving"]], "ignored": []}))
    s = Session(FileSource(path), tmp_path / "data")
    run(s.read())
    s.edit(lambda layout: layout.move("moving", "dock"))
    result = apply(s)
    assert result["ok"] and plistlib.loads(path.read_bytes())["iconLists"][0][0] == folder
    assert run(s.restore(result["backup"]))["ok"]
    assert plistlib.loads(path.read_bytes())["iconLists"][0][0] == folder


@pytest.mark.parametrize("reported", [False, True])
@pytest.mark.parametrize("timing", [1, 2, 3, None])
def test_folder_wire_exchanged_between_pages(tmp_path, timing, reported):
    src = FrameSource(reported)
    folders = [{**folder(key, [app(key, key)]), "uniqueIdentifier": key} for key in ("alpha", "beta")]
    src.raw = to_root_archive([[], [folders[0]], [folders[1]]], [])
    s, _ = loaded(tmp_path, src)
    s.edit(lambda l: l.pin("folder:alpha", 1, 7))
    before = reviewed(s)
    def exchange(raw):
        a, b = [p[0] for p in raw["iconLists"]]
        a["uniqueIdentifier"], b["uniqueIdentifier"] = b["uniqueIdentifier"], a["uniqueIdentifier"]
        if timing == 3:
            metadata(raw)["fixedLocations"] = {"beta": 7}
    change_read(src, timing, exchange)
    if timing in (1, 2):
        with pytest.raises(LayoutError, match="changed"):
            apply(s)
        assert src.payloads == [] and not s.backups() and not s._pending_path.exists()
    else:
        assert apply(s)["ok"] is (timing is None)
        assert s._pending_path.exists() is (timing == 3)
    if timing:
        assert reviewed(s) == before


@pytest.mark.parametrize("mutation", ["rename", "contents", "move-and-rename", "pre-1", "pre-2"])
def test_remembered_folder_instance_after_its_twin_leaves(tmp_path, mutation):
    s, src = paired_folders(tmp_path)
    src.reported = False
    src.raw["iconLists"].append(["spare", "other"])
    run(s.read())
    s.edit(lambda l: l.pin("folder:alpha", 1, 17))
    assert apply(s)["ok"]
    src.raw["iconLists"][0].pop()
    run(s.read())
    assert s.require().pins == {"folder:alpha": 17}
    if mutation.startswith("pre"):
        before = reviewed(s)
        change_read(src, int(mutation[-1]), lambda raw: raw["iconLists"][0][0].update(uniqueIdentifier="beta"))
        with pytest.raises(LayoutError, match="changed"):
            apply(s)
        assert len(src.payloads) == 1 and reviewed(s) == before
        return
    if mutation in ("rename", "move-and-rename"):
        s.edit(lambda l: l.rename_folder("folder:alpha", "Renamed"))
    if mutation == "contents":
        s.edit(lambda l: l.move("spare", "folder:alpha"))
    if mutation == "move-and-rename":
        s.edit(lambda l: (l.move("spare", "page:1"), l.pin("folder:alpha", 2, 17)))
    assert apply(s)["ok"]
    s = Session(src, s.dir)
    run(s.read())
    assert s.require().pins == {"folder:alpha": 17}
    for page in src.raw["iconLists"]:
        for item in page:
            if isinstance(item, dict) and item.get("uniqueIdentifier") == "alpha":
                item["uniqueIdentifier"] = "beta"
    run(s.read())
    assert s.require().pins == {}


def test_folder_owner_cannot_swap_via_intermediate_fresh_ids(tmp_path):
    s, src = paired_folders(tmp_path, same_content=False)
    read = src.read_raw
    calls = 0
    async def samples(version):
        nonlocal calls
        raw = await read(version)
        calls += 1
        if calls in (1, 2):
            first, second = raw["iconLists"][0]
            first["uniqueIdentifier"], second["uniqueIdentifier"] = (("new-a", "new-b") if calls == 1
                                                                     else ("beta", "alpha"))
        return raw
    src.read_raw = samples
    s.edit(lambda l: l.pin("folder:alpha", 1, 7))
    with pytest.raises(LayoutError, match="changed"):
        apply(s)
    assert not s.backups() and not src.payloads


def app_with_wire(bundle, wire):
    return {**app(bundle, bundle), "displayIdentifier": wire}


@pytest.mark.parametrize("legacy", [False, True])
def test_backup_distinguishes_crossed_bundle_and_wire(tmp_path, legacy):
    src = FrameSource()
    src.raw = [[], [app_with_wire("one", "two"), app_with_wire("two", "wire-two")]]
    s, _ = loaded(tmp_path, src)
    s.edit(lambda l: l.pin("one", 1, 7))
    assert apply(s)["ok"]
    name = apply(s)["backup"]
    path = s._dev_dir("backups") / name
    if legacy:
        blob = plistlib.loads(path.read_bytes())
        blob["provenance"].pop("pin_keys", None)
        path.write_bytes(plistlib.dumps(blob))
        with pytest.raises(LayoutError, match="ambiguous"):
            run(s.restore(name))
        assert len(src.payloads) == 2 and not s._pending_path.exists()
    else:
        assert run(s.restore(name))["ok"]
        assert metadata(src.raw)["fixedLocations"] == {"two": 7}


@pytest.mark.parametrize("failed", [False, True])
def test_unreported_same_bundle_copies_keep_distinct_cells(tmp_path, failed):
    src = FrameSource(False)
    src.raw = [[], [app_with_wire("bundle", "wire-a"), app_with_wire("bundle", "wire-b")]]
    s, _ = loaded(tmp_path, src)
    s.edit(lambda l: (l.pin(l.pages[0][0].id, 1, 7), l.pin(l.pages[0][1].id, 1, 12)))
    assert apply(s)["ok"]
    assert metadata(src.raw)["fixedLocations"] == {"wire-a": 7, "wire-b": 12}
    s = Session(src, s.dir)
    run(s.read())
    assert [s.require().pins[n.id] for n in s.require().pages[0]] == [7, 12]
    s.edit(lambda l: (l.unpin(l.pages[0][0].id), l.unpin(l.pages[0][1].id),
                      l.pin(l.pages[0][1].id, 1, 7)))
    if failed:
        async def reject(payload):
            raise ConnectionError("send failed")
        src.set_icon_state = reject
    assert apply(s)["ok"] is (not failed)
    assert s._pending_path.exists() is failed


def test_unmarked_gap_memory_refuses_bundle_or_wire_cross_owner(tmp_path):
    src = FrameSource(False)
    src.raw = [[], [app_with_wire("one", "two"), app_with_wire("two", "wire-two")]]
    s, _ = loaded(tmp_path, src)
    s._gaps_path.write_text(json.dumps({"version": 2, "grid": s.grid.to_dict(), "pages": [{"two": 7}]}))
    run(s.read())
    assert not s.require().pins


@pytest.mark.parametrize("wire", ["wire-ride", "ride"])
def test_library_inventory_uses_semantic_bundle(tmp_path, wire):
    src = FrameSource()
    src.raw = [[], [app_with_wire("ride", wire), app("other", "Other")]]
    async def installed():
        return {key: {"CFBundleDisplayName": key} for key in ("ride", "other")}
    src.installed_apps = installed
    s, _ = loaded(tmp_path, src)
    s.edit(lambda l: l.move("ride", "library"))
    assert apply(s)["ok"]
    assert src.raw["ignored"] == [wire]


def test_page_icon_wire_cannot_hide_another_installed_bundle(tmp_path):
    src = FrameSource()
    src.raw = [[], [app_with_wire("one", "two")]]
    async def installed():
        return {"one": {}, "two": {}}
    src.installed_apps = installed
    s, _ = loaded(tmp_path, src)
    assert [n.bundle_id for n in s.require().library] == ["two"]


def test_fixture_approval_uses_canonical_path_and_rebinds_legacy(tmp_path):
    raw = plistlib.dumps(to_root_archive([[], [app("a", "A")]], []))
    paths = []
    sessions = []
    for root in ("one", "two"):
        p = tmp_path / root / "same.plist"
        p.parent.mkdir()
        p.write_bytes(raw)
        paths.append(p)
        sessions.append(loaded(p.parent / "data", FileSource(p))[0])
    first, second = sessions
    with pytest.raises(LayoutError, match="token"):
        run(second.apply(first.require().token()))
    assert paths[1].read_bytes() == raw and not second.backups()
    link = tmp_path / "alias.plist"
    link.symlink_to(paths[1])
    assert run(FileSource(link).info())["UniqueDeviceID"] == second.device_id
    blob = plistlib.loads(second.draft_path.read_bytes())
    blob["device"] = "fixture-same"
    second.draft_path.write_bytes(plistlib.dumps(blob))
    old = Session(FileSource(paths[1]), second.dir)
    approval = old.require().token()
    with pytest.raises(LayoutError, match="identity"):
        run(old.apply(approval))
    run(old.read())
    assert old.require().token() != approval and apply(old)["ok"]


@pytest.mark.parametrize("adapter", ["file", "simulator"])
@pytest.mark.parametrize("cols", [4, 5])
@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_zero_page_archive_geometry_checked_before_publication(tmp_path, monkeypatch, adapter, cols, operation):
    rows = 6 if cols == 4 else 7
    path = tmp_path / "IconState.plist"
    raw = plistlib.dumps(to_root_archive([[], [app("a", "A")]], [], [{"a": 7}], Grid(cols, rows)))
    path.write_bytes(raw)
    src = FileSource(path)
    if adapter == "simulator":
        src = SimSource("synthetic")
        src.dir = tmp_path
        monkeypatch.setattr(src, "apps", lambda: {})
        monkeypatch.setattr(device, "_run", lambda *args: json.dumps({"devices": {
            "test.iOS-27-0": [{"udid": src.udid, "name": "Synthetic", "state": "Booted"}]}}))
        # Stand in for the restart after writing the same production archive representation.
        monkeypatch.setattr(src, "write", FileSource(path).write)
    s, _ = loaded(tmp_path / "data", src)
    s.edit(lambda l: (l.move("a", "library"), l.drop_empty()))
    backup = s._backup([[]], [app("a", "A")], "empty") if operation == "restore" else None
    def write():
        return run(s.restore(backup.name)) if backup else apply(s)
    if cols == 4:
        assert write()["ok"] and run(src.read())[0] == [[]]
    else:
        before, backups = reviewed(s), s.backups()
        with pytest.raises(LayoutError, match="page.*grid"):
            write()
        assert reviewed(s) == before and s.backups() == backups and not s._pending_path.exists()
        assert path.read_bytes() == raw


@pytest.mark.parametrize("reported", [False, True])
@pytest.mark.parametrize("regenerated", [False, True])
def test_page_scoped_occurrences_of_a_shared_folder_wire(tmp_path, reported, regenerated):
    src = FrameSource(reported)
    folders = [{**folder(key, [app(key, key)]), "uniqueIdentifier": key if regenerated else "shared"}
               for key in ("a", "b")]
    src.raw = to_root_archive([[], [folders[0]], [folders[1]]], [])
    s, _ = loaded(tmp_path, src)
    def place(lay):
        for index, page in enumerate(lay.pages, 1):
            lay.pin(page[0].id, index, index + 3)
    s.edit(place)
    if regenerated:
        def change(raw):
            for index, page in enumerate(raw["iconLists"]):
                page[0]["uniqueIdentifier"] = "shared"
                page_id = raw["listUniqueIdentifiers"][index]
                raw["listMetadata"][page_id]["fixedLocations"] = {"shared": index + 4}
        change_read(src, 3, change)
    assert apply(s)["ok"]
    lay = s.require()
    assert [lay.pins[page[0].id] for page in lay.pages] == [4, 5]


def test_root_writer_preserves_library_copies_and_adds_only_missing_bundles():
    src = FrameSource()
    async def installed():
        return {"ride": {}, "other": {}, "extra": {}}
    src.installed_apps = installed
    run(src.write([[], [app("other", "Other")]], [app_with_wire("ride", "first"), app_with_wire("ride", "second")]))
    assert src.payloads[-1]["ignored"] == ["first", "second", "extra"]


def test_independent_phone_metrics_allow_zero_pages_on_nondefault_grid(tmp_path):
    s, src = loaded(tmp_path, FrameSource())
    s.edit(lambda l: (l.move("example.a", "library"), l.drop_empty()))
    assert apply(s)["ok"] and src.raw["iconLists"] == []
    run(s.read())
    assert s.grid == Grid(5, 7)


def test_generated_recovery_backup_keeps_sticky_folder_ownership(tmp_path):
    s, src = paired_folders(tmp_path)
    src.reported = False
    run(s.read())
    s.edit(lambda l: l.pin("folder:alpha", 1, 17))
    assert apply(s)["ok"]
    src.raw["iconLists"][0].pop()
    run(s.read())
    s.edit(lambda l: l.rename_folder("folder:alpha", "Renamed"))
    name = apply(s)["backup"]
    assert run(s.restore(name))["ok"]
    src.raw["iconLists"][0][0]["uniqueIdentifier"] = "beta"
    run(s.read())
    assert not s.require().pins


def test_failed_send_with_matching_order_and_unchanged_anchors_can_verify(tmp_path):
    src = FrameSource(False)
    src.raw = [[], [app("a", "A")]]
    s, _ = loaded(tmp_path, src)
    s.edit(lambda l: l.pin("a", 1, 7))
    assert apply(s)["ok"]
    # The odd-display copy was in the library on both prewrite samples.
    src.raw["ignored"] = [app_with_wire("bundle", "wire")]
    run(s.read())
    s.edit(lambda l: l.move("bundle", "page:1"))
    send = src.set_icon_state
    async def delivered(payload):
        await send(payload)
        raise ConnectionError("response lost")
    src.set_icon_state = delivered
    assert apply(s)["ok"] and not s._pending_path.exists()
    assert s.require().pins == {"a": 7}


@pytest.mark.parametrize("flat", [False, True])
def test_dense_legacy_memory_checks_a_bounded_number_of_saved_keys(flat):
    from homescreen.model import Layout
    from homescreen.session import node_pins

    class CountedPins(dict):
        lookups = 0

        def __iter__(self):
            for key in super().__iter__():
                self.lookups += 1
                yield key

        def __contains__(self, key):
            self.lookups += 1
            return super().__contains__(key)

    grid = Grid()
    pages = [[app_with_wire(f"a{page}-{cell}", f"wire{page}-{cell}") for cell in range(grid.slots)]
             for page in range(4)]
    layout = Layout([[], *pages], grid=grid)
    per_page = [{node.id: cell for cell, node in enumerate(page)} for page in layout.pages]
    expected = {key: cell for pins in per_page for key, cell in pins.items()}
    saved = [CountedPins(expected)] if flat else [CountedPins(pins) for pins in per_page]
    memory = saved[0] if flat else {"version": 2, "grid": grid.to_dict(), "pages": saved}

    assert node_pins(layout, memory, by_memory=True, strict=True) == expected
    assert sum(pins.lookups for pins in saved) <= 5 * len(expected)


@pytest.mark.parametrize("flat", [False, True])
@pytest.mark.parametrize("where", ["same-page", "other-page", "distinct"])
def test_legacy_alias_claims_are_counted_across_apps_and_widgets(tmp_path, flat, where):
    from fake_phone import widget
    from homescreen.session import node_pins

    src = FrameSource(False)
    src.raw = [[], [app_with_wire("bundle-one", "wire-one"), app("keep", "Keep")]]
    s, _ = loaded(tmp_path, src)
    saved = {"bundle-one": 7, "keep": 3}
    memory = saved if flat else {"version": 2, "grid": s.grid.to_dict(), "pages": [saved]}
    s._gaps_path.write_text(json.dumps(memory))
    item = {**widget("Sample", "small"), "uniqueIdentifier": "other" if where == "distinct" else "bundle-one"}
    if where == "other-page":
        src.raw.append([item])
    else:
        src.raw[1].append(item)
    run(s.read())
    ambiguous = where == "same-page" or (flat and where == "other-page")
    assert s.require().pins == ({"keep": 3} if ambiguous else {"keep": 3, "bundle-one": 7})
    if ambiguous:
        with pytest.raises(LayoutError, match="ambiguous|identify|identifiers"):
            node_pins(s.require(), memory, by_memory=True, strict=True)


def test_flat_folder_name_alias_cannot_be_claimed_by_an_app(tmp_path):
    src = FrameSource(False)
    src.raw = [[], [app_with_wire("folder:Tools", "wire"), folder("Tools", [app("a", "A")])]]
    s, _ = loaded(tmp_path, src)
    s._gaps_path.write_text(json.dumps({"folder:Tools": 7}))
    run(s.read())
    assert s.require().pins == {}


def test_typed_app_pin_is_independent_of_matching_widget_wire(tmp_path):
    from fake_phone import widget

    src = FrameSource(False)
    src.raw = [[], [app_with_wire("bundle-one", "wire-one")]]
    s, _ = loaded(tmp_path, src)
    s.edit(lambda l: l.pin("bundle-one", 1, 7))
    assert apply(s)["ok"]
    src.raw["iconLists"][0].append({**widget("Sample", "small"), "uniqueIdentifier": "bundle-one"})
    run(s.read())
    assert s.require().pins == {"bundle-one": 7}
    s.edit(lambda l: l.pin("widget:bundle-one", 1, 15))
    assert apply(s)["ok"]
    assert s.require().pins == {"bundle-one": 7, "widget:bundle-one": 15}


@pytest.mark.parametrize("operation", ["apply", "restore", "direct"])
@pytest.mark.parametrize("overlap", [False, True])
def test_root_archive_checks_final_inventory_owners_before_send(tmp_path, operation, overlap):
    src = FrameSource()
    target = [[], [app_with_wire("one", "two" if overlap else "wire-one")]]
    src.raw = target if operation != "restore" else [[], [app("one", "One"), app("two", "Two")]]
    async def installed():
        return {"one": {}, "two": {}}
    src.installed_apps = installed
    s, _ = loaded(tmp_path, src)
    backup = s._backup(target, [], "older") if operation == "restore" else None
    before, backups = reviewed(s), s.backups()
    def write():
        if backup:
            return run(s.restore(backup.name))
        if operation == "direct":
            return run(src.write(target, []))
        return apply(s)
    if overlap:
        with pytest.raises(LayoutError, match="identifier"):
            write()
        assert src.payloads == [] and not s._pending_path.exists()
        assert reviewed(s) == before and s.backups() == backups
    else:
        result = write()
        assert operation == "direct" or result["ok"]
        assert src.payloads[-1]["ignored"] == ["two"]
        if operation != "direct":
            assert s.require().pages[0][0].bundle_id == "one" and s.require().library[0].bundle_id == "two"


def test_root_archive_prepares_inventory_once_before_publication(tmp_path):
    src = FrameSource()
    src.raw = [[], [app("one", "One")]]
    s, _ = loaded(tmp_path, src)
    old = s._backup
    published = False
    def backup(*args, **kwargs):
        nonlocal published
        published = True
        return old(*args, **kwargs)
    s._backup = backup
    observed = []
    async def inventory():
        observed.append(published)
        return {"one": {}, "extra": {}} if len(observed) >= 3 else {"one": {}}
    src.installed_apps = inventory
    assert apply(s)["ok"]
    assert not any(observed) and len(observed) == 3
    assert src.payloads[-1]["ignored"] == ["extra"]


def test_same_bundle_repetition_across_page_and_library_keeps_multiplicity(tmp_path):
    src = FrameSource()
    src.raw = [[], [app("one", "One")]]
    s, _ = loaded(tmp_path, src)
    lib = [app("one", "One"), app_with_wire("one", "another"), app("one", "One")]
    backup = s._backup(src.raw, lib, "previous")
    assert run(s.restore(backup.name))["ok"]
    assert src.payloads[-1]["ignored"] == ["one", "another", "one"]


def test_literal_root_string_cannot_claim_another_apps_display_id():
    with pytest.raises(LayoutError, match="identifier"):
        to_root_archive([[], ["two", app_with_wire("one", "two")]], [])
    assert to_root_archive([[], ["two", app("two", "Two")]], [])["iconLists"] == [["two", "two"]]


@pytest.mark.parametrize("container", ["dock", "folder"])
def test_root_identifier_ownership_includes_all_serialized_containers(container):
    odd = app_with_wire("one", "two")
    state = [[odd], []] if container == "dock" else [[], [folder("Tools", [odd])]]
    with pytest.raises(LayoutError, match="identifier"):
        to_root_archive(state, [app("two", "Two")])


@pytest.mark.parametrize("dictionary", [False, True])
def test_other_phone_formats_preserve_explicit_odd_bundle_fields(tmp_path, dictionary):
    src = FrameSource()
    state = [[], [app_with_wire("one", "two"), app("two", "Two")]]
    src.raw = {"buttonBar": [], "iconLists": state[1:], "ignored": []} if dictionary else state
    src.write_mode = "list"
    s, _ = loaded(tmp_path, src)
    assert apply(s)["ok"]
    assert [n.bundle_id for n in s.require().pages[0]] == ["one", "two"]


@pytest.mark.parametrize("shared", [False, True])
def test_typed_app_cannot_consume_missing_clip(tmp_path, shared):
    clip = {"iconType": "webClip", "displayIdentifier": "wire", "bundleIdentifier": "semantic",
            "webClipURL": "/news", "config": {"columns": 3}}
    ordinary = {"iconType": "app", "displayIdentifier": "wire" if shared else "another",
                "bundleIdentifier": "semantic", "displayName": "Reader"}
    path = tmp_path / "layout.plist"
    raw = {"buttonBar": [], "iconLists": [[clip, ordinary, "move"]], "ignored": []}
    path.write_bytes(plistlib.dumps(raw))
    sess = Session(FileSource(path), tmp_path / "data")
    run(sess.read())
    assert sorted(n.kind for n in sess.require().nodes.values()) == ["app", "app", "webclip"]
    raw["iconLists"] = [[ordinary, "move"]]
    path.write_bytes(plistlib.dumps(raw))
    run(sess.read())
    draft = [n.raw for n in sess.require().library]
    sess.edit(lambda l: l.move("move", "dock"))
    result = apply(sess)
    written = plistlib.loads(path.read_bytes())
    assert result["ok"] and written["iconLists"][0] == [ordinary]
    assert draft == written["ignored"] == [clip]


def test_bare_and_typed_clip_occurrences_preserve_both_remembered_configurations(tmp_path):
    wire = "com.apple.webclip.shared"
    clips = [{"iconType": "webClip", "displayIdentifier": wire, "webClipURL": name} for name in ("/a", "/b")]
    raw = {"iconLists": [[*clips, "stay"]], "ignored": []}
    path = tmp_path / "layout.plist"
    path.write_bytes(plistlib.dumps(raw))
    session = Session(FileSource(path), tmp_path / "data")
    run(session.read())
    for page in ([wire, clips[1], "stay"], ["stay"]):
        raw["iconLists"] = [page]
        path.write_bytes(plistlib.dumps(raw))
        run(session.read())
    assert sorted((n.raw for n in session.require().library), key=lambda x: x["webClipURL"]) == clips


@pytest.mark.parametrize("cross_page", [False, True])
def test_reported_anchor_identifies_unique_owner_across_pages(tmp_path, cross_page):
    src = ArchiveSource(4, 6, "dictionary")
    src.raw = to_root_archive([[], [app("target", "Target"), app("left", "Left")], [app("right", "Right")]], [])
    sess = Session(src, tmp_path / "data")
    run(sess.read())
    sess.edit(lambda l: l.pin("target", 1, 5))
    send = src.set_icon_state

    async def returned(payload):
        await send(payload)
        raw = src.raw
        first, second = raw["listUniqueIdentifiers"]
        meta = raw["listMetadata"]
        meta[first]["fixedLocations"]["target"] = 3
        if cross_page:
            raw["iconLists"][1].append(raw["iconLists"][0].pop(0))
            meta[second] = meta.pop(first)

    src.set_icon_state = returned
    result = apply(sess)
    fixed = result["mismatches"]["fixed_position"]
    assert result["status"] == "unknown" and sess._pending_path.exists() and result["backup"] in sess.backups()
    assert fixed["observed_cell"] == 3 and fixed["observed"] == {"row": 1, "column": 4}
    if cross_page:
        assert fixed["page"] == 1 and fixed["observed_page"] == 2


@pytest.mark.parametrize("owner", ["stable", "regenerated", "ambiguous"])
def test_unique_reported_folder_location_after_cross_page_move(tmp_path, owner):
    source = ArchiveSource(4, 6, "dictionary")
    item = {**folder("Tools", [app("inside", "Inside")]), "uniqueIdentifier": "tools"}
    other = [app("right", "Right")]
    if owner == "ambiguous":
        other.append({**item, "uniqueIdentifier": "other-tools"})
    source.raw = to_root_archive([[], [item, app("left", "Left")], other], [])
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda layout: layout.pin("folder:tools", 1, 5))
    send = source.set_icon_state

    async def moved(payload):
        await send(payload)
        raw = source.raw
        first, second = raw["listUniqueIdentifiers"]
        item = raw["iconLists"][0].pop(0)
        raw["iconLists"][1].append(item)
        if owner != "stable":
            item["uniqueIdentifier"] = "new-tools"
        record = raw["listMetadata"].pop(first)
        record["fixedLocations"] = {item["uniqueIdentifier"]: 3}
        raw["listMetadata"][second] = record

    source.set_icon_state = moved
    result = apply(session)
    assert result["status"] == "unknown" and session._pending_path.exists() and result["backup"] in session.backups()
    detail = result.get("mismatches", {}).get("fixed_position", {})
    if owner == "ambiguous":
        assert detail.get("observed_page") is None and detail.get("observed_cell") is None
    else:
        assert detail.get("observed_page") == 2 and detail.get("observed_cell") == 3


@pytest.mark.parametrize("explicit", [True, False])
def test_compact_owned_app_keeps_unrelated_clip(tmp_path, explicit):
    clip = {"iconType": "webClip", "displayIdentifier": "shared", "bundleIdentifier": "reader.bookmark",
            "webClipURL": "/news", "settings": {"owner": "clip"}}
    ordinary = {"displayIdentifier": "shared", "bundleIdentifier": "reader.native", "displayName": "Reader"}
    if explicit:
        ordinary["iconType"] = "app"
    path = tmp_path / "phone.plist"
    raw = {"buttonBar": [], "iconLists": [[clip, ordinary, "move"]], "ignored": []}
    path.write_bytes(plistlib.dumps(raw))
    s = Session(FileSource(path), tmp_path / "store")
    run(s.read())
    assert s.require().get("reader.native").kind == "app" and s.require().get("reader.bookmark").kind == "webclip"
    raw["iconLists"] = [[ordinary, "move"]]
    path.write_bytes(plistlib.dumps(raw))
    run(s.read())
    s.edit(lambda l: l.move("move", "dock"))
    outcome = apply(s)
    actual = plistlib.loads(path.read_bytes())
    assert outcome["ok"] and actual["iconLists"][0] == [ordinary] and actual["ignored"] == [clip]


@pytest.mark.parametrize("bare_first", [False, True])
def test_mixed_proxy_occurrences_reserve_matching_semantic_bundles(tmp_path, bare_first):
    clips = [{"iconType": "webClip", "displayIdentifier": "wire", "bundleIdentifier": owner,
              "webClipURL": "/" + owner} for owner in ("one", "two", "three", "four")]
    path = tmp_path / "layout.plist"
    raw = {"iconLists": [[*clips, "stay"]], "ignored": []}
    path.write_bytes(plistlib.dumps(raw))
    session = Session(FileSource(path), tmp_path / "data")
    run(session.read())
    proxies = [{"displayIdentifier": "wire", "bundleIdentifier": "two"},
               {"displayIdentifier": "wire", "bundleIdentifier": "foreign"}]
    proxies.insert(0 if bare_first else len(proxies), "wire")
    raw["iconLists"] = [[*proxies, clips[3], "stay"]]
    path.write_bytes(plistlib.dumps(raw))
    run(session.read())
    assert [node.raw for node in session.require().library] == [clips[2]]


@pytest.mark.parametrize("move_app_too", [False, True])
def test_early_folder_owner_diagnostic_keeps_its_cause(tmp_path, move_app_too):
    src = ArchiveSource(4, 6, "dictionary")
    f = {**folder("Filed", [app("inside", "Inside")]), "uniqueIdentifier": "f"}
    src.raw = to_root_archive([[], [app("first", "First"), f, app("left", "Left")],
                                [app("second", "Second")], [app("third", "Third")]], [])
    s = Session(src, tmp_path / "data")
    run(s.read())
    def pin(l):
        l.pin("first", 1, 5)
        l.pin("folder:f", 1, 7)
    s.edit(pin)
    write = src.set_icon_state
    async def moved(payload):
        await write(payload)
        raw = src.raw
        srcpage, apppage, folderpage = raw["iconLists"]
        p1,p2,p3 = raw["listUniqueIdentifiers"]
        meta = raw["listMetadata"]
        row = meta[p1]
        folderpage.append(srcpage.pop(next(i for i,n in enumerate(srcpage) if isinstance(n,dict))))
        del row["fixedLocations"]["f"]
        meta[p3] = {**row, "fixedLocations": {"f": 2}}
        if move_app_too:
            apppage.append(srcpage.pop(srcpage.index("first")))
            del row["fixedLocations"]["first"]
            meta[p2] = {**row, "fixedLocations": {"first": 1}}
    src.set_icon_state = moved
    result = apply(s)
    assert result["status"] == "unknown" and s._pending_path.exists() and result["backup"] in s.backups()
    detail = result["mismatches"]["fixed_position"]
    assert (detail["owner"], detail["page"], detail["requested_cell"]) == ("folder:f", 1, 7)
    assert (detail["observed_page"], detail["observed_cell"]) == (3, 2)
    assert detail["observed"] == {"row": 1, "column": 3}
