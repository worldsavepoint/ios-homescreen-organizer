import asyncio
import copy
import os
import plistlib
from pathlib import Path
import subprocess
import sys

import pytest

from homescreen import config
from homescreen.model import LayoutError
from homescreen.session import Session
from test_integrity import MemorySource


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin inherited ACLs")


def grants(path):
    return " allow " in subprocess.check_output(["/bin/ls", "-lde", path], text=True)


def shared_parent(path):
    path.mkdir()
    subprocess.run(["/bin/chmod", "+a", "everyone allow read,execute,file_inherit,directory_inherit", path], check=True)


def test_new_session_root_prevents_inherited_grants(tmp_path):
    root = tmp_path / "shared"
    shared_parent(root)
    session = Session(MemorySource(), root / "data")
    asyncio.run(session.read())
    assert grants(root) and not grants(session.dir)
    assert not grants(session.draft_path)
    assert not grants(next((session.dir / "reads").rglob("*.plist")))


@pytest.mark.parametrize("entry", ["session", "known", "switching"])
def test_existing_shared_root_remains_intact_and_private_data_is_not_opened(tmp_path, monkeypatch, entry):
    parent = tmp_path / "shared"
    shared_parent(parent)
    root = parent / "data"
    root.mkdir()
    record = root / "draft.plist"
    record.write_bytes(b"original")
    monkeypatch.setattr(config, "DATA", root)
    for name in ("HOMESCREEN_FIXTURE", "HOMESCREEN_SIM", "HOMESCREEN_UDID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises((LayoutError, PermissionError)):
        if entry == "session":
            Session(MemorySource(), root)
        elif entry == "known":
            config.known_devices()
        else:
            config.enable_device_switching()
    assert record.read_bytes() == b"original" and grants(root) and grants(record)


def test_deny_only_acl_remains_usable_and_unchanged(tmp_path):
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    subprocess.run(["/bin/chmod", "+a", "everyone deny delete", root], check=True)
    session = Session(MemorySource(), root)
    asyncio.run(session.read())
    assert " deny delete" in subprocess.check_output(["/bin/ls", "-lde", root], text=True)


@pytest.mark.parametrize("level", ["kind", "device"])
def test_backup_descendant_links_cannot_redirect_publication(tmp_path, level):
    session = Session(MemorySource(), tmp_path / "data")
    asyncio.run(session.read())
    outside = tmp_path / "outside"
    shared_parent(outside)
    foreign = outside / "user-file"
    foreign.write_bytes(b"user data")
    original = foreign.stat()
    link = session.dir / "backups"
    if level == "device":
        link.mkdir(mode=0o700)
        link /= session.device_id
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises((PermissionError, OSError)):
        session._backup(session.require().to_state(), None, "capture")
    assert sorted(path.name for path in outside.iterdir()) == ["user-file"]
    assert foreign.read_bytes() == b"user data" and foreign.stat().st_mode == original.st_mode
    assert link.is_symlink() and grants(outside)


def test_shared_backup_descendant_is_rejected_without_repair(tmp_path):
    session = Session(MemorySource(), tmp_path / "data")
    asyncio.run(session.read())
    backups = session.dir / "backups"
    shared_parent(backups)
    before = subprocess.check_output(["/bin/ls", "-lde", backups])
    with pytest.raises(PermissionError):
        session._backup(session.require().to_state(), None, "capture")
    assert subprocess.check_output(["/bin/ls", "-lde", backups]) == before
    assert list(backups.iterdir()) == []


@pytest.mark.parametrize("kind", ["drafts", "reads", "unresolved-writes"])
def test_private_descendant_links_are_rejected_before_data_access(tmp_path, kind):
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / kind).symlink_to(outside, target_is_directory=True)
    if kind == "drafts":
        with pytest.raises((PermissionError, OSError)):
            Session(MemorySource(), root, device="synthetic-device-a")
    elif kind == "reads":
        with pytest.raises((PermissionError, OSError)):
            asyncio.run(Session(MemorySource(), root).read())
    else:
        session = Session(MemorySource(), root)
        with pytest.raises((PermissionError, OSError)):
            session._dev_dir(kind)
    assert list(outside.iterdir()) == []


def test_backup_publication_stays_on_opened_directory_after_rename(tmp_path, monkeypatch):
    session = Session(MemorySource(), tmp_path / "data")
    asyncio.run(session.read())
    backups = session._dev_dir("backups").parent
    moved = session.dir / "saved-backups"
    outside = tmp_path / "outside"
    shared_parent(outside)
    original = os.open
    swapped = False
    def swap(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and "capture" in Path(os.fsdecode(path)).name and flags & os.O_EXCL:
            swapped = True
            backups.rename(moved)
            backups.symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swap)
    try:
        session._backup(session.require().to_state(), None, "capture")
    except OSError:
        pass
    assert swapped and list(outside.iterdir()) == []
    assert len(list(moved.rglob("*capture*.plist"))) == 1


def test_legacy_owned_descendants_preserve_modes_and_recovery(tmp_path):
    source = MemorySource()
    original_state = copy.deepcopy(source.state)
    session = Session(source, tmp_path / "data")
    asyncio.run(session.read())
    session.edit(lambda lay: lay.move("a", "dock"))
    result = asyncio.run(session.apply(session.require().token()))
    assert result["ok"]
    backup = result["backup"]
    directories = [path for path in session.dir.rglob("*") if path.is_dir()]
    for path in directories:
        path.chmod(0o755)
    reopened = Session(source, session.dir)
    assert reopened.require().dock[0].id == "a"
    asyncio.run(reopened.read())
    reopened.edit(lambda lay: lay.move("a", "page:1"))
    assert asyncio.run(reopened.apply(reopened.require().token()))["ok"]
    assert asyncio.run(reopened.restore(backup))["ok"]
    assert source.state == original_state
    assert all(path.stat().st_mode & 0o777 == 0o755 for path in directories)
    assert session.dir.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("kind", ["writable", "foreign"])
