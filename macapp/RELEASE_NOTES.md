Screen Organizer 1.3.0 — for iOS

Arrange iPhone and iPad Home Screen apps, folders, widgets and the App Library from your Mac.

- Shared language support for English, Simplified Chinese, Traditional Chinese, Spanish, German, French, Russian and Portuguese. Display names are Screen Organizer, 屏幕整理 and 螢幕整理.
- Every app launch presents the agreement. Read to the end and select agreement to continue; declining exits.
- MCP Setup registers and updates Codex and Claude Code and exports settings for any stdio client. New registrations default to read-only. Configuration, startup checks and client-reported connection states are distinct.
- Advanced MCP exports follow the selected access mode. Device Apply and Restore require the existing write controls.
- Native Apple silicon and Intel apps require macOS 14+. The ad-hoc signed app includes its Python engine, dependencies and native language resources.
- iOS 16+ layout support, per-device drafts and backups, layout comparison, local editor, CLI and MCP server remain available.

Choose `macos-arm64` for Apple silicon or `macos-x86_64` for Intel. Open the matching DMG and drag **Screen Organizer.app** into Applications, or extract the ZIP. Use **System Settings → Privacy & Security → Open Anyway** if macOS requests approval. After an upgrade or move, update the engine path in **MCP Setup** or your client settings. Commands, archive basenames, Registry namespace and saved-data locations stay compatible.

For an MCPB-compatible client, install `home-screen-organizer-1.3.0-macos.mcpb`. It includes both native engines and defaults to `HOMESCREEN_READ_ONLY=1`. To enable MCP Apply and Restore, set both `HOMESCREEN_READ_ONLY=0` and `HOMESCREEN_ALLOW_APPLY=1`, then reconnect. Before Apply, review `diff` and use its latest `apply_token`.

`SHA256SUMS.txt` covers both DMGs, both ZIPs, the MCPB, the Corresponding Source archive and `server.json`. Save it beside your download and run `shasum -a 256 --check --ignore-missing SHA256SUMS.txt`. Native CI verifies both architectures, localized app resources, signatures, checksums and discovery-only MCP startup before publication. Registry metadata retains `io.github.worldsavepoint/home-screen-organizer`.

The bundled Python engine is distributed under GPL version 3 or later. The per-component notices are in the app and MCPB. Download [Corresponding Source and rebuild instructions]({{SOURCE_URL}}) for this release.

Original project by [Jonathan Kats](https://github.com/kats1123/homescreen-mcp), distributed under the [MIT License](https://github.com/worldsavepoint/ios-homescreen-organizer/blob/main/LICENSE). Screen Organizer is not affiliated with, endorsed by, or sponsored by Apple Inc. IOS is a trademark or registered trademark of Cisco in the U.S. and other countries and is used by Apple under license.
