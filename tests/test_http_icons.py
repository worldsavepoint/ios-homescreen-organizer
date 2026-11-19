"""Browser sessions, icon reads, and color sorting with owned in-memory devices."""
import asyncio
import json
from collections import Counter
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlsplit
import socket

import pytest
from PIL import Image
from starlette.requests import Request
from starlette.testclient import TestClient

from fake_phone import app, folder, widget
from homescreen import colors, server, ui
from homescreen.session import Session
from test_integrity import MemorySource
from test_web_i18n import browser


def png(rgb):
    data = BytesIO()
    Image.new("RGB", (4, 4), rgb).save(data, format="PNG")
    return data.getvalue()


class IconSource(MemorySource):
    def __init__(self, device="first"):
        super().__init__()
        self.udid = device
        self.images = {"a": png("blue"), "b": png("red")}
        self.requested = []
        self.library = None

    async def read(self):
        state, _ = await super().read()
        return state, self.library

    async def icon_png(self, bundle):
        self.requested.append(bundle)
        return self.images.get(bundle)


def loaded(root, source):
    selected = Session(source, root, device=source.udid)
    asyncio.run(selected.read())
    return selected


def icon_request(bundle):
    return Request({"type": "http", "scheme": "http", "method": "GET", "path": "/icon",
                    "query_string": ("id=" + bundle).encode(), "server": ("127.0.0.1", 80),
                    "headers": [(b"host", b"127.0.0.1"), (b"x-token", ui.TOKEN.encode())]})


def test_two_editors_receive_origin_scoped_explicit_credentials(tmp_path, monkeypatch):
    selected = loaded(tmp_path, IconSource())
    monkeypatch.setattr(ui, "session", lambda: selected)
    editors = []
    for port in (8765, 8766):
        origin = f"http://127.0.0.1:{port}"
        client = TestClient(ui.app, base_url=origin)
        reply = client.post("/v1/authenticate", headers={"Origin": origin, "X-Token": ui.TOKEN})
        sets_cookie = "set-cookie" in reply.headers
        assert reply.status_code == 200 and not sets_cookie
        credential = reply.json().get("credential")
        issued = isinstance(credential, str) and bool(credential) and credential != ui.TOKEN
        assert issued
        editors.append((client, origin, {"X-Editor-Session": credential}))
    distinct = editors[0][2] != editors[1][2]
    assert distinct
    for client, origin, headers in editors:
        assert client.get("/api/state", headers=headers).json()["loaded"]
        assert client.get("/api/state").status_code == 401
        assert client.get("/icon?id=a", headers=headers).content == selected.source.images["a"]
        assert client.get("/api/state", headers={"X-Token": ui.TOKEN}).status_code == 200
        assert client.get("/v1/health", headers=headers).status_code == 401
        assert client.post("/api/auth", headers={**headers, "Origin": origin}).status_code == 401


@pytest.mark.parametrize("occupied,open_browser", [(True, True), (False, True), (False, False)])
def test_cli_notifies_once_at_its_ready_listener(monkeypatch, capsys, occupied, open_browser):
    import uvicorn

    opened, listeners = [], []
    monkeypatch.setattr(ui.config, "enable_device_switching", lambda: None)
    monkeypatch.setattr(ui.webbrowser, "open", lambda url: opened.append(urlsplit(url).port))
    monkeypatch.setattr(ui.threading, "Timer", lambda delay, fn, args: SimpleNamespace(start=lambda: fn(*args)))
    async def end(server):
        assert server.started
        listeners.extend(sock.getsockname()[1] for listener in server.servers for sock in listener.sockets)
        server.should_exit = True
    monkeypatch.setattr(uvicorn.Server, "main_loop", end)
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        if occupied:
            with pytest.raises(SystemExit):
                ui.main(port=held.getsockname()[1], open_browser=open_browser)
        else:
            ui.main(port=0, open_browser=open_browser)
    assert opened == (listeners if open_browser else [])
    assert len(listeners) == (0 if occupied else 1)
    assert ui.TOKEN not in capsys.readouterr().out


