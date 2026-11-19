"""One-click arrangements. Each works on the draft only and keeps the item set intact.

"Loose" items are the top-level apps and web clips on pages (not in folders, not the dock).
Widgets and folders keep their relative order and go first unless a sort says otherwise.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .model import Layout, LayoutError, Node
from .private_store import ensure_private_directory, open_private_directory, open_private_file

COLOR_ORDER = ["red", "orange", "yellow", "green", "teal", "blue", "purple", "pink",
               "white", "gray", "black", "unknown"]


def _pages_from(lay: Layout, start: int) -> list[Node]:
    return [n for p in lay.pages[start:] for n in p]


def pack(lay: Layout, items: list[Node], start_page: int = 0) -> None:
    """Lay items out row by row from page start_page+1 on, starting a new page when one is full.
    Gaps on those pages are dropped: a sort re-flows them."""
    lay.unpin_pages(start_page)
    kept = lay.pages[:start_page]
    pages: list[list[Node]] = [[]]
    uniform = True
    for n in items:
        single = lay.dims(n) == (1, 1)
        if pages[-1] and ((len(pages[-1]) >= lay.grid.slots) if uniform and single
                         else lay.place([*pages[-1], n])[1]):
            pages.append([])
            uniform = True
        pages[-1].append(n)
        uniform = uniform and single
    lay.repack_pages(kept + [p for p in pages if p])


def _split(lay: Layout, keep_first_page: bool) -> tuple[int, list[Node], list[Node]]:
    start = 1 if keep_first_page and lay.pages else 0
    nodes = _pages_from(lay, start)
    fixed = [n for n in nodes if n.kind in ("widget", "folder")]
    loose = [n for n in nodes if n.kind in ("app", "webclip", "other")]
    return start, fixed, loose


def color_ids(lay: Layout, keep_first_page: bool) -> list[str]:
    _, _, loose = _split(lay, keep_first_page)
    return [node.id for node in loose if node.bundle_id]


def by_color(lay: Layout, colors: dict[str, dict[str, Any]], keep_first_page: bool = True) -> None:
    def key(n: Node) -> tuple[int, int]:
        c = colors.get(n.id, {})
        fam = c.get("family", "unknown")
        return (COLOR_ORDER.index(fam) if fam in COLOR_ORDER else len(COLOR_ORDER), c.get("hue", 0))
    start, fixed, loose = _split(lay, keep_first_page)
    pack(lay, fixed + sorted(loose, key=key), start)


def alphabetical(lay: Layout, keep_first_page: bool = True, folders_first: bool = True) -> None:
    start, fixed, loose = _split(lay, keep_first_page)
    widgets = [n for n in fixed if n.kind == "widget"]
    folders = sorted((n for n in fixed if n.kind == "folder"), key=lambda n: n.name.lower())
    apps = sorted(loose, key=lambda n: n.name.lower())
    pack(lay, widgets + (folders + apps if folders_first else sorted(folders + apps, key=lambda n: n.name.lower())),
         start)


def sort_folder_contents(lay: Layout) -> None:
    """A-Z inside every folder, refilled in full folder pages."""
    placed = {x.id for p in lay.pages for x in p} | {x.id for x in lay.dock}
    for n in list(lay.nodes.values()):
        if n.kind == "folder" and n.id in placed:
            items = sorted((c for p in n.children for c in p), key=lambda c: c.name.lower())
            lay.refold(n, items)


def into_folders(lay: Layout, groups: dict[str, list[str]], keep_first_page: bool = True,
                 min_size: int = 2) -> dict[str, Any]:
    """groups: folder name -> app ids. Apps join an existing top-level folder of that name, or a
    new one when at least min_size apps share it. Returns what happened."""
    start = 1 if keep_first_page and lay.pages else 0
    allowed = {n.id for p in lay.pages[start:] for n in p if n.kind in ("app", "webclip")}
    existing = {n.name.lower(): n for p in lay.pages for n in p if n.kind == "folder"}
    filed: dict[str, list[str]] = {}
    for name, ids in groups.items():
        ids = [i for i in dict.fromkeys(ids) if i in allowed]
        folder = existing.get(name.lower())
        if folder is None and len(ids) < min_size:
            continue
        if not ids:
            continue
        if folder is None:
            first = ids[0]
            c, i = lay.locate(first)
            target = f"page:{lay.pages.index(c) + 1}"
            fid = lay.create_folder(name, ids, target, i)
            folder = lay.get(fid)
            existing[name.lower()] = folder
        else:
            lay.move_many(ids, folder.id)
        filed.setdefault(folder.name, []).extend(lay.nodes[i].name for i in ids)
        allowed.difference_update(ids)
    lay.remove_empty_pages()
    if not lay.pages:
        lay.insert_page(1)
    return {"filed": filed, "count": sum(len(v) for v in filed.values())}


# ---- App Store categories (opt-in network lookup) ------------------------------------

LOOKUP = "https://itunes.apple.com/lookup?"


def fetch_categories(bundle_ids: list[str], cache: Path, country: str = "us") -> dict[str, str]:
    """Primary App Store genre per bundle id, via Apple's public lookup API. Only bundle ids are
    sent. Results (and misses, as "") are cached so each id is asked about once."""
    from .session import _private_replace
    bundle_ids = list(dict.fromkeys(bundle_ids))
    ensure_private_directory(cache.parent)
    with open_private_directory(cache.parent) as directory:
        try:
            fd = open_private_file(cache.parent, cache, directory_fd=directory)
        except FileNotFoundError:
            known: dict[str, str] = {}
        else:
            with os.fdopen(fd) as stream:
                known = json.load(stream)
        todo = [b for b in bundle_ids if b not in known and not b.startswith("com.apple.")]
        for i in range(0, len(todo), 100):
            chunk = todo[i:i + 100]
            url = LOOKUP + urllib.parse.urlencode({"bundleId": ",".join(chunk), "country": country, "entity": "software"})
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.load(r)
            found = {res.get("bundleId"): res.get("primaryGenreName", "") for res in data.get("results", [])}
            for b in chunk:
                known[b] = found.get(b, "")
        for b in bundle_ids:
            if b.startswith("com.apple.") and b not in known:
                known[b] = "Apple"
        _private_replace(cache, json.dumps(known, indent=1, sort_keys=True).encode(), directory_fd=directory)
    return {b: known.get(b, "") for b in bundle_ids}


def category_groups(lay: Layout, categories: dict[str, str], keep_first_page: bool = True) -> dict[str, list[str]]:
    start = 1 if keep_first_page and lay.pages else 0
    groups: dict[str, list[str]] = {}
    for p in lay.pages[start:]:
        for n in p:
            if n.kind == "app" and categories.get(n.bundle_id):
                groups.setdefault(categories[n.bundle_id], []).append(n.id)
    return groups


def check(lay: Layout) -> None:
    problems = lay.validate()
    if problems:
        raise LayoutError("; ".join(problems))
