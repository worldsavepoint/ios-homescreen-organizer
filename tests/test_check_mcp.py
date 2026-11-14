"""The native distribution check only initializes and discovers tools, with a deadline."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "macapp/check_mcp.py"


def check(tmp_path, behavior="ok"):
    server = tmp_path / "fixture server.py"
    pid = tmp_path / "pid"
    server.write_text('''import json, os, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
behavior = sys.argv[2]
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
        info = {"name": "wrong" if behavior == "name" else "ios-homescreen-mcp", "version": "1.2.0"}
        response = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": info}
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        response = {"tools": [{"name": name} for name in (["read_phone"] if behavior == "tools" else ["read_phone", "show_layout", "move", "diff"])]}
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
                             str(server), str(pid), behavior], capture_output=True, text=True, timeout=5,
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


def test_real_server_initializes_and_discovers_without_device_operations(tmp_path):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("HOMESCREEN_")}
    env.update(HOMESCREEN_DATA=str(tmp_path / "unused data"), HOMESCREEN_ALLOW_APPLY="0")
    result = subprocess.run([sys.executable, str(SCRIPT), "--timeout", "30", "--",
                             sys.executable, "-m", "homescreen.server"],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=35)
    assert result.returncode == 0, result.stderr
    assert "ios-homescreen-mcp initialized; required tools discovered" in result.stdout
    assert not (tmp_path / "unused data").exists()


@pytest.mark.parametrize("behavior", ["name", "tools", "error", "eof"])
def test_invalid_server_fails_and_closes_process(tmp_path, behavior):
    result, _ = check(tmp_path, behavior)
    assert result.returncode != 0
    assert result.stderr


def test_startup_deadline_stops_and_closes_unresponsive_process(tmp_path):
    result, elapsed = check(tmp_path, "hang")
    assert result.returncode != 0
    assert "deadline" in result.stderr.lower()
    assert elapsed < 3