def test_startup_failure_closes_its_listener_before_any_browser_callback(monkeypatch):
    import uvicorn

    listeners, opened = [], []
    original = uvicorn.Server.startup
    async def interrupted(server, sockets=None):
        await original(server, sockets)
        listeners.extend(server.servers)
        raise RuntimeError("fixture startup interruption")
    monkeypatch.setattr(uvicorn.Server, "startup", interrupted)
    monkeypatch.setattr(ui.config, "enable_device_switching", lambda: None)
    monkeypatch.setattr(ui.webbrowser, "open", lambda url: opened.append(True))
    monkeypatch.setattr(ui.threading, "Timer", lambda delay, fn, args: SimpleNamespace(start=lambda: fn(*args)))
    with pytest.raises(RuntimeError, match="fixture startup interruption"):
        ui.main(port=0)
    try:
        assert listeners and all(not listener.sockets for listener in listeners) and not opened
    finally:
        for listener in listeners:
            listener.close()


@pytest.mark.parametrize("switch_at", ["queued", "reading", "never"])
def test_icon_checks_its_selected_session_through_delivery(tmp_path, monkeypatch, switch_at):
    first = loaded(tmp_path, IconSource("first"))
    second = loaded(tmp_path, IconSource("second"))
    active = [first]
    monkeypatch.setattr(ui, "session", lambda: active[0])

    async def request():
        gate = asyncio.Semaphore(0)
        monkeypatch.setattr(ui, "_icon_gate", gate)
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def supplied(bundle):
            calls.append(("first", bundle))
            started.set()
            await finish.wait()
            return b"selected-image"
        async def unexpected(bundle):
            calls.append(("second", bundle))
            return None
        monkeypatch.setattr(first, "icon", supplied)
        monkeypatch.setattr(second, "icon", unexpected)
        task = asyncio.create_task(ui.get_icon(icon_request("a")))
        await asyncio.sleep(0)
        if switch_at == "queued":
            active[0] = second
        gate.release()
        if switch_at == "reading":
            await started.wait()
            active[0] = second
        finish.set()
        result = await asyncio.wait_for(task, 2)
        if switch_at == "never":
            assert result.status_code == 200 and result.body == b"selected-image"
        else:
            assert result.status_code == 409 and not result.body
        assert calls == ([] if switch_at == "queued" else [("first", "a")])
    asyncio.run(request())


def test_membership_is_built_once_and_follows_layout_replacement(tmp_path, monkeypatch):
    source = IconSource()
    source.state = [[], [app("a", "First"), widget("Calendar", "small")]]
    selected = loaded(tmp_path, source)
    monkeypatch.setattr(ui, "session", lambda: selected)
    calls = []
    original = ui.widget_apps
    def observed(raw):
        calls.append(True)
        return original(raw)
    monkeypatch.setattr(ui, "widget_apps", observed)
    query = lambda name: asyncio.run(ui.get_icon(icon_request(name))).status_code
    for _ in range(3):
        assert query("a") == 200 and query("absent") == 404
    assert calls == [True]
    source.state = [[], [app("b", "Second"), widget("Calendar", "small")]]
    asyncio.run(selected.read())
    assert query("a") == 404 and query("b") == 200
    assert len(calls) == 2


def test_warm_icon_is_opened_once_and_access_errors_propagate(tmp_path, monkeypatch):
    import homescreen.session as storage

    selected = loaded(tmp_path, IconSource())
    assert asyncio.run(selected.icon("a")) == selected.source.images["a"]
    opens = []
    original = storage.open_private_file
    def counted(root, path, *args, **kwargs):
        if path.suffix == ".png":
            opens.append(path)
        return original(root, path, *args, **kwargs)
    monkeypatch.setattr(storage, "open_private_file", counted)
    assert asyncio.run(selected.icon("a")) == selected.source.images["a"]
    assert len(opens) == 1 and selected.source.requested == ["a"]
    def deny(path):
        raise PermissionError("unavailable")
    monkeypatch.setattr(selected, "_read_local", deny)
    with pytest.raises(PermissionError):
        asyncio.run(selected.icon("a"))
    assert selected.source.requested == ["a"]


@pytest.mark.parametrize("route", ["http", "mcp"])
@pytest.mark.parametrize("keep", [True, False])
def test_color_sort_reads_only_sortable_occurrences(tmp_path, monkeypatch, route, keep):
    source = IconSource()
    source.state = [[app("dock", "Dock")], [app("first", "First")],
                    [folder("Shelf", [app("child", "Nested")]), app("a", "Blue"), app("b", "Red")]]
    source.library = [app("library", "Library")]
    selected = loaded(tmp_path, source)
    monkeypatch.setattr(ui, "session", lambda: selected)
    monkeypatch.setattr(server, "session", lambda: selected)
    if route == "mcp":
        assert asyncio.run(server.sort_by_color(keep)) == {"ok": True, "problems": []}
    else:
        result = TestClient(ui.app, base_url="http://127.0.0.1").post(
            "/api/op", headers={"X-Token": ui.TOKEN},
            json={"op": "sort_color", "binding": selected.binding, "args": {"keep_first_page": keep}})
        assert result.status_code == 200
    assert source.requested == (["a", "b"] if keep else ["first", "a", "b"])
    ordered = [node.id for page in selected.require().pages for node in page]
    assert ordered.index("b") < ordered.index("a")
    assert selected.history()["undo"] == 1 and selected.undo()


