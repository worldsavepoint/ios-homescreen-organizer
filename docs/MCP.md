# MCP setup

[README](../README.md) · [简体中文](MCP.zh-CN.md) · [How it works](HOW-IT-WORKS.md)

Screen Organizer exposes a local **stdio MCP server** for inspecting layouts, editing drafts, sorting, previews and backups. The client launches the process on the Mac connected to your device. No HTTP endpoint or API key is required.

## Choose a launch method

| Installation | Executable | Arguments |
| --- | --- | --- |
| Mac app in Applications | `/Applications/Screen Organizer.app/Contents/Resources/engine/engine` | `mcp` |
| [Source installation](../macapp/BUILDING.md#source-installation) | `/absolute/path/ios-homescreen-organizer/.venv/bin/home-screen-mcp` | None |
| Standalone MCPB | Client-managed `server/launch.sh` | Managed by the bundle manifest |

Open **MCP Setup** in the app after accepting the launch agreement. The client panel detects Codex and Claude Code and can register, inspect and update this server. If a client executable is missing, install it using its official guide or choose its executable in the panel. **Read-only** is the default. Register uses the mode selected in **Advanced settings** and the existing **stdio** engine.

**Advanced settings** show the current engine path and access mode and copy a launch command, Cursor / Claude JSON, Codex TOML, VS Code JSON or Copilot JSON. Exports use the selected mode; Apply-enabled mode sets both `HOMESCREEN_READ_ONLY=0` and `HOMESCREEN_ALLOW_APPLY=1`. Configuration status records a saved registration; the separate startup check initializes and discovers tools; live connection status comes from client-reported state.

Use an absolute executable path in client settings. After upgrading from the old app name or moving the app, select **Update** in the client panel or replace the old engine path with `/Applications/Screen Organizer.app/Contents/Resources/engine/engine`. The package name, commands, server ID, Registry namespace and saved-data location stay compatible. **Device → MCP** also offers read-only configuration shortcuts. Any local stdio MCP client can use the settings below; consult its official configuration guide for its destination.

The client starts and maintains the stdio process. A terminal launch waits for MCP input; it does not open the editor:

```sh
HOMESCREEN_READ_ONLY=1 '/Applications/Screen Organizer.app/Contents/Resources/engine/engine' mcp
```

For a source install, the equivalent is:

```sh
HOMESCREEN_READ_ONLY=1 /absolute/path/ios-homescreen-organizer/.venv/bin/home-screen-mcp
```

The stable server ID is `ios-homescreen-mcp`. The legacy commands `ios-homescreen-mcp` and `homescreen-mcp` remain aliases of `home-screen-mcp`.

## Client guides

Keep existing servers when adding these entries. Restart or reconnect the client after changing its configuration, then confirm that Screen Organizer’s tools appear.

<a id="codex"></a>

<details open>
<summary><strong>Codex — CLI or TOML</strong></summary>

Register the bundled engine from a terminal:

```sh
codex mcp add ios-homescreen-mcp --env HOMESCREEN_READ_ONLY=1 -- \
  '/Applications/Screen Organizer.app/Contents/Resources/engine/engine' mcp
```

Or add this to `~/.codex/config.toml`:

```toml
[mcp_servers.ios-homescreen-mcp]
command = "/Applications/Screen Organizer.app/Contents/Resources/engine/engine"
args = ["mcp"]
environment_id = "local"

[mcp_servers.ios-homescreen-mcp.env]
HOMESCREEN_READ_ONLY = "1"
```

For a source install, set `command` to the absolute `.venv/bin/home-screen-mcp` path and remove `args`, or register that executable directly with the CLI. See the [official Codex MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

</details>

<a id="cursor-and-claude"></a>

<details>
<summary><strong>Cursor / Claude — JSON or Claude Code CLI</strong></summary>

Use the JSON below in Cursor’s `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project), or in Claude Desktop’s `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ios-homescreen-mcp": {
      "command": "/Applications/Screen Organizer.app/Contents/Resources/engine/engine",
      "args": ["mcp"],
      "env": { "HOMESCREEN_READ_ONLY": "1" }
    }
  }
}
```

For Claude Code, register the same server with:

```sh
claude mcp add --env HOMESCREEN_READ_ONLY=1 --transport stdio ios-homescreen-mcp -- \
  '/Applications/Screen Organizer.app/Contents/Resources/engine/engine' mcp
