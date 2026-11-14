# Rebuilding Screen Organizer

The combined Python engine is distributed under GPL version 3 or later. The original Homescreen source and its MIT copyright notice are in `project/LICENSE`. Each upstream component keeps its own license.

Use the source archive from the same release as your app or MCPB. Verify the archive against that release’s `SHA256SUMS.txt`, then extract it into a new directory. The commands below run from that directory with a Python 3.12 bootstrap interpreter. `MANIFEST.json` identifies the release commit, the native ZIP hashes and the original source archives. `inventories/arm64.json` and `inventories/x86_64.json` map the frozen modules, native files and notices to those inputs.

## Edit a library

Use a native Mac of the target architecture, macOS 14 or later, the Xcode Command Line Tools and Python 3.12. Intel builds of cryptography also require Perl, Rust 1.95.0 and the matching Intel OpenSSL build. The source helper checks each selected input before opening it.

```sh
ARCH=$(uname -m)
python3.12 project/macapp/rebuild_sources.py component pylzss --arch "$ARCH" --output editable-pylzss
```

Edit `editable-pylzss/src/pylzss.c`. To build a matching native extension, use the release interpreter, install the locked build tools, and build a wheel from the edited source directory.

```sh
export HOMESCREEN_SOURCE_CACHE="$PWD/sources"
mkdir -p release-tools local-wheels
python3.12 project/macapp/native_sources.py --arch "$ARCH" --destination "$PWD/release-tools/python"
release-tools/python/bin/python3.12 -m venv release-tools/venv
PYTHON="$PWD/release-tools/venv/bin/python"
LOCK="$PWD/project/requirements-build.lock"
if [ "$ARCH" = x86_64 ]; then
  bash project/macapp/prepare_intel.sh "$PYTHON" "$PWD/project/build/dependencies/x86_64"
  LOCK="$PWD/project/build/dependencies/x86_64/requirements-build.lock"
  "$PYTHON" -m pip install --require-hashes --only-binary=:all: --find-links "$PWD/project/build/dependencies/x86_64/wheels" -r "$LOCK"
else
  "$PYTHON" -m pip install --require-hashes --only-binary=:all: -r "$LOCK"
fi
"$PYTHON" project/macapp/rebuild_sources.py wheel --arch "$ARCH" --source editable-pylzss --wheelhouse local-wheels --output local-requirements.lock
"$PYTHON" -m pip install --no-deps --force-reinstall local-wheels/pylzss-*.whl
"$PYTHON" -c 'import lzss; assert lzss.decompress(lzss.compress(b"Hello")) == b"Hello"'
```

The generated `local-requirements.lock` selects your edited wheel by its local file URI and hash, and retains the original requirements for unchanged components, including the pure-Python chardet wheel. Keep the wheel directory in place and install the lock into a fresh virtual environment with `pip install --require-hashes --only-binary=:all: --find-links local-wheels -r local-requirements.lock`. On Intel also pass the directory containing the prepared cryptography wheel. Use this environment to run the editable project, its command line tools and the PyInstaller and Swift build steps described in `project/macapp/BUILDING.md`. Refreeze can also use authenticated wheels from `local-wheels` or `PIP_FIND_LINKS`. The lock embedded in a signed local app retains filenames and hashes with download locations removed.

## Refreeze a modified Python package

For an edit to the project’s own Python, Swift or web sources, run `"$PYTHON" project/macapp/rebuild_sources.py refreeze --arch "$ARCH" --output "$PWD/modified-build"`. This also builds the original project from the unpacked source archive.

For a pure-Python component such as h11, run the `component` command with `h11`, edit its Python source, then run the same `wheel` command with that editable directory. Refreeze the engine and Swift app using the resulting local lock.

```sh
"$PYTHON" project/macapp/rebuild_sources.py refreeze --arch "$ARCH" --wheelhouse local-wheels --lock local-requirements.lock --output "$PWD/modified-build"
"$PYTHON" project/macapp/check_mcp.py -- "$PWD/modified-build/Screen Organizer.app/Contents/Resources/engine/engine" mcp
```

The local build creates a signed app in the selected directory. Its `Contents/Resources/LOCAL-BUILD.json` records the original and replacement wheel hashes, the frozen engine digest, project input hashes and modified, added or removed Swift inputs relative to the source manifest. The Swift inputs include `macapp/Shell/Package.swift` and regular files under `macapp/Shell/Sources`.

