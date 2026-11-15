"""Identify and preserve editable inputs of a completed PyInstaller freeze."""
from collections import defaultdict
from fnmatch import fnmatchcase
from hashlib import sha256 as digest
from importlib.metadata import distributions, version as package_version
from itertools import chain
from pathlib import Path, PurePosixPath
import argparse
import configparser
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
import zipfile

from source_inputs import (HEX, cache_directory, canonical, fetch, json_bytes, read_locks, relative_name,
                           sha256, source_member, source_members)

NOTICE = re.compile(r"^(?:licenses?|licences?|copying|copyright|notice|authors|patents)(?:[._-].*)?$", re.I)


def installed_files(packages):
    result = {}
    for dist in packages:
        metadata = dist.metadata
        owner, version = canonical(metadata["Name"]), metadata["Version"]
        for item in dist.files or ():
            relative = str(item)
            if relative.startswith("../") or "/__pycache__/" in relative:
                continue
            path = Path(dist.locate_file(item)).resolve()
            if path in result and result[path][0] != owner:
                raise ValueError(f"Multiple owners for installed file: {item}")
            result[path] = (owner, version, relative)
    return result


def wheel_material(owner, wheel, source, collected, notices, overlays):
    """Keep grants verbatim and editable files that differ from the release source."""
    names = set(wheel.namelist())
    for name in sorted(names):
        if (not name.endswith("/") and (".dist-info/licenses/" in name
                or NOTICE.fullmatch(name.rsplit("/", 1)[-1]))):
            put(notices / owner, name, wheel.read(name))
    # Index only needed source suffixes, so two differently nested packages cannot collide.
    candidates = {name.rsplit("/", 1)[-1] for name in collected}
    original = defaultdict(set)

    def consider(name, stream):
        if name.rsplit("/", 1)[-1] not in candidates:
            return
        parts = name.split("/")[1:]
        for index in range(len(parts)):
            relative = "/".join(parts[index:])
            if relative in collected:
                with stream() as source_file:
                    original[relative].add(digest(source_file.read()).hexdigest())
                return

    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    consider(info.filename, lambda: archive.open(info))
    else:
        with tarfile.open(source) as archive:
            for info in archive:
                if info.isfile():
                    consider(info.name, lambda: archive.extractfile(info))
    for name in sorted(collected & names):
        data = wheel.read(name)
        if digest(data).hexdigest() not in original[name]:
            put(overlays / owner, name, data)


def put(root, name, data):
    destination = root / relative_name(name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def files_below(root):
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}


def source_url(repository, version):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or ".." in repository:
        raise ValueError("Supply a public owner/repository")
    return f"https://github.com/{repository}/releases/download/v{version}/home-screen-organizer-{version}-corresponding-source.tar.gz"


def distribution_notice(repository, version):
    return (
        "Screen Organizer " + version + "\n\n"
        "Original Homescreen source: Copyright © 2026 Jonathan Kats. See LICENSE for its MIT license.\n"
        "The combined frozen Python engine is distributed under the GNU General Public License, "
        "version 3 or any later version. The individual components retain their own grants. "
        "The lzss shared library is covered by the GNU Lesser General Public License version 3.\n"
        "The per-component texts and copyright notices are in ThirdPartyLicenses in the app "
        "and licenses/<architecture>/ThirdPartyLicenses in the MCP bundle.\n\n"
        "Download the matching Corresponding Source, build scripts and replacement instructions:\n"
        + source_url(repository, version) + "\n"
    ).encode()


def project_release_version(root, installed):
    from packaging.version import Version
    from release import project_version
    version = project_version(root=root)
    if Version(version) != Version(installed):
        raise ValueError("Installed project version differs from the release version")
    return version


LAUNCHER_DIGEST = "sha256-unsigned-macho64-linkedit-align16-v2"


