"""Assemble a standalone macOS MCP bundle from checksum-verified app archives."""
import argparse
import copy
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat
import zipfile

from release import ARCHITECTURES, ROOT, project_version

APP = "Screen Organizer.app"
RESOURCES = f"{APP}/Contents/Resources/"
ENGINE = RESOURCES + "engine/"
NOTICES = ("LICENSE", "dependencies.txt", "requirements-build.lock", "THIRD-PARTY-NOTICES.txt", "SourceInventory.json", "ThirdPartyLicenses/components.json")
INTEL_NOTICES = ("OPENSSL-LICENSE.txt", "requirements-cryptography-x86_64.lock")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_assets(folder, version):
    expected = {f"home-screen-organizer-{version}-macos-{arch}.{extension}"
                for arch in ARCHITECTURES for extension in ("dmg", "zip")}
    seen = {}
    for line in (folder / "SHA256SUMS.txt").read_text().splitlines():
        checksum, name = line.split("  ")
        if name not in expected or name in seen or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("Supply the four-archive release checksum manifest")
        if digest(folder / name) != checksum:
            raise ValueError(f"Archive checksum differs: {name}")
        seen[name] = checksum
    if seen.keys() != expected:
        raise ValueError("Supply both architecture archive pairs")
    return seen


def engine_entries(archive, arch, version):
    """Validate the complete ZIP, then map engine and notice members without extraction."""
    from check_package_paths import check_zip_metadata
    check_zip_metadata(archive)
    entries = {}
    for info in archive.infolist():
        name = info.filename.rstrip("/")
        parts = name.split("/")
        if (not name or any(part in ("", ".", "..") for part in parts)
                or "\\" in name or "\x00" in name or parts[0] != APP):
            raise ValueError(f"Unexpected ZIP path: {info.filename}")
        if name in entries:
            raise ValueError(f"Duplicate ZIP path: {name}")
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
            raise ValueError(f"Unsupported ZIP file type: {name}")
        entries[name] = info
    links = {name for name, info in entries.items() if stat.S_ISLNK(info.external_attr >> 16)}
    for name in entries:
        if any(str(parent) in links for parent in PurePosixPath(name).parents):
            raise ValueError(f"ZIP member descends through symlink: {name}")
    targets = {}
    paths = set(entries)
    for name in entries:
        paths.update(str(parent) for parent in PurePosixPath(name).parents)
    for name in links:
        target = archive.read(entries[name]).decode("utf-8")
        if (not name.startswith(ENGINE) or not target or target.startswith("/")
                or "\\" in target or "\x00" in target):
            raise ValueError(f"Unsupported engine symlink: {name}")
        targets[name] = target
    for name in links:
        pending = name.split("/")
        resolved = []
        hops = 0
        while pending:
            part = pending.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if len(resolved) <= len(ENGINE.rstrip("/").split("/")):
                    raise ValueError(f"Engine symlink escapes its tree: {name}")
                resolved.pop()
                continue
            resolved.append(part)
            prefix = "/".join(resolved)
            if prefix in targets:
                hops += 1
                if hops > 40:
                    raise ValueError(f"Cyclic engine symlink: {name}")
                resolved.pop()
                pending = targets[prefix].split("/") + pending
        target_path = "/".join(resolved)
        if not target_path.startswith(ENGINE) or target_path not in paths:
            raise ValueError(f"Dangling engine symlink: {name}")
    mapped = []
    for name, info in entries.items():
        if name.startswith(ENGINE):
            destination = f"server/{arch}/" + name[len(ENGINE):]
            mapped.append((info, destination + ("/" if info.is_dir() else "")))
        elif (name in {RESOURCES + notice for notice in NOTICES + (INTEL_NOTICES if arch == "x86_64" else ())}
              or name.startswith(RESOURCES + "ThirdPartyLicenses/")):
            if info.is_dir():
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f"Expected notice file: {name}")
            mapped.append((info, f"licenses/{arch}/" + name[len(RESOURCES):]))
    plist = plistlib.loads(archive.read(f"{APP}/Contents/Info.plist"))
    if (plist.get("HomeScreenOrganizerReleaseVersion", plist.get("CFBundleShortVersionString")) != version
            or plist.get("CFBundleShortVersionString") != version.split("-")[0]
            or plist.get("HomeScreenOrganizerArchitecture") != arch):
        raise ValueError(f"App version or architecture differs: {arch}")
    executable = entries.get(ENGINE + "engine")
    if (executable is None or not stat.S_ISREG(executable.external_attr >> 16)
            or not executable.external_attr >> 16 & 0o111):
        raise ValueError(f"Missing native engine executable: {arch}")
    for notice in NOTICES + (INTEL_NOTICES if arch == "x86_64" else ()):
        if RESOURCES + notice not in entries:
            raise ValueError(f"Missing engine notice: {arch}/{notice}")
    return mapped


def write_file(archive, name, data, mode=0o644):
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data)


