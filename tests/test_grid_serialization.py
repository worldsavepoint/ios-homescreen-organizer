"""Loaded grids reach serialized payloads through synthetic source transactions."""

import asyncio
import copy
import plistlib

import pytest

from homescreen.device import FileSource, PhoneSource, disk_pins, model_to_disk, to_root_archive
from homescreen.model import Grid, LayoutError
from homescreen.session import Session
from homescreen import ui
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


def test_folder_columns_survive_reopen_refresh_and_failed_read_without_changing_receipt_grid(tmp_path, monkeypatch):
    source = ArchiveSource(4, 6, "dictionary")
    metrics = {"homeScreenIconFolderColumns": 4, "homeScreenIconFolderRows": 3}
    info = source.info
    async def rectangular():
        observation = await info()
        observation["icon_metrics"].update(metrics)
        return observation
    source.info = rectangular
    first = Session(source, tmp_path)
    asyncio.run(first.read())
    second = Session(source, tmp_path)
    assert ui.view(second)["grid"]["folder_columns"] == 4
    metrics.update(homeScreenIconFolderColumns=3, homeScreenIconFolderRows=4)
    with monkeypatch.context() as patch:
        def fail():
            raise OSError("draft unavailable")
        patch.setattr(first, "_save_draft", fail)
        with pytest.raises(OSError):
            asyncio.run(first.read())
    assert ui.view(first)["grid"]["folder_columns"] == 4
    asyncio.run(first.read())
    with second.refreshed_draft():
        assert ui.view(second)["grid"]["folder_columns"] == 3
    result = asyncio.run(second.apply(second.require().token()))
    assert result["ok"]
    receipt = plistlib.loads((second._dev_dir("backups") / result["backup"]).read_bytes())
    assert receipt["provenance"]["grid"] == {"cols": 4, "rows": 6, "dock": 4, "folder": 12}
    saved = plistlib.loads(second.draft_path.read_bytes())
    saved.pop("folder_columns")
    second.draft_path.write_bytes(plistlib.dumps(saved))
    assert "folder_columns" not in ui.view(Session(source, tmp_path))["grid"]


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


@pytest.mark.parametrize("regroup", [False, True])
def test_today_typed_values_are_independent_of_object_references(tmp_path, regroup):
    src = ArchiveSource(4, 6, "dictionary")
    src.raw = to_root_archive([[], [app("a", "A"), app("b", "B")]], [])
    options = {"width": 2, "compact": False}
    src.raw["today"] = [{"name": "Weather", "options": options}, {"name": "Clock", "options": options}]
    sess = Session(src, tmp_path / "data")
    asyncio.run(sess.read())
    sess.edit(lambda l: l.move("b", "dock"))
    send = src.set_icon_state

    async def returned(payload):
        await send(payload)
        if regroup:
            src.raw["today"] = [copy.deepcopy(entry) for entry in src.raw["today"]]

    src.set_icon_state = returned
    result = asyncio.run(sess.apply(sess.require().token()))
    original = src.payloads[-1]["today"]
    after = src.raw["today"]
    assert original == after
    assert result["status"] == "verified"


@pytest.mark.parametrize("swapped", [False, True])
def test_shape_reported_late_in_read_is_checked(tmp_path, swapped):
    src = ArchiveSource(4, 6, "dictionary")
    info = src.info
    calls = 0
    async def changed_metrics():
        nonlocal calls
        details = await info()
        calls += 1
        # Initial metrics have the supported default three columns, four observed rows.
        # The follow-up reports the same effective geometry or transposes that rectangle.
        geometry = {"homeScreenIconFolderRows": 4}
        if calls > 1:
            geometry = {"homeScreenIconFolderColumns": 4 if swapped else 3,
                        "homeScreenIconFolderRows": 3 if swapped else 4}
        details["icon_metrics"].update(geometry)
        return details
    src.info = changed_metrics
    s = Session(src, tmp_path / "data")
    if swapped:
        with pytest.raises(LayoutError, match="grid changed"):
            asyncio.run(s.read())
        assert not s.draft_path.exists()
    else:
        asyncio.run(s.read())
        assert ui.view(Session(src, s.dir))["grid"]["folder_columns"] == 3
