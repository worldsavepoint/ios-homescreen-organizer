"""Assemble versioned macOS release assets from a completed app build."""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tarfile
import zipfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURES = ("arm64", "x86_64")


RELEASE_VERSION = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-(?:alpha|beta|rc)\.(?:0|[1-9]\d*))?"


def project_version(tag=None):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(RELEASE_VERSION, version):
        raise ValueError("Set the project version to a SemVer release or alpha/beta/rc version")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Release tag must match project version: v{version}")
    return version


def localization_resources(locales):
    manifest = json.loads((locales / "manifest.json").read_text())
    codes = [language["code"] for language in manifest["languages"]]
    if (manifest.get("default") != "en" or "en" not in codes or len(codes) != len(set(codes))
            or any(not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)*", code) for code in codes)):
        raise ValueError("Check the language manifest before packaging")
    files = {"Localization/manifest.json": (locales / "manifest.json").read_bytes()}
    for code in codes:
        data = (locales / f"{code}.json").read_bytes()
        catalog = json.loads(data)
        expected = {"zh-Hans": "屏幕整理", "zh-Hant": "螢幕整理"}.get(code, "Screen Organizer")
        if catalog.get("app.name") != expected:
            raise ValueError(f"Check the localized app display name: {code}")
        files[f"Localization/{code}.json"] = data
        files[f"{code}.lproj/InfoPlist.strings"] = plistlib.dumps({
            "CFBundleName": catalog["app.name"], "CFBundleDisplayName": catalog["app.name"]})
    return codes, files


def localize(app, locales, check=False):
    """Assemble or verify the native catalogs before engine startup."""
    codes, files = localization_resources(locales)
    plist_path = app / "Contents/Info.plist"
    bundle = plistlib.loads(plist_path.read_bytes())
    metadata = {"CFBundleName": "Screen Organizer", "CFBundleDisplayName": "Screen Organizer",
                "CFBundleDevelopmentRegion": "en", "CFBundleLocalizations": codes}
    if check:
        if any(bundle.get(key) != value for key, value in metadata.items()):
            raise ValueError("App localization metadata differs from the language manifest")
        for name, data in files.items():
            if (app / "Contents/Resources" / name).read_bytes() != data:
                raise ValueError(f"App localization resource differs from the shared catalog: {name}")
    else:
        bundle.update(metadata)
        plist_path.write_bytes(plistlib.dumps(bundle))
        for name, data in files.items():
            path = app / "Contents/Resources" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


def private_transaction(parent):
    folder = Path(tempfile.mkdtemp(dir=parent, prefix=".native-release-"))
    try:
        subprocess.run(["/bin/chmod", "-N", folder], check=True, capture_output=True)
        folder.chmod(0o700)
        mode = folder.stat()
        if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) != 0o700 or any(folder.iterdir()):
            raise OSError("Could not prepare a private release transaction")
        return folder
    except BaseException:
        folder.rmdir()
        raise


def package(build_dir, output_dir, version, arch):
    app = build_dir / "Screen Organizer.app"
    bundle = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if (bundle.get("HomeScreenOrganizerReleaseVersion", bundle["CFBundleShortVersionString"]) != version
            or bundle["CFBundleShortVersionString"] != version.split("-")[0]):
        raise ValueError("Build the app with the current project version; bundle version differs")
    if bundle.get("HomeScreenOrganizerArchitecture") != arch:
        raise ValueError("Build the app for the selected architecture")
    dmg = build_dir / f"home-screen-organizer-{version}.dmg"
    if not dmg.is_file():
        raise ValueError(f"Build the disk image first: {dmg.name}")
    from build_paths import read_roots, runner_roots
    from check_package_paths import check_archive

    if output_dir in (build_dir, *build_dir.parents) or build_dir in output_dir.parents:
        raise ValueError("Choose a separate directory for the release archives")
    roots = [ROOT, build_dir, output_dir, *read_roots(build_dir), *runner_roots()]
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    basename = f"home-screen-organizer-{version}-macos-{arch}"
    folder = private_transaction(output_dir.parent)
    committed = False
    try:
        staged = folder / "assets"
        staged.mkdir()
        disk_image = staged / (basename + ".dmg")
        archive = staged / (basename + ".zip")
        shutil.copyfile(dmg, disk_image)
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(app), str(archive)], check=True, capture_output=True)
        check_archive(archive, [*roots, staged])
        verify_image(disk_image, archive, Path(folder))
        lines = []
        for asset in (disk_image, archive):
            with asset.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            lines.append(f"{digest}  {asset.name}\n")
        (staged / "SHA256SUMS.txt").write_text("".join(lines))
        install_generation(staged, output_dir, arch)
        committed = True
    finally:
        # Retained entries can share an inode with the live generation and with another hardlink.
        # Ordinary unlink never resets an ACL or mode to force retirement.
        try:
            shutil.rmtree(folder)
        except OSError:
            if committed:
                print("Release installed. A protected previous file was retained privately.", file=sys.stderr)


