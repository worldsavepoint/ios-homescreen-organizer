"""The native distribution check only initializes and discovers tools, with a deadline."""
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "macapp/check_mcp.py"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def check(tmp_path, behavior="ok"):
    server = tmp_path / "fixture server.py"
    pid = tmp_path / "pid"
    server.write_text('''import json, os, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
behavior = sys.argv[2]
release = sys.argv[3]
assert os.environ["HOMESCREEN_READ_ONLY"] == "1"
assert Path(os.environ["HOMESCREEN_DATA"]).is_dir()
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "initialize":
        assert request["params"]["protocolVersion"] == "2025-11-25"
        if behavior == "hang":
            time.sleep(30)
        if behavior == "eof":
            sys.exit(0)
        info = {"name": "wrong" if behavior == "name" else "ios-homescreen-mcp", "version": "0.1.0" if behavior == "release" else release}
        response = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": info}
        if behavior == "version": response["protocolVersion"] = "not-supported"
        if behavior == "capability": response["capabilities"] = {}
        if behavior == "info": info["version"] = False
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        response = {"tools": [{"name": name, "inputSchema":{"type":"object"}} for name in (["read_phone"] if behavior == "tools" else ["read_phone", "show_layout", "move", "diff"])]}
    else:
        raise AssertionError("Unexpected method " + method)
    if behavior == "error":
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32603, "message": "fixture failure"}}), flush=True)
    else:
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "fixture"}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": response}), flush=True)
time.sleep(30)
''')
    start = time.monotonic()
    result = subprocess.run([sys.executable, str(SCRIPT), "--timeout", "0.5", "--", sys.executable,
                             str(server), str(pid), behavior, VERSION], capture_output=True, text=True, timeout=5,
                            env={**os.environ, "HOMESCREEN_READ_ONLY": "0", "HOMESCREEN_DATA": "/unused-fixture-path"})
    elapsed = time.monotonic() - start
    assert pid.exists(), result.stderr
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)
    return result, elapsed


def test_check_initializes_discovers_tools_and_closes_process(tmp_path):
    result, _ = check(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "ios-homescreen-mcp" in result.stdout


def test_real_server_initializes_and_discovers_without_device_operations(tmp_path, monkeypatch):
    from macapp.check_mcp import check as check_server
    fixture = tmp_path / "source.json"
    fixture.write_text("[]")
    monkeypatch.setenv("HOMESCREEN_FIXTURE", str(fixture))
    monkeypatch.setenv("HOMESCREEN_READ_ONLY", "1")
    monkeypatch.setenv("HOMESCREEN_DATA", str(tmp_path / "unused data"))
    monkeypatch.chdir(ROOT)
    received = check_server([sys.executable, "-m", "homescreen.server"], timeout=30)
    assert received == VERSION
    assert not (tmp_path / "unused data").exists()


@pytest.mark.parametrize("behavior", ["name", "tools", "error", "eof", "version", "release", "capability", "info"])
def test_invalid_server_fails_and_closes_process(tmp_path, behavior):
    result, _ = check(tmp_path, behavior)
    assert result.returncode != 0
    assert result.stderr


def test_startup_deadline_stops_and_closes_unresponsive_process(tmp_path):
    result, elapsed = check(tmp_path, "hang")
    assert result.returncode != 0
    assert "deadline" in result.stderr.lower()
    assert elapsed < 3


def test_source_version_available_without_installed_distribution(monkeypatch):
    from importlib.metadata import PackageNotFoundError
    from homescreen import server

    def source_only(_):
        raise PackageNotFoundError()

    monkeypatch.setattr(server, "version", source_only)
    assert server._project_version() == tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


@pytest.mark.parametrize("installed, release", [("1.3.0", "1.3.0"), ("1.3.0a2", "1.3.0-alpha.2"),
                                                ("1.3.0b12", "1.3.0-beta.12"), ("1.3.0rc3", "1.3.0-rc.3")])
def test_installed_and_source_prerelease_identity(tmp_path, monkeypatch, installed, release):
    from homescreen import server
    from importlib.metadata import PackageNotFoundError

    monkeypatch.setattr(server, "version", lambda name: installed)
    assert server._project_version() == release
    def missing(name):
        raise PackageNotFoundError(name)
    monkeypatch.setattr(server, "version", missing)
    monkeypatch.setattr(server, "__file__", str(tmp_path / "homescreen/server.py"))
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion="{release}"\n')
    assert server._project_version() == release
