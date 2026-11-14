"""Loaded grids reach serialized payloads through synthetic source transactions."""

import asyncio
import copy
import plistlib

import pytest

from homescreen.device import FileSource, PhoneSource, disk_pins, model_to_disk, to_root_archive
from homescreen.model import Grid
from homescreen.session import Session
from fake_phone import app


class ArchiveSource(PhoneSource):
    """Run the source serializers against an in-memory SpringBoard transport."""

    def __init__(self, cols, rows, representation):
        super().__init__("synthetic")
        self.cols, self.rows = cols, rows
        self.raw = [[], [app("com.example.a", "A"), app("com.example.b", "B")]]
        if representation == "dictionary":
            self.raw = to_root_archive(self.raw, [])
        self.payloads = []

    async def info(self):
        return {"UniqueDeviceID": "synthetic", "ProductVersion": "27.0", "icon_metrics": {
            "homeScreenIconColumns": self.cols, "homeScreenIconRows": self.rows}}

    async def read_raw(self, format_version):
        return copy.deepcopy(self.raw)

    async def read(self):
        result = await super().read()
        self.pins = disk_pins(self.raw) if isinstance(self.raw, dict) else None
        return result

    async def installed_apps(self):
        return {}

    async def _call(self, operation):
        return await operation(self)

    async def set_icon_state(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        self.raw = copy.deepcopy(payload)


def metadata(payload):
    return payload["listMetadata"][payload["listUniqueIdentifiers"][0]]


@pytest.mark.parametrize("cols,rows,cell", [(4, 6, 23), (5, 7, 34)])
@pytest.mark.parametrize("representation", ["root", "dictionary"])
def test_session_apply_and_restore_serialize_loaded_grid(tmp_path, monkeypatch, cols, rows, cell, representation):
    monkeypatch.delenv("HOMESCREEN_READ_ONLY", raising=False)
    source = ArchiveSource(cols, rows, representation)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda lay: lay.pin("com.example.a", 1, cell))
    assert asyncio.run(session.apply(session.require().token()))["ok"] is True
    assert metadata(source.payloads[-1]) == {
        "fixedLocations": {"com.example.a": cell},
        "fixedLocationsGridColumns": cols, "fixedLocationsGridRows": rows}
    session.edit(lambda lay: lay.pin("com.example.a", 1, 0))
    second = asyncio.run(session.apply(session.require().token()))
    assert second["ok"] is True
    assert asyncio.run(session.restore(second["backup"]))["ok"] is True
    assert metadata(source.payloads[-1]) == {
        "fixedLocations": {"com.example.a": cell},
        "fixedLocationsGridColumns": cols, "fixedLocationsGridRows": rows}
    assert session.require().summary()["pages"]["1"][0]["placed_at"] == {"row": rows, "column": cols}


def test_file_source_serializes_explicit_grid_and_legacy_calls_keep_iphone_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("HOMESCREEN_READ_ONLY", raising=False)
    state = [[], [app("com.example.a", "A")]]
    pins = [{"com.example.a": 23}]
    base = to_root_archive(state, [])
    for payload in (to_root_archive(state, [], pins), model_to_disk(state, [], base, pins)):
        assert metadata(payload) == {"fixedLocations": {"com.example.a": 23},
                                     "fixedLocationsGridColumns": 4, "fixedLocationsGridRows": 6}
    path = tmp_path / "synthetic.plist"
    path.write_bytes(plistlib.dumps(base))
    asyncio.run(FileSource(path).write(state, [], [{"com.example.a": 34}], Grid(cols=5, rows=7)))
    assert metadata(plistlib.loads(path.read_bytes())) == {"fixedLocations": {"com.example.a": 34},
                                                         "fixedLocationsGridColumns": 5,
                                                         "fixedLocationsGridRows": 7}