def app_contents(app):
    entries = {Path("."): (app.lstat().st_mode, None)}
    for path in app.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            content = os.readlink(path)
        elif stat.S_ISDIR(mode):
            content = None
        elif stat.S_ISREG(mode):
            with path.open("rb") as stream:
                content = hashlib.file_digest(stream, "sha256").digest()
        else:
            raise ValueError("Check the app file types in the native release")
        entries[path.relative_to(app)] = (mode, content)
    return entries


def verify_image(image, archive, folder):
    unpacked = folder / "unpacked"
    mount = folder / "mount"
    mount.mkdir()
    subprocess.run(["ditto", "-x", "-k", archive, unpacked], check=True, capture_output=True)
    subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, image],
                   check=True, capture_output=True)
    try:
        app = "Screen Organizer.app"
        if {entry.name for entry in mount.iterdir()} != {app, "Applications"}:
            raise ValueError("Check the release disk image contents")
        if not (mount / "Applications").is_symlink() or os.readlink(mount / "Applications") != "/Applications":
            raise ValueError("Check the Applications link in the release image")
        for location in (unpacked, mount):
            if (location / app).is_symlink() or not (location / app).is_dir():
                raise ValueError("Check the app directory in the native release")
            subprocess.run(["codesign", "--verify", "--deep", "--strict", location / app],
                           check=True, capture_output=True)
        if app_contents(unpacked / app) != app_contents(mount / app):
            raise ValueError("The native ZIP and disk image contain different apps")
    finally:
        subprocess.run(["hdiutil", "detach", mount], check=True, capture_output=True)


def previous_assets(output, arch):
    manifest = output / "SHA256SUMS.txt"
    if not manifest.exists() and not manifest.is_symlink():
        return set()
    error = "Check the previous native release manifest"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError(error)
    pattern = re.compile(r"(home-screen-organizer-" + RELEASE_VERSION + r"-macos-" + arch + r")\.(zip|dmg)")
    names = set()
    bases = set()
    for row in manifest.read_text().splitlines():
        checksum, separator, name = row.partition("  ")
        match = pattern.fullmatch(name)
        if not separator or not match or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError(error)
        path = output / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(error)
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                raise ValueError(error)
        if name in names:
            raise ValueError(error)
        names.add(name)
        bases.add(match[1])
    if len(names) != 2 or len(bases) != 1:
        raise ValueError(error)
    return names


def preserve_entry(source, destination, libc):
    if source.is_symlink() or not source.is_dir():
        os.link(source, destination, follow_symlinks=False)
        return
    destination.mkdir()
    # Retained directories keep their own ACL, including an empty one.
    subprocess.run(["chmod", "-N", destination], check=True, capture_output=True)
    for child in source.iterdir():
        preserve_entry(child, destination / child.name, libc)
    # Set the directory's own restrictions after copying its existing children.
    if libc.copyfile(os.fsencode(source), os.fsencode(destination), None, 7) != 0:  # COPYFILE_METADATA
        raise OSError(ctypes.get_errno(), "Could not preserve the release directory metadata")


def install_generation(staged, output, arch):
    # Lock the parent directory while preserving sibling files and exchanging generations.
    parent_fd = os.open(output.parent, os.O_RDONLY)
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        assets = list(staged.iterdir())
        owned = previous_assets(output, arch)
        if owned:
            owned.add("SHA256SUMS.txt")
        for asset in assets:
            destination = output / asset.name
            if asset.name not in owned and os.path.lexists(destination):
                raise ValueError("An incoming native release name is occupied by an unowned entry")
        current_names = {asset.name for asset in assets} | owned
        replacement = staged / ".generation"
        libc = ctypes.CDLL(None, use_errno=True)
        existing = output.exists()
        if existing:
            replacement.mkdir()
            subprocess.run(["chmod", "-N", replacement], check=True, capture_output=True)
            # Darwin COPYFILE_SECURITY copies the directory stat and ACL before its children inherit them.
            if libc.copyfile(os.fsencode(output), os.fsencode(replacement), None, 3) != 0:
                raise OSError(ctypes.get_errno(), "Could not preserve the release directory permissions")
        else:
            # A new output inherits its parent's ACL before its files are assembled in private.
            with tempfile.TemporaryDirectory(dir=output.parent, prefix=".native-generation-") as folder:
                Path(folder).rename(replacement)
            replacement.chmod(stat.S_IMODE(staged.stat().st_mode))
        for asset in assets:
            shutil.copyfile(asset, replacement / asset.name)
        if not existing:
            replacement.rename(output)
            return
        for entry in output.iterdir():
            if entry.name in current_names:
                continue
            preserve_entry(entry, replacement / entry.name, libc)
        # Darwin RENAME_SWAP exchanges two populated directories on the same volume.
        if libc.renamex_np(os.fsencode(replacement), os.fsencode(output), 2) != 0:
            raise OSError(ctypes.get_errno(), "Could not install the verified release")
    finally:
        os.close(parent_fd)


