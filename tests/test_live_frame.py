"""Fresh source metrics guard approved coordinates using a trapped memory transport."""

import copy
import os
import socket
import subprocess

import pytest

from fake_phone import app
from homescreen.device import to_root_archive
from homescreen.model import Grid, LayoutError
from homescreen.session import Session
from test_observed_placement import PhoneArchiveSource
from test_pin_integrity import apply, run


@pytest.fixture(autouse=True)
def external_traps(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external transport forbidden in synthetic regression")
    monkeypatch.delenv("HOMESCREEN_READ_ONLY", raising=False)
    for owner, names in ((socket, ("create_connection",)),
                         (socket.socket, ("connect", "connect_ex", "sendto")),
                         (subprocess, ("Popen", "run")), (os, ("system",))):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)


class FrameSource(PhoneArchiveSource):
    def __init__(self, reported=True):
        super().__init__(5, 7, "dictionary")
        self.metrics = {"homeScreenIconColumns": 5, "homeScreenIconRows": 7}
        self.info_count = 0
        self.transition_at = None
        self.next_metrics = None
        self.reported = reported
        self.raw = to_root_archive([[], [app("example.a", "A")]], [])

    async def info(self):
        self.info_count += 1
        if self.info_count == self.transition_at:
            self.metrics = self.next_metrics
        return {"UniqueDeviceID": "synthetic", "ProductVersion": "27.0",
                "icon_metrics": copy.deepcopy(self.metrics)}

    async def read(self):
        state, library = await super().read()
        if not self.reported:
            self.pins = None
        return state, library


def reviewed(session):
    return (copy.deepcopy(session._snap()), session.draft_path.read_bytes(), session.history(),
            session._gaps_path.read_bytes() if session._gaps_path.exists() else None)


@pytest.mark.parametrize("operation", ["apply", "restore"])
@pytest.mark.parametrize("reported", [False, True])
@pytest.mark.parametrize("boundary", ["initial", "final", "readback"])
@pytest.mark.parametrize("change", ["page", "dock", "folder", "unavailable"])
def test_live_frame_drift_never_verifies_stale_coordinates(tmp_path, operation, reported, boundary, change):
    source = FrameSource(reported)
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("example.a", 1, 34))
    session._remember_gaps(session.require())
    backup = session._backup(session.require().to_state(), [], "synthetic", [{"example.a": 34}])
    before, backups = reviewed(session), session.backups()
    source.next_metrics = ({"homeScreenIconColumns": 4, "homeScreenIconRows": 6} if change == "page"
                           else None if change == "unavailable" else
                           {**source.metrics, "homeScreenIconDockMaxCount": 5} if change == "dock" else
                           {**source.metrics, "homeScreenIconFolderColumns": 4})
    source.transition_at = source.info_count + {"initial": 1, "final": 2, "readback": 3}[boundary]
    execute = lambda: apply(session) if operation == "apply" else run(session.restore(backup.name))
    if boundary == "readback":
        assert execute()["status"] == "unknown"
        assert len(source.payloads) == 1
        assert session._pending_path.exists()
    else:
        with pytest.raises(LayoutError, match="grid.*read|grid.*Read"):
            execute()
        assert source.payloads == []
        assert session.backups() == backups
        assert not session._pending_path.exists()
    assert reviewed(session) == before


@pytest.mark.parametrize("before,after", [(None, {"homeScreenIconColumns": 5, "homeScreenIconRows": 7}),
                                         ({"homeScreenIconColumns": 5, "homeScreenIconRows": 7}, None)])
def test_read_rejects_frame_transition_without_committing_snapshot(tmp_path, before, after):
    source = FrameSource()
    session = Session(source, tmp_path / "data")
    run(session.read())
    session.edit(lambda lay: lay.pin("example.a", 1, 0))
    saved = reviewed(session)
    source.metrics, source.next_metrics = before, after
    source.transition_at = source.info_count + 2
    with pytest.raises(LayoutError, match="grid.*read|grid.*Read"):
        run(session.read())
    assert reviewed(session) == saved


@pytest.mark.parametrize("metrics", [None, {}, {"homeScreenIconColumns": 4, "homeScreenIconRows": 6}])
def test_unchanged_effective_default_frame_can_apply_and_restore(tmp_path, metrics):
    source = FrameSource(False)
    source.metrics = metrics
    session = Session(source, tmp_path / "data")
    run(session.read())
    assert session.grid == Grid()
    session.edit(lambda lay: lay.pin("example.a", 1, 23))
    backup = session._backup(session.require().to_state(), [], "synthetic", [{"example.a": 23}])
    assert apply(session)["status"] == "verified"
    assert run(session.restore(backup.name))["status"] == "verified"
