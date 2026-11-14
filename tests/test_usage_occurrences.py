import asyncio

import pytest

from fake_phone import app
from homescreen import organize, server, ui, usage
from homescreen.device import PhoneSource
from homescreen.model import LayoutError
from homescreen.session import Session
from test_integrity import MemorySource, fake_phone_transport


def test_bundle_lookup_and_search_visit_each_placement(tmp_path, monkeypatch):
    source = MemorySource()
    source.state = [[], [app("sample.shared", "One"), app("sample.shared", "Two")]]
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    monkeypatch.setattr(server, "session", lambda: session)
    matches = server.find_apps("sample.shared")
    assert {row["id"] for row in matches} == {"sample.shared", "app:1"}
    seen = []
    def lookup(ids, cache):
        seen.extend(ids)
        return {"sample.shared": "Music"}
    monkeypatch.setattr(organize, "fetch_categories", lookup)
    assert server.app_store_categories() == {"sample.shared": "Music"} and seen == ["sample.shared"]
    assert organize.category_groups(session.require(), {"sample.shared": "Music"}, False) == {"Music": ["sample.shared", "app:1"]}


def test_reports_cost_name_and_position_of_the_same_nearest_copy(tmp_path):
    source = MemorySource()
    source.state = [[app("a", "Dock")], [app("keep", "Keep")], [app("a", "Later")]]
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    session.usage = {"ok": True, "days": 7, "opens": {"a": 10}}
    for move, expected in [(False, "Dock"), (True, "Later")]:
        if move:
            session.edit(lambda l: l.swap("a", "app:1"))
        result = ui.view(session)["usage"]
        assert result["now"] == 0 and result["top"] == usage.top_apps(session.require(), {"a": 10})
        assert result["top"] == [{"id": "a", "name": expected, "opens": 10, "where": "dock #1"}]


def test_usage_source_selection_happens_before_any_provider(tmp_path, monkeypatch):
    session = Session(MemorySource(), tmp_path)
    asyncio.run(session.read())
    monkeypatch.setattr(server, "session", lambda: session)
    monkeypatch.setattr(session, "load_usage", lambda *args: pytest.fail("unexpected Mac read"))
    monkeypatch.setattr(session, "load_device_usage", lambda *args: pytest.fail("unexpected device read"))
    result = asyncio.run(server.screen_time(source="devcie"))
    assert not result["ok"] and "source" in result["error"]


def test_device_change_clears_cached_usage_and_rejects_previous_inflight_load(tmp_path):
    source = MemorySource()
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    session.usage = {"ok": True, "days": 5, "opens": {"a": 8}}
    async def reports(path):
        source.udid = "next"
        await session.read()
        return []
    async def installed():
        return {}
    source.memory_reports = reports
    source.installed_apps = installed
    with pytest.raises(LayoutError):
        asyncio.run(session.load_device_usage())
    assert session.device_id == "next" and session.usage is None


@pytest.mark.parametrize("identity", ["synthetic-a", "synthetic-b"])
def test_reopened_usage_checks_the_live_phone_before_accessing_reports(tmp_path, monkeypatch, fake_phone_transport, identity):
    from pymobiledevice3.services import crash_reports, installation_proxy
    control = fake_phone_transport
    control.active = identity
    prior = MemorySource()
    prior.udid = "synthetic-a"
    session = Session(prior, tmp_path)
    asyncio.run(session.read())
    phone = PhoneSource()
    reopened = Session(phone, tmp_path)
    providers = []
    class Provider:
        def __init__(self, lockdown):
            self.identity = lockdown.identity
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def pull(self, out, **kwargs):
            from pathlib import Path
            providers.append(("reports", self.identity))
            (Path(out) / "JetsamEvent-fixture.ips").write_text("{}")
        async def get_apps(self, **kwargs):
            providers.append(("apps", self.identity))
            return {"a": {}}
    monkeypatch.setattr(crash_reports, "CrashReportsManager", Provider)
    monkeypatch.setattr(installation_proxy, "InstallationProxyService", Provider)
    monkeypatch.setattr(usage, "from_memory_reports", lambda *args: {"ok": True, "days": 4, "opens": {"a": 7}})
    monkeypatch.setattr(server, "session", lambda: reopened)
    result = asyncio.run(server.screen_time(source="device"))
    assert reopened.device_id == "synthetic-a" and phone._bound_udid == identity
    if identity == "synthetic-a":
        assert reopened.usage["opens"] == {"a": 7} and result["top"][0]["name"] == "A"
        assert providers == [("reports", identity), ("apps", identity)]
    else:
        assert result.get("ok") is False and "identity" in result["error"]
        assert reopened.usage is None and not providers
        assert not (tmp_path / "usage").exists()


@pytest.mark.parametrize("change_after", ["reports", "installed"])
def test_device_usage_rechecks_source_identity_before_adopting_reports(tmp_path, monkeypatch, change_after):
    source = MemorySource()
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    draft = session.draft_path.read_bytes()
    async def reports(path):
        if change_after == "reports":
            source.udid = "other-device"
        return []
    async def installed():
        if change_after == "installed":
            source.udid = "other-device"
        return {}
    source.memory_reports, source.installed_apps = reports, installed
    monkeypatch.setattr(usage, "from_memory_reports", lambda *args: {"ok": True, "days": 4, "opens": {"a": 7}})
    with pytest.raises(LayoutError, match="identity"):
        asyncio.run(session.load_device_usage())
    assert session.usage is None and session.draft_path.read_bytes() == draft