def test_existing_unowned_or_writable_descendant_is_preserved(tmp_path, monkeypatch, kind):
    session = Session(MemorySource(), tmp_path / "data")
    asyncio.run(session.read())
    backups = session._dev_dir("backups")
    original = backups.stat()
    if kind == "writable":
        backups.chmod(0o777)
    else:
        fstat = os.fstat
        def foreign(fd):
            value = fstat(fd)
            if value.st_dev == original.st_dev and value.st_ino == original.st_ino:
                fields = list(value)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return value
        monkeypatch.setattr(os, "fstat", foreign)
    with pytest.raises(PermissionError):
        session._backup(session.require().to_state(), None, "capture")
    assert list(backups.iterdir()) == []
    assert backups.stat().st_mode & 0o777 == (0o777 if kind == "writable" else 0o700)


@pytest.mark.parametrize("kind", ["regular", "unsupported", "shared-writable"])
def test_private_file_validates_nonblocking_descriptor_before_use(tmp_path, monkeypatch, kind):
    import stat
    from homescreen.private_store import open_private_file

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    record = root / "record"
    record.write_bytes(b"record")
    record.chmod(0o664 if kind == "shared-writable" else 0o644)
    opened = []
    real_open, real_fstat = os.open, os.fstat

    def observe(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        if name == record.name:
            opened.append((fd, flags, kwargs["dir_fd"]))
        return fd

    def descriptor(fd):
        info = real_fstat(fd)
        if kind == "unsupported" and opened and opened[0][0] == fd:
            fields = list(info)
            fields[0] = stat.S_IFIFO | 0o600
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "open", observe)
    monkeypatch.setattr(os, "fstat", descriptor)
    if kind == "regular":
        with os.fdopen(open_private_file(root, record), "rb") as stream:
            assert os.get_blocking(stream.fileno()) and stream.read() == b"record"
    else:
        with pytest.raises(PermissionError):
            with os.fdopen(open_private_file(root, record), "rb"):
                pass
        with pytest.raises(OSError):
            real_fstat(opened[0][0])
    _, flags, parent = opened[0]
    required = os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    assert parent is not None and flags & required == required


@pytest.mark.parametrize("malformed", [
    [], "scalar", {"device": "broken", "info": []}, {"device": "broken", "info": "scalar"},
    {"device": ["broken"]}, {"device": {"id": "broken"}}, {"device": 7}, {"device": True},
    {"device": ""}, {"info": {}},
], ids=["root-list", "root-scalar", "info-list", "info-scalar", "device-list", "device-dict",
        "device-int", "device-bool", "device-empty", "device-missing"])
def test_malformed_remembered_draft_preserves_public_device_discovery(tmp_path, monkeypatch, malformed):
    import httpx
    from homescreen import ui

    root = tmp_path / "data"
    selected = Session(MemorySource(), root, device="valid-device")
    monkeypatch.setattr(config, "DATA", root)
    monkeypatch.setattr(config, "_switching", True)
    monkeypatch.setattr(config, "_current", "valid-device")
    monkeypatch.setattr(ui, "session", lambda: selected)
    monkeypatch.setattr(ui, "_names", {})

    async def connected():
        return {"connected-device": {"name": "Connected fixture"}}

    monkeypatch.setattr(ui, "_connected", connected)
    drafts = root / "drafts"
    neighbors = {
        "valid.plist": {"device": "valid-device", "info": {
            "DeviceName": "Valid fixture", "ProductVersion": "18.0", "ProductType": "iPhone-fixture"},
            "futureField": {"retained": True}},
        "legacy.plist": {"device": "legacy identity / unrestricted", "futureField": [1, 2]},
    }
    for name, blob in neighbors.items():
        (drafts / name).write_bytes(plistlib.dumps(blob))
    damaged = drafts / "damaged.plist"

    async def scenario():
        transport = httpx.ASGITransport(app=ui.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1",
                                     headers={"x-token": ui.TOKEN}) as client:
            normal = await client.get("/api/devices")
            assert normal.status_code == 200
            assert normal.json() == {"switching": True, "current": "valid-device", "discovery": "complete",
                "devices": [
                    {"udid": "connected-device", "name": "Connected fixture", "connected": True, "draft": False},
                    {"udid": "legacy identity / unrestricted", "name": None, "version": None,
                     "model": None, "connected": False, "draft": True},
                    {"udid": "valid-device", "name": "Valid fixture", "version": "18.0",
                     "model": "iPhone-fixture", "connected": False, "draft": True},
                ]}
            damaged.write_bytes(b"undecodable plist")
            undecodable = await client.get("/api/devices")
            assert undecodable.status_code == 200 and undecodable.json() == normal.json()
            damaged.write_bytes(plistlib.dumps(malformed))
            before = {path.name: path.read_bytes() for path in drafts.iterdir()}
            response = await client.get("/api/devices")
            assert response.status_code == 200
            assert response.json() == normal.json()
            assert {path.name: path.read_bytes() for path in drafts.iterdir()} == before

    asyncio.run(scenario())
