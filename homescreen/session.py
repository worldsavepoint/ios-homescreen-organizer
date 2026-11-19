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
import re
import stat
import tempfile
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from collections import Counter, OrderedDict
from collections.abc import Set
from pathlib import Path
from typing import Any, Callable
from xml.parsers.expat import ExpatError

from . import colors, organize
from .device import (DISK_MARK, NAME_MARK, ObservedPins, Source, archive_page_order, disk_pins,
                     disk_to_model, page_fields, require_writable, today_evidence)
from .model import Grid, Layout, LayoutError, Node, is_folder, is_widget, node_structure, same_fields, widget_configuration
from .private_store import ensure_private_directory, open_private_directory, open_private_file

KEEP_READS = 10
MAX_UNDO = 200
MAX_ICON_COLORS = 256
OPERATION_TIMEOUT = 30.0
_MISSING = object()  # Older drafts did not record the source's placement observation.


class ReviewChanged(LayoutError):
    """The editor action was prepared against a different device, draft or backup."""


class _ReadbackMismatch(LayoutError):
    def __init__(self, reason, details=None):
        super().__init__(reason)
        self.details = details


@contextmanager
def _local_directory(path: Path, root: Path | None = None, directory_fd: int | None = None):
    if directory_fd is not None:
        yield directory_fd
    else:
        with open_private_directory(root or path, path) as fd:
            yield fd


def _sync_directory(path: Path, *, root: Path | None = None, directory_fd: int | None = None) -> None:
    with _local_directory(path, root, directory_fd) as fd:
        os.fsync(fd)


