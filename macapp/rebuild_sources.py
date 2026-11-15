"""Prepare editable components and local rebuild inputs from a release source tree."""
import argparse
import errno
import hashlib
import json
import os
import platform
from pathlib import Path, PurePosixPath
import shutil
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import unquote_to_bytes, urlsplit
import zipfile

from source_inputs import INVENTORY_LIMIT, canonical, relative_name, sha256


def unpack_source(archive, output):
    if output.exists():
        raise ValueError("Select a new editable source directory")
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        stage = Path(directory)
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zipped:
                size = 0
                seen = set()
                for info in zipped.infolist():
                    if ".git" in PurePosixPath(info.filename).parts:
                        continue
                    name = relative_name(info.filename.rstrip("/"))
                    size += info.file_size
                    if (name in seen or size > 2 * 1024**3 or len(seen) > 150000
                            or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFDIR, stat.S_IFREG)):
                        raise ValueError("Unsupported upstream ZIP member")
                    seen.add(name)
                    if not info.is_dir():
                        dest = stage / name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zipped.open(info) as data, dest.open("xb") as stream:
                            shutil.copyfileobj(data, stream)
                        if info.external_attr >> 16 & 0o111:
                            dest.chmod(0o755)
        else:
            with tarfile.open(archive) as tar:
                members, seen, size = [], set(), 0
                for item in tar:
                    if ".git" in PurePosixPath(item.name).parts:
                        continue
                    name = relative_name(item.name.rstrip("/"))
                    if not (item.isfile() or item.isdir() or item.issym() or item.islnk()):
                        raise ValueError("Unsupported upstream source member")
                    size += item.size
                    if name in seen or size > 2 * 1024**3 or len(seen) > 150000:
                        raise ValueError("Unexpected upstream archive bounds")
                    seen.add(name)
                    members.append(item)
                links = {item.name for item in members if item.issym() or item.islnk()}
                if any(any(str(parent) in links for parent in PurePosixPath(item.name).parents) for item in members):
                    raise ValueError("Source member descends through an archive link")
                try:
                    tar.extractall(stage, members=members, filter="data")
                except tarfile.FilterError as error:
                    raise ValueError("Unsafe upstream archive link or member") from error
        children = list(stage.iterdir())
        unpacked = children[0] if len(children) == 1 and children[0].is_dir() and not children[0].is_symlink() else stage
        root = unpacked.resolve()
        for path in unpacked.rglob("*"):
            if path.is_symlink():
                target = path.readlink()
                if target.is_absolute():
                    raise ValueError("Invalid archive link in editable source tree")
                current = path.parent.resolve(strict=True)
                try:
                    for part in target.parts:
                        current = (current / part).resolve(strict=True)
                        if not current.is_relative_to(root):
                            raise ValueError("Invalid archive link in editable source tree")
                except (OSError, RuntimeError) as error:
                    raise ValueError("Invalid archive link in editable source tree") from error
        unpacked.rename(output)


def blob(root, entry):
    path = root / relative_name(entry["path"])
    if not path.is_file() or path.is_symlink() or sha256(path) != entry["sha256"]:
        raise ValueError("Changed release source input")
    return path


