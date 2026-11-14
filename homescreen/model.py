"""Home Screen layout model.

Wraps the raw SpringBoard icon state (formatVersion 2: a list whose first element is
the dock and the rest are pages) in nodes with stable ids. Nodes keep their original
dicts; serializing moves those dicts around and never rebuilds them, so any keys we
don't understand (new iOS fields, widget config) go back to the phone untouched.
"""

from __future__ import annotations

import bisect
import copy
import hashlib
import json
import plistlib
from dataclasses import dataclass, field
from typing import Any

# Grid slots a widget takes on an iPhone page (4 columns x 6 rows = 24 slots).
WIDGET_SLOTS = {"small": 4, "medium": 8, "large": 16, "extralarge": 32}
WIDGET_DIMS = {"small": (2, 2), "medium": (4, 2), "large": (4, 4), "extralarge": (4, 4)}  # columns, rows
GRID_COLS, GRID_ROWS = 4, 6
DEFAULT_PAGE_SLOTS = 24
DEFAULT_DOCK_SLOTS = 4
DEFAULT_FOLDER_PAGE_SLOTS = 9


@dataclass(frozen=True)
class Grid:
    """A device's Home Screen grid. iPhone: 4 x 6, 9 per folder page, 4 in the dock. iPads differ
    (e.g. 5 x 6, 16 per folder page, a larger dock), and report it in their icon metrics."""
    cols: int = GRID_COLS
    rows: int = GRID_ROWS
    dock: int = DEFAULT_DOCK_SLOTS
    folder: int = DEFAULT_FOLDER_PAGE_SLOTS

    @property
    def slots(self) -> int:
        return self.cols * self.rows

    @classmethod
    def from_metrics(cls, m: dict[str, Any] | None) -> "Grid":
        m = m or {}
        try:
            return cls(int(m["homeScreenIconColumns"]), int(m["homeScreenIconRows"]),
                       int(m.get("homeScreenIconDockMaxCount", DEFAULT_DOCK_SLOTS)),
                       int(m.get("homeScreenIconFolderColumns", 3)) * int(m.get("homeScreenIconFolderRows", 3)))
        except (KeyError, TypeError, ValueError):
            return cls()

    def to_dict(self) -> dict[str, int]:
        return {"cols": self.cols, "rows": self.rows, "dock": self.dock, "folder": self.folder}


IPHONE = Grid()


class LayoutError(ValueError):
    """An edit that would produce an invalid or lossy layout."""


@dataclass
class Node:
    id: str
    kind: str  # app | webclip | folder | widget | other
    name: str
    raw: dict[str, Any]
    bundle_id: str | None = None
    slots: int = 1
    children: list[list["Node"]] = field(default_factory=list)  # folder pages

    def describe(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "kind": self.kind, "name": self.name}
        if self.kind == "widget":
            d["size"] = self.raw.get("gridSize")
        if self.kind == "folder":
            d["pages"] = [[c.id for c in page] for page in self.children]
        return d


def is_widget(raw: dict[str, Any]) -> bool:
    return ("gridSize" in raw or "elements" in raw or raw.get("iconType") == "custom"
            or raw.get("elementType") == "widget")


def is_folder(raw: dict[str, Any]) -> bool:
    """Real phones put an empty iconLists on widgets and some apps, so iconLists alone
    doesn't make a folder."""
    if raw.get("listType") == "folder":
        return True
    return "iconLists" in raw and not is_widget(raw) and raw.get("iconType") != "app"


def _kind(raw: dict[str, Any]) -> str:
    if is_widget(raw):
        return "widget"
    if is_folder(raw):
        return "folder"
    ident = str(raw.get("displayIdentifier") or raw.get("bundleIdentifier") or "")
    if "webClipURL" in raw or "webclip" in ident.lower() or raw.get("iconType") == "webClip":
        return "webclip"
    if raw.get("bundleIdentifier") or raw.get("displayIdentifier"):
        return "app"
    return "other"


def widget_apps(raw: dict[str, Any]) -> list[str]:
    """Bundle ids of the apps behind a widget (several for a Smart Stack)."""
    parts = raw.get("elements") or [raw]
    return [str(p.get("containerBundleIdentifier") or p.get("bundleIdentifier") or "")
            for p in parts if isinstance(p, dict)]


