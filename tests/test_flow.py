import asyncio
import plistlib
from pathlib import Path

import pytest

from homescreen.device import FileSource, disk_to_model, model_to_disk
from homescreen.model import Layout, LayoutError, structure
from homescreen.session import Session

from fake_phone import app, build


@pytest.fixture
def phone(tmp_path):
    path = tmp_path / "phone.plist"
    colors = build(path)
    return path, colors


def run(coro):
    return asyncio.run(coro)


def test_round_trip_is_byte_identical(phone):
    path, _ = phone
    raw = plistlib.loads(path.read_bytes())
    assert Layout(raw).to_state() == raw


def test_arrange_rejects_lossy_layouts(phone):
    lay = Layout(plistlib.loads(phone[0].read_bytes()))
    top = [n.id for p in lay.pages for n in p]
    with pytest.raises(LayoutError, match="left out"):
        lay.arrange([top[:-1]])
    with pytest.raises(LayoutError, match="twice"):
        lay.arrange([top + [top[0]]])


def test_capacity_and_folder_rules(phone):
    lay = Layout(plistlib.loads(phone[0].read_bytes()))
    top = [n.id for p in lay.pages for n in p]
    lay.arrange([top])  # everything on one page
    assert any("slots" in e for e in lay.validate())
    lay = Layout(plistlib.loads(phone[0].read_bytes()))
    w = next(n.id for n in lay.nodes.values() if n.kind == "widget")
    f = next(n.id for n in lay.nodes.values() if n.kind == "folder")
    with pytest.raises(LayoutError):
        lay.move(w, f)
    with pytest.raises(LayoutError):
        lay.move(w, "dock")


def test_color_sort_apply_verify_restore(phone, tmp_path):
    path, color_of = phone
    s = Session(FileSource(path), tmp_path / "data")
    run(s.read())
    lay = s.require()
    original = path.read_bytes()

    cols = run(s.icon_colors())
    sample = [k for k in cols if k in color_of]
    agree = sum(cols[k]["family"] == color_of[k] for k in sample)
    assert agree == len(sample), [(k, cols[k], color_of[k]) for k in sample if cols[k]["family"] != color_of[k]]

    # rainbow sort of loose apps; folders and widgets keep their spot at the front
    order = ["red", "orange", "yellow", "green", "blue", "purple", "white", "black"]
    top = [n for p in lay.pages for n in p]
    fixed = [n.id for n in top if n.kind != "app"]
    loose = sorted((n.id for n in top if n.kind == "app"), key=lambda i: order.index(cols[i]["family"]))
    items = fixed + loose
    pages, cur, used = [], [], 0
    for i in items:
        sl = lay.nodes[i].slots
        if used + sl > 24:
            pages.append(cur)
            cur, used = [], 0
        cur.append(i)
        used += sl
    pages.append(cur)
    lay.arrange(pages)
    s.edited()
    d = lay.diff()
    assert d["problems"] == [] and d["items_moved"] > 100

    with pytest.raises(LayoutError, match="token"):
        run(s.apply("apply-wrong"))
    res = run(s.apply(d["apply_token"]))
    assert res["ok"], res
    written = plistlib.loads(path.read_bytes())
    assert structure(written) == structure(lay.to_state())
    # unknown keys survived
    assert all(a.get("someFutureKey") == {"nested": [1, 2, 3]} for a in written[0])

    # restore the pre-apply backup
    backup = [b for b in s.backups() if "before-apply" in b][0]
    res = run(s.restore(backup))
    assert res["ok"]
    assert plistlib.loads(path.read_bytes()) == plistlib.loads(original)


def test_apply_refuses_when_phone_changed(phone, tmp_path):
    path, _ = phone
    s = Session(FileSource(path), tmp_path / "data")
    run(s.read())
    lay = s.require()
    lay.move(lay.pages[0][1].id, f"page:{len(lay.pages)}", 0)
    s.edited()
    assert lay.validate() == []
    # user moves something on the phone meanwhile
    raw = plistlib.loads(path.read_bytes())
    raw[-1].append(raw[-2].pop())
    path.write_bytes(plistlib.dumps(raw))
    with pytest.raises(LayoutError, match="changed since"):
        run(s.apply(lay.token()))


