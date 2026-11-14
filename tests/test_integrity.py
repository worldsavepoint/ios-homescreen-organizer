"""Write-safety regressions. All sources are synthetic; no device connection is made."""

import asyncio
import copy
import os
import plistlib
import shutil
import stat

import pytest

from homescreen.device import FileSource, PhoneSource
from homescreen.model import LayoutError, structure
from homescreen.session import Session
from fake_phone import app, widget


def run(coro):
    return asyncio.run(coro)


class MemorySource:
    name = "synthetic"

    def __init__(self):
        self.udid = "synthetic-device-a"
        self.state = [[], [app("a", "A"), app("b", "B")]]
        self.reads = 0
        self.writes = 0
        self.io = 0
        self.change_on_read = None
        self.unreadable = False
        self.corrupt_widget = False

    async def info(self):
        self.io += 1
        return {"UniqueDeviceID": self.udid, "DeviceName": "Synthetic"}

    async def read(self):
        self.io += 1
        self.reads += 1
        if self.unreadable:
            raise ConnectionError("synthetic disconnected")
        if self.reads == self.change_on_read:
            self.state[1].reverse()
        return copy.deepcopy(self.state), None

    async def write(self, state, library, pins=None, grid=None):
        self.io += 1
        self.writes += 1
        self.state = copy.deepcopy(state)
        if self.corrupt_widget:
            self.state[1][0]["elements"][0]["configuration"] = {"city": "Other"}


def loaded(tmp_path, source=None):
    source = source or MemorySource()
    session = Session(source, tmp_path / "data")
    run(session.read())
    return session, source


def test_device_switch_before_apply_is_rejected(tmp_path):
    session, source = loaded(tmp_path)
    source.udid = "synthetic-device-b"
    with pytest.raises(LayoutError, match="identity|device"):
        run(session.apply(session.require().token()))
    assert source.writes == 0


def test_last_read_change_is_rejected(tmp_path):
    session, source = loaded(tmp_path)
    source.change_on_read = source.reads + 2
    with pytest.raises(LayoutError, match="changed"):
        run(session.apply(session.require().token()))
    assert source.writes == 0


def test_widget_configuration_is_part_of_stale_comparison(tmp_path):
    source = MemorySource()
    source.state[1] = [widget("Weather", "small")]
    session, source = loaded(tmp_path, source)
    source.state[1][0]["elements"][0]["configuration"] = {"city": "Other"}
    with pytest.raises(LayoutError, match="changed"):
        run(session.apply(session.require().token()))
    assert source.writes == 0


def test_widget_configuration_is_part_of_readback(tmp_path):
    source = MemorySource()
    source.state[1] = [widget("Weather", "small")]
    session, source = loaded(tmp_path, source)
    source.corrupt_widget = True
    result = run(asyncio.wait_for(session.apply(session.require().token()), 0.2))
    assert result["ok"] is False
    assert result["status"] == "unknown"
    with pytest.raises(LayoutError, match="explicit read|unknown|pending"):
        run(session.apply(session.require().token()))
    assert source.writes == 1


def test_backup_is_unique_private_durable_and_has_identity(tmp_path, monkeypatch):
    session, source = loaded(tmp_path)
    monkeypatch.setattr("homescreen.session._stamp", lambda: "same-second")
    first = session._backup(source.state, None, "before-apply")
    second = session._backup(source.state, None, "before-apply")
    assert first != second
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert plistlib.loads(first.read_bytes())["provenance"]["device_id"] == source.udid


def test_copied_other_device_backup_is_rejected(tmp_path):
    session, source = loaded(tmp_path)
    backup = session._backup(source.state, None, "before-apply")
    source.udid = "synthetic-device-b"
    run(session.read())
    shutil.copyfile(backup, session._dev_dir("backups") / backup.name)
    with pytest.raises(LayoutError, match="identity|device"):
        run(session.restore(backup.name))
    assert source.writes == 0


def test_restore_rechecks_actual_device_and_rejects_legacy_backup(tmp_path):
    session, source = loaded(tmp_path)
    backup = session._backup(source.state, None, "before-apply")
    source.udid = "synthetic-device-b"
    with pytest.raises(LayoutError, match="identity|device"):
        run(session.restore(backup.name))
    source.udid = session.device_id
    old = session._dev_dir("backups") / "legacy.plist"
    old.write_bytes(plistlib.dumps(source.state))
    with pytest.raises(LayoutError, match="identity|provenance"):
        run(session.restore(old.name))
    assert source.writes == 0


