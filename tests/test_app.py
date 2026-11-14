import asyncio
import importlib
import plistlib

import pytest

from homescreen import organize
from homescreen.device import FileSource
from homescreen.model import Layout, LayoutError
from homescreen.session import Session

from fake_phone import build


@pytest.fixture
def dict_phone(tmp_path):
    path = tmp_path / "phone.plist"
    colors = build(path, as_dict=True)
    return path, colors


def session_for(path, tmp_path):
    s = Session(FileSource(path), tmp_path / "data")
    asyncio.run(s.read())
    return s


def test_edit_is_atomic_and_undoable(dict_phone, tmp_path):
    s = session_for(dict_phone[0], tmp_path)
    before = s.require().token()
    first = s.require().pages[2][1].id
    with pytest.raises(LayoutError):
        # first move succeeds, second fails: the whole step must roll back
        w = next(n.id for n in s.require().nodes.values() if n.kind == "widget")
        s.edit(lambda lay: (lay.move(first, "library"), lay.move(w, "library")))
    assert s.require().token() == before and s.history() == {"undo": 0, "redo": 0}
    s.edit(lambda lay: lay.move(first, "library"))
    assert s.require().diff()["to_app_library"] == 1
    assert s.undo() and s.require().token() == before
    assert s.redo() and s.require().diff()["to_app_library"] == 1


def test_page_ops_and_dissolve(dict_phone, tmp_path):
    s = session_for(dict_phone[0], tmp_path)
    lay = s.require()
    n = len(lay.pages)
    lay.insert_page(2)
    assert lay.pages[1] == [] and len(lay.pages) == n + 1
    lay.move_page(2, n + 1)
    assert lay.pages[-1] == []
    lay.delete_page(n + 1)
    folder = next(x for x in lay.pages[1] if x.kind == "folder")
    kids = [c.id for p in folder.children for c in p]
    lay.dissolve_folder(folder.id)
    assert [x.id for x in lay.pages[1]][1:1 + len(kids)] == kids
    in_lib = len(lay.library)
    page = next(i for i, p in enumerate(lay.pages, 1) if p and all(x.kind != "widget" for x in p))
    apps_on_page = sum(len([c for q in x.children for c in q]) if x.kind == "folder" else 1 for x in lay.pages[page - 1])
    lay.delete_page(page)
    assert len(lay.library) == in_lib + apps_on_page
    with pytest.raises(LayoutError, match="widgets"):
        lay.delete_page(1)


def test_sorts_keep_items_and_page_one(dict_phone, tmp_path):
    path, color_of = dict_phone
    s = session_for(path, tmp_path)
    page1 = [x.id for x in s.require().pages[0]]
    colors = asyncio.run(s.icon_colors())
    s.edit(lambda lay: organize.by_color(lay, colors))
    lay = s.require()
    assert lay.validate() == [] and [x.id for x in lay.pages[0]] == page1
    loose = [x.id for p in lay.pages[1:] for x in p if x.kind == "app"]
    fams = [color_of[i] for i in loose]
    order = [f for f in organize.COLOR_ORDER if f in fams]
    assert fams == sorted(fams, key=order.index)
    s.edit(lambda lay: organize.alphabetical(lay))
    assert s.require().validate() == []
    s.edit(lambda lay: organize.sort_folder_contents(lay))
    assert s.require().validate() == []


def test_into_folders(dict_phone, tmp_path):
    s = session_for(dict_phone[0], tmp_path)
    lay = s.require()
    social = next(x for p in lay.pages for x in p if x.kind == "folder" and x.name == "Social")
    loose = [x.id for p in lay.pages[2:] for x in p if x.kind == "app"]
    res = s.edit(lambda lay: organize.into_folders(lay, {"social": loose[:2], "Brand New": loose[2:5],
                                                         "Lonely": loose[5:6]}))
    lay = s.require()
    assert res["count"] == 5 and set(res["filed"]) == {"Social", "Brand New"}
    assert set(loose[:2]) <= {c.id for p in social.children for c in p}
    assert all(len(p) <= 9 for p in social.children)
    new = next(x for p in lay.pages for x in p if x.kind == "folder" and x.name == "Brand New")
    assert [c.id for c in new.children[0]] == loose[2:5]
    assert lay.validate() == []


