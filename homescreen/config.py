"""Shared setup for the CLI, the MCP server and the app: where data lives, which device to use."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .device import FileSource, PhoneSource, SimSource, Source
from .session import Session
from .private_store import ensure_private_directory

DATA = Path(os.environ.get("HOMESCREEN_DATA") or Path.home() / "Library/Application Support/homescreen-mcp")


def booted_sim() -> str:
    devs = json.loads(subprocess.run(["xcrun", "simctl", "list", "devices", "booted", "-j"],
                                     capture_output=True, text=True, check=True).stdout)["devices"]
    booted = [d["udid"] for items in devs.values() for d in items if d["state"] == "Booted"]
    if len(booted) != 1:
        raise RuntimeError(f"HOMESCREEN_SIM=booted needs exactly one booted simulator, found {len(booted)}")
    return booted[0]


def make_source() -> Source:
    """HOMESCREEN_FIXTURE (a saved layout) > HOMESCREEN_SIM (booted|udid) > USB iPhone."""
    fixture, sim = os.environ.get("HOMESCREEN_FIXTURE"), os.environ.get("HOMESCREEN_SIM")
    if fixture:
        return FileSource(fixture)
    if sim:
        return SimSource(booted_sim() if sim == "booted" else sim)
    return PhoneSource(os.environ.get("HOMESCREEN_UDID"))


_session: Session | None = None

# The app (and `homescreen ui`) can switch between connected devices, each with its own draft.
# The CLI and MCP server keep one session for the device the environment picks.
_switching = False
_sessions: dict[str, Session] = {}
_current: str | None = None
LAST_DEVICE = DATA / "last-device.txt"


def enable_device_switching() -> None:
    global _switching, _current
    if os.environ.get("HOMESCREEN_FIXTURE") or os.environ.get("HOMESCREEN_SIM") or os.environ.get("HOMESCREEN_UDID"):
        return  # a pinned source: nothing to switch between
    ensure_private_directory(DATA)
    _switching = True
    _adopt_shared_draft()
    if LAST_DEVICE.exists():
        _current = LAST_DEVICE.read_text().strip() or None


def _adopt_shared_draft() -> None:
    """Carry the single shared draft (CLI / older versions) over as its device's own draft."""
    import plistlib
    import shutil
    shared = DATA / "draft.plist"
    try:
        dev = plistlib.loads(shared.read_bytes()).get("device") if shared.exists() else None
    except Exception:
        return
    if dev and dev != "unselected":
        target = DATA / "drafts" / f"{''.join(c if c.isalnum() or c in '-._' else '_' for c in dev)}.plist"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shared, target)


def switching() -> bool:
    return _switching


def current_device() -> str | None:
    return _current if _switching else None


def select_device(udid: str) -> Session:
    global _current
    ensure_private_directory(DATA)
    _current = udid
    LAST_DEVICE.write_text(udid)
    return session()


def known_devices() -> dict[str, dict]:
    """Devices with a saved draft: {udid: {"name", "version", "model"}}."""
    import plistlib
    ensure_private_directory(DATA)
    out = {}
    for f in sorted((DATA / "drafts").glob("*.plist")) if (DATA / "drafts").exists() else []:
        try:
            blob = plistlib.loads(f.read_bytes())
        except Exception:
            continue
        info = blob.get("info", {})
        if blob.get("device"):
            out[blob["device"]] = {"name": info.get("DeviceName"), "version": info.get("ProductVersion"),
                                   "model": info.get("ProductType")}
    return out


def session() -> Session:
    global _session
    if _switching:
        key = _current or ""
        if key not in _sessions:
            _sessions[key] = Session(PhoneSource(_current), DATA, device=_current or "unselected")
        return _sessions[key]
    if _session is None:
        _session = Session(make_source(), DATA)
    return _session