def _widget_name(raw: dict[str, Any]) -> str:
    if raw.get("displayName"):
        return str(raw["displayName"])
    apps = [b.split(".")[-1] for b in widget_apps(raw)]
    return ("stack: " if len(apps) > 1 else "") + ", ".join(apps) + " widget"


def _slots(raw: dict[str, Any], kind: str) -> int:
    if kind != "widget":
        return 1
    return WIDGET_SLOTS.get(str(raw.get("gridSize", "small")).lower(), 4)


class Layout:
    def __init__(self, raw_state: list[Any], page_slots: int = DEFAULT_PAGE_SLOTS,
                 dock_slots: int = DEFAULT_DOCK_SLOTS, source: list[Any] | None = None,
                 library: list[Any] | None = None, source_library: list[Any] | None = None,
                 bank: list[Any] | None = None, pins: dict[str, int] | None = None,
                 source_pins: dict[str, int] | None = None, grid: Grid | None = None,
                 identity: dict[str, Any] | None = None, source_identity: dict[str, Any] | None = None):
        """source / source_library: the state as read from the device, when raw_state is a saved draft.

        library: App Library-only apps (iOS stores these as "ignored"). None means the device
        interface doesn't expose them, so moving apps to the library isn't possible.
        """
        if not isinstance(raw_state, list) or not raw_state:
            raise LayoutError("icon state must be a non-empty list (dock, then pages)")
        self.grid = grid or IPHONE
        self.page_slots = self.grid.slots if grid else page_slots
        self.dock_slots = self.grid.dock if grid else dock_slots
        self.source = copy.deepcopy(raw_state if source is None else source)
        self.source_library = copy.deepcopy(library if source is None else source_library)
        self._counter: dict[str, int] = {}
        self.nodes: dict[str, Node] = {}
        self.dock = [self._wrap(r, "dock") for r in raw_state[0]]
        self.pages = [[self._wrap(r, f"p{i}") for r in page] for i, page in enumerate(raw_state[1:], 1)]
        self.library = None if library is None else [self._wrap(r, "library") for r in library]
        # App Bank: a draft-only tray for items set aside while rearranging. Never written.
        self.bank = [self._wrap(r, "bank") for r in bank or []]
        if identity is not None:
            self._restore_identity(identity)
        self.source_identity = copy.deepcopy(source_identity) if source_identity is not None else (
            self.identity(include_bank=False) if source is None else
            Layout(self.source, library=self.source_library, grid=self.grid).identity())
        # Gaps: items pinned to a grid cell (row * 4 + column) on their page. Unpinned items flow
        # into the free cells in order, like iOS does. iOS doesn't report pins over USB.
        self.pins: dict[str, int] = dict(pins or {})
        self.source_pins: dict[str, int] = dict((pins if source is None else source_pins) or {})

    def original(self) -> "Layout":
        return Layout(copy.deepcopy(self.source), self.page_slots, self.dock_slots,
                      library=copy.deepcopy(self.source_library),
                      pins=self.source_pins, grid=self.grid, identity=self.source_identity)

    def identity(self, include_bank: bool = True) -> dict[str, Any]:
        """Local IDs in wire serialization order, separate from untouched device fields."""
        ids = []

        def visit(nodes: list[Node]) -> None:
            for node in nodes:
                ids.append(node.id)
                for page in node.children:
                    visit(page)

        for nodes in [self.dock, *self.pages, self.library or [], self.bank if include_bank else []]:
            visit(nodes)
        return {"version": 1, "ids": ids, "counters": dict(self._counter)}

    def _restore_identity(self, identity: dict[str, Any]) -> None:
        try:
            ids, counters = identity["ids"], identity["counters"]
            if (identity["version"] != 1 or not isinstance(ids, list) or len(ids) != len(self.nodes)
                    or any(not isinstance(nid, str) or not nid for nid in ids) or len(set(ids)) != len(ids)
                    or not isinstance(counters, dict)
                    or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in counters.items())):
                raise ValueError("invalid identity sidecar")
        except (KeyError, TypeError, ValueError) as e:
            raise LayoutError("saved draft identity is invalid; call read again") from e
        nodes = list(self.nodes.values())
        for node, nid in zip(nodes, ids):
            node.id = nid
        self.nodes = {node.id: node for node in nodes}
        self._counter = dict(counters)

    def snapshot(self) -> dict[str, Any]:
        """A local memento includes independent current/source ownership and allocation state."""
        return copy.deepcopy({"state": self.to_state(), "library": self.to_library(), "bank": self.to_bank(),
                              "source": self.source, "source_library": self.source_library,
                              "pins": self.pins, "source_pins": self.source_pins,
                              "identity": self.identity(), "source_identity": self.source_identity,
                              "grid": self.grid.to_dict()})

    @classmethod
    def from_snapshot(cls, snap: dict[str, Any]) -> "Layout":
        return cls(snap["state"], source=snap["source"], library=snap.get("library"),
                   source_library=snap.get("source_library"), bank=snap.get("bank"),
                   pins=snap.get("pins"), source_pins=snap.get("source_pins"),
                   grid=Grid(**snap["grid"]) if "grid" in snap else Grid(),
                   identity=snap.get("identity"), source_identity=snap.get("source_identity"))

    def refold(self, folder: Node, items: list[Node] | None = None) -> None:
        """Lay a folder's items (or the given ones) out in full folder pages."""
        items = [c for p in folder.children for c in p] if items is None else items
        k = self.grid.folder
        folder.children = [items[i:i + k] for i in range(0, len(items), k)] or [[]]

    # ---- grid / gaps ----------------------------------------------------------------

    @staticmethod
    def dims(n: Node) -> tuple[int, int]:
        if n.kind == "widget":
            return WIDGET_DIMS.get(str(n.raw.get("gridSize", "small")).lower(), (2, 2))
        return (1, 1)

    def place(self, page: list[Node]) -> tuple[dict[str, tuple[int, int, int, int]], list[str]]:
        """Where each item lands on a page: {id: (row, col, width, height)} and any problems.
        Pinned items take their cell first; the rest fill free cells in order, row by row,
        widgets on column boundaries that fit their width."""
        grid = [[None] * self.grid.cols for _ in range(self.grid.rows)]
        out: dict[str, tuple[int, int, int, int]] = {}
        problems: list[str] = []

        def fits(r: int, c: int, w: int, h: int) -> bool:
            return r + h <= self.grid.rows and c + w <= self.grid.cols and all(
                grid[r + y][c + x] is None for y in range(h) for x in range(w))

        def put(n: Node, r: int, c: int, w: int, h: int) -> None:
            for y in range(h):
                for x in range(w):
                    grid[r + y][c + x] = n.id
            out[n.id] = (r, c, w, h)

        for n in sorted((n for n in page if n.id in self.pins), key=lambda n: self.pins[n.id]):
            w, h = self.dims(n)
            r, c = divmod(self.pins[n.id], self.grid.cols)
            if fits(r, c, w, h):
                put(n, r, c, w, h)
            else:
                problems.append(f"{n.name} can't be pinned at row {r + 1}, column {c + 1}")
        for n in page:
            if n.id in out or (n.id in self.pins and any(n.name in p for p in problems)):
                continue
            w, h = self.dims(n)
            spot = next(((r, c) for r in range(self.grid.rows) for c in range(0, self.grid.cols, w if w > 1 else 1)
                         if fits(r, c, w, h)), None)
            if spot:
                put(n, *spot, w, h)
            else:
                problems.append(f"no room for {n.name}")
        return out, problems

    def pin(self, node_id: str, page: int, cell: int) -> None:
        """Put an item at a grid cell (0 to cols*rows-1, row by row) on a page, leaving gaps around it."""
        node = self.get(node_id)
        if node.kind not in ("app", "webclip", "folder", "widget"):
            raise LayoutError(f"can't place {node.name} on the grid")
        if not 0 <= cell < self.grid.rows * self.grid.cols:
            raise LayoutError(f"cells are 0-{self.grid.slots - 1}")
        target = self._target(f"page:{page}")
        w, h = self.dims(node)
        r, c = divmod(cell, self.grid.cols)
        if r + h > self.grid.rows or c + w > self.grid.cols:
            raise LayoutError(f"{node.name} doesn't fit at row {r + 1}, column {c + 1}")
        mine = {(r + y, c + x) for y in range(h) for x in range(w)}
        for other in target:
            if other.id != node_id and other.id in self.pins:
                orow, ocol = divmod(self.pins[other.id], self.grid.cols)
                ow, oh = self.dims(other)
                if mine & {(orow + y, ocol + x) for y in range(oh) for x in range(ow)}:
                    raise LayoutError(f"that spot is taken by {other.name}")
        if node not in target:
            self.move(node_id, f"page:{page}")
        self.pins[node_id] = cell
        problems = self.place(self.pages[page - 1])[1]
        if problems:
            raise LayoutError(f"page {page} has no room for that: {problems[0]}")

    def unpin(self, node_id: str) -> None:
        self.pins.pop(node_id, None)

    def unpin_pages(self, start: int = 0) -> None:
        """Forget the gaps on pages start+1 and later (sorts re-flow those pages)."""
        on = {n.id for p in self.pages[start:] for n in p}
        self.pins = {k: v for k, v in self.pins.items() if k not in on}

    def tidy_pins(self) -> None:
        """Pins only mean something for items on a page."""
        on = {n.id for p in self.pages for n in p}
        self.pins = {k: v for k, v in self.pins.items() if k in on}

    # ---- parsing / serializing -------------------------------------------------

    def _new_id(self, prefix: str) -> str:
        n = self._counter.get(prefix, 0) + 1
        while f"{prefix}{n}" in self.nodes:
            n += 1
        self._counter[prefix] = n
        return f"{prefix}{n}"

    def _wrap(self, raw: Any, where: str) -> Node:
        if not isinstance(raw, dict):
            node = Node(self._new_id("other:"), "other", repr(raw)[:40], {"__value__": raw})
            self.nodes[node.id] = node
            return node
        kind = _kind(raw)
        name = str(raw.get("displayName") or raw.get("bundleIdentifier") or raw.get("displayIdentifier") or kind)
        if kind == "widget":
            name = _widget_name(raw)
        bundle = raw.get("bundleIdentifier") or raw.get("displayIdentifier")
        uid = raw.get("uniqueIdentifier") or (raw.get("displayIdentifier") if kind == "widget" else None)
        if kind in ("app", "webclip") and bundle and bundle not in self.nodes:
            nid = str(bundle)
        elif kind in ("widget", "folder") and uid and f"{kind}:{uid}" not in self.nodes and ":" not in str(uid):
            nid = f"{kind}:{uid}"  # iOS's own id: stays the same across undo, reloads and moves
        else:
            nid = self._new_id(f"{kind}:")
            while nid in self.nodes:
                nid = self._new_id(f"{kind}:")
        node = Node(nid, kind, name, raw, bundle_id=bundle if kind in ("app", "webclip") else None,
                    slots=_slots(raw, kind))
        self.nodes[nid] = node
        if kind == "folder":
            node.children = [[self._wrap(r, nid) for r in page] for page in raw.get("iconLists", [])]
        return node

    @staticmethod
    def _unwrap(node: Node) -> Any:
        if "__value__" in node.raw and len(node.raw) == 1:
            return node.raw["__value__"]
        if node.kind != "folder":
            return node.raw
        out = dict(node.raw)
        out["displayName"] = node.name
        out["iconLists"] = [[Layout._unwrap(c) for c in page] for page in node.children if page]
        return out

    def to_state(self) -> list[Any]:
        return [[self._unwrap(n) for n in self.dock]] + [[self._unwrap(n) for n in page] for page in self.pages]

    def to_bank(self) -> list[Any]:
        return [self._unwrap(n) for n in self.bank]

    def to_library(self) -> list[Any] | None:
        return None if self.library is None else [self._unwrap(n) for n in self.library]

    # ---- lookups ----------------------------------------------------------------

    def leaf_ids(self, state: list[Any] | None = None) -> list[str]:
        """Every app/webclip/widget id currently placed (including inside folders)."""
        out: list[str] = []

        def walk(nodes: list[Node]) -> None:
            for n in nodes:
                if n.kind == "folder":
                    for page in n.children:
                        walk(page)
                else:
                    out.append(n.id)

        walk(self.dock)
        for page in self.pages:
            walk(page)
        walk(self.library or [])
        walk(self.bank)
        return out

    def locate(self, node_id: str) -> tuple[list[Node], int]:
        """Return (container list, index) holding node_id."""
        containers: list[list[Node]] = [self.dock, *self.pages, self.bank,
                                        *([self.library] if self.library is not None else [])]
        for n in self.bank:
            if n.kind == "folder":
                containers.extend(n.children)
        for n in [*self.dock, *(x for p in self.pages for x in p)]:
            if n.kind == "folder":
                containers.extend(n.children)
        for c in containers:
            for i, n in enumerate(c):
                if n.id == node_id:
                    return c, i
        raise LayoutError(f"no item with id {node_id!r}")

    def widget_label(self, node: Node) -> str:
        """'Calendar' or 'Calendar, News, Photos' using the apps' own names where known."""
        if node.raw.get("displayName"):
            return str(node.raw["displayName"])
        names = []
        for b in widget_apps(node.raw):
            app = self.nodes.get(b)
            names.append(app.name if app else b.split(".")[-1].capitalize())
        return ", ".join(names)

    def get(self, node_id: str) -> Node:
        if node_id not in self.nodes:
            raise LayoutError(f"unknown id {node_id!r}")
        return self.nodes[node_id]

    def find(self, query: str) -> list[Node]:
        q = query.lower()
        return [n for n in self.nodes.values() if q in n.name.lower() or q in n.id.lower()]

    # ---- edits ------------------------------------------------------------------

    def _target(self, target: str) -> list[Node]:
        """'dock', 'library', 'page:N' (1-based) or 'folder:<id>[:N]'."""
        if target == "dock":
            return self.dock
        if target == "bank":
            return self.bank
        if target == "library":
            if self.library is None:
                raise LayoutError("this device interface doesn't expose the App Library, so apps can't be "
                                  "sent there (leaving them out just makes iOS put them back)")
            return self.library
        if target.startswith("page:"):
            idx = int(target.split(":", 1)[1])
            while idx > len(self.pages):
                if idx > len(self.pages) + 1:
                    raise LayoutError(f"page {idx} does not exist (there are {len(self.pages)})")
                self.pages.append([])
            if idx < 1:
                raise LayoutError("pages are numbered from 1")
            return self.pages[idx - 1]
        if target.startswith("folder:"):
            parts = target.split(":")
            fid = ":".join(parts[:2])
            page = int(parts[2]) if len(parts) > 2 else 1
            folder = self.get(fid)
            if folder.kind != "folder":
                raise LayoutError(f"{fid} is not a folder")
            while len(folder.children) < page:
                folder.children.append([])
            return folder.children[page - 1]
        raise LayoutError(f"bad target {target!r}; use dock, library, bank, page:N or folder:<id>[:N]")

    def move(self, node_id: str, target: str, index: int | None = None, before: str | None = None) -> None:
        """Move an item to target, at index (0-based, default end) or just before item `before`."""
        node = self.get(node_id)
        if before == node_id:
            return
        dest = self._target(target)
        if target.startswith("folder:") and node.kind in ("folder", "widget"):
            raise LayoutError(f"a {node.kind} can't go inside a folder")
        if target == "dock" and node.kind == "widget":
            raise LayoutError("widgets can't go in the dock")
        if target == "library" and node.kind not in ("app", "webclip"):
            raise LayoutError(f"only apps go in the App Library, not a {node.kind}")
        src, i = self.locate(node_id)
        src.pop(i)
        self.pins.pop(node_id, None)
        if before is not None:
            ids = [n.id for n in dest]
            if before not in ids:
                raise LayoutError(f"{before!r} is not in {target}")
            index = ids.index(before)
        dest.insert(len(dest) if index is None else max(0, min(index, len(dest))), node)

    def swap(self, a: str, b: str) -> None:
        """Trade the places of two items."""
        na, nb = self.get(a), self.get(b)
        ca, ia = self.locate(a)
        cb, ib = self.locate(b)
        for node, dest in ((na, cb), (nb, ca)):
            in_folder = any(dest is p for f in self.nodes.values() if f.kind == "folder" for p in f.children)
            if in_folder and node.kind in ("folder", "widget"):
                raise LayoutError(f"a {node.kind} can't go inside a folder")
            if dest is self.dock and node.kind == "widget":
                raise LayoutError("widgets can't go in the dock")
            if self.library is not None and dest is self.library and node.kind not in ("app", "webclip"):
                raise LayoutError(f"only apps go in the App Library, not a {node.kind}")
        ca[ia], cb[ib] = nb, na
        pa, pb = self.pins.pop(a, None), self.pins.pop(b, None)
        if pa is not None:
            self.pins[b] = pa
        if pb is not None:
            self.pins[a] = pb

    def insert_page(self, at: int) -> None:
        """Add an empty page so it becomes page `at` (1-based)."""
        if not 1 <= at <= len(self.pages) + 1:
            raise LayoutError(f"can't insert page {at}; there are {len(self.pages)}")
        self.pages.insert(at - 1, [])

    def move_page(self, page: int, to: int) -> None:
        n = len(self.pages)
        if not (1 <= page <= n and 1 <= to <= n):
            raise LayoutError(f"pages are 1..{n}")
        self.pages.insert(to - 1, self.pages.pop(page - 1))

    def delete_page(self, page: int) -> None:
        """Remove a page like iOS does: its apps (folders' too) go to the App Library."""
        if not 1 <= page <= len(self.pages):
            raise LayoutError(f"no page {page}")
        items = self.pages[page - 1]
        if items and self.library is None:
            raise LayoutError("page isn't empty and this device has no App Library to send its apps to")
        if any(n.kind == "widget" for n in items):
            raise LayoutError("move or remove the widgets on this page first")
        self.pins = {k: v for k, v in self.pins.items() if self.nodes[k] not in items}
        for n in list(items):
            for leaf in ([c for p in n.children for c in p] if n.kind == "folder" else [n]):
                self.library.append(leaf)
        self.pages.pop(page - 1)

    def dissolve_folder(self, folder_id: str) -> None:
        """Put a folder's apps where the folder was, in order, and remove the folder."""
        folder = self.get(folder_id)
        if folder.kind != "folder":
            raise LayoutError(f"{folder_id} is not a folder")
        c, i = self.locate(folder_id)
        c[i:i + 1] = [x for p in folder.children for x in p]
        self.pins.pop(folder_id, None)

    def create_folder(self, name: str, item_ids: list[str], target: str, index: int | None = None) -> str:
        if not item_ids:
            raise LayoutError("a folder needs at least one item")
        fid = self._new_id("folder:")
        while fid in self.nodes:
            fid = self._new_id("folder:")
        folder = Node(fid, "folder", name, {"displayName": name, "listType": "folder", "iconLists": []},
                      children=[[]])
        self.nodes[fid] = folder
        dest = self._target(target)
        dest.insert(len(dest) if index is None else index, folder)
        for item in item_ids:
            self.move(item, fid)
        return fid

    def rename_folder(self, folder_id: str, name: str) -> None:
        node = self.get(folder_id)
        if node.kind != "folder":
            raise LayoutError(f"{folder_id} is not a folder")
        node.name = name

    def arrange(self, pages: list[list[str]], dock: list[str] | None = None,
                library: list[str] | None = None) -> None:
        """Replace the whole top-level arrangement in one go.

        Every top-level item (app, folder, widget) and App Library app must appear exactly
        once across dock, pages and library. Folder contents stay as they are.
        dock / library omitted = unchanged.
        """
        dock = [n.id for n in self.dock] if dock is None else dock
        if library is not None and self.library is None:
            raise LayoutError("this device interface doesn't expose the App Library")
        lib_now = [n.id for n in self.library or []]
        library = lib_now if library is None else library
        wanted = dock + [i for p in pages for i in p] + library
        banked = [n.id for n in self.bank]
        current = [n.id for n in self.dock] + [n.id for p in self.pages for n in p] + lib_now + banked
        dupes = sorted({i for i in wanted if wanted.count(i) > 1})
        missing = sorted(set(current) - set(wanted) - set(banked))
        extra = sorted(set(wanted) - set(current))
        problems = []
        if dupes:
            problems.append(f"listed twice: {dupes}")
        if missing:
            problems.append(f"left out (iOS would put these back randomly): {missing}")
        if extra:
            problems.append(f"not top-level items (inside a folder, or unknown): {extra}")
        bad_lib = [i for i in library if self.nodes[i].kind not in ("app", "webclip")]
        if bad_lib:
            problems.append(f"only apps go in the App Library: {bad_lib}")
        if problems:
            raise LayoutError("; ".join(problems))
        old_page = {n.id: k for k, p in enumerate(self.pages) for n in p}
        self.dock = [self.nodes[i] for i in dock]
        self.pages = [[self.nodes[i] for i in p] for p in pages]
        if self.library is not None:
            self.library = [self.nodes[i] for i in library]
        self.bank = [n for n in self.bank if n.id not in set(wanted)]
        new_page = {i: k for k, p in enumerate(pages) for i in p}  # a pin survives only on the same page
        self.pins = {k: v for k, v in self.pins.items() if old_page.get(k) == new_page.get(k) is not None}

    def drop_empty(self) -> None:
        for n in [*self.dock, *(x for p in self.pages for x in p)]:
            if n.kind == "folder":
                n.children = [p for p in n.children if p]
        empties = [n for p in self.pages for n in p if n.kind == "folder" and not n.children]
        for f in empties:
            c, i = self.locate(f.id)
            c.pop(i)
        self.pages = [p for p in self.pages if p]
        self.tidy_pins()

    # ---- checks -----------------------------------------------------------------

    def validate(self) -> list[str]:
        """Problems that would make iOS reflow or drop things. Empty list = OK."""
        errs = []
        if self.bank:
            errs.append(f"the App Bank still holds {len(self.bank)} item(s); place them or send them to the App Library")
        if len(self.dock) > self.dock_slots:
            errs.append(f"dock has {len(self.dock)} items (max {self.dock_slots})")
        # a page shaped as the device laid it out (same widgets in the same places, the same number
        # of single-cell items) is trusted: iPad widget sizes don't map onto the icon grid the way
        # iPhone ones do, so a widget page can look "too full" here
        shape = lambda p: tuple(n.id if n.kind == "widget" else 1 for n in p)
        as_read = {shape(p) for p in self.original().pages}
        for i, page in enumerate(self.pages, 1):
            if not page:
                errs.append(f"page {i} is empty (iOS keeps it as a blank page; use remove_empty)")
            used = sum(n.slots for n in page)
            if used > self.page_slots and shape(page) not in as_read:
                errs.append(f"page {i} needs {used} slots (max {self.page_slots})")
            elif any(n.id in self.pins for n in page):
                errs += [f"page {i}: {p}" for p in self.place(page)[1]]
            for n in page:
                if n.kind == "folder":
                    for j, fp in enumerate(n.children, 1):
                        if len(fp) > self.grid.folder:
                            errs.append(f"folder {n.name!r} page {j} has {len(fp)} items (max {self.grid.folder})")
                        if any(c.kind in ("folder", "widget") for c in fp):
                            errs.append(f"folder {n.name!r} contains a folder or widget")
        before = sorted(self.original().leaf_ids())
        after = sorted(self.leaf_ids())
        if before != after:
            lost = sorted(set(before) - set(after))
            gained = sorted(set(after) - set(before))
            errs.append(f"item set changed: lost {lost}, gained {gained}")
        return errs

    # ---- views ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        def row(nodes: list[Node]) -> list[dict[str, Any]]:
            out = []
            for n in nodes:
                d: dict[str, Any] = {"id": n.id, "name": n.name}
                if n.kind == "webclip":
                    d["webclip"] = True
                if n.kind == "folder":
                    d["folder"] = [[{"id": c.id, "name": c.name} for c in p] for p in n.children]
                elif n.kind == "widget":
                    d["widget"] = n.raw.get("gridSize", "?")
                if n.id in self.pins:
                    r, c = divmod(self.pins[n.id], self.grid.cols)
                    d["placed_at"] = {"row": r + 1, "column": c + 1}
                out.append(d)
            return out

        placed = set(self.leaf_ids())
        return {
            "dock": row(self.dock),
            "pages": {str(i): row(p) for i, p in enumerate(self.pages, 1)},
            "app_library_only": "not available on this device interface" if self.library is None
                                else row(self.library),
            "app_bank": row(self.bank),
            "counts": {
                "pages": len(self.pages),
                "app_library_only": None if self.library is None else len(self.library),
                "apps": sum(1 for n in self.nodes.values() if n.kind in ("app", "webclip") and n.id in placed),
                "folders": sum(1 for p in self.pages for n in p if n.kind == "folder"),
                "widgets": sum(1 for p in self.pages for n in p if n.kind == "widget"),
            },
        }

    def positions(self) -> dict[str, str]:
        """id -> human-readable position, for diffs."""
        pos: dict[str, str] = {}

        def walk(nodes: list[Node], where: str) -> None:
            for i, n in enumerate(nodes, 1):
                pos[n.id] = f"{where} #{i}"
                if n.kind == "folder":
                    for j, page in enumerate(n.children, 1):
                        walk(page, f"folder {n.name!r} p{j}")

        walk(self.dock, "dock")
        for i, page in enumerate(self.pages, 1):
            walk(page, f"page {i}")
        for n in self.library or []:
            pos[n.id] = "App Library"
        for n in self.bank:
            pos[n.id] = "App Bank"
        return pos

    def containers(self) -> dict[str, list[str]]:
        """container name -> ordered ids in it."""
        out: dict[str, list[str]] = {}

        def walk(nodes: list[Node], where: str) -> None:
            out[where] = [n.id for n in nodes]
            for n in nodes:
                if n.kind == "folder":
                    for j, page in enumerate(n.children, 1):
                        walk(page, f"folder {n.name!r} p{j}")

        walk(self.dock, "dock")
        for i, page in enumerate(self.pages, 1):
            walk(page, f"page {i}")
        if self.library is not None:
            out["App Library"] = [n.id for n in self.library]
        if self.bank:
            out["App Bank"] = [n.id for n in self.bank]
        return out

    def diff(self) -> dict[str, Any]:
        """Items that really moved: changed container, or broke relative order within one.
        Icons that only slid over because a neighbor moved don't count."""
        src = self.original()
        old, new = src.positions(), self.positions()
        old_c, new_c = src.containers(), self.containers()
        where_old = {i: c for c, ids in old_c.items() for i in ids}
        moved: dict[str, str] = {}
        for c, ids in new_c.items():
            stayed = [i for i in ids if where_old.get(i) == c]
            keep = set(stayed) if c in ("App Library", "App Bank") else _lcs(old_c.get(c, []), stayed)
            for i in ids:
                if i in old and i not in keep and not i.startswith(("folder:", "widget:")):
                    moved[i] = f"{old[i]} -> {new[i]}"
        names = {k: self.nodes[k].name for k in moved}
        return {
            "items_moved": len(moved),
            "pages_before": len(self.source) - 1,
            "pages_after": len(self.pages),
            "to_app_library": sum(1 for v in moved.values() if v.endswith("-> App Library")),
            "gaps_changed": len({k for k in set(self.pins) | set(self.source_pins)
                                 if self.pins.get(k) != self.source_pins.get(k)}),
            "moves": {f"{names[k]} [{k}]": v for k, v in sorted(moved.items(), key=lambda kv: kv[1])},
            "problems": self.validate(),
            "apply_token": self.token(),
        }

    def token(self) -> str:
        """Short hash of the exact state that would be written."""
        blob = plistlib.dumps({"state": self.to_state(), "library": self.to_library() or [],
                               "pins": {k: v for k, v in sorted(self.pins.items())}},
                              fmt=plistlib.FMT_BINARY, sort_keys=True)
        return "apply-" + hashlib.sha256(blob).hexdigest()[:10]


