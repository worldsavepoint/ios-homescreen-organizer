import asyncio
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
