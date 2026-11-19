import asyncio
import json
import sys
from argparse import Namespace

import pytest

from homescreen import cli
from homescreen.session import Session
from test_integrity import MemorySource


@pytest.mark.parametrize("operation", ["apply", "restore"])
@pytest.mark.parametrize("confirmation", ["no", "yes"])
def test_readonly_cli_handles_confirmation_without_a_device_write(tmp_path, monkeypatch, capsys, operation, confirmation):
    source = MemorySource()
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    backup = session._backup(source.state, None, "saved")
    monkeypatch.setenv("HOMESCREEN_READ_ONLY", "1")
    monkeypatch.setattr(cli, "session", lambda: session)
    monkeypatch.setattr("builtins.input", lambda prompt: confirmation)
    monkeypatch.setattr(sys, "argv", ["home-screen-organizer", operation,
                                     backup.name if operation == "restore" else session.require().token()])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1 and source.writes == 0
    error = capsys.readouterr().err
    assert ("read-only" in error) is (confirmation == "yes")
    assert "Traceback" not in error and not session._pending_path.exists()


@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_write_and_readback_errors_both_survive(tmp_path, operation):
    source = MemorySource()
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    backup = session._backup(source.state, None, "saved")

    async def fail(*args):
        source.unreadable = True
        raise TimeoutError("synthetic path and account")

    source.write = fail
    result = asyncio.run(session.apply(session.require().token()) if operation == "apply" else session.restore(backup.name))
    assert result["status"] == "unknown"
    assert result["write_error"] == "TimeoutError" and result["readback_error"] == "ConnectionError"
    assert "TimeoutError" in result["error"] and "ConnectionError" in result["error"]
    assert result["recovery"] == "read" and result["backup"] in session.backups()
    assert session._pending_path.exists()


@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_cli_returns_failure_for_unverified_write(tmp_path, monkeypatch, capsys, operation):
    source = MemorySource()
    session = Session(source, tmp_path)
    asyncio.run(session.read())
    backup = session._backup([[], list(reversed(source.state[1]))], None, "saved")
    session.edit(lambda lay: lay.swap("a", "b"))
    async def unchanged(*args):
        pass
    source.write = unchanged
    monkeypatch.setattr(cli, "session", lambda: session)
    monkeypatch.setattr(cli, "_confirm", lambda message: True)
    args = Namespace(cmd=operation, token=session.require().token(), name=backup.name)
    assert asyncio.run(cli.run(args)) != 0
    output = capsys.readouterr().out
    assert json.loads(output[output.index("{"):])["backup"] in session.backups()