def _exclusive_file(path: Path, data: bytes, *, root: Path | None = None, directory_fd: int | None = None) -> None:
    """Create a private file without overwriting another operation's recovery data."""
    with _local_directory(path.parent, root, directory_fd) as directory:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        owned = os.fstat(fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                if stream.write(data) != len(data):
                    raise OSError("incomplete local file write")
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(path.parent, directory_fd=directory)
        except BaseException:
            with suppress(FileNotFoundError):
                if os.path.samestat(owned, os.stat(path.name, dir_fd=directory, follow_symlinks=False)):
                    _unlink_file(path, directory_fd=directory)
            raise


def _publish_exclusive(path: Path, data: bytes, *, root: Path | None = None) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with _local_directory(path.parent, root) as directory:
        try:
            _exclusive_file(temporary, data, directory_fd=directory)
            os.link(temporary.name, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            _sync_directory(path.parent, directory_fd=directory)
        finally:
            with suppress(OSError):
                _unlink_file(temporary, directory_fd=directory, missing_ok=True)


@contextmanager
def _previous_file(path: Path, *, root: Path | None = None, directory_fd: int | None = None):
    """Keep the old inode until all updates in the transaction succeed."""
    previous = path.with_name(f".{path.name}.{uuid.uuid4().hex}.previous")
    with _local_directory(path.parent, root, directory_fd) as directory:
        linked = False
        try:
            info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise PermissionError("the Session data file must be a regular owned file")
            os.link(path.name, previous.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            linked = True
        except FileNotFoundError:
            pass
        try:
            yield directory
        except BaseException:
            if linked:
                os.replace(previous.name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            else:
                _unlink_file(path, directory_fd=directory, missing_ok=True)
            _sync_directory(path.parent, directory_fd=directory)
            raise
        finally:
            if linked:
                # Removing a private rollback link is housekeeping after the commit.
                with suppress(OSError):
                    _unlink_file(previous, directory_fd=directory, missing_ok=True)


def _unlink_file(path: Path, *, root: Path | None = None, directory_fd: int | None = None, missing_ok=False):
    with _local_directory(path.parent, root, directory_fd) as directory:
        try:
            os.unlink(path.name, dir_fd=directory)
        except FileNotFoundError:
            if not missing_ok:
                raise


def _private_replace(path: Path, data: bytes, *, root: Path | None = None, directory_fd: int | None = None) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with _local_directory(path.parent, root, directory_fd) as directory:
        try:
            _exclusive_file(temporary, data, directory_fd=directory)
            with _previous_file(path, directory_fd=directory):
                os.replace(temporary.name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                _sync_directory(path.parent, directory_fd=directory)
        finally:
            with suppress(OSError):
                _unlink_file(temporary, directory_fd=directory, missing_ok=True)


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


_READ_NAME = re.compile(r"([0-9]{8}-[0-9]{6})(?:~([0-9]{10}))?\.plist")


def _read_key(name: str) -> tuple[str, int] | None:
    match = _READ_NAME.fullmatch(name)
    if match:
        stamp, sequence = match.groups()
        try:
            dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S")
        except ValueError:
            return None
        return stamp, int(sequence or 0)
    return None


@contextmanager
def _read_snapshot(root: Path, reads: Path, data: bytes):
    """Publish one observation and roll back only the inode created by this Read."""
    stamp = _stamp()
    temporary = f".read-{uuid.uuid4().hex}.tmp"
    with open_private_directory(root, reads) as directory:
        _exclusive_file(reads / temporary, data, directory_fd=directory)
        try:
            latest = max((key for name in os.listdir(directory) if (key := _read_key(name))),
                         default=(stamp, 0))
            stamp, previous = max(latest, (stamp, 0))
            sequence = previous + 1
            while True:
                if sequence > 9_999_999_999:
                    stamp = (dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S") +
                             dt.timedelta(seconds=1)).strftime("%Y%m%d-%H%M%S")
                    sequence = 1
                name = f"{stamp}~{sequence:010d}.plist"
                try:
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    break
                except FileExistsError:
                    sequence += 1
            try:
                _sync_directory(reads, directory_fd=directory)
                yield
            except BaseException:
                if os.path.samestat(os.stat(temporary, dir_fd=directory, follow_symlinks=False),
                                    os.stat(name, dir_fd=directory, follow_symlinks=False)):
                    _unlink_file(reads / name, directory_fd=directory)
                    _sync_directory(reads, directory_fd=directory)
                raise
        finally:
            with suppress(OSError):
                _unlink_file(reads / temporary, directory_fd=directory, missing_ok=True)


def _pending_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record = {}
    for key, value in pairs:
        if key in record:
            raise LayoutError("pending write evidence has repeated fields; its original record was retained")
        record[key] = value
    return record


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


def _pin_mismatch(requested: Layout, observed: Layout, memory: dict[str, Any] | None = None, *,
                  changed_folders: set | None = None) -> dict | None:
    instances = _instance_ids(requested, memory or {})
    for index, page in enumerate(requested.pages):
        pinned = [node for node in page if node.id in requested.pins and
                  (changed_folders is None or (node.kind == "folder" and pin_key(node) in changed_folders))]
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
                    matches = []
            if len(matches) != 1 or observed.pins.get(matches[0].id) != cell:
                actual = observed.pins.get(matches[0].id) if len(matches) == 1 else None
                result = {"owner": owner.id, "page": index + 1, "requested_cell": cell, "observed_cell": actual}
                wire = pin_key(owner)
                if len(matches) != 1 or pin_key(matches[0]) != wire:
                    # A unique folder can be exported with a fresh wire ID. Instance-bound
                    # folders must still match their wire, as in the requested-page check.
                    use_wire = bool(wire and any(pin_key(n) == wire and n.kind == owner.kind
                                                 for n in observed.nodes.values()))
                    def same_owner(node):
                        return (node.kind == owner.kind and node_structure(node) == shape
                                and (not use_wire or pin_key(node) == wire))

                    locations = [(i, node) for i, nodes in enumerate(observed.pages, 1)
                                 for node in nodes if same_owner(node)]
                    if ((use_wire or (owner.kind == "folder" and owner.id not in instances))
                            and len(locations) == 1 and locations[0][0] != index + 1
                            and sum(same_owner(n) for n in observed.nodes.values()) == 1
                            and sum(same_owner(n) for n in requested.nodes.values()) == 1):
                        actual = observed.pins.get(locations[0][1].id)
                        if actual is not None:
                            result.update(observed_page=locations[0][0], observed_cell=actual)
                for label, value in (("requested", cell), ("observed", actual)):
                    if value is not None:
                        row, col = divmod(value, requested.grid.cols)
                        result[label] = {"row": row + 1, "column": col + 1}
                if changed_folders is None or "observed_page" in result:
                    return result
    return None


def _retains_pins(requested: Layout, observed: Layout, memory: dict[str, Any] | None = None) -> bool:
    return _pin_mismatch(requested, observed, memory) is None


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
                  if k not in (NAME_MARK, "iconLists", "uniqueIdentifier")}
        return [node.name, plistlib.dumps(fields, fmt=plistlib.FMT_BINARY).hex(),
                [[_carried_node(c, leaf_dicts) for c in page] for page in node.exported_folder_pages()]]
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


def _changed_folder_wires(before: Layout, after: Layout, bound_ids: Set[str], *, ordered: bool) -> set:
    old, ambiguous = _folder_owners(before, ordered=ordered)
    new, _ = _folder_owners(after, ordered=ordered)
    required = ambiguous | {pin_key(before.nodes[nid]) for nid in bound_ids}
    return (required - new.keys()) | {wire for wire in old.keys() & new.keys() if old[wire] != new[wire]}


def _compatible_folder_ids(before: Layout, after: Layout, bound_ids: Set[str] = frozenset(), *,
                           ordered: bool = True) -> bool:
    return not _changed_folder_wires(before, after, bound_ids, ordered=ordered)


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
        self.folder_columns: int | None = None
        self._observed_placement = _MISSING
        self._page_identity = _MISSING
        self._reported_clips = _MISSING
        self._placement_revision = ""
        self._draft_digest: bytes | None = None
        self._icon_colors: OrderedDict[bytes, dict] = OrderedDict()
        self.usage: dict[str, Any] | None = None  # Screen Time opens, loaded on request
        self._undo: list[dict[str, Any]] = []
        self._redo: list[dict[str, Any]] = []
        self._history_baseline: dict[str, Any] | None = None
        self._load_draft()

    # ---- persistence ------------------------------------------------------------

    def _read_local(self, path: Path) -> bytes:
        with os.fdopen(open_private_file(self.dir, path), "rb") as stream:
            return stream.read()

    def _exists_local(self, path: Path) -> bool:
        try:
            fd = open_private_file(self.dir, path)
        except FileNotFoundError:
            return False
        os.close(fd)
        return True

    def _local_files(self, directory: Path, suffix: str) -> list[Path]:
        with open_private_directory(self.dir, directory) as fd:
            return [directory / name for name in os.listdir(fd) if name.endswith(suffix)
                    and stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)]

    @contextmanager
    def _exclusive(self, *, refresh: bool = False, expected: dict | None = None):
        fd = open_private_file(self.dir, self.dir / ".session.lock", os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise LayoutError("another Session operation holds the local data lock; try again after it finishes") from e
            if expected is not None and expected != self.binding:
                raise ReviewChanged("the reviewed device or draft changed; review the current layout")
            if not refresh or expected is not None:
                try:
                    digest = hashlib.sha256(self._read_local(self.draft_path)).digest()
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

    @contextmanager
    def refreshed_draft(self):
        """Present a saved draft and its binding under the same local lock."""
        with self._exclusive(refresh=True):
            try:
                digest = hashlib.sha256(self._read_local(self.draft_path)).digest()
            except FileNotFoundError:
                digest = None
            if digest != self._draft_digest:
                fresh = Session(self.source, self.dir, device=self._draft_key)
                self.layout, self.device_id, self.info, self.grid = fresh.layout, fresh.device_id, fresh.info, fresh.grid
                self.folder_columns = fresh.folder_columns
                self._draft_error, self._draft_digest = fresh._draft_error, fresh._draft_digest
                self._observed_placement, self._page_identity = fresh._observed_placement, fresh._page_identity
                self._reported_clips, self._placement_revision = fresh._reported_clips, fresh._placement_revision
                self.usage = None
                self._undo, self._redo = [], []
                self._history_baseline = None
            yield

    @property
    def _pending_path(self) -> Path:
        return self.dir / ".pending-write.json"

    def _require_known(self) -> None:
        if self._exists_local(self._pending_path):
            raise LayoutError("a pending write has an unknown outcome; perform an explicit read before any further write")

    def _clear_pending(self) -> None:
        if self._exists_local(self._pending_path):
            with _previous_file(self._pending_path, root=self.dir) as directory:
                _unlink_file(self._pending_path, directory_fd=directory)
                _sync_directory(self.dir, directory_fd=directory)

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
            with open_private_directory(self.dir, self.dir / "drafts"):
                pass
            return self.dir / "drafts" / f"{_safe(self._draft_key)}.plist"
        return self.dir / "draft.plist"

    def _dev_dir(self, kind: str) -> Path:
        p = self.dir / kind / _safe(self.device_id or "unknown")
        with open_private_directory(self.dir, p):
            pass
        return p

    def _save_draft(self) -> None:
        assert self.layout is not None
        blob = {"device": self.device_id, "info": self.info, "draft_revision": uuid.uuid4().hex,
                **self.layout.current_fields(), **self.layout.baseline()}
        folders = {n.id: n.saved_folder_fields() for n in self.layout.nodes.values() if n.kind == "folder"}
        if folders:
            blob["saved_folder_fields"] = folders
        blob["placement_revision"] = self._placement_revision
        if self.folder_columns is not None:
            blob["folder_columns"] = self.folder_columns
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
        _private_replace(self.draft_path, data, root=self.dir)
        self._draft_digest = hashlib.sha256(data).digest()

    def _load_draft(self) -> None:
        if self._exists_local(self.draft_path):
            data = self._read_local(self.draft_path)
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
                lay.recover_saved_folders(original, blob)
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
                columns = blob.get("folder_columns")
                if columns is not None and (type(columns) is not int or columns < 1 or lay.grid.folder % columns):
                    raise ValueError("invalid folder grid")
                self.folder_columns = columns
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
                target = steps[-1]
                baseline_keys = {"source", "source_library", "source_identity", "source_pins"}
                current = copy.deepcopy({k: v for k, v in target.items() if k not in baseline_keys})
                self.layout = Layout.from_snapshot({**target, **current})
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

    @contextmanager
    def _usage_review(self, expected: dict, selected: Callable[[], bool] | None):
        with self._exclusive(expected=expected):
            if selected is not None and not selected():
                raise ReviewChanged("the reviewed device changed; load usage for the current device")
            yield self.require()

    async def _assert_usage_identity(self, expected: dict, selected: Callable[[], bool] | None) -> None:
        with self._usage_review(expected, selected):
            pass
        await self._assert_identity()
        with self._usage_review(expected, selected):
            pass

    def _adopt_usage(self, result: dict, expected: dict, selected: Callable[[], bool] | None) -> dict:
        with self._usage_review(expected, selected):
            self.usage = result
        return result

    def load_usage(self, days: int = 30, *, expected: dict | None = None,
                   selected: Callable[[], bool] | None = None) -> dict[str, Any]:
        from . import usage
        binding = self.binding if expected is None else expected
        with self._usage_review(binding, selected) as lay:
            known = {n.bundle_id for n in lay.nodes.values() if n.bundle_id}
        return self._adopt_usage(usage.load(known, days), binding, selected)

    async def load_device_usage(self, *, expected: dict | None = None,
                                selected: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Usage from the device's own memory reports (no Screen Time sync needed)."""
        from . import usage
        binding = self.binding if expected is None else expected
        with self._usage_review(binding, selected) as lay:
            known = {n.bundle_id for n in lay.nodes.values() if n.bundle_id}
        if not hasattr(self.source, "memory_reports"):
            return self._adopt_usage({"ok": False, "error": "This source can't report usage."}, binding, selected)
        await self._assert_usage_identity(binding, selected)
        with self._usage_review(binding, selected):
            directory = self._dev_dir("usage")
        with tempfile.TemporaryDirectory(prefix="homescreen-usage-") as temporary:
            staging = Path(temporary)
            ensure_private_directory(staging)
            files = await self.source.memory_reports(staging)
            await self._assert_usage_identity(binding, selected)
            apps = await self.source.installed_apps()
            await self._assert_usage_identity(binding, selected)
            for path in files:
                with os.fdopen(open_private_file(staging, path), "rb") as stream:
                    _private_replace(directory / path.relative_to(staging), stream.read(), root=self.dir)
            result = usage.from_memory_reports(files, apps, known)
        return self._adopt_usage(result, binding, selected)

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
        if self._exists_local(path):
            stored = json.loads(self._read_local(path))
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
                {"version": 3, "instances": known, "owners": owners}).encode(), root=self.dir)

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

    def _known_webclips(self) -> list[dict[str, Any]]:
        path = self._webclips_path
        try:
            known = plistlib.loads(self._read_local(path))
        except FileNotFoundError:
            try:
                known = json.loads(self._read_local(path.with_suffix(".json")))
            except FileNotFoundError:
                return []
        if isinstance(known, dict):
            # Legacy keys were draft-local node IDs. The raw records retain their wire owners.
            known = [n.raw for n in Layout([[], list(known.values())]).nodes.values() if n.kind == "webclip"]
        return known

    @staticmethod
    def _clip_proxy(raw):
        # Dictionary archives can report only the bare wire identifier and its display label.
        return raw.keys() <= {"bundleIdentifier", "displayIdentifier", "displayName", DISK_MARK}

    @staticmethod
    def _missing_webclips(known, observed):
        groups = {}
        for raw in known:
            wire = raw.get("displayIdentifier") or raw.get("bundleIdentifier") or raw.get("webClipURL")
            groups.setdefault(wire, []).append(raw)
        unmatched = []
        for raw in observed:
            wire = raw.get("displayIdentifier") or raw.get("bundleIdentifier") or raw.get("webClipURL")
            available = groups.get(wire, [])
            exact = next((i for i, known in enumerate(available) if same_fields(raw, known)), None)
            if exact is not None:
                available.pop(exact)
            else:
                unmatched.append((wire, raw))
        def semantic_bundle(raw):
            # A converted bare string repeats its wire ID in the model's bundle field.
            return None if raw.get(DISK_MARK) else raw.get("bundleIdentifier")

        for wire, raw in sorted(unmatched, key=lambda pair: not semantic_bundle(pair[1])):
            available = groups.get(wire, [])
            bundle = semantic_bundle(raw)
            candidates = [(i, item) for i, item in enumerate(available)
                          if not bundle or semantic_bundle(item) in (None, bundle)]
            if candidates:
                match, _ = max(candidates, key=lambda pair: (
                    bool(bundle and semantic_bundle(pair[1]) == bundle),
                    pair[1].get("webClipURL") == raw.get("webClipURL")))
                available.pop(match)
        return [raw for records in groups.values() for raw in records]

    def _remember_webclips(self, state: list[Any], library: list[Any] | None) -> None:
        known = self._known_webclips()
        observed = [n.raw for n in Layout(state, library=library).nodes.values() if n.kind == "webclip"]
        explicit = [raw for raw in observed if not self._clip_proxy(raw)]
        proxies = [raw for raw in observed if self._clip_proxy(raw)]
        remaining = self._missing_webclips(known, explicit)
        updated = [*explicit, *remaining, *self._missing_webclips(proxies, remaining)]
        if not same_fields(updated, known):
            _private_replace(self._webclips_path, plistlib.dumps(updated, fmt=plistlib.FMT_BINARY), root=self.dir)

    def _with_webclips(self, state: list[Any], library: list[Any] | None) -> list[Any] | None:
        """library plus every remembered web clip that isn't on the Home Screen."""
        if library is None:
            return None
        leaves = Layout(state, library=library).nodes.values()
        clips = [node.raw for node in leaves if node.kind == "webclip" and not self._clip_proxy(node.raw)]
        proxies = [node.raw for node in leaves if node.kind in ("app", "webclip")
                   and self._clip_proxy(node.raw)]
        missing = self._missing_webclips(self._known_webclips(), clips)
        return library + self._missing_webclips(missing, proxies)

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
            return self._read_local(self._placement_revision_path).decode()
        except FileNotFoundError:
            return ""

    def _gap_memory(self) -> dict[str, Any]:
        if not self._exists_local(self._gaps_path):
            return {}
        try:
            memory = json.loads(self._read_local(self._gaps_path))
            if not isinstance(memory, dict):
                raise ValueError("invalid gap memory")
            return memory
        except (UnicodeDecodeError, ValueError) as e:
            raise LayoutError("invalid saved pin memory; read again") from e

    def _remember_gaps(self, lay: Layout, previous: dict[str, Any] | None = None) -> None:
        _private_replace(self._gaps_path, json.dumps(memory_pins(lay, previous), indent=1, sort_keys=True).encode(), root=self.dir)

    def _layout_from(self, state: list[Any], library: list[Any] | None, *, recover_memory: bool = False) -> Layout:
        """A Layout with gaps: from the source when it can report them, else from memory."""
        lay = Layout(state, library=library, grid=self.grid)
        reported = getattr(self.source, "pins", None)
        if reported is not None:
            node_pins(lay, reported)
        if self._exists_local(self._gaps_path) and (reported is None or recover_memory):
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
        return self._observed_placement is not _MISSING and self._observed_placement is not None

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
        metrics = info.get("icon_metrics")
        grid = Grid.from_metrics(metrics)
        folder_columns = int(metrics["homeScreenIconFolderColumns"]) if metrics and "homeScreenIconFolderColumns" in metrics else None
        state, library = await asyncio.wait_for(self.source.read(), OPERATION_TIMEOUT)
        verified = await asyncio.wait_for(self.source.info(), OPERATION_TIMEOUT)
        if verified.get("UniqueDeviceID") != device_id:
            raise LayoutError("device identity changed during read; no layout was loaded")
        final_metrics = verified.get("icon_metrics")
        if Grid.from_metrics(final_metrics) != grid:
            raise LayoutError("device grid changed during read; no layout was loaded")
        reported_columns = (final_metrics or {}).get("homeScreenIconFolderColumns")
        final_columns = int(reported_columns) if reported_columns is not None else None
        if (folder_columns or 3) != (final_columns or 3) or (folder_columns is not None and final_columns is None):
            raise LayoutError("device grid changed during read; no layout was loaded")
        if final_columns is not None:
            folder_columns = final_columns
        raw = Layout(state, library=library, grid=grid)  # validate the shape before committing identity
        reported_clips = _clip_inventory(raw) if getattr(self.source, "source_archive", None) is not None else None
        clear_pending = False
        legacy_pending = None
        if self._exists_local(self._pending_path):
            pending_bytes = self._read_local(self._pending_path)
            pending = json.loads(pending_bytes, object_pairs_hook=_pending_object)
            clear_pending = pending.get("device_id") == device_id
            name = pending.get("backup")
            if (set(pending) == {"device_id", "backup", "status"}
                    and isinstance(name, str) and name.endswith(".plist") and Path(name).name == name
                    and "\x00" not in name and getattr(self.source, "legacy_fixture_id", None) and
                    pending.get("device_id") == self.source.legacy_fixture_id and pending.get("status") == "pending"
                    ):
                legacy_pending = pending_bytes
        previous = (self.device_id, self.info, self.grid, self.layout, self._undo, self._redo, self.folder_columns,
                    self._observed_placement, self._page_identity, self._reported_clips,
                    self._draft_digest, self._placement_revision)
        if self.layout is not None and device_id == self.device_id:
            _restore_archive_app_ids(state, library, (self.layout.source, self.layout.source_library))
        try:
            self.device_id = device_id
            self.info = {k: v for k, v in info.items() if k in ("DeviceName", "ProductType", "ProductVersion")}
            self.grid = grid
            self.folder_columns = folder_columns
            reads = self._dev_dir("reads")
            with ExitStack() as transaction:
                for path in (self._widget_ids_path, self._webclips_path, self._gaps_path, self.draft_path):
                    transaction.enter_context(_previous_file(path, root=self.dir))
                self._remember_widget_ids(state)
                self._remember_webclips(state, library)
                library = self._with_webclips(state, library)
                transaction.enter_context(_read_snapshot(self.dir, reads, _pack(state, library)))
                snapshots = sorted(p for p in self._local_files(reads, ".plist") if _read_key(p.name))
                for old in snapshots[:-KEEP_READS]:
                    directory = transaction.enter_context(_previous_file(old, root=self.dir))
                    _unlink_file(old, directory_fd=directory)
                self.layout = self._layout_from(state, library, recover_memory=True)
                self.layout.approval_device = device_id
                self._placement_revision = self._saved_placement_revision()
                self._observed_placement = copy.deepcopy(getattr(self.source, "pins", None))
                self._page_identity = copy.deepcopy(getattr(self.source, "page_identity", {}))
                self._reported_clips = reported_clips
                self._save_draft()
                if legacy_pending is not None:
                    unresolved = self.dir / "unresolved-writes"
                    with open_private_directory(self.dir, unresolved):
                        pass
                    path = unresolved / (hashlib.sha256(legacy_pending).hexdigest() + ".json")
                    transaction.enter_context(_previous_file(path, root=self.dir))
                    try:
                        _publish_exclusive(path, legacy_pending, root=self.dir)
                    except FileExistsError:
                        if self._read_local(path) != legacy_pending:
                            raise LayoutError("unresolved write evidence changed; the pending record was retained")
                    _sync_directory(self.dir, root=self.dir)
                if clear_pending or legacy_pending is not None:
                    self._clear_pending()
            self._undo, self._redo = [], []
            self._history_baseline = None
            if previous[0] != device_id:
                self.usage = None
        except BaseException:
            (self.device_id, self.info, self.grid, self.layout, self._undo, self._redo, self.folder_columns,
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
        try:
            return self._read_local(cache)
        except FileNotFoundError:
            pass
        png = await self.source.icon_png(bundle_id)
        if png:
            _private_replace(cache, png, root=self.dir)
        return png

    async def icon_colors(self, ids: list[str] | None = None) -> dict[str, Any]:
        lay = self.require()
        ids = ids or [i for i in lay.leaf_ids() if lay.nodes[i].bundle_id]
        out: dict[str, Any] = {}
        for i in ids:
            node = lay.get(i)
            png = await self.icon(node.bundle_id) if node.bundle_id else None
            out[i] = {"name": node.name, **(self._icon_color(png) if png else {"family": "unknown"})}
        return out

    def _icon_color(self, png: bytes) -> dict:
        digest = hashlib.sha256(png).digest()
        color = self._icon_colors.pop(digest, None)
        if color is None:
            color = colors.dominant(png)
        self._icon_colors[digest] = color
        if len(self._icon_colors) > MAX_ICON_COLORS:
            self._icon_colors.popitem(last=False)
        return color

    async def colors_for_sort(self, keep_first_page: bool) -> dict[str, Any]:
        ids = organize.color_ids(self.require(), keep_first_page)
        return await self.icon_colors(ids) if ids else {}

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
        _publish_exclusive(p, data, root=self.dir)
        if pages:
            receipt_dir = self._dev_dir("observed-backups")
            _publish_exclusive(receipt_dir / p.name, hashlib.sha256(data).digest(), root=self.dir)
            _sync_directory(receipt_dir.parent, root=self.dir)
        # The device and backups directories may have been created by this call.
        _sync_directory(p.parent.parent, root=self.dir)
        _sync_directory(self.dir, root=self.dir)
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
                           "backup": backup.name, "status": "pending"}).encode(), root=self.dir)
        write_error = None
        if hasattr(self.source, "written_today"):
            self.source.written_today = None
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
            if final.archive is not None:
                wanted_today = getattr(self.source, "written_today", None)
                if wanted_today is None:
                    wanted_today = today_evidence(final.archive)
                if not same_fields(wanted_today, today_evidence(getattr(self.source, "source_archive", None))):
                    raise _ReadbackMismatch("readback changed the Today View")
            changed_folders = _changed_folder_wires(requested, observed, _instance_ids(requested, requested_memory),
                                                   ordered=False)
            if changed_folders:
                details = None
                if got_pins is not None and requested.pins:
                    with suppress(LayoutError):
                        node_pins(observed, got_pins, strict=True)
                        mismatch = _pin_mismatch(requested, observed, requested_memory, changed_folders=changed_folders)
                        if mismatch:
                            details = {"fixed_position": mismatch}
                raise _ReadbackMismatch("readback changed a folder's owner", details)
            if unreported:
                # Retain requested gaps under the no-pin-map contract. Match folders by
                # exported content without adding outgoing UUIDs to the returned state.
                if write_error is not None:
                    submitted = prepared if prepared is not None else requested
                    obligations = bound_ids if prepared is not None else backup_instances
                    proposed = memory_pins(submitted, previous_memory, obligations, typed=True)["pages"]
                    prior = memory_pins(saved, previous_memory, typed=True)["pages"]
                    if {i: p for i, p in enumerate(proposed) if p} != {i: p for i, p in enumerate(prior) if p}:
                        raise _ReadbackMismatch("write failed and the source did not report the changed fixed positions")
                got_pins = requested_memory
            try:
                node_pins(observed, got_pins, by_memory=unreported, strict=True)
            except LayoutError as e:
                raise _ReadbackMismatch(str(e)) from e
            if mismatch := _pin_mismatch(requested, observed, requested_memory):
                raise _ReadbackMismatch("readback did not retain every requested fixed position", {"fixed_position": mismatch})
            ok = (placed_structure(after, after_lib if lib_known else None, got_pins, self.grid,
                                   by_memory=unreported, leaf_dicts=final.leaf_dicts)
                  == placed_structure(target, target_lib, want_pins, self.grid, leaf_dicts=final.leaf_dicts))
            if expected_page_ids is not None:
                raw = getattr(self.source, "source_archive", None) or {}
                if raw.get("listUniqueIdentifiers") != expected_page_ids:
                    raise _ReadbackMismatch("readback changed the exported page identifiers")
            if retained_fields:
                reported = page_fields(getattr(self.source, "source_archive", None) or {}, len(after) - 1)
                for pid, wanted in retained_fields.items():
                    present = {k: v for k, v in reported.get(pid, {}).items() if k in wanted}
                    if plistlib.dumps(wanted, fmt=plistlib.FMT_BINARY) != plistlib.dumps(present, fmt=plistlib.FMT_BINARY):
                        raise _ReadbackMismatch("readback changed an exported page field")
        except Exception as e:
            readback_error = "LayoutError" if isinstance(e, _ReadbackMismatch) else type(e).__name__
            result = {"ok": False, "status": "unknown", "backup": backup.name, "recovery": "read",
                      "warnings": [verification_note], "readback_error": readback_error,
                      "error": f"Readback failed ({readback_error}); run Read before applying another change."}
            if isinstance(e, _ReadbackMismatch):
                result["error"] = f"{e}. Run Read before applying another change."
                if e.details:
                    result["mismatches"] = e.details
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
            with (_previous_file(self._gaps_path, root=self.dir), _previous_file(self.draft_path, root=self.dir),
                  _previous_file(self._placement_revision_path, root=self.dir)):
                self._placement_revision = uuid.uuid4().hex
                _private_replace(self._placement_revision_path, self._placement_revision.encode(), root=self.dir)
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
        return sorted(p.name for p in self._local_files(self._dev_dir("backups"), ".plist"))

    def _backup_bytes(self, name: str) -> bytes:
        directory = self._dev_dir("backups")
        if not name or Path(name).name != name:
            raise LayoutError("invalid backup filename")
        try:
            return self._read_local(directory / name)
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
                        and self._exists_local(record)
                        and self._read_local(record) == hashlib.sha256(data).digest()):
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
