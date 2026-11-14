Arrange Home Screen apps, folders, widgets and the App Library from your Mac.

- Version 1.2.1 carries the loaded grid dimensions through fixed-position placement, Apply and Restore.
- Apply and Restore compare folder contents and exported position data. Widget pins require a known wire identifier.
- Since 1.2.1, pinned backups require saved grid dimensions for Restore. Trusted unpinned legacy backups remain supported; restored coordinates and item footprints must fit the loaded grid.
- HTML previews display draft and last-read rows, columns, fixed positions and widget spans.
- MCP discovery describes tool effects, inputs and layout workflows, including same-page pin preservation during Arrange.
- Native Intel support alongside Apple silicon, with separate DMG and ZIP downloads for each architecture.
- A standalone MCPB contains both native engines and dependencies for MCP clients, with read-only access configured by default.
- Layout support for iOS 16, 17, 18, 26 and 27; iOS 16 uses sequential icon placement.
- Per-device drafts and backups, readback comparison, local editor, CLI and MCP server.
- macOS 14 or later; ad-hoc signed app with the Python engine and dependencies included.

Choose your download in Apple menu → About This Mac: an Apple M-series chip uses `macos-arm64`; an Intel processor uses `macos-x86_64`. Open the matching DMG and drag Home Screen Organizer into Applications, or extract the matching ZIP. Open System Settings → Privacy & Security → Open Anyway when macOS requests approval.

For an MCPB-compatible client, install `home-screen-organizer-1.2.1-macos.mcpb`. Its launcher selects the native engine and runs without a separate Python or Mac app installation. The MCPB configures `HOMESCREEN_READ_ONLY=1`. To enable MCP Apply and Restore, set both `HOMESCREEN_READ_ONLY=0` and `HOMESCREEN_ALLOW_APPLY=1` in the client, then reconnect to restart the server. Before Apply, review `diff` and use its latest `apply_token`.

`server.json` describes the versioned MCPB package for the official MCP Registry. The release workflow publishes this metadata after GitHub release publication.

`SHA256SUMS.txt` contains hashes for both DMGs, both ZIPs, the MCPB and `server.json`. Save it in the same directory as your chosen DMG or ZIP, then run `shasum -a 256 --check --ignore-missing SHA256SUMS.txt`.

Native Apple silicon and Intel CI builds validate the functional modules, packaged engine startup, MCP initialization and tools, Mach-O architectures, signatures and checksums. Both architectures then initialize and discover tools from the assembled standalone MCPB before publication. Each new version is released after successful builds of both architectures, with a matching version tag at the built commit.

Original project by [Jonathan Kats](https://github.com/kats1123/homescreen-mcp), distributed under the MIT License.