def test_empty_color_sort_keeps_public_all_occurrences(tmp_path, monkeypatch):
    source = IconSource()
    selected = loaded(tmp_path, source)
    monkeypatch.setattr(server, "session", lambda: selected)
    assert asyncio.run(server.sort_by_color()) == {"ok": True, "problems": []}
    assert source.requested == [] and selected.history()["undo"] == 1
    assert set(asyncio.run(server.icon_colors([]))) == {"a", "b"}


def test_color_cache_uses_current_png_and_occurrence_names(tmp_path, monkeypatch):
    source = IconSource()
    source.state = [[], [app("a", "First"), app("a", "Second"), app("b", "Third")]]
    source.images["b"] = source.images["a"]
    selected = loaded(tmp_path, source)
    ids = [n.id for n in selected.require().pages[0]]
    counted = Counter()
    analyze = colors.dominant
    def observed(data):
        counted[data] += 1
        return analyze(data)
    monkeypatch.setattr(colors, "dominant", observed)
    first = asyncio.run(selected.icon_colors())
    assert [first[i]["name"] for i in ids] == ["First", "Second", "Third"]
    first[ids[0]]["family"] = "changed by caller"
    source.state[1][0]["displayName"] = "Renamed"
    asyncio.run(selected.read())
    again = asyncio.run(selected.icon_colors())
    assert again[ids[0]]["name"] == "Renamed" and all(row["family"] == "blue" for row in again.values())
    assert counted[source.images["a"]] == 1
    cache = selected._dev_dir("icons") / "a.png"
    cache.write_bytes(png("red"))
    assert asyncio.run(selected.icon_colors([ids[0]]))[ids[0]]["family"] == "red"
    cache.write_bytes(b"invalid fixture")
    for _ in range(2):
        with pytest.raises(ValueError):
            asyncio.run(selected.icon_colors([ids[0]]))
    assert counted[b"invalid fixture"] == 2


@pytest.mark.parametrize("storage", ["writable", "quota", "remove", "unavailable", "unreadable"])
def test_browser_exchange_authorizes_the_page_independently_of_tab_storage(storage):
    browser('''
const status=await vm.runInContext('authentication',context);
assert.equal(status,undefined);
assert.equal(context.location.hash,'');
assert.equal(context.stateRequests,1);
assert.equal(vm.runInContext('V.loaded',context),context.storageMode!=='unreadable');
assert.equal(context.sentEditorCredential,context.storageMode!=='unreadable');
''', native=True, setup='''
context.storageMode=MODE;
context.location.hash=MODE==='unreadable'?'':'#token=fixture-launch';
context.history.replaceState=()=>{context.location.hash='';};
Object.defineProperty(context,'sessionStorage',{get(){
 if(MODE==='unavailable')throw Error('storage unavailable');
 return {
  getItem(){if(MODE==='unreadable')throw Error('storage unavailable');return null;},
  removeItem(){if(MODE==='remove')throw Error('storage unavailable');},
  setItem(){if(MODE==='quota')throw Error('storage full');}
 };
}});
const fetchPage=context.fetch;context.stateRequests=0;
context.fetch=(path,options={})=>{
 if(path==='/v1/authenticate')return Promise.resolve({ok:true,json:async()=>({credential:'fixture-editor'})});
 if(path==='/api/state'){
  context.stateRequests++;
  context.sentEditorCredential=options.headers?.['X-Editor-Session']==='fixture-editor';
  if(!context.sentEditorCredential)return Promise.resolve({ok:false,status:401,json:async()=>({error:'Authorization required'})});
 }
 return fetchPage(path,options);
};
'''.replace('MODE',json.dumps(storage)))


