import asyncio
import copy
import io
import json
import plistlib
from pathlib import Path

import pytest
from PIL import Image

from homescreen.device import SimSource
from homescreen.model import LayoutError
from homescreen.session import Session
from fake_phone import app, widget


def png():
    output = io.BytesIO()
    Image.new('RGB', (120, 120), 'blue').save(output, format='PNG')
    return output.getvalue()


def simulator_app(tmp_path, info):
    bundle = tmp_path / 'Example.app'
    bundle.mkdir()
    (bundle / 'Info.plist').write_bytes(plistlib.dumps(info))
    source = SimSource('synthetic-simulator')
    source._apps = {'com.example.app': {'Bundle': bundle.as_uri()}}
    return source, bundle


def test_simulator_icon_reads_primary_files_from_bundle_metadata(tmp_path):
    source, bundle = simulator_app(tmp_path, {'CFBundleIcons': {'CFBundlePrimaryIcon': {
        'CFBundleIconFiles': ['LegacyIcon-120']}}})
    expected = png()
    (bundle / 'LegacyIcon-120.png').write_bytes(expected)
    assert asyncio.run(source.icon_png('com.example.app')) == expected


@pytest.mark.parametrize("type_name,has_tablet_icon,legacy_phone,reopen,expected", [
    ("iPhone", True, False, False, b"phone"), ("iPad", True, False, False, b"tablet"),
    ("iPad", False, False, False, b"phone"), ("iPhone", True, True, False, b"phone"),
    ("iPad", True, False, True, b"tablet"),
])
def test_simulator_selects_declared_icon_for_device_family(tmp_path, monkeypatch, type_name, has_tablet_icon,
                                                           legacy_phone, reopen, expected):
    info = {"CFBundleIcons~ipad": {"CFBundlePrimaryIcon": {"CFBundleIconFiles": ["Tablet"]}}}
    if legacy_phone:
        info["CFBundleIconFiles"] = ["Phone"]
    else:
        info["CFBundleIcons"] = {"CFBundlePrimaryIcon": {"CFBundleIconFiles": ["Phone"]}}
    source, bundle = simulator_app(tmp_path, info)
    (bundle / "Phone@3x.png").write_bytes(b"phone")
    if has_tablet_icon:
        (bundle / "Tablet@2x~ipad.png").write_bytes(b"tablet")
    devices = {"devices": {"fixture.iOS-27-2": [{"udid": source.udid, "name": "Fixture", "state": "Booted",
                                                "deviceTypeIdentifier": f"fixture.{type_name}-sample"}]}}
    monkeypatch.setattr("homescreen.device._run", lambda *args: json.dumps(devices))
    source.dir = tmp_path
    source.path.write_bytes(plistlib.dumps({"iconLists": [], "ignored": []}))
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    if reopen:
        fresh = SimSource(source.udid)
        fresh.dir, fresh._apps = source.dir, copy.deepcopy(source._apps)
        session = Session(fresh, session.dir)
    assert asyncio.run(session.icon("com.example.app")) == expected
    assert asyncio.run(session.icon("com.example.app")) == expected


def test_partial_primary_does_not_hide_declared_inventory_file(tmp_path, monkeypatch):
    source, bundle = simulator_app(tmp_path, {"CFBundleIcons": {"CFBundlePrimaryIcon": {"CFBundleIconName": "Main"}}})
    source._apps["com.example.app"]["CFBundleIcons"] = {"CFBundlePrimaryIcon": {"CFBundleIconFiles": ["Declared"]}}
    (bundle / "Declared@2x.png").write_bytes(b"inventory")
    monkeypatch.setattr("homescreen.device._simulator_icon_png", lambda *args: pytest.fail("unexpected renderer"))
    assert asyncio.run(source.icon_png("com.example.app")) == b"inventory"


def test_simulator_icon_reads_top_level_scaled_file_names(tmp_path):
    source, bundle = simulator_app(tmp_path, {'CFBundleIconFiles': ['AppIcon@3x']})
    expected = png()
    (bundle / 'AppIcon@3x.png').write_bytes(expected)
    assert asyncio.run(source.icon_png('com.example.app')) == expected


def test_simulator_icon_scales_an_extension_bearing_name(tmp_path):
    source, bundle = simulator_app(tmp_path, {'CFBundleIconFiles': ['LegacyIcon.png']})
    expected = png()
    (bundle / 'LegacyIcon@2x.png').write_bytes(expected)
    assert asyncio.run(source.icon_png('com.example.app')) == expected