def test_draft_survives_restart(phone, tmp_path):
    path, _ = phone
    s = Session(FileSource(path), tmp_path / "data")
    run(s.read())
    fid = s.require().create_folder("Mine", [s.require().pages[0][1].id], "page:1", 0)
    s.edited()
    tok = s.require().token()
    s2 = Session(FileSource(path), tmp_path / "data")
    assert s2.require().token() == tok
    assert s2.require().diff()["items_moved"] >= 1


class LossySource(FileSource):
    """Write raises after landing, like a dropped USB response."""

    async def write(self, state, library, pins=None, grid=None):
        await super().write(state, library, pins, grid)
        raise ConnectionError("response lost")


def test_lost_response_is_not_retried(phone, tmp_path):
    path, _ = phone
    s = Session(LossySource(path), tmp_path / "data")
    run(s.read())
    lay = s.require()
    lay.move(lay.pages[0][1].id, "page:3", 0)
    s.edited()
    res = run(s.apply(lay.token()))
    assert res["ok"] and "Readback result: matched" in res["error"]


def test_single_move_counts_once(phone):
    lay = Layout(plistlib.loads(phone[0].read_bytes()))
    lay.move(lay.pages[2][5].id, f"page:{len(lay.pages)}", 0)
    assert lay.diff()["items_moved"] == 1
    lay = Layout(plistlib.loads(phone[0].read_bytes()))
    a = lay.pages[2][1].id
    assert lay.nodes[a].kind == "app"
    lay.move(a, "page:3", 10)  # reorder within the page
    assert lay.diff()["items_moved"] == 1


IOS27 = Path(__file__).parent / "fixtures" / "ios27.2-sim-IconState.plist"


def test_ios27_dict_format_round_trips():
    raw = plistlib.loads(IOS27.read_bytes())
    state, library = disk_to_model(raw)
    lay = Layout(state, library=library)
    assert [n.id for n in lay.library] == ["com.apple.TVRemoteApp"]
    assert {n.kind for p in lay.pages for n in p} >= {"app", "widget", "folder"}
    back = model_to_disk(lay.to_state(), lay.to_library(), raw)
    assert back == raw


def test_library_needs_device_support(phone):
    lay = Layout(plistlib.loads(phone[0].read_bytes()))  # list format: no App Library
    with pytest.raises(LayoutError, match="App Library"):
        lay.move(lay.pages[0][1].id, "library")


def test_send_to_app_library_and_back(tmp_path):
    path = tmp_path / "IconState.plist"
    path.write_bytes(IOS27.read_bytes())
    s = Session(FileSource(path), tmp_path / "data")
    run(s.read())
    lay = s.require()
    lay.move("com.apple.news", "library")
    with pytest.raises(LayoutError, match="only apps"):
        lay.move(next(n.id for n in lay.nodes.values() if n.kind == "widget"), "library")
    s.edited()
    d = lay.diff()
    assert d["items_moved"] == 1 and d["to_app_library"] == 1 and d["problems"] == []
    assert run(s.apply(d["apply_token"]))["ok"]
    raw = plistlib.loads(path.read_bytes())
    assert raw["ignored"] == ["com.apple.TVRemoteApp", "com.apple.news"]
    assert "com.apple.news" not in raw["iconLists"][0]
    assert len(raw["listUniqueIdentifiers"]) == len(raw["iconLists"])

    # and back onto a new page 3
    lay = s.require()
    lay.move("com.apple.news", "page:3")
    s.edited()
    assert run(s.apply(lay.token()))["ok"]
    raw = plistlib.loads(path.read_bytes())
    assert raw["iconLists"][2] == ["com.apple.news"] and "com.apple.news" not in raw["ignored"]
    assert len(raw["listUniqueIdentifiers"]) == 3