def test_missing_actual_device_identity_blocks_write(tmp_path):
    session, source = loaded(tmp_path)
    source.udid = None
    with pytest.raises(LayoutError, match="identity|device"):
        run(session.apply(session.require().token()))
    assert source.writes == 0


def test_final_identity_recheck_blocks_switch_during_read(tmp_path):
    session, source = loaded(tmp_path)
    original_read = source.read

    async def switch_during_read():
        state = await original_read()
        source.udid = "synthetic-device-b"
        return state

    source.read = switch_during_read
    with pytest.raises(LayoutError, match="identity|device"):
        run(session.apply(session.require().token()))
    assert source.writes == 0


def test_unavailable_readback_blocks_restart_until_explicit_read(tmp_path):
    session, source = loaded(tmp_path)
    original_write = source.write

    async def disconnect_after_write(*args):
        await original_write(*args)
        source.unreadable = True

    source.write = disconnect_after_write
    result = run(session.apply(session.require().token()))
    assert result["ok"] is False and result["status"] == "unknown"
    again = Session(source, session.dir)
    source.unreadable = False
    with pytest.raises(LayoutError, match="explicit read|unknown|pending"):
        run(again.apply(again.require().token()))
    assert source.writes == 1
    run(again.read())
    source.write = original_write
    assert run(again.apply(again.require().token()))["ok"]


@pytest.mark.parametrize("action", ["apply", "restore", "phone-write"])
def test_read_only_rejects_before_any_source_io(tmp_path, monkeypatch, action):
    session, source = loaded(tmp_path)
    before = source.io
    monkeypatch.setenv("HOMESCREEN_READ_ONLY", "1")
    if action == "apply":
        operation = session.apply(session.require().token())
    elif action == "restore":
        operation = session.restore("missing.plist")
    else:
        phone = PhoneSource()

        async def forbidden():
            pytest.fail("read-only write attempted device I/O")

        phone.installed_apps = forbidden
        operation = phone.write([[]], None)
    with pytest.raises((LayoutError, PermissionError), match="read.only"):
        run(operation)
    assert source.io == before


@pytest.mark.parametrize("bundle", ["../secret", "/tmp/secret", "..", "com.example/../../secret"])
def test_fixture_icon_rejects_path_escape(tmp_path, bundle):
    source = FileSource(tmp_path / "fixture.plist")
    source.path.with_suffix(".icons").mkdir()
    (tmp_path / "secret.png").write_bytes(b"private")
    assert run(source.icon_png(bundle)) is None


def test_fixture_icon_rejects_symlink_escape(tmp_path):
    source = FileSource(tmp_path / "fixture.plist")
    icons = source.path.with_suffix(".icons")
    icons.mkdir()
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"private")
    (icons / "com.example.app.png").symlink_to(secret)
    assert run(source.icon_png("com.example.app")) is None


def test_fixture_icon_rejects_symlinked_sidecar_directory(tmp_path):
    source = FileSource(tmp_path / "fixture.plist")
    private = tmp_path / "private"
    private.mkdir()
    (private / "com.example.app.png").write_bytes(b"private")
    source.path.with_suffix(".icons").symlink_to(private, target_is_directory=True)
    assert run(source.icon_png("com.example.app")) is None


def test_session_operation_is_bounded_without_write_retry(tmp_path, monkeypatch):
    session, source = loaded(tmp_path)
    session.require().pages[0].reverse()
    monkeypatch.setattr("homescreen.session.OPERATION_TIMEOUT", 0.01, raising=False)

    async def hanging_write(*args):
        source.writes += 1
        await asyncio.Event().wait()

    source.write = hanging_write
    result = run(asyncio.wait_for(session.apply(session.require().token()), 0.2))
    assert result["ok"] is False
    assert source.writes == 1


