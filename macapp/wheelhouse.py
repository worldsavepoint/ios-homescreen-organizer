"""Stage only authenticated release wheels from an existing wheelhouse."""
import argparse
from pathlib import Path
import re
import shutil

from source_inputs import relative_name, sha256


def entries(lock):
    return entries_text(lock.read_text())


def entries_text(text):
    for filename, requirement, checksum in records_text(text):
        yield filename, checksum


def records(lock):
    return records_text(lock.read_text())


def records_text(text):
    filename = None
    count = 0
    for line in text.splitlines():
        if line.startswith("# ") and line.endswith(".whl"):
            filename = relative_name(line[2:])
        elif line.strip() and not line.startswith("#"):
            match = re.fullmatch(r"(.+) --hash=sha256:([0-9a-f]{64})", line)
            if not filename or not match or "/" in filename:
                raise ValueError("Invalid release wheel lock")
            yield filename, match[1], match[2]
            count += 1
            filename = None
    if not count or filename:
        raise ValueError("Incomplete release wheel lock")


def stage(lock, folder, destination):
    for requirement, checksum in entries(lock):
        path = folder / requirement
        if not path.is_file() or sha256(path) != checksum:
            raise ValueError(f"Missing authenticated wheel: {requirement}")
        shutil.copyfile(path, destination / requirement)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    parser.add_argument("folder", nargs="?", type=Path)
    parser.add_argument("destination", nargs="?", type=Path)
    parser.add_argument("--install-lock", type=Path)
    args = parser.parse_args()
    if args.install_lock:
        lines = []
        for wheel, checksum in entries(args.lock):
            name, version = wheel.split("-")[:2]
            lines.append(f"# {wheel}\n{name}=={version} --hash=sha256:{checksum}\n")
        args.install_lock.write_text("".join(lines))
    else:
        stage(args.lock, args.folder, args.destination)
