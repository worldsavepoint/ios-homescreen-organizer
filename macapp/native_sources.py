"""Select the native release interpreter and preserve verified vendor grants."""
import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256 as digest
from pathlib import Path
import platform
import sys
import tarfile
import tempfile
import zipfile

from source_inputs import cache_directory, fetch, read_locks, relative_name, sha256


def selected_assets(lock, arch, owners):
    included = set(owners) | {"cpython"}
    return [item for item in lock["assets"] if arch in item["architectures"] and included.intersection(item["owners"])]


def interpreter(arch, target, cache):
    _, native = read_locks()
    runtime = native["runtime"][arch]
    entry = next(item for item in native["assets"] if item["id"] == runtime["archive"])
    library = "python/lib/libpython3.12.dylib"
    if target.exists():
        raise ValueError("Choose an empty release interpreter destination")
    source = fetch(entry, cache)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        folder = Path(temp)
        with tarfile.open(source) as archive:
            members = archive.getmembers()
            if (any(not item.name.startswith("python/") for item in members if item.name != "python")
                    or len({item.name for item in members}) != len(members)):
                raise ValueError("Invalid Python distributor archive")
            archive.extractall(folder, members=members, filter="data")
        if sha256(folder / library) != runtime["libpython"]:
            raise ValueError("Different Python distributor library")
        (folder / "python").rename(target)
    return target / "bin/python3.12"


def verify_provider_files(archive_path, files):
    found = set()
    with tarfile.open(archive_path, "r|gz") as archive:
        for member in archive:
            name = member.name.removeprefix("python/")
            if name not in files:
                continue
            if (name in found or not member.isfile()
                    or hashlib.file_digest(archive.extractfile(member), "sha256").hexdigest() != files[name]):
                raise ValueError("Collected Python runtime files differ from the release provider")
            found.add(name)
    if found != files.keys():
        raise ValueError("Collected Python runtime files missing from the release provider")


def check_runtime(arch):
    _, native = read_locks()
    runtime = native["runtime"][arch]
    if (platform.machine() != arch or platform.python_version() != runtime["version"]
            or sha256(Path(sys.base_prefix) / "lib/libpython3.12.dylib") != runtime["libpython"]):
        raise ValueError("Build the release with its pinned native Python distribution")
    return {"archive": runtime["archive"], "libpython": runtime["libpython"], "version": runtime["version"]}


def add_native_notices(lock, arch, owners, destination, cache):
    """Open each authenticated vendor archive once and copy its selected grants."""
    def collect(item):
        notices = item.get("notices", {})
        if not notices:
            return
        path = fetch(item, cache)
        def write(name, data):
            if digest(data).hexdigest() != notices[name]:
                raise ValueError("Different native component notice")
            output = destination / "native" / relative_name(item["id"]) / relative_name(name)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)

        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for name in notices:
                    if archive.getinfo(name).file_size > 32 * 1024**2:
                        raise ValueError("Invalid native component notice")
                    write(name, archive.read(name))
            return
        if path.name.endswith(".tar.zst"):
            # The pinned build environment includes backports.zstd on macOS and Python 3.12.
            from backports import zstd
            stream = zstd.open(path)
            archive = tarfile.open(fileobj=stream, mode="r|")
        else:
            stream = path.open("rb")
            archive = tarfile.open(fileobj=stream, mode="r|*")
        found = set()
        with stream, archive:
            for member in archive:
                if member.name not in notices:
                    continue
                if not member.isfile() or member.size > 32 * 1024**2 or member.name in found:
                    raise ValueError("Invalid native component notice")
                data = archive.extractfile(member).read()
                write(member.name, data)
                found.add(member.name)
                if found == notices.keys():
                    break
        if found != notices.keys():
            raise ValueError("Missing native component notice")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(collect, selected_assets(lock, arch, owners)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=("arm64", "x86_64"), required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(interpreter(args.arch, args.destination, cache_directory()))
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"{error}\n")
