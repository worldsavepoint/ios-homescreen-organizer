"""Assemble versioned macOS release assets from a completed app build."""
import argparse
import ctypes
from contextlib import ExitStack
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
import uuid

ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURES = ("arm64", "x86_64")


RELEASE_VERSION = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-(?:alpha|beta|rc)\.(?:0|[1-9]\d*))?"


def project_version(tag=None, *, root=ROOT):
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
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


def check_tree_metadata(root):
    if any(path.name == "__MACOSX" or path.name.startswith("._") for path in (root, *root.rglob("*"))):
        raise ValueError("Native payload contains provenance metadata members")
    result = subprocess.run(["/usr/bin/xattr", "-rs", root], check=True, capture_output=True)
    if result.stdout:
        raise ValueError("Native payload contains extended metadata")


def clean_copy(app, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ditto", "--norsrc", "--noextattr", "--noqtn", "--noacl", "--nopersistRootless",
                    app, destination], check=True, capture_output=True)
    check_tree_metadata(destination)


def native_zip(app, destination):
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in (app, *sorted(app.rglob("*"))):
            mode = path.lstat().st_mode
            name = path.relative_to(app.parent).as_posix()
            if stat.S_ISDIR(mode):
                name += "/"
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            if stat.S_ISLNK(mode):
                archive.writestr(info, os.fsencode(os.readlink(path)))
            elif stat.S_ISDIR(mode):
                archive.writestr(info, b"")
            elif stat.S_ISREG(mode):
                with path.open("rb") as source, archive.open(info, "w", force_zip64=path.stat().st_size >= zipfile.ZIP64_LIMIT) as output:
                    shutil.copyfileobj(source, output)
            else:
                raise ValueError("Check the app file types in the native release")


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
    publication = {}
    try:
        staged = folder / "assets"
        staged.mkdir()
        disk_image = staged / (basename + ".dmg")
        archive = staged / (basename + ".zip")
        shutil.copyfile(dmg, disk_image)
        clean = folder / "clean" / app.name
        clean_copy(app, clean)
        native_zip(clean, archive)
        check_archive(archive, [*roots, staged])
        verify_image(disk_image, archive, Path(folder))
        lines = []
        for asset in (disk_image, archive):
            with asset.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            lines.append(f"{digest}  {asset.name}\n")
        (staged / "SHA256SUMS.txt").write_text("".join(lines))
        install_generation(staged, output_dir, arch, publication=publication)
    finally:
        # Retained entries can share an inode with the live generation and with another hardlink.
        # Ordinary unlink never resets an ACL or mode to force retirement.
        retained = publication.get("retained")
        if retained is None or not retained.is_relative_to(folder):
            try:
                shutil.rmtree(folder)
            except OSError:
                if publication.get("installed") and retained is None:
                    retained = folder
        if publication.get("installed") and retained is not None:
            print(f"Release installed. Previous files were retained in {retained}.", file=sys.stderr)


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


def image_error_note(error, image, mount):
    stderr = error.stderr
    detail = stderr[:4096].decode(errors="replace") if isinstance(stderr, bytes) else (stderr or "")[:4096]
    detail = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", detail)
    detail = "".join(character if character.isprintable() or character in "\r\n\t" else "?" for character in detail)
    roots = [image, image.parent, mount, mount.parent, ROOT, Path.home(), Path(tempfile.gettempdir())]
    forms = set()
    for root in roots:
        forms.add(str(root.absolute()))
        try:
            forms.add(str(root.resolve()))
        except (OSError, ValueError, RuntimeError):
            pass
    for form in sorted(forms, key=len, reverse=True):
        detail = detail.replace(form, "<native-build>")
    detail = re.sub(r"(?m)(?:[A-Za-z]:[\\/]|/)[^\r\n]*", "<path>", detail)
    detail = " ".join(detail.split())[:512]
    return "Image attachment failed. hdiutil stderr: " + (detail or "<empty>")


def verify_image(image, archive, folder):
    unpacked = folder / "unpacked"
    mount = folder / "mount"
    mount.mkdir()
    subprocess.run(["ditto", "-x", "-k", archive, unpacked], check=True, capture_output=True)
    try:
        subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, image],
                       check=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        error.add_note(image_error_note(error, image, mount))
        raise
    try:
        app = "Screen Organizer.app"
        if {entry.name for entry in mount.iterdir()} != {app, "Applications"}:
            raise ValueError("Check the release disk image contents")
        if not (mount / "Applications").is_symlink() or os.readlink(mount / "Applications") != "/Applications":
            raise ValueError("Check the Applications link in the release image")
        for location in (unpacked, mount):
            check_tree_metadata(location)
            if (location / app).is_symlink() or not (location / app).is_dir():
                raise ValueError("Check the app directory in the native release")
            subprocess.run(["codesign", "--verify", "--deep", "--strict", location / app],
                           check=True, capture_output=True)
        if app_contents(unpacked / app) != app_contents(mount / app):
            raise ValueError("The native ZIP and disk image contain different apps")
    finally:
        subprocess.run(["hdiutil", "detach", mount], check=True, capture_output=True)


