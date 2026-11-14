# How Home Screen Organizer works

[README](../README.md) · [中文 README](../README.zh-CN.md) · [MCP setup](MCP.md) · [Compatibility](COMPATIBILITY.md)

The Mac app, browser editor, CLI and MCP server share a Python layout engine. [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) communicates with a trusted device over USB: `com.apple.springboardservices` supplies layouts and icons, and `installation_proxy` supplies installed-app information. Home Screen Organizer uses private device interfaces for layout operations.

## Local editor and storage

The editor listens on `127.0.0.1` and uses authenticated local sessions. The native launcher starts the bundled engine, checks backend health and validates editor URLs and requests. See the [local editor API reference](LOCAL-EDITOR.md) for session behavior and API routes.

Drafts, backups, icon caches, category caches and generated previews live under `~/Library/Application Support/homescreen-mcp`. Set `HOMESCREEN_DATA` to use another directory. Drafts and backups retain device provenance so that layouts can be associated with the intended device. The app can switch devices; CLI and MCP sessions use the source selected by their environment.

## App Library and widget research

**Jonathan Kats’s upstream research** established the SpringBoard icon-state import behavior used here. The original investigation, editor and Mac app are in [kats1123/homescreen-mcp](https://github.com/kats1123/homescreen-mcp); the simulator research harness is in [`tools/sbhelper/`](../tools/sbhelper/README.md).

1. USB `getIconState` returns icon order as `[dock, page 1, page 2, …]`.
2. `setIconState` reaches `-[SBIconController importIconState:]`. `+[SBIconStateArchiver rootArchiveFromITunesRepresentation:]` converts that list into `buttonBar` and `iconLists`.
3. Omitting an app from the list can cause it to reappear on a Home Screen page. The dictionary import format uses `ignored` to retain App Library placement.

Home Screen Organizer sends the dictionary form: `buttonBar`, `iconLists` and `ignored`. App Library contents are inferred from installed apps, including offloaded apps, minus apps on the Home Screen and hidden/system-only apps. App and folder data and unknown fields are retained as items move.

**Widgets:** configuration is associated with an element ID exported as `userSelectedElementIdentifier`. The engine sends it back as `uniqueIdentifier` and remembers IDs across reads. Readback compares exported widget settings and element identifiers.

**Web clips:** the engine remembers clips observed in each device’s layout reads and includes off-screen clips in `ignored`. If a new clip appears after a write, the result names it under `hidden_webclips`. Move the clip to the App Library and apply again.

The import preserves existing device values by omitting Today View and page-ID keys where appropriate. Writes with editor-created gaps also carry the page IDs needed for gap metadata.

<a id="layout-writes"></a>

## Layout writes

**Before applying:** unhide Home Screen pages on the device by long-pressing the Home Screen and tapping the page dots. Recreate desired gaps in the editor and empty the App Bank. Keep an archived device backup as well as the layout backups created by this tool.

### Gaps and placement

iOS stores pinned empty cells in `listMetadata` → `fixedLocations`, keyed by page ID. Dictionary-format reads expose that position data; ordinary USB list reads use the engine's remembered pins. Create the desired spacing by dropping an app on its target empty position in the editor. The engine remembers those pins per device and sends them with the corresponding page IDs.

Pins and remembered gaps retain page ownership, including separate copies of the same app on different pages. Remembered coordinates retain their saved grid; a later Read maps their row and column to the current grid and clears a page's remembered gaps if they no longer fit. Older gap files migrate keys that identify a single item.

On iOS 16, use sequential app order and keep folders after widgets originally read at a page’s start. New fixed positions are rejected with placement guidance. On iOS 17, 18, 26 and 27, the engine uses fixed-position editing. See [version-family layout modes](COMPATIBILITY.md).

### Validation and recovery

Validation checks page, Dock and folder capacities; item duplication or loss; nested folders or widgets inside folders; and an empty App Bank. A standard iPhone 4×6 page has 24 slots; small, medium and large widgets occupy 4, 8 and 16 slots; the Dock holds 4 apps; folders hold 9 items per page. iPad capacities follow the device’s icon metrics.

Apply requires the token from the latest layout comparison. The session refreshes device identity and layout, saves a backup with provenance, writes the draft and reads back the result. Restore checks that backup provenance matches the selected device and also saves a backup before writing. An interrupted or mismatching operation requires a fresh **Read** before continuing.

Read checks that both device-info observations agree on the effective page, Dock and folder capacities. Apply and Restore check those capacities against the loaded draft before backup/write and after readback. A changed frame requires Read; a change detected after writing leaves the result unknown and pending. Unavailable metrics retain the standard iPhone fallback.

Drafts and undo history retain local node identities separately from device identifiers, so rearranging or renaming folders preserves pin ownership. Older drafts with ambiguous pin identities require Read; their saved draft remains intact until that Read succeeds. Unknown write outcomes preserve the reviewed draft and history.

Since 1.2.1, pinned backups require saved grid dimensions for Restore. Trusted unpinned legacy backups remain supported. Restore maps saved pin coordinates to the loaded grid and checks every item's footprint before writing.

`HOMESCREEN_READ_ONLY=1` blocks Apply and Restore across the app, CLI and MCP while allowing local draft editing. MCP writes additionally require `HOMESCREEN_ALLOW_APPLY=1`; see [MCP write controls](MCP.md#data-and-write-controls).

System apps that iOS does not display on the Home Screen are listed in `NEVER_ON_HOME_SCREEN` in [`homescreen/device.py`](../homescreen/device.py). Unexpected system apps reported under `added_by_ios` may require updating that list.

<a id="data-and-privacy"></a>

## Data and privacy

### Category lookup

**Group by App Store category** sends app bundle IDs to Apple’s public lookup API and caches the returned categories locally. The editor asks in a dialog before lookup. The MCP tool describes this transfer and asks the assistant to obtain user agreement through its client flow; it does not show the editor dialog.

### Usage sorting

- **Device memory reports:** periodic iOS reports provide approximate app usage rankings without Screen Time setup. Frequently resident or continuously running apps receive less weight and are identified as background apps.
- **Mac Screen Time source:** enable **Settings → Screen Time → Share Across Devices** on the device and Mac using the same Apple Account. Grant **Full Disk Access** to Home Screen Organizer, or the terminal running the source installation, in **System Settings → Privacy & Security**.

The Mac source reads synced Biome `App.InFocus` records under `~/Library/Biome/streams/restricted/App.InFocus/remote`. It counts bundle-ID occurrences and estimates opens from paired focus records rather than fully decoding the records. It selects and combines synced records by overlap with installed apps, so results can include more than one synced device. These estimates support relative rankings rather than exact usage measurement.

The page-swipe estimate counts page *N* as *N−1* swipes, adds folder-page cost and treats the Dock as free. Usage optimization brings frequently used apps and folders forward while retaining widget pages, the Dock and App Library placement.

### MCP client processing

A local server does not determine where an assistant processes its inputs. Requested tool results, including layout details, icon images or usage estimates, pass to the MCP client and may be sent to its configured model provider. See [the returned data and client controls](MCP.md#data-and-write-controls) before connecting an assistant.

## Development sources

After [source installation](../macapp/BUILDING.md#source-installation), explore the editor with a synthetic fixture:

```sh
.venv/bin/python tests/fake_phone.py fake.plist --dict
HOMESCREEN_FIXTURE=fake.plist .venv/bin/home-screen-organizer ui
```

For simulator development with an already booted runtime:

```sh
HOMESCREEN_SIM=booted .venv/bin/home-screen-organizer ui
```

The simulator adapter uses bundled icon metadata and standalone UIKit rendering for asset-catalog and dynamic icons. Source selection follows `HOMESCREEN_FIXTURE`, then `HOMESCREEN_SIM`, then USB. See [compatibility checks](COMPATIBILITY.md) and [native build details](../macapp/BUILDING.md).