```

For a source install, replace `command` with the absolute `.venv/bin/home-screen-mcp` path and omit `args`; use that executable without `mcp` in the CLI command. See [Cursor MCP configuration](https://cursor.com/docs/mcp), [Claude Desktop local servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers) and [Claude Code MCP](https://code.claude.com/docs/en/mcp).

</details>

<a id="vs-code-and-copilot"></a>

<details>
<summary><strong>VS Code / Copilot — native MCP configuration</strong></summary>

For VS Code workspace settings, add the server to `.vscode/mcp.json`:

```json
{
  "servers": {
    "ios-homescreen-mcp": {
      "type": "stdio",
      "command": "/Applications/Screen Organizer.app/Contents/Resources/engine/engine",
      "args": ["mcp"],
      "env": { "HOMESCREEN_READ_ONLY": "1" }
    }
  }
}
```

For personal Copilot settings, add the following to `~/.copilot/mcp-config.json`. If `COPILOT_HOME` is set, use `$COPILOT_HOME/mcp-config.json`. A workspace can instead use `.mcp.json` at its root.

```json
{
  "mcpServers": {
    "ios-homescreen-mcp": {
      "type": "stdio",
      "command": "/Applications/Screen Organizer.app/Contents/Resources/engine/engine",
      "args": ["mcp"],
      "env": { "HOMESCREEN_READ_ONLY": "1" },
      "tools": ["*"]
    }
  }
}
```

In VS Code, select the Copilot harness, run **Chat: Open Customizations → MCP Servers → Add Server**, and choose **Global → Copilot Global** for the same personal destination. `tools: ["*"]` exposes all server tools to Copilot; client approvals and the server’s write controls still apply.

For a source install, replace `command` with the absolute `.venv/bin/home-screen-mcp` path and omit `args`. See the [official VS Code / Copilot configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) for destinations and fields.

</details>

<a id="standalone-mcpb"></a>

## Standalone MCPB

Download `home-screen-organizer-<version>-macos.mcpb` and `SHA256SUMS.txt` from [Releases](https://github.com/worldsavepoint/ios-homescreen-organizer/releases), then [verify the checksum](../README.md#install). The bundle contains Apple silicon and Intel engines, dependencies and license records; it requires macOS 14+ and needs neither a Python install nor the Mac app.

Import the file using your client’s MCPB install action. Client support determines whether direct import is available. The manifest and launcher default to read-only mode. For a client with stdio configuration but no MCPB importer, use the Mac app or source executable above.

For bundle structure and native build details, see [standalone MCP bundle assembly](../macapp/BUILDING.md#standalone-mcp-bundle). The format is documented in the [official MCPB project](https://github.com/anthropics/mcpb#readme).

## Draft workflow

1. Connect and unlock the device, trust the Mac, then ask the assistant to call `read_phone`. A fresh read replaces the current draft.
2. Inspect with `show_layout` or `find_apps`, then edit with moves, folders or sorting tools. Edits support `undo`, `redo` and `reset_draft`.
3. Ask for `diff` and `render_preview`; the preview tool returns a local HTML path. Empty the App Bank and resolve validation problems.
4. Before applying, **unhide Home Screen pages on the device and recreate desired gaps in the editor**. Review the comparison and use the write controls below.

Example requests: “Sort the loose apps by color, keeping page 1”; “Group these apps into a Travel folder”; “Compare the draft with the current layout.” See [layout modes](COMPATIBILITY.md) for iOS 16 placement rules and iPad capacities.

<a id="data-and-write-controls"></a>

## Data and write controls

### What reaches the client

Tool results can contain device name, model and software information; app names and bundle IDs; folder names, widget sizes and layout positions; icon colors and requested icon images; layout differences and validation results; backup names and local preview paths. `read_phone` excludes `UniqueDeviceID` from its device information result. Usage tools can return per-app usage estimates, background-app classifications and page-swipe estimates.

These results pass to the MCP client. Its selected model, provider and privacy settings determine whether they are processed locally or sent to a service. Check those settings before reading a personal layout or usage history. Drafts, backups, icon/category caches and previews are stored under `~/Library/Application Support/homescreen-mcp`, or the directory set by `HOMESCREEN_DATA`.

**Category lookup:** the editor asks in a dialog before sending app bundle IDs to Apple’s public lookup API. The MCP tool’s instructions ask the assistant to obtain user agreement; confirmation behavior follows the client’s tool flow. MCP does not present the editor’s dialog. Results are cached locally.

**Usage sources:** device memory reports provide approximate usage rankings. The optional Mac source reads synced Biome `App.InFocus` records and needs Screen Time **Share Across Devices** and **Full Disk Access** for the app or terminal. It selects and combines records by app overlap, so rankings may reflect multiple synced devices. See [data sources and estimates](HOW-IT-WORKS.md#data-and-privacy).

### Enabling writes

| Setting | Effect |
| --- | --- |
| `HOMESCREEN_READ_ONLY=1` | Reads and draft edits work; device Apply and Restore are blocked in app, CLI and MCP. |
| `HOMESCREEN_READ_ONLY=0` or unset | App/CLI device writes are available after their review or confirmation flow. |
| `HOMESCREEN_ALLOW_APPLY=1` | Enables MCP Apply and Restore, provided read-only mode is also off. |

To keep assistant writes off while applying a reviewed draft yourself, start an app/CLI session without read-only mode, inspect the draft, then use **Review & Apply** or:

```sh
home-screen-organizer diff
home-screen-organizer apply <apply_token>
```

The CLI asks you to type `yes`. Use the token from the latest `diff`. For assistant writes, configure **both** `HOMESCREEN_READ_ONLY=0` and `HOMESCREEN_ALLOW_APPLY=1`, then restart the server. Its instructions require an explicit user request; the environment gates are enforced by the server and do not add a separate confirmation dialog. Apply checks the token, validates the draft, refreshes device identity/layout, saves a backup and verifies readback. Restore uses matching backup provenance. After an interrupted or mismatching write, read the device again.

Since 1.2.1, pinned backups require saved grid dimensions for Restore. Trusted unpinned legacy backups remain supported. Restore maps saved pin coordinates and requires every item footprint to fit the loaded grid.

## Distribution and discovery

GitHub Releases host the DMGs, ZIPs, dual-architecture MCPB, `server.json` and shared checksums. Registry publication uses the name **`io.github.worldsavepoint/home-screen-organizer`**, with metadata pointing clients to the exact versioned MCPB download and its SHA256.

The release workflow publishes Registry metadata after the GitHub release succeeds, using GitHub OIDC. See [release publishing](../macapp/BUILDING.md#automatic-releases), the [Registry overview](https://modelcontextprotocol.io/registry/about) and [GitHub Actions publishing guide](https://modelcontextprotocol.io/registry/github-actions).

Registry publication awaits the updated default branch and live release. Glama and awesome-mcp-servers submissions are planned after those prerequisites; they are separate community discovery channels.
