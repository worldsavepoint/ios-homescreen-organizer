"""Check a packaged stdio MCP server using initialization and tool discovery only."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import selectors
import subprocess
import tempfile
import time
import tomllib


def valid_tool(tool):
    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
        return False
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    if "$schema" in schema and not isinstance(schema["$schema"], str):
        return False
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    return (isinstance(properties, dict) and all(isinstance(item, dict) for item in properties.values())
            and isinstance(required, list) and all(isinstance(key, str) for key in required))


def check(command, timeout):
    with tempfile.TemporaryDirectory(prefix="home-screen-mcp-check-") as data:
        fixture = Path(data) / "layout.plist"
        fixture.write_bytes(plistlib.dumps([]))
        environment = {**os.environ, "HOMESCREEN_READ_ONLY": "1", "HOMESCREEN_DATA": data,
                       "HOMESCREEN_FIXTURE": str(fixture)}
        environment.pop("HOMESCREEN_ALLOW_APPLY", None)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   env=environment)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        pending = b""

        def send(message):
            process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
            process.stdin.flush()

        def receive(identifier):
            nonlocal pending
            while True:
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    message = json.loads(line)
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                        raise ValueError("Invalid MCP response")
                    if "id" not in message and isinstance(message.get("method"), str):
                        continue
                    if type(message.get("id")) is not int or message["id"] != identifier:
                        raise ValueError("Unexpected MCP response ID")
                    if "error" in message:
                        raise ValueError("MCP request failed")
                    if "method" in message or not isinstance(message.get("result"), dict):
                        raise ValueError("Invalid MCP result")
                    return message["result"]
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ValueError("MCP startup deadline exceeded")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise ValueError("MCP server closed before completing startup")
                pending += chunk
                if len(pending) > 8 * 1024 * 1024:
                    raise ValueError("MCP response too large")

        try:
            send({"id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "home-screen-organizer-distribution-check", "version": "1.0.0"}}})
            result = receive(1)
            if (result.get("protocolVersion") != "2025-11-25"
                    or not isinstance(result.get("capabilities"), dict)
                    or not isinstance(result["capabilities"].get("tools"), dict)
                    or not isinstance(result.get("serverInfo"), dict)
                    or not isinstance(result["serverInfo"].get("version"), str)
                    or not result["serverInfo"]["version"]):
                raise ValueError("Invalid MCP initialization")
            if result["serverInfo"]["name"] != "ios-homescreen-mcp":
                raise ValueError("Unexpected MCP server identity")
            send({"method": "notifications/initialized"})
            send({"id": 2, "method": "tools/list", "params": {}})
            descriptors = receive(2).get("tools")
            if not isinstance(descriptors, list) or not all(valid_tool(tool) for tool in descriptors):
                raise ValueError("Invalid MCP tool descriptor")
            tools = {tool["name"] for tool in descriptors}
            if not {"read_phone", "show_layout", "move", "diff"} <= tools:
                raise ValueError("Required MCP tools are missing")
            return result["serverInfo"]["version"]
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.timeout <= 0:
        parser.error("Supply a server command and a positive timeout")
    try:
        with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as project:
            expected_version = tomllib.load(project)["project"]["version"]
        if check(command, args.timeout) != expected_version:
            raise ValueError("Packaged MCP version differs from the project release")
        print(f"ios-homescreen-mcp {expected_version} initialized and listed the required tools")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
