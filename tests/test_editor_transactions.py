import asyncio
import json
import plistlib

import pytest
from starlette.requests import Request

from homescreen import config, server, ui, usage
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
    async def fetch(ids):
        assert ids == ["a", "b"]
        config.select_device("second")
        return {}
    first.icon_colors = fetch
    response = asyncio.run(ui.post_op(request({"op": "sort_color", "binding": binding,
                                               "args": {"keep_first_page": False}})))
    assert response.status_code == 409 and first.history()["undo"] == second.history()["undo"] == 0
    assert first.draft_path.read_bytes() == saved


@pytest.mark.parametrize("intervening", ["edit", "read", "switch"])
def test_mcp_color_sort_commits_only_to_its_original_selected_revision(pair, monkeypatch, intervening):
    first, second = pair
    monkeypatch.setattr(server, "session", config.session)
    expected = []
    async def analyze(*args):
        if intervening == "edit":
            first.edit(lambda layout: layout.move("b", "page:2"))
        elif intervening == "read":
            await first.read()
        else:
            config.select_device("second")
        expected.extend((s.draft_path.read_bytes(), s.history()) for s in pair)
        return {}
    monkeypatch.setattr(first, "icon_colors", analyze)
    result = asyncio.run(server.sort_by_color(keep_first_page=False))
    assert not result["ok"] and "changed" in result["error"]
    assert [(s.draft_path.read_bytes(), s.history()) for s in pair] == expected
    assert first.source.writes == second.source.writes == 0


def test_failed_device_selection_retains_persisted_and_displayed_session(pair, monkeypatch):
    first, _ = pair
    config.select_device("first")
    def full(*args, **kwargs):
        raise OSError("selector storage unavailable")
    monkeypatch.setattr(config, "_private_replace", full)
    response = asyncio.run(ui.post_device(request({"binding": first.binding, "udid": "second"})))
    assert response.status_code == 502 and json.loads(response.body)["view"]["binding"] == first.binding
    assert config.session() is first and config.LAST_DEVICE.read_text() == "first"


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


@pytest.mark.parametrize("switch", [False, True])
def test_usage_response_rechecks_selection_after_worker_adoption(pair, monkeypatch, switch):
    first, second = pair
    monkeypatch.setattr(usage, "load", lambda known, days: {"ok": True, "days": days, "opens": {"a": 3}})
    async def handoff(fn, *args, **kwargs):
        result = fn(*args, **kwargs)
        if switch:
            response = await ui.post_device(request({"binding": first.binding, "udid": "second"}))
            assert response.status_code == 200
        return result
    monkeypatch.setattr(ui.asyncio, "to_thread", handoff)
    response = asyncio.run(ui.post_op(request({"binding": first.binding, "op": "load_usage", "args": {"source": "mac"}})))
    assert response.status_code == (409 if switch else 200)
    body = json.loads(response.body)
    current = body["view"] if switch else body
    assert current["device_id"] == config.current_device() == ("second" if switch else "first")
    assert first.usage["opens"] == {"a": 3} and second.usage is None


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


@pytest.mark.parametrize("case", ["discovered", "selected", "no-details"])
def test_unavailable_discovery_retains_a_previously_seen_device_without_a_draft(pair, monkeypatch, case):
    from types import SimpleNamespace
    from pymobiledevice3 import lockdown, usbmux
    first, _ = pair
    monkeypatch.setattr(ui, "_names", {})
    async def found():
        return [SimpleNamespace(serial="new", connection_type="USB")]
    async def details(**kwargs):
        if case == "no-details":
            raise ConnectionError("details unavailable")
        return SimpleNamespace(all_values={"DeviceName": "New device"})
    async def failed():
        raise ConnectionError("discovery unavailable")
    monkeypatch.setattr(usbmux, "list_devices", found)
    monkeypatch.setattr(lockdown, "create_using_usbmux", details)
    normal = json.loads(asyncio.run(ui.get_devices(request({}))).body)
    assert any(row["udid"] == "new" and row["connected"] is True and not row["draft"] for row in normal["devices"])
    if case == "selected":
        source = MemorySource()
        source.udid = "new"
        config._sessions["new"] = Session(source, first.dir, device="new")
        response = asyncio.run(ui.post_device(request({"binding": first.binding, "udid": "new"})))
        assert response.status_code == 200
    monkeypatch.setattr(usbmux, "list_devices", failed)
    unavailable = json.loads(asyncio.run(ui.get_devices(request({}))).body)
    assert unavailable["discovery"] == "unavailable"
    new = next(row for row in unavailable["devices"] if row["udid"] == "new")
    assert new["connected"] is None and new["draft"] is False
    assert unavailable["current"] == ("new" if case == "selected" else "first")
    monkeypatch.setattr(usbmux, "list_devices", found)
    recovered = json.loads(asyncio.run(ui.get_devices(request({}))).body)
    assert recovered["discovery"] == "complete" and any(row["udid"] == "new" and row["connected"] is True for row in recovered["devices"])
