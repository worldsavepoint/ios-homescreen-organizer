import asyncio
import copy
import io
import plistlib

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