def valid_inline_command(data, offset, length, command, cpu):
    fixed = {0x1A: 72, 0x1B: 24, 0x24: 16, 0x25: 16, 0x2A: 16, 0x2F: 16, 0x30: 16}
    if command in fixed:
        return length == fixed[command]
    if command == 0x32:
        return length >= 24 and length == 24 + 8 * struct.unpack_from("<I", data, offset + 20)[0]
    dylibs = {0xC, 0xD, 0x80000018, 0x8000001F, 0x20, 0x80000023}
    strings = {0xE, 0xF, 0x12, 0x13, 0x14, 0x15, 0x8000001C}
    header = 24 if command in dylibs else 12 if command in strings else 20 if command == 0x10 else 0
    if header:
        if length < header:
            return False
        start = struct.unpack_from("<I", data, offset + 8)[0]
        if start < header or start >= length or data.find(b"\0", offset + start, offset + length) < 0:
            return False
        if command == 0x10:
            modules, bitmap = struct.unpack_from("<II", data, offset + 12)
            if modules and (bitmap < header or bitmap + (modules + 7) // 8 > length):
                return False
        return True
    if command in (4, 5):
        cursor, end = offset + 8, offset + length
        if cursor == end:
            return False
        while cursor + 8 <= end:
            flavor, count = struct.unpack_from("<II", data, cursor)
            if (flavor, count) != ({0x0100000C: (6, 68), 0x01000007: (4, 42)}[cpu]):
                return False
            cursor += 8 + count * 4
        return cursor == end
    if command == 0x2D:
        if length < 16:
            return False
        count = struct.unpack_from("<I", data, offset + 8)[0]
        cursor, end = offset + 12, offset + length
        if count > end - cursor:
            return False
        for _ in range(count):
            terminator = data.find(b"\0", cursor, end)
            if terminator < 0:
                return False
            cursor = terminator + 1
        return (cursor - offset + 7) // 8 * 8 == length and not any(data[cursor:end])
    return False


def canonical_launcher_payload(data):
    error = "Could not canonicalize the native launcher"
    if len(data) < 32:
        raise ValueError(error)
    magic, cpu, _, filetype, count, size, _, _ = struct.unpack_from("<IiiIIIII", data)
    page = {0x0100000C: 16384, 0x01000007: 4096}.get(cpu)
    if magic != 0xFEEDFACF or page is None or filetype != 2 or not 0 < count <= 4096 or size > len(data) - 32:
        raise ValueError(error)
    offset, end, linkedit, references = 32, 32 + size, None, []
    linkedit_commands = {0x1E, 0x26, 0x29, 0x2B, 0x2E, 0x80000033, 0x80000034, 0x36, 0x37, 0x38}
    for _ in range(count):
        if offset + 8 > end:
            raise ValueError(error)
        command, length = struct.unpack_from("<II", data, offset)
        if length < 8 or length % 8 or offset + length > end or command == 0x1D:
            raise ValueError(error)
        if command == 0x19:
            if length < 72:
                raise ValueError(error)
            _, _, name, address, virtual_size, file_offset, file_size, _, _, sections, _ = struct.unpack_from("<II16sQQQQiiII", data, offset)
            if length != 72 + 80 * sections or file_offset + file_size > len(data):
                raise ValueError(error)
            for index in range(sections):
                section = struct.unpack_from("<16s16sQQ8I", data, offset + 72 + 80 * index)
                section_size, section_offset, relocations, relocation_count, flags = section[3], section[4], section[6], section[7], section[8]
                if (flags & 0xFF) not in (1, 0xC, 0x12) and section_size:
                    if section_offset < file_offset or section_offset + section_size > file_offset + file_size:
                        raise ValueError(error)
                    references.append((section_offset, section_size))
                references.append((relocations, relocation_count * 8))
            if name.rstrip(b"\0") == b"__LINKEDIT":
                rounded = (file_size + page - 1) // page * page
                if (linkedit is not None or file_offset < end or address % page or virtual_size % page
                        or virtual_size < rounded or file_offset + file_size != len(data)):
                    raise ValueError(error)
                linkedit = offset, file_size
        elif command == 2:
            if length != 24:
                raise ValueError(error)
            symbols, count, strings, string_size = struct.unpack_from("<4I", data, offset + 8)
            references.extend(((symbols, count * 16), (strings, string_size)))
        elif command == 0xB:
            if length != 80:
                raise ValueError(error)
            values = struct.unpack_from("<18I", data, offset + 8)
            references.extend((values[index], values[index + 1] * width)
                              for index, width in zip(range(6, 18, 2), (8, 56, 4, 4, 8, 8)))
        elif command in (0x22, 0x80000022):
            if length != 48:
                raise ValueError(error)
            values = struct.unpack_from("<10I", data, offset + 8)
            references.extend(zip(values[::2], values[1::2]))
        elif command in linkedit_commands or command == 0x16:
            if length != 16:
                raise ValueError(error)
            start, size = struct.unpack_from("<II", data, offset + 8)
            references.append((start, size * 4 if command == 0x16 else size))
        elif command == 0x31:
            if length != 40:
                raise ValueError(error)
            references.append(struct.unpack_from("<QQ", data, offset + 24))
        elif command == 0x80000028:
            if length != 24:
                raise ValueError(error)
            references.append((struct.unpack_from("<Q", data, offset + 8)[0], 1))
        elif not valid_inline_command(data, offset, length, command, cpu):
            raise ValueError(error)
        offset += length
    if (offset != end or linkedit is None
            or any(size and (start < end or start + size > len(data)) for start, size in references)):
        raise ValueError(error)
    payload = bytearray(data)
    padding = -len(payload) % 16
    payload.extend(bytes(padding))
    offset, file_size = linkedit
    file_size += padding
    struct.pack_into("<Q", payload, offset + 32, (file_size + page - 1) // page * page)
    struct.pack_into("<Q", payload, offset + 48, file_size)
    return bytes(payload)


def launcher_digest(executable):
    """Hash every unsigned launcher byte with deterministic LINKEDIT alignment."""
    try:
        with tempfile.TemporaryDirectory() as folder:
            payload = Path(folder) / "launcher"
            shutil.copyfile(executable, payload)
            subprocess.run(["codesign", "--remove-signature", payload], check=True, capture_output=True)
            return digest(canonical_launcher_payload(payload.read_bytes())).hexdigest()
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("Could not fingerprint the native launcher") from None


ROOT_RECIPES = {"pyproject.toml", "LICENSE", "requirements-build.lock", "requirements-build-x86_64.lock",
                "requirements-cryptography-x86_64.lock", "setup.py", "setup.cfg", "MANIFEST.in"}


def source_glob_matches(name, pattern):
    """Match standard recursive glob components without accessing a filesystem."""
    names = name.split("/")
    reachable = {0}
    for component in pattern.split("/"):
        if component == "**":
            for index, value in enumerate(names):
                if index in reachable and not value.startswith("."):
                    reachable.add(index + 1)
        else:
            reachable = {index + 1 for index in reachable if index < len(names)
                         and (not names[index].startswith(".") or component.startswith("."))
                         and fnmatchcase(names[index], component)}
    return len(names) in reachable


def project_root_inputs(names, read, expand=lambda pattern: ()):
    """Select the pinned backend's declared metadata and native recipe inputs."""
    names = set(names)
    selected = ROOT_RECIPES & names
    def content(name):
        try:
            return read(name).decode("utf-8") if name in names else ""
        except FileNotFoundError:
            return ""
    config = tomllib.loads(content("pyproject.toml"))
    project = config.get("project", {})
    setuptools = config.get("tool", {}).get("setuptools", {})
    def metadata_path(value):
        if not isinstance(value, str) or value.endswith(("/", "/.")):
            raise ValueError("Expected a regular project metadata file")
        return relative_name(PurePosixPath(value).as_posix())
    def files(value):
        if isinstance(value, str):
            selected.add(metadata_path(value))
        elif isinstance(value, list):
            for name in value:
                files(name)
        elif isinstance(value, dict):
            if "file" in value:
                files(value["file"])
    files(project.get("readme"))
    license = project.get("license")
    if isinstance(license, dict):
        files(license)
    def dynamic(value):
        if isinstance(value, dict):
            files(value)
            for child in value.values():
                dynamic(child)
    dynamic(setuptools.get("dynamic", {}))
    setup = configparser.ConfigParser(interpolation=None)
    setup.read_string(content("setup.cfg"))
    for section in setup.sections():
        for value in setup[section].values():
            if value.strip().startswith("file:"):
                files([name.strip() for name in re.split(r"[,\n]", value.strip()[5:]) if name.strip()])
    patterns = project.get("license-files", setuptools.get("license-files"))
    legacy = setup["metadata"].get("license_file") if setup.has_section("metadata") else None
    if patterns is None and setup.has_section("metadata"):
        value = setup["metadata"].get("license_files")
        if value is not None:
            patterns = [name.strip() for name in re.split(r"[,\n]", value) if name.strip()]
    if patterns is None:
        patterns = [] if legacy else ["LICEN[CS]E*", "COPYING*", "NOTICE*", "AUTHORS*"]
    if legacy:
        patterns = [*patterns, legacy]
    for pattern in patterns:
        pattern = metadata_path(pattern)
        if pattern.rsplit("/", 1)[-1] == "**":
            pattern += "/*"  # A file needs a basename beneath the directory matched by **.
        selected.update(relative_name(name) for name in chain(names, expand(pattern))
                        if not name.endswith("~") and source_glob_matches(name, pattern))
    return selected


def local_project_input(project, name):
    path = project
    for part in relative_name(name).split("/"):
        path /= part
        if path.is_symlink():
            raise ValueError("Use regular local project inputs")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Use regular local project inputs")
    return path.read_bytes()


def local_glob_candidates(project, pattern):
    parts = pattern.split("/")
    parents = ["/".join(parts[:end]) for end in range(1, len(parts))]
    if not parents:
        return  # The root listing already supplies these candidates.
    for directory, dirs, files, fd in os.fwalk(project, follow_symlinks=False):
        parent = Path(directory).relative_to(project)
        children = []
        for leaf in dirs:
            name = (parent / leaf).as_posix()
            descend = any(source_glob_matches(name, prefix) for prefix in parents)
            if stat.S_ISLNK(os.lstat(leaf, dir_fd=fd).st_mode):
                if descend:
                    raise ValueError("Use regular local project inputs")
                yield name
            elif descend:
                children.append(leaf)
        dirs[:] = children
        for leaf in files:
            mode = os.lstat(leaf, dir_fd=fd).st_mode
            if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                yield (parent / leaf).as_posix()


def local_root_inputs(project, baseline=()):
    names = {*baseline, *(path.name for path in project.iterdir() if path.is_file() or path.is_symlink())}
    return project_root_inputs(names, lambda name: local_project_input(project, name),
                               lambda pattern: local_glob_candidates(project, pattern))


def captured_root_inputs(manifest, baseline, public):
    data = json.loads(manifest.read_bytes())
    name = data.get("architectures", {}).get(public.get("arch"), {}).get("inventory")
    if name is None:
        return {}
    path = manifest.parent / relative_name(name)
    if sha256(path) != baseline.get(name):
        raise ValueError("Different original project input inventory")
    selected = json.loads(path.read_bytes()).get("projectInputs", {})
    if (not isinstance(selected, dict) or any(not isinstance(value, str) or not HEX.fullmatch(value)
            or baseline.get("project/" + relative_name(name)) != value for name, value in selected.items())):
        raise ValueError("Different original project input inventory")
    return selected


def finalize_local_record(project, manifest, record, executable):
    public = json.loads(record.read_bytes())
    baseline = json.loads(manifest.read_bytes())["files"]
    roots = ("homescreen/", "macapp/", "tools/")
    root_files = local_root_inputs(project, (name.removeprefix("project/") for name in baseline if name.startswith("project/")))
    root_files.update(captured_root_inputs(manifest, baseline, public))
    relevant = {name.removeprefix("project/") for name in baseline if name.startswith("project/")
                and (name.removeprefix("project/").startswith(roots) or name.removeprefix("project/") in root_files)}
    paths = {project / name for name in relevant if (project / name).exists() or (project / name).is_symlink()}
    generated = {project / "macapp/Shell/.build", project / "macapp/Shell/.swiftpm"}
    def collect(directory):
        if not directory.exists():
            return
        for path in directory.iterdir():
            if path in generated:
                continue
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode) and path.parent.name == "__pycache__" and path.suffix == ".pyc":
                continue
            if stat.S_ISDIR(mode):
                collect(path)
            else:
                paths.add(path)
    for name in roots:
        collect(project / name)
    paths.update(project / name for name in root_files if (project / name).exists())
    inputs = {}
    for path in sorted(paths):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("Use regular local project inputs")
        name = path.relative_to(project).as_posix()
        inputs[name] = digest(local_project_input(project, name)).hexdigest()
    public["projectFiles"].update(inputs)
    current = public["projectFiles"]
    public["projectChanges"] = {
        "modified": {name: value for name, value in current.items()
                     if "project/" + name in baseline and baseline["project/" + name] != value},
        "added": {name: value for name, value in current.items() if "project/" + name not in baseline},
        "removed": sorted(relevant - current.keys())}
    public["launcher"] = {"algorithm": LAUNCHER_DIGEST, "sha256": launcher_digest(executable)}
    record.write_bytes(json_bytes(public))


def verify_local_record(app):
    record = json.loads((app / "Contents/Resources/LOCAL-BUILD.json").read_bytes())
    if record["launcher"] != {"algorithm": LAUNCHER_DIGEST,
                              "sha256": launcher_digest(app / "Contents/MacOS/HomeScreenOrganizer")}:
        raise ValueError("The signed native launcher differs from the local build record")


def capture(analysis, pyz, executable, collection, root, output, wheel_dir, arch, repository, runtime, project_commit, local_build=None):
    from PyInstaller.archive.readers import CArchiveReader
    from PyInstaller.depend.analysis import get_bootstrap_modules

    packages, native = read_locks()
    local = json.loads(Path(local_build).read_bytes()) if local_build else None
    replacements = local["components"] if local else {}
    wheel_paths = {canonical(path.name.split("-", 1)[0]): path for path in wheel_dir.glob("*.whl")}
    installed = installed_files(distributions())
    version = project_release_version(root, package_version("home-screen-organizer"))
    entries, owners, editable, opened = [], {}, defaultdict(set), {}
    notices, overlays = output / "ThirdPartyLicenses", output / "overlays"
    cache = cache_directory()
    base = Path(sys.base_prefix).resolve()
    sysconfig_name = sysconfig._get_sysconfigdata_name()
    provider = {Path(sys._base_executable).resolve().relative_to(base).as_posix(): sha256(Path(sys._base_executable)),
                "lib/libpython3.12.dylib": runtime["libpython"]}
    sysconfig_input = Path(__import__(sysconfig_name).__file__).resolve()
    provider[sysconfig_input.relative_to(base).as_posix()] = sha256(sysconfig_input)
    bootstrap = {name: path for name, path, kind in get_bootstrap_modules() if kind == "PYMODULE"}
    engine_dir = Path(collection.name)
    package = CArchiveReader(str(engine_dir / "engine"))
    pyz_reader = package.open_embedded_archive(next(k for k, v in package.toc.items() if v[-1] == "z"))
    project = "home-screen-organizer"

    project_files = {}

    def record(section, target, path, kind, payload_hash):
        relative_name(target)
        if path in ("-", None):
            entries.append([section, target, "pyinstaller", "namespace", payload_hash])
            return
        path = Path(path).resolve()
        if path in installed:
            owner, installed_version, relative = installed[path]
            if owner == project:
                if ".dist-info/" in relative or ".egg-info/" in relative:
                    relative = "pyproject.toml"
                elif (root / relative).read_bytes() != path.read_bytes():
                    raise ValueError(f"Different project source: {relative}")
            else:
                pin = packages.get(owner)
                if not pin or pin["version"] != installed_version or owner not in wheel_paths:
                    raise ValueError(f"Missing source pin for frozen component: {owner}")
                if owner not in opened:
                    wheel_path = wheel_paths[owner]
                    value = sha256(wheel_path)
                    pinned = pin["wheels"].get(arch)
                    if owner in replacements:
                        selected = replacements[owner]
                        if selected["upstreamWheel"] != pinned and not (arch == "x86_64" and owner == "cryptography"):
                            raise ValueError("Different upstream wheel in local source inventory")
                        pinned = selected["wheel"]
                    if pinned != value and not (owner == "cryptography" and arch == "x86_64" and pinned is None):
                        raise ValueError(f"Different release wheel: {owner}")
                    opened[owner] = zipfile.ZipFile(wheel_path)
                    owners[owner] = {"version": installed_version, "wheel": value}
                wheel = opened[owner]
                if relative in wheel.NameToInfo and not relative.endswith(".dist-info/RECORD"):
                    if digest(wheel.read(relative)).hexdigest() != sha256(path):
                        raise ValueError(f"Installed source differs from wheel: {owner}/{relative}")
                    if kind in ("DATA", "PYMODULE", "PYSOURCE"):
                        editable[owner].add(relative)
                elif not (".dist-info/" in relative and relative.rsplit("/", 1)[-1]
                          in {"INSTALLER", "RECORD", "REQUESTED"}):
                    raise ValueError(f"Missing authenticated wheel member: {owner}/{relative}")
        elif target == sysconfig_name and path.name == sysconfig_name + ".py":
            owner, relative = "cpython", "generated/" + path.name
            put(overlays / owner, relative, path.read_bytes())
        elif path.is_relative_to(base):
            owner, relative = "cpython", path.relative_to(base).as_posix()
            if relative not in provider:
                provider[relative] = sha256(path)
        elif path.is_relative_to(root) and path == root / "macapp/engine.py":
            owner, relative = project, "macapp/engine.py"
        else:
            raise ValueError(f"Unknown source owner for frozen member: {section}/{target}")
        if owner == project and relative not in project_files:
            project_files[relative] = sha256(root / relative)
        entries.append([section, target, owner, relative_name(relative), payload_hash])

    try:
        if {x[0] for x in pyz.toc} != set(pyz_reader.toc):
            raise ValueError("Frozen PYZ inventory differs from analysis")
        for name, path, kind in pyz.toc:
            record("pyz", name, path, kind, digest(pyz_reader.extract(name, raw=True) or b"").hexdigest())
        carchive = {name: (path, kind) for name, path, kind in executable.pkg.toc if kind not in ("OPTION", "PYZ")}
        if set(carchive) != {k for k, v in package.toc.items() if v[-1] != "z"}:
            raise ValueError("Frozen bootstrap inventory differs from analysis")
        for name, (path, kind) in carchive.items():
            record("bootstrap", name, bootstrap.get(name, path), kind, digest(package.extract(name)).hexdigest())
        for name, path, kind in executable.exefiles:
            record("bootloader", name, path, kind, sha256(Path(path)))
        for name, path, kind in collection.toc:
            if kind == "EXECUTABLE":
                continue
            if kind == "SYMLINK":
                entries.append(["link", relative_name(name), "pyinstaller", relative_name(path), digest(path.encode()).hexdigest()])
                continue
            final = engine_dir / "_internal" / name
            if name == "base_library.zip":
                source_by_name = {n: (s, t) for n, s, t in analysis._modules_outside_pyz}
                with zipfile.ZipFile(final) as library:
                    for item in library.infolist():
                        module = item.filename.removesuffix(".pyc").replace("/", ".").removesuffix(".__init__")
                        src, kind = source_by_name[module]
                        record("base", item.filename, src, kind, digest(library.read(item)).hexdigest())
                entries.append(["file", name, "pyinstaller", "base_library.zip", sha256(final)])
                continue
            record("file", name, path, kind, sha256(final))
        for owner, wheel in sorted(opened.items()):
            source = fetch(packages[owner]["source"], cache)
            wheel_material(owner, wheel, source, editable[owner], notices, overlays)
            for member, data in source_members(source, packages[owner].get("notices", {})).items():
                put(notices / owner / "upstream", member, data)
            if owner == "developer-disk-image":
                metadata = next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
                put(notices / owner, "METADATA", wheel.read(metadata))
                parent = opened["pymobiledevice3"]
                member = next(n for n in parent.namelist() if n.endswith(".dist-info/licenses/LICENSE"))
                put(notices / owner, "COPYING", parent.read(member))
            elif owner == "bpylist2":
                put(notices / owner, "README.rst", source_member(source, "bpylist2-4.1.1/README.rst"))
            elif owner == "loguru":
                data = next(x for x in native["assets"] if x["id"] == "loguru-license")
                put(notices / owner, "LICENSE", fetch(data, cache).read_bytes())
            if not (notices / owner).is_dir():
                raise ValueError(f"Missing license for frozen component: {owner}")
        from native_sources import add_native_notices, verify_provider_files
        interpreter_pin = next(item for item in native["assets"] if item["id"] == runtime["archive"])
        verify_provider_files(fetch(interpreter_pin, cache), provider)
        add_native_notices(native, arch, owners, notices, cache)
        index = {owner: {"version": item["version"], "license": packages[owner]["license"]}
                 for owner, item in sorted(owners.items())}
        (notices / "components.json").write_bytes(json_bytes(index))
        project_inputs = {name: digest(local_project_input(root, name)).hexdigest() for name in local_root_inputs(root)}
        public = {"projectCommit": project_commit, "projectFiles": project_files, "projectInputs": project_inputs, "engine": sha256(engine_dir / "engine"), "version": version, "arch": arch, "runtime": runtime, "owners": owners, "members": sorted(entries),
                  "notices": files_below(notices), "overlays": files_below(overlays)}
        notice = distribution_notice(repository, version)
        if local:
            if not replacements.keys() <= owners.keys():
                raise ValueError("The modified component is absent from the frozen engine")
            for owner, values in replacements.items():
                owners[owner].update(modified=True, upstreamWheel=values["upstreamWheel"])
            (output / "LOCAL-BUILD.json").write_bytes(json_bytes({
                "upstreamCommit": local["upstreamCommit"], "arch": arch, "components": owners, "projectFiles": project_files,
                "engine": public["engine"], "editableOverlayHashes": public["overlays"]}))
            notice = (b"Locally modified build. LOCAL-BUILD.json records the actual selected wheels.\n"
                      + notice.replace(b"Download the matching Corresponding Source", b"Download the original release source"))
        else:
            (output / "inventory.json").write_bytes(json_bytes(public))
        (output / "THIRD-PARTY-NOTICES.txt").write_bytes(notice)
    finally:
        for wheel in opened.values():
            wheel.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    local = commands.add_parser("local-record")
    local.add_argument("--project", type=Path, required=True)
    local.add_argument("--settings", type=Path, required=True)
    local.add_argument("--app", type=Path, required=True)
    verify = commands.add_parser("verify-local-record")
    verify.add_argument("--app", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "local-record":
            settings = json.loads(args.settings.read_bytes())
            finalize_local_record(args.project, Path(settings["manifest"]),
                                  args.app / "Contents/Resources/LOCAL-BUILD.json",
                                  args.app / "Contents/MacOS/HomeScreenOrganizer")
        else:
            verify_local_record(args.app)
    except (OSError, ValueError, KeyError):
        parser.exit(1, "Could not verify the local native build record\n")