def _lcs(a: list[str], b: list[str]) -> set[str]:
    """Ids in the longest common subsequence of a and b (both duplicate-free)."""
    pos = {x: i for i, x in enumerate(a)}
    seq = [pos[x] for x in b if x in pos]
    # longest increasing subsequence over positions, with back-pointers
    tails: list[int] = []
    tail_idx: list[int] = []
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        k = bisect.bisect_left(tails, v)
        if k == len(tails):
            tails.append(v)
            tail_idx.append(i)
        else:
            tails[k] = v
            tail_idx[k] = i
        prev[i] = tail_idx[k - 1] if k else -1
    out: set[str] = set()
    i = tail_idx[-1] if tail_idx else -1
    while i >= 0:
        out.add(a[seq[i]])
        i = prev[i]
    return out


def widget_wire_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a single widget's exported element identity onto its root-archive alias."""
    fields = dict(raw)
    if "uniqueIdentifier" not in fields and fields.get("userSelectedElementIdentifier") and "elements" not in fields:
        fields["uniqueIdentifier"] = fields["userSelectedElementIdentifier"]
    return fields


def widget_configuration(raw: dict[str, Any]) -> str:
    """Compare every exported widget field, including configuration and stack elements.

    USB may attach an empty iconLists which the root archive omits. Private device
    configuration that is never exported cannot be verified by this fingerprint.
    """
    fields = {k: v for k, v in widget_wire_fields(raw).items() if k != "iconLists" or v}
    return plistlib.dumps(fields, fmt=plistlib.FMT_BINARY, sort_keys=True).hex()


def structure(state: list[Any], library: list[Any] | None = None) -> str:
    """Comparable layout fingerprint, including all exported widget configuration."""
    lay = Layout(state, library=library)

    def key(n: Node) -> Any:
        if n.kind == "folder":
            return [n.name, [shape(p) for p in n.children]]
        if n.kind == "widget":  # generated model ids are positional
            return "widget:" + widget_configuration(n.raw)
        return n.id

    def shape(nodes: list[Node]) -> list[Any]:
        return [key(n) for n in nodes]

    lib = None if lay.library is None else sorted(shape(lay.library))
    return json.dumps({"layout": [shape(lay.dock)] + [shape(p) for p in lay.pages], "library": lib})
