"""Draft, backups and the guarded apply.

Rules:
- Edits only ever touch the draft. The phone is written by apply() and restore() alone.
- apply() needs the token from the latest diff, refuses if the draft has problems or the
  phone changed since it was read, backs up first, verifies after, and never retries.
"""

from __future__ import annotations

import copy
import asyncio
import datetime as dt
import fcntl
import json
import os
import uuid
import plistlib
from contextlib import contextmanager
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from . import colors
from .device import ObservedPins, Source, disk_pins, require_writable
from .model import Grid, Layout, LayoutError, Node, is_widget, structure, widget_configuration

KEEP_READS = 10
MAX_UNDO = 200
OPERATION_TIMEOUT = 30.0


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _exclusive_file(path: Path, data: bytes) -> None:
    """Create a private file without overwriting another operation's recovery data."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _private_replace(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _exclusive_file(temporary, data)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _pack(state: list[Any], library: list[Any] | None, pins: list[dict[str, int]] | None = None) -> bytes:
    blob: dict[str, Any] = {"state": state}
    if library is not None:
        blob["library"] = library
    if pins:
        blob["pins"] = pins
    return plistlib.dumps(blob, fmt=plistlib.FMT_BINARY)


def _unpack(data: bytes) -> tuple[list[Any], list[Any] | None]:
    blob = plistlib.loads(data)
    if isinstance(blob, list):  # backups from before the App Library was tracked
        return blob, None
    return blob["state"], blob.get("library")


def _unpack_pins(data: bytes) -> list[dict[str, int]] | None:
    blob = plistlib.loads(data)
    return blob.get("pins") if isinstance(blob, dict) else None


# ---- gaps (pinned cells) --------------------------------------------------------------
# In the dict format a pinned item is named by its identifier: bundle id for apps, the icon's
# uniqueIdentifier for widgets and folders.

def pin_key(node: Node) -> str | None:
    if node.kind in ("app", "webclip"):
        return node.bundle_id or node.id
    if node.kind == "widget":
        return node.raw.get("uniqueIdentifier") or node.raw.get("userSelectedElementIdentifier")
    if node.kind == "folder":
        return node.raw.get("uniqueIdentifier")
    return None


def memory_key(node: Node) -> str | None:
    """Folders can omit UUIDs on readback; their name and ordered contents identify gaps."""
    if node.kind == "folder":
        return "folder-content:" + json.dumps([node.name, [[_node_structure(child) for child in page]
                                                         for page in node.children]], separators=(",", ":"))
    return pin_key(node)


def memory_pins(lay: Layout) -> dict[str, Any]:
    return {"version": 2, "grid": lay.grid.to_dict(),
            "pages": [{key: lay.pins[node.id] for node in page
                       if node.id in lay.pins and (key := memory_key(node))} for page in lay.pages]}


def pins_by_page(lay: Layout) -> list[dict[str, int]]:
    """Per page, {identifier: cell} for the dict format. Pinned folders without an id get one."""
    out = []
    for page in lay.pages:
        d = {}
        for n in page:
            if n.id not in lay.pins:
                continue
            if n.kind == "folder" and not n.raw.get("uniqueIdentifier"):
                n.raw["uniqueIdentifier"] = str(uuid.uuid4()).upper()
            key = pin_key(n)
            if not key:
                raise LayoutError(f"{n.kind} {n.name!r} has no wire identifier for its pin; use ordinary page order")
            if sum(pin_key(item) == key for item in page) != 1:
                raise LayoutError("ambiguous pin identifier on a layout page")
            d[key] = lay.pins[n.id]
        out.append(d)
    return out


def node_pins(lay: Layout, keyed: dict[str, Any] | list[dict[str, int]], by_memory: bool = False,
              strict: bool = False) -> dict[str, int]:
    """Resolve wire pins. Reads may discard stale gaps; write comparison must reject them."""
    if isinstance(keyed, ObservedPins):
        try:
            keyed.validate([{pin_key(n) for n in page} for page in lay.pages], lay.grid)
        except LayoutError:
            if strict:
                raise
            lay.pins = {}
            return lay.pins
        keyed = keyed.pages(len(lay.pages))
    saved_grid = lay.grid
    if by_memory and isinstance(keyed, dict) and "version" in keyed:
        try:
            if keyed["version"] != 2:
                raise ValueError("unsupported gap memory version")
            saved_grid = Grid(**keyed["grid"])
            if any(type(value) is not int or value <= 0 for value in saved_grid.to_dict().values()):
                raise ValueError("invalid gap memory grid")
            keyed = keyed["pages"]
            if not isinstance(keyed, list):
                raise ValueError("invalid gap memory pages")
        except (KeyError, TypeError, ValueError):
            if strict:
                raise LayoutError("invalid saved pin memory")
            lay.pins = {}
            return lay.pins
    scoped = isinstance(keyed, list)
    if scoped and any(not isinstance(page, dict) for page in keyed):
        raise LayoutError("pins must identify items on each page")
    if strict and scoped and any(keyed[len(lay.pages):]):
        raise LayoutError("pin identifiers do not match the layout's page items")
    wanted: dict[str, int] = {}
    key_for = memory_key if by_memory else pin_key
    global_nodes = [n for page in lay.pages for n in page]
    matched_global = set()
    for index, page in enumerate(lay.pages):
        values = (keyed[index] if index < len(keyed) else {}) if scoped else keyed
        candidates = page if scoped else global_nodes
        identities = Counter(key_for(n) for n in candidates)
        names = Counter(n.name for n in candidates if n.kind == "folder")
        matched = set()
        invalid = False
        for n in page:
            key = key_for(n)
            if by_memory:
                # Older global files used child model IDs or just a folder name. An
                # enumeration can change on reorder, so require a unique folder name.
                if not scoped and key not in values and n.kind == "folder" and names[n.name] == 1:
                    legacy = "folder-content:" + json.dumps(
                        [n.name, [[child.id for child in p] for p in n.children]], separators=(",", ":"))
                    key = legacy if legacy in values else f"folder:{n.name}"
            if key and key in values:
                if identities[key_for(n)] != 1:
                    if strict:
                        raise LayoutError("ambiguous pin identifier on a layout page")
                    continue
                cell = values[key]
                if type(cell) is not int or not 0 <= cell < saved_grid.slots:
                    if strict:
                        raise LayoutError(f"invalid pin cell for {n.name!r} on the loaded grid")
                    invalid = True
                    continue
                row, column = divmod(cell, saved_grid.cols)
                width, height = lay.dims(n)
                if (column + width > min(saved_grid.cols, lay.grid.cols)
                        or row + height > min(saved_grid.rows, lay.grid.rows)):
                    if strict:
                        raise LayoutError("saved pin footprint does not fit the loaded grid")
                    invalid = True
                    continue
                wanted[n.id] = row * lay.grid.cols + column
                matched.add(key)
        if strict and scoped and matched != set(values):
            raise LayoutError("pin identifiers do not match the layout's page items")
        if invalid:
            for n in page:
                wanted.pop(n.id, None)
        matched_global.update(matched)
    if strict and not scoped and matched_global != set(keyed):
        raise LayoutError("pin identifiers do not match the layout's page items")
    lay.pins = wanted
    for page in lay.pages:  # a page whose remembered gaps no longer fit loses them
        if any(n.id in wanted for n in page) and lay.place(page)[1]:
            if strict:
                raise LayoutError(f"pins do not fit the loaded grid: {lay.place(page)[1]}")
            for n in page:
                wanted.pop(n.id, None)
    return wanted


def _node_structure(node: Node) -> Any:
    if node.kind == "folder":
        return [node.name, [[_node_structure(child) for child in page] for page in node.children]]
    if node.kind == "widget":
        return "widget:" + widget_configuration(node.raw)
    return node.bundle_id or node.id


def _pin_intents(lay: Layout) -> set[str]:
    """Explicit anchors by page, coordinate and the same semantics used for layout comparison."""
    return {json.dumps([index, divmod(lay.pins[node.id], lay.grid.cols), _node_structure(node)])
            for index, page in enumerate(lay.pages) for node in page if node.id in lay.pins}


def placed_structure(state: list[Any], library: list[Any] | None,
                     keyed_pins: dict[str, Any] | list[dict[str, int]] | None,
                     grid: Grid | None = None, by_memory: bool = False) -> str:
    """Like model.structure, but each page lists its items in grid order (row by row) after
    gaps are applied. iOS reorders a page's list by position when it has gaps, so list order
    alone would report a correct write as a mismatch."""
    lay = Layout(state, library=library, grid=grid)
    if keyed_pins is not None:
        node_pins(lay, keyed_pins, by_memory=by_memory, strict=True)

    pages = []
    for page in lay.pages:
        pos, _ = lay.place(page)
        order = sorted(page, key=lambda n: (pos[n.id][0], pos[n.id][1]) if n.id in pos else (99, 0))
        pages.append([[_node_structure(n), pos.get(n.id, (0, 0))[:2]] for n in order])
    return json.dumps({"dock": [_node_structure(n) for n in lay.dock], "pages": pages,
                       "library": None if library is None else sorted(n.id for n in lay.library or [])},
                      default=list)


def _widgets(state: list[Any]) -> list[dict[str, Any]]:
    """Every top-level widget dict in a list-format state (widgets can't be in folders)."""
    return [x for page in state for x in page
            if isinstance(x, dict) and is_widget(x)]


def same_reported_pins(first: dict[str, int] | None, second: dict[str, int] | None) -> bool:
    if isinstance(first, ObservedPins) or isinstance(second, ObservedPins):
        return (isinstance(first, ObservedPins) and isinstance(second, ObservedPins)
                and first.archive == second.archive)
    return first == second


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in s)