def test_app_api_guards_and_ops(dict_phone, tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setenv("HOMESCREEN_FIXTURE", str(dict_phone[0]))
    monkeypatch.setenv("HOMESCREEN_DATA", str(tmp_path / "appdata"))
    import homescreen.config as config
    import homescreen.ui as ui
    importlib.reload(config)
    importlib.reload(ui)
    c = TestClient(ui.app, base_url="http://127.0.0.1:8765")
    assert c.get("/").status_code == 200 and ui.TOKEN not in c.get("/").text
    assert c.post("/api/read", json={}).status_code == 401
    assert TestClient(ui.app, base_url="http://evil.example").get("/api/state").status_code == 403
    h = {"X-Token": ui.TOKEN}
    v = c.post("/api/read", json={}, headers=h).json()
    assert v["loaded"] and v["library"] is not None
    app_id = next(x["id"] for x in v["pages"][2] if x["kind"] == "app")
    v = c.post("/api/op", json={"op": "move", "args": {"id": app_id, "target": "library"}}, headers=h).json()
    assert v["diff"]["to_app_library"] == 1 and v["history"]["undo"] == 1
    wid = next(x["id"] for p in v["pages"] for x in p if x["kind"] == "widget")
    bad = c.post("/api/op", json={"op": "move", "args": {"id": wid, "target": "dock"}}, headers=h)
    assert bad.status_code == 400 and "dock" in bad.json()["error"]
    r = c.post("/api/apply", json={"token": v["diff"]["apply_token"]}, headers=h).json()
    assert r["result"]["ok"]
    assert c.get("/api/backups", headers=h).json()["backups"]
    assert c.get("/icon", params={"id": app_id}, headers=h).status_code == 200


def test_screen_time_reader_and_optimizer(tmp_path):
    import time
    from homescreen import usage
    from fake_phone import app as mkapp
    # two synced "devices": the phone (opens of its own apps) and an unrelated Mac
    now_us = int((time.time() - usage.MAC_EPOCH) * 1e6)
    old_us = int((time.time() - 60 * 86400 - usage.MAC_EPOCH) * 1e6)
    phone, mac, other = (tmp_path / "remote" / n for n in ("PHONE", "MAC", "OLD"))
    for d in (phone, mac, other):
        d.mkdir(parents=True)
    rec = lambda b, n: (b"SEGB" + (b"\x00\x0a" + b.encode() + b"J") * (2 * n))
    (phone / str(now_us)).write_bytes(rec("com.a.hot", 50) + rec("com.a.warm", 10) + rec("com.a.cold", 1))
    (mac / str(now_us)).write_bytes(rec("com.apple.dt.Xcode", 99))
    (other / str(old_us)).write_bytes(rec("com.a.cold", 500))  # older than 30 days: ignored
    known = {"com.a.hot", "com.a.warm", "com.a.cold", "com.a.never"}
    u = usage.load(known, 30, tmp_path / "remote")
    assert u["ok"] and u["opens"] == {"com.a.hot": 50, "com.a.warm": 10, "com.a.cold": 1}
    assert [d["id"] for d in u["devices"]] == ["PHONE"]
    # hot app buried on page 3, cold app on page 1
    state = [[], [mkapp("com.a.cold", "Cold")], [mkapp("com.a.never", "Never")], [mkapp("com.a.hot", "Hot"), mkapp("com.a.warm", "Warm")]]
    lay = Layout(state, library=[])
    assert usage.swipes(lay, u["opens"]) == 50 * 2 + 10 * 2
    res = usage.optimize(lay, u["opens"])
    assert res["swipes_after"] == 0 and res["swipes_before"] == 120
    assert [n.id for n in lay.pages[0]][:3] == ["com.a.hot", "com.a.warm", "com.a.cold"]
    assert lay.validate() == []
    assert usage.load(known, 30, tmp_path / "missing")["ok"] is False


def test_bank_and_swap(dict_phone, tmp_path):
    s = session_for(dict_phone[0], tmp_path)
    lay = s.require()
    wpage, w = next((i, x.id) for i, p in enumerate(lay.pages, 1) for x in p if x.kind == "widget")
    a, b = [x.id for x in lay.pages[3] if x.kind == "app"][:2]
    s.edit(lambda lay: (lay.move(w, "bank"), lay.move(a, "bank")))
    assert any("App Bank" in p for p in s.require().validate())
    # the bank survives undo/redo and a restart of the session
    s.undo(); s.redo()
    s2 = Session(FileSource(dict_phone[0]), tmp_path / "data")
    assert [n.id for n in s2.require().bank] == [w, a]
    with pytest.raises(LayoutError):
        s2.edit(lambda lay: lay.move(w, next(n.id for n in lay.nodes.values() if n.kind == "folder")))
    s2.edit(lambda lay: (lay.move(w, f"page:{wpage}", 0), lay.move(a, "page:4", 0)))
    assert s2.require().validate() == []
    s2.edit(lambda lay: lay.swap(a, b))
    ids = [x.id for x in s2.require().pages[3]]
    assert ids.index(b) < ids.index(a)
    with pytest.raises(LayoutError, match="widgets"):
        s2.edit(lambda lay: lay.swap(w, next(x.id for x in lay.dock)))


def test_widget_configuration_ids_survive_writes(tmp_path):
    """A configured widget's element id must go out as uniqueIdentifier, and an id iOS dropped
    must be restored from an earlier read."""
    from homescreen.device import to_root_archive
    from fake_phone import app as mkapp
    w = {"bundleIdentifier": "dk.simonbs.Scriptable.ScriptableWidget", "containerBundleIdentifier": "dk.simonbs.Scriptable",
         "displayIdentifier": "W-DISPLAY", "elementType": "widget", "gridSize": "small", "iconLists": [],
         "iconType": "custom", "userSelectedElementIdentifier": "ELEMENT-1"}
    out = to_root_archive([[], [w, mkapp("com.a.b", "B")]], [])
    sent = out["iconLists"][0][0]
    assert sent["uniqueIdentifier"] == "ELEMENT-1" and "iconLists" not in sent
    stack = {**w, "elements": [{"uniqueIdentifier": "E1"}, {"uniqueIdentifier": "E2"}]}
    assert "uniqueIdentifier" not in to_root_archive([[], [stack]], [])["iconLists"][0][0]

    path = tmp_path / "phone.plist"
    path.write_bytes(plistlib.dumps([[], [w, mkapp("com.a.b", "B")]]))
    s = Session(FileSource(path), tmp_path / "data")
    asyncio.run(s.read())                       # remembers ELEMENT-1
    dropped = {k: v for k, v in w.items() if k != "userSelectedElementIdentifier"}
    path.write_bytes(plistlib.dumps([[], [dropped, mkapp("com.a.b", "B")]]))   # iOS dropped it
    asyncio.run(s.read())
    res = asyncio.run(s.apply(s.require().token()))
    assert res["ok"]
    written = plistlib.loads(path.read_bytes())
    assert written[1][0]["userSelectedElementIdentifier"] == "ELEMENT-1"


# ---- gaps ---------------------------------------------------------------------------------

from pathlib import Path
IOS27 = Path(__file__).parent / "fixtures" / "ios27.2-sim-IconState.plist"

def _gap_layout():
    from fake_phone import app as mkapp, widget
    page = [widget("Weather", "small")] + [mkapp(f"com.g.a{i}", f"A{i}") for i in range(6)]
    return [[mkapp("com.g.dock", "Dock")], page]


def test_grid_placement_and_pins():
    lay = Layout(_gap_layout(), library=[])
    pos, problems = lay.place(lay.pages[0])
    w = next(n.id for n in lay.pages[0] if n.kind == "widget")
    assert problems == [] and pos[w] == (0, 0, 2, 2)
    assert pos["com.g.a0"] == (0, 2, 1, 1) and pos["com.g.a2"] == (1, 2, 1, 1) and pos["com.g.a4"] == (2, 0, 1, 1)
    lay.pin("com.g.a5", 1, 23)                       # bottom-right corner
    pos, problems = lay.place(lay.pages[0])
    assert problems == [] and pos["com.g.a5"] == (5, 3, 1, 1)
    lay.pin("com.g.a4", 1, 0)                        # a pinned app wins; the widget flows on
    pos = lay.place(lay.pages[0])[0]
    assert pos["com.g.a4"] == (0, 0, 1, 1) and pos[w][1] == 2
    with pytest.raises(LayoutError):
        lay.pin("com.g.a3", 1, 0)                    # two pins in one cell
    lay.unpin("com.g.a4")
    lay.pin(w, 1, 2)                                 # widget to the right half
    assert lay.place(lay.pages[0])[0][w] == (0, 2, 2, 2)
    lay.move("com.g.a5", "page:2")                   # moving drops the pin
    assert "com.g.a5" not in lay.pins
    lay.swap("com.g.a0", "com.g.a1")
    assert lay.validate() == []
    assert lay.diff()["gaps_changed"] == 1           # the widget pin


def test_gaps_round_trip_dict_format(tmp_path):
    from homescreen.device import to_root_archive, disk_pins
    raw = plistlib.loads(IOS27.read_bytes())
    path = tmp_path / "IconState.plist"
    path.write_bytes(plistlib.dumps(raw))
    s = Session(FileSource(path), tmp_path / "data")
    asyncio.run(s.read())
    s.edit(lambda lay: lay.pin("com.apple.MobileAddressBook", 2, 13))
    tok = s.require().token()
    s2 = Session(FileSource(path), tmp_path / "data")          # draft keeps the gap
    assert s2.require().pins == {"com.apple.MobileAddressBook": 13} and s2.require().token() == tok
    s.undo()
    assert s.require().pins == {}
    s.redo()
    assert asyncio.run(s.apply(s.require().token()))["ok"]
    written = plistlib.loads(path.read_bytes())
    assert disk_pins(written) == {"com.apple.MobileAddressBook": 13}
    asyncio.run(s.read())
    assert s.require().pins == {"com.apple.MobileAddressBook": 13}   # read back from the file
    # the phone payload carries page ids and listMetadata only when there are gaps
    lay = s.require()
    from homescreen.session import pins_by_page
    out = to_root_archive(lay.to_state(), lay.to_library() or [], pins_by_page(lay))
    pid = out["listUniqueIdentifiers"][1]
    assert out["listMetadata"][pid]["fixedLocations"] == {"com.apple.MobileAddressBook": 13}
    assert "listMetadata" not in to_root_archive(lay.to_state(), [], [{}, {}])


def test_gaps_remembered_when_device_cannot_report_them(tmp_path):
    """A real phone never reports gaps; gaps set in Homescreen must survive a re-read."""
    path = tmp_path / "phone.plist"
    path.write_bytes(plistlib.dumps(_gap_layout()))           # list format: no gap info
    s = Session(FileSource(path), tmp_path / "data")
    asyncio.run(s.read())
    assert s.gaps_readable is False
    s.edit(lambda lay: lay.pin("com.g.a5", 1, 23))
    assert asyncio.run(s.apply(s.require().token()))["ok"]
    asyncio.run(s.read())                                     # phone reports no gaps...
    assert s.require().pins == {"com.g.a5": 23}               # ...but Homescreen remembers it
    # a gap that no longer fits (page changed on the phone) is dropped, not forced
    page = _gap_layout()
    page[1] = page[1] + [{"bundleIdentifier": f"com.g.x{i}", "displayIdentifier": f"com.g.x{i}", "displayName": "X"} for i in range(13)]
    path.write_bytes(plistlib.dumps(page))
    asyncio.run(s.read())
    assert s.require().validate() == []


class HiddenWebClipPhone:
    """Like a real phone: the App Library is worked out from installed apps (so web clips in it
    are never reported), and a write puts any web clip it wasn't told about into a free cell."""
    name = "hidden-webclip-phone"
    pins = None

    def __init__(self, state, hidden):
        self.state, self.hidden, self.writes, self.apps = state, hidden, 0, []

    async def info(self):
        return {"DeviceName": "fake", "UniqueDeviceID": self.name}

    async def read(self):
        return [list(p) for p in self.state], list(self.apps)  # installed apps only

    async def write(self, state, library, pins=None, grid=None):
        self.writes += 1
        listed = {x.get("displayIdentifier") for x in library}
        state = [list(p) for p in state]
        for w in [w for w in self.hidden if w["displayIdentifier"] not in listed]:
            self.hidden.remove(w)
            state[-1].append(w)  # first free cell
        for x in library:
            if x.get("webClipURL") and x not in self.hidden:
                self.hidden.append({k: x[k] for k in ("displayIdentifier", "displayName", "webClipURL")})
        self.apps = [x for x in library if not x.get("webClipURL")]
        self.state = [[x for x in p if x.get("displayIdentifier") not in listed] for p in state]


def test_hidden_webclips_are_learned_and_kept_in_app_library(tmp_path):
    a = lambda i: {"bundleIdentifier": f"com.w.a{i}", "displayIdentifier": f"com.w.a{i}", "displayName": f"A{i}"}
    clip = {"displayIdentifier": "C5066B8C", "displayName": "Odds", "webClipURL": "/"}
    phone = HiddenWebClipPhone([[a(0)], [a(i) for i in range(1, 6)]], [dict(clip)])
    s = Session(phone, tmp_path / "data")
    asyncio.run(s.read())
    s.edit(lambda lay: lay.move("com.w.a5", "library"))
    res = asyncio.run(s.apply(s.require().token()))
    assert not res["ok"] and res["hidden_webclips"] == ["Odds"]  # iOS surfaced it; reported, not retried
    assert phone.writes == 1
    # A mismatching readback blocks further writes until an explicit fresh read.
    asyncio.run(s.read())
    # it's remembered: send it to the App Library, and later writes keep it there
    s.edit(lambda lay: lay.move("C5066B8C", "library"))
    res = asyncio.run(s.apply(s.require().token()))
    assert res["ok"], res
    asyncio.run(s.read())
    assert "C5066B8C" in [n.id for n in s.require().library]
    s.edit(lambda lay: lay.move("com.w.a4", "library"))  # frees another cell
    assert asyncio.run(s.apply(s.require().token()))["ok"]
    assert all(x["displayIdentifier"] != "C5066B8C" for p in phone.state for x in p)


def test_ipad_grid_from_icon_metrics():
    from homescreen.model import Grid
    g = Grid.from_metrics({"homeScreenIconColumns": 5, "homeScreenIconRows": 6, "homeScreenIconDockMaxCount": 26,
                           "homeScreenIconFolderColumns": 4, "homeScreenIconFolderRows": 4})
    assert (g.cols, g.rows, g.slots, g.dock, g.folder) == (5, 6, 30, 26, 16)
    assert Grid.from_metrics(None) == Grid()  # no metrics: iPhone
    a = lambda i: {"bundleIdentifier": f"com.p.a{i}", "displayIdentifier": f"com.p.a{i}", "displayName": f"A{i}"}
    big = {"iconType": "custom", "gridSize": "large", "displayName": "Big", "uniqueIdentifier": "W1"}
    state = [[a(i) for i in range(7)], [big] + [a(i) for i in range(7, 21)]]  # 16 + 14 = 30 slots
    assert Layout(state).validate()  # too full for an iPhone page
    lay = Layout(state, grid=g)
    assert lay.validate() == [] and lay.page_slots == 30
    pos, problems = lay.place(lay.pages[0])
    assert not problems and pos["widget:W1"] == (0, 0, 4, 4) and pos["com.p.a7"] == (0, 4, 1, 1)  # 5th column
    lay.create_folder("F", [f"com.p.a{i}" for i in range(7, 21)], "page:1")
    lay.refold(lay.pages[0][-1])
    assert [len(p) for p in lay.pages[0][-1].children] == [14]  # 16 per folder page


def test_usage_from_memory_reports(tmp_path):
    """Days in memory rank apps; apps iOS runs in the background, or keeps resident, count less."""
    import json
    from homescreen.usage import from_memory_reports
    apps = {"com.g.game": {"CFBundleExecutable": "Game"},
            "com.g.chat": {"CFBundleExecutable": "Chat", "UIBackgroundModes": ["voip"]},
            "com.g.resident": {"CFBundleExecutable": "Resident"},
            "com.g.rare": {"CFBundleExecutable": "Rare"}}
    for day in range(1, 8):
        procs = [{"name": "Game", "states": ["suspended"]}, {"name": "Chat", "states": ["suspended"]},
                 {"name": "Resident", "states": ["suspended"]}, {"name": "launchd", "states": ["daemon"]}]
        if day == 1:
            procs.append({"name": "Rare", "states": ["frontmost"]})
        if day in (5, 6, 7):
            procs = procs[1:]  # the game wasn't open those days
        if day in (3, 4, 5):
            procs = [x for x in procs if x["name"] != "Chat"]
        (tmp_path / f"JetsamEvent-{day}.ips").write_text(
            '{"bug_type":"298"}\n' + json.dumps({"date": f"2026-10-0{day} 10:00:00", "processes": procs}))
    r = from_memory_reports(sorted(tmp_path.glob("*.ips")), apps, set(apps))
    o = r["opens"]
    assert r["ok"] and r["days"] == 7
    assert o["com.g.game"] > o["com.g.chat"] > o["com.g.rare"]   # 4 days > 4 days halved > 1 day
    assert o["com.g.resident"] < o["com.g.game"]                  # in memory every day: discounted
    assert set(r["background"]) == {"com.g.chat", "com.g.resident"}
    assert not from_memory_reports([], apps, set(apps))["ok"]
