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
import struct
import threading
import webbrowser
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
from .model import Layout, LayoutError, Node, widget_apps

WEB = Path(__file__).parent / "web"
TOKEN = os.environ.get("HOMESCREEN_UI_TOKEN") or secrets.token_urlsafe(32)
_HEALTH_SECRET = os.environ.get("HOMESCREEN_HEALTH_SECRET")
_BROWSER_TOKEN = secrets.token_urlsafe(32)
_COOKIE = "homescreen_session"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
_device_lock = asyncio.Lock()  # one read/apply/restore at a time
_icon_gate = asyncio.Semaphore(4)


# ---- view -----------------------------------------------------------------------------

def _item(n: Node, lay: Layout) -> dict[str, Any]:
    d: dict[str, Any] = {"id": n.id, "name": n.name, "kind": n.kind}
    if n.bundle_id:
        d["icon"] = n.bundle_id
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


_GridPosition = tuple[int, int, int, int]
_PageCells = dict[str, _GridPosition]


def _page(page: list[Node], lay: Layout, pos: _PageCells) -> list[dict[str, Any]]:
    out = []
    for n in page:
        d = _item(n, lay)
        if n.id in pos:
            d["pos"] = list(pos[n.id])
        if n.id in lay.pins:
            d["pinned"] = True
        out.append(d)
    return out


def _usage_view(lay: Layout, u: dict[str, Any] | None, original: Callable[[], Layout]) -> dict[str, Any] | None:
    if u is None:
        return None
    if not u.get("ok"):
        return {"ok": False, "error": u.get("error")}
    opens = u["opens"]
    return {"ok": True, "days": u["days"], "source": u.get("source", "mac"), "reports": u.get("reports"),
            "background": [lay.nodes[b].name for b in u.get("background", []) if b in lay.nodes],
            "original": usage.swipes(original(), opens),
            "now": usage.swipes(lay, opens), "top": usage.top_apps(lay, opens, 12, locations=False)}


def _read_only() -> bool:
    return os.environ.get("HOMESCREEN_READ_ONLY") == "1"