def collect(input_dir, output_dir, version):
    verified = []
    for arch in ARCHITECTURES:
        folder = input_dir / f"home-screen-organizer-macos-{arch}"
        expected = {f"home-screen-organizer-{version}-macos-{arch}.{extension}"
                    for extension in ("dmg", "zip")}
        seen = set()
        for line in (folder / "SHA256SUMS.txt").read_text().splitlines():
            digest, name = line.split("  ")
            if name not in expected or name in seen or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"Check the {arch} release manifest")
            asset = folder / name
            with asset.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != digest:
                raise ValueError(f"Archive checksum differs: {name}")
            seen.add(name)
            verified.append((asset, digest))
        if seen != expected:
            raise ValueError(f"Supply both {arch} release archives")
    output_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for asset, digest in sorted(verified):
        shutil.copyfile(asset, output_dir / asset.name)
        lines.append(f"{digest}  {asset.name}\n")
    (output_dir / "SHA256SUMS.txt").write_text("".join(lines))


def verify_distribution(folder, version, repository, commit=None):
    bundle_name = f"home-screen-organizer-{version}-macos.mcpb"
    expected = {f"home-screen-organizer-{version}-macos-{arch}.{extension}"
                for arch in ARCHITECTURES for extension in ("dmg", "zip")}
    source_name = f"home-screen-organizer-{version}-corresponding-source.tar.gz"
    expected.update((bundle_name, source_name, "server.json"))
    seen = set()
    for line in (folder / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = line.split("  ")
        if name not in expected or name in seen or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Check the complete release checksum manifest")
        with (folder / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError(f"Archive checksum differs: {name}")
        seen.add(name)
    if seen != expected:
        raise ValueError("Supply checksums for all release assets")
    from source_archive import trusted_file, verify_tar
    source_manifest = verify_tar(folder / source_name, version, commit, release=True, root=ROOT, repository=repository)
    from mcp_bundle import verify_bundle
    revision = source_manifest["commit"]
    verify_bundle(folder / bundle_name, folder, version, repository, trusted_file(ROOT, revision, "LICENSE"),
                  trusted_file(ROOT, revision, "macapp/mcp_launcher.sh"))
    metadata = json.loads((folder / "server.json").read_text())
    with (folder / bundle_name).open("rb") as stream:
        bundle_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    package = {"registryType": "mcpb", "identifier": f"https://github.com/{repository}/releases/download/v{version}/{bundle_name}",
               "fileSha256": bundle_hash, "transport": {"type": "stdio"}}
    if (metadata["name"] != "io.github.worldsavepoint/home-screen-organizer"
            or metadata["version"] != version or metadata["packages"] != [package]):
        raise ValueError("Registry metadata differs from the release bundle, repository or version")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    version_command = commands.add_parser("version", help="Check the project version and release tag")
    version_command.add_argument("--tag")
    package_command = commands.add_parser("package", help="Create DMG, ZIP and SHA256 assets")
    package_command.add_argument("--tag")
    package_command.add_argument("--arch", choices=ARCHITECTURES, default=platform.machine())
    package_command.add_argument("--build-dir", type=Path)
    package_command.add_argument("--output-dir", type=Path)
    collect_command = commands.add_parser("collect", help="Verify and combine both architecture builds")
    collect_command.add_argument("--tag")
    collect_command.add_argument("--input-dir", type=Path, required=True)
    collect_command.add_argument("--output-dir", type=Path, required=True)
    verify_command = commands.add_parser("verify", help="Verify the complete standalone distribution and Registry metadata")
    verify_command.add_argument("--tag")
    verify_command.add_argument("--commit")
    verify_command.add_argument("--input-dir", type=Path, required=True)
    verify_command.add_argument("--repository", required=True)
    for action in ("localize", "check-localization"):
        localization_command = commands.add_parser(action, help="Assemble or check native language resources")
        localization_command.add_argument("--app", type=Path, required=True)
        localization_command.add_argument("--locales", type=Path, default=ROOT / "homescreen/web/locales")
    args = parser.parse_args()
    try:
        version = project_version(getattr(args, "tag", None))
        if args.command in ("localize", "check-localization"):
            localize(args.app, args.locales, check=args.command == "check-localization")
        elif args.command == "package":
            build_dir = args.build_dir or ROOT / "build" / args.arch
            output_dir = args.output_dir or ROOT / "build/release" / args.arch
            package(build_dir.resolve(), output_dir.resolve(), version, args.arch)
        elif args.command == "collect":
            collect(args.input_dir.resolve(), args.output_dir.resolve(), version)
        elif args.command == "verify":
            verify_distribution(args.input_dir.resolve(), version, args.repository, args.commit)
        print(version)
    except (OSError, subprocess.CalledProcessError, tarfile.TarError, zipfile.BadZipFile) as exc:
        if args.command == "package":
            parser.exit(1, "Could not prepare the verified native release\n")
        parser.exit(1, f"{exc}\n")
    except (ValueError, KeyError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
