"""Assemble versioned macOS release assets from a completed app build."""
import argparse
import base64
import ctypes
from contextlib import ExitStack
import errno
import fcntl
from functools import cached_property
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
    from tools.check_translations import product_display_name

    manifest = json.loads((locales / "manifest.json").read_text())
    codes = [language["code"] for language in manifest["languages"]]
    if (manifest.get("default") != "en" or "en" not in codes or len(codes) != len(set(codes))
            or any(not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)*", code) for code in codes)):
        raise ValueError("Check the language manifest before packaging")
    files = {"Localization/manifest.json": (locales / "manifest.json").read_bytes()}
    for code in codes:
        data = (locales / f"{code}.json").read_bytes()
        catalog = json.loads(data)
        expected = product_display_name(code)
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
    from homescreen.private_store import create_private_directory

    for _ in range(tempfile.TMP_MAX):
        folder = parent / (".native-release-" + uuid.uuid4().hex)
        try:
            create_private_directory(folder)
        except FileExistsError:
            continue
        return folder
    raise FileExistsError("Could not allocate a new private release transaction")


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
    retirement_error = None
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
    except OSError as error:
        if not publication.get("installed"):
            raise
        retirement_error = error if isinstance(error, _RetirementError) else _RetirementError(
            "Release installed. Could not complete private release cleanup.")
    finally:
        # Retained entries can share an inode with the live generation and with another hardlink.
        # Ordinary unlink never resets an ACL or mode to force retirement.
        retained = publication.get("retained")
        if retained is None or not retained.is_relative_to(folder):
            try:
                shutil.rmtree(folder)
            except OSError:
                if publication.get("installed") and retirement_error is None:
                    retirement_error = _RetirementError("Release installed. Could not complete private release cleanup.")
        if publication.get("installed"):
            messages = []
            record = publication.get("permissions_record")
            if retained is not None:
                if not _recovery_still_named(*publication["retained_binding"]):
                    messages.append("Could not confirm the location of the remaining previous directory.")
                    retirement_error = retirement_error or _RetirementError("Release installed. Recovery is incomplete.")
                elif record:
                    messages.append(f"The remaining previous directory was retained in {retained}.")
                else:
                    messages.append(f"Previous files were retained in {retained}.")
            if record:
                if _recovery_still_named(record, *publication["permissions_binding"], private_record=True,
                                         payload=publication["permissions_payload"]):
                    messages.append(f"Previous directory permissions were saved in {record}.")
                else:
                    messages.append("Could not verify the saved directory permissions.")
                    retirement_error = retirement_error or _RetirementError("Release installed. Recovery is incomplete.")
            if retirement_error:
                raise _RetirementError(" ".join([str(retirement_error), *messages])) from None
            if messages:
                print("Release installed.", *messages, file=sys.stderr)


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
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        raise _DirectoryChanged("The retained release directory changed") from None
    if entry_identity(current) != identity:
        raise _DirectoryChanged("The retained release directory changed")


def _recovery_still_named(path, parent_identity, identity, *, private_record=False, payload=None):
    def private_parent(info):
        return stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.geteuid()

    try:
        with ExitStack() as stack:
            parent, entry = path.parent.lstat(), path.lstat()
            if entry_identity(parent) != parent_identity or entry_identity(entry) != identity:
                return False
            if private_record and (not private_parent(parent) or not stat.S_ISREG(entry.st_mode)
                                   or stat.S_IMODE(entry.st_mode) != 0o600
                                   or entry.st_nlink != 1 or entry.st_uid != os.geteuid()):
                return False
            if payload is None and not private_record:
                return True
            if payload is not None and entry.st_size != len(payload):
                return False
            directory = None
            if private_record:
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                stack.callback(os.close, directory)
                opened_parent = os.fstat(directory)
                if not os.path.samestat(opened_parent, parent) or not private_parent(opened_parent):
                    return False
            fd = os.open(path.name if private_record else path,
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
            stream = stack.enter_context(os.fdopen(fd, "rb", buffering=0))
            opened = os.fstat(fd)
            if entry_identity(opened) != identity or asset_state(opened) != asset_state(entry):
                return False
            if payload is not None and stream.read(len(payload) + 1) != payload:
                return False
            if private_record and sys.platform == "darwin":
                from homescreen.private_store import _check_darwin_acl
                _check_darwin_acl(directory)
                _check_darwin_acl(fd)
            named = path.lstat()
            if (not os.path.samestat(named, opened) or asset_state(named) != asset_state(opened)
                    or asset_state(os.fstat(fd)) != asset_state(opened)):
                return False
            final_parent = path.parent.lstat()
            final_open_parent = os.fstat(directory) if private_record else None
            return (os.path.samestat(final_parent, parent)
                    and (not private_record or private_parent(final_parent)
                         and os.path.samestat(final_parent, final_open_parent)
                         and private_parent(final_open_parent)
                         and asset_state(final_open_parent) == asset_state(opened_parent)))
    except OSError:
        return False


def _reserve_permissions_record(directory, name, stack):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600, dir_fd=directory)
    stream = stack.enter_context(os.fdopen(fd, "wb"))
    if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
        os.fchmod(fd, 0o600)
    return stream


def directory_permissions(fd, libc):
    info = os.fstat(fd)
    acl = libc.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED
    encoded = b""
    if acl:
        try:
            size = libc.acl_size(acl)
            if size < 0:
                raise OSError(ctypes.get_errno(), "Could not read the release directory permissions")
            data = ctypes.create_string_buffer(size)
            if libc.acl_copy_ext(data, acl, size) != size:
                raise OSError(ctypes.get_errno(), "Could not read the release directory permissions")
            encoded = data.raw
        finally:
            libc.acl_free(acl)
    elif ctypes.get_errno() != errno.ENOENT:
        raise OSError(ctypes.get_errno(), "Could not read the release directory permissions")
    return stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid, encoded


class _DirectoryChanged(OSError):
    pass


class _RemovedDirectoryChanged(_DirectoryChanged):
    def __init__(self, directory, permissions):
        super().__init__("The removed directory's permissions changed")
        self.directory = directory
        self.permissions = permissions

    @cached_property
    def payload(self):
        mode, uid, gid, encoded = self.permissions
        info = os.fstat(self.directory.source)
        metadata = {"directory": str(self.directory.relative), "device": info.st_dev, "inode": info.st_ino,
                    "uid": uid, "gid": gid, "mode": mode, "aclFormat": "Darwin acl_copy_ext",
                    "aclBase64": base64.b64encode(encoded).decode("ascii")}
        return (json.dumps(metadata) + "\n").encode("utf-8")

    def save(self, record):
        record.write(self.payload)
        record.flush()
        os.fsync(record.fileno())


class _RetirementError(_DirectoryChanged):
    pass


def set_directory_permissions(fd, current, wanted, libc):
    mode, uid, gid, encoded = wanted
    ownership_changed = (uid, gid) != current[1:3]
    if ownership_changed:
        os.fchown(fd, uid if uid != current[1] else -1, gid if gid != current[2] else -1)
    if ownership_changed or mode != current[0]:
        os.fchmod(fd, mode)
    if encoded != current[3]:
        acl = libc.acl_copy_int(encoded) if encoded else libc.acl_init(0)
        if not acl:
            raise OSError(ctypes.get_errno(), "Could not read the saved directory ACL")
        try:
            if libc.acl_set_fd_np(fd, acl, 0x100):
                raise OSError(ctypes.get_errno(), "Could not set the release directory ACL")
        finally:
            libc.acl_free(acl)


class _DirectoryCopy:
    def __init__(self, source, destination, source_entry, destination_entry, parent, libc):
        self.source, self.destination = source, destination
        self.source_entry, self.destination_entry = source_entry, destination_entry
        self.parent = parent
        self.relative = parent.relative / source_entry[1] if parent else Path(".")
        self.permissions = directory_permissions(source, libc)
        self.names = set()

    def check_bindings(self, *, removed=False):
        if self.parent:
            self.parent.check_bindings()
        if not removed:
            check_preserved_entry(*self.source_entry, entry_identity(os.fstat(self.source)))
        check_preserved_entry(*self.destination_entry, entry_identity(os.fstat(self.destination)))

    def reconcile(self, libc, source, *, removed=False):
        self.check_bindings(removed=removed)
        current = directory_permissions(self.destination, libc)
        if current == self.permissions and directory_permissions(self.source, libc) == source:
            set_directory_permissions(self.destination, current, source, libc)
            self.check_bindings(removed=removed)
            if directory_permissions(self.destination, libc) != directory_permissions(self.source, libc):
                raise _DirectoryChanged("The retained release directory permissions changed again")

    def check_removed(self, libc):
        try:
            permissions = directory_permissions(self.source, libc)
        except OSError as error:
            raise _RetirementError("Release installed. Could not read the removed directory's permissions.") from error
        if permissions != self.permissions:
            raise _RemovedDirectoryChanged(self, permissions)

    def check(self, libc, *, installed=False):
        self.check_bindings()
        source = directory_permissions(self.source, libc)
        destination = directory_permissions(self.destination, libc)
        if source == destination == self.permissions:
            return
        if installed and source != self.permissions and destination == self.permissions:
            # Reconcile only the bound copy while it still has the permissions we installed.
            self.reconcile(libc, source)
        # Even a reconciled change keeps the original generation.
        raise _DirectoryChanged("The retained release directory permissions changed")


def preserve_entry(name, parent, libc, stack, checks, directories):
    source_parent, destination_parent = parent.source, parent.destination
    identity = entry_identity(os.stat(name, dir_fd=source_parent, follow_symlinks=False))
    entry = {"identity": identity}
    checks.append((source_parent, name, identity))
    if identity[2] != stat.S_IFDIR:
        os.link(name, name, src_dir_fd=source_parent, dst_dir_fd=destination_parent, follow_symlinks=False)
        check_preserved_entry(destination_parent, name, identity)
        checks.append((destination_parent, name, identity))
        return entry
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    source = os.open(name, flags, dir_fd=source_parent)
    stack.callback(os.close, source)
    if entry_identity(os.fstat(source)) != identity:
        raise OSError("The retained release directory changed")
    os.mkdir(name, dir_fd=destination_parent)
    destination = os.open(name, flags, dir_fd=destination_parent)
    stack.callback(os.close, destination)
    directory = _DirectoryCopy(source, destination, (source_parent, name), (destination_parent, name), parent, libc)
    directories.append(directory)
    entry["directory"] = directory
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
    directory.names = set(children)
    entry["children"] = {}
    for child in children:
        entry["children"][child] = preserve_entry(child, directory, libc, stack, checks, directories)
    check_preserved_entry(source_parent, name, identity)
    # Set the directory's own restrictions after copying its existing children.
    if libc.fcopyfile(source, destination, None, 7) != 0:  # COPYFILE_METADATA
        raise OSError(ctypes.get_errno(), "Could not preserve the release directory metadata")
    directory.check(libc)
    return entry


def retire_entry(parent, name, expected, libc, containing):
    containing.check(libc, installed=True)
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
    retained = expected.get("directory")
    if retained is not None:
        retained.source_entry = (parent, claimed)
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
                    retire_entry(directory, child, expected["children"].get(child), libc, retained)
                retained.check(libc, installed=True)
            finally:
                os.close(directory)
            # A child saved at the final syscall boundary prevents removal.
            os.rmdir(claimed, dir_fd=parent)
            retained.check_removed(libc)
    except _DirectoryChanged:
        raise
    except OSError:
        pass
    finally:
        # A changed or protected object keeps its original private name when available.
        libc.renameatx_np(parent, claimed, parent, name, 4)
        if retained is not None:
            retained.source_entry = (parent, name)


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
        libc.acl_copy_int.argtypes = [ctypes.c_char_p]
        libc.acl_copy_int.restype = ctypes.c_void_p
        libc.acl_get_fd_np.restype = ctypes.c_void_p
        libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.acl_size.argtypes = [ctypes.c_void_p]
        libc.acl_size.restype = ctypes.c_ssize_t
        libc.acl_copy_ext.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t]
        libc.acl_copy_ext.restype = ctypes.c_ssize_t
        libc.acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        libc.acl_free.argtypes = [ctypes.c_void_p]
        checks = []
        if existing:
            replacement.mkdir()
            subprocess.run(["chmod", "-N", replacement], check=True, capture_output=True)
            replacement_fd = os.open(".generation", flags, dir_fd=staged_fd)
            stack.callback(os.close, replacement_fd)
            root = _DirectoryCopy(output_fd, replacement_fd, (parent_fd, output.name),
                                  (staged_fd, ".generation"), None, libc)
            directories = [root]
            # Darwin COPYFILE_SECURITY copies the directory stat and ACL before its children inherit them.
            if libc.fcopyfile(output_fd, replacement_fd, None, 3) != 0:
                raise OSError(ctypes.get_errno(), "Could not preserve the release directory permissions")
            root.check(libc)
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
        root.names = set(names)
        retired = {name: getattr(owned, "verified", {}).get(name) for name in names}
        for name in names:
            if name in current_names:
                continue
            retired[name] = preserve_entry(name, root, libc, stack, checks, directories)
        for parent, name, identity in checks:
            check_preserved_entry(parent, name, identity)
        for directory in directories:
            if set(os.listdir(directory.source)) != directory.names:
                raise OSError("The retained release directory changed")
            directory.check(libc)
        previous_identity = entry_identity(os.fstat(output_fd))
        retained_binding = (replacement, entry_identity(os.fstat(staged_fd)), previous_identity)
        # Once Darwin RENAME_SWAP succeeds, the caller must protect the named previous generation.
        if libc.renameatx_np(staged_fd, b".generation", parent_fd, os.fsencode(output.name), 2) != 0:
            raise OSError(ctypes.get_errno(), "Could not install the verified release")
        publication.update(installed=True, retained=replacement, retained_binding=retained_binding)
        root.source_entry, root.destination_entry = root.destination_entry, root.source_entry
        protected = None
        protected_fd = None
        permission_record = None
        record = None
        record_saved = False
        recovery_error = None
        try:
            changed = False
            for directory in directories:
                try:
                    directory.check(libc, installed=True)
                except _DirectoryChanged:
                    changed = True
                except OSError as error:
                    raise _RetirementError("Release installed. Could not verify the previous release directory.") from error
            if changed:
                raise _DirectoryChanged("The retained release directory changed")
            # Reserve a private recovery name before the last link to a directory can be removed.
            protected = private_transaction(output.parent)
            protected_fd = os.open(protected.name, flags, dir_fd=parent_fd)
            stack.callback(os.close, protected_fd)
            parent_identity = entry_identity(os.fstat(protected_fd))
            permission_record = protected / "directory-permissions.json"
            record = _reserve_permissions_record(protected_fd, permission_record.name, stack)
            try:
                for name in os.listdir(output_fd):
                    retire_entry(output_fd, name, retired.get(name), libc, root)
                root.check(libc, installed=True)
                os.rmdir(".generation", dir_fd=staged_fd)
                publication["retained"] = None
                root.check_removed(libc)
            except _RemovedDirectoryChanged as change:
                try:
                    # If the reservation moved, use one fresh private name for the held permissions.
                    for attempt in range(2):
                        if attempt:
                            check_preserved_entry(parent_fd, protected.name, parent_identity)
                            fresh = protected / ("directory-permissions-" + uuid.uuid4().hex + ".json")
                            record = _reserve_permissions_record(protected_fd, fresh.name, stack)
                            permission_record = fresh
                            record_saved = False
                        identity = entry_identity(os.fstat(record.fileno()))
                        if _recovery_still_named(permission_record, parent_identity, identity, private_record=True):
                            change.save(record)
                            record_saved = True
                            if _recovery_still_named(permission_record, parent_identity, identity, private_record=True,
                                                     payload=change.payload):
                                break
                            try:
                                check_preserved_entry(protected_fd, permission_record.name, identity)
                            except _DirectoryChanged:
                                continue
                            raise _RetirementError("Release installed. Could not verify the saved directory permissions.")
                    else:
                        raise _RetirementError("Release installed. Could not confirm the saved directory permissions location.")
                except _RetirementError:
                    raise
                except OSError as error:
                    raise _RetirementError("Release installed. Could not save the removed directory's permissions.") from error
                publication.update(permissions_record=permission_record, permissions_binding=(parent_identity, identity),
                                   permissions_payload=change.payload)
                try:
                    os.fsync(protected_fd)
                except OSError:
                    recovery_error = _RetirementError("Release installed. Could not confirm that the saved directory permissions would survive a crash.")
                change.directory.reconcile(libc, change.permissions, removed=True)
                raise
        except OSError as error:
            if isinstance(error, _RetirementError):
                recovery_error = error
            # Keep protected data outside any caller-owned temporary cleanup envelope.
            if publication["retained"] is not None:
                try:
                    check_preserved_entry(staged_fd, ".generation", previous_identity)
                    if protected is None:
                        protected = private_transaction(output.parent)
                    if protected_fd is None:
                        protected_fd = os.open(protected.name, flags, dir_fd=parent_fd)
                        stack.callback(os.close, protected_fd)
                    parent_identity = entry_identity(os.fstat(protected_fd))
                    check_preserved_entry(parent_fd, protected.name, parent_identity)
                    if libc.renameatx_np(staged_fd, b".generation", protected_fd, b"previous", 4):  # RENAME_EXCL
                        raise OSError(ctypes.get_errno(), "Could not move the previous release directory")
                    publication["retained"] = protected
                    publication["retained_binding"] = (protected / "previous", parent_identity, previous_identity)
                except OSError:
                    # Publication completed; the original private container remains protected.
                    pass
        finally:
            try:
                if record is not None and not record_saved:
                    identity = entry_identity(os.fstat(record.fileno()))
                    if _recovery_still_named(permission_record, parent_identity, identity, private_record=True):
                        os.unlink(permission_record.name, dir_fd=protected_fd)
                if protected_fd is not None and protected != publication["retained"] and not record_saved:
                    check_preserved_entry(parent_fd, protected.name, entry_identity(os.fstat(protected_fd)))
                    os.rmdir(protected.name, dir_fd=parent_fd)
            except OSError:
                recovery_error = recovery_error or _RetirementError("Release installed. Could not complete private release cleanup.")
        if recovery_error is not None:
            raise recovery_error


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
    except _RetirementError as exc:
        parser.exit(1, f"{exc}\n")
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
    sys.path.insert(0, str(ROOT))
    main()
