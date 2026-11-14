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
import hashlib
import json
import os
import uuid
import plistlib
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from collections import Counter
from collections.abc import Set
from pathlib import Path
from typing import Any, Callable
from xml.parsers.expat import ExpatError

from . import colors
from .device import DISK_MARK, NAME_MARK, ObservedPins, Source, archive_page_order, disk_pins, disk_to_model, page_fields, require_writable
from .model import Grid, Layout, LayoutError, Node, is_folder, is_widget, node_structure, widget_configuration
from .private_store import ensure_private_directory

KEEP_READS = 10
MAX_UNDO = 200
OPERATION_TIMEOUT = 30.0
_MISSING = object()  # Older drafts did not record the source's placement observation.


class ReviewChanged(LayoutError):
    """The editor action was prepared against a different device, draft or backup."""


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
        if stream.write(data) != len(data):
            raise OSError("incomplete local file write")
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _publish_exclusive(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _exclusive_file(temporary, data)
        os.link(temporary, path)
        _sync_directory(path.parent)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


@contextmanager
def _previous_file(path: Path):
    """Keep the old inode until all updates in the transaction succeed."""
    previous = path.with_name(f".{path.name}.{uuid.uuid4().hex}.previous")
    linked = False
    try:
        os.link(path, previous, follow_symlinks=False)
        linked = True
    except FileNotFoundError:
        pass
    try:
        yield
    except BaseException:
        if linked:
            os.replace(previous, path)
        else:
            path.unlink(missing_ok=True)
        _sync_directory(path.parent)
        raise
    finally:
        if linked:
            # Removing a private rollback link is housekeeping after the commit.
            with suppress(OSError):
                previous.unlink(missing_ok=True)


def _private_replace(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _exclusive_file(temporary, data)
        with _previous_file(path):
            os.replace(temporary, path)
            _sync_directory(path.parent)
    finally:
        with suppress(OSError):
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
# Dictionary-format anchors refer to the serialized display ID or the widget/folder instance ID.

def pin_key(node: Node) -> str | None:
    if node.kind in ("app", "webclip"):
        return node.raw.get("displayIdentifier") or node.bundle_id or node.id
    if node.kind == "widget":
        return node.raw.get("uniqueIdentifier") or node.raw.get("userSelectedElementIdentifier")
    if node.kind == "folder":
        return node.raw.get("uniqueIdentifier")
    return None


def memory_key(node: Node) -> str | None:
    """Folders can omit UUIDs on readback; their name and ordered contents identify gaps."""
    if node.kind == "folder":
        return "folder-content:" + json.dumps(node_structure(node), separators=(",", ":"))
    return node.bundle_id or pin_key(node)


def _memory_keys(nodes: list[Node], saved: dict[str, Any] | None = None,
                 bound_ids: Set[str] = frozenset(), typed_apps: bool = False) -> dict[str, str | None]:
    keys = {node.id: memory_key(node) for node in nodes}
    counts = Counter(keys.values())
    for node in nodes:
        key = keys[node.id]
        wire = pin_key(node)
        if node.bundle_id and typed_apps:
            keys[node.id] = "app:" + json.dumps([node.bundle_id, wire], separators=(",", ":"))
        elif node.kind == "widget" and typed_apps and wire:
            keys[node.id] = "widget:" + json.dumps(wire)
        elif node.kind == "folder":
            instance = "folder-instance:" + json.dumps([key, wire], separators=(",", ":")) if wire else None
            if counts[key] > 1 or node.id in bound_ids or (saved is not None and instance in saved):
                keys[node.id] = instance
    return keys


def _instance_ids(lay: Layout, memory: dict[str, Any]) -> set[str]:
    """Find the local folder IDs required by page-scoped instance memory."""
    pages = memory.get("pages", [])
    owners = set()
    for nodes, page in zip(lay.pages, pages if isinstance(pages, list) else []):
        if isinstance(page, dict):
            for nid, key in _memory_keys(nodes, page).items():
                if lay.nodes[nid].kind == "folder" and key in page and key.startswith("folder-instance:"):
                    owners.add(nid)
    return owners


def memory_pins(lay: Layout, previous: dict[str, Any] | None = None,
                bound_ids: Set[str] = frozenset(), *, typed: bool | None = None) -> dict[str, Any]:
    pages = []
    saved_pages = (previous or {}).get("pages", [])
    if typed is None:
        typed = any(n.bundle_id and pin_key(n) != n.bundle_id for page in lay.pages for n in page)
    for index, page in enumerate(lay.pages):
        saved = saved_pages[index] if isinstance(saved_pages, list) and index < len(saved_pages) else {}
        keys = _memory_keys(page, saved, bound_ids, typed)
        pages.append({keys[n.id]: lay.pins[n.id] for n in page if n.id in lay.pins and keys[n.id]})
    result = {"version": 2, "grid": lay.grid.to_dict(), "pages": pages}
    if typed:
        result["app_keys"] = "bundle-display-v1"
    return result


def pins_by_page(lay: Layout) -> list[dict[str, int]]:
    """Per page, {identifier: cell} for the dict format. Pinned folders without an id get one."""
    out = []
    for page in lay.pages:
        d = {}
        for n in page:
            if n.id in lay.pins and n.kind == "folder" and not n.raw.get("uniqueIdentifier"):
                n.raw["uniqueIdentifier"] = str(uuid.uuid4()).upper()
        counts = Counter(pin_key(n) for n in page)
        for n in page:
            if n.id not in lay.pins:
                continue
            key = pin_key(n)
            if not key:
                raise LayoutError(f"{n.kind} {n.name!r} has no wire identifier for its pin; use ordinary page order")
            if counts[key] != 1:
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
    typed_apps = False
    if by_memory and isinstance(keyed, dict) and any(type(value) is not int for value in keyed.values()):
        try:
            if type(keyed["version"]) is not int or keyed["version"] != 2:
                raise ValueError("unsupported gap memory version")
            if "app_keys" in keyed:
                if keyed["app_keys"] != "bundle-display-v1":
                    raise ValueError("unsupported gap memory app keys")
                typed_apps = True
            dimensions = keyed["grid"]
            if not isinstance(dimensions, dict) or not {"cols", "rows"} <= dimensions.keys():
                raise ValueError("missing gap memory dimensions")
            saved_grid = Grid(**dimensions)
            if any(type(value) is not int or value <= 0 for value in saved_grid.to_dict().values()):
                raise ValueError("invalid gap memory grid")
            keyed = keyed["pages"]
            if not isinstance(keyed, list):
                raise ValueError("invalid gap memory pages")
        except (KeyError, TypeError, ValueError) as e:
            raise LayoutError("invalid saved pin memory") from e
    scoped = isinstance(keyed, list)
    if scoped and any(not isinstance(page, dict) for page in keyed):
        raise LayoutError("pins must identify items on each page")
    if strict and scoped and any(keyed[len(lay.pages):]):
        raise LayoutError("pin identifiers do not match the layout's page items")

    def owners(nodes: list[Node], values: dict[str, Any]):
        keys = _memory_keys(nodes, values, typed_apps=typed_apps) if by_memory and scoped else {
            n.id: (memory_key(n) if by_memory else pin_key(n)) for n in nodes}
        if not by_memory or typed_apps:
            return keys, Counter(keys.values())

        # Qualify each historical key against every possible owner, even when another
        # alias of that owner is also present. Page-scoped and flat files use the same rule.
        names = Counter(n.name for n in nodes if n.kind == "folder")
        candidates = {}
        for n in nodes:
            aliases = {keys[n.id]}
            if n.bundle_id:
                aliases.add(pin_key(n))
            if not scoped and n.kind == "folder" and names[n.name] == 1:
                aliases.add(f"folder:{n.name}")
                aliases.add("folder-content:" + json.dumps(
                    [n.name, [[child.id for child in page] for page in n.children]], separators=(",", ":")))
            candidates[n.id] = {key for key in aliases if key in values}
        counts = Counter(key for aliases in candidates.values() for key in aliases)
        if strict and any(count > 1 for count in counts.values()):
            raise LayoutError("ambiguous pin identifier on a layout page")
        for nid, aliases in candidates.items():
            unique = [key for key in aliases if counts[key] == 1]
            keys[nid] = unique[0] if len(unique) == 1 else None
        return keys, counts

    wanted: dict[str, int] = {}
    global_owners = None if scoped else owners([n for page in lay.pages for n in page], keyed)
    matched_global = set()
    for index, page in enumerate(lay.pages):
        values = (keyed[index] if index < len(keyed) else {}) if scoped else keyed
        keys, identities = global_owners or owners(page, values)
        matched = set()
        invalid = False
        for n in page:
            key = keys[n.id]
            if key and key in values:
                if identities[key] != 1:
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


def _retains_pins(requested: Layout, observed: Layout, memory: dict[str, Any] | None = None) -> bool:
    instances = _instance_ids(requested, memory or {})
    for index, page in enumerate(requested.pages):
        pinned = [node for node in page if node.id in requested.pins]
        if not pinned:
            continue
        others = observed.pages[index] if index < len(observed.pages) else []
        requested_shapes = {node.id: node_structure(node) for node in page}
        observed_shapes = {node.id: node_structure(node) for node in others}
        for owner in pinned:
            cell = requested.pins[owner.id]
            shape = requested_shapes[owner.id]
            matches = [node for node in others if observed_shapes[node.id] == shape]
            if owner.kind == "folder":
                wire = pin_key(owner)
                same_wire = [node for node in others if wire and pin_key(node) == wire]
                if same_wire:
                    matches = [node for node in same_wire if observed_shapes[node.id] == shape]
                elif owner.id in instances or sum(value == shape for value in requested_shapes.values()) != 1:
                    return False
            if len(matches) != 1 or observed.pins.get(matches[0].id) != cell:
                return False
    return True


def placed_structure(state: list[Any], library: list[Any] | None,
                     keyed_pins: dict[str, Any] | list[dict[str, int]] | None,
                     grid: Grid | None = None, by_memory: bool = False, *, leaf_dicts: bool = False) -> str:
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
        pages.append([[_carried_node(n, leaf_dicts), pos[n.id][:2] if n.id in pos else None] for n in order])
    return json.dumps({"dock": [_carried_node(n, leaf_dicts) for n in lay.dock], "pages": pages,
                       "library": _carried_library(lay, leaf_dicts)},
                      default=list)


def _carried_node(node: Node, leaf_dicts: bool) -> Any:
    """Compare fields actually sent by the selected writer, separately from persistent pin identities."""
    if node.kind == "folder":
        fields = {k: v for k, v in node.raw.items()
                  if k not in (NAME_MARK, "iconLists", "displayName", "uniqueIdentifier")}
        return [node.name, plistlib.dumps(fields, fmt=plistlib.FMT_BINARY).hex(),
                [[_carried_node(c, leaf_dicts) for c in page] for page in node.children if page]]
    if leaf_dicts and node.bundle_id and not node.raw.get(DISK_MARK):
        return {"fields": plistlib.dumps(node.raw, fmt=plistlib.FMT_BINARY).hex()}
    return node_structure(node)


def _carried_library(lay: Layout, leaf_dicts: bool) -> list | None:
    if lay.library is None:
        return None
    return sorted((_carried_node(n, leaf_dicts) for n in lay.library), key=json.dumps)


def _carried_layout(lay: Layout, leaf_dicts: bool) -> tuple[list, list | None]:
    return ([[_carried_node(n, leaf_dicts) for n in page] for page in [lay.dock, *lay.pages]],
            _carried_library(lay, leaf_dicts))


def _clip_inventory(lay: Layout) -> list[str]:
    return sorted(plistlib.dumps(n.raw, fmt=plistlib.FMT_BINARY).hex()
                  for n in lay.nodes.values() if n.kind == "webclip")


def _widgets(state: list[Any]) -> list[dict[str, Any]]:
    """Every top-level widget dict in a list-format state (widgets can't be in folders)."""
    return [x for page in state for x in page
            if isinstance(x, dict) and is_widget(x)]


def same_reported_pins(first: dict[str, int] | None, second: dict[str, int] | None) -> bool:
    if isinstance(first, ObservedPins) != isinstance(second, ObservedPins):
        return False
    if isinstance(first, ObservedPins):
        first, second = first.archive, second.archive
    if first is None or second is None:
        return first is second
    return plistlib.dumps(first, fmt=plistlib.FMT_BINARY) == plistlib.dumps(second, fmt=plistlib.FMT_BINARY)


def _anchor_owners(lay: Layout) -> list[dict[str, Any]]:
    return [{pin_key(n): [index, node_structure(n), lay.pins[n.id]]
             for index, n in enumerate(page) if n.id in lay.pins}
            for page in lay.pages]


def _folder_owners(lay: Layout, *, ordered: bool):
    """Each wire's full occurrence list, even when two different pages reuse it."""
    wires = {}
    ambiguous = set()
    for index, page in enumerate([lay.dock, *lay.pages]):
        folders = [(offset, n, memory_key(n)) for offset, n in enumerate(page) if n.kind == "folder"]
        shapes = Counter(shape for _, _, shape in folders)
        for offset, node, shape in folders:
            wire = pin_key(node)
            if wire:
                wires.setdefault(wire, Counter())[(index, offset if ordered else None, shape)] += 1
                if shapes[shape] > 1:
                    ambiguous.add(wire)
    return wires, ambiguous


def _compatible_folder_ids(before: Layout, after: Layout, bound_ids: Set[str] = frozenset(), *,
                           ordered: bool = True) -> bool:
    old, ambiguous = _folder_owners(before, ordered=ordered)
    new, _ = _folder_owners(after, ordered=ordered)
    required = ambiguous | {pin_key(before.nodes[nid]) for nid in bound_ids}
    return required <= new.keys() and all(old[wire] == new[wire] for wire in old.keys() & new.keys())


def _restore_archive_app_ids(state, library, reference):
    """A bare archive string reports the display ID. Retain a known unambiguous bundle alias."""
    def leaves(items):
        for item in items:
            if isinstance(item, dict):
                if is_folder(item):
                    for page in item["iconLists"]:
                        yield from leaves(page)
                elif not is_widget(item):
                    yield item

    aliases = {}
    ref_state, ref_library = reference
    for page in [*ref_state, ref_library or []]:
        for item in leaves(page):
            wire, bundle = item.get("displayIdentifier"), item.get("bundleIdentifier")
            if wire and bundle:
                aliases[wire] = bundle if aliases.get(wire, bundle) == bundle else None
    for page in [*state, library or []]:
        for item in leaves(page):
            if item.get(DISK_MARK) and (bundle := aliases.get(item.get("displayIdentifier"))):
                item["bundleIdentifier"] = bundle


@dataclass
class _SourceObservation:
    state: list[Any]
    library: list[Any] | None
    pins: Any
    page_identity: Any
    layout: Layout
    archive: dict | None
    leaf_dicts: bool
    reported_clips: list[str] | None

    def same_layout(self, other: "_SourceObservation") -> bool:
        return (self.leaf_dicts == other.leaf_dicts and self.reported_clips == other.reported_clips and
                _carried_layout(self.layout, self.leaf_dicts) == _carried_layout(other.layout, other.leaf_dicts))

    def same_placement(self, other: "_SourceObservation") -> bool:
        return (same_reported_pins(self.pins, other.pins)
                and self.page_identity == other.page_identity
                and _anchor_owners(self.layout) == _anchor_owners(other.layout))


def _source_shape_receipt(body: dict[str, Any], device: str, grid: Grid, pages: list[int]) -> dict[str, Any]:
    evidence = {"body": body, "device": device, "grid": grid.to_dict(), "widget_pages": pages}
    digest = hashlib.sha256(plistlib.dumps(evidence, fmt=plistlib.FMT_BINARY)).hexdigest()
    return {"widget_pages": pages, "sha256": digest}


def _overflow_widget_pages(lay: Layout) -> list[int]:
    return [i for i, page in enumerate(lay.pages)
            if any(n.kind == "widget" for n in page) and not any(n.id in lay.pins for n in page)
            and lay.place(page)[1]]


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in s)


class Session:
    def __init__(self, source: Source, data_dir: str | Path, device: str | None = None):
        """device: keep this session's draft per device (the app switches between devices);
        None keeps the single shared draft the CLI and MCP server use."""
        self.source = source
        self.dir = Path(data_dir)
        ensure_private_directory(self.dir)
        self._draft_key = device
        self.layout: Layout | None = None
        self._draft_error: str | None = None
        self.device_id: str | None = device
        self.info: dict[str, Any] = {}
        self.grid = Grid()
        self._observed_placement = _MISSING
        self._page_identity = _MISSING
        self._reported_clips = _MISSING
        self._placement_revision = ""
        self._draft_digest: bytes | None = None
        self.usage: dict[str, Any] | None = None  # Screen Time opens, loaded on request
        self._undo: list[dict[str, Any]] = []
        self._redo: list[dict[str, Any]] = []
        self._history_baseline: dict[str, Any] | None = None
        self._load_draft()

    # ---- persistence ------------------------------------------------------------

    @contextmanager
    def _exclusive(self, *, refresh: bool = False, expected: dict | None = None):
        fd = os.open(self.dir / ".session.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise LayoutError("another Session operation holds the local data lock; try again after it finishes") from e
            if expected is not None and expected != self.binding:
                raise ReviewChanged("the reviewed device or draft changed; review the current layout")
            if not refresh or expected is not None:
                try:
                    digest = hashlib.sha256(self.draft_path.read_bytes()).digest()
                except FileNotFoundError:
                    digest = None
                if digest != self._draft_digest:
                    if expected is not None:
                        raise ReviewChanged("the saved draft changed; reopen the current layout before continuing")
                    raise LayoutError("the saved draft changed in another session; reopen it or read the device again")
            yield
        finally:
            os.close(fd)

    @property
    def binding(self) -> dict:
        return {"device": self.device_id, "revision": self._draft_digest.hex() if self._draft_digest else ""}

    @property
    def _pending_path(self) -> Path:
        return self.dir / ".pending-write.json"

    def _require_known(self) -> None:
        if self._pending_path.exists():
            raise LayoutError("a pending write has an unknown outcome; perform an explicit read before any further write")

    def _clear_pending(self) -> None:
        if self._pending_path.exists():
            with _previous_file(self._pending_path):
                self._pending_path.unlink()
                _sync_directory(self.dir)

    async def _assert_identity(self) -> dict[str, Any]:
        info = await asyncio.wait_for(self.source.info(), OPERATION_TIMEOUT)
        actual = info.get("UniqueDeviceID")
        if not isinstance(actual, str) or not actual or actual != self.device_id:
            raise LayoutError("device identity changed or is unavailable; explicitly read the intended device again")
        if Grid.from_metrics(info.get("icon_metrics")) != self.grid:
            raise LayoutError("device grid changed; explicitly read the intended device again")
        return info

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
        blob = {"device": self.device_id, "info": self.info, "draft_revision": uuid.uuid4().hex,
                **self.layout.current_fields(), **self.layout.baseline()}
        blob["placement_revision"] = self._placement_revision
        if self._page_identity is not _MISSING:
            blob["source_page_identity"] = self._page_identity
        if self._reported_clips is not _MISSING:
            blob["source_reported_clips"] = self._reported_clips
        if isinstance(self._observed_placement, ObservedPins):
            blob["source_placement"] = self._observed_placement.archive
        elif self._observed_placement is not _MISSING:
            blob["source_pins_reported"] = self._observed_placement is not None
            if self._observed_placement is not None:
                blob["source_pin_map"] = self._observed_placement
        blob = {k: v for k, v in blob.items() if v is not None}  # plists have no null
        data = plistlib.dumps(blob, fmt=plistlib.FMT_BINARY)
        _private_replace(self.draft_path, data)
        self._draft_digest = hashlib.sha256(data).digest()

    def _load_draft(self) -> None:
        if self.draft_path.exists():
            data = self.draft_path.read_bytes()
            self._draft_digest = hashlib.sha256(data).digest()
            try:
                blob = plistlib.loads(data)
                if not isinstance(blob, dict):
                    raise ValueError("invalid draft")
                device = blob["device"]
                info = blob.get("info", {})
                if not isinstance(device, str) or not device or not isinstance(info, dict):
                    raise ValueError("invalid draft identity")
                if self._draft_key not in (None, "unselected", device):
                    raise ValueError("draft belongs to another device")
                self.device_id = device  # Recovery can still list this device's backups.
                if any(info.get(key) is not None and not isinstance(info[key], str)
                       for key in ("DeviceName", "ProductType", "ProductVersion")):
                    raise ValueError("invalid draft device info")
                json.dumps(info, allow_nan=False)
                placement = _MISSING
                if "source_placement" in blob:
                    if not isinstance(blob["source_placement"], dict):
                        raise ValueError("invalid draft placement")
                    placement = disk_pins(blob["source_placement"])
                elif "source_pins_reported" in blob:
                    placement = blob["source_pin_map"] if blob["source_pins_reported"] else None
                lay = Layout.from_snapshot(blob)
                original = lay.original()  # validate source identity independently
                reported_clips = blob.get("source_reported_clips", _MISSING)
                if reported_clips is not _MISSING and (not isinstance(reported_clips, list) or
                        any(not isinstance(value, str) for value in reported_clips)):
                    raise ValueError("invalid reported clip inventory")
                for view in (lay, original):
                    if set(view.pins) - view.nodes.keys():
                        raise LayoutError("saved draft pin identity is unavailable; call read again")
                    if any(type(v) is not int or not 0 <= v < view.grid.slots for v in view.pins.values()):
                        raise LayoutError("saved draft pin cell is invalid; read the device again")
                if "identity" not in blob or "source_identity" not in blob:
                    # Legacy enumeration IDs cannot prove ownership after reordering.
                    for view in (lay, original):
                        owners = Counter(pin_key(node) for node in view.nodes.values())
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
                            stable &= owners[pin_key(node)] == 1
                            if not stable:
                                raise LayoutError("saved draft pin identity is ambiguous; call read again")
                self.info, self.grid = info, lay.grid
                self._observed_placement = placement
                self._page_identity = blob.get("source_page_identity", _MISSING)
                self._reported_clips = reported_clips
                self._placement_revision = blob.get("placement_revision", "")
                lay.approval_device = device
                self.layout = lay
            except LayoutError as e:
                self._draft_error = str(e)
            except (ValueError, TypeError, KeyError, OverflowError, ExpatError, plistlib.InvalidFileException):
                self._draft_error = "saved draft data is invalid; read the device again"

    def require(self) -> Layout:
        if self.layout is None:
            raise LayoutError(self._draft_error or "no layout loaded yet; call read first")
        return self.layout

    def edited(self) -> None:
        with self._exclusive():
            self._save_draft()

    # ---- edits with undo ----------------------------------------------------------

    def _snap(self) -> dict[str, Any]:
        return self.require().snapshot()

    def _history_snapshot(self, detached: Layout | None = None) -> dict[str, Any]:
        lay = self.require() if detached is None else detached
        baseline = lay.baseline()
        if baseline != self._history_baseline:
            self._history_baseline = copy.deepcopy(baseline) if detached is None else baseline
        fields = lay.current_snapshot() if detached is None else lay.current_fields()
        return {**fields, **self._history_baseline}

    def edit(self, fn: Callable[[Layout], Any], *, expected: dict | None = None) -> Any:
        """Run fn on the draft as one undoable step. If it raises, the draft is left untouched."""
        with self._exclusive(expected=expected):
            before = copy.deepcopy(self.require())
            try:
                out = fn(self.require())
                self._check_placement()
                self._prepare_widget_pins(self.require())
                self.require().approval_device = self.device_id
                self._save_draft()
            except BaseException:
                self.layout = before
                raise
            self._undo.append(self._history_snapshot(before))
            del self._undo[:-MAX_UNDO]
            self._redo.clear()
            return out

    def _check_placement(self, target: Layout | None = None, baseline: Layout | None = None,
                         info: dict[str, Any] | None = None) -> None:
        if str((self.info if info is None else info).get("ProductVersion", "")).split(".", 1)[0] != "16":
            return
        lay = target or self.require()
        original = baseline or lay.original()
        if not _retains_pins(lay, original):
            raise LayoutError("iOS 16 does not preserve fixed icon positions; use ordinary page order")
        leading_widgets = set()
        for page in original.pages:
            for node in page:
                if node.kind != "widget":
                    break
                leading_widgets.add(node_structure(node))
        for page in lay.pages:
            folder_seen = False
            for node in page:
                folder_seen |= node.kind == "folder"
                if folder_seen and node.kind == "widget" and node_structure(node) in leading_widgets:
                    raise LayoutError("iOS 16 keeps these widgets ahead of folders; place the folder after the widgets")

    def _step_history(self, steps: list[dict[str, Any]], reverse: list[dict[str, Any]], expected: dict | None) -> bool:
        with self._exclusive(expected=expected):
            if not steps:
                return False
            before = self.require()
            snapshot = self._history_snapshot()
            try:
                self.layout = Layout.from_snapshot(copy.deepcopy(steps[-1]))
                self.layout.approval_device = self.device_id
                self._save_draft()
            except BaseException:
                self.layout = before
                raise
            steps.pop()
            reverse.append(snapshot)
            return True

    def undo(self, *, expected: dict | None = None) -> bool:
        return self._step_history(self._undo, self._redo, expected)

    def redo(self, *, expected: dict | None = None) -> bool:
        return self._step_history(self._redo, self._undo, expected)

    def _adopt_usage(self, result: dict, expected: dict, selected: Callable[[], bool] | None) -> dict:
        with self._exclusive(expected=expected):
            if selected is not None and not selected():
                raise ReviewChanged("the reviewed device changed; load usage for the current device")
            self.usage = result
        return result

    def load_usage(self, days: int = 30, *, expected: dict | None = None,
                   selected: Callable[[], bool] | None = None) -> dict[str, Any]:
        from . import usage
        binding = self.binding if expected is None else expected
        lay = self.require()
        known = {n.bundle_id for n in lay.nodes.values() if n.bundle_id}
        return self._adopt_usage(usage.load(known, days), binding, selected)

    async def load_device_usage(self, *, expected: dict | None = None,
                                selected: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Usage from the device's own memory reports (no Screen Time sync needed)."""
        from . import usage
        binding = self.binding if expected is None else expected
        lay = self.require()
        if not hasattr(self.source, "memory_reports"):
            return self._adopt_usage({"ok": False, "error": "This source can't report usage."}, binding, selected)
        files = await self.source.memory_reports(self._dev_dir("usage"))
        apps = await self.source.installed_apps()
        known = {n.bundle_id for n in lay.nodes.values() if n.bundle_id}
        return self._adopt_usage(usage.from_memory_reports(files, apps, known), binding, selected)

    def history(self) -> dict[str, int]:
        return {"undo": len(self._undo), "redo": len(self._redo)}

    # ---- read -------------------------------------------------------------------

    # ---- widget element ids -------------------------------------------------------------
    @property
    def _widget_ids_path(self) -> Path:
        return self._dev_dir("widgets") / "element-ids.json"

    @staticmethod
    def _widget_key(widget: dict[str, Any], wire: str | None = None) -> str:
        config = {k: v for k, v in widget.items() if k not in ("uniqueIdentifier", "userSelectedElementIdentifier")}
        if wire:
            config["uniqueIdentifier"] = wire
        return hashlib.sha256(bytes.fromhex(widget_configuration(config))).hexdigest()

    def _widget_memory(self) -> tuple[dict[str, str | None], dict[str, str | None]]:
        path = self._widget_ids_path
        if path.exists():
            stored = json.loads(path.read_text())
            if isinstance(stored, dict) and isinstance(stored.get("instances"), dict):
                if stored.get("version") == 3 and isinstance(stored.get("owners"), dict):
                    return stored["instances"], stored["owners"]
                if stored.get("version") == 2:
                    # Old wire+configuration keys remain usable. Configuration-only entries
                    # gain owner evidence once the element is actually observed again.
                    return stored["instances"], {}
        return {}, {}

    def _remember_widget_ids(self, state: list[Any]) -> None:
        known, owners = self._widget_memory()
        widgets = _widgets(state)
        counts = Counter(self._widget_key(widget) for widget in widgets)
        for widget in widgets:
            if not widget.get("displayIdentifier"):
                continue
            element = widget.get("userSelectedElementIdentifier")
            wire = widget.get("uniqueIdentifier")
            config = self._widget_key(widget)
            owner = wire or element
            prior = owners.get(config)
            if not wire and element and prior and known.get(self._widget_key(widget, prior)) == element:
                owner = prior  # This element was previously exported with the recorded wire ID.
            if counts[config] > 1:
                owners[config] = None
            elif owner:
                # A wire-only observation cannot identify the owner of a legacy element.
                if config not in owners and config in known and not element:
                    owners[config] = None
                else:
                    owners[config] = owner if owners.get(config, owner) == owner else None
            if isinstance(element, str) and element:
                for key in {config, self._widget_key(widget, wire)}:
                    known[key] = element if known.get(key, element) == element else None
        if known or owners:
            _private_replace(self._widget_ids_path, json.dumps(
                {"version": 3, "instances": known, "owners": owners}).encode())

    def _recover_widget_ids(self, widgets: list[dict[str, Any]]) -> None:
        known, owners = self._widget_memory()
        keys = [self._widget_key(widget, widget.get("uniqueIdentifier")) for widget in widgets]
        configs = Counter(self._widget_key(widget) for widget in widgets)
        counts = Counter(keys)
        claimed = Counter(value for widget in widgets
                          for value in {widget.get("uniqueIdentifier"), widget.get("userSelectedElementIdentifier")}
                          if value)
        candidates = []
        for widget, key in zip(widgets, keys):
            wire = widget.get("uniqueIdentifier")
            if widget.get("userSelectedElementIdentifier") or counts[key] != 1:
                continue
            if not wire and (configs[key] != 1 or not owners.get(key)):
                continue
            element = known.get(key)
            if element and claimed[element] == int(wire == element):
                candidates.append((widget, element))
        selected = Counter(element for _, element in candidates)
        for widget, element in candidates:
            if selected[element] == 1:
                widget["userSelectedElementIdentifier"] = element

    def _prepare_widget_pins(self, lay: Layout) -> None:
        widgets = [node for node in lay.nodes.values() if node.kind == "widget"]
        if not any(node.id in lay.pins and not pin_key(node) for node in widgets):
            return
        raw = [copy.deepcopy(node.raw) for node in widgets]
        self._recover_widget_ids(raw)
        for node, recovered in zip(widgets, raw):
            if node.id in lay.pins and not pin_key(node):
                node.raw = recovered
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
        return self._dev_dir("webclips") / "seen.plist"

    def _known_webclips(self) -> dict[str, dict[str, Any]]:
        path = self._webclips_path
        if path.exists():
            return plistlib.loads(path.read_bytes())
        legacy = path.with_suffix(".json")
        return json.loads(legacy.read_text()) if legacy.exists() else {}

    def _remember_webclips(self, state: list[Any], library: list[Any] | None) -> None:
        known = self._known_webclips()
        updated = dict(known)
        for n in Layout(state, library=library).nodes.values():
            if n.kind == "webclip":
                updated[n.id] = n.raw
        if updated:
            result = plistlib.dumps(updated, fmt=plistlib.FMT_BINARY)
            if result != plistlib.dumps(known, fmt=plistlib.FMT_BINARY):
                _private_replace(self._webclips_path, result)

    def _with_webclips(self, state: list[Any], library: list[Any] | None) -> list[Any] | None:
        """library plus every remembered web clip that isn't on the Home Screen."""
        if library is None:
            return None
        here = {n.id for n in Layout(state, library=library).nodes.values()}
        return library + [dict(w) for i, w in sorted(self._known_webclips().items()) if i not in here]

    async def _read_source(self, reference=None, *, verifying: bool = False) -> tuple[list[Any], list[Any] | None, list[str] | None]:
        state, library = await asyncio.wait_for(self.source.read(), OPERATION_TIMEOUT)
        raw = Layout(state, library=library, grid=self.grid)
        reported_clips = _clip_inventory(raw) if getattr(self.source, "source_archive", None) is not None else None
        if reference is not None:
            _restore_archive_app_ids(state, library, reference)
        self._remember_widget_ids(state)
        self._remember_webclips(state, library)
        if verifying and getattr(self.source, "source_archive", None) is not None:
            return state, library, reported_clips
        return state, self._with_webclips(state, library), reported_clips

    # ---- gap memory ----------------------------------------------------------------------
    # The phone never reports gaps over USB, and a write without them closes every gap. So the
    # gaps set in Homescreen are remembered per phone and sent again on every write.

    @property
    def _gaps_path(self) -> Path:
        return self._dev_dir("gaps") / "pins.json"

    @property
    def _placement_revision_path(self) -> Path:
        return self._gaps_path.with_name("revision")

    def _saved_placement_revision(self) -> str:
        try:
            return self._placement_revision_path.read_text()
        except FileNotFoundError:
            return ""

    def _gap_memory(self) -> dict[str, Any]:
        if not self._gaps_path.exists():
            return {}
        try:
            memory = json.loads(self._gaps_path.read_bytes())
            if not isinstance(memory, dict):
                raise ValueError("invalid gap memory")
            return memory
        except (UnicodeDecodeError, ValueError) as e:
            raise LayoutError("invalid saved pin memory; read again") from e

    def _remember_gaps(self, lay: Layout, previous: dict[str, Any] | None = None) -> None:
        _private_replace(self._gaps_path, json.dumps(memory_pins(lay, previous), indent=1, sort_keys=True).encode())

    def _layout_from(self, state: list[Any], library: list[Any] | None, *, recover_memory: bool = False) -> Layout:
        """A Layout with gaps: from the source when it can report them, else from memory."""
        lay = Layout(state, library=library, grid=self.grid)
        reported = getattr(self.source, "pins", None)
        if reported is not None:
            node_pins(lay, reported)
        if self._gaps_path.exists() and (reported is None or recover_memory):
            memory_layout = lay if reported is None else Layout(state, library=library, grid=self.grid)
            try:
                node_pins(memory_layout, self._gap_memory(), by_memory=True)
            except LayoutError:
                if not recover_memory:
                    raise
                self._remember_gaps(lay)
        return Layout(state, library=library, pins=lay.pins, grid=self.grid)

    @property
    def gaps_readable(self) -> bool:
        return getattr(self.source, "pins", None) is not None

    async def read(self, *, expected: dict | None = None) -> dict[str, Any]:
        with self._exclusive(refresh=True, expected=expected):
            return await self._read_locked()

    async def _read_locked(self) -> dict[str, Any]:
        info = await asyncio.wait_for(self.source.info(), OPERATION_TIMEOUT)
        device_id = info.get("UniqueDeviceID")
        if not isinstance(device_id, str) or not device_id:
            raise LayoutError("device identity is unavailable; no layout was loaded")
        if self._draft_key not in (None, "unselected", device_id):
            raise LayoutError("device identity differs from the selected device; select the intended device")
        grid = Grid.from_metrics(info.get("icon_metrics"))
        state, library = await asyncio.wait_for(self.source.read(), OPERATION_TIMEOUT)
        verified = await asyncio.wait_for(self.source.info(), OPERATION_TIMEOUT)
        if verified.get("UniqueDeviceID") != device_id:
            raise LayoutError("device identity changed during read; no layout was loaded")
        if Grid.from_metrics(verified.get("icon_metrics")) != grid:
            raise LayoutError("device grid changed during read; no layout was loaded")
        raw = Layout(state, library=library, grid=grid)  # validate the shape before committing identity
        reported_clips = _clip_inventory(raw) if getattr(self.source, "source_archive", None) is not None else None
        clear_pending = False
        legacy_pending = None
        if self._pending_path.exists():
            pending_bytes = self._pending_path.read_bytes()
            pending = json.loads(pending_bytes)
            clear_pending = pending.get("device_id") == device_id
            name = pending.get("backup")
            if (set(pending) == {"device_id", "backup", "status"}
                    and isinstance(name, str) and name.endswith(".plist") and Path(name).name == name
                    and "\x00" not in name and getattr(self.source, "legacy_fixture_id", None) and
                    pending.get("device_id") == self.source.legacy_fixture_id and pending.get("status") == "pending"
                    ):
                legacy_pending = pending_bytes
        previous = (self.device_id, self.info, self.grid, self.layout, self._undo, self._redo,
                    self._observed_placement, self._page_identity, self._reported_clips,
                    self._draft_digest, self._placement_revision)
        if self.layout is not None and device_id == self.device_id:
            _restore_archive_app_ids(state, library, (self.layout.source, self.layout.source_library))
        try:
            self.device_id = device_id
            self.info = {k: v for k, v in info.items() if k in ("DeviceName", "ProductType", "ProductVersion")}
            self.grid = grid
            reads = self._dev_dir("reads")
            snapshot = reads / f"{_stamp()}.plist"
            with ExitStack() as transaction:
                for path in (self._widget_ids_path, self._webclips_path, snapshot, self._gaps_path, self.draft_path):
                    transaction.enter_context(_previous_file(path))
                self._remember_widget_ids(state)
                self._remember_webclips(state, library)
                library = self._with_webclips(state, library)
                _private_replace(snapshot, _pack(state, library))
                for old in sorted(reads.glob("*.plist"))[:-KEEP_READS]:
                    transaction.enter_context(_previous_file(old))
                    old.unlink()
                self.layout = self._layout_from(state, library, recover_memory=True)
                self.layout.approval_device = device_id
                self._placement_revision = self._saved_placement_revision()
                self._observed_placement = copy.deepcopy(getattr(self.source, "pins", None))
                self._page_identity = copy.deepcopy(getattr(self.source, "page_identity", {}))
                self._reported_clips = reported_clips
                self._save_draft()
                if legacy_pending is not None:
                    unresolved = self.dir / "unresolved-writes"
                    unresolved.mkdir(mode=0o700, exist_ok=True)
                    path = unresolved / (hashlib.sha256(legacy_pending).hexdigest() + ".json")
                    transaction.enter_context(_previous_file(path))
                    try:
                        _publish_exclusive(path, legacy_pending)
                    except FileExistsError:
                        if path.is_symlink() or path.read_bytes() != legacy_pending:
                            raise LayoutError("unresolved write evidence changed; the pending record was retained")
                    _sync_directory(self.dir)
                if clear_pending or legacy_pending is not None:
                    self._clear_pending()
            self._undo, self._redo = [], []
            self._history_baseline = None
            if previous[0] != device_id:
                self.usage = None
        except BaseException:
            (self.device_id, self.info, self.grid, self.layout, self._undo, self._redo,
             self._observed_placement, self._page_identity, self._reported_clips,
             self._draft_digest, self._placement_revision) = previous
            raise
        self._draft_error = None
        return {"device": {k: v for k, v in info.items() if k != "UniqueDeviceID"},
                **self.layout.summary()["counts"]}

    def reset(self, *, expected: dict | None = None) -> None:
        def restore_original(lay: Layout) -> None:
            self.layout = lay.original()

        self.edit(restore_original, expected=expected)

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

    async def _observe(self, reference) -> _SourceObservation:
        state, library, reported_clips = await self._read_source(reference)
        pins = copy.deepcopy(getattr(self.source, "pins", None))
        layout = Layout(state, library=library, grid=self.grid)
        if pins is not None:
            node_pins(layout, pins, strict=True)
        return _SourceObservation(state, library, pins, copy.deepcopy(getattr(self.source, "page_identity", {})),
                                  layout, copy.deepcopy(getattr(self.source, "source_archive", None)),
                                  getattr(self.source, "writes_leaf_dictionaries", True), reported_clips)

    def _backup(self, state: list[Any], library: list[Any] | None, label: str,
                pins: list[dict[str, int]] | None = None, *, observed: Layout | None = None,
                archive: dict[str, Any] | None = None) -> Path:
        if not self.device_id:
            raise LayoutError("device identity is required for a backup")
        p = self._dev_dir("backups") / f"{_stamp()}-{label}-{uuid.uuid4().hex}.plist"
        blob = plistlib.loads(_pack(state, library, pins))
        if archive is not None:
            blob["source_archive"] = archive
        provenance = {"version": 1, "device_id": self.device_id, "grid": self.grid.to_dict()}
        if archive is not None:
            provenance["archive_pins_reported"] = getattr(self.source, "pins", None) is not None
        if observed is not None:
            provenance["pin_keys"] = "wire-v1"
            bound = _instance_ids(observed, self._gap_memory())
            if bound:
                provenance["folder_instances"] = [[pin_key(n) for n in page if n.id in bound and n.id in observed.pins]
                                                  for page in observed.pages]
        pages = _overflow_widget_pages(observed) if observed is not None else []
        if pages:
            provenance["source_shape"] = _source_shape_receipt(blob, self.device_id, self.grid, pages)
        blob["provenance"] = provenance
        data = plistlib.dumps(blob, fmt=plistlib.FMT_BINARY)
        _publish_exclusive(p, data)
        if pages:
            receipt_dir = self._dev_dir("observed-backups")
            _publish_exclusive(receipt_dir / p.name, hashlib.sha256(data).digest())
            _sync_directory(receipt_dir.parent)
        # The device and backups directories may have been created by this call.
        _sync_directory(p.parent.parent)
        _sync_directory(self.dir)
        return p

    async def _write_verified(self, target: list[Any], target_lib: list[Any] | None,
                              label: str, pins: list[dict[str, int]] | None = None,
                              expected: tuple[list[Any], list[Any] | None] | None = None,
                              expected_pins: Any = _MISSING, prepared: Layout | None = None,
                              backup_instances: Set[str] = frozenset(), page_archive: dict | None = None) -> dict[str, Any]:
        # Invalid requested pins must fail before a backup, pending marker or source write.
        want_pins = pins or []
        if self._placement_revision != self._saved_placement_revision():
            raise LayoutError("the device placement changed in another session; read the device again")
        reference = expected
        if reference is None and self.layout is not None:
            reference = (self.layout.source, self.layout.source_library)
        await self._assert_identity()
        current = await self._observe(reference)
        previous_memory = self._gap_memory()
        original = self.require().original() if expected is not None else current.layout
        bound_ids = _instance_ids(original, previous_memory)
        if expected is not None:
            if current.reported_clips is not None:
                reported = _clip_inventory(original) if self._reported_clips is _MISSING else self._reported_clips
                if reported != current.reported_clips:
                    raise LayoutError("the phone's reported web clips changed since it was read; read again")
            if _carried_layout(current.layout, current.leaf_dicts) != _carried_layout(original, current.leaf_dicts):
                raise LayoutError("the phone's layout changed since it was read; read again and redo the edits")
            if expected_pins is not _MISSING and not same_reported_pins(current.pins, expected_pins):
                raise LayoutError("the phone's exported gaps changed since it was read; read again and redo the edits")
            if not _compatible_folder_ids(original, current.layout, bound_ids):
                raise LayoutError("the phone's layout changed since it was read; read again and redo the edits")
            if current.pins is not None and _anchor_owners(current.layout) != _anchor_owners(original):
                raise LayoutError("the phone's exported gaps changed since it was read; read again and redo the edits")
            if self._page_identity is not _MISSING and self._page_identity != current.page_identity:
                raise LayoutError("the phone's page identifiers changed since it was read; read again and redo the edits")
        target = copy.deepcopy(target)
        self._recover_widget_ids(_widgets(target))
        if target_lib is not None:
            target_lib = self._with_webclips(target, target_lib)
        requested = Layout(target, library=target_lib, grid=self.grid)
        node_pins(requested, want_pins, strict=True)
        # Both reads must describe the same page owners and coordinate frame.
        final = await self._observe((current.state, current.library))
        if (not current.same_layout(final) or not _compatible_folder_ids(current.layout, final.layout)
                or not _compatible_folder_ids(original, final.layout, bound_ids)):
            raise LayoutError("the phone's layout changed during the final prewrite read; read again")
        if not current.same_placement(final):
            raise LayoutError("the phone's exported gaps changed during the final prewrite read; read again")
        saved, saved_pins = self._prepared_layout(self._layout_from(final.state, final.library))
        expected_page_ids = None
        if prepared is not None and final.archive is not None and "listUniqueIdentifiers" in final.archive:
            old_ids = final.archive["listUniqueIdentifiers"]
            if len(prepared.page_origins) != len(target) - 1:
                raise LayoutError("page ownership changed; read the device again before writing")
            expected_page_ids = archive_page_order(final.archive, prepared.page_origins)
            if not prepared.page_origins_known and page_fields(final.archive, len(old_ids)):
                raise LayoutError("the saved draft has no original page ownership; read the device again")
            if expected_page_ids != old_ids:
                page_archive = {"listUniqueIdentifiers": expected_page_ids,
                                "listMetadata": final.archive.get("listMetadata", {})}
        elif page_archive is not None and final.archive is not None:
            expected_page_ids = page_archive.get("listUniqueIdentifiers")
        archive_args = {}
        retained_fields = page_fields(page_archive if page_archive is not None else final.archive or {}, len(target) - 1)
        if page_archive is not None and final.archive is not None:
            archive_args["page_archive"] = page_archive
        elif retained_fields and final.archive is None:
            raise LayoutError("this source cannot restore the backup's exported page metadata")
        prepared_write = None
        prepare_write = getattr(self.source, "prepare_write", None)
        if prepare_write is not None:
            target_lib, prepared_write = await asyncio.wait_for(
                prepare_write(target, target_lib, want_pins, self.grid, **archive_args), OPERATION_TIMEOUT)
        info = await self._assert_identity()
        self._check_placement(requested, saved, info)
        check_write = getattr(self.source, "check_write", None)
        if check_write is not None:
            check_write(want_pins, target_lib)
        check_frame = getattr(self.source, "check_archive_frame", None)
        if check_frame:
            check_frame(target, self.grid)
        # A list-only reader and writer represent the empty App Library as unavailable.
        if current.library is None and not target_lib:
            target_lib = None
        if prepared is not None:
            requested_memory = memory_pins(prepared, previous_memory, bound_ids)
        else:
            requested_memory = memory_pins(requested, previous_memory, backup_instances)
        backup = self._backup(saved.to_state(), saved.to_library(), label, saved_pins, observed=saved, archive=final.archive)
        lib_known = target_lib is not None
        _publish_exclusive(self._pending_path, json.dumps({"device_id": self.device_id,
                           "backup": backup.name, "status": "pending"}).encode())
        write_error = None
        try:
            operation = (prepared_write() if prepared_write is not None
                         else self.source.write(target, target_lib, want_pins, self.grid, **archive_args))
            await asyncio.wait_for(operation, OPERATION_TIMEOUT)
        except Exception as e:
            write_error = type(e).__name__
        verification_note = "Readback compares the layout fields returned by the selected source."
        try:
            after, after_lib, after_clips = await self._read_source((target, target_lib), verifying=True)
            await self._assert_identity()
            got_pins = getattr(self.source, "pins", None)
            unreported = got_pins is None
            observed = Layout(after, library=after_lib, grid=self.grid)
            if not _compatible_folder_ids(requested, observed, _instance_ids(requested, requested_memory), ordered=False):
                raise LayoutError("readback changed a folder's owner")
            if unreported:
                # Retain requested gaps under the no-pin-map contract. Match folders by
                # exported content without adding outgoing UUIDs to the returned state.
                if write_error is not None:
                    submitted = prepared if prepared is not None else requested
                    obligations = bound_ids if prepared is not None else backup_instances
                    proposed = memory_pins(submitted, previous_memory, obligations, typed=True)["pages"]
                    prior = memory_pins(saved, previous_memory, typed=True)["pages"]
                    if {i: p for i, p in enumerate(proposed) if p} != {i: p for i, p in enumerate(prior) if p}:
                        raise LayoutError("write failed and the source did not report the changed fixed positions")
                got_pins = requested_memory
            node_pins(observed, got_pins, by_memory=unreported, strict=True)
            if not _retains_pins(requested, observed, requested_memory):
                raise LayoutError("readback did not retain every requested fixed position")
            ok = (placed_structure(after, after_lib if lib_known else None, got_pins, self.grid,
                                   by_memory=unreported, leaf_dicts=final.leaf_dicts)
                  == placed_structure(target, target_lib, want_pins, self.grid, leaf_dicts=final.leaf_dicts))
            if expected_page_ids is not None:
                raw = getattr(self.source, "source_archive", None) or {}
                if raw.get("listUniqueIdentifiers") != expected_page_ids:
                    raise LayoutError("readback changed the exported page identifiers")
            if retained_fields:
                reported = page_fields(getattr(self.source, "source_archive", None) or {}, len(after) - 1)
                for pid, wanted in retained_fields.items():
                    present = {k: v for k, v in reported.get(pid, {}).items() if k in wanted}
                    if plistlib.dumps(wanted, fmt=plistlib.FMT_BINARY) != plistlib.dumps(present, fmt=plistlib.FMT_BINARY):
                        raise LayoutError("readback changed an exported page field")
        except Exception as e:
            readback_error = type(e).__name__
            result = {"ok": False, "status": "unknown", "backup": backup.name, "recovery": "read",
                      "warnings": [verification_note], "readback_error": readback_error,
                      "error": f"Readback failed ({readback_error}); run Read before applying another change."}
            if write_error:
                result["write_error"] = write_error
                result["error"] = f"Write returned {write_error}. " + result["error"]
            return result
        res: dict[str, Any] = {"ok": ok, "status": "verified" if ok else "unknown",
                               "backup": backup.name, "warnings": [verification_note]}
        if write_error is not None:
            res["write_error"] = write_error
            res["error"] = (f"Write returned {write_error}. "
                            f"Readback result: {'matched' if ok else 'different'}.")
        if after_lib is not None:
            res["app_library_only"] = len(after_lib)
        if not ok:
            res["recovery"] = "read"
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
        if not ok:
            return res
        written = Layout(after, library=after_lib, grid=self.grid)
        node_pins(written, got_pins, by_memory=unreported, strict=True)
        adopted = Layout(after, library=after_lib, pins=written.pins, grid=self.grid)
        adopted.approval_device = self.device_id
        previous = (self.layout, self._observed_placement, self._page_identity,
                    self._reported_clips, self._draft_digest, self._placement_revision)
        try:
            with (_previous_file(self._gaps_path), _previous_file(self.draft_path),
                  _previous_file(self._placement_revision_path)):
                self._placement_revision = uuid.uuid4().hex
                _private_replace(self._placement_revision_path, self._placement_revision.encode())
                self._remember_gaps(written, requested_memory)
                self.layout = adopted
                self._observed_placement = copy.deepcopy(getattr(self.source, "pins", None))
                self._page_identity = copy.deepcopy(getattr(self.source, "page_identity", {}))
                self._reported_clips = after_clips
                self._save_draft()
                self._clear_pending()
        except BaseException as e:
            (self.layout, self._observed_placement, self._page_identity, self._reported_clips, self._draft_digest,
             self._placement_revision) = previous
            if isinstance(e, Exception):
                detail = f"Readback matched. Saving the local draft failed ({type(e).__name__}). Run Read to recover."
                return {**res, "ok": False, "status": "unknown", "readback": "matched",
                        "local_adoption": "failed", "recovery": "read",
                        "error": f"{res['error']} {detail}" if "error" in res else detail}
            raise
        self._undo, self._redo = [], []
        self._history_baseline = None
        return res

    async def apply(self, token: str, *, expected: dict | None = None) -> dict[str, Any]:
        require_writable()
        with self._exclusive(expected=expected):
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
                                          self._observed_placement, prepared)

    def backups(self) -> list[str]:
        return sorted(p.name for p in self._dev_dir("backups").glob("*.plist"))

    def _backup_bytes(self, name: str) -> bytes:
        directory = self._dev_dir("backups")
        if not name or Path(name).name != name:
            raise LayoutError("invalid backup filename")
        try:
            fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                return stream.read()
        except OSError as e:
            raise LayoutError(f"no backup named {name!r}") from e

    def _restore_review(self, name: str, data: bytes) -> str:
        body = [self.device_id, self.binding["revision"], name, hashlib.sha256(data).digest()]
        return hashlib.sha256(plistlib.dumps(body, fmt=plistlib.FMT_BINARY)).hexdigest()

    def review_backup(self, name: str, expected: dict) -> str:
        with self._exclusive(expected=expected):
            self.require()
            return self._restore_review(name, self._backup_bytes(name))

    async def restore(self, name: str, *, expected: dict | None = None, review: str | None = None) -> dict[str, Any]:
        require_writable()
        with self._exclusive(expected=expected):
            self._require_known()
            if self._draft_error:
                raise LayoutError(self._draft_error)
            data = self._backup_bytes(name)
            if expected is not None and review != self._restore_review(name, data):
                raise ReviewChanged("the reviewed backup changed; select the backup again")
            return await self._restore_locked(name, data)

    async def _restore_locked(self, name: str, data: bytes) -> dict[str, Any]:
        try:
            blob = plistlib.loads(data)
            provenance = blob.get("provenance") if isinstance(blob, dict) else None
            if (not isinstance(provenance, dict) or type(provenance.get("version")) is not int
                    or provenance["version"] != 1):
                raise LayoutError("backup has no trusted device identity provenance; legacy backups cannot be restored")
            if not self.device_id or provenance.get("device_id") != self.device_id:
                raise LayoutError("backup device identity does not match this Session's device")
            state, library = _unpack(data)
            if not isinstance(state, list) or (library is not None and not isinstance(library, list)):
                raise ValueError("invalid layout payload")
            restored = Layout(state, library=library, grid=self.grid)
            page_archive = None
            archive = blob.get("source_archive") if isinstance(blob, dict) else None
            if archive is not None:
                if not isinstance(archive, dict):
                    raise ValueError("invalid saved source archive")
                original_state, original_lib = disk_to_model(archive)
                _restore_archive_app_ids(original_state, original_lib, (state, library))
                original = Layout(original_state, library=original_lib, grid=restored.grid)
                metadata = disk_pins(archive)
                if metadata.archive:
                    page_archive = metadata.archive
            pins = _unpack_pins(data)
            saved_wire_pins = pins
            if pins is not None and (not isinstance(pins, list) or any(not isinstance(page, dict) for page in pins)):
                raise LayoutError("backup pins must identify items on each page")
            key_format = provenance.get("pin_keys")
            if "pin_keys" in provenance and key_format != "wire-v1":
                raise LayoutError("backup pin identifier format is unsupported")
            dimensions = provenance.get("grid")
            old_grid = None
            if dimensions is not None:
                if not isinstance(dimensions, dict) or not {"cols", "rows"} <= dimensions.keys():
                    raise LayoutError("backup grid dimensions must be positive integers")
                old_grid = Grid(**dimensions)
                if any(type(value) is not int or value <= 0 for value in old_grid.to_dict().values()):
                    raise LayoutError("backup grid dimensions must be positive integers")
            if pins and any(pins):
                if old_grid is None:
                    raise LayoutError("pinned backup has no saved grid dimensions; use a backup with grid provenance")
                mapped = []
                for index, page_pins in enumerate(pins):
                    page = restored.pages[index] if index < len(restored.pages) else []
                    nodes = {}
                    for node in page:
                        aliases = {pin_key(node)}
                        if key_format is None and node.bundle_id:
                            aliases.add(node.bundle_id)
                        for key in aliases - {None}:
                            nodes.setdefault(key, []).append(node)
                    mapped_page = {}
                    for key, cell in page_pins.items():
                        if key not in nodes or type(cell) is not int or not 0 <= cell < old_grid.slots:
                            raise LayoutError("backup pin is outside its saved grid or does not identify an item on its page")
                        if len(nodes[key]) != 1:
                            raise LayoutError("ambiguous pin identifier on a backup page")
                        row, column = divmod(cell, old_grid.cols)
                        node = nodes[key][0]
                        wire = pin_key(node)
                        if wire in mapped_page:
                            raise LayoutError("ambiguous pin identifier on a backup page")
                        width, height = restored.dims(node)
                        if (column + width > min(old_grid.cols, self.grid.cols)
                                or row + height > min(old_grid.rows, self.grid.rows)):
                            raise LayoutError("backup pin footprint does not fit the saved or loaded grid")
                        mapped_page[wire] = row * self.grid.cols + column
                    mapped.append(mapped_page)
                pins = mapped
                node_pins(restored, pins, strict=True)
            if page_archive is not None:
                metadata.validate([{pin_key(n) for n in page} for page in original.pages], old_grid or self.grid)
                reported = provenance.get("archive_pins_reported", "listMetadata" in archive)
                if type(reported) is not bool:
                    raise LayoutError("backup archive placement evidence is invalid")
                if reported and metadata.pages(len(original.pages)) != (saved_wire_pins or [{} for page in original.pages]):
                    raise LayoutError("backup archive positions differ from the saved layout")
            bound = set()
            if "folder_instances" in provenance:
                pages = provenance["folder_instances"]
                if not isinstance(pages, list) or len(pages) != len(restored.pages):
                    raise LayoutError("backup folder instance does not identify a pinned folder on its page")
                for page, wires in zip(restored.pages, pages):
                    owners = {pin_key(n): n.id for n in page if n.kind == "folder" and n.id in restored.pins}
                    if not isinstance(wires, list) or any(not isinstance(w, str) or w not in owners for w in wires):
                        raise LayoutError("backup folder instance does not identify a pinned folder on its page")
                    bound.update(owners[w] for w in wires)
            problems = restored.validate()
            observed_pages = []
            receipt = provenance.get("source_shape")
            if receipt and old_grid == self.grid:
                pages = _overflow_widget_pages(restored)
                body = {k: v for k, v in blob.items() if k != "provenance"}
                record = self._dev_dir("observed-backups") / name
                if (receipt == _source_shape_receipt(body, self.device_id, self.grid, pages)
                        and record.is_file() and not record.is_symlink()
                        and record.read_bytes() == hashlib.sha256(data).digest()):
                    observed_pages = pages
            for index, page in enumerate(restored.pages):
                if index not in observed_pages:
                    problems += [f"page {index + 1}: {problem}" for problem in restored.place(page)[1]]
            if problems:
                raise LayoutError(f"backup layout has problems, not restoring: {problems}")
            if archive is not None:
                raw_pages, raw_lib = _carried_layout(original, True)
                saved_pages, _ = _carried_layout(restored, True)
                if raw_pages != saved_pages or not _compatible_folder_ids(original, restored):
                    raise LayoutError("backup archive page owners differ from the saved layout")
                inventory = Counter(json.dumps(value, sort_keys=True) for value in raw_lib or [])
                for node in restored.library or []:
                    key = json.dumps(_carried_node(node, True), sort_keys=True)
                    if inventory[key]:
                        inventory[key] -= 1
                    elif node.kind != "webclip":
                        raise LayoutError("backup archive library differs from the saved layout")
                if any(inventory.values()):
                    raise LayoutError("backup archive library differs from the saved layout")
        except LayoutError:
            raise
        except (ValueError, TypeError, KeyError, plistlib.InvalidFileException) as e:
            raise LayoutError("backup identity or layout data is invalid") from e
        return await self._write_verified(state, library, "before-restore", pins, backup_instances=bound,
                                          page_archive=page_archive)
