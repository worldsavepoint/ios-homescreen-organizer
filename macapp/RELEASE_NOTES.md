Screen Organizer 1.3.1 — for iOS

Arrange iPhone and iPad Home Screen apps, folders, widgets and the App Library from your Mac.

- Fix the launch agreement staying locked after its full text becomes visible. The agreement checkbox becomes available when the last line is visible, including after a window resize.
- Recompute reading progress after language changes and update the reading hint when reading is complete. Continuing requires selecting the agreement checkbox.
- Eight interface languages, MCP Setup, native Apple silicon and Intel packages, and iOS 16+ layout support remain available on macOS 14+.

Choose `macos-arm64` for Apple silicon or `macos-x86_64` for Intel. Open the matching DMG and drag **Screen Organizer.app** into Applications, or extract the ZIP. Use **System Settings → Privacy & Security → Open Anyway** if macOS requests approval. After an upgrade or move, update the engine path in **MCP Setup** or your client settings. Commands, archive basenames, Registry namespace and saved-data locations stay compatible.

For an MCPB-compatible client, install `home-screen-organizer-1.3.1-macos.mcpb`. It includes both native engines and defaults to `HOMESCREEN_READ_ONLY=1`. To enable MCP Apply and Restore, set both `HOMESCREEN_READ_ONLY=0` and `HOMESCREEN_ALLOW_APPLY=1`, then reconnect. Before Apply, review `diff` and use its latest `apply_token`.

`SHA256SUMS.txt` covers both DMGs, both ZIPs, the MCPB, the Corresponding Source archive and `server.json`. Save it beside your download and run `shasum -a 256 --check --ignore-missing SHA256SUMS.txt`. Native CI verifies both architectures, localized app resources, signatures, checksums and discovery-only MCP startup before publication. Registry metadata retains `io.github.worldsavepoint/home-screen-organizer`.

The bundled Python engine is distributed under GPL version 3 or later. The per-component notices are in the app and MCPB. Download [Corresponding Source and rebuild instructions]({{SOURCE_URL}}) for this release.

Original project by [Jonathan Kats](https://github.com/kats1123/homescreen-mcp), distributed under the [MIT License](https://github.com/worldsavepoint/ios-homescreen-organizer/blob/main/LICENSE). Screen Organizer is not affiliated with, endorsed by, or sponsored by Apple Inc. IOS is a trademark or registered trademark of Cisco in the U.S. and other countries and is used by Apple under license.