class _VerifiedAssets(set):
    def __init__(self, names=(), verified=None):
        super().__init__(names)
        self.verified = verified or {}


def asset_state(info):
    return info.st_mode, info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def previous_assets(output, arch):
    error = "Check the previous native release manifest"
    pattern = re.compile(r"(home-screen-organizer-" + RELEASE_VERSION + r"-macos-" + arch + r")\.(zip|dmg)")
    names, bases, verified = set(), set(), {}
    try:
        directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return _VerifiedAssets()
    try:
        def read(name, checksum=None, *, contents=False):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError(error)
                os.set_blocking(stream.fileno(), True)
                data = stream.read() if contents else None
                value = hashlib.sha256(data).hexdigest() if contents else hashlib.file_digest(stream, "sha256").hexdigest()
                after = os.fstat(stream.fileno())
                if asset_state(before) != asset_state(after) or checksum is not None and value != checksum:
                    raise ValueError(error)
                verified[name] = {"identity": entry_identity(after), "verified_state": asset_state(after), "sha256": value}
                return data
        try:
            manifest = read("SHA256SUMS.txt", contents=True)
        except FileNotFoundError:
            return _VerifiedAssets()
        for row in manifest.decode("utf-8").splitlines():
            checksum, separator, name = row.partition("  ")
            match = pattern.fullmatch(name)
            if not separator or not match or not re.fullmatch(r"[0-9a-f]{64}", checksum) or name in names:
                raise ValueError(error)
            read(name, checksum)
            names.add(name)
            bases.add(match[1])
        if len(names) != 2 or len(bases) != 1:
            raise ValueError(error)
        return _VerifiedAssets(names, verified)
    except (OSError, UnicodeError):
        raise ValueError(error) from None
    finally:
        os.close(directory)