class Session:
    def __init__(self, source: Source, data_dir: str | Path, device: str | None = None):
        """device: keep this session's draft per device (the app switches between devices);
        None keeps the single shared draft the CLI and MCP server use."""
        self.source = source
        self.dir = Path(data_dir)
        if self.dir.is_symlink():
            raise LayoutError("the Session data directory must not be a symlink")
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
        self._draft_key = device
        self.layout: Layout | None = None
        self._draft_error: str | None = None
        self.device_id: str | None = device
        self.info: dict[str, Any] = {}
        self.grid = Grid()
        self._observed_placement: ObservedPins | None = None
        self.usage: dict[str, Any] | None = None  # Screen Time opens, loaded on request
        self._undo: list[dict[str, Any]] = []
        self._redo: list[dict[str, Any]] = []
        self._load_draft()

    # ---- persistence ------------------------------------------------------------

    @contextmanager
    def _exclusive(self):
        fd = os.open(self.dir / ".session.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise LayoutError("another Session operation holds the local data lock; try again after it finishes") from e
            yield
        finally:
            os.close(fd)

    @property
    def _pending_path(self) -> Path:
        return self.dir / ".pending-write.json"

    def _require_known(self) -> None:
        if self._pending_path.exists():
            raise LayoutError("a pending write has an unknown outcome; perform an explicit read before any further write")

    def _clear_pending(self) -> None:
        if self._pending_path.exists():
            self._pending_path.unlink()
            _sync_directory(self.dir)

    async def _assert_identity(self) -> None:
        info = await asyncio.wait_for(self.source.info(), OPERATION_TIMEOUT)
        actual = info.get("UniqueDeviceID")
        if not isinstance(actual, str) or not actual or actual != self.device_id:
            raise LayoutError("device identity changed or is unavailable; explicitly read the intended device again")
        if Grid.from_metrics(info.get("icon_metrics")) != self.grid:
            raise LayoutError("device grid changed; explicitly read the intended device again")

    @property
    def draft_path(self) -> Path:
        if self._draft_key:
            (self.dir / "drafts").mkdir(exist_ok=True)
            return self.dir / "drafts" / f"{_safe(self._draft_key)}.plist"
        return self.dir / "draft.plist"

    def _dev_dir(self, kind: str) -> Path:
        p = self.dir / kind / _safe(self.device_id or "unknown")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _save_draft(self) -> None:
        assert self.layout is not None
        blob = {"device": self.device_id, "info": self.info, **self.layout.snapshot()}
        if self._observed_placement is not None:
            blob["source_placement"] = self._observed_placement.archive
        blob = {k: v for k, v in blob.items() if v is not None}  # plists have no null
        _private_replace(self.draft_path, plistlib.dumps(blob, fmt=plistlib.FMT_BINARY))

    def _load_draft(self) -> None:
        if self.draft_path.exists():
            blob = plistlib.loads(self.draft_path.read_bytes())
            self.device_id = blob["device"]
            self.info = blob.get("info", {})
            self.grid = Grid(**blob["grid"]) if "grid" in blob else Grid()
            self._observed_placement = disk_pins(blob["source_placement"]) if "source_placement" in blob else None
            try:
                lay = Layout.from_snapshot(blob)
                original = lay.original()  # validate source identity independently
                for view in (lay, original):
                    if set(view.pins) - view.nodes.keys():
                        raise LayoutError("saved draft pin identity is unavailable; call read again")
                if "identity" not in blob or "source_identity" not in blob:
                    # Legacy enumeration IDs cannot prove ownership after reordering.
                    for view in (lay, original):
                        for nid in view.pins:
                            node = view.nodes[nid]
                            wire = node.raw.get("uniqueIdentifier") or (
                                node.raw.get("displayIdentifier") if node.kind == "widget" else None)
                            stable = (node.bundle_id == nid if node.kind in ("app", "webclip") else
                                      wire is not None and nid == f"{node.kind}:{wire}")
                            # A numeric wire suffix can collide with a generated ID from
                            # another node before the legacy draft was reordered.
                            prefix, _, suffix = nid.partition(":")
                            if prefix in {"app", "webclip", "widget", "folder", "other"} and suffix.isdecimal():
                                stable = False
                            stable &= sum(pin_key(other) == pin_key(node) for other in view.nodes.values()) == 1
                            if not stable:
                                raise LayoutError("saved draft pin identity is ambiguous; call read again")
                self.layout = lay
            except LayoutError as e:
                self._draft_error = str(e)

    def require(self) -> Layout:
        if self.layout is None:
            raise LayoutError(self._draft_error or "no layout loaded yet; call read first")
        return self.layout

    def edited(self) -> None:
        self._save_draft()

    # ---- edits with undo ----------------------------------------------------------

    def _snap(self) -> dict[str, Any]:
        return self.require().snapshot()

    def _load_snap(self, snap: dict[str, Any]) -> None:
        self.layout = Layout.from_snapshot(copy.deepcopy(snap))

    def edit(self, fn: Callable[[Layout], Any]) -> Any:
        """Run fn on the draft as one undoable step. If it raises, the draft is left untouched."""
        before = copy.deepcopy(self.require())
        try:
            out = fn(self.require())
            self._check_placement()
            self._prepare_widget_pins(self.require())
        except Exception:
            self.layout = before
            raise
        self._undo.append(before.snapshot())
        del self._undo[:-MAX_UNDO]
        self._redo.clear()
        self._save_draft()
        return out

    def _check_placement(self) -> None:
        if str(self.info.get("ProductVersion", "")).split(".", 1)[0] != "16":
            return
        lay = self.require()
        if any(lay.source_pins.get(node) != cell for node, cell in lay.pins.items()):
            raise LayoutError("iOS 16 does not preserve fixed icon positions; use ordinary page order")
        leading_widgets = set()
        for page in lay.original().pages:
            for node in page:
                if node.kind != "widget":
                    break
                leading_widgets.add(node.id)
        for page in lay.pages:
            folder_seen = False
            for node in page:
                folder_seen |= node.kind == "folder"
                if folder_seen and node.id in leading_widgets:
                    raise LayoutError("iOS 16 keeps these widgets ahead of folders; place the folder after the widgets")

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._snap())
        self._load_snap(self._undo.pop())
        self._save_draft()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._snap())
        self._load_snap(self._redo.pop())
        self._save_draft()
        return True

    def load_usage(self, days: int = 30) -> dict[str, Any]:
        from . import usage
        lay = self.require()
        known = {n.bundle_id for n in lay.nodes.values() if n.bundle_id}
        self.usage = usage.load(known, days)
        return self.usage

    async def load_device_usage(self) -> dict[str, Any]:
        """Usage from the device's own memory reports (no Screen Time sync needed)."""
        from . import usage
        lay = self.require()
        if not hasattr(self.source, "memory_reports"):
            self.usage = {"ok": False, "error": "This source can't report usage."}
            return self.usage
        files = await self.source.memory_reports(self._dev_dir("usage"))
        apps = await self.source.installed_apps()
        known = {n.bundle_id for n in lay.nodes.values() if n.bundle_id}
        self.usage = usage.from_memory_reports(files, apps, known)
        return self.usage

    def history(self) -> dict[str, int]:
        return {"undo": len(self._undo), "redo": len(self._redo)}

    # ---- read -------------------------------------------------------------------

    # ---- widget element ids -------------------------------------------------------------
    # A widget's link to its configuration is its element id (userSelectedElementIdentifier over
    # USB). iOS can drop it, so every id ever seen is remembered per widget (by displayIdentifier)
    # and put back on write.

    @property
    def _widget_ids_path(self) -> Path:
        return self._dev_dir("widgets") / "element-ids.json"

    def _remember_widget_ids(self, state: list[Any]) -> None:
        import json
        known = json.loads(self._widget_ids_path.read_text()) if self._widget_ids_path.exists() else {}
        for w in _widgets(state):
            if w.get("displayIdentifier") and w.get("userSelectedElementIdentifier"):
                known[w["displayIdentifier"]] = w["userSelectedElementIdentifier"]
        self._widget_ids_path.write_text(json.dumps(known, indent=1, sort_keys=True))

    def _restore_widget_ids(self, state: list[Any]) -> list[Any]:
        import json
        if not self._widget_ids_path.exists():
            return state
        known = json.loads(self._widget_ids_path.read_text())
        state = copy.deepcopy(state)
        for w in _widgets(state):
            if not w.get("userSelectedElementIdentifier") and known.get(w.get("displayIdentifier")):
                w["userSelectedElementIdentifier"] = known[w["displayIdentifier"]]
        return state

    def _prepare_widget_pins(self, lay: Layout) -> None:
        for node in lay.nodes.values():
            if node.kind == "widget" and node.id in lay.pins and not pin_key(node):
                node.raw = self._restore_widget_ids([[node.raw]])[0][0]
                if not pin_key(node):
                    raise LayoutError(f"widget {node.name!r} has no known wire identifier; use ordinary page order")

    def _prepared_layout(self, lay: Layout) -> tuple[Layout, list[dict[str, int]]]:
        """Prepare an isolated snapshot so wire ids never change draft ids, pins or history."""
        prepared = copy.deepcopy(lay)
        self._prepare_widget_pins(prepared)
        pins = pins_by_page(prepared)
        return prepared, pins

    # ---- web clip memory -----------------------------------------------------------------
    # Web clips kept off the Home Screen aren't installed apps, so the installed-app list can't
    # put them in the App Library, and USB never reports them. A write that leaves one out makes
    # iOS put it in the first free cell on a page. So every web clip ever seen is remembered per
    # phone, and one that isn't on the Home Screen is sent in the App Library.

    @property
    def _webclips_path(self) -> Path:
        return self._dev_dir("webclips") / "seen.json"

    def _known_webclips(self) -> dict[str, dict[str, str]]:
        return json.loads(self._webclips_path.read_text()) if self._webclips_path.exists() else {}

    def _remember_webclips(self, state: list[Any], library: list[Any] | None) -> None:
        known = self._known_webclips()
        before = len(known)
        for n in Layout(state, library=library).nodes.values():
            if n.kind == "webclip":
                known[n.id] = {k: n.raw[k] for k in ("displayIdentifier", "displayName", "webClipURL")
                               if isinstance(n.raw.get(k), str)}
        if len(known) != before:
            self._webclips_path.write_text(json.dumps(known, indent=1, sort_keys=True))

    def _with_webclips(self, state: list[Any], library: list[Any] | None) -> list[Any] | None:
        """library plus every remembered web clip that isn't on the Home Screen."""
        if library is None:
            return None
        here = {n.id for n in Layout(state, library=library).nodes.values()}
        return library + [dict(w) for i, w in sorted(self._known_webclips().items()) if i not in here]

    async def _read_source(self) -> tuple[list[Any], list[Any] | None]:
        state, library = await asyncio.wait_for(self.source.read(), OPERATION_TIMEOUT)
        self._remember_widget_ids(state)
        self._remember_webclips(state, library)
        return state, self._with_webclips(state, library)

    # ---- gap memory ----------------------------------------------------------------------
    # The phone never reports gaps over USB, and a write without them closes every gap. So the
    # gaps set in Homescreen are remembered per phone and sent again on every write.

    @property
    def _gaps_path(self) -> Path:
        return self._dev_dir("gaps") / "pins.json"

    def _remember_gaps(self, lay: Layout) -> None:
        self._gaps_path.write_text(json.dumps(memory_pins(lay), indent=1, sort_keys=True))

    def _layout_from(self, state: list[Any], library: list[Any] | None) -> Layout:
        """A Layout with gaps: from the source when it can report them, else from memory."""
        lay = Layout(state, library=library, grid=self.grid)
        reported = getattr(self.source, "pins", None)
        if reported is not None:
            node_pins(lay, reported)
        elif self._gaps_path.exists():
            node_pins(lay, json.loads(self._gaps_path.read_text()), by_memory=True)
        return Layout(state, library=library, pins=lay.pins, grid=self.grid)

    @property
    def gaps_readable(self) -> bool:
        return getattr(self.source, "pins", None) is not None

    async def read(self) -> dict[str, Any]:
        with self._exclusive():
            return await self._read_locked()

    async def _read_locked(self) -> dict[str, Any]:
        info = await asyncio.wait_for(self.source.info(), OPERATION_TIMEOUT)
        device_id = info.get("UniqueDeviceID")
        if not isinstance(device_id, str) or not device_id:
            raise LayoutError("device identity is unavailable; no layout was loaded")
        grid = Grid.from_metrics(info.get("icon_metrics"))
        state, library = await asyncio.wait_for(self.source.read(), OPERATION_TIMEOUT)
        verified = await asyncio.wait_for(self.source.info(), OPERATION_TIMEOUT)
        if verified.get("UniqueDeviceID") != device_id:
            raise LayoutError("device identity changed during read; no layout was loaded")
        if Grid.from_metrics(verified.get("icon_metrics")) != grid:
            raise LayoutError("device grid changed during read; no layout was loaded")
        Layout(state, library=library, grid=grid)  # validate the shape before committing identity
        previous = self.device_id, self.info, self.grid, self.layout, self._undo, self._redo, self._observed_placement
        try:
            self.device_id = device_id
            self.info = {k: v for k, v in info.items() if k in ("DeviceName", "ProductType", "ProductVersion")}
            self.grid = grid
            self._remember_widget_ids(state)
            self._remember_webclips(state, library)
            library = self._with_webclips(state, library)
            reads = self._dev_dir("reads")
            _private_replace(reads / f"{_stamp()}.plist", _pack(state, library))
            for old in sorted(reads.glob("*.plist"))[:-KEEP_READS]:
                old.unlink()
            self.layout = self._layout_from(state, library)
            reported = getattr(self.source, "pins", None)
            self._observed_placement = copy.deepcopy(reported) if isinstance(reported, ObservedPins) else None
            self._undo, self._redo = [], []
            self._save_draft()
            if self._pending_path.exists():
                pending = json.loads(self._pending_path.read_text())
                if pending.get("device_id") == self.device_id:
                    self._clear_pending()
        except BaseException:
            self.device_id, self.info, self.grid, self.layout, self._undo, self._redo, self._observed_placement = previous
            raise
        return {"device": {k: v for k, v in info.items() if k != "UniqueDeviceID"},
                **self.layout.summary()["counts"]}

    def reset(self) -> None:
        def restore_original(lay: Layout) -> None:
            self.layout = lay.original()

        self.edit(restore_original)

    # ---- icons ------------------------------------------------------------------

    async def icon(self, bundle_id: str) -> bytes | None:
        cache = self._dev_dir("icons") / f"{_safe(bundle_id)}.png"
        if cache.exists():
            return cache.read_bytes()
        png = await self.source.icon_png(bundle_id)
        if png:
            cache.write_bytes(png)
        return png

    async def icon_colors(self, ids: list[str] | None = None) -> dict[str, Any]:
        lay = self.require()
        ids = ids or [i for i in lay.leaf_ids() if lay.nodes[i].bundle_id]
        out: dict[str, Any] = {}
        for i in ids:
            node = lay.get(i)
            png = await self.icon(node.bundle_id) if node.bundle_id else None
            out[i] = {"name": node.name, **(colors.dominant(png) if png else {"family": "unknown"})}
        return out

    # ---- write ------------------------------------------------------------------

    def _backup(self, state: list[Any], library: list[Any] | None, label: str,
                pins: list[dict[str, int]] | None = None) -> Path:
        if not self.device_id:
            raise LayoutError("device identity is required for a backup")
        p = self._dev_dir("backups") / f"{_stamp()}-{label}-{uuid.uuid4().hex}.plist"
        blob = plistlib.loads(_pack(state, library, pins))
        blob["provenance"] = {"version": 1, "device_id": self.device_id, "grid": self.grid.to_dict()}
        _exclusive_file(p, plistlib.dumps(blob, fmt=plistlib.FMT_BINARY))
        # The device and backups directories may have been created by this call.
        _sync_directory(p.parent.parent)
        _sync_directory(self.dir)
        return p

    async def _write_verified(self, target: list[Any], target_lib: list[Any] | None,
                              label: str, pins: list[dict[str, int]] | None = None,
                              expected: tuple[list[Any], list[Any] | None] | None = None,
                              expected_pins: dict[str, int] | list[dict[str, int]] | None = None) -> dict[str, Any]:
        # Invalid requested pins must fail before a backup, pending marker or source write.
        want_pins = pins or []
        placed_structure(target, target_lib, want_pins, self.grid)
        await self._assert_identity()
        current, current_lib = await self._read_source()
        baseline = expected if expected is not None else (current, current_lib)
        baseline_pins = copy.deepcopy(getattr(self.source, "pins", None))
        if baseline_pins is not None:
            baseline_layout = Layout(current, library=current_lib, grid=self.grid)
            node_pins(baseline_layout, baseline_pins, strict=True)
        if structure(current, current_lib) != structure(*baseline):
            raise LayoutError("the phone's layout changed since it was read; read again and redo the edits")
        if expected_pins is not None and (baseline_pins is not None or isinstance(expected_pins, ObservedPins)):
            if isinstance(expected_pins, ObservedPins):
                same_pins = same_reported_pins(baseline_pins, expected_pins)
            else:
                expected_layout = Layout(current, library=current_lib, grid=self.grid)
                node_pins(expected_layout, expected_pins, strict=True)
                same_pins = _pin_intents(baseline_layout) == _pin_intents(expected_layout)
            if not same_pins:
                raise LayoutError("the phone's exported gaps changed since it was read; read again and redo the edits")
        saved, saved_pins = self._prepared_layout(self._layout_from(current, current_lib))
        target = self._restore_widget_ids(target)
        if target_lib is not None:
            target_lib = self._with_webclips(target, target_lib)
        # The final read is checked under the same cross-process lock as the write.
        final, final_lib = await self._read_source()
        final_pins = getattr(self.source, "pins", None)
        if final_pins is not None:
            node_pins(Layout(final, library=final_lib, grid=self.grid), final_pins, strict=True)
        if structure(final, final_lib) != structure(*baseline):
            raise LayoutError("the phone's layout changed during the final prewrite read; read again")
        if not same_reported_pins(final_pins, baseline_pins):
            raise LayoutError("the phone's exported gaps changed during the final prewrite read; read again")
        await self._assert_identity()
        backup = self._backup(saved.to_state(), saved.to_library(), label, saved_pins)
        # Match the library comparison to the source's layout format.
        lib_known = target_lib is not None
        _exclusive_file(self._pending_path, json.dumps({"device_id": self.device_id,
                        "backup": backup.name, "status": "pending"}).encode())
        write_error = None
        try:
            await asyncio.wait_for(self.source.write(target, target_lib, pins, self.grid), OPERATION_TIMEOUT)
        except Exception as e:
            write_error = str(e) or type(e).__name__
        verification_note = "Readback compares the layout fields returned by the selected source."
        try:
            after, after_lib = await self._read_source()
            await self._assert_identity()
            got_pins = getattr(self.source, "pins", None)
            unreported = got_pins is None
            requested = Layout(target, library=target_lib, grid=self.grid)
            node_pins(requested, want_pins, strict=True)
            if unreported:
                # Retain requested gaps under the no-pin-map contract. Match folders by
                # exported content without adding outgoing UUIDs to the returned state.
                got_pins = memory_pins(requested)
            else:
                observed = Layout(after, library=after_lib, grid=self.grid)
                node_pins(observed, got_pins, strict=True)
                if not _pin_intents(requested) <= _pin_intents(observed):
                    raise LayoutError("readback did not retain every requested fixed position")
            ok = (placed_structure(after, after_lib if lib_known else None, got_pins, self.grid,
                                   by_memory=unreported)
                  == placed_structure(target, target_lib, want_pins, self.grid))
        except Exception as e:
            return {"ok": False, "status": "unknown", "backup": backup.name,
                    "warnings": [verification_note],
                    "error": f"Readback failed ({type(e).__name__}); run Read before applying another change."}
        res: dict[str, Any] = {"ok": ok, "status": "verified" if ok else "unknown",
                               "backup": backup.name, "warnings": [verification_note]}
        if write_error is not None:
            res["error"] = (f"Write returned {write_error}. "
                            f"Readback result: {'matched' if ok else 'different'}.")
        if after_lib is not None:
            res["app_library_only"] = len(after_lib)
        if not ok:
            res["error"] = (res.get("error", "Readback differs from the target layout.")
                            + " Run Read before applying another change.")
            want = Layout(target, library=target_lib, grid=self.grid)
            got = Layout(after, library=after_lib if lib_known else None, grid=self.grid)
            wp, gp = want.positions(), got.positions()
            res["mismatches"] = {
                "added_by_ios": sorted(set(gp) - set(wp)),
                "missing": sorted(set(wp) - set(gp)),
                "moved": {k: f"wanted {wp[k]}, got {gp[k]}" for k in wp
                          if k in gp and wp[k] != gp[k] and not k.startswith(("folder:", "widget:"))},
                "pages": f"wanted {len(target) - 1}, got {len(after) - 1}",
            }
            surfaced = [got.nodes[i].name for i in res["mismatches"]["added_by_ios"]
                        if got.nodes[i].kind == "webclip"]
            if surfaced:
                # remembered now, so the next write keeps them in the App Library
                res["hidden_webclips"] = surfaced
                res["hint"] = ("iOS put web clips it had never reported into free spots. They're "
                               "remembered now: move them to the App Library and apply again.")
        if ok:
            # remember the gaps that were just written (the phone won't report them back)
            written = Layout(after, library=after_lib, grid=self.grid)
            node_pins(written, got_pins, by_memory=unreported, strict=True)
            self._remember_gaps(written)
        if not ok:
            return res
        self.layout = self._layout_from(after, after_lib)
        reported = getattr(self.source, "pins", None)
        self._observed_placement = copy.deepcopy(reported) if isinstance(reported, ObservedPins) else None
        self._undo.clear()
        self._redo.clear()
        self._save_draft()
        if ok:
            self._clear_pending()
        return res

    async def apply(self, token: str) -> dict[str, Any]:
        require_writable()
        with self._exclusive():
            self._require_known()
            return await self._apply_locked(token)

    async def _apply_locked(self, token: str) -> dict[str, Any]:
        lay = self.require()
        if token != lay.token():
            raise LayoutError("token doesn't match the current draft; run diff and use its apply_token")
        self._check_placement()
        problems = lay.validate()
        if problems:
            raise LayoutError(f"draft has problems, not applying: {problems}")
        prepared, pins = self._prepared_layout(lay)
        return await self._write_verified(prepared.to_state(), prepared.to_library(), "before-apply", pins,
                                          (lay.source, lay.source_library),
                                          self._observed_placement if self._observed_placement is not None else
                                          pins_by_page(lay.original()))

    def backups(self) -> list[str]:
        return sorted(p.name for p in self._dev_dir("backups").glob("*.plist"))

    async def restore(self, name: str) -> dict[str, Any]:
        require_writable()
        with self._exclusive():
            self._require_known()
            return await self._restore_locked(name)

    async def _restore_locked(self, name: str) -> dict[str, Any]:
        p = self._dev_dir("backups") / name
        if not p.exists() or p.is_symlink() or p.parent != self._dev_dir("backups"):
            raise LayoutError(f"no backup named {name!r}")
        data = p.read_bytes()
        try:
            blob = plistlib.loads(data)
            provenance = blob.get("provenance") if isinstance(blob, dict) else None
            if not isinstance(provenance, dict) or provenance.get("version") != 1:
                raise LayoutError("backup has no trusted device identity provenance; legacy backups cannot be restored")
            if not self.device_id or provenance.get("device_id") != self.device_id:
                raise LayoutError("backup device identity does not match this Session's device")
            state, library = _unpack(data)
            if not isinstance(state, list) or (library is not None and not isinstance(library, list)):
                raise ValueError("invalid layout payload")
            restored = Layout(state, library=library, grid=self.grid)
            pins = _unpack_pins(data)
            if pins and any(pins):
                dimensions = provenance.get("grid")
                if not isinstance(dimensions, dict) or not {"cols", "rows"} <= dimensions.keys():
                    raise LayoutError("pinned backup has no saved grid dimensions; use a backup with grid provenance")
                old_grid = Grid(**dimensions)
                if any(type(value) is not int or value <= 0 for value in old_grid.to_dict().values()):
                    raise LayoutError("backup grid dimensions must be positive integers")
                if not isinstance(pins, list) or any(not isinstance(page, dict) for page in pins):
                    raise LayoutError("backup pins must identify items on each page")
                mapped = []
                for index, page_pins in enumerate(pins):
                    page = restored.pages[index] if index < len(restored.pages) else []
                    nodes = {pin_key(node): node for node in page}
                    mapped_page = {}
                    for key, cell in page_pins.items():
                        if key not in nodes or type(cell) is not int or not 0 <= cell < old_grid.slots:
                            raise LayoutError("backup pin is outside its saved grid or does not identify an item on its page")
                        if sum(pin_key(node) == key for node in page) != 1:
                            raise LayoutError("ambiguous pin identifier on a backup page")
                        row, column = divmod(cell, old_grid.cols)
                        width, height = restored.dims(nodes[key])
                        if (column + width > min(old_grid.cols, self.grid.cols)
                                or row + height > min(old_grid.rows, self.grid.rows)):
                            raise LayoutError("backup pin footprint does not fit the saved or loaded grid")
                        mapped_page[key] = row * self.grid.cols + column
                    mapped.append(mapped_page)
                pins = mapped
                node_pins(restored, pins, strict=True)
            problems = restored.validate()
            for index, page in enumerate(restored.pages, 1):
                problems += [f"page {index}: {problem}" for problem in restored.place(page)[1]]
            if problems:
                raise LayoutError(f"backup layout has problems, not restoring: {problems}")
        except LayoutError:
            raise
        except (ValueError, TypeError, KeyError, plistlib.InvalidFileException) as e:
            raise LayoutError("backup identity or layout data is invalid") from e
        return await self._write_verified(state, library, "before-restore", pins)
