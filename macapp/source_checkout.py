"""Export the exact tracked project inputs used for an official native build."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
from source_inputs import relative_name


def git_environment():
    return {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}


def tracked_blobs(root, commit):
    yield from _tracked_blobs(root, commit)


def tracked_blob(root, commit, name):
    relative_name(name)
    entries = _tracked_blobs(root, commit, name)
    try:
        _, _, data = next(entries)
        next(entries, None)
        return data
    finally:
        entries.close()


def _tracked_blobs(root, commit, name=None):
    error = "Could not verify the requested Git blob"
    paths = [] if name is None else ["--", name]
    records = subprocess.check_output(["git", "-C", root, "ls-tree", "-rzl", "--full-tree", commit, *paths],
                                      env=git_environment()).split(b"\0")
    if name is not None and (len(records) != 2 or records[0].partition(b"\t")[2] != name.encode()):
        raise ValueError(error)
    child = subprocess.Popen(["git", "-C", root, "cat-file", "--batch"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git_environment())
    try:
        for row in records:
            if not row:
                continue
            header, raw_name = row.split(b"\t", 1)
            mode, kind, object_id, raw_size = header.split()
            name = relative_name(raw_name.decode())
            if kind != b"blob" or mode not in (b"100644", b"100755") or not raw_size.isdigit():
                raise ValueError("Unsupported tracked release member type")
            size = int(raw_size)
            child.stdin.write(object_id + b"\n")
            child.stdin.flush()
            if child.stdout.readline(256) != object_id + b" blob " + str(size).encode() + b"\n":
                raise ValueError(error)
            data = child.stdout.read(size)
            digest = hashlib.new("sha1" if len(object_id) == 40 else "sha256")
            digest.update(b"blob " + str(size).encode() + b"\0")
            digest.update(data)
            if len(data) != size or child.stdout.read(1) != b"\n" or digest.hexdigest().encode() != object_id:
                raise ValueError(error)
            yield name, int(mode, 8) & 0o777, data
        child.stdin.close()
        if child.stdout.read(1) or child.wait() != 0:
            raise ValueError(error)
    except (BrokenPipeError, OSError) as exc:
        raise ValueError(error) from exc
    finally:
        if child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
        for stream in (child.stdin, child.stdout):
            try:
                stream.close()
            except OSError:
                pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def export_project(root, target):
    commit = subprocess.check_output(["git", "-C", root, "rev-parse", "--verify", "HEAD"], text=True, env=git_environment()).strip()
    if subprocess.check_output(["git", "-C", root, "status", "--porcelain", "--untracked-files=all"], env=git_environment()):
        raise ValueError("Use a clean release checkout or a local source rebuild")
    target.mkdir()
    for name, mode, data in tracked_blobs(root, commit):
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
        path.chmod(mode)
    return commit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        print(export_project(args.root, args.destination))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")