def entry_identity(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def check_preserved_entry(parent, name, identity):
    if entry_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity:
        raise OSError("The retained release directory changed")


def preserve_entry(name, source_parent, destination_parent, libc, stack, checks, directories):
    identity = entry_identity(os.stat(name, dir_fd=source_parent, follow_symlinks=False))
    entry = {"identity": identity}
    checks.append((source_parent, name, identity))
    if identity[2] != stat.S_IFDIR:
        os.link(name, name, src_dir_fd=source_parent, dst_dir_fd=destination_parent, follow_symlinks=False)
        check_preserved_entry(destination_parent, name, identity)
        return entry
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    source = os.open(name, flags, dir_fd=source_parent)
    stack.callback(os.close, source)
    if entry_identity(os.fstat(source)) != identity:
        raise OSError("The retained release directory changed")
    os.mkdir(name, dir_fd=destination_parent)
    destination = os.open(name, flags, dir_fd=destination_parent)
    stack.callback(os.close, destination)
    # New directories receive the retained ACL after their children are linked.
    acl = libc.acl_init(0)
    if not acl:
        raise OSError(ctypes.get_errno(), "Could not preserve the release directory permissions")
    try:
        if libc.acl_set_fd_np(destination, acl, 0x100) != 0:  # ACL_TYPE_EXTENDED
            raise OSError(ctypes.get_errno(), "Could not preserve the release directory permissions")
    finally:
        libc.acl_free(acl)
    children = os.listdir(source)
    directories.append((source, set(children)))
    entry["children"] = {}
    for child in children:
        entry["children"][child] = preserve_entry(child, source, destination, libc, stack, checks, directories)
    check_preserved_entry(source_parent, name, identity)
    # Set the directory's own restrictions after copying its existing children.
    if libc.fcopyfile(source, destination, None, 7) != 0:  # COPYFILE_METADATA
        raise OSError(ctypes.get_errno(), "Could not preserve the release directory metadata")
    return entry


def retire_entry(parent, name, expected, libc):
    if expected is None:
        return
    if "verified_state" in expected:
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            return
        if entry_identity(current) != expected["identity"] or asset_state(current) != expected["verified_state"]:
            return
    claimed = os.fsencode(".retired-" + uuid.uuid4().hex)
    name = os.fsencode(name)
    if libc.renameatx_np(parent, name, parent, claimed, 4) != 0:  # RENAME_EXCL
        return
    try:
        info = os.stat(claimed, dir_fd=parent, follow_symlinks=False)
        if entry_identity(info) != expected["identity"]:
            return
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            if "sha256" in expected:
                fd = os.open(claimed, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    # Claiming changes ctime; content and the other verified fields must still match.
                    if (entry_identity(before) != expected["identity"]
                            or asset_state(before)[:-1] != expected["verified_state"][:-1]):
                        return
                    os.set_blocking(stream.fileno(), True)
                    value = hashlib.file_digest(stream, "sha256").hexdigest()
                    if value != expected["sha256"] or asset_state(before) != asset_state(os.fstat(stream.fileno())):
                        return
            os.unlink(claimed, dir_fd=parent)
        elif stat.S_ISDIR(info.st_mode) and "children" in expected:
            directory = os.open(claimed, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            try:
                if entry_identity(os.fstat(directory)) != expected["identity"]:
                    return
                for child in os.listdir(directory):
                    retire_entry(directory, child, expected["children"].get(child), libc)
            finally:
                os.close(directory)
            # A child saved at the final syscall boundary prevents removal.
            os.rmdir(claimed, dir_fd=parent)
    except OSError:
        pass
    finally:
        # A changed or protected object keeps its original private name when available.
        libc.renameatx_np(parent, claimed, parent, name, 4)


def install_generation(staged, output, arch, *, publication=None):
    # Lock the parent directory while preserving sibling files and exchanging generations.
    publication = publication if publication is not None else {}
    publication.update(installed=False, retained=None)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        parent_fd = os.open(output.parent, flags)
        stack.callback(os.close, parent_fd)
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        staged_fd = os.open(staged, flags)
        stack.callback(os.close, staged_fd)
        try:
            output_fd = os.open(output.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            output_fd = None
        if output_fd is not None:
            stack.callback(os.close, output_fd)
        existing = output_fd is not None
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
        libc.acl_init.restype = ctypes.c_void_p
        libc.acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        libc.acl_free.argtypes = [ctypes.c_void_p]
        checks = []
        if existing:
            checks.append((parent_fd, output.name, entry_identity(os.fstat(output_fd))))
            replacement.mkdir()
            subprocess.run(["chmod", "-N", replacement], check=True, capture_output=True)
            replacement_fd = os.open(".generation", flags, dir_fd=staged_fd)
            stack.callback(os.close, replacement_fd)
            # Darwin COPYFILE_SECURITY copies the directory stat and ACL before its children inherit them.
            if libc.fcopyfile(output_fd, replacement_fd, None, 3) != 0:
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
            publication["installed"] = True
            return
        names = os.listdir(output_fd)
        directories = [(output_fd, set(names))]
        retired = {name: getattr(owned, "verified", {}).get(name) for name in names}
        for name in names:
            if name in current_names:
                continue
            retired[name] = preserve_entry(name, output_fd, replacement_fd, libc, stack, checks, directories)
        for parent, name, identity in checks:
            check_preserved_entry(parent, name, identity)
        if any(set(os.listdir(directory)) != names for directory, names in directories):
            raise OSError("The retained release directory changed")
        # Darwin RENAME_SWAP exchanges two populated directories on the same volume.
        if libc.renameatx_np(staged_fd, b".generation", parent_fd, os.fsencode(output.name), 2) != 0:
            raise OSError(ctypes.get_errno(), "Could not install the verified release")
        publication.update(installed=True, retained=replacement)
        try:
            for name in os.listdir(output_fd):
                retire_entry(output_fd, name, retired.get(name), libc)
            os.rmdir(".generation", dir_fd=staged_fd)
            publication["retained"] = None
        except OSError:
            # Keep protected data outside any caller-owned temporary cleanup envelope.
            try:
                protected = private_transaction(output.parent)
                protected_fd = os.open(protected, flags)
                try:
                    os.rename(".generation", "previous", src_dir_fd=staged_fd, dst_dir_fd=protected_fd)
                    publication["retained"] = protected
                finally:
                    os.close(protected_fd)
            except OSError:
                # Publication completed; the original private container remains protected.
                pass


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
    copy_command = commands.add_parser("clean-copy", help="Stage an app without filesystem provenance metadata")
    copy_command.add_argument("--app", type=Path, required=True)
    copy_command.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        version = project_version(getattr(args, "tag", None))
        if args.command == "clean-copy":
            clean_copy(args.app, args.destination)
        elif args.command in ("localize", "check-localization"):
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
        from check_package_paths import BuildPathError
        if isinstance(exc, BuildPathError):
            for note in getattr(exc, "__notes__", ()):
                print(note, file=sys.stderr)
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
