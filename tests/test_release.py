"""Versioned release assets preserve app contents and executable permissions."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tomllib
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "macapp/release.py"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def release(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def test_matching_version_tag():
    result = release("version", "--tag", f"v{VERSION}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == VERSION


@pytest.mark.parametrize("tag", ["v99.0.0", "v1.1.1/extra", "1.1.1"])
def test_tag_mismatch_stops_packaging(tag):
    result = release("version", "--tag", tag)
    assert result.returncode != 0
    assert "tag" in result.stderr.lower()


def make_build(tmp_path, version=VERSION, arch="arm64"):
    build = tmp_path / "build with spaces"
    app = build / "Screen Organizer.app"
    executable = app / "Contents/MacOS/HomeScreenOrganizer"
    executable.parent.mkdir(parents=True)
    shutil.copyfile("/usr/bin/true", executable)
    executable.chmod(0o755)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "org.example.release-fixture",
        "CFBundleExecutable": executable.name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version.split("-")[0],
        "CFBundleVersion": version.split("-")[0],
        "HomeScreenOrganizerReleaseVersion": version,
        "HomeScreenOrganizerArchitecture": arch,
    }))
    from test_package_privacy import native_zip
    from macapp.build_paths import write_record

    native_zip(tmp_path)
    engine = app / "Contents/Resources/engine/engine"
    engine.parent.mkdir(parents=True)
    engine.write_bytes((tmp_path / "engine").read_bytes())
    write_record(build, [build])
    result = release("localize", "--app", app)
    assert result.returncode == 0, result.stderr
    make_image(app, build / f"home-screen-organizer-{version}.dmg", tmp_path / "image-files")
    return build


def make_image(app, image, folder):
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app], check=True, capture_output=True)
    folder.mkdir(exist_ok=True)
    copy = folder / app.name
    if copy.exists():
        shutil.rmtree(copy)
    subprocess.run(["ditto", app, copy], check=True)
    if not (folder / "Applications").is_symlink():
        (folder / "Applications").symlink_to("/Applications")
    subprocess.run(["hdiutil", "create", "-quiet", "-ov", "-format", "UDZO", "-srcfolder", folder, image],
                   check=True, capture_output=True)


@pytest.mark.parametrize("arch", ["arm64", "x86_64"])
def test_release_archives_and_manifest(tmp_path, arch):
    build = make_build(tmp_path, arch=arch)
    output = tmp_path / "release assets"
    result = release("package", "--build-dir", build, "--output-dir", output,
                     "--tag", f"v{VERSION}", "--arch", arch)
    assert result.returncode == 0, result.stderr
    dmg = f"home-screen-organizer-{VERSION}-macos-{arch}.dmg"
    archive = f"home-screen-organizer-{VERSION}-macos-{arch}.zip"
    assert sorted(p.name for p in output.iterdir()) == sorted([dmg, archive, "SHA256SUMS.txt"])
    assert (output / dmg).read_bytes() == (build / f"home-screen-organizer-{VERSION}.dmg").read_bytes()
    with zipfile.ZipFile(output / archive) as zipped:
        info = zipped.getinfo("Screen Organizer.app/Contents/MacOS/HomeScreenOrganizer")
        assert zipped.read(info) == (build / info.filename).read_bytes()
        assert (info.external_attr >> 16) & 0o111
        source = ROOT / "homescreen/web/locales"
        manifest = json.loads((source / "manifest.json").read_text())
        resources = "Screen Organizer.app/Contents/Resources/"
        for language in manifest["languages"]:
            code = language["code"]
            assert zipped.read(resources + f"Localization/{code}.json") == (source / f"{code}.json").read_bytes()
            strings = plistlib.loads(zipped.read(resources + f"{code}.lproj/InfoPlist.strings"))
            assert strings["CFBundleDisplayName"] == json.loads((source / f"{code}.json").read_text())["app.name"]
    lines = (output / "SHA256SUMS.txt").read_text().splitlines()
    assert len(lines) == 2
    for line, name in zip(lines, [dmg, archive]):
        checksum, filename = line.split("  ")
        assert filename == name
        assert checksum == hashlib.sha256((output / name).read_bytes()).hexdigest()


def test_bundle_version_mismatch_stops_packaging(tmp_path):
    build = make_build(tmp_path, version="0.0.0")
    output = tmp_path / "release"
    result = release("package", "--build-dir", build, "--output-dir", output, "--arch", "arm64")
    assert result.returncode != 0
    assert "bundle version" in result.stderr.lower()
    assert not output.exists()


def test_prerelease_keeps_tag_version_with_numeric_bundle_version(tmp_path):
    version = "1.2.0-beta.1"
    build = make_build(tmp_path, version=version)
    project = tmp_path / "project"
    script = project / "macapp/release.py"
    script.parent.mkdir(parents=True)
    for name in ("release.py", "check_package_paths.py", "build_paths.py"):
        (script.parent / name).write_bytes((SCRIPT.parent / name).read_bytes())
    (project / "pyproject.toml").write_text('[project]\nversion = "1.2.0-beta.1"\n')
    output = tmp_path / "prerelease"
    result = subprocess.run([sys.executable, str(script), "package", "--build-dir", str(build),
                             "--output-dir", str(output), "--tag", "v1.2.0-beta.1", "--arch", "arm64"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (output / "home-screen-organizer-1.2.0-beta.1-macos-arm64.dmg").read_bytes() == (build / f"home-screen-organizer-{version}.dmg").read_bytes()
    assert (output / "home-screen-organizer-1.2.0-beta.1-macos-arm64.zip").is_file()


def test_architecture_label_matches_bundle(tmp_path):
    build = make_build(tmp_path, arch="arm64")
    result = release("package", "--build-dir", build, "--output-dir", tmp_path / "release",
                     "--arch", "x86_64")
    assert result.returncode != 0
    assert "architecture" in result.stderr.lower()


def artifact_set(tmp_path):
    incoming = tmp_path / "incoming"
    for arch in ("arm64", "x86_64"):
        folder = incoming / f"home-screen-organizer-macos-{arch}"
        folder.mkdir(parents=True)
        lines = []
        for extension in ("dmg", "zip"):
            name = f"home-screen-organizer-{VERSION}-macos-{arch}.{extension}"
            data = f"{arch} {extension}".encode()
            (folder / name).write_bytes(data)
            lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        (folder / "SHA256SUMS.txt").write_text("".join(lines))
    return incoming


def test_collect_combines_verified_architectures(tmp_path):
    incoming = artifact_set(tmp_path)
    output = tmp_path / "release"
    result = release("collect", "--input-dir", incoming, "--output-dir", output)
    assert result.returncode == 0, result.stderr
    assert len(list(output.iterdir())) == 5
    lines = (output / "SHA256SUMS.txt").read_text().splitlines()
    assert len(lines) == 4
    for line in lines:
        digest, name = line.split("  ")
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest


def test_collect_requires_both_architectures(tmp_path):
    incoming = artifact_set(tmp_path)
    (incoming / "home-screen-organizer-macos-x86_64/SHA256SUMS.txt").unlink()
    output = tmp_path / "release"
    result = release("collect", "--input-dir", incoming, "--output-dir", output)
    assert result.returncode != 0
    assert not output.exists()


def test_collect_checks_each_archive_before_copying(tmp_path):
    incoming = artifact_set(tmp_path)
    archive = next((incoming / "home-screen-organizer-macos-x86_64").glob("*.zip"))
    archive.write_bytes(b"changed archive")
    output = tmp_path / "release"
    result = release("collect", "--input-dir", incoming, "--output-dir", output)
    assert result.returncode != 0
    assert not output.exists()


def distribution(tmp_path):
    output = tmp_path / "release"
    output.mkdir()
    from source_fixture import release_source
    release_source(output, VERSION, tmp_path / "source-checkout")
    refresh_distribution_checksums(output)
    return output


def refresh_distribution_checksums(output):
    (output / "SHA256SUMS.txt").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(output.iterdir()) if path.name != "SHA256SUMS.txt"))


def verify_fixture(tmp_path, *args):
    script = tmp_path / "source-checkout/macapp/release.py"
    return subprocess.run([sys.executable, script, "verify", *map(str, args)], capture_output=True, text=True)


def test_verify_distribution_checks_all_assets_and_package_metadata(tmp_path):
    output = distribution(tmp_path)
    result = verify_fixture(tmp_path, "--input-dir", output, "--repository", "example/project", "--tag", f"v{VERSION}")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("damage", ["non-zip", "engine", "notice", "mode"])
def test_verify_rejects_unrelated_bundle_even_with_matching_registry_hash(tmp_path, damage):
    output = distribution(tmp_path)
    bundle = next(output.glob("*.mcpb"))
    if damage == "non-zip":
        bundle.write_bytes(b"unrelated bundle")
    else:
        target = "licenses/arm64/THIRD-PARTY-NOTICES.txt" if damage == "notice" else "server/arm64/engine"
        with zipfile.ZipFile(bundle) as original:
            entries = [(entry, original.read(entry)) for entry in original.infolist()]
        with zipfile.ZipFile(bundle, "w") as modified:
            for entry, data in entries:
                if entry.filename == target:
                    if damage == "mode":
                        entry.external_attr ^= 0o100 << 16
                    else:
                        data = bytes([data[0] ^ 1]) + data[1:]
                modified.writestr(entry, data)
    meta = output / "server.json"
    data = json.loads(meta.read_bytes())
    data["packages"][0]["fileSha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    meta.write_text(json.dumps(data))
    refresh_distribution_checksums(output)
    result = verify_fixture(tmp_path, "--input-dir", output, "--repository", "example/project", "--tag", f"v{VERSION}")
    assert result.returncode != 0


@pytest.mark.parametrize("damage", ["missing-checksum", "duplicate-checksum", "hash", "url", "version", "name"])
def test_verify_rejects_incomplete_checksums_and_mismatched_registry_metadata(tmp_path, damage):
    import json
    output = distribution(tmp_path)
    if damage in ("missing-checksum", "duplicate-checksum"):
        manifest = output / "SHA256SUMS.txt"
        lines = manifest.read_text().splitlines()
        manifest.write_text("\n".join(lines[1:] if damage == "missing-checksum" else lines + [lines[0]]) + "\n")
    else:
        metadata_path = output / "server.json"
        metadata = json.loads(metadata_path.read_text())
        if damage == "hash":
            metadata["packages"][0]["fileSha256"] = "f" * 64
        elif damage == "url":
            metadata["packages"][0]["identifier"] = "https://example.com/other.mcpb"
        else:
            metadata[damage] = "different"
        metadata_path.write_text(json.dumps(metadata))
        refresh_distribution_checksums(output)
    result = verify_fixture(tmp_path, "--input-dir", output, "--repository", "example/project", "--tag", f"v{VERSION}")
    assert result.returncode != 0


def test_rejected_package_preserves_verified_assets(tmp_path):
    build = make_build(tmp_path)
    output = tmp_path / "release"
    args = ("package", "--arch", "arm64", "--build-dir", build, "--output-dir", output)
    dirty = build / "Screen Organizer.app/Contents/Resources/note"
    dirty.write_text(str(build / "private"))
    assert release(*args).returncode != 0
    assert not output.exists()
    dirty.unlink()
    result = release(*args)
    assert result.returncode == 0, result.stderr
    (output / "other").write_text("keep")
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    dirty.write_text(str(build / "another"))
    assert release(*args).returncode != 0
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before
    dirty.write_text("public resource")
    app = build / "Screen Organizer.app"
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app], check=True, capture_output=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", app], check=True)
    stale = release(*args)
    assert stale.returncode != 0 and "different apps" in stale.stderr
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before
    make_image(app, build / f"home-screen-organizer-{VERSION}.dmg", tmp_path / "image-files")
    result = release(*args)
    assert result.returncode == 0, result.stderr
    assert (output / "other").read_text() == "keep"
    assert (output / "SHA256SUMS.txt").read_bytes() != before["SHA256SUMS.txt"]


@pytest.mark.parametrize("change", ["extra-file", "root-mode", "wrong-link", "matching"])
def test_disk_image_root_contract(tmp_path, change):
    from macapp.release import verify_image

    build = make_build(tmp_path)
    app = build / "Screen Organizer.app"
    archive = tmp_path / "release.zip"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", app, archive], check=True)
    layout = tmp_path / "image-files"
    if change == "extra-file":
        (layout / ".extra").write_text(str(tmp_path / "private checkout"))
    elif change == "root-mode":
        (layout / app.name).chmod(0o700)
    elif change == "wrong-link":
        (layout / "Applications").unlink()
        (layout / "Applications").symlink_to("/tmp")
    image = tmp_path / "release.dmg"
    subprocess.run(["hdiutil", "create", "-quiet", "-format", "UDZO", "-srcfolder", layout, image], check=True)
    verification = tmp_path / "verify"
    verification.mkdir()
    if change == "matching":
        verify_image(image, archive, verification)
    else:
        with pytest.raises(ValueError):
            verify_image(image, archive, verification)
    assert not os.path.ismount(verification / "mount")


def test_invalid_build_records_have_one_package_diagnostic(tmp_path):
    from macapp.build_paths import RECORD

    checkout = tmp_path / "private checkout"
    (checkout / "macapp").mkdir(parents=True)
    for name in ("release.py", "check_package_paths.py", "build_paths.py"):
        shutil.copyfile(SCRIPT.with_name(name), checkout / "macapp" / name)
    shutil.copyfile(ROOT / "pyproject.toml", checkout / "pyproject.toml")
    build = tmp_path / "build"
    contents = build / "Screen Organizer.app/Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleShortVersionString": VERSION, "HomeScreenOrganizerArchitecture": "arm64"}))
    (build / f"home-screen-organizer-{VERSION}.dmg").touch()
    output = tmp_path / "previous release"
    output.mkdir()
    (output / "note").write_text("preserve")
    invalid = ["[]", '"value"', "7", "true", "null", "{", "{}",
               json.dumps({"version": 2, "roots": {str(checkout): True}, "inputs": {}}),
               json.dumps({"version": 2, "roots": [str(checkout)], "inputs": []}),
               json.dumps({"version": 2, "roots": ["/fixture/invalid\0path"], "inputs": {}}),
               json.dumps({"version": 2, "roots": ["/fixture/invalid\ud800"], "inputs": {}})]
    for record in invalid:
        (build / RECORD).write_text(record)
        result = subprocess.run([sys.executable, checkout / "macapp/release.py", "package",
                                 "--arch", "arm64", "--build-dir", build, "--output-dir", output],
                                capture_output=True, text=True)
        assert result.returncode != 0
        assert result.stderr == "Prepare a fresh native build to record its locations\n"
        assert list(output.iterdir()) == [output / "note"] and (output / "note").read_text() == "preserve"


def test_recreated_package_uses_original_intel_roots(tmp_path):
    from macapp.build_paths import write_record

    build = make_build(tmp_path, arch="x86_64")
    original = tmp_path / "old-temp/intel"
    prepared = tmp_path / "copied-dependencies"
    wheel = prepared / "wheels/cryptography-source.whl"
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"fixture")
    lock = prepared / "requirements-build.lock"
    lock.write_text("fixture")
    write_record(prepared, [original], (lock, wheel))
    subprocess.run([sys.executable, str(SCRIPT.with_name("build_paths.py")), "app-record",
                    str(build), str(build), "--intel", str(prepared)], check=True)
    (build / "Screen Organizer.app/Contents/Resources/note").write_text(str(original / "openssl/lib"))
    output = tmp_path / "release"
    result = release("package", "--build-dir", build, "--output-dir", output, "--arch", "x86_64")
    assert result.returncode != 0 and "build path" in result.stderr
    assert str(original) not in result.stderr and not output.exists()


@pytest.mark.parametrize("host", ["local", "self-hosted", "github-hosted"])
def test_app_record_retains_builder_account_rules(tmp_path, monkeypatch, host):
    from macapp.build_paths import read_roots
    original = tmp_path / "first-home"
    runner_temp = tmp_path / "original-runner-temp"
    monkeypatch.setenv("HOME", str(original))
    monkeypatch.setenv("GITHUB_ACTIONS", "false" if host == "local" else "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", host)
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    subprocess.run([sys.executable, str(SCRIPT.with_name("build_paths.py")), "app-record",
                    str(tmp_path), str(tmp_path / "checkout")], check=True)
    monkeypatch.setenv("HOME", str(tmp_path / "other-home"))
    roots = read_roots(tmp_path)
    assert (original in roots) == (host != "github-hosted")
    assert (runner_temp in roots) == (host != "local")


def test_official_build_records_physical_checkout_and_export(tmp_path, monkeypatch):
    from macapp.build_paths import read_roots
    from macapp.check_package_paths import check_archive
    from test_package_privacy import native_zip

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    build, checkout, export, venv = (tmp_path / name for name in ("build", "checkout", "export", "venv"))
    build.mkdir()
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner-temp"))
    line, = (line for line in SCRIPT.with_name("build.sh").read_text().splitlines() if " app-record " in line)
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    (export / "macapp").mkdir(parents=True)
    (export / "macapp/build_paths.py").symlink_to(SCRIPT.with_name("build_paths.py"))
    env = {**os.environ, "BUILD": str(build), "CHECKOUT": str(checkout), "ROOT": str(export), "VENV": str(venv)}
    subprocess.run(["zsh", "-c", "BUILD_PATH_ARGS=()\n" + line], env=env, check=True)
    roots = read_roots(build)
    for private in (checkout, export):
        with pytest.raises(ValueError, match="build path"):
            check_archive(native_zip(tmp_path, config=str(private / "src")), roots)
    check_archive(native_zip(tmp_path, config=str(tmp_path / "other-checkout/src")), roots)


def test_replacement_preserves_permissions_and_drops_managed_prior_pair(tmp_path):
    from macapp.release import install_generation

    output = tmp_path / "assets"
    output.mkdir(mode=0o700)
    (output / "notes").write_text("private")
    subprocess.run(["chmod", "+a", "everyone allow readattr", str(output)], check=True)
    acl = subprocess.check_output(["ls", "-lde", str(output)], text=True).splitlines()[1:]
    for version in ("1.2.0-beta.1", "1.3.0"):
        staged = tmp_path / version
        staged.mkdir()
        rows = []
        for extension in ("zip", "dmg"):
            name = f"home-screen-organizer-{version}-macos-arm64.{extension}"
            data = (name + version).encode()
            (staged / name).write_bytes(data)
            rows.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        (staged / "SHA256SUMS.txt").write_text("".join(rows))
        install_generation(staged, output, "arm64")
    assert output.stat().st_mode & 0o777 == 0o700
    assert subprocess.check_output(["ls", "-lde", str(output)], text=True).splitlines()[1:] == acl
    assert (output / "notes").read_text() == "private"
    assert sorted(p.suffix for p in output.iterdir()) == ["", ".dmg", ".txt", ".zip"]


@pytest.mark.parametrize("prior", [None, "1.2.0"])
@pytest.mark.parametrize("collision", ["directory", "file", "dangling-link"])
def test_incoming_native_name_must_be_owned_before_replacement(tmp_path, prior, collision):
    from macapp.release import app_contents, install_generation

    def generation(version, parent):
        parent.mkdir()
        rows = []
        for suffix in ("zip", "dmg"):
            name = f"home-screen-organizer-{version}-macos-arm64.{suffix}"
            data = (version + suffix).encode()
            (parent / name).write_bytes(data)
            rows.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        (parent / "SHA256SUMS.txt").write_text("".join(rows))
        return parent

    output = tmp_path / "published"
    if prior:
        install_generation(generation(prior, tmp_path / "previous"), output, "arm64")
    else:
        output.mkdir(mode=0o750)
    (output / "ordinary").write_bytes(b"keep ordinary sibling")
    occupied = output / "home-screen-organizer-1.3.0-macos-arm64.zip"
    if collision == "directory":
        occupied.mkdir(mode=0o750)
        (occupied / "notes").write_bytes(b"keep recipient notes")
    elif collision == "file":
        occupied.write_bytes(b"keep recipient bytes")
        occupied.chmod(0o640)
    else:
        occupied.symlink_to("missing-recipient-notes")
    before = app_contents(output)
    staged = generation("1.3.0", tmp_path / "incoming")
    with pytest.raises(ValueError, match="occupied"):
        install_generation(staged, output, "arm64")
    assert app_contents(output) == before
    assert not (staged / ".generation").exists()


def test_owned_native_pair_can_be_refreshed_in_place(tmp_path):
    from macapp.release import install_generation
    output = tmp_path / "published"
    for index in (0, 1):
        staged = tmp_path / str(index)
        staged.mkdir()
        rows = []
        for suffix in ("zip", "dmg"):
            name = f"home-screen-organizer-1.3.0-macos-arm64.{suffix}"
            data = f"generation {index} {suffix}".encode()
            (staged / name).write_bytes(data)
            rows.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        (staged / "SHA256SUMS.txt").write_text("".join(rows))
        install_generation(staged, output, "arm64")
    assert (output / "home-screen-organizer-1.3.0-macos-arm64.zip").read_bytes() == b"generation 1 zip"


@pytest.mark.parametrize("inherited", [False, True])
def test_release_descendant_acls(tmp_path, inherited):
    from macapp.release import install_generation

    def acl(path):
        return subprocess.check_output(["ls", "-lde", path], text=True).splitlines()[1:]

    output = tmp_path / "release"
    output.mkdir(mode=0o700)
    nested = output / "notes"
    nested.mkdir()
    plain = output / "existing folder"
    plain.mkdir()
    (plain / "record").write_text("existing")
    note = nested / "retained"
    note.write_text("keep")
    (nested / ".nfs-retained").write_text("keep hidden file")
    link = nested / "shortcut"
    link.symlink_to("../ordinary")
    subprocess.run(["chmod", "+a", "everyone deny write", note], check=True)
    subprocess.run(["chmod", "+a", "everyone deny add_file", nested], check=True)
    if inherited:
        subprocess.run(["chmod", "+a", "everyone deny write,file_inherit,only_inherit", output], check=True)
        subprocess.run(["chmod", "+a", "everyone deny add_file,directory_inherit,only_inherit", output], check=True)
    ordinary = output / "ordinary"
    ordinary.write_bytes(b"regular")
    expected = [acl(p) for p in (output, ordinary, nested, note)]
    assert expected[2] and expected[3]
    assert bool(expected[1]) == inherited
    staged = tmp_path / "assets"
    staged.mkdir()
    for name in ("archive.zip", "image.dmg", "SHA256SUMS.txt"):
        (staged / name).write_bytes(name.encode())
    install_generation(staged, output, "arm64")
    for name in ("archive.zip", "image.dmg", "SHA256SUMS.txt"):
        assert acl(output / name) == expected[1]
        assert (output / name).read_bytes() == name.encode()
    assert [acl(p) for p in (output, ordinary, nested, note)] == expected
    assert note.read_text() == "keep" and link.readlink() == Path("../ordinary")
    assert (nested / ".nfs-retained").read_text() == "keep hidden file"
    assert acl(plain) == [] and (plain / "record").read_text() == "existing"


def test_first_release_inherits_output_parent_acl(tmp_path):
    from macapp.release import install_generation

    def acl(path):
        return subprocess.check_output(["ls", "-lde", path], text=True).splitlines()[1:]

    parent = tmp_path / "published"
    parent.mkdir()
    staging = parent / "private staging"
    staging.mkdir()
    staged = staging / "assets"
    staged.mkdir()
    (staged / "asset.zip").write_bytes(b"verified")
    subprocess.run(["chmod", "+a", "everyone deny writeextattr,file_inherit,directory_inherit,only_inherit", parent],
                   check=True)
    expected = parent / "normal"
    expected.mkdir()
    (expected / "asset.zip").write_bytes(b"ordinary")
    output = parent / "first release"
    install_generation(staged, output, "arm64")
    assert acl(expected) and acl(output) == acl(expected)
    assert acl(expected / "asset.zip") and acl(output / "asset.zip") == acl(expected / "asset.zip")


@pytest.mark.parametrize("bad", ["../relative", "wrong-architecture", "mismatched-pair", "duplicate", "hash"])
def test_previous_manifest_establishes_owned_native_pair(tmp_path, bad):
    from macapp.release import previous_assets

    base = "home-screen-organizer-1.3.0-macos-arm64"
    rows = []
    for suffix in ("zip", "dmg"):
        name = f"{base}.{suffix}"
        (tmp_path / name).write_bytes(b"old")
        rows.append(f"{hashlib.sha256(b'old').hexdigest()}  {name}\n")
    if bad == "duplicate":
        rows[1] = rows[0]
    else:
        source, change = {"../relative": (base, "../" + base),
                          "wrong-architecture": ("arm64", "x86_64"),
                          "mismatched-pair": ("1.3.0", "1.2.0"),
                          "hash": (rows[0][:64], "0" * 64)}[bad]
        rows[0] = rows[0].replace(source, change)
    (tmp_path / "SHA256SUMS.txt").write_text("".join(rows))
    with pytest.raises(ValueError, match="manifest"):
        previous_assets(tmp_path, "arm64")


@pytest.mark.parametrize("nested", [False, True])
def test_protected_retirement_preserves_shared_file(tmp_path, nested):
    build = make_build(tmp_path)
    output = tmp_path / "published"
    result = release("package", "--arch", "arm64", "--build-dir", build, "--output-dir", output)
    assert result.returncode == 0, result.stderr
    retained = output / "existing" if nested else output
    retained.mkdir(exist_ok=True)
    note = retained / "retained"
    note.write_bytes(b"ordinary retained file")
    note.chmod(0o644)
    outside = tmp_path / "second-link"
    os.link(note, outside)
    protected = retained if nested else note
    restriction = "everyone deny delete_child" if nested else "everyone deny delete"
    subprocess.run(["chmod", "+a", restriction, protected], check=True)
    acl = subprocess.check_output(["ls", "-lde", outside], text=True).splitlines()[1:]
    try:
        for _ in range(2):
            result = release("package", "--arch", "arm64", "--build-dir", build, "--output-dir", output)
            assert result.returncode == 0, result.stderr
            assert (note.stat().st_mode & 0o777, outside.stat().st_mode & 0o777) == (0o644, 0o644)
            assert note.samefile(outside) and note.read_bytes() == b"ordinary retained file"
            assert subprocess.check_output(["ls", "-lde", outside], text=True).splitlines()[1:] == acl
            assert all((p.stat().st_mode & 0o777) == 0o700 for p in tmp_path.glob(".native-release-*"))
        previous = (output / "SHA256SUMS.txt").read_bytes()
        subprocess.run(["chmod", "+a", "everyone deny add_file", output], check=True)
        failed = release("package", "--arch", "arm64", "--build-dir", build, "--output-dir", output)
        assert failed.returncode != 0 and (output / "SHA256SUMS.txt").read_bytes() == previous
        assert note.samefile(outside) and note.stat().st_mode & 0o777 == 0o644
    finally:
        subprocess.run(["chmod", "-N", output], check=True)
        subprocess.run(["chmod", "-N", outside], check=True)
        if nested:
            for path in tmp_path.rglob("existing"):
                subprocess.run(["chmod", "-N", path], check=True)


def test_distribution_requires_an_archive_with_verified_native_sources(tmp_path):
    import io
    import tarfile
    output = distribution(tmp_path)
    source = output / f"home-screen-organizer-{VERSION}-corresponding-source.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        manifest = json.dumps({"version": VERSION, "commit": "a" * 40, "files": {}, "architectures": {
            arch: {"zip": hashlib.sha256((output / f"home-screen-organizer-{VERSION}-macos-{arch}.zip").read_bytes()).hexdigest()}
            for arch in ("arm64", "x86_64")}}).encode()
        item = tarfile.TarInfo("MANIFEST.json")
        item.size = len(manifest)
        archive.addfile(item, io.BytesIO(manifest))
    refresh_distribution_checksums(output)
    result = verify_fixture(tmp_path, "--input-dir", output, "--repository", "example/project", "--tag", f"v{VERSION}", "--commit", "a" * 40)
    assert result.returncode != 0


@pytest.mark.parametrize("missing", ["project/homescreen/example.py", "native/dependency"])
def test_publish_verifier_derives_source_closure_from_commit_and_native_zip(tmp_path, missing):
    import io
    import tarfile
    output = distribution(tmp_path)
    archive = output / f"home-screen-organizer-{VERSION}-corresponding-source.tar.gz"
    with tarfile.open(archive) as source:
        items = {member.name: (member, source.extractfile(member).read()) for member in source}
    manifest = json.loads(items.pop("MANIFEST.json")[1])
    name = manifest["inputs"].pop(missing)["path"] if missing.startswith("native/") else missing
    del items[name], manifest["files"][name]
    with tarfile.open(archive, "w:gz") as replaced:
        for header, data in items.values():
            replaced.addfile(header, io.BytesIO(data))
        data = json.dumps(manifest).encode()
        header = tarfile.TarInfo("MANIFEST.json")
        header.size = len(data)
        replaced.addfile(header, io.BytesIO(data))
    refresh_distribution_checksums(output)
    result = verify_fixture(tmp_path, "--input-dir", output, "--repository", "example/project")
    assert result.returncode != 0 and "source" in result.stderr.lower()


def test_build_record_round_trips_non_utf8_filesystem_root(tmp_path):
    from macapp.build_paths import RECORD, read_roots
    from macapp.check_package_paths import check_archive
    from test_package_privacy import native_zip
    root = Path(os.fsdecode(b"/fixture/nonutf8-\xff"))
    (tmp_path / RECORD).write_text(json.dumps({"version": 2, "roots": [str(root)], "inputs": {}}))
    assert read_roots(tmp_path) == [root]
    with pytest.raises(ValueError, match="build path"):
        check_archive(native_zip(tmp_path, extra=os.fsencode(root) + b"/child"), [root])


def test_native_retirement_envelope_clears_inherited_acl(tmp_path):
    from macapp.release import private_transaction
    parent = tmp_path / "inherited"
    parent.mkdir()
    subprocess.run(["chmod", "+a", "everyone allow list,search,directory_inherit", parent], check=True)
    try:
        folder = private_transaction(parent)
        result = subprocess.check_output(["ls", "-lde", folder], text=True).splitlines()
        assert len(result) == 1 and folder.stat().st_mode & 0o777 == 0o700
        assert not list(folder.iterdir())
    finally:
        subprocess.run(["chmod", "-N", parent], check=True)


def test_native_zip_preserves_modes_links_and_omits_owner_extras(tmp_path):
    import stat
    import struct
    from macapp.release import app_contents, native_zip
    app = tmp_path / "Screen Organizer.app"
    app.mkdir(mode=0o750)
    (app / "folder").mkdir(mode=0o710)
    (app / "folder/executable").write_bytes(b"\x00\xff signed payload")
    (app / "folder/executable").chmod(0o751)
    (app / "shortcut").symlink_to("folder/executable")
    path = tmp_path / "native.zip"
    native_zip(app, path)
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            assert info.create_system == 3 and info.extra == b""
            archive.fp.seek(info.header_offset + 26)
            name_length, extra_length = struct.unpack("<HH", archive.fp.read(4))
            assert extra_length == 0
        assert stat.S_ISLNK(archive.getinfo(app.name + "/shortcut").external_attr >> 16)
        assert archive.read(app.name + "/shortcut") == b"folder/executable"
    unpacked = tmp_path / "unpacked"
    subprocess.run(["ditto", "-x", "-k", path, unpacked], check=True)
    assert app_contents(unpacked / app.name) == app_contents(app)


def test_clean_native_staging_preserves_input_xattrs_and_signed_bytes(tmp_path):
    from macapp.release import app_contents, clean_copy, check_tree_metadata
    build = make_build(tmp_path)
    app = build / "Screen Organizer.app"
    directory = app / "Contents/Resources"
    payload = directory / "LICENSE-fixture"
    payload.write_bytes(b"ordinary signed resource")
    link = directory / "resource-link"
    link.symlink_to(payload.name)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app], check=True, capture_output=True)
    value = plistlib.dumps(["https://unrelated.example/private-origin"])
    attribute = "com.apple.metadata:kMDItemWhereFroms"
    for path in (app, directory, payload):
        subprocess.run(["xattr", "-wx", attribute, value.hex(), path], check=True)
    before = app_contents(app)
    clean = tmp_path / "staging" / app.name
    clean_copy(app, clean)
    check_tree_metadata(clean)
    assert app_contents(clean) == before
    subprocess.run(["codesign", "--verify", "--deep", "--strict", clean], check=True, capture_output=True)
    assert all(bytes.fromhex(subprocess.check_output(["xattr", "-px", attribute, path], text=True)) == value
               for path in (app, directory, payload))
    with pytest.raises(ValueError, match="metadata"):
        check_tree_metadata(app)
    (clean.parent / "Applications").symlink_to("/Applications")
    subprocess.run(["hdiutil", "create", "-quiet", "-ov", "-format", "UDZO", "-srcfolder", clean.parent,
                    build / f"home-screen-organizer-{VERSION}.dmg"], check=True, capture_output=True)
    output = tmp_path / "release"
    result = release("package", "--build-dir", build, "--output-dir", output, "--arch", "arm64")
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        assert all(not name.startswith("__MACOSX/") and "/._" not in name for name in archive.namelist())
    assert app_contents(app) == before
    assert all(bytes.fromhex(subprocess.check_output(["xattr", "-px", attribute, path], text=True)) == value
               for path in (app, directory, payload))


def test_tagged_zip_and_dmg_are_rejected_without_path_marker(tmp_path):
    from macapp.release import verify_image
    build = make_build(tmp_path)
    app = build / "Screen Organizer.app"
    archive = tmp_path / "app.zip"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", app, archive], check=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    verify_image(build / f"home-screen-organizer-{VERSION}.dmg", archive, plain)
    subprocess.run(["xattr", "-wx", "com.apple.metadata:kMDItemWhereFroms",
                    plistlib.dumps(["https://unrelated.example/private-origin"]).hex(), app / "Contents/Resources"], check=True)
    image = build / "tagged.dmg"
    make_image(app, image, tmp_path / "tagged-files")
    tagged = tmp_path / "tagged"
    tagged.mkdir()
    with pytest.raises(ValueError, match="metadata"):
        verify_image(image, archive, tagged)


@pytest.mark.parametrize("reason", [b"Resource temporarily unavailable", "无法识别映像", b"", None])
def test_image_attach_failure_preserves_process_error_and_reports_stderr(tmp_path, monkeypatch, reason):
    from macapp.release import verify_image
    calls = []
    error = subprocess.CalledProcessError(1, ["hdiutil", "attach"], output=b"native stdout", stderr=reason)
    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["hdiutil", "attach"]:
            raise error
        assert args[:3] == ["ditto", "-x", "-k"]
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        verify_image(tmp_path / "release.dmg", tmp_path / "release.zip", tmp_path)
    assert caught.value is error and not isinstance(error, ValueError)
    assert error.returncode == 1 and error.output == b"native stdout" and error.stderr == reason
    notes = " ".join(getattr(error, "__notes__", []))
    assert notes.startswith("Image attachment failed. hdiutil stderr: ")
    expected = reason.decode() if isinstance(reason, bytes) else reason
    assert (expected or "<empty>") in notes
    assert sum(args[:2] == ["hdiutil", "attach"] for args in calls) == 1


def test_image_error_note_redacts_untrusted_paths_controls_and_excess_output(tmp_path, monkeypatch):
    from macapp.release import verify_image
    image = tmp_path / "private-source-marker/release.dmg"
    reason = (f"hdiutil: {image}: failed\n/unknown/account secret/with spaces\n"
              "\x1b[31m::error::injected\x1b[0m\r\x00\u202e" + "x" * 9000).encode()
    error = subprocess.CalledProcessError(1, ["hdiutil", "attach"], stderr=reason)
    def run(args, **kwargs):
        if args[:2] == ["hdiutil", "attach"]:
            raise error
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        verify_image(image, tmp_path / "release.zip", tmp_path)
    note, = error.__notes__
    assert str(tmp_path) not in note and "private-source-marker" not in note
    assert "/unknown" not in note and "account secret" not in note
    assert all(character.isprintable() for character in note) and len(note) <= 560
    assert error.stderr == reason


def test_release_cli_prints_only_safe_privacy_attribution(tmp_path, monkeypatch, capsys):
    import check_package_paths
    import macapp.release as module
    from test_package_privacy import native_zip
    path = native_zip(tmp_path, extra=bytes(tmp_path / "private-marker"))
    def package(*args):
        check_package_paths.check_archive(path, [tmp_path / "private-marker"])
    monkeypatch.setattr(module, "package", package)
    monkeypatch.setattr(sys, "argv", ["release.py", "package", "--arch", "arm64"])
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 1
    output = capsys.readouterr().err
    assert "Package location: ZIP!Screen Organizer.app/Contents/MacOS/HomeScreenOrganizer" in output
    assert "path rule: recorded-build-root" in output
    assert "Release payload contains a local build path" in output
    assert str(tmp_path) not in output and "private-marker" not in output
