#!/bin/bash
# Prepare CPython 3.12 Intel dependencies: prepare_intel.sh PYTHON OUTPUT_DIR
# Requires native Intel macOS, Xcode command line tools, Perl, curl and rustup.
set -euo pipefail

if [[ "$#" != 2 ]]; then
    echo "Usage: $0 PYTHON OUTPUT_DIR" >&2
    exit 2
fi
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != x86_64 || \
      "$(sysctl -in sysctl.proc_translated 2>/dev/null || true)" == 1 ]]; then
    echo "Intel dependency preparation requires native Intel macOS." >&2
    exit 1
fi

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=$1
OUTPUT=$2
"$PYTHON" -c 'import platform, sys; assert sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 12) and platform.machine() == "x86_64", "Requires native Intel CPython 3.12"'
for tool in clang make perl curl rustup lipo otool shasum; do
    command -v "$tool" >/dev/null || { echo "Missing build tool: $tool" >&2; exit 1; }
done

mkdir -p "$(dirname "$OUTPUT")"
WORK=$(mktemp -d "$(cd "$(dirname "$OUTPUT")" && pwd)/.homescreen-intel.XXXXXXXX")
mkdir "$WORK/tmp"
export TMPDIR="$WORK/tmp"
trap 'rm -rf "$WORK"' EXIT
# Release checksum: https://github.com/openssl/openssl/releases/tag/openssl-4.0.3
OPENSSL_VERSION=4.0.3
OPENSSL_SHA256=325b5c806167c13b40b1ffeadfe0248197c00eccc4cf123ec1e28d2d2fd216d9
OPENSSL_ARCHIVE="$WORK/openssl-$OPENSSL_VERSION.tar.gz"
curl --fail --location --retry 3 --output "$OPENSSL_ARCHIVE" \
    "https://github.com/openssl/openssl/releases/download/openssl-$OPENSSL_VERSION/openssl-$OPENSSL_VERSION.tar.gz"
echo "$OPENSSL_SHA256  $OPENSSL_ARCHIVE" | shasum -a 256 -c -
tar -xzf "$OPENSSL_ARCHIVE" -C "$WORK"

"$PYTHON" -m venv "$WORK/venv"
BUILD_PYTHON="$WORK/venv/bin/python"
export PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1
"$BUILD_PYTHON" -m pip install --no-deps --require-hashes --only-binary=:all: \
    -r "$ROOT/requirements-build-x86_64.lock"
rustup toolchain install 1.95.0 --profile minimal --target x86_64-apple-darwin
export RUSTUP_TOOLCHAIN=1.95.0 CARGO_BUILD_TARGET=x86_64-apple-darwin
export CARGO_HOME="$WORK/cargo" CARGO_TARGET_DIR="$WORK/target"
export PATH="$WORK/venv/bin:${CARGO_HOME}/bin:$HOME/.cargo/bin:$PATH"
export MACOSX_DEPLOYMENT_TARGET=14.0
export CFLAGS="-mmacosx-version-min=14.0" ARCHFLAGS="-arch x86_64"
# Cargo separates encoded rustc arguments with the unit separator.
export CARGO_ENCODED_RUSTFLAGS
CARGO_ENCODED_RUSTFLAGS=$(printf '%s\037%s\037%s\037%s' -C link-arg=-mmacosx-version-min=14.0 \
    "--remap-path-prefix=$WORK=/build/cryptography" "--remap-path-prefix=$(cd "$WORK" && pwd -P)=/build/cryptography")
OPENSSL_PREFIX=/usr/local
export OPENSSL_DIR="$WORK/stage$OPENSSL_PREFIX" OPENSSL_STATIC=1 OPENSSL_NO_VENDOR=1
export OPENSSL_LIB_DIR="$OPENSSL_DIR/lib" OPENSSL_INCLUDE_DIR="$OPENSSL_DIR/include"
(
    cd "$WORK/openssl-$OPENSSL_VERSION"
    perl Configure darwin64-x86_64-cc no-shared no-tests \
        --prefix="$OPENSSL_PREFIX" --libdir=lib -mmacosx-version-min=14.0
    make -j "$(sysctl -n hw.ncpu)"
    make DESTDIR="$WORK/stage" install_sw
)

