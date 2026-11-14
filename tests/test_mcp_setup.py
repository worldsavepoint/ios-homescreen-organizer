"""Native setup formats preserve the engine path and read-only MCP launch options."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def setup_binary(tmp_path_factory):
    if shutil.which("swiftc") is None:
        pytest.skip("Swift compiler unavailable")
    source = ROOT / "macapp/Shell/Sources/Homescreen/MCPSetup.swift"
    assert source.is_file(), "The native app needs a client-neutral MCP setup helper"
    folder = tmp_path_factory.mktemp("mcp-setup")
    harness = folder / "main.swift"
    harness.write_text('''import Foundation
let mode: MCPAccessMode = CommandLine.arguments.count > 2 ? .applyEnabled : .readOnly
let setup = MCPSetup(executablePath: CommandLine.arguments[1], mode: mode)
let output = ["launch": setup.launchCommand,
              "json": setup.cursorClaudeJSON,
              "toml": setup.codexTOML,
              "vscode": setup.vscodeJSON,
              "editor": setup.editorJSON,
              "copilot": setup.copilotJSON]
let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
print(String(decoding: data, as: UTF8.self))
''')
    binary = folder / "mcp-setup"
    result = subprocess.run(["swiftc", "-module-cache-path", str(folder / "modules"),
                             str(source), *[str(source.with_name(name)) for name in ("MCPClientManager.swift", "MCPSubprocess.swift", "MCPEntryStore.swift", "MCPRegistrationJournal.swift", "MCPJSON.swift")], str(harness), "-o", str(binary)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return binary


def generate(binary, executable):
    result = subprocess.run([str(binary), str(executable)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("executable", [
    "/Applications/Home Screen Organizer.app/Contents/Resources/engine/engine",
    '/Applications/Organize "引用" \\ folder\'s.app/Contents/Resources/engine/engine',
    '/Applications/Line\nTab\tControl\x01.app/engine',
    '/Applications/Example\x7f.app/engine',
])
def test_client_formats_preserve_engine_and_read_only_environment(setup_binary, executable):
    output = generate(setup_binary, executable)
    expected = {"command": executable, "args": ["mcp"], "env": {"HOMESCREEN_READ_ONLY": "1"}}
    assert json.loads(output["json"]) == {"mcpServers": {"ios-homescreen-mcp": expected}}
    assert tomllib.loads(output["toml"]) == {"mcp_servers": {"ios-homescreen-mcp": {**expected,"environment_id":"local"}}}
    assert json.loads(output["editor"]) == {"servers": {"ios-homescreen-mcp": {"type": "stdio", **expected}}}
    assert output["copilot"] == output["vscode"]
    assert json.loads(output["vscode"]) == {
        "mcpServers": {"ios-homescreen-mcp": {"type": "stdio", "tools": ["*"], **expected}}
    }


def test_launch_command_preserves_shell_path_and_sets_read_only(setup_binary, tmp_path):
    executable = tmp_path / 'Home Screen "引用" \\ folder\'s $(false) `false`.app' / "engine"
    executable.parent.mkdir()
    executable.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
print(json.dumps({"args": sys.argv[1:], "read_only": os.environ.get("HOMESCREEN_READ_ONLY")}))
''')
    executable.chmod(0o755)
    output = generate(setup_binary, executable)
    result = subprocess.run(["/bin/sh", "-c", output["launch"]], capture_output=True, text=True,
                            env={**os.environ, "HOMESCREEN_READ_ONLY": "0"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"args": ["mcp"], "read_only": "1"}


def test_apply_mode_exports_match_registration_environment(setup_binary):
    result = subprocess.run([str(setup_binary), "/synthetic engine", "apply"], capture_output=True, text=True, check=True)
    output = json.loads(result.stdout)
    expected = {"HOMESCREEN_READ_ONLY": "0", "HOMESCREEN_ALLOW_APPLY": "1"}
    assert json.loads(output["json"])["mcpServers"]["ios-homescreen-mcp"]["env"] == expected
    assert json.loads(output["vscode"])["mcpServers"]["ios-homescreen-mcp"]["env"] == expected
    assert json.loads(output["editor"])["servers"]["ios-homescreen-mcp"]["env"] == expected
    assert json.loads(output["copilot"])["mcpServers"]["ios-homescreen-mcp"]["env"] == expected
    codex = tomllib.loads(output["toml"])["mcp_servers"]["ios-homescreen-mcp"]
    assert codex["env"] == expected and codex["environment_id"] == "local"
    assert output["launch"] == "HOMESCREEN_READ_ONLY=0 HOMESCREEN_ALLOW_APPLY=1 '/synthetic engine' mcp"


def test_public_command_names_keep_legacy_aliases():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "home-screen-organizer"
    for name in ("home-screen-organizer", "ios-homescreen-organizer", "homescreen"):
        assert project["scripts"][name] == "homescreen.cli:main"
    for name in ("home-screen-mcp", "ios-homescreen-mcp", "homescreen-mcp"):
        assert project["scripts"][name] == "homescreen.server:main"


def test_cli_help_uses_formal_command(tmp_path):
    result = subprocess.run([sys.executable, "-m", "homescreen.cli", "--help"],
                            capture_output=True, text=True, env={
                                **os.environ, "HOMESCREEN_READ_ONLY": "1",
                                "HOMESCREEN_DATA": str(tmp_path / "data"),
                            })
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: home-screen-organizer ")
