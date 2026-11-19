"""The Screen Organizer app: a local web UI over the same draft, apply and backup engine.

Listens on 127.0.0.1 only. Sensitive routes require a per-launch credential delivered
out of band; browser sessions also enforce the exact local origin.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import threading
import webbrowser
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import organize, usage
from . import config
from .config import DATA, session
from .model import Layout, LayoutError, Node, PageCells, widget_apps
from .session import ReviewChanged

WEB = Path(__file__).parent / "web"
TOKEN = os.environ.get("HOMESCREEN_UI_TOKEN") or secrets.token_urlsafe(32)
_HEALTH_SECRET = os.environ.get("HOMESCREEN_HEALTH_SECRET")
_BROWSER_SECRET = secrets.token_bytes(32)
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
_device_lock = asyncio.Lock()  # one read/apply/restore at a time
_icon_gate = asyncio.Semaphore(4)


# ---- view -----------------------------------------------------------------------------

def _item(n: Node, lay: Layout) -> dict[str, Any]:
    d: dict[str, Any] = {"id": n.id, "name": n.name, "kind": n.kind}
    if n.bundle_id:
        d["icon"] = n.bundle_id
        d["bundle_id"] = n.bundle_id
    if n.kind == "widget":
        apps = [b for b in widget_apps(n.raw) if b]
        d["name"] = lay.widget_label(n)
        d["stack"] = len(apps) > 1
        if apps:
            d["icon"] = apps[0]
        d["size"] = str(n.raw.get("gridSize", "small")).lower()
        d["slots"] = n.slots
    if n.kind == "folder":
        d["pages"] = [[_item(c, lay) for c in p] for p in n.children]
    return d


def _page(page: list[Node], lay: Layout, pos: PageCells) -> list[dict[str, Any]]:
    out = []
    for n in page:
        d = _item(n, lay)
        if n.id in pos:
            d["pos"] = list(pos[n.id])
        if n.id in lay.pins:
            d["pinned"] = True
        out.append(d)
    return out


def _usage_view(lay: Layout, u: dict[str, Any] | None, original: Layout) -> dict[str, Any] | None:
    if u is None:
        return None
    if not u.get("ok"):
        return {"ok": False, "error": u.get("error")}
    opens = u["opens"]
    return {"ok": True, "days": u["days"], "source": u.get("source", "mac"), "reports": u.get("reports"),
            "background": [lay.nodes[b].name for b in u.get("background", []) if b in lay.nodes],
            "original": usage.swipes(original, opens),
            "now": usage.swipes(lay, opens), "top": usage.top_apps(lay, opens, 12, locations=False)}


def _read_only() -> bool:
    return os.environ.get("HOMESCREEN_READ_ONLY") == "1"


def _problem_details(lay: Layout, problems: list[str]) -> list[dict[str, Any]]:
    """Match known diagnostics against raw layout names without parsing their text."""
    candidates: dict[str, dict[str, Any]] = {}

    def add(message: str, key: str, **params: Any) -> None:
        candidates[message] = {"key": "validation." + key, "params": params}

    add(f"the App Bank still holds {len(lay.bank)} item(s); place them or send them to the App Library",
        "bank", count=len(lay.bank))
    add(f"dock has {len(lay.dock)} items (max {lay.dock_slots})", "dock", count=len(lay.dock), capacity=lay.dock_slots)
    for page, nodes in enumerate(lay.pages, 1):
        add(f"page {page} is empty (iOS keeps it as a blank page; use remove_empty)", "emptyPage", page=page)
        add(f"page {page} needs {sum(n.slots for n in nodes)} slots (max {lay.page_slots})", "pageCapacity",
            page=page, count=sum(n.slots for n in nodes), capacity=lay.page_slots)
        for node in nodes:
            add(f"page {page}: no room for {node.name}", "noRoom", page=page, name=node.name)
            if node.id in lay.pins:
                row, column = divmod(lay.pins[node.id], lay.grid.cols)
                add(f"page {page}: {node.name} can't be pinned at row {row + 1}, column {column + 1}",
                    "pin", page=page, name=node.name, row=row + 1, column=column + 1)
    for nodes in [lay.dock, *lay.pages]:
        for node in nodes:
            if node.kind == "folder":
                for folder_page, children in enumerate(node.children, 1):
                    add(f"folder {node.name!r} page {folder_page} has {len(children)} items (max {lay.grid.folder})",
                        "folderCapacity", name=node.name, page=folder_page, count=len(children), capacity=lay.grid.folder)
                add(f"folder {node.name!r} contains a folder or widget", "folderContents", name=node.name)
    return [candidates.get(message, {"key": "validation.unknown", "params": {}, "diagnostic": message})
            for message in problems]


def view(s=None) -> dict[str, Any]:
    s = session() if s is None else s
    if s.layout is None:
        result = {"loaded": False, "source": s.source.name, "device_id": config.current_device(),
                  "read_only": _read_only(), "binding": getattr(s, "binding", None)}
        if getattr(s, "_draft_error", None):
            result["draft_error"] = s._draft_error
        return result
    lay = s.layout
    original = lay.original()
    placements = {page: lay.place(nodes) for page, nodes in enumerate(lay.pages, 1)}
    new_cells = {page: result[0] for page, result in placements.items()}
    page_problems = {page: result[1] for page, result in placements.items()}
    usage_view = _usage_view(lay, s.usage, original)
    top = usage_view.get("top", []) if usage_view else []
    usage_ids = {row["id"]: row.pop("placement_id") for row in top}
    d, old, new = lay.review(original, page_cells=new_cells, page_problems=page_problems,
                           location_ids=set(usage_ids.values()))
    labels = dict(old.values())
    labels.update(new.values())
    for row in top:
        node_id = usage_ids[row["id"]]
        row.update(name=lay.nodes[node_id].name, where=new[node_id][0])
    return {
        "loaded": True,
        "binding": getattr(s, "binding", None),
        "read_only": _read_only(),
        "source": s.source.name,
        "device_id": s.device_id,
        "device": s.info,
        "dock": [_item(n, lay) for n in lay.dock],
        "pages": [_page(nodes, lay, new_cells[page]) for page, nodes in enumerate(lay.pages, 1)],
        "library": None if lay.library is None else [_item(n, lay) for n in lay.library],
        "bank": [_item(n, lay) for n in lay.bank],
        "usage": usage_view,
        "moved": sorted(detail["id"] for detail in d["move_details"].values()),
        "diff": {**{k: d[k] for k in ("items_moved", "to_app_library", "pages_before", "pages_after",
                                   "problems", "apply_token", "gaps_changed", "has_changes", "placement_changes")},
                 "problem_details": _problem_details(lay, d["problems"]) if d["problems"] else []},
        "location_labels": labels,
        "move_details": d["move_details"],
        "moves": d["moves"],
        "history": s.history(),
        "gaps": {"readable": s.gaps_readable, "count": len(lay.pins), "changed": d["gaps_changed"]},
        "slots": {"page": lay.page_slots, "dock": lay.dock_slots},
        "grid": {**lay.grid.to_dict(), **({"folder_columns": s.folder_columns} if getattr(s, "folder_columns", None) else {})},
    }


# ---- operations -------------------------------------------------------------------------

def _ops() -> dict[str, Callable[..., Callable[[Layout], Any]]]:
    return {
        "move": lambda id, target, index=None, before=None: lambda lay: lay.move(id, target, index, before),
        "move_many": lambda ids, target, before=None: lambda lay: lay.move_many(ids, target, before),
        "swap": lambda a, b: lambda lay: lay.swap(a, b),
        "pin": lambda id, page, cell: lambda lay: lay.pin(id, page, cell),
        "unpin": lambda ids: lambda lay: [lay.unpin(i) for i in ids],
        "create_folder": lambda name, ids, target, index=None: lambda lay: lay.create_folder(name, ids, target, index),
        "rename_folder": lambda id, name: lambda lay: lay.rename_folder(id, name),
        "dissolve_folder": lambda id: lambda lay: lay.dissolve_folder(id),
        "insert_page": lambda page: lambda lay: lay.insert_page(page),
        "move_page": lambda page, to: lambda lay: lay.move_page(page, to),
        "delete_page": lambda page: lambda lay: lay.delete_page(page),
        "remove_empty": lambda: lambda lay: lay.drop_empty(),
        "sort_alpha": lambda keep_first_page=True: lambda lay: organize.alphabetical(lay, keep_first_page),
        "sort_folders": lambda: lambda lay: organize.sort_folder_contents(lay),
        "file_into_folders": lambda groups, keep_first_page=True:
            lambda lay: organize.into_folders(lay, groups, keep_first_page),
    }


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


def _matches(value: str | None, expected: str) -> bool:
    return value is not None and secrets.compare_digest(value.encode(), expected.encode())


def _browser_credential(request: Request) -> str:
    origin = f"{request.url.scheme}://{request.headers['host']}"
    return hmac.new(_BROWSER_SECRET, origin.encode(), hashlib.sha256).hexdigest()


def _security_error(message: str, status: int, code: str, param: str | None = None) -> JSONResponse:
    error_type = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error"}[status]
    return JSONResponse({"error": {"message": message, "type": error_type,
                                   "param": param, "code": code}}, status_code=status)


def _guard(request: Request, *, public: bool = False, header_only: bool = False) -> Response | None:
    host = request.headers.get("host", "")
    try:
        authority = urlsplit("http://" + host)
        valid_host = (authority.hostname in LOCAL_HOSTS and authority.username is None
                      and not authority.path and not authority.query and not authority.fragment)
        authority.port  # reject malformed port numbers
    except ValueError:
        valid_host = False
    if not valid_host:
        return _security_error("bad host", 403, "invalid_host", "host")
    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{host}":
        return _security_error("bad origin", 403, "invalid_origin", "origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        return _security_error("cross-site request", 403, "cross_site_request", "sec_fetch_site")
    if public:
        return None
    if _matches(request.headers.get("x-token"), TOKEN):
        return None
    if not header_only and _matches(request.headers.get("x-editor-session"), _browser_credential(request)):
        if request.method in {"GET", "HEAD"} or origin is not None:
            return None
        return _security_error("same-origin proof required", 403, "origin_required", "origin")
    return _security_error("authentication required", 401, "invalid_credentials")


async def post_auth(request: Request) -> Response:
    if (bad := _guard(request, header_only=True)):
        return bad
    return JSONResponse({"ok": True, "credential": _browser_credential(request)})


async def get_health(request: Request) -> Response:
    if _HEALTH_SECRET:
        if (bad := _guard(request, public=True)):
            return bad
        challenge = request.headers.get("x-homescreen-challenge", "")
        if len(challenge) != 64 or re.fullmatch(r"[0-9a-fA-F]{64}", challenge) is None:
            return _security_error("invalid health challenge", 400, "invalid_health_challenge", "challenge")
        proof = hmac.new(_HEALTH_SECRET.encode(), challenge.encode(), hashlib.sha256).hexdigest()
        return JSONResponse({"app": "homescreen", "ready": True, "proof": proof})
    if (bad := _guard(request, header_only=True)):
        return bad
    return JSONResponse({"app": "homescreen", "ready": True})


def _err(e: Exception, status: int = 400, s=None) -> JSONResponse:
    if isinstance(e, ReviewChanged):
        status = 409
    return JSONResponse({"error": str(e), "view": view(s)}, status_code=status)


def _reviewed_session(body: dict):
    selected = session()
    if body.get("binding") != selected.binding:
        raise ReviewChanged("the reviewed device or draft changed; review the current layout")
    return selected


async def index(request: Request) -> Response:
    if (bad := _guard(request, public=True)):
        return bad
    stamp = str(int(max(f.stat().st_mtime for f in WEB.iterdir())))  # fresh assets after an update
    html = ((WEB / "index.html").read_text()
            .replace("/static/app.css", f"/static/app.css?v={stamp}").replace("/static/app.js", f"/static/app.js?v={stamp}"))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


async def get_state(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    async with _device_lock:
        s = session()
        try:
            with s.refreshed_draft():
                return JSONResponse(view(s))
        except (LayoutError, OSError) as e:
            return _err(e, 502, s)


async def post_op(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    body = await _json(request)
    name, args = body.get("op"), body.get("args") or {}
    try:
        s = _reviewed_session(body)
        expected = body["binding"]
        if name == "undo":
            s.undo(expected=expected)
        elif name == "redo":
            s.redo(expected=expected)
        elif name == "reset":
            s.reset(expected=expected)
        elif name == "sort_color":
            keep_first = args.get("keep_first_page", True)
            colors = await s.colors_for_sort(keep_first)
            if session() is not s:
                raise ReviewChanged("the reviewed device or draft changed; review the current layout")
            s.edit(lambda lay: organize.by_color(lay, colors, keep_first), expected=expected)
        elif name == "load_usage":
            provider = args.get("source", "mac")
            if provider not in ("device", "mac"):
                raise LayoutError("usage source must be device or mac")
            selected = lambda: session() is s
            if provider == "device":
                async with _device_lock:
                    _reviewed_session(body)
                    await s.load_device_usage(expected=expected, selected=selected)
            else:
                await asyncio.to_thread(s.load_usage, int(args.get("days", 30)), expected=expected, selected=selected)
            _reviewed_session(body)
            with s._exclusive(expected=expected):
                return JSONResponse(view(s))
        elif name == "optimize_usage":
            if s.usage is None:  # the device's own reports first, then Screen Time on this Mac
                try:
                    async with _device_lock:
                        _reviewed_session(body)
                        await s.load_device_usage(expected=expected, selected=lambda: session() is s)
                except LayoutError:
                    raise
                except Exception:
                    pass
                if not (s.usage or {}).get("ok"):
                    _reviewed_session(body)
                    await asyncio.to_thread(s.load_usage, expected=expected, selected=lambda: session() is s)
            if not s.usage.get("ok"):
                raise LayoutError(s.usage.get("error", "no usage data"))
            opens = s.usage["opens"]
            _reviewed_session(body).edit(lambda lay: usage.optimize(lay, opens, args.get("keep_first_page", False)),
                                        expected=expected)
        elif name == "group_categories":
            lay = s.require()
            ids = list(dict.fromkeys(n.bundle_id for p in lay.pages for n in p if n.kind == "app" and n.bundle_id))
            cats = await asyncio.to_thread(organize.fetch_categories, ids, DATA / "categories.json")
            keep = args.get("keep_first_page", True)
            _reviewed_session(body).edit(lambda lay: organize.into_folders(lay, organize.category_groups(lay, cats, keep), keep),
                                        expected=expected)
        else:
            factory = _ops().get(name)
            if factory is None:
                raise LayoutError(f"unknown operation {name!r}")
            s.edit(factory(**args), expected=expected)
    except (LayoutError, TypeError, ValueError, KeyError) as e:
        return _err(e)
    except OSError as e:
        return _err(e, 502)
    return JSONResponse(view(s))


async def post_read(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    body = await _json(request)
    async with _device_lock:
        try:
            s = _reviewed_session(body)
            await s.read(expected=body["binding"])
        except ReviewChanged as e:
            return _err(e)
        except Exception as e:
            return _err(RuntimeError(f"couldn't read the device: {e}"), 502, s)
    return JSONResponse(view(s))


async def post_apply(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    if _read_only():
        return _security_error("read-only mode: device writes are disabled", 403, "read_only")
    body = await _json(request)
    async with _device_lock:
        try:
            s = _reviewed_session(body)
            result = await s.apply(str(body.get("token", "")), expected=body["binding"])
        except LayoutError as e:
            return _err(e)
        except Exception as e:
            return _err(RuntimeError(f"apply failed: {e}"), 502)
    return JSONResponse({"result": result, "view": view(s)})


_names: dict[str, dict[str, Any]] = {}  # udid -> name/model/version, looked up once per launch


async def _connected() -> dict[str, dict[str, Any]]:
    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import create_using_usbmux
    out: dict[str, dict[str, Any]] = {}
    devs = await usbmux.list_devices()
    for d in sorted(devs, key=lambda d: d.connection_type != "USB"):  # USB first
        if d.serial in out:
            continue
        if not _names.get(d.serial):
            try:
                v = (await create_using_usbmux(serial=d.serial)).all_values
                _names[d.serial] = {"name": v.get("DeviceName"), "model": v.get("ProductType"),
                                    "version": v.get("ProductVersion"), "class": v.get("DeviceClass")}
            except Exception:
                pass  # Discovery still identified the transport when device details are unavailable.
        _names.setdefault(d.serial, {})
        out[d.serial] = {**_names.get(d.serial, {}), "connection": "USB" if d.connection_type == "USB" else "Wi-Fi"}
    return out


async def get_devices(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    if not config.switching():
        return JSONResponse({"switching": False, "current": None, "devices": []})
    try:
        connected = await _connected()
        discovery = "complete"
    except Exception:
        connected = {}
        discovery = "unavailable"
    known = config.known_devices()
    remembered = known
    current = config.current_device()
    if discovery == "unavailable":
        remembered = {**_names, **{u: {**_names.get(u, {}), **k} for u, k in known.items()}}
        if current:
            remembered.setdefault(current, {})
    devices = [{"udid": u, **known.get(u, {}), **c, "connected": True, "draft": u in known} for u, c in connected.items()]
    devices += [{"udid": u, **k, "connected": False if discovery == "complete" else None, "draft": u in known}
                for u, k in remembered.items() if u not in connected]
    return JSONResponse({"switching": True, "current": current, "devices": devices, "discovery": discovery})


async def post_device(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    body = await _json(request)
    udid = str(body.get("udid", ""))
    if not udid:
        return _err(LayoutError("no device given"))
    async with _device_lock:
        try:
            s = _reviewed_session(body)
            with s._exclusive(expected=body["binding"]):
                config.select_device(udid)
        except LayoutError as e:
            return _err(e)
        except OSError as e:
            return _err(e, 502, s)
    return JSONResponse(view())


async def get_backups(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    s = session()
    return JSONResponse({"backups": list(reversed(s.backups())) if s.device_id else [], "binding": s.binding})


async def post_backup_review(request: Request) -> Response:
    if bad := _guard(request):
        return bad
    body = await _json(request)
    try:
        s = _reviewed_session(body)
        review = s.review_backup(str(body.get("name", "")), body["binding"])
    except LayoutError as e:
        return _err(e)
    return JSONResponse({"review": review})


async def post_restore(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    if _read_only():
        return _security_error("read-only mode: device writes are disabled", 403, "read_only")
    body = await _json(request)
    async with _device_lock:
        try:
            s = _reviewed_session(body)
            result = await s.restore(str(body.get("name", "")), expected=body["binding"], review=body.get("review"))
        except LayoutError as e:
            return _err(e)
        except Exception as e:
            return _err(RuntimeError(f"restore failed: {e}"), 502)
    return JSONResponse({"result": result, "view": view(s)})


@lru_cache(maxsize=1)
def _icon_membership(selected, lay: Layout | None, revision: str) -> frozenset[str]:
    """Index the complete selection for this layout instance and saved revision."""
    known = set()
    if lay is not None:
        for node in lay.nodes.values():
            if node.bundle_id:
                known.add(node.bundle_id)
            if node.kind == "widget":
                known.update(widget_apps(node.raw))
    return frozenset(known)


async def get_icon(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    bundle = request.query_params.get("id", "")
    selected = session()
    source, lay, binding = selected.source, selected.layout, selected.binding
    revision = binding["revision"]
    if request.headers.get("x-draft-revision", revision) != revision:
        return Response(status_code=409)
    if not bundle or bundle not in _icon_membership(selected, lay, revision):
        return Response(status_code=404)

    def current() -> bool:
        return (session() is selected and selected.source is source
                and selected.layout is lay and selected.binding == binding)

    async with _icon_gate:
        if not current():
            return Response(status_code=409)
        try:
            png = await selected.icon(bundle)
        except Exception:
            png = None
        if not current():
            return Response(status_code=409)
    if not png:
        return Response(status_code=404)
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


app = Starlette(routes=[
    Route("/", index),
    Route("/v1/health", get_health),
    Route("/health", get_health),
    Route("/v1/authenticate", post_auth, methods=["POST"]),
    Route("/api/auth", post_auth, methods=["POST"]),
    Route("/api/state", get_state),
    Route("/api/op", post_op, methods=["POST"]),
    Route("/api/read", post_read, methods=["POST"]),
    Route("/api/apply", post_apply, methods=["POST"]),
    Route("/api/devices", get_devices),
    Route("/api/device", post_device, methods=["POST"]),
    Route("/api/backups", get_backups),
    Route("/api/backup-review", post_backup_review, methods=["POST"]),
    Route("/api/restore", post_restore, methods=["POST"]),
    Route("/icon", get_icon),
    Mount("/static", StaticFiles(directory=WEB), name="static"),
])


async def local_boundary(request: Request, call_next: Callable) -> Response:
    response = _guard(request, public=True)
    if response is None:
        response = await call_next(request)
    response.headers.update({
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; "
            "frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
    })
    return response


app.add_middleware(BaseHTTPMiddleware, dispatch=local_boundary)


def _exit_with_parent(pid: int) -> None:
    """Inside Screen Organizer.app: stop when the app is gone, even if it crashed."""
    import os
    import time

    def watch() -> None:
        while True:
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                os._exit(0)
            except PermissionError:
                pass
    threading.Thread(target=watch, daemon=True).start()


def main(port: int = 8765, open_browser: bool = True) -> None:
    import os
    import uvicorn
    config.enable_device_switching()
    if os.environ.get("HOMESCREEN_PARENT_PID", "").isdigit():
        _exit_with_parent(int(os.environ["HOMESCREEN_PARENT_PID"]))
    class EditorServer(uvicorn.Server):
        async def startup(self, sockets=None):
            try:
                await super().startup(sockets)
                if self.started and not self.should_exit:
                    actual_port = self.servers[0].sockets[0].getsockname()[1]
                    url = f"http://127.0.0.1:{actual_port}/"
                    print(f"Screen Organizer is running at {url}  (Ctrl-C to stop)")
                    if open_browser:
                        try:
                            await asyncio.to_thread(webbrowser.open, url + "#token=" + quote(TOKEN, safe=""))
                        except Exception:
                            raise RuntimeError("The browser could not open the editor.") from None
            except BaseException:
                for listener in getattr(self, "servers", []):
                    listener.close()
                    await listener.wait_closed()
                raise

    EditorServer(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")).run()