def test_session_lock_excludes_another_process(tmp_path):
    import subprocess
    import sys

    session, source = loaded(tmp_path)
    lock_path = session.dir / ".session.lock"
    code = "import fcntl,sys; f=open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()"
    holder = subprocess.Popen([sys.executable, "-c", code, str(lock_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(LayoutError, match="another|busy|lock"):
            run(session.apply(session.require().token()))
        assert source.writes == 0
    finally:
        holder.communicate("", timeout=5)


def test_pending_marker_exists_before_device_write(tmp_path):
    session, source = loaded(tmp_path)
    session.require().pages[0].reverse()
    original_write = source.write

    async def inspect_then_write(*args):
        marker = session.dir / ".pending-write.json"
        assert marker.exists()
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600
        assert source.udid in marker.read_text()
        await original_write(*args)

    source.write = inspect_then_write
    result = run(session.apply(session.require().token()))
    assert result["ok"]
    assert not (session.dir / ".pending-write.json").exists()


def test_backup_flushes_data_and_directory(tmp_path, monkeypatch):
    session, source = loaded(tmp_path)
    synced = []
    real_fsync = os.fsync

    def fsync(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    session._backup(source.state, None, "before-apply")
    assert False in synced and True in synced


def test_phone_connection_has_a_deadline_without_retry(monkeypatch):
    import pymobiledevice3.lockdown as lockdown

    calls = []

    async def hanging_connection(**kwargs):
        calls.append(kwargs)
        await asyncio.Event().wait()

    monkeypatch.setattr(lockdown, "create_using_usbmux", hanging_connection)
    monkeypatch.setattr("homescreen.device.DEVICE_OPERATION_TIMEOUT", 0.01, raising=False)
    phone = PhoneSource("synthetic-only")

    async def check_deadline():
        task = asyncio.create_task(phone.info())
        try:
            await asyncio.sleep(0.05)
            assert task.done(), "PhoneSource did not enforce its operation deadline"
            with pytest.raises(TimeoutError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(check_deadline())
    assert calls == [{"serial": "synthetic-only"}]
    assert phone._lockdown is None


@pytest.mark.parametrize("action", ["read", "restore"])
def test_read_and_restore_respect_session_lock(tmp_path, action):
    import fcntl

    session, source = loaded(tmp_path)
    backup = session._backup(source.state, None, "before-apply")
    before = source.io
    with (session.dir / ".session.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        operation = session.read() if action == "read" else session.restore(backup.name)
        with pytest.raises(LayoutError, match="lock"):
            run(operation)
    assert source.io == before


def test_successful_readback_describes_compared_fields(tmp_path):
    session, source = loaded(tmp_path)
    result = run(session.apply(session.require().token()))
    assert result["ok"]
    assert result["warnings"] == ["Readback compares the layout fields returned by the selected source."]


def test_widget_stack_payload_is_preserved_and_compared():
    first = [[], [{**widget("Weather", "small"), "configuration": b"private-setting"}]]
    changed = copy.deepcopy(first)
    changed[1][0]["configuration"] = b"different-setting"
    assert structure(first) != structure(changed)


def test_widget_configuration_comparison_preserves_value_types():
    first = [[], [{**widget("Weather", "small"), "configuration": b"setting"}]]
    changed = copy.deepcopy(first)
    changed[1][0]["configuration"] = "b'setting'"
    assert structure(first) != structure(changed)


def test_legacy_backup_refusal_explains_missing_identity(tmp_path):
    session, source = loaded(tmp_path)
    path = session._dev_dir("backups") / "legacy.plist"
    path.write_bytes(plistlib.dumps(source.state))
    with pytest.raises(LayoutError, match="legacy backups cannot be restored"):
        run(session.restore(path.name))
    assert source.writes == 0


def test_restore_rejects_invalid_backed_up_layout_before_write(tmp_path):
    session, source = loaded(tmp_path)
    # A corrupt backup puts a widget in the dock, which SpringBoard does not support.
    backup = session._backup([[widget("Weather", "small")], []], None, "invalid")
    with pytest.raises(LayoutError, match="backup|widget"):
        run(session.restore(backup.name))
    assert source.writes == 0


def test_cancelled_write_keeps_pending_until_explicit_read(tmp_path):
    session, source = loaded(tmp_path)
    session.require().pages[0].reverse()

    async def cancel_write():
        started = asyncio.Event()

        async def hanging_write(*args):
            source.writes += 1
            started.set()
            await asyncio.Event().wait()

        source.write = hanging_write
        task = asyncio.create_task(session.apply(session.require().token()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(cancel_write())
    restarted = Session(source, session.dir)
    before = source.io
    with pytest.raises(LayoutError, match="pending|unknown"):
        run(restarted.restore("anything.plist"))
    assert source.io == before
    assert (session.dir / ".pending-write.json").exists()


def test_failed_explicit_read_does_not_clear_pending(tmp_path):
    session, source = loaded(tmp_path)
    marker = session.dir / ".pending-write.json"
    marker.write_text('{"device_id": "synthetic-device-a"}')
    source.unreadable = True
    with pytest.raises(ConnectionError):
        run(session.read())
    assert marker.exists()


def test_reading_other_device_does_not_clear_pending(tmp_path):
    session, source = loaded(tmp_path)
    marker = session.dir / ".pending-write.json"
    marker.write_text('{"device_id": "synthetic-device-a"}')
    source.udid = "synthetic-device-b"
    run(session.read())
    assert marker.exists()
    with pytest.raises(LayoutError, match="pending|unknown"):
        run(session.apply(session.require().token()))
    assert source.writes == 0


def test_backup_exclusive_creation_does_not_overwrite(tmp_path, monkeypatch):
    from types import SimpleNamespace

    session, source = loaded(tmp_path)
    monkeypatch.setattr("homescreen.session._stamp", lambda: "same-time")
    monkeypatch.setattr("homescreen.session.uuid.uuid4", lambda: SimpleNamespace(hex="collision"))
    backup = session._backup(source.state, None, "before-apply")
    original = backup.read_bytes()
    with pytest.raises(FileExistsError):
        session._backup([[]], None, "before-apply")
    assert backup.read_bytes() == original


def test_backup_sync_failure_prevents_device_write(tmp_path, monkeypatch):
    session, source = loaded(tmp_path)

    def sync_failure(path):
        raise OSError("synthetic storage error")

    monkeypatch.setattr("homescreen.session._sync_directory", sync_failure)
    with pytest.raises(OSError, match="storage error"):
        run(session.apply(session.require().token()))
    assert source.writes == 0


def test_first_backup_syncs_new_directory_ancestors(tmp_path, monkeypatch):
    session, source = loaded(tmp_path)
    synced = []
    original = __import__("homescreen.session", fromlist=["_sync_directory"])._sync_directory

    def sync(path):
        synced.append(path)
        original(path)

    monkeypatch.setattr("homescreen.session._sync_directory", sync)
    session._backup(source.state, None, "before-apply")
    assert session.dir / "backups" / "synthetic-device-a" in synced
    assert session.dir / "backups" in synced
    assert session.dir in synced


def test_subprocess_operations_have_a_deadline(monkeypatch):
    import subprocess
    import sys
    from homescreen.device import _run

    monkeypatch.setattr("homescreen.device.SUBPROCESS_TIMEOUT", 0.01, raising=False)
    with pytest.raises(subprocess.TimeoutExpired):
        _run(sys.executable, "-c", "import time; time.sleep(0.1)")


@pytest.mark.parametrize("kind", ["fixture", "simulator"])
def test_read_only_blocks_other_sources_before_file_io(tmp_path, monkeypatch, kind):
    from homescreen.device import SimSource

    if kind == "fixture":
        source = FileSource(tmp_path / "does-not-exist.plist")
    else:
        source = SimSource("synthetic-only")
        source.dir = tmp_path
    monkeypatch.setenv("HOMESCREEN_READ_ONLY", "1")
    with pytest.raises(PermissionError, match="read.only"):
        run(source.write([[]], None))


def test_session_data_root_drafts_and_reads_are_private(tmp_path):
    os.chmod(tmp_path, 0o755)
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    os.chmod(data, 0o755)
    source = MemorySource()
    session = Session(source, data)
    run(session.read())
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE(session.draft_path.stat().st_mode) == 0o600
    reads = list((data / "reads").rglob("*.plist"))
    assert reads and all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in reads)
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755


def test_session_refuses_symlink_data_root(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    os.chmod(actual, 0o755)
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises((LayoutError, OSError), match="symlink|symbolic|Too many levels"):
        Session(MemorySource(), link)
    assert stat.S_IMODE(actual.stat().st_mode) == 0o755


def test_failed_read_cannot_reassign_old_draft_to_another_device(tmp_path):
    session, source = loaded(tmp_path)
    original_device = session.device_id
    original_info = copy.deepcopy(session.info)
    original_grid = session.grid
    original_layout = session.require()
    token = original_layout.token()
    source.udid = "synthetic-device-b"
    source.unreadable = True
    with pytest.raises(ConnectionError):
        run(session.read())
    assert session.device_id == original_device
    assert session.info == original_info
    assert session.grid == original_grid
    assert session.require() is original_layout
    source.unreadable = False
    with pytest.raises(LayoutError, match="identity|device"):
        run(session.apply(token))
    assert source.writes == 0


@pytest.mark.parametrize("when", ["before-apply", "last-read"])
def test_exported_pin_only_change_is_stale(tmp_path, when):
    source = MemorySource()
    source.pins = {}
    session, source = loaded(tmp_path, source)
    session.require().pages[0].reverse()
    if when == "before-apply":
        source.pins = {"a": 7}
    else:
        original_read = source.read
        change_at = source.reads + 2

        async def change_during_final_read():
            result = await original_read()
            if source.reads == change_at:
                source.pins = {"a": 7}
            return result

        source.read = change_during_final_read
    with pytest.raises(LayoutError, match="changed"):
        run(session.apply(session.require().token()))
    assert source.writes == 0


@pytest.mark.parametrize("failure", ["identity", "shape", "save"])
def test_failed_read_restores_complete_previous_context(tmp_path, monkeypatch, failure):
    session, source = loaded(tmp_path)
    previous = session.device_id, session.info, session.grid, session.layout
    source.udid = "synthetic-device-b"
    if failure == "identity":
        original_info = source.info
        calls = 0

        async def switched_info():
            nonlocal calls
            calls += 1
            result = await original_info()
            if calls == 2:
                result["UniqueDeviceID"] = "synthetic-device-c"
            return result

        source.info = switched_info
    elif failure == "shape":
        source.state = [None]
    else:
        def save_failure():
            raise OSError("synthetic storage failure")

        monkeypatch.setattr(session, "_save_draft", save_failure)
    with pytest.raises((LayoutError, TypeError, OSError)):
        run(session.read())
    assert (session.device_id, session.info, session.grid, session.layout) == previous
    assert source.writes == 0


@pytest.fixture
def fake_phone_transport(monkeypatch):
    """Fake the physical transport boundary, retaining the real PhoneSource/Session."""
    from types import SimpleNamespace
    import pymobiledevice3.lockdown as lockdown
    import pymobiledevice3.services.springboard as springboard

    control = SimpleNamespace(active="synthetic-a", serials=[], writes=[], metrics=0,
                              switch_on_metrics=None, honor_serial=True, live_ids={}, closed=[])
    state = [[], [app("a", "A"), app("b", "B")]]

    class Lockdown:
        def __init__(self, identity):
            self.identity = identity
            self.all_values = {"UniqueDeviceID": identity, "DeviceName": "Synthetic"}

        async def get_value(self):
            return {**self.all_values, "UniqueDeviceID": control.live_ids.get(self.identity, self.identity)}

        async def close(self):
            control.closed.append(self.identity)

    async def create_using_usbmux(*, serial):
        control.serials.append(serial)
        if control.honor_serial and serial is not None and serial != control.active:
            raise ConnectionError("the requested synthetic device is disconnected")
        return Lockdown(control.active)

    class SpringBoard:
        def __init__(self, *, lockdown):
            self.lockdown = lockdown

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_homescreen_icon_metrics(self):
            control.metrics += 1
            if control.metrics == control.switch_on_metrics:
                control.active = "synthetic-b"
                raise ConnectionError("synthetic USB disconnected during metrics")
            return {}

        async def get_icon_state(self, **kwargs):
            return copy.deepcopy(state)

        async def set_icon_state(self, payload):
            control.writes.append(self.lockdown.identity)

    monkeypatch.setattr(lockdown, "create_using_usbmux", create_using_usbmux)
    monkeypatch.setattr(springboard, "SpringBoardServicesService", SpringBoard)
    return control


def test_phone_reconnect_is_bound_to_first_actual_identity(fake_phone_transport):
    control = fake_phone_transport
    phone = PhoneSource()
    run(phone._ld())
    control.active = "synthetic-b"
    phone._lockdown = None
    with pytest.raises((ConnectionError, ValueError), match="identity|device"):
        run(phone._ld())
    assert control.serials == [None, "synthetic-a"]
    assert not control.writes


def test_phone_reconnect_rejects_transport_returning_wrong_identity(fake_phone_transport):
    control = fake_phone_transport
    phone = PhoneSource("synthetic-a")
    control.active = "synthetic-b"
    control.honor_serial = False
    with pytest.raises(ValueError, match="identity|device"):
        run(phone._ld())
    assert phone._lockdown is None
    assert control.closed == ["synthetic-b"]


def test_phone_info_rejects_live_identity_disagreeing_with_cached_metadata(fake_phone_transport):
    control = fake_phone_transport
    phone = PhoneSource("synthetic-a")
    run(phone._ld())
    control.live_ids["synthetic-a"] = "synthetic-b"
    with pytest.raises((ConnectionError, ValueError), match="identity|device"):
        run(phone.info())
    assert not control.writes


def test_metrics_disconnect_during_final_identity_cannot_write_another_phone(tmp_path, fake_phone_transport):
    control = fake_phone_transport
    phone = PhoneSource()
    phone.write_mode = "list"
    session = Session(phone, tmp_path / "data")
    run(session.read())
    session.require().pages[0].reverse()
    control.switch_on_metrics = 4
    with pytest.raises((ConnectionError, LayoutError, ValueError), match="identity|disconnected|device"):
        run(session.apply(session.require().token()))
    assert not control.writes
    assert session.device_id == "synthetic-a"