def bundle_manifest(repository, version):
    url = f"https://github.com/{repository}"
    return {
        "manifest_version": "0.3", "name": "home-screen-organizer", "display_name": "Screen Organizer",
        "version": version, "description": "Arrange iPhone Home Screen apps, folders, widgets and the App Library from your Mac.",
        "author": {"name": "Jonathan Kats", "url": "https://github.com/kats1123/homescreen-mcp"},
        "homepage": url, "repository": {"type": "git", "url": url}, "support": url + "/issues", "license": "GPL-3.0-or-later",
        "server": {"type": "binary", "entry_point": "server/launch.sh", "mcp_config": {
            "command": "/bin/sh", "args": ["${__dirname}/server/launch.sh"], "env": {"HOMESCREEN_READ_ONLY": "1"}}},
        "compatibility": {"platforms": ["darwin"]},
    }


def verify_bundle(path, assets, version, repository, license_text, launcher):
    def verify_file(bundle, name, data, mode):
        info = bundle.getinfo(name)
        if (info.external_attr >> 16 != (stat.S_IFREG | mode) or info.file_size != len(data)
                or bundle.read(info) != data):
            raise ValueError("Different root MCP bundle member")

    with zipfile.ZipFile(path) as bundle:
        members = {i.filename: i for i in bundle.infolist()}
        if len(members) != len(bundle.infolist()):
            raise ValueError("Duplicate MCP bundle member")
        expected = {"LICENSE", "server/launch.sh", "manifest.json", "THIRD-PARTY-NOTICES.txt"}
        verify_file(bundle, "LICENSE", license_text, 0o644)
        verify_file(bundle, "server/launch.sh", launcher, 0o755)
        manifest = (json.dumps(bundle_manifest(repository, version), indent=2) + "\n").encode()
        verify_file(bundle, "manifest.json", manifest, 0o644)
        for arch in ARCHITECTURES:
            with zipfile.ZipFile(assets / f"home-screen-organizer-{version}-macos-{arch}.zip") as source:
                verify_file(bundle, "THIRD-PARTY-NOTICES.txt", source.read(RESOURCES + "THIRD-PARTY-NOTICES.txt"), 0o644)
                for info, name in engine_entries(source, arch, version):
                    copied = members.get(name)
                    if copied is None or copied.external_attr != info.external_attr or copied.file_size != info.file_size:
                        raise ValueError("Different native member in MCP bundle")
                    with source.open(info) as left, bundle.open(copied) as right:
                        if hashlib.file_digest(left, "sha256").digest() != hashlib.file_digest(right, "sha256").digest():
                            raise ValueError("Different native member in MCP bundle")
                    expected.add(name)
        if members.keys() != expected:
            raise ValueError("Unexpected MCP bundle member")


def assemble(assets_dir, output_dir, repository, version):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Supply a GitHub OWNER/REPO repository")
    checksums = verify_assets(assets_dir, version)
    archives = []
    with ExitStack() as stack:
        for arch in ARCHITECTURES:
            archive = stack.enter_context(zipfile.ZipFile(assets_dir / f"home-screen-organizer-{version}-macos-{arch}.zip"))
            archives.append((archive, engine_entries(archive, arch, version)))
        url = f"https://github.com/{repository}"
        manifest = bundle_manifest(repository, version)
        output_dir.mkdir(parents=True, exist_ok=True)
        bundle = output_dir / f"home-screen-organizer-{version}-macos.mcpb"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as destination:
            write_file(destination, "manifest.json", json.dumps(manifest, indent=2) + "\n")
            write_file(destination, "LICENSE", (ROOT / "LICENSE").read_bytes())
            text = archives[0][0].read(RESOURCES + "THIRD-PARTY-NOTICES.txt")
            if archives[1][0].read(RESOURCES + "THIRD-PARTY-NOTICES.txt") != text:
                raise ValueError("The native source download notices differ")
            write_file(destination, "THIRD-PARTY-NOTICES.txt", text)
            write_file(destination, "server/launch.sh", (ROOT / "macapp/mcp_launcher.sh").read_bytes(), 0o755)
            for archive, entries in archives:
                for info, name in entries:
                    copied = copy.copy(info)
                    copied.filename = name
                    destination.writestr(copied, archive.read(info))
        server = {
            "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
            "name": "io.github.worldsavepoint/home-screen-organizer", "title": "Screen Organizer", "version": version,
            "description": manifest["description"], "repository": {"url": url, "source": "github"},
            "packages": [{"registryType": "mcpb", "identifier": f"{url}/releases/download/v{version}/{bundle.name}",
                          "fileSha256": digest(bundle), "transport": {"type": "stdio"}}],
        }
        metadata = output_dir / "server.json"
        metadata.write_text(json.dumps(server, indent=2) + "\n")
        checksums.update({bundle.name: digest(bundle), metadata.name: digest(metadata)})
        (output_dir / "SHA256SUMS.txt").write_text("".join(f"{value}  {name}\n" for name, value in sorted(checksums.items())))
        return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    try:
        print(assemble(args.assets_dir.resolve(), args.output_dir.resolve(), args.repository, project_version()))
    except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
