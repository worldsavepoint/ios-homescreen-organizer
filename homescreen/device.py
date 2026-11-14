"""Where layouts come from: a USB iPhone (pymobiledevice3), an iOS Simulator, or a fixture file.

Every source reads and writes (state, library):
- state: [dock, page1, page2, ...] in SpringBoard's "formatVersion 2" shape (apps are dicts).
- library: App Library-only apps (iOS calls them "ignored"), or None when the source's
  interface doesn't expose them.

iOS also has a dict-shaped layout ({buttonBar, iconLists, ignored, ...}, apps as bundle id
strings). The Simulator stores that on disk; disk_to_model / model_to_disk convert it.

Over USB, getIconState only returns the list, and a list written back with setIconState has
no App Library: SpringBoard turns it into {buttonBar, iconLists} and puts every installed app
it doesn't mention onto a page. SpringBoard's import also accepts the dict ("root archive")
form, which carries the ignored list, so PhoneSource writes that (see to_root_archive).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import platform
import plistlib
import re
import subprocess
import uuid
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any, Protocol

from .model import Grid, LayoutError, is_folder, is_widget, widget_wire_fields

Layout = tuple[list[Any], list[Any] | None]

DISK_MARK = "__bundleIdOnly__"  # an app that was a bare bundle id string in the dict format
NAME_MARK = "__nameFromDefault__"  # folder had no displayName of its own
DEVICE_OPERATION_TIMEOUT = 30.0
SUBPROCESS_TIMEOUT = 10.0


def require_writable() -> None:
    if os.environ.get("HOMESCREEN_READ_ONLY") == "1":
        raise PermissionError("read-only mode: device writes are disabled")


def _bounded(operation):
    @wraps(operation)
    async def call(*args, **kwargs):
        async with asyncio.timeout(DEVICE_OPERATION_TIMEOUT):
            return await operation(*args, **kwargs)
    return call


class Source(Protocol):
    name: str

    async def info(self) -> dict[str, Any]: ...
    async def read(self) -> Layout: ...
    async def write(self, state: list[Any], library: list[Any] | None,
                    pins: list[dict[str, int]] | None = None, grid: Grid | None = None) -> None: ...
    async def icon_png(self, bundle_id: str) -> bytes | None: ...


# ---- dict-format conversion ---------------------------------------------------------

def _item_in(x: Any, names: dict[str, str]) -> Any:
    if isinstance(x, str):
        return {"bundleIdentifier": x, "displayIdentifier": x, "displayName": names.get(x, x), DISK_MARK: True}
    if isinstance(x, dict) and is_folder(x):
        out = dict(x)
        if "displayName" not in x:  # iOS leaves it out while the folder keeps its default name
            out["displayName"] = x.get("defaultDisplayName") or "Folder"
            out[NAME_MARK] = True
        out["iconLists"] = [[_item_in(i, names) for i in page] for page in x["iconLists"]]
        return out
    return x


def _fix_list_ids(d: dict[str, Any], n: int) -> None:
    """listUniqueIdentifiers has one UUID per page; keep it in step with the page count."""
    if "listUniqueIdentifiers" in d:
        ids = list(d["listUniqueIdentifiers"])
        d["listUniqueIdentifiers"] = (ids + [str(uuid.uuid4()).upper() for _ in range(n)])[:n]


def _item_out(x: Any) -> Any:
    if isinstance(x, dict) and x.get(DISK_MARK):
        return x["bundleIdentifier"]
    if isinstance(x, dict) and is_folder(x):
        out = {k: v for k, v in x.items() if k != NAME_MARK}
        if x.get(NAME_MARK) and out.get("displayName") == out.get("defaultDisplayName"):
            del out["displayName"]
        out["iconLists"] = [[_item_out(i) for i in page] for page in x["iconLists"]]
        _fix_list_ids(out, len(out["iconLists"]))
        return out
    return x


def _leaf_id(x: dict[str, Any]) -> str:
    return x.get("displayIdentifier") or x.get("bundleIdentifier")


def _modern(x: Any) -> Any:
    """A list-format item as SpringBoard stores it on disk: apps and web clips become their
    identifier string; widgets lose the empty iconLists phones attach; folders recurse."""
    if not isinstance(x, dict):
        return x
    if x.get(DISK_MARK):
        return x["bundleIdentifier"]
    if is_widget(x):
        out = {k: v for k, v in widget_wire_fields(x).items() if k != "iconLists"}
        # Over USB a single widget's uniqueIdentifier isn't exported. Its configuration (which
        # script, which person) hangs off that id, and userSelectedElementIdentifier holds it for
        # configured widgets. Without it SpringBoard saves the widget with no id and the
        # configuration is lost, so put it back.
        return out
    if is_folder(x):
        out = {k: v for k, v in x.items() if k not in (NAME_MARK, "iconLists")}
        out["iconLists"] = [[_modern(i) for i in page] for page in x["iconLists"] if page]
        return out
    return _leaf_id(x)


def to_root_archive(state: list[Any], library: list[Any],
                    pins: list[dict[str, int]] | None = None, grid: Grid | None = None) -> dict[str, Any]:
    """The dict form SpringBoard's importIconState accepts, App Library included.
    pins: per page, {identifier: grid cell} for items placed with gaps around them. The Today
    View keeps its value on the phone; page ids and gaps are replaced (iOS doesn't report gaps
    over USB, so the phone's own gaps are lost either way).
    grid supplies the loaded page dimensions; omitted metrics use the iPhone grid."""
    out = {
        "buttonBar": [_modern(x) for x in state[0]],
        "iconLists": [[_modern(x) for x in page] for page in state[1:]],
        "ignored": [_modern(x) for x in library],
    }
    if pins and any(pins):
        ids = [str(uuid.uuid4()).upper() for _ in out["iconLists"]]
        out["listUniqueIdentifiers"] = ids
        out["listMetadata"] = _list_metadata(ids, pins, grid)
    return out


def _list_metadata(ids: list[str], pins: list[dict[str, int]], grid: Grid | None = None) -> dict[str, Any]:
    grid = grid or Grid()
    return {ids[i]: {"fixedLocations": dict(p), "fixedLocationsGridColumns": grid.cols,
                     "fixedLocationsGridRows": grid.rows}
            for i, p in enumerate(pins) if p and i < len(ids)}


class ObservedPins(dict):
    """Dict-compatible pin lookup retaining the archive evidence used to verify it.

    Cells keep their exported types. Page IDs and metadata survive snapshots so a
    flat lookup never becomes proof of page ownership or a coordinate frame.
    """

    def __init__(self, raw: dict[str, Any]):
        self.archive = copy.deepcopy({k: raw[k] for k in ("listUniqueIdentifiers", "listMetadata") if k in raw})
        super().__init__()
        ids = self.archive.get("listUniqueIdentifiers", [])
        meta = self.archive.get("listMetadata", {})
        if isinstance(ids, list) and isinstance(meta, dict):
            for pid in ids:
                page = meta.get(pid, {}) if isinstance(pid, str) else {}
                pins = page.get("fixedLocations", {}) if isinstance(page, dict) else {}
                if isinstance(pins, dict):
                    self.update(pins)

    def pages(self, count: int) -> list[dict[str, int]]:
        """Page-local evidence after validate(); the dict view is for legacy callers."""
        ids = self.archive.get("listUniqueIdentifiers", [])
        meta = self.archive.get("listMetadata", {})
        if not meta:
            return [{} for _ in range(count)]
        return [meta.get(pid, {}).get("fixedLocations", {}) for pid in ids]

    def validate(self, page_keys: list[set[str]], grid: Grid) -> None:
        ids = self.archive.get("listUniqueIdentifiers", [])
        meta = self.archive.get("listMetadata", {})
        if not isinstance(meta, dict):
            raise LayoutError("invalid exported pin metadata")
        if not meta:
            return
        if (not isinstance(ids, list) or len(ids) != len(page_keys)
                or any(not isinstance(pid, str) or not pid for pid in ids) or len(set(ids)) != len(ids)):
            raise LayoutError("exported pin metadata has invalid page identifiers")
        for pid, page in meta.items():
            if pid not in ids or not isinstance(page, dict):
                raise LayoutError("exported pin metadata does not identify a layout page")
            for name, expected in (("fixedLocationsGridColumns", grid.cols), ("fixedLocationsGridRows", grid.rows)):
                if name in page and (type(page[name]) is not int or page[name] != expected):
                    raise LayoutError("exported pin metadata dimensions differ from the loaded grid")
            pins = page.get("fixedLocations", {})
            if not isinstance(pins, dict):
                raise LayoutError("invalid exported pin locations")
            for key, cell in pins.items():
                if key not in page_keys[ids.index(pid)]:
                    raise LayoutError("exported pin does not identify an item on its page")
                if type(cell) is not int or not 0 <= cell < grid.slots:
                    raise LayoutError("invalid exported pin cell on the loaded grid")


def disk_pins(raw: dict[str, Any]) -> ObservedPins:
    """Read actual page-scoped placement metadata, retaining raw values and dimensions."""
    return ObservedPins(raw)


def is_dict_format(raw: Any) -> bool:
    return isinstance(raw, dict) and "iconLists" in raw


def disk_to_model(raw: dict[str, Any], names: dict[str, str] | None = None) -> Layout:
    names = names or {}
    state = [[_item_in(x, names) for x in raw.get("buttonBar", [])]]
    state += [[_item_in(x, names) for x in page] for page in raw.get("iconLists", [])]
    library = [_item_in(x, names) for x in raw.get("ignored", [])]
    return state, library


def model_to_disk(state: list[Any], library: list[Any] | None, base: dict[str, Any],
                  pins: list[dict[str, int]] | None = None, grid: Grid | None = None) -> dict[str, Any]:
    """Rebuild the dict format; keys we don't touch (today view, ids) come from base. pins
    (per page) replace the gaps when given. grid supplies their page dimensions, with
    iPhone defaults when omitted."""
    out = dict(base)
    out["buttonBar"] = [_item_out(x) for x in state[0]]
    out["iconLists"] = [[_item_out(x) for x in page] for page in state[1:]]
    _fix_list_ids(out, len(out["iconLists"]))
    if library is not None:
        out["ignored"] = [_item_out(x) for x in library]
    if pins is not None:
        if "listUniqueIdentifiers" not in out:
            out["listUniqueIdentifiers"] = [str(uuid.uuid4()).upper() for _ in out["iconLists"]]
        meta = _list_metadata(out["listUniqueIdentifiers"], pins, grid)
        if meta:
            out["listMetadata"] = meta
        else:
            out.pop("listMetadata", None)
    return out


# ---- USB iPhone -----------------------------------------------------------------------

# Installed system apps that iOS never puts on a Home Screen page, though nothing in their
# metadata says so. Seen on iOS 27.0.1: a list-format write left all of these alone.
NEVER_ON_HOME_SCREEN = {
    "com.apple.FTMInternal", "com.apple.Posters.ImagePlaygroundPosterApp", "com.apple.PreviewShell",
    "com.apple.TVRemoteApp", "com.apple.sidecar", "com.apple.webapp",
    "com.apple.appleseed.FeedbackAssistant",  # iOS 26.6.2 keeps it in the App Library
}


class PhoneSource:
    """A USB-connected iPhone. Pairing goes through Apple's 'Trust This Computer' prompt.

    The App Library isn't readable over USB, so read() works it out: installed apps
    (offloaded ones included) that aren't in the layout, minus hidden and never-shown apps.
    write() sends the dict form with that list as "ignored", so those apps stay put.
    HOMESCREEN_WRITE=list sends the bare list instead (iOS then moves every App Library app
    onto a page). HOMESCREEN_FORMAT picks the formatVersion for getIconState (default "2").
    """

    def __init__(self, udid: str | None = None, format_version: str | None = None):
        self.udid = udid
        self._bound_udid = udid
        self.name = "phone"
        self.format_version = format_version or os.environ.get("HOMESCREEN_FORMAT", "2")
        self.write_mode = os.environ.get("HOMESCREEN_WRITE", "full")
        self._lockdown = None
        self._last_dict: dict[str, Any] | None = None

    def _check_identity(self, values: Any) -> None:
        actual = values.get("UniqueDeviceID") if isinstance(values, dict) else None
        if not isinstance(actual, str) or not actual:
            raise ValueError("connected device identity is unavailable")
        if self._bound_udid is not None and actual != self._bound_udid:
            raise ValueError("connected device identity does not match the bound device")
        self._bound_udid = self.udid = actual

    async def _ld(self):
        if self._lockdown is None:
            from pymobiledevice3.lockdown import create_using_usbmux
            connection = await create_using_usbmux(serial=self._bound_udid)
            try:
                self._check_identity(connection.all_values)
            except ValueError:
                await connection.close()
                raise
            self._lockdown = connection
        return self._lockdown

    @_bounded
    async def _call(self, fn):
        """Run fn(springboard) on a fresh service connection; drop the lockdown on failure
        so an unplugged/replugged phone reconnects next time."""
        from pymobiledevice3.services.springboard import SpringBoardServicesService
        try:
            async with SpringBoardServicesService(lockdown=await self._ld()) as sb:
                return await fn(sb)
        except Exception:
            self._lockdown = None
            raise

    @_bounded
    async def info(self) -> dict[str, Any]:
        connection = await self._ld()
        try:
            vals = await connection.get_value()
            self._check_identity(vals)
        except BaseException:
            self._lockdown = None
            raise
        out = {k: vals.get(k) for k in ("DeviceName", "ProductType", "ProductVersion", "UniqueDeviceID")}
        # A service failure invalidates the connection. Do not return cached identity
        # as a successful prewrite check after _call has discarded that connection.
        out["icon_metrics"] = await self._call(lambda sb: sb.get_homescreen_icon_metrics())
        if self._lockdown is not connection:
            raise ConnectionError("device connection changed during identity verification")
        return out

    async def read_raw(self, format_version: str | None) -> Any:
        return await self._call(lambda sb: sb.get_icon_state(format_version=format_version))

    @_bounded
    async def installed_apps(self) -> dict[str, dict[str, Any]]:
        from pymobiledevice3.services.installation_proxy import InstallationProxyService
        async with InstallationProxyService(lockdown=await self._ld()) as ip:
            return await ip.get_apps(application_type="Any", calculate_sizes=False, show_placeholders=True)

    @_bounded
    async def memory_reports(self, out: Path) -> list[Path]:
        """Copy the device's JetsamEvent reports (written when memory runs low; each lists the
        apps held in memory) into out. Read-only: nothing is erased on the device."""
        from pymobiledevice3.services.crash_reports import CrashReportsManager
        out.mkdir(parents=True, exist_ok=True)
        async with CrashReportsManager(await self._ld()) as cr:
            await cr.pull(str(out), match=r"JetsamEvent", progress_bar=False)
        return sorted(out.rglob("JetsamEvent*.ips"))

    @_bounded
    async def read(self) -> Layout:
        raw = await self.read_raw(self.format_version)
        self.pins = None
        self._last_dict = None
        if is_dict_format(raw):
            self._last_dict = raw
            if "listMetadata" in raw:
                self.pins = disk_pins(raw)
            return disk_to_model(raw)
        if self.write_mode == "list":
            return raw, None
        return raw, library_from_installed(raw, await self.installed_apps())

    pins: dict[str, int] | None = None  # ordinary USB list reads do not report gaps

    @_bounded
    async def write(self, state: list[Any], library: list[Any] | None,
                    pins: list[dict[str, int]] | None = None, grid: Grid | None = None) -> None:
        require_writable()
        if self._last_dict is not None:
            payload: Any = model_to_disk(state, library, self._last_dict, pins, grid)
        elif self.write_mode == "list":
            if library:
                raise ValueError("HOMESCREEN_WRITE=list can't keep apps in the App Library")
            payload = state
        else:
            # Safety net: any installed app in neither the layout nor the library (an old
            # backup, an app installed meanwhile) goes to the library, never onto a page.
            library = list(library or [])
            listed = {_leaf_id(x) for x in library if isinstance(x, dict)}
            library += [x for x in library_from_installed(state, await self.installed_apps())
                        if x["bundleIdentifier"] not in listed]
            payload = to_root_archive(state, library, pins, grid)
        await self._call(lambda sb: sb.set_icon_state(payload))

    @_bounded
    async def icon_png(self, bundle_id: str) -> bytes | None:
        return await self._call(lambda sb: sb.get_icon_pngdata(bundle_id))


def library_from_installed(state: list[Any], apps: dict[str, dict[str, Any]]) -> list[Any]:
    placed: set[str] = set()

    def walk(items: list[Any]) -> None:
        for x in items:
            if isinstance(x, dict) and is_folder(x):
                for page in x["iconLists"]:
                    walk(page)
            elif isinstance(x, dict) and not is_widget(x):
                placed.update(filter(None, (x.get("bundleIdentifier"), x.get("displayIdentifier"))))

    for page in state:
        walk(page)
    out = []
    for b, a in sorted(apps.items(), key=lambda kv: (kv[1].get("CFBundleDisplayName") or kv[0]).lower()):
        if b in placed or b in NEVER_ON_HOME_SCREEN or "hidden" in (a.get("SBAppTags") or []):
            continue
        name = a.get("CFBundleDisplayName") or a.get("CFBundleName") or b
        out.append({"bundleIdentifier": b, "displayIdentifier": b, "displayName": name})
    return out


# ---- iOS Simulator --------------------------------------------------------------------

def _run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT).stdout


@lru_cache(maxsize=1)
def _simulator_icon_reader():
    import tempfile

    directory = tempfile.TemporaryDirectory(prefix="homescreen-sim-icons-")
    executable = Path(directory.name) / "read-icon"
    sdk = _run("xcrun", "--sdk", "iphonesimulator", "--show-sdk-path").strip()
    subprocess.run(["xcrun", "clang", "-target", f"{platform.machine()}-apple-ios16.0-simulator",
                    "-isysroot", sdk, "-fobjc-arc", "-framework", "UIKit",
                    str(Path(__file__).with_name("simulator_icon.m")), "-o", str(executable)],
                   check=True, capture_output=True, timeout=SUBPROCESS_TIMEOUT)
    subprocess.run(["codesign", "-s", "-", "-f", str(executable)], check=True,
                   capture_output=True, timeout=SUBPROCESS_TIMEOUT)
    return directory, executable


def _simulator_icon_png(udid: str, bundle_id: str) -> bytes | None:
    try:
        directory, executable = _simulator_icon_reader()
        result = subprocess.run(["xcrun", "simctl", "spawn", udid, str(executable), bundle_id],
                                capture_output=True, check=True, timeout=SUBPROCESS_TIMEOUT)
        return result.stdout if result.stdout.startswith(b"\x89PNG\r\n\x1a\n") else None
    except (OSError, subprocess.SubprocessError):
        return None


class SimSource:
    """A booted iOS Simulator. Reads and writes SpringBoard's IconState.plist, then restarts
    SpringBoard so it loads the new layout (it re-saves the file if it changes anything)."""

    def __init__(self, udid: str):
        self.udid = udid
        self.name = f"sim:{udid[:8]}"
        self.dir = Path.home() / f"Library/Developer/CoreSimulator/Devices/{udid}/data/Library/SpringBoard"
        self._apps: dict[str, dict[str, Any]] | None = None

    @property
    def path(self) -> Path:
        return self.dir / "IconState.plist"

    def apps(self) -> dict[str, dict[str, Any]]:
        if self._apps is None:
            text = _run("xcrun", "simctl", "listapps", self.udid)
            conv = subprocess.run(["plutil", "-convert", "json", "-o", "-", "-"], input=text.encode(),
                                  check=True, capture_output=True, timeout=SUBPROCESS_TIMEOUT)
            self._apps = json.loads(conv.stdout)
        return self._apps

    async def info(self) -> dict[str, Any]:
        devs = json.loads(_run("xcrun", "simctl", "list", "devices", "-j"))["devices"]
        for runtime, items in devs.items():
            for d in items:
                if d["udid"] == self.udid:
                    return {"DeviceName": d["name"], "ProductVersion": runtime.rsplit(".", 1)[-1].replace("iOS-", "").replace("-", "."),
                            "State": d["state"], "UniqueDeviceID": self.udid}
        raise ValueError(f"no simulator {self.udid}")

    async def read(self) -> Layout:
        raw = plistlib.loads(self.path.read_bytes())
        names = {b: a.get("CFBundleDisplayName") or a.get("CFBundleName") or b for b, a in self.apps().items()}
        self.pins = disk_pins(raw)
        return disk_to_model(raw, names)

    def _springboard_pid(self) -> str | None:
        for line in _run("xcrun", "simctl", "spawn", self.udid, "launchctl", "list").splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[2] == "com.apple.SpringBoard" and parts[0] != "-":
                return parts[0]
        return None

    @_bounded
    async def write(self, state: list[Any], library: list[Any] | None,
                    pins: list[dict[str, int]] | None = None, grid: Grid | None = None) -> None:
        require_writable()
        base = plistlib.loads(self.path.read_bytes())
        out = model_to_disk(state, library, base, pins if pins is not None else [], grid)
        before = self._springboard_pid()
        self.path.write_bytes(plistlib.dumps(out, fmt=plistlib.FMT_BINARY))
        # SIGKILL so SpringBoard can't save its old in-memory layout over ours on the way out
        subprocess.run(["xcrun", "simctl", "spawn", self.udid, "launchctl", "kill", "SIGKILL",
                        "system/com.apple.SpringBoard"], capture_output=True, timeout=SUBPROCESS_TIMEOUT)
        for _ in range(120):
            await asyncio.sleep(0.5)
            pid = self._springboard_pid()
            if pid and pid != before:
                break
        # SpringBoard is up long before it draws the Home Screen; wait until the screen
        # isn't black, then give it a moment to re-save any corrections
        for _ in range(90):
            if self._screen_lit():
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)

    def _screen_lit(self) -> bool:
        import tempfile
        from PIL import Image, ImageStat
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            r = subprocess.run(["xcrun", "simctl", "io", self.udid, "screenshot", f.name],
                               capture_output=True, timeout=SUBPROCESS_TIMEOUT)
            if r.returncode:
                return False
            return ImageStat.Stat(Image.open(f.name).convert("L")).mean[0] > 40

    async def icon_png(self, bundle_id: str) -> bytes | None:
        app = self.apps().get(bundle_id)
        if not app or "Bundle" not in app:
            return None
        from urllib.parse import unquote, urlparse
        bundle = Path(unquote(urlparse(app["Bundle"]).path))
        # Older simctl versions omit the icon declarations supplied by the bundle itself.
        try:
            info = plistlib.loads((bundle / "Info.plist").read_bytes())
        except (OSError, ValueError):
            info = {}
        primary = info.get("CFBundleIcons", {}).get("CFBundlePrimaryIcon", {})
        names = (primary.get("CFBundleIconFiles") or info.get("CFBundleIconFiles")
                 or app.get("CFBundleIcons", {}).get("CFBundlePrimaryIcon", {}).get("CFBundleIconFiles")
                 or ["AppIcon60x60"])
        if info.get("SBIconClass"):
            png = await asyncio.to_thread(_simulator_icon_png, self.udid, bundle_id)
            if png:
                return png
        for name in names:
            if not isinstance(name, str):
                continue
            has_extension = name.lower().endswith(".png")
            stem = name[:-4] if has_extension else name
            for filename in (f"{stem}@3x.png", f"{stem}@2x.png", name if has_extension else f"{name}.png"):
                p = bundle / filename
                if p.is_file() and p.resolve().is_relative_to(bundle.resolve()):
                    return p.read_bytes()
        # many system apps don't list their icon files; take the biggest phone-size one
        found = sorted((p for p in bundle.glob("*60x60@*.png")
                        if "~ipad" not in p.name and p.is_file()
                        and p.resolve().is_relative_to(bundle.resolve())), key=lambda p: p.name)
        if found:
            return found[-1].read_bytes()
        if primary.get("CFBundleIconName") and (bundle / "Assets.car").exists():
            return await asyncio.to_thread(_simulator_icon_png, self.udid, bundle_id)
        return None


# ---- fixture file ---------------------------------------------------------------------

class FileSource:
    """A saved layout on disk standing in for a device. Writes go back to the file.

    The file holds either the list format (no App Library) or the dict format.
    Optional sidecar <file>.icons/<bundle_id>.png supplies icons.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.name = f"file:{self.path.name}"

    async def info(self) -> dict[str, Any]:
        return {"DeviceName": f"fixture {self.path.name}", "UniqueDeviceID": f"fixture-{self.path.stem}"}

    async def read(self) -> Layout:
        raw = plistlib.loads(self.path.read_bytes())
        self.pins = disk_pins(raw) if is_dict_format(raw) else None
        return disk_to_model(raw) if is_dict_format(raw) else (raw, None)

    async def write(self, state: list[Any], library: list[Any] | None,
                    pins: list[dict[str, int]] | None = None, grid: Grid | None = None) -> None:
        require_writable()
        raw = plistlib.loads(self.path.read_bytes())
        out = model_to_disk(state, library, raw, pins if pins is not None else [], grid) if is_dict_format(raw) else state
        self.path.write_bytes(plistlib.dumps(out, fmt=plistlib.FMT_BINARY))

    async def icon_png(self, bundle_id: str) -> bytes | None:
        if not isinstance(bundle_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", bundle_id):
            return None
        sidecar = self.path.with_suffix(".icons")
        icons = sidecar.parent.resolve() / sidecar.name
        p = (icons / f"{bundle_id}.png").resolve()
        if not p.is_relative_to(icons):
            return None
        return p.read_bytes() if p.exists() else None