def test_arrange_with_library(tmp_path):
    raw = plistlib.loads(IOS27.read_bytes())
    state, library = disk_to_model(raw)
    lay = Layout(state, library=library)
    top = [n.id for p in lay.pages for n in p]
    keep = [i for i in top if i != "com.apple.Preview"]
    lay.arrange([keep[:10], keep[10:]], library=["com.apple.TVRemoteApp", "com.apple.Preview"])
    assert lay.validate() == []
    with pytest.raises(LayoutError, match="left out"):
        lay.arrange([keep], library=["com.apple.Preview"])  # TV Remote dropped


def test_real_phone_item_shapes():
    """Shapes seen on an iOS 27.0.1 phone over USB: widgets and some apps carry an empty
    iconLists, web clips have a UUID displayIdentifier and a webClipURL."""
    widget = {"bundleIdentifier": "com.apple.weather.widget", "containerBundleIdentifier": "com.apple.weather",
              "displayIdentifier": "3150CE23", "elementType": "widget", "gridSize": "small",
              "iconLists": [], "iconType": "custom", "widgetIdentifier": "com.apple.weather"}
    odd_app = {"bundleIdentifier": "com.zimride.instant", "displayIdentifier": "138B31A6",
               "iconLists": [], "iconType": "app"}
    clip = {"displayIdentifier": "9F1E", "displayName": "Router", "webClipURL": "http://192.168.1.1"}
    folder = {"displayName": "Travel", "listType": "folder", "iconLists": [[app("com.a.b", "B"), odd_app]]}
    state = [[app("com.apple.mobilesafari", "Safari")], [widget, clip, folder]]
    lay = Layout(state)
    kinds = {n.id: n.kind for n in lay.nodes.values()}
    assert kinds["widget:3150CE23"] == "widget" and kinds["9F1E"] == "webclip" and kinds["folder:1"] == "folder"
    assert kinds["com.zimride.instant"] == "app"
    assert lay.pages[0][0].slots == 4
    assert lay.to_state() == state


def test_root_archive_for_phone_write():
    """What PhoneSource sends: list-format items become SpringBoard's on-disk form."""
    from homescreen.device import library_from_installed, to_root_archive
    widget = {"bundleIdentifier": "com.apple.weather.widget", "elementType": "widget", "gridSize": "small",
              "iconLists": [], "iconType": "custom", "displayIdentifier": "W1"}
    clip = {"displayIdentifier": "9F1E", "displayName": "Router", "webClipURL": "/"}
    folder = {"displayName": "Travel", "listType": "folder", "iconLists": [[app("com.a.b", "B")], []]}
    state = [[app("com.apple.mobilesafari", "Safari")], [widget, clip, folder, app("com.x.y", "Y")]]
    installed = {"com.apple.mobilesafari": {}, "com.a.b": {}, "com.x.y": {}, "com.lib.app": {"CFBundleDisplayName": "Lib"},
                 "com.hidden": {"SBAppTags": ["hidden"]}, "com.apple.TVRemoteApp": {}}
    lib = library_from_installed(state, installed)
    assert [x["bundleIdentifier"] for x in lib] == ["com.lib.app"]
    out = to_root_archive(state, lib)
    assert out["buttonBar"] == ["com.apple.mobilesafari"]
    page = out["iconLists"][0]
    assert "iconLists" not in page[0] and page[0]["gridSize"] == "small"
    assert page[1] == "9F1E" and page[3] == "com.x.y"
    assert page[2]["iconLists"] == [["com.a.b"]] and page[2]["displayName"] == "Travel"
    assert out["ignored"] == ["com.lib.app"]


def test_restore_old_backup_without_provenance_is_rejected(tmp_path):
    """Legacy layouts cannot prove which device they came from."""
    path = tmp_path / "IconState.plist"
    path.write_bytes(IOS27.read_bytes())
    s = Session(FileSource(path), tmp_path / "data")
    run(s.read())
    state = s.require().to_state()
    bdir = s._dev_dir("backups")
    (bdir / "20200101-000000-old.plist").write_bytes(plistlib.dumps(state))  # old style: bare list
    before = path.read_bytes()
    with pytest.raises(LayoutError):
        run(s.restore("20200101-000000-old.plist"))
    assert path.read_bytes() == before
