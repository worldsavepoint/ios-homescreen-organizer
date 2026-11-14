import asyncio
import json
import plistlib

import pytest
from starlette.requests import Request

from homescreen import config, ui
from homescreen.session import Session
from test_integrity import MemorySource


def request(body):
    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/api/op", "query_string": b"",
                    "headers": [(b"host", b"127.0.0.1"), (b"x-token", ui.TOKEN.encode())]}, receive)


def view_binding(session):
    return ui.view(session).get("binding", {"device": session.device_id, "revision": "initial"})


@pytest.fixture
def pair(tmp_path, monkeypatch):
    sessions = {}
    for udid in ("first", "second"):
        source = MemorySource()
        source.udid = udid
        s = Session(source, tmp_path / "data", device=udid)
        asyncio.run(s.read())
        sessions[udid] = s
    monkeypatch.setattr(config, "_sessions", sessions)
    monkeypatch.setattr(config, "_switching", True)
    monkeypatch.setattr(config, "_current", "first")
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "LAST_DEVICE", tmp_path / "data" / "last-device.txt")
    monkeypatch.setattr(config, "make_source", lambda: pytest.fail("unexpected source"))
    monkeypatch.setattr(config, "PhoneSource", lambda *args: pytest.fail("unexpected phone"))
    monkeypatch.setattr(ui, "session", config.session)
    return sessions["first"], sessions["second"]


@pytest.mark.parametrize("operation", ["move", "undo", "redo", "reset", "read", "device"])
def test_stale_tab_cannot_replace_or_edit_selected_device(pair, operation):
    first, second = pair
    binding = view_binding(first)
    config.select_device("second")
    second.edit(lambda lay: lay.move("b", "dock"))
    before = second.draft_path.read_bytes(), second.history(), second.source.reads
    handler = {"read": ui.post_read, "device": ui.post_device}.get(operation, ui.post_op)
    body = {"binding": binding, "op": operation, "args": {"id": "a", "target": "dock"}, "udid": "first"}
    result = asyncio.run(handler(request(body)))
    assert result.status_code == 409
    assert config.current_device() == "second"
    assert before == (second.draft_path.read_bytes(), second.history(), second.source.reads)


def test_undo_to_identical_state_does_not_revive_stale_binding(pair):
    session, _ = pair
    stale = view_binding(session)
    session.edit(lambda lay: lay.swap("a", "b"))
    session.undo()
    result = asyncio.run(ui.post_op(request({"binding": stale, "op": "move", "args": {"id": "a", "target": "dock"}})))
    assert result.status_code == 409
    assert not session.require().dock


def test_restore_confirmation_requires_same_backup_content_device_and_draft(pair):
    first, second = pair
    path = first._backup(first.require().to_state(), None, "saved")
    binding = view_binding(first)
    missing = asyncio.run(ui.post_restore(request({"name": path.name, "binding": binding})))
    assert missing.status_code == 409 and first.source.writes == 0
    handle = asyncio.run(ui.post_backup_review(request({"name": path.name, "binding": binding})))
    assert handle.status_code == 200
    body = {"name": path.name, "binding": binding, "review": json.loads(handle.body)["review"]}
    old = path.read_bytes()
    changed = plistlib.loads(old)
    changed["state"][1].reverse()
    path.write_bytes(plistlib.dumps(changed))
    assert asyncio.run(ui.post_restore(request(body))).status_code == 409
    path.write_bytes(old)
    first.edit(lambda lay: lay.swap("a", "b"))
    assert asyncio.run(ui.post_restore(request(body))).status_code == 409
    config.select_device("second")
    assert asyncio.run(ui.post_restore(request(body))).status_code == 409
    assert first.source.writes == second.source.writes == 0
    config.select_device("first")
    body["binding"] = view_binding(first)
    handle = asyncio.run(ui.post_backup_review(request(body)))
    body["review"] = json.loads(handle.body)["review"]
    assert json.loads(asyncio.run(ui.post_restore(request(body))).body)["result"]["ok"]


def test_suspended_color_operation_rechecks_binding_before_edit(pair):
    first, second = pair
    binding = view_binding(first)
    saved = first.draft_path.read_bytes()
    async def fetch():
        config.select_device("second")
        return {}
    first.icon_colors = fetch
    response = asyncio.run(ui.post_op(request({"op": "sort_color", "binding": binding, "args": {}})))
    assert response.status_code == 409 and first.history()["undo"] == second.history()["undo"] == 0
    assert first.draft_path.read_bytes() == saved


def test_reviewed_read_checks_shared_committed_draft_before_source_access(pair):
    first, _ = pair
    binding = view_binding(first)
    another = Session(first.source, first.dir, device=first.device_id)
    another.edit(lambda lay: lay.move("b", "dock"))
    prior, reads = first.draft_path.read_bytes(), first.source.reads
    result = asyncio.run(ui.post_read(request({"binding": binding})))
    assert result.status_code == 409
    assert first.draft_path.read_bytes() == prior and first.source.reads == reads


@pytest.mark.parametrize("action", ["read", "device"])
def test_state_refresh_reopens_an_external_commit_without_reviving_the_old_view(pair, action):
    first, _ = pair
    first.edit(lambda lay: lay.move("a", "dock"))
    old = view_binding(first)
    another = Session(first.source, first.dir, device=first.device_id)
    another.edit(lambda lay: lay.move("b", "dock"))
    saved, reads = first.draft_path.read_bytes(), first.source.reads
    assert asyncio.run(ui.post_read(request({"binding": old}))).status_code == 409
    state = asyncio.run(ui.get_state(request({})))
    current = json.loads(state.body)
    assert state.status_code == 200 and current["binding"] == another.binding
    assert [row["id"] for row in current["dock"]] == ["a", "b"]
    assert first.history() == {"undo": 0, "redo": 0}
    assert first.draft_path.read_bytes() == saved and first.source.reads == reads
    assert asyncio.run(ui.post_read(request({"binding": old}))).status_code == 409
    handler = ui.post_read if action == "read" else ui.post_device
    assert asyncio.run(handler(request({"binding": current["binding"], "udid": "second"}))).status_code == 200


def test_state_refresh_exposes_recoverable_external_draft_damage(pair):
    first, _ = pair
    first.draft_path.write_bytes(b"bplist00incomplete")
    current = json.loads(asyncio.run(ui.get_state(request({}))).body)
    assert not current["loaded"] and current["draft_error"]
    assert first.draft_path.read_bytes() == b"bplist00incomplete"
    assert asyncio.run(ui.post_read(request({"binding": current["binding"]}))).status_code == 200


def test_discovery_failure_retains_known_devices_with_unknown_status(pair, monkeypatch):
    from pymobiledevice3 import usbmux
    async def failed():
        raise ConnectionError("discovery unavailable")
    async def empty():
        return []
    monkeypatch.setattr(usbmux, "list_devices", failed)
    result = json.loads(asyncio.run(ui.get_devices(request({}))).body)
    assert result.get("discovery") == "unavailable"
    assert {row["udid"] for row in result["devices"]} == {"first", "second"}
    assert all(row["connected"] is None for row in result["devices"])
    monkeypatch.setattr(usbmux, "list_devices", empty)
    result = json.loads(asyncio.run(ui.get_devices(request({}))).body)
    assert result["discovery"] == "complete" and all(row["connected"] is False for row in result["devices"])