def test_repeated_visible_icons_share_only_their_pending_read():
    browser('''
const make=()=>vm.runInContext('iconImg({icon:"fixture.icon",name:"Actual name"}).firstChild',context);
const show=(img,yes)=>context.visibility([{target:img,isIntersecting:yes}]);
const release=img=>{context.selectedImage=img;vm.runInContext('releaseIcon(selectedImage)',context);};
const held=[];
const original=context.fetch;
context.fetch=(path,options)=>path==='/icon?id=fixture.icon'?
 new Promise(resolve=>held.push({resolve,signal:options.signal,revision:options.headers['X-Draft-Revision']})):original(path,options);
const ready=(entry,ok=true,status=200)=>entry.resolve({ok,status,blob:async()=>({})});
const a=make(),b=make();show(a,true);show(b,true);await tick();
assert.equal(held.length,1);
show(a,false);assert.equal(held[0].signal.aborted,false);
show(a,true);await tick();assert.equal(held.length,1);
ready(held[0]);await tick();await tick();assert.deepEqual([a.src,b.src].sort(),['blob:1','blob:2']);
a.onload();b.onload();assert.deepEqual(context.released.sort(),['blob:1','blob:2']);
const c=make(),d=make();show(c,true);show(d,true);await tick();
assert.equal(held.length,2);release(c);assert.equal(held[1].signal.aborted,false);
release(d);assert.equal(held[1].signal.aborted,true);
const e=make();show(e,true);await tick();assert.equal(held.length,3);
ready(held[1]);await tick();const f=make();show(f,true);await tick();
assert.equal(held.length,3,'late completion retained the replacement request');
fixture.binding={device:'synthetic',revision:'next'};const g=make();show(g,true);await tick();
assert.equal(held.length,4);assert.equal(held[2].revision,'current');assert.equal(held[3].revision,'next');
ready(held[2],false,409);await tick();await tick();assert.equal(held.length,4);
ready(held[3]);await tick();await tick();
assert.equal(new Set([e.src,f.src,g.src]).size,3);
[e,f,g].forEach(img=>img.onload());assert.equal(context.released.length,5);
''',native=True,setup='''
context.AbortController=AbortController;
context.IntersectionObserver=class{constructor(callback){context.visibility=callback;}observe(){}unobserve(){}};
Object.defineProperty(Element.prototype,'isConnected',{get(){return Boolean(this.parent);}});
context.released=[];let urls=0;
context.URL=class extends URL{};
context.URL.createObjectURL=()=>'blob:'+(++urls);
context.URL.revokeObjectURL=url=>context.released.push(url);
''')


def test_visible_icons_share_one_fresh_read_after_an_unchanged_revision_conflict():
    browser('''
const held=[];
const original=context.fetch;
context.fetch=(path, options)=>path==='/icon?id=fixture.icon'?
 new Promise(resolve=>held.push({resolve,signal:options.signal})):original(path,options);
function visible(name) {
 context.iconName=name;
 const tile=vm.runInContext('iconImg({icon:"fixture.icon",name:iconName})',context);
 context.visibility([{target:tile.firstChild,isIntersecting:true}]);
 return tile;
}
const finish=(request,status)=>request.resolve({ok:status===200,status,blob:async()=>({})});
const a=visible('Alpha'),b=visible('Beta');
await tick();assert.equal(held.length,1);
finish(held[0],409);await tick();
assert.equal(held.length,2,'still-visible copies need one fresh admission');
const c=visible('Charlie');await tick();
assert.equal(held.length,2,'the later copy joins the fresh read');
finish(held[1],409);await tick();await tick();
assert.equal(held.length,2,'joining a pending retry preserves its limit');
assert.deepEqual([a,b,c].map(tile=>tile.textContent),['A','B','C']);
const next=visible('Delta');await tick();
assert.equal(held.length,3);finish(held[2],200);await tick();await tick();
assert.equal(next.firstChild.src,'blob:recovered');
next.firstChild.onload();assert.deepEqual(context.released,['blob:recovered']);
''',native=True,setup='''
context.AbortController=AbortController;
context.IntersectionObserver=class{constructor(callback){context.visibility=callback;}observe(){}unobserve(){}};
Object.defineProperty(Element.prototype,'isConnected',{get(){return Boolean(this.parent);}});
Object.defineProperty(Element.prototype,'parentNode',{get(){return this.parent;}});
context.released=[];
context.URL=class extends URL{};
context.URL.createObjectURL=()=>'blob:recovered';
context.URL.revokeObjectURL=url=>context.released.push(url);
''')
