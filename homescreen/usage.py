"""Screen Time usage from this Mac, and page-swipe estimates.

With Screen Time's "Share Across Devices" on, macOS keeps the iPhone's app-focus history in
Biome: ~/Library/Biome/streams/restricted/App.InFocus/remote/<device>/<file>. Files are SEGB
logs named by their start time (microseconds since 2001-01-01); each record holds the bundle id
of the app in focus. Reading them needs Full Disk Access for the app running this code.

We don't decode the records fully. Each app session shows up as a pair of records (start, end)
carrying its bundle id, so counting bundle-id occurrences per file and halving gives "opens".
That's an estimate, good for ranking apps, which is all the optimizer needs.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .model import Layout, Node

BIOME = Path.home() / "Library/Biome/streams/restricted/App.InFocus/remote"
MAC_EPOCH = 978307200  # 2001-01-01 in Unix time
BUNDLE_RE = re.compile(rb"[A-Za-z0-9][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]+){1,}")


def _file_time(p: Path) -> float | None:
    try:
        return int(p.name) / 1e6 + MAC_EPOCH
    except ValueError:
        return None


def _counts(files: list[Path], known: set[str]) -> Counter:
    c: Counter = Counter()
    for f in files:
        for m in BUNDLE_RE.findall(f.read_bytes()):
            s = m.decode("ascii", "ignore")
            # a protobuf field tag can follow the string with no separator; trim one char if needed
            if s in known:
                c[s] += 1
            elif s[:-1] in known:
                c[s[:-1]] += 1
    return c


def load(known: set[str], days: int = 30, root: Path = BIOME) -> dict[str, Any]:
    """App opens per bundle id over the last `days`, from the synced device(s) whose apps best
    match `known` (the apps on the phone being arranged)."""
    if not root.exists():
        return {"ok": False, "error": "No synced Screen Time data on this Mac. Turn on Screen Time > "
                "Share Across Devices on both the iPhone and the Mac (same Apple Account)."}
    since = time.time() - days * 86400
    devices = []
    try:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    except PermissionError:
        return {"ok": False, "error": "macOS blocked access to Screen Time data. Give Screen Organizer (or the terminal "
                "running the source installation) Full Disk Access in System Settings > Privacy & Security."}
    for d in dirs:
        files = [f for f in d.iterdir() if f.is_file() and (_file_time(f) or 0) >= since]
        if not files:
            continue
        c = _counts(files, known)
        if c:
            devices.append({"id": d.name, "apps": len(c), "events": sum(c.values()), "counts": c,
                            "last": max(_file_time(f) or 0 for f in files)})
    if not devices:
        return {"ok": False, "error": f"No Screen Time data from this iPhone in the last {days} days."}
    # one phone can appear under several device ids (e.g. after an iOS update); merge every
    # device that looks like the same phone: at least half the apps of the best match
    devices.sort(key=lambda d: d["apps"], reverse=True)
    best = devices[0]["apps"]
    used = [d for d in devices if d["apps"] >= best / 2]
    total: Counter = Counter()
    for d in used:
        total.update(d["counts"])
    opens = {b: max(1, round(n / 2)) for b, n in total.items()}
    return {"ok": True, "days": days, "opens": opens,
            "devices": [{k: d[k] for k in ("id", "apps", "events", "last")} for d in used]}


# ---- usage from the device itself ----------------------------------------------------------
# Without Screen Time synced to this Mac, the device's JetsamEvent reports still show which apps
# were held in memory, several times a day. An app in memory on many days is one the person
# opens, with two catches: iOS also wakes apps in the background (notifications, calls, widgets,
# location), and keeps some resident all the time. Those are discounted, not trusted.

BACKGROUND_MODES = {"voip"}  # calls wake an app and keep it in memory without it being opened


def from_memory_reports(files: list[Path], apps: dict[str, dict[str, Any]], known: set[str]) -> dict[str, Any]:
    """apps: installed apps as installation_proxy returns them (executable name, background modes)."""
    import json as _json
    exe = {a.get("CFBundleExecutable"): b for b, a in apps.items() if a.get("CFBundleExecutable")}
    days: dict[str, set[str]] = {}
    seen: Counter = Counter()  # reports an app is in
    front: Counter = Counter()
    report_days: set[str] = set()
    for f in files:
        try:
            body = _json.loads(f.read_text(errors="ignore").split("\n", 1)[1])
        except (ValueError, IndexError):
            continue
        day = str(body.get("date", ""))[:10] or f.name
        report_days.add(day)
        for p in body.get("processes", []):
            b = exe.get(p.get("name"))
            states = p.get("states") or []
            if not b or b not in known or "daemon" in states:
                continue
            days.setdefault(b, set()).add(day)
            seen[b] += 1
            if "frontmost" in states:
                front[b] += 1
    if not report_days:
        return {"ok": False, "error": "This device has no memory reports yet (iOS writes them now and then). "
                "Try again after a few days of use, or use Screen Time."}
    n = len(report_days)
    opens: dict[str, int] = {}
    background: list[str] = []
    for b, d in days.items():
        score = len(d) * 10 + seen[b] + front[b] * 5
        modes = set(apps.get(b, {}).get("UIBackgroundModes") or [])
        if n >= 7 and len(d) >= 0.9 * n:          # in memory on (nearly) every day of a week+: resident
            score //= 4
            background.append(b)
        elif modes & BACKGROUND_MODES:            # may have been woken, not opened
            score //= 2
            background.append(b)
        opens[b] = max(1, score)
    return {"ok": True, "source": "device", "days": n, "reports": len(files), "opens": opens,
            "background": sorted(background)}


# ---- swipes -----------------------------------------------------------------------------

def _app_positions(lay: Layout) -> dict[str, tuple[int, Node]]:
    """Swipes to reach each app from page 1: page N costs N-1, a folder's page K adds K-1,
    the dock is free, an App Library-only app costs one swipe past the last page."""
    nearest: dict[str, tuple[int, Node]] = {}

    def record(node: Node, cost: int):
        if node.kind == "folder":
            for index, page in enumerate(node.children):
                for child in page:
                    record(child, cost + index)
        elif node.bundle_id and (node.bundle_id not in nearest or cost < nearest[node.bundle_id][0]):
            nearest[node.bundle_id] = cost, node

    for node in lay.dock:
        record(node, 0)
    for index, page in enumerate(lay.pages):
        for node in page:
            record(node, index)
    for node in lay.library or []:
        record(node, len(lay.pages))
    return nearest


def _leaves(n: Node) -> list[Node]:
    return [c for p in n.children for c in p] if n.kind == "folder" else [n]


def swipes(lay: Layout, opens: dict[str, int]) -> int:
    cost = _app_positions(lay)
    return sum(opens.get(b, 0) * c for b, (c, _) in cost.items())


def top_apps(lay: Layout, opens: dict[str, int], n: int = 25, *, locations: bool = True) -> list[dict[str, Any]]:
    nearest = _app_positions(lay)
    pos = lay.positions() if locations else {}
    ranked = sorted(((b, c) for b, c in opens.items() if b in nearest), key=lambda kv: -kv[1])[:n]
    rows = []
    for bundle, count in ranked:
        node = nearest[bundle][1]
        row = {"id": bundle, "name": node.name, "opens": count, "where": pos.get(node.id, "")}
        if not locations:
            row["placement_id"] = node.id
        rows.append(row)
    return rows


# ---- optimizer --------------------------------------------------------------------------

def optimize(lay: Layout, opens: dict[str, int], keep_first_page: bool = False) -> dict[str, Any]:
    """Put the most-opened apps and folders on the earliest pages (row by row) and the most
    opened apps on each folder's first page. Widgets stay on their pages; the dock and the
    App Library aren't touched. Returns the swipe estimate before and after."""
    before = swipes(lay, opens)
    score = lambda n: sum(opens.get(c.bundle_id or c.id, 0) for c in _leaves(n))
    for n in list(lay.nodes.values()):  # folder contents first, so folder scores stay the same
        if n.kind == "folder":
            items = sorted(_leaves(n), key=lambda c: -opens.get(c.bundle_id or c.id, 0))
            lay.refold(n, items)
    start = 1 if keep_first_page and lay.pages else 0
    lay.unpin_pages(start)
    pages = lay.pages[start:]
    widgets = [[x for x in p if x.kind == "widget"] for p in pages]
    movable = sorted((x for p in pages for x in p if x.kind != "widget"), key=lambda n: -score(n))
    out: list[list[Node]] = []
    i = 0
    for k in range(max(len(pages), 1)):
        page = list(widgets[k]) if k < len(widgets) else []
        used = sum(x.slots for x in page)
        while i < len(movable) and used + movable[i].slots <= lay.page_slots:
            page.append(movable[i])
            used += movable[i].slots
            i += 1
        out.append(page)
    while i < len(movable):  # anything left gets new pages
        page, used = [], 0
        while i < len(movable) and used + movable[i].slots <= lay.page_slots:
            page.append(movable[i])
            used += movable[i].slots
            i += 1
        out.append(page)
    lay.repack_pages(lay.pages[:start] + [p for p in out if p])
    return {"swipes_before": before, "swipes_after": swipes(lay, opens)}
