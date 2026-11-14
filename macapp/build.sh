#!/bin/zsh
# Build Screen Organizer.app and home-screen-organizer-<version>.dmg into build/.
#   macapp/build.sh            full build
# Requires Python 3.12 and Xcode command line tools. See macapp/BUILDING.md.
set -euo pipefail

CHECKOUT=${0:A:h:h}
ROOT="$CHECKOUT"
ARCH=${HOMESCREEN_BUILD_ARCH:-$(uname -m)}
case "$ARCH" in
  arm64|x86_64) ;;
  *) echo "Select arm64 or x86_64 for the build architecture." >&2; exit 1 ;;
esac
if [[ "$(uname -m)" != "$ARCH" ]]; then
  echo "Use a native $ARCH build host." >&2
  exit 1
fi
export HOMESCREEN_BUILD_ARCH="$ARCH"
export MACOSX_DEPLOYMENT_TARGET=14.0
BUILD=$ROOT/build/$ARCH
unset HOMESCREEN_LOCAL_SOURCE
LOCAL_LOCK=
LOCAL_LINKS=()
if [[ $# -gt 0 ]]; then
  if [[ $# -ne 5 || "$1" != --local-build ]]; then
    echo "Use rebuild_sources.py refreeze for an edited local build." >&2
    exit 2
  fi
  BUILD=$2
  LOCAL_LOCK=$3
  LOCAL_LINKS=(--find-links "$4")
  export HOMESCREEN_LOCAL_SOURCE=$5
fi
BOOTSTRAP=${HOMESCREEN_BUILD_PYTHON:-python3.12}
VENV=$(mktemp -d "${TMPDIR:-/tmp}/homescreen-build.XXXXXXXX")
trap 'rm -rf "$VENV"' EXIT
APP="$BUILD/Screen Organizer.app"
PYTHON="$BOOTSTRAP"
if [[ -z "${HOMESCREEN_BUILD_PYTHON:-}" ]]; then
  PYTHON=$("$BOOTSTRAP" "$ROOT/macapp/native_sources.py" --arch "$ARCH" --destination "$VENV/runtime")
fi
WHEELS="$VENV/wheels"
SOURCE_STAGE="$VENV/source-inputs"
mkdir -p "$WHEELS" "$SOURCE_STAGE"
export HOMESCREEN_SOURCE_COMMIT=
if [[ -z "$LOCAL_LOCK" ]]; then
  HOMESCREEN_SOURCE_COMMIT=$("$BOOTSTRAP" "$CHECKOUT/macapp/source_checkout.py" "$CHECKOUT" "$VENV/project")
  ROOT="$VENV/project"
fi
export HOMESCREEN_FREEZE_WHEELS="$WHEELS" HOMESCREEN_FREEZE_SOURCES="$SOURCE_STAGE"
export PYINSTALLER_CONFIG_DIR="$VENV/pyinstaller-cache"

LOCK="$ROOT/requirements-build.lock"
WHEEL_LINKS=()
BUILD_PATH_ARGS=()
if [[ "$ARCH" == x86_64 ]]; then
  DEPENDENCIES="$CHECKOUT/build/dependencies/x86_64"
  if ! "$PYTHON" "$ROOT/macapp/build_paths.py" intel-check "$DEPENDENCIES" 2>/dev/null; then
    bash "$ROOT/macapp/prepare_intel.sh" "$PYTHON" "$DEPENDENCIES"
  fi
  BUILD_PATH_ARGS=(--intel "$DEPENDENCIES")
  LOCK="$DEPENDENCIES/requirements-build.lock"
  WHEEL_LINKS=(--find-links "$DEPENDENCIES/wheels")
fi
"$PYTHON" -m venv "$VENV"
if [[ -n "$LOCAL_LOCK" ]]; then
  LOCK="$LOCAL_LOCK"
  "$VENV/bin/python" -m pip download --require-hashes --only-binary=:all: --no-deps "${WHEEL_LINKS[@]}" "${LOCAL_LINKS[@]}" --dest "$WHEELS" -r "$LOCK"
elif [[ -n "${HOMESCREEN_WHEELHOUSE:-}" ]]; then
  "$VENV/bin/python" "$ROOT/macapp/wheelhouse.py" "$LOCK" "$HOMESCREEN_WHEELHOUSE" "$WHEELS"
else
  "$VENV/bin/python" -m pip download --require-hashes --only-binary=:all: "${WHEEL_LINKS[@]}" --dest "$WHEELS" -r "$LOCK"
fi
"$VENV/bin/python" "$ROOT/macapp/wheelhouse.py" --install-lock "$VENV/requirements.lock" "$LOCK"
# The staged directory contains the only eligible bytes for the same name and version.
env -u PIP_FIND_LINKS PIP_CONFIG_FILE=/dev/null "$VENV/bin/python" -m pip install --require-hashes --only-binary=:all: --no-index --find-links "$WHEELS" -r "$VENV/requirements.lock"
"$VENV/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Build lock requires Python 3.12"'
"$VENV/bin/python" -m pip install --no-deps --no-build-isolation "$ROOT"
"$VENV/bin/python" -m pip check
"$VENV/bin/python" -c 'import platform, sys; assert platform.machine() == sys.argv[1], "Use a matching Python architecture"' "$ARCH"
VERSION=$("$VENV/bin/python" "$ROOT/macapp/release.py" version)
BUNDLE_VERSION=${VERSION%%-*}

echo "• engine"
(cd "$ROOT/macapp" && "$VENV/bin/pyinstaller" --clean --noconfirm --log-level ERROR \
    --distpath "$BUILD/dist" --workpath "$BUILD/work" engine.spec)

echo "• shell"
(cd "$ROOT/macapp/Shell" && swift build -c release --arch "$ARCH")
SHELL_BIN=$(cd "$ROOT/macapp/Shell" && swift build -c release --arch "$ARCH" --show-bin-path)/HomeScreenOrganizer

echo "• app bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$SHELL_BIN" "$APP/Contents/MacOS/HomeScreenOrganizer"
/usr/bin/strip -S "$APP/Contents/MacOS/HomeScreenOrganizer"
cp -R "$BUILD/dist/engine" "$APP/Contents/Resources/engine"
"$VENV/bin/python" -c 'import importlib.metadata as m; print("\n".join(sorted(d.metadata["Name"] + "==" + d.version for d in m.distributions())))' \
  > "$APP/Contents/Resources/dependencies.txt"
if [[ "$ARCH" == x86_64 ]]; then
  cp "$ROOT/requirements-cryptography-x86_64.lock" "$APP/Contents/Resources/requirements-cryptography-x86_64.lock"
  "$VENV/bin/python" "$ROOT/macapp/source_archive.py" intel-notice --inputs "$SOURCE_STAGE" --output "$APP/Contents/Resources/OPENSSL-LICENSE.txt"
fi
cp "$ROOT/LICENSE" "$APP/Contents/Resources/LICENSE"
cp "$SOURCE_STAGE/THIRD-PARTY-NOTICES.txt" "$APP/Contents/Resources/"
cp -R "$SOURCE_STAGE/ThirdPartyLicenses" "$APP/Contents/Resources/"
if [[ -n "$LOCAL_LOCK" ]]; then
  cp "$VENV/requirements.lock" "$APP/Contents/Resources/requirements-build.lock"
  cp "$SOURCE_STAGE/LOCAL-BUILD.json" "$APP/Contents/Resources/"
else
  cp "$LOCK" "$APP/Contents/Resources/requirements-build.lock"
  cp "$SOURCE_STAGE/inventory.json" "$APP/Contents/Resources/SourceInventory.json"
fi
"$VENV/bin/python" "$ROOT/macapp/make_icon.py" "$BUILD/AppIcon.iconset"
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Screen Organizer</string>
  <key>CFBundleDisplayName</key><string>Screen Organizer</string>
  <key>CFBundleIdentifier</key><string>io.github.worldsavepoint.ios-homescreen-organizer</string>
  <key>CFBundleExecutable</key><string>HomeScreenOrganizer</string>
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$BUNDLE_VERSION</string>
  <key>CFBundleVersion</key><string>$BUNDLE_VERSION</string>
  <key>HomeScreenOrganizerReleaseVersion</key><string>$VERSION</string>
  <key>HomeScreenOrganizerArchitecture</key><string>$ARCH</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>Copyright © 2026 Jonathan Kats. Original Homescreen source: MIT. Bundled engine: GPL-3.0-or-later.</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

"$VENV/bin/python" "$ROOT/tools/check_translations.py"
"$VENV/bin/python" "$ROOT/macapp/release.py" localize --app "$APP"
"$VENV/bin/python" "$ROOT/macapp/release.py" check-localization --app "$APP"
if [[ -n "$LOCAL_LOCK" ]]; then
  "$VENV/bin/python" "$ROOT/macapp/frozen_sources.py" local-record --project "$ROOT" --settings "$HOMESCREEN_LOCAL_SOURCE" --app "$APP"
fi

echo "• ad-hoc signing"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

if [[ -n "$LOCAL_LOCK" ]]; then
  "$VENV/bin/python" "$ROOT/macapp/frozen_sources.py" verify-local-record --app "$APP"
  "$VENV/bin/python" "$ROOT/macapp/check_package_paths.py" --local-build "$HOMESCREEN_LOCAL_SOURCE" "${BUILD_PATH_ARGS[@]}" "$APP" "$CHECKOUT" "$ROOT" "$BUILD" "$VENV"
  echo "Built locally modified $APP"
  exit 0
fi

echo "• dmg"
DMG=$BUILD/home-screen-organizer-$VERSION.dmg
STAGE=$BUILD/dmg
rm -rf "$STAGE" "$DMG" && mkdir -p "$STAGE"
"$VENV/bin/python" "$ROOT/macapp/release.py" clean-copy --app "$APP" --destination "$STAGE/Screen Organizer.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "Screen Organizer" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
echo "built $APP ($(du -sh "$APP" | cut -f1)) and $DMG ($(du -sh "$DMG" | cut -f1))"

echo "• release archives"
"$VENV/bin/python" "$ROOT/macapp/build_paths.py" app-record "$BUILD" "$CHECKOUT" "$ROOT" "$BUILD" "$VENV" "${BUILD_PATH_ARGS[@]}"
"$VENV/bin/python" "$ROOT/macapp/release.py" package --build-dir "$BUILD" --output-dir "$CHECKOUT/build/release/$ARCH" --arch "$ARCH"
"$VENV/bin/python" "$ROOT/macapp/source_archive.py" native --inputs "$SOURCE_STAGE" --archive "$CHECKOUT/build/release/$ARCH/home-screen-organizer-$VERSION-macos-$ARCH.zip" --output "$BUILD/source-inputs.zip"