# pip verifies the source hash before invoking upstream's locked maturin build.
"$BUILD_PYTHON" -m pip wheel --no-cache-dir --no-deps --no-build-isolation \
    --require-hashes --no-binary=cryptography --wheel-dir "$WORK/wheels" \
    -r "$ROOT/requirements-cryptography-x86_64.lock"
"$BUILD_PYTHON" "$ROOT/macapp/wheel_sbom.py" "$WORK/wheels"
"$BUILD_PYTHON" - "$WORK/wheels" "$OUTPUT" "$ROOT/requirements-build-x86_64.lock" \
    "$WORK/openssl-$OPENSSL_VERSION/LICENSE.txt" <<'PY'
import email
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

wheels, output, base, openssl_license = map(Path, sys.argv[1:])
candidates = list(wheels.glob("cryptography-*.whl"))
assert len(candidates) == 1, "Expected one cryptography wheel"
wheel = candidates[0]
assert wheel.name.startswith("cryptography-50.0.2-") and wheel.name.endswith("-macosx_14_0_x86_64.whl"), wheel.name
with zipfile.ZipFile(wheel) as archive, tempfile.TemporaryDirectory() as extracted:
    metadata_files = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    assert len(metadata_files) == 1, "Invalid wheel metadata"
    metadata = email.message_from_bytes(archive.read(metadata_files[0]))
    assert metadata["Name"] == "cryptography" and metadata["Version"] == "50.0.2", "Wrong wheel version"
    libraries = [name for name in archive.namelist() if name.endswith((".so", ".dylib"))]
    assert libraries, "Wheel contains no native extensions"
    for index, name in enumerate(libraries):
        library = Path(extracted) / f"{index}-{Path(name).name}"
        library.write_bytes(archive.read(name))
        architectures = subprocess.check_output(["lipo", "-archs", str(library)], text=True).strip()
        assert architectures == "x86_64", f"Unexpected native architectures: {architectures}"
        links = subprocess.check_output(["otool", "-L", str(library)], text=True)
        assert not re.search(r"lib(?:ssl|crypto)[^/\s]*\.dylib", links), "OpenSSL must be statically linked"
        commands = subprocess.check_output(["otool", "-l", str(library)], text=True)
        minimums = []
        for block in re.split(r"Load command \d+", commands):
            if re.search(r"cmd LC_(?:BUILD_VERSION|VERSION_MIN_MACOSX)\b", block):
                value = re.search(r"\b(?:minos|version)\s+(\d+)\.(\d+)", block)
                assert value, "Missing macOS deployment minimum"
                minimums.append(tuple(map(int, value.groups())))
        assert minimums and all(value == (14, 0) for value in minimums), "Expected macOS 14.0 deployment minimum"

digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
wheel_dir = output / "wheels"
wheel_dir.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(dir=wheel_dir, delete=False) as stream:
    new_wheel = Path(stream.name)
try:
    shutil.copy2(wheel, new_wheel)
    new_wheel.replace(wheel_dir / wheel.name)
finally:
    new_wheel.unlink(missing_ok=True)
for previous in wheel_dir.glob("cryptography-*.whl"):
    if previous.name != wheel.name and (previous.is_file() or previous.is_symlink()):
        previous.unlink()
shutil.copy2(openssl_license, output / "OPENSSL-LICENSE.txt")
lock = output / "requirements-build.lock"
temporary = lock.with_suffix(".tmp")
temporary.write_text(base.read_text() + f"\n# {wheel.name}\ncryptography==50.0.2 --hash=sha256:{digest}\n")
temporary.replace(lock)
print(f"Prepared Intel dependencies: {lock}")
PY

"$PYTHON" "$ROOT/macapp/build_paths.py" intel-record "$OUTPUT" "$WORK"
