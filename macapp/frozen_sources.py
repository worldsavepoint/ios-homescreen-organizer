"""Identify and preserve editable inputs of a completed PyInstaller freeze."""
from collections import defaultdict
from hashlib import sha256 as digest
from importlib.metadata import distributions, version as package_version
from pathlib import Path
import json
import re
import sys
import sysconfig
import tarfile
import zipfile

from source_inputs import (cache_directory, canonical, fetch, json_bytes, read_locks, relative_name,
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
        public = {"projectCommit": project_commit, "projectFiles": project_files, "engine": sha256(engine_dir / "engine"), "version": version, "arch": arch, "runtime": runtime, "owners": owners, "members": sorted(entries),
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