def _same_fields(a: Any, b: Any) -> bool:
    """Python equality merges plist integers, booleans and reals."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same_fields(value, b[key]) for key, value in a.items())
    if isinstance(a, list):
        return len(a) == len(b) and all(_same_fields(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        return struct.pack(">d", a) == struct.pack(">d", b)
    return a == b


def _has_changes(lay: Layout, original: Callable[[], Layout]) -> bool:
    if (lay.bank or lay.pins != lay.source_pins or
            lay.identity()["ids"] != lay.source_identity["ids"] or
            not _same_fields(lay.to_library(), lay.source_library)):
        return True
    state = lay.to_state()
    # Serialization normalizes imported folders with unnamed or empty subpages.
    return not _same_fields(state, lay.source) and not _same_fields(state, original().to_state())


def _original_cells(original: Layout, lay: Layout, current_cells: dict[int, _PageCells],
                    pinned: set[str]) -> dict[int, _PageCells]:
    """Reuse current geometry when order, footprints and anchors match the original page."""
    cells = {}
    for page, nodes in enumerate(original.pages, 1):
        if not any(n.kind in ("widget", "folder") or n.id in pinned for n in nodes):
            continue
        current = lay.pages[page - 1] if page <= len(lay.pages) else []
        same = (len(nodes) == len(current) and original.grid == lay.grid and
                all(old.id == new.id and original.dims(old) == lay.dims(new)
                    and original.pins.get(old.id) == lay.pins.get(new.id) for old, new in zip(nodes, current)))
        cells[page] = current_cells[page] if same else original.place(nodes)[0]
    return cells


def _special_positions(
    lay: Layout, pinned: set[str], cells: dict[int, _PageCells]
) -> dict[str, tuple[str, int, int, _GridPosition | None]]:
    positions = {}
    for kind, page, nodes in [("dock", 0, lay.dock), ("bank", 0, lay.bank),
                              *(("page", p, nodes) for p, nodes in enumerate(lay.pages, 1))]:
        for index, node in enumerate(nodes, 1):
            if node.kind in ("folder", "widget") or node.id in pinned:
                positions[node.id] = (kind, page, index, cells.get(page, {}).get(node.id))
    return positions


def _placement_changes(lay: Layout, original: Layout, pins: set[str], old_cells: dict[int, _PageCells],
                       new_cells: dict[int, _PageCells]) -> dict[str, dict[str, Any]]:
    old = _special_positions(original, pins, old_cells)
    new = _special_positions(lay, pins, new_cells)
    changes = {}
    for nid in sorted(old.keys() | new.keys()):
        before, after = original.nodes.get(nid), lay.nodes.get(nid)
        node = after or before
        reason = None
        if nid not in old and node.kind in ("folder", "widget"):
            reason = "added"
        elif nid not in new and node.kind in ("folder", "widget"):
            reason = "removed"
        elif before and after and before.name != after.name:
            reason = "renamed"
        elif nid in pins:
            reason = "pinned" if nid in lay.pins else "unpinned"
        elif node.kind == "folder" and (
            [[n.id for n in p] for p in before.children if p] != [[n.id for n in p] for p in after.children if p]
            or not _same_fields(original._unwrap(before), lay._unwrap(after))
        ):
            reason = "edited"
        elif node.kind == "widget" and not _same_fields(before.raw, after.raw):
            reason = "edited"
        if reason or old.get(nid) != new.get(nid):
            row = {"id": nid, "kind": node.kind, "name": lay.widget_label(node) if node.kind == "widget" else node.name,
                   "reason": reason or "moved"}
            if reason == "renamed":
                row["before_name"] = before.name
            changes[nid] = row
    return changes


def _locations(lay: Layout, wanted: set[str], physical: set[str],
               page_cells: dict[int, _PageCells]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Only materialize endpoints needed by Review or the visible usage list."""
    found = {}
    labels = lay.folder_labels()

    def walk(nodes: list[Node], kind: str, where: str, page: int = 0, name: str = "") -> None:
        cells = page_cells.get(page, {}) if kind == "page" else {}
        for index, node in enumerate(nodes, 1):
            if node.id in wanted:
                descriptor = {"kind": kind}
                raw = where
                if kind not in ("library", "bank"):
                    descriptor["index"] = index
                    raw += f" #{index}"
                if page:
                    descriptor["page"] = page
                if kind == "folder":
                    descriptor["name"] = name
                if node.id in physical and node.id in cells:
                    row, col, _, _ = cells[node.id]
                    descriptor.update(row=row + 1, column=col + 1)
                found[node.id] = (raw, descriptor)
            if node.kind == "folder":
                for p, children in enumerate(node.children, 1):
                    walk(children, "folder", f"folder {labels[node.id]!r} p{p}", p, labels[node.id])

    walk(lay.dock, "dock", "dock")
    for page, nodes in enumerate(lay.pages, 1):
        walk(nodes, "page", f"page {page}", page)
    walk(lay.library or [], "library", "App Library")
    walk(lay.bank, "bank", "App Bank")
    return found


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
                  "read_only": _read_only()}
        if s._draft_error:
            result["draft_error"] = s._draft_error
        return result
    lay = s.layout
    d = lay.diff()
    baseline = None

    def original() -> Layout:
        nonlocal baseline
        if baseline is None:
            baseline = lay.original()
        return baseline

    has_changes = _has_changes(lay, original)
    new_cells = {page: lay.place(nodes)[0] for page, nodes in enumerate(lay.pages, 1)}
    old_cells = {}
    placements = {}
    if has_changes:
        pins = {key for key in lay.pins.keys() | lay.source_pins.keys() if lay.pins.get(key) != lay.source_pins.get(key)}
        old_cells = _original_cells(original(), lay, new_cells, pins)
        placements = _placement_changes(lay, original(), pins, old_cells, new_cells)
    moved = {}
    if d["moves"]:
        for node in lay.nodes.values():
            key = f"{node.name} [{node.id}]"
            if key in d["moves"]:
                moved[node.id] = key
    usage_view = _usage_view(lay, s.usage, original)
    top = usage_view.get("top", []) if usage_view else []
    usage_ids = {}
    if top:
        # Resolve each visible bundle to one occurrence for both its name and location.
        top_bundles = {row["id"] for row in top}
        for node in lay.nodes.values():
            if node.bundle_id in top_bundles:
                usage_ids.setdefault(node.bundle_id, node.id)
    changed_ids = moved.keys() | placements.keys()
    wanted = changed_ids | {usage_ids[row["id"]] for row in top}
    physical = set(placements)
    old = _locations(original(), changed_ids, physical, old_cells) if changed_ids else {}
    new = _locations(lay, wanted, physical, new_cells) if wanted else {}
    labels = dict(old.values())
    labels.update(new.values())
    for row in top:
        node_id = usage_ids[row["id"]]
        row.update(name=lay.nodes[node_id].name, where=new[node_id][0])
    move_details = {key: {"id": nid, "name": lay.nodes[nid].name, "from": old[nid][1], "to": new[nid][1]}
                    for nid, key in moved.items()}
    for nid, change in placements.items():
        change["from"] = old[nid][1] if nid in old else None
        change["to"] = new[nid][1] if nid in new else None
    return {
        "loaded": True,
        "read_only": _read_only(),
        "source": s.source.name,
        "device_id": s.device_id,
        "device": s.info,
        "dock": [_item(n, lay) for n in lay.dock],
        "pages": [_page(nodes, lay, new_cells[page]) for page, nodes in enumerate(lay.pages, 1)],
        "library": None if lay.library is None else [_item(n, lay) for n in lay.library],
        "bank": [_item(n, lay) for n in lay.bank],
        "usage": usage_view,
        "moved": sorted(moved),
        "diff": {**{k: d[k] for k in ("items_moved", "to_app_library", "pages_before", "pages_after",
                                   "problems", "apply_token", "gaps_changed")},
                 "has_changes": has_changes, "placement_changes": list(placements.values()),
                 "problem_details": _problem_details(lay, d["problems"]) if d["problems"] else []},
        "location_labels": labels,
        "move_details": move_details,
        "moves": d["moves"],
        "history": s.history(),
        "gaps": {"readable": s.gaps_readable, "count": len(lay.pins), "changed": d["gaps_changed"]},
        "slots": {"page": lay.page_slots, "dock": lay.dock_slots},
        "grid": lay.grid.to_dict(),
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
    if not header_only and _matches(request.cookies.get(_COOKIE), _BROWSER_TOKEN):
        if request.method in {"GET", "HEAD"} or origin is not None:
            return None
        return _security_error("same-origin proof required", 403, "origin_required", "origin")
    return _security_error("authentication required", 401, "invalid_credentials")


async def post_auth(request: Request) -> Response:
    if (bad := _guard(request, header_only=True)):
        return bad
    response = JSONResponse({"ok": True})
    response.set_cookie(_COOKIE, _BROWSER_TOKEN, httponly=True, samesite="strict", path="/")
    return response


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
    return JSONResponse({"error": str(e), "view": view(s)}, status_code=status)


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
    return JSONResponse(view())


async def post_op(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    body = await _json(request)
    name, args = body.get("op"), body.get("args") or {}
    s = session()
    try:
        if name == "undo":
            s.undo()
        elif name == "redo":
            s.redo()
        elif name == "reset":
            s.reset()
        elif name == "sort_color":
            colors = await s.icon_colors()
            s.edit(lambda lay: organize.by_color(lay, colors, args.get("keep_first_page", True)))
        elif name == "load_usage":
            if args.get("source") == "device":
                async with _device_lock:
                    await s.load_device_usage()
            else:
                await asyncio.to_thread(s.load_usage, int(args.get("days", 30)))
        elif name == "optimize_usage":
            if s.usage is None:  # the device's own reports first, then Screen Time on this Mac
                try:
                    async with _device_lock:
                        await s.load_device_usage()
                except Exception:
                    s.usage = None
                if not (s.usage or {}).get("ok"):
                    await asyncio.to_thread(s.load_usage)
            if not s.usage.get("ok"):
                raise LayoutError(s.usage.get("error", "no usage data"))
            opens = s.usage["opens"]
            s.edit(lambda lay: usage.optimize(lay, opens, args.get("keep_first_page", False)))
        elif name == "group_categories":
            lay = s.require()
            ids = [n.id for p in lay.pages for n in p if n.kind == "app"]
            cats = await asyncio.to_thread(organize.fetch_categories, ids, DATA / "categories.json")
            keep = args.get("keep_first_page", True)
            s.edit(lambda lay: organize.into_folders(lay, organize.category_groups(lay, cats, keep), keep))
        else:
            factory = _ops().get(name)
            if factory is None:
                raise LayoutError(f"unknown operation {name!r}")
            s.edit(factory(**args))
    except (LayoutError, TypeError, ValueError, KeyError) as e:
        return _err(e)
    except OSError as e:
        return _err(e, 502)
    return JSONResponse(view())


async def post_read(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    async with _device_lock:
        s = session()
        try:
            await s.read()
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
            result = await session().apply(str(body.get("token", "")))
        except LayoutError as e:
            return _err(e)
        except Exception as e:
            return _err(RuntimeError(f"apply failed: {e}"), 502)
    return JSONResponse({"result": result, "view": view()})


_names: dict[str, dict[str, Any]] = {}  # udid -> name/model/version, looked up once per launch


async def _connected() -> dict[str, dict[str, Any]]:
    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import create_using_usbmux
    out: dict[str, dict[str, Any]] = {}
    try:
        devs = await usbmux.list_devices()
    except Exception:
        return out
    for d in sorted(devs, key=lambda d: d.connection_type != "USB"):  # USB first
        if d.serial in out:
            continue
        if d.serial not in _names:
            try:
                v = (await create_using_usbmux(serial=d.serial)).all_values
                _names[d.serial] = {"name": v.get("DeviceName"), "model": v.get("ProductType"),
                                    "version": v.get("ProductVersion"), "class": v.get("DeviceClass")}
            except Exception:
                continue  # locked and never trusted, or gone already
        out[d.serial] = {**_names[d.serial], "connection": "USB" if d.connection_type == "USB" else "Wi-Fi"}
    return out


async def get_devices(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    if not config.switching():
        return JSONResponse({"switching": False, "current": None, "devices": []})
    connected = await _connected()
    known = config.known_devices()
    devices = [{"udid": u, **known.get(u, {}), **c, "connected": True, "draft": u in known} for u, c in connected.items()]
    devices += [{"udid": u, **k, "connected": False, "draft": True} for u, k in known.items() if u not in connected]
    return JSONResponse({"switching": True, "current": config.current_device(), "devices": devices})


async def post_device(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    udid = str((await _json(request)).get("udid", ""))
    if not udid:
        return _err(LayoutError("no device given"))
    async with _device_lock:
        config.select_device(udid)
    return JSONResponse(view())


async def get_backups(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    s = session()
    return JSONResponse({"backups": list(reversed(s.backups())) if s.device_id else []})


async def post_restore(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    if _read_only():
        return _security_error("read-only mode: device writes are disabled", 403, "read_only")
    body = await _json(request)
    async with _device_lock:
        try:
            result = await session().restore(str(body.get("name", "")))
        except LayoutError as e:
            return _err(e)
        except Exception as e:
            return _err(RuntimeError(f"restore failed: {e}"), 502)
    return JSONResponse({"result": result, "view": view()})


async def get_icon(request: Request) -> Response:
    if (bad := _guard(request)):
        return bad
    bundle = request.query_params.get("id", "")
    lay = session().layout
    known = set()
    if lay is not None:
        for node in lay.nodes.values():
            if node.bundle_id:
                known.add(node.bundle_id)
            if node.kind == "widget":
                known.update(widget_apps(node.raw))
    if not bundle or bundle not in known:
        return Response(status_code=404)
    async with _icon_gate:
        try:
            png = await session().icon(bundle)
        except Exception:
            png = None
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
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
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
    url = f"http://127.0.0.1:{port}/"
    print(f"Screen Organizer is running at {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url + "#token=" + quote(TOKEN, safe=""),)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