@pytest.mark.parametrize('symlink', [False, True])
def test_declared_icon_is_an_application_resource(tmp_path, symlink):
    name = 'AppIcon60x60@3x.png' if symlink else '../Other.png'
    source, bundle = simulator_app(tmp_path, {'CFBundleIconFiles': [name]})
    outside = tmp_path / 'Other.png'
    outside.write_bytes(png())
    if symlink:
        (bundle / name).symlink_to(outside)
    assert asyncio.run(source.icon_png('com.example.app')) is None


def test_simulator_asset_catalog_uses_its_pinned_simulator(tmp_path, monkeypatch):
    source, bundle = simulator_app(tmp_path, {'CFBundleIcons': {'CFBundlePrimaryIcon': {
        'CFBundleIconName': 'AppIcon'}}})
    (bundle / 'Assets.car').write_bytes(b'synthetic catalog')
    expected = png()
    calls = []

    def read_icon(udid, bundle_id):
        calls.append((udid, bundle_id))
        return expected

    monkeypatch.setattr('homescreen.device._simulator_icon_png', read_icon, raising=False)
    assert asyncio.run(source.icon_png('com.example.app')) == expected
    assert calls == [('synthetic-simulator', 'com.example.app')]


class VersionedSource:
    name = 'synthetic'

    def __init__(self, version):
        self.version = version
        self.state = [[], [widget('Sample', 'small'), app('a', 'A'), app('b', 'B')]]
        self.writes = 0

    async def info(self):
        return {'UniqueDeviceID': 'synthetic', 'ProductVersion': self.version}

    async def read(self):
        return copy.deepcopy(self.state), []

    async def write(self, state, library, pins=None, grid=None):
        self.writes += 1
        self.state = copy.deepcopy(state)


def loaded(tmp_path, version='16.0'):
    source = VersionedSource(version)
    session = Session(source, tmp_path / 'session')
    asyncio.run(session.read())
    return session, source


def test_legacy_fixed_position_edit_preserves_draft(tmp_path):
    session, source = loaded(tmp_path)
    before = session.require().token()
    with pytest.raises(LayoutError, match='iOS 16.*fixed'):
        session.edit(lambda lay: lay.pin('a', 1, 23))
    assert session.require().token() == before
    assert session.history() == {'undo': 0, 'redo': 0}
    assert source.writes == 0


def test_legacy_fixed_position_apply_does_not_write(tmp_path):
    session, source = loaded(tmp_path)
    session.require().pin('a', 1, 23)
    with pytest.raises(LayoutError, match='iOS 16.*fixed'):
        asyncio.run(session.apply(session.require().token()))
    assert source.writes == 0


def test_legacy_folder_before_widget_is_an_atomic_edit(tmp_path):
    session, source = loaded(tmp_path)
    before = session.require().token()
    with pytest.raises(LayoutError, match='iOS 16.*widget'):
        session.edit(lambda lay: lay.create_folder('Example', ['a', 'b'], 'page:1', 0))
    assert session.require().token() == before
    assert source.writes == 0


def test_legacy_folder_after_widget_remains_editable(tmp_path):
    session, _ = loaded(tmp_path)
    folder_id = session.edit(lambda lay: lay.create_folder('Example', ['a', 'b'], 'page:1', 1))
    assert session.require().pages[0][1].id == folder_id
    assert not session.require().validate()


def test_newer_placement_remains_editable(tmp_path):
    session, _ = loaded(tmp_path, '17.0')
    session.edit(lambda lay: lay.pin('a', 1, 23))
    assert session.require().pins['a'] == 23


def test_legacy_reset_clears_a_saved_unsupported_position(tmp_path):
    session, source = loaded(tmp_path)
    original = session.require().token()
    session.require().pin('a', 1, 23)
    session.edited()
    session.reset()
    assert session.require().token() == original
    assert not Session(source, session.dir).require().pins
    assert session.undo()
    assert session.require().pins['a'] == 23


@pytest.mark.parametrize("family", ["iPad", "unknown"])
def test_generic_image_when_tablet_resource_is_absent(tmp_path, monkeypatch, family):
    source, bundle = simulator_app(tmp_path, {"CFBundleIcons": {"CFBundlePrimaryIcon": {"CFBundleIconFiles": ["Generic"]}},
                                            "CFBundleIcons~ipad": {"CFBundlePrimaryIcon": {"CFBundleIconFiles": ["Unavailable"]}}})
    item = {"udid": source.udid, "name": "Private", "state": "Booted"}
    if family != "unknown":
        item["deviceTypeIdentifier"] = "fixture.iPad"
    inventory = {"devices": {"test.iOS-27": [item]}}
    def fixture(*args):
        assert args == ("xcrun", "simctl", "list", "devices", "-j")
        return json.dumps(inventory)
    monkeypatch.setattr("homescreen.device._run", fixture)
    monkeypatch.setattr("homescreen.device._simulator_icon_png", lambda *args: pytest.fail("unexpected renderer"))
    (bundle / "Generic@2x.png").write_bytes(b"generic")
    got = asyncio.run(source.icon_png("com.example.app"))
    assert got == b"generic"