Its launcher digest uses `sha256-unsigned-macho64-linkedit-align16-v2`: copy `Contents/MacOS/HomeScreenOrganizer` to a temporary file and run `codesign --remove-signature`. Validate the thin 64-bit Mach-O layout and its referenced file extents against the actual input, with `__LINKEDIT` ending at EOF. Append zero bytes up to 16-byte file alignment, increase only the canonical `__LINKEDIT.filesize` by that padding, and set `__LINKEDIT.vmsize` to the canonical file size rounded up to the target CPU's page size (16,384 bytes for arm64; 4,096 for x86_64). Hash all resulting bytes with SHA-256. Every original byte remains in the digest except these two size fields. The helper rejects unsupported or malformed layouts and verifies this digest again after signing. The record is a signed resource and its digest excludes its own signing envelope. An official release uses the clean Git export and its recorded wheel hashes. For a development build of an edited source tree, use the local refreeze route.

The wheel helper compiles from a fresh source staging tree, preserves supplied compiler flags, maps editable, temporary and interpreter roots in C/C++ and Rust inputs, and omits Darwin linker debug-map paths. It checks the completed wheel before calculating its lock hash. Private build roots stay in the wheel directory's `.build-paths.json`; they are carried into the final signed-app privacy scan. That scan checks compiled files, nested archives and compressed Python constants before the local build reports success.

## Install a replacement into your own app

Copy the installed macOS app into your build directory. Replace the shared extension and ad-hoc sign that copy.

```sh
ditto "/Applications/Screen Organizer.app" "$PWD/Screen Organizer.app"
LIB=$("$PYTHON" -c 'import lzss; print(lzss.__file__)')
cp "$LIB" "$PWD/Screen Organizer.app/Contents/Resources/engine/_internal/"
codesign --force --deep --sign - "$PWD/Screen Organizer.app"
codesign --verify --deep --strict "$PWD/Screen Organizer.app"
"$PYTHON" project/macapp/check_mcp.py -- "$PWD/Screen Organizer.app/Contents/Resources/engine/engine" mcp
```

For the unpacked MCPB, replace the same library in `server/$ARCH/_internal/` and sign the replacement extension using `codesign --force --sign -`. The engine runs as a separate process. The launcher uses the selected architecture and its ordinary stdio entry point.

## Rebuild native inputs

Use `MANIFEST.json` to locate upstream archives by their input names. The source archive names retain their SHA-256 prefixes. To reuse embedded source downloads, set `HOMESCREEN_SOURCE_CACHE="$PWD/sources"` before invoking the build helpers. `project/macapp/sources/native.json` names the provider, native recipes, source archives and Cargo requirements for each owning component.

- CPython comes from the recorded python-build-standalone release. `pbs/recipe` contains its patches, configure/build scripts and download definitions. The two `provider-full-metadata` archives contain the original `python/PYTHON.json` target/build details. The `pbs-sources/` inputs include CPython and its statically linked libraries. Build the provider's recorded PGO and LTO target using its Darwin build scripts. The prebuilt, verified interpreter used by the release is also included.
- Pillow’s `pillow/recipe` contains its macOS wheel workflow, dependency-version file and native build patches. Use the included `pillow/multibuild` checkout and the `pillow-mirror/` dependency archives named by the recipe. The `avif/` archives supply the selected nested AOM, dav1d, libyuv and SharpYUV sources. Use the selected Darwin feature flags from the wheel recipe.
- For Rust libraries, extract the owner's Python source and run `python3.12 project/macapp/rebuild_sources.py cargo-vendor --output "$PWD/cargo-vendor" > cargo-config.toml`. Install that file as `.cargo/config.toml` in the editable workspace. Run the owner’s original maturin or Cargo recipe in locked offline mode. The registry archives and checksum data come from the original workspaces’ Cargo locks. The `wheel-recipe/` inputs contain upstream generation and wheel workflows.
- For Intel cryptography, follow `project/macapp/prepare_intel.sh` with the recorded OpenSSL source and static configure/link flags. The helper replaces Cargo SBOM local component IDs with their existing source-relative package URLs and updates every dependency reference. It preserves the graph, licenses and other SBOM fields, verifies the original wheel RECORD, and updates that RECORD before calculating the prepared wheel hash. For other native extensions follow the component's `pyproject.toml`, C/Cython build scripts and wheel recipe. The `overlays/` directories retain generated or changed editable package files from each actual wheel. Each path is relative to its installed owner.

The Swift shell and the macOS integration sources are in `project/macapp/Shell/`. Build the app from this archive on the corresponding native architecture with the `refreeze` command above. macOS frameworks and ordinary development tools are supplied by the system and its toolchain.