def editable(root, owner, arch, output):
    manifest = json.loads((root / "MANIFEST.json").read_bytes())
    volume = json.loads((root / relative_name(manifest["architectures"][arch]["inventory"])).read_bytes())
    if owner not in volume["owners"]:
        raise ValueError("Select a component in the native source inventory")
    unpack_source(blob(root, manifest["inputs"]["python/" + owner]), output)
    for name, checksum in volume["overlays"].items():
        package, _, relative = name.partition("/")
        if package != owner or ".dist-info/" in relative:
            continue
        replacement = root / "overlays" / arch / relative_name(name)
        if replacement.is_symlink() or sha256(replacement) != checksum:
            raise ValueError("Changed component source overlay")
        # Wheels use installed paths. Locate source-layout packages by their top-level directory.
        top = relative.split("/")[0]
        prefixes = [candidate for candidate in (output, output / "src", output / "python")
                    if (candidate / top).exists()]
        if not prefixes:
            for source_root in (output / "src", output / "python"):
                if source_root.is_dir():
                    prefixes.append(source_root)
        if len(prefixes) != 1:
            raise ValueError(f"Map the wheel overlay to its source layout: {relative}")
        target = prefixes[0] / relative_name(relative)
        if not target.resolve().is_relative_to(output.resolve()):
            raise ValueError("Source overlay escapes its component directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(replacement, target)


def cargo_vendor(root, destination):
    manifest = json.loads((root / "MANIFEST.json").read_bytes())
    destination.mkdir()
    for key, item in sorted(manifest["inputs"].items()):
        if not key.startswith("crate/"):
            continue
        path = blob(root, item)
        name, version = key[6:].rsplit("@", 1)
        crate = destination / relative_name(name + "-" + version)
        unpack_source(path, crate)
        files = {p.relative_to(crate).as_posix(): sha256(p) for p in crate.rglob("*") if p.is_file()}
        (crate / ".cargo-checksum.json").write_text(json.dumps({"package": item["sha256"], "files": files}))
    print('[source.crates-io]\nreplace-with = "release-source"\n[source.release-source]\n'
          + "directory = " + json.dumps(str(destination.resolve())) + "\n[net]\noffline = true")


def local_wheel(root, source, arch, wheelhouse, output):
    from wheelhouse import records
    from build_paths import RECORD, read_roots, write_record
    from check_package_paths import check_archive
    python = sys.executable
    wheelhouse.mkdir(parents=True, exist_ok=True)
    previous_roots = read_roots(wheelhouse, sorted(wheelhouse.glob("*.whl"))) if (wheelhouse / RECORD).exists() else []
    with tempfile.TemporaryDirectory() as folder:
        stage = Path(folder)
        editable = stage / "source"
        def generated(directory, names):
            return [name for name in names if name in (".git", "__pycache__") or name.endswith(".egg-info")
                    or Path(directory) == source and name in ("build", "dist", "target")]
        shutil.copytree(source, editable, symlinks=True, ignore=generated)
        temporary = stage / "temporary"
        temporary.mkdir()
        roots = [source.absolute(), source.resolve(), root.absolute(), root.resolve(), stage,
                 Path(sys.prefix), Path(sys.base_prefix), Path(sys.executable).absolute().parent]
        roots.extend(Path(os.environ.get(key, Path.home() / default)) for key, default in
                     (("CARGO_HOME", ".cargo"), ("RUSTUP_HOME", ".rustup")))
        env = compiler_environment(roots, arch)
        env.update(TMPDIR=str(temporary), PIP_NO_CACHE_DIR="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
        subprocess.run([python, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                        "--wheel-dir", folder, editable], env=env, check=True)
        wheel, = Path(folder).glob("*.whl")
        check_archive(wheel, roots, engine=None)
        owner, version = wheel.name.split("-")[:2]
        with zipfile.ZipFile(wheel) as package:
            meta, = (name for name in package.namelist() if name.endswith(".dist-info/METADATA"))
            from email import message_from_bytes
            header = message_from_bytes(package.read(meta))
            for name in package.namelist():
                if name.endswith((".so", ".dylib", ".bundle")):
                    binary = stage / "native-extension"
                    binary.write_bytes(package.read(name))
                    subprocess.run(["lipo", binary, "-verify_arch", arch], check=True, capture_output=True)
        owner = canonical(owner)
        if canonical(header["Name"]) != owner or header["Version"] != version:
            raise ValueError("Wheel name differs from metadata")
        lock = root / "project/requirements-build.lock"
        if arch == "x86_64":
            lock = root / "project/build/dependencies/x86_64/requirements-build.lock"
        selected = []
        replaced = False
        for filename, requirement, checksum in records(lock):
            name, expected = filename.split("-")[:2]
            if canonical(name) == owner:
                if version != expected:
                    raise ValueError("Rebuild the matching release component version")
                filename, checksum = wheel.name, sha256(wheel)
                replaced = True
            selected.append((filename, requirement, checksum))
        if not replaced:
            raise ValueError("Wheel is absent from the release build lock")
        shutil.copyfile(wheel, wheelhouse / wheel.name)
        write_record(wheelhouse, [*previous_roots, *roots], sorted(wheelhouse.glob("*.whl")))
        output.write_text(selected_lock(selected, wheelhouse.resolve(), {owner}))
        print(wheelhouse / wheel.name)


def compiler_environment(roots, arch):
    env = dict(os.environ)
    roots = [*roots, *(Path(env.get(key, Path.home() / default)) for key, default in
                       (("CARGO_HOME", ".cargo"), ("RUSTUP_HOME", ".rustup")))]
    paths = sorted({str(form) for path in roots for form in (path.absolute(), path.resolve())}, key=len)
    flags = [flag for path in paths for flag in (f"-ffile-prefix-map={path}=.", f"-fdebug-prefix-map={path}=.")]
    for key in ("CFLAGS", "CXXFLAGS", "CPPFLAGS"):
        env[key] = (env.get(key, "") + " " + shlex.join(flags)).strip()
    env["LDFLAGS"] = (env.get("LDFLAGS", "") + " -Wl,-S").strip()
    rust = env.get("CARGO_ENCODED_RUSTFLAGS")
    rust = (rust.split("\x1f") if rust else []) if rust is not None else shlex.split(env.get("RUSTFLAGS", ""))
    env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join([*rust, *(f"--remap-path-prefix={path}=." for path in paths)])
    env.setdefault("ARCHFLAGS", f"-arch {arch}")
    env.setdefault("MACOSX_DEPLOYMENT_TARGET", "14.0")
    return env


def selected_lock(records, wheelhouse, changed, *, cached=False, prepared=None):
    lines = []
    locations = [wheelhouse]
    if cached:
        if prepared is not None:
            locations.append(prepared)
        for location in shlex.split(os.environ.get("PIP_FIND_LINKS", "")):
            if not location.lower().startswith("file://"):
                directory = Path(location)
                try:
                    if directory.is_dir():
                        locations.append(directory)
                        continue
                except OSError as error:
                    if error.errno != errno.ENAMETOOLONG:
                        raise
            url = urlsplit(location)
            if url.scheme == "file" and url.netloc.lower() in ("", "localhost") and url.path:
                locations.append(Path(os.fsdecode(unquote_to_bytes(url.path))))
    for filename, requirement, value in records:
        owner = canonical(filename.split("-")[0])
        if owner in changed:
            path = wheelhouse / filename
            if not path.is_file() or sha256(path) != value:
                raise ValueError("Edited wheel does not match its local lock")
            requirement = f"{filename.split('-')[0]} @ {path.resolve().as_uri()}"
        elif cached:
            path = next((folder / filename for folder in locations
                         if (folder / filename).is_file() and sha256(folder / filename) == value), None)
            if path is not None:
                requirement = f"{filename.split('-')[0]} @ {path.resolve().as_uri()}"
        lines.extend(["# " + filename, f"{requirement} --hash=sha256:{value}"])
    return "\n".join(lines) + "\n"


def local_selections(original, edited, shipped):
    from wheelhouse import entries
    def indexed(path):
        records = {}
        for wheel, value in entries(path):
            name, version = wheel.split("-")[:2]
            key = canonical(name)
            if key in records:
                raise ValueError("Duplicate release component")
            records[key] = (version, value)
        return records
    before, after = indexed(original), indexed(edited)
    if before.keys() != after.keys() or any(v[0] != after[k][0] for k, v in before.items()):
        raise ValueError("The local lock must retain the release component names and versions")
    if any(before[name] != after[name] for name in before.keys() - shipped.keys()):
        raise ValueError("Select an edited frozen runtime component")
    if any(name not in after or after[name][0] != owner["version"] for name, owner in shipped.items()):
        raise ValueError("The local lock must retain the shipped component versions")
    return {name: {"upstreamWheel": owner["wheel"], "wheel": after[name][1]}
            for name, owner in shipped.items() if owner["wheel"] != after[name][1]}


def refreeze(root, arch, wheelhouse, lock, output):
    if platform.machine() != arch or output.exists():
        raise ValueError("Use a new output directory on the matching native Mac")
    from wheelhouse import entries, records
    manifest = json.loads((root / "MANIFEST.json").read_bytes())
    inventory = relative_name(manifest["architectures"][arch]["inventory"])
    fd = os.open(root / inventory, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > INVENTORY_LIMIT:
            raise ValueError("Invalid source inventory size or type")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(info.st_size + 1)
    finally:
        os.close(fd)
    if len(data) != info.st_size or hashlib.sha256(data).hexdigest() != manifest["files"][inventory]:
        raise ValueError("Changed release source input inventory")
    volume = json.loads(data)
    project = root / "project"
    original = project / ("requirements-build.lock" if arch == "arm64" else "build/dependencies/x86_64/requirements-build.lock")
    lock = lock or original
    changed = local_selections(original, lock, volume["owners"])
    prepared = set(entries(original))
    edited = {canonical(wheel.split("-")[0]) for wheel, value in entries(lock) if (wheel, value) not in prepared}
    if edited and wheelhouse is None:
        raise ValueError("Supply the wheelhouse containing your modified components")
    wheels = wheelhouse.resolve() if wheelhouse is not None else None
    roots = [root, project, output, lock.parent, Path(sys.prefix), Path(sys.base_prefix)]
    if wheels is not None:
        from build_paths import RECORD, read_roots
        roots.append(wheels)
        if (wheels / RECORD).exists():
            roots.extend(read_roots(wheels, sorted(wheels.glob("*.whl"))))
    with tempfile.TemporaryDirectory() as temporary:
        if wheels is None:
            wheels = Path(temporary) / "wheels"
            wheels.mkdir()
        # A package index or find-links directory may also have the original wheel under
        # the same filename. Direct local references select the trusted replacement bytes.
        effective_lock = Path(temporary) / "local-download.lock"
        prepared_wheels = original.parent / "wheels" if arch == "x86_64" else None
        effective_lock.write_text(selected_lock(records(lock), wheels, edited, cached=True, prepared=prepared_wheels))
        output.mkdir(parents=True)
        settings = Path(temporary) / "rebuild.json"
        settings.write_text(json.dumps({"upstreamCommit": manifest["commit"], "components": changed,
                                        "manifest": str(root / "MANIFEST.json"),
                                        "buildRoots": [str(path.absolute()) for path in [*roots, Path(temporary)]]}))
        env = {**os.environ, "HOMESCREEN_BUILD_ARCH": arch, "HOMESCREEN_BUILD_PYTHON": sys.executable,
               "HOMESCREEN_SOURCE_CACHE": str(root / "sources")}
        subprocess.run([str(project / "macapp/build.sh"), "--local-build", output.resolve(), effective_lock,
                        wheels, settings], env=env, check=True)



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    edit = commands.add_parser("component")
    edit.add_argument("owner")
    edit.add_argument("--arch", choices=("arm64", "x86_64"), required=True)
    edit.add_argument("--output", type=Path, required=True)
    cargo = commands.add_parser("cargo-vendor")
    cargo.add_argument("--output", type=Path, required=True)
    wheel = commands.add_parser("wheel")
    wheel.add_argument("--arch", choices=("arm64", "x86_64"), required=True)
    for name in ("source", "wheelhouse", "output"):
        wheel.add_argument("--" + name, type=Path, required=True)
    local = commands.add_parser("refreeze")
    local.add_argument("--arch", required=True, choices=("arm64", "x86_64"))
    local.add_argument("--wheelhouse", type=Path)
    local.add_argument("--lock", type=Path)
    local.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "component":
        editable(args.root, canonical(args.owner), args.arch, args.output)
    elif args.command == "cargo-vendor":
        cargo_vendor(args.root, args.output)
    elif args.command == "refreeze":
        refreeze(args.root.resolve(), args.arch, args.wheelhouse, args.lock, args.output)
    else:
        local_wheel(args.root, args.source, args.arch, args.wheelhouse, args.output)


if __name__ == "__main__":
    main()