@pytest.mark.parametrize("tablet_image", [b"", b"generic", b"tablet"])
def test_unknown_family_only_caches_an_image_common_to_both_families(tmp_path, monkeypatch, tablet_image):
    info = {"CFBundleIcons": {"CFBundlePrimaryIcon": {"CFBundleIconFiles": ["Generic"]}},
            "CFBundleIcons~ipad": {"CFBundlePrimaryIcon": {"CFBundleIconFiles": ["Tablet"]}}}
    source, bundle = simulator_app(tmp_path, info)
    source.dir = tmp_path
    source.path.write_bytes(plistlib.dumps({"iconLists": [["com.example.app"]], "ignored": []}))
    inventory = {"devices": {"test.iOS-27": [{"udid": source.udid, "name": "Private", "state": "Booted"}]}}
    monkeypatch.setattr("homescreen.device._run", lambda *args: json.dumps(inventory))
    monkeypatch.setattr("homescreen.device._simulator_icon_png", lambda *args: pytest.fail("unexpected renderer"))
    (bundle / "Generic@2x.png").write_bytes(b"generic")
    (bundle / "Tablet@2x~ipad.png").write_bytes(tablet_image)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    result = asyncio.run(session.icon("com.example.app"))
    assert result == (None if tablet_image == b"tablet" else b"generic")
    assert bool(list(session._dev_dir("icons").glob("*.png"))) == (result is not None)


@pytest.mark.parametrize("family", ["iPhone", "iPad"])
@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("selected_denied", [False, True])
def test_known_family_opens_only_its_selected_icon(tmp_path, monkeypatch, family, reopen, selected_denied):
    info = {"CFBundleIcons": {"CFBundlePrimaryIcon": {"CFBundleIconFiles": ["Phone"]}},
            "CFBundleIcons~ipad": {"CFBundlePrimaryIcon": {"CFBundleIconFiles": ["Tablet"]}}}
    source, bundle = simulator_app(tmp_path, info)
    source.dir = tmp_path
    source.path.write_bytes(plistlib.dumps({"iconLists": [["com.example.app"]], "ignored": []}))
    phone, tablet = bundle / "Phone@2x.png", bundle / "Tablet@2x~ipad.png"
    phone.write_bytes(b"phone")
    tablet.write_bytes(b"tablet")
    calls = []

    def inventory(*args):
        assert args == ("xcrun", "simctl", "list", "devices", "-j")
        calls.append("family")
        return json.dumps({"devices": {"test.iOS-27": [{"udid": source.udid, "name": "Fixture", "state": "Booted",
                          "deviceTypeIdentifier": f"fixture.{family}"}]}})
    monkeypatch.setattr("homescreen.device._run", inventory)
    monkeypatch.setattr("homescreen.device._simulator_icon_png", lambda *args: pytest.fail("unexpected renderer"))
    session = Session(source, tmp_path / "store")
    asyncio.run(session.read())
    if reopen:
        fresh = SimSource(source.udid)
        fresh.dir, fresh._apps = source.dir, copy.deepcopy(source._apps)
        session = Session(fresh, session.dir)
    selected, other = (phone, tablet) if family == "iPhone" else (tablet, phone)
    expected = selected.read_bytes()
    denied = selected if selected_denied else other
    read_bytes = Path.read_bytes

    def read_image(path):
        if path in (phone, tablet):
            calls.append(path.name)
        if path == denied:
            raise PermissionError(13, "Permission denied", str(path))
        return read_bytes(path)
    monkeypatch.setattr(Path, "read_bytes", read_image)
    calls.clear()
    if selected_denied:
        with pytest.raises(PermissionError) as failure:
            asyncio.run(session.icon("com.example.app"))
        assert Path(failure.value.filename) == selected
    else:
        assert asyncio.run(session.icon("com.example.app")) == expected
    assert calls == (["family"] if reopen else []) + [selected.name]
    assert bool(list(session._dev_dir("icons").glob("*.png"))) == (not selected_denied)
