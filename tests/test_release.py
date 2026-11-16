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
    package = project / "homescreen"
    package.mkdir()
    for name in ("__init__.py", "private_store.py"):
        (package / name).write_bytes((ROOT / "homescreen" / name).read_bytes())
    (project / "pyproject.toml").write_text('[project]\nversion = "1.2.0-beta.1"\n')
    output = tmp_path / "prerelease"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result = subprocess.run([sys.executable, str(script), "package", "--build-dir", str(build),
                             "--output-dir", str(output), "--tag", "v1.2.0-beta.1", "--arch", "arm64"],
                            capture_output=True, text=True, env=env)
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


def test_release_aborts_when_retained_directory_name_changes(tmp_path, monkeypatch):
    from macapp.release import install_generation
    output, staged, outside = (tmp_path / name for name in ("release", "assets", "outside"))
    for directory in (output, staged, outside):
        directory.mkdir()
    retained = output / "notes"
    retained.mkdir()
    (retained / "record").write_bytes(b"user notes")
    retained.chmod(0o750)
    (retained / "record").chmod(0o640)
    subprocess.run(["chmod", "+a", "everyone deny write", retained / "record"], check=True)
    original_acl = subprocess.check_output(["ls", "-lde", retained / "record"], text=True).splitlines()[1:]
    parked = output / "parked"
    (outside / "secret").write_bytes(b"outside subtree")
    (outside / "secret").chmod(0o640)
    for name in ("archive.zip", "image.dmg", "SHA256SUMS.txt"):
        (staged / name).write_bytes(b"new verified asset")
    identity = retained.stat().st_dev, retained.stat().st_ino
    original_iterdir, original_listdir = Path.iterdir, os.listdir
    swapped = False
    def swap():
        nonlocal swapped
        if not swapped:
            retained.rename(parked)
            retained.symlink_to(outside, target_is_directory=True)
            swapped = True
    def iterdir(path):
        if path == retained:
            swap()
        return original_iterdir(path)
    def listdir(path):
        if isinstance(path, int) and (os.fstat(path).st_dev, os.fstat(path).st_ino) == identity:
            swap()
        return original_listdir(path)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(os, "listdir", listdir)
    with pytest.raises((OSError, ValueError), match="changed|preserve"):
        install_generation(staged, output, "arm64")
    assert swapped and retained.is_symlink() and retained.readlink() == outside
    assert (parked / "record").read_bytes() == b"user notes"
    assert (outside / "secret").read_bytes() == b"outside subtree"
    assert parked.stat().st_mode & 0o777 == 0o750
    assert (parked / "record").stat().st_mode & 0o777 == 0o640
    assert (outside / "secret").stat().st_mode & 0o777 == 0o640
    assert subprocess.check_output(["ls", "-lde", parked / "record"], text=True).splitlines()[1:] == original_acl
    assert not (output / "archive.zip").exists()
    assert not (staged / ".generation/notes/secret").exists()


def test_release_aborts_if_new_user_entry_appears_after_listing(tmp_path, monkeypatch):
    from macapp.release import install_generation
    output, staged = tmp_path / "release", tmp_path / "assets"
    output.mkdir()
    staged.mkdir()
    notes = output / "notes"
    notes.mkdir()
    (notes / "original").write_bytes(b"original")
    (staged / "asset.zip").write_bytes(b"verified")
    identity = notes.stat().st_dev, notes.stat().st_ino
    original_listdir = os.listdir
    added = False
    def listdir(directory):
        nonlocal added
        names = original_listdir(directory)
        if isinstance(directory, int) and (os.fstat(directory).st_dev, os.fstat(directory).st_ino) == identity and not added:
            (notes / "new-user-entry").write_bytes(b"retain newly created object")
            added = True
        return names
    monkeypatch.setattr(os, "listdir", listdir)
    with pytest.raises(OSError, match="changed"):
        install_generation(staged, output, "arm64")
    assert added and (notes / "new-user-entry").read_bytes() == b"retain newly created object"
    assert (notes / "original").read_bytes() == b"original"
    assert not (output / "asset.zip").exists()


@pytest.mark.parametrize("save_at", ["none", "exchange", "replace", "cleanup", "rmdir", "parking_failure", "parking_move_failure", "pre_exchange_failure"])
def test_public_package_preserves_successful_saves_during_retirement(tmp_path, monkeypatch, capsys, save_at):
    import ctypes
    import build_paths
    import check_package_paths
    from macapp import release as publisher
    build, output = tmp_path / "build", tmp_path / "published"
    app = build / "Screen Organizer.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "1.3.0",
                                                            "HomeScreenOrganizerArchitecture": "arm64"}))
    (build / "home-screen-organizer-1.3.0.dmg").write_bytes(b"synthetic image")
    notes = output / "notes"
    notes.mkdir(parents=True, mode=0o750)
    original = notes / "original.txt"
    original.write_bytes(b"existing user notes")
    original.chmod(0o640)
    outside = tmp_path / "external-hardlink"
    os.link(original, outside)
    subprocess.run(["chmod", "+a", "everyone deny write", outside], check=True)
    acl = subprocess.check_output(["ls", "-lde", outside], text=True).splitlines()[1:]
    old_notes = os.open(notes, os.O_RDONLY | os.O_DIRECTORY)
    identity = notes.stat().st_dev, notes.stat().st_ino
    saved = False
    payload = b"newly saved user document"
    def save(replace=False):
        nonlocal saved
        if saved:
            return
        try:
            fd = os.open("new-user-document.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640, dir_fd=old_notes)
        except FileNotFoundError:
            return  # An already retired empty directory cannot accept a successful save.
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        if replace:
            os.replace("new-user-document.txt", "original.txt", src_dir_fd=old_notes, dst_dir_fd=old_notes)
        saved = True
    cdll, rmtree, rmdir = ctypes.CDLL, shutil.rmtree, os.rmdir
    class Library:
        def __init__(self):
            self.real = cdll(None, use_errno=True)
        def __getattr__(self, name):
            if name == "renameatx_np":
                def rename(*arguments):
                    if save_at == "parking_move_failure" and arguments[3:] == (b"previous", 4):
                        ctypes.set_errno(5)
                        return -1
                    if arguments[-1] == 2 and save_at == "pre_exchange_failure":
                        ctypes.set_errno(5)
                        return -1
                    if arguments[-1] == 2 and save_at in ("exchange", "replace", "parking_failure", "parking_move_failure"):
                        save(save_at == "replace")
                    return self.real.renameatx_np(*arguments)
                return rename
            return getattr(self.real, name)
    def cleanup(folder, *args, **kwargs):
        if save_at == "cleanup":
            save()
        return rmtree(folder, *args, **kwargs)
    def remove_directory(name, *, dir_fd=None):
        if save_at == "rmdir":
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == identity:
                save()
        return rmdir(name, dir_fd=dir_fd)
    monkeypatch.setattr(publisher.ctypes, "CDLL", lambda *args, **kwargs: Library())
    monkeypatch.setattr(publisher.shutil, "rmtree", cleanup)
    monkeypatch.setattr(publisher.os, "rmdir", remove_directory)
    monkeypatch.setattr(build_paths, "read_roots", lambda _: [])
    monkeypatch.setattr(publisher, "verify_image", lambda *args: None)
    monkeypatch.setattr(check_package_paths, "check_archive", lambda *args: None)
    if save_at == "parking_failure":
        transaction = publisher.private_transaction
        calls = 0
        def private_transaction(parent):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("private parking unavailable")
            return transaction(parent)
        monkeypatch.setattr(publisher, "private_transaction", private_transaction)
    try:
        if save_at == "pre_exchange_failure":
            with pytest.raises(OSError, match="Could not install"):
                publisher.package(build, output, "1.3.0", "arm64")
        else:
            publisher.package(build, output, "1.3.0", "arm64")
    finally:
        os.close(old_notes)
    assert (output / "home-screen-organizer-1.3.0-macos-arm64.zip").is_file() == (save_at != "pre_exchange_failure")
    assert original.samefile(outside) and outside.read_bytes() == b"existing user notes"
    assert outside.stat().st_mode & 0o777 == 0o640
    assert subprocess.check_output(["ls", "-lde", outside], text=True).splitlines()[1:] == acl
    retained = list(tmp_path.glob(".native-release-*"))
    message = capsys.readouterr().err
    if saved:
        copies = [path for path in tmp_path.rglob("*") if path.is_file() and path.read_bytes() == payload]
        assert copies, "successful user save must survive the complete public retirement"
        assert "Release installed" in message and "retained" in message
        assert any(str(folder) in message for folder in retained)
        assert all(folder.stat().st_mode & 0o777 == 0o700 for folder in retained)
        assert all(path.stat().st_mode & 0o777 == 0o640 for path in copies)
    else:
        assert retained == [] and message == ""


def test_retained_generation_survives_callers_temporary_directory_cleanup(tmp_path, monkeypatch):
    import ctypes
    import tempfile
    from macapp import release as publisher
    output = tmp_path / "published"
    notes = output / "notes"
    notes.mkdir(parents=True)
    (notes / "original").write_bytes(b"original notes")
    cdll = ctypes.CDLL
    class Library:
        def __init__(self):
            self.real = cdll(None, use_errno=True)
        def __getattr__(self, name):
            if name == "renameatx_np":
                def exchange(*args):
                    if args[-1] == 2:
                        (notes / "late-document").write_bytes(b"saved at publication")
                    return self.real.renameatx_np(*args)
                return exchange
            return getattr(self.real, name)
    monkeypatch.setattr(publisher.ctypes, "CDLL", lambda *args, **kwargs: Library())
    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        staged = Path(temporary) / "assets"
        staged.mkdir()
        (staged / "asset.zip").write_bytes(b"verified")
        assert publisher.install_generation(staged, output, "arm64") is None
    assert not Path(temporary).exists()
    copies = [path for folder in tmp_path.glob(".native-release-*") for path in folder.rglob("late-document")]
    assert len(copies) == 1 and copies[0].read_bytes() == b"saved at publication"
    assert (output / "notes/original").read_bytes() == b"original notes"


@pytest.mark.parametrize("target,change", [("zip", "none"), ("zip", "before"), ("zip", "replace"),
    ("zip", "inplace"), ("dmg", "replace"), ("dmg", "inplace"),
    ("manifest", "replace"), ("manifest", "inplace"), ("zip", "after_claim"), ("zip", "during_validation")])
def test_public_package_binds_retirement_to_verified_ownership(tmp_path, monkeypatch, capsys, target, change):
    import ctypes
    import build_paths
    import check_package_paths
    from macapp import release as publisher
    build, output = tmp_path / "build", tmp_path / "published"
    app = build / "Screen Organizer.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "1.3.0",
                                                            "HomeScreenOrganizerArchitecture": "arm64"}))
    (build / "home-screen-organizer-1.3.0.dmg").write_bytes(b"synthetic image")
    (output / "notes").mkdir(parents=True)
    (output / "notes/keep").write_bytes(b"existing user notes")
    old = {kind: output / f"home-screen-organizer-1.2.0-macos-arm64.{kind}" for kind in ("zip", "dmg")}
    rows = []
    for kind, path in old.items():
        data = ("owned previous " + kind).encode().ljust(64, b".")
        path.write_bytes(data)
        path.chmod(0o640)
        rows.append(hashlib.sha256(data).hexdigest() + "  " + path.name + "\n")
    old["manifest"] = output / "SHA256SUMS.txt"
    old["manifest"].write_text("".join(rows))
    old["manifest"].chmod(0o640)
    path = old[target]
    original = path.stat()
    payload = b"user document after verification".ljust(64, b"!")
    def edit():
        if change == "replace":
            pending = output / "user-save.pending"
            pending.write_bytes(payload)
            pending.chmod(0o640)
            os.replace(pending, path)
            assert path.stat().st_ino != original.st_ino
        else:
            path.write_bytes(payload)
            assert path.stat().st_ino == original.st_ino
    if change == "before":
        edit()
    previous = publisher.previous_assets
    def validated(folder, arch):
        result = previous(folder, arch)
        if change in ("replace", "inplace"):
            edit()
        return result
    monkeypatch.setattr(publisher, "previous_assets", validated)
    monkeypatch.setattr(build_paths, "read_roots", lambda _: [])
    monkeypatch.setattr(publisher, "verify_image", lambda *args: None)
    monkeypatch.setattr(check_package_paths, "check_archive", lambda *args: None)
    if change == "during_validation":
        file_digest = hashlib.file_digest
        def digest(stream, *args, **kwargs):
            result = file_digest(stream, *args, **kwargs)
            if os.fstat(stream.fileno()).st_ino == original.st_ino:
                edit()
            return result
        monkeypatch.setattr(publisher.hashlib, "file_digest", digest)
    held = None
    if change == "after_claim":
        held = os.open(path, os.O_RDWR)
        cdll = ctypes.CDLL
        class Library:
            def __init__(self):
                self.real = cdll(None, use_errno=True)
            def __getattr__(self, name):
                if name == "renameatx_np":
                    def rename(*args):
                        result = self.real.renameatx_np(*args)
                        if result == 0 and args[-1] == 4 and args[1] == os.fsencode(path.name):
                            os.lseek(held, 0, os.SEEK_SET)
                            os.write(held, payload)
                            os.ftruncate(held, len(payload))
                            os.utime(held, ns=(original.st_atime_ns, original.st_mtime_ns))
                        return result
                    return rename
                return getattr(self.real, name)
        monkeypatch.setattr(publisher.ctypes, "CDLL", lambda *args, **kwargs: Library())
    try:
        if change in ("before", "during_validation"):
            with pytest.raises(ValueError, match="previous native release manifest"):
                publisher.package(build, output, "1.3.0", "arm64")
        else:
            publisher.package(build, output, "1.3.0", "arm64")
    finally:
        if held is not None:
            os.close(held)
    assert (output / "notes/keep").read_bytes() == b"existing user notes"
    published = output / "home-screen-organizer-1.3.0-macos-arm64.zip"
    assert published.is_file() == (change not in ("before", "during_validation"))
    copies = [item for item in tmp_path.rglob("*") if item.is_file() and item.read_bytes() == payload]
    retained = list(tmp_path.glob(".native-release-*"))
    message = capsys.readouterr().err
    if change == "none":
        assert not copies and not retained and not message
        assert not old["zip"].exists() and not old["dmg"].exists()
    else:
        assert copies, "unverified replacement or observed edit must survive public package cleanup"
        assert all(item.stat().st_mode & 0o777 == 0o640 for item in copies)
        if change in ("before", "during_validation"):
            assert not retained and not message
        else:
            assert "Release installed" in message and "retained" in message
            assert any(str(folder) in message for folder in retained)
            assert all(folder.stat().st_mode & 0o777 == 0o700 for folder in retained)


@pytest.mark.parametrize("target", ["manifest", "zip"])
def test_previous_asset_validation_rejects_pipes_without_waiting_for_writer(tmp_path, target):
    import sys
    old_zip = tmp_path / "home-screen-organizer-1.2.0-macos-arm64.zip"
    old_dmg = old_zip.with_suffix(".dmg")
    rows = []
    for path in (old_zip, old_dmg):
        path.write_bytes(b"owned archive")
        rows.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n")
    manifest = tmp_path / "SHA256SUMS.txt"
    manifest.write_text("".join(rows))
    path = manifest if target == "manifest" else old_zip
    path.unlink()
    os.mkfifo(path)
    script = '''
from pathlib import Path
import sys
from macapp.release import previous_assets
try:
    previous_assets(Path(sys.argv[1]), "arm64")
except ValueError:
    pass
else:
    raise AssertionError("Only regular previous assets can establish ownership")
'''
    try:
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=1)
    except subprocess.TimeoutExpired:
        pytest.fail("Previous asset validation must reject a pipe without waiting for its writer")
    assert result.returncode == 0, result.stderr


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


def test_private_release_envelope_permissions_at_creation(tmp_path, monkeypatch):
    import tempfile
    from homescreen import private_store
    from macapp.release import private_transaction

    parent = tmp_path / "shared"
    parent.mkdir()
    subprocess.run(["chmod", "+a", "everyone allow list,search,directory_inherit", parent], check=True)
    created = []

    def sample(path):
        created.append((path.stat().st_mode & 0o777,
                        subprocess.check_output(["ls", "-lde", path], text=True).splitlines()[1:],
                        list(path.iterdir())))

    mkdtemp = tempfile.mkdtemp
    def temporary(*args, **kwargs):
        name = mkdtemp(*args, **kwargs)
        sample(Path(name))
        return name

    system = private_store._libsystem()
    class Creation:
        def __getattr__(self, name):
            return getattr(system, name)
        def mkdirx_np(self, path, security):
            status = system.mkdirx_np(path, security)
            if status == 0:
                sample(Path(os.fsdecode(path)))
            return status

    monkeypatch.setattr(tempfile, "mkdtemp", temporary)
    monkeypatch.setattr(private_store, "_libsystem", lambda: Creation())
    folder = private_transaction(parent)
    assert folder.parent == parent and created == [(0o700, [], [])]


def test_private_release_retries_exclusive_name(tmp_path, monkeypatch):
    import uuid
    from macapp.release import private_transaction

    first, second = uuid.UUID(int=1), uuid.UUID(int=2)
    occupied = tmp_path / (".native-release-" + first.hex)
    occupied.mkdir(mode=0o755)
    note = occupied / "note"
    note.write_bytes(b"existing")
    ids = iter([first, second])
    monkeypatch.setattr(uuid, "uuid4", lambda: next(ids))
    created = private_transaction(tmp_path)
    assert created.name == ".native-release-" + second.hex and not list(created.iterdir())
    assert created.stat().st_mode & 0o777 == 0o700
    assert occupied.stat().st_mode & 0o777 == 0o755 and note.read_bytes() == b"existing"


@pytest.mark.parametrize("relative", [".", "notes"])
@pytest.mark.parametrize("permission", ["mode", "acl", "remove-grant", "inheritance"])
@pytest.mark.parametrize("when", ["before-copy", "after-copy", "after-exchange"])
def test_release_keeps_directory_permission_changes(tmp_path, monkeypatch, relative, permission, when):
    import ctypes
    from macapp import release

    output, stage = tmp_path / "published", tmp_path / "stage"
    (output / "notes").mkdir(parents=True)
    stage.mkdir()
    (stage / "asset.zip").write_bytes(b"new release")
    note = output / "notes" / "record"
    note.write_bytes(b"user notes")
    external = tmp_path / "other-name"
    external.hardlink_to(note)
    (output / "shortcut").symlink_to("notes/record")
    selected = output / relative
    selected.chmod(0o755)
    if permission in ("remove-grant", "inheritance"):
        subprocess.run(["chmod", "+a", "everyone allow readattr,file_inherit,only_inherit", selected], check=True)
    identity = selected.stat().st_ino
    expected = []

    def permissions(directory):
        return (directory.stat().st_mode & 0o777,
                subprocess.check_output(["ls", "-lde", directory], text=True).splitlines()[1:])

    def change(directory):
        if permission == "mode":
            directory.chmod(0o700)
        elif permission in ("remove-grant", "inheritance"):
            subprocess.run(["chmod", "-N", directory], check=True)
            if permission == "inheritance":
                subprocess.run(["chmod", "+a", "everyone allow readattr,directory_inherit,only_inherit", directory],
                               check=True)
        else:
            subprocess.run(["chmod", "+a", "everyone deny execute,file_inherit,only_inherit", directory], check=True)
        expected.append(permissions(directory))

    system = ctypes.CDLL(None, use_errno=True)
    class DuringInstall:
        def __getattr__(self, name):
            return getattr(system, name)
        def fcopyfile(self, source, destination, state, flags):
            result = system.fcopyfile(source, destination, state, flags)
            if result == 0 and when == "after-copy" and not expected and os.fstat(source).st_ino == identity:
                change(selected)
            return result
        def renameatx_np(self, *args):
            result = system.renameatx_np(*args)
            if result == 0 and args[-1] == 2 and when == "after-exchange":
                change(stage / ".generation" / relative)
            return result

    if when == "before-copy":
        change(selected)
    monkeypatch.setattr(release.ctypes, "CDLL", lambda *args, **kwargs: DuringInstall())
    publication = {}
    if when == "after-copy":
        with pytest.raises(OSError, match="changed"):
            release.install_generation(stage, output, "arm64", publication=publication)
        assert not publication["installed"] and not (output / "asset.zip").exists()
    else:
        release.install_generation(stage, output, "arm64", publication=publication)
        assert publication["installed"] and (output / "asset.zip").is_file()
    assert expected == [permissions(selected)]
    assert note.samefile(external) and (output / "shortcut").read_bytes() == b"user notes"
    if when == "after-exchange":
        previous = publication["retained"] / "previous"
        assert permissions(previous / relative) == expected[0]
        assert (previous / "notes/record").samefile(external)


@pytest.mark.parametrize("identity", ["owner", "group"])
def test_retained_directory_checks_both_ownership_fields(tmp_path, monkeypatch, identity):
    import ctypes
    from macapp import release

    source, staged = tmp_path / "published", tmp_path / "stage"
    source.mkdir()
    staged.mkdir()
    (staged / "new").write_bytes(b"release")
    inode = source.stat().st_ino
    fstat = os.fstat
    copied = False
    def metadata(fd):
        info = fstat(fd)
        if copied and info.st_ino == inode:
            fields = list(info)
            fields[4 if identity == "owner" else 5] += 1
            return os.stat_result(fields)
        return info
    system = ctypes.CDLL(None, use_errno=True)
    class Copy:
        def __getattr__(self, name):
            return getattr(system, name)
        def fcopyfile(self, *args):
            nonlocal copied
            result = system.fcopyfile(*args)
            copied = True
            return result
    monkeypatch.setattr(release.ctypes, "CDLL", lambda *args, **kwargs: Copy())
    monkeypatch.setattr(release.os, "fstat", metadata)
    with pytest.raises(OSError, match="permissions changed"):
        release.install_generation(staged, source, "arm64")
    assert copied and not (source / "new").exists()


@pytest.mark.parametrize("relative", [".", "notes"])
@pytest.mark.parametrize("edit", ["mode", "acl", "independent", "replacement"])
def test_final_directory_removal_saves_exact_permissions(tmp_path, monkeypatch, relative, edit):
    import base64
    import ctypes
    from macapp import release

    output, stage = tmp_path / "public", tmp_path / "incoming"
    notes = output / "notes"
    notes.mkdir(parents=True)
    note = notes / "user-note"
    note.write_bytes(b"private note")
    external = tmp_path / "other-name"
    external.hardlink_to(note)
    stage.mkdir()
    (stage / "new.zip").write_bytes(b"fixture")
    selected = output / relative
    moved = tmp_path / "owner-moved-generation"
    selected.chmod(0o700)
    original = os.open(selected, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    identity = os.fstat(original)
    native = ctypes.CDLL(None, use_errno=True)
    monkeypatch.setattr(release.ctypes, "CDLL", lambda *args, **kwargs: native)
    rmdir = os.rmdir
    requested_time = 1650000000
    expected = []
    def remove(name, *, dir_fd=None):
        if dir_fd is not None and os.path.samestat(os.stat(name, dir_fd=dir_fd), identity):
            old = stage / ".generation"
            if relative != ".":
                old /= os.fsdecode(name)
            assert os.path.samestat(old.stat(), identity)
            if edit in ("acl", "independent"):
                subprocess.run(["chmod", "+a", "everyone allow readattr,directory_inherit,only_inherit", old], check=True)
            else:
                os.fchmod(original, 0o1700)
            if edit == "independent":
                selected.chmod(0o710)
            elif edit == "replacement":
                selected.rename(moved)
                selected.mkdir(mode=0o750)
                (selected / "owner-entry").write_bytes(b"independent")
            os.utime(selected, (requested_time, requested_time))
            expected.append(release.directory_permissions(original, native))
        return rmdir(name, dir_fd=dir_fd)
    monkeypatch.setattr(os, "rmdir", remove)
    state = {}
    try:
        release.install_generation(stage, output, "arm64", publication=state)
        saved = state["permissions_record"]
        assert saved.is_file() and saved.stat().st_mode & 0o777 == 0o600 and saved.parent.stat().st_mode & 0o777 == 0o700
        record = json.loads(saved.read_bytes())
        permissions = (record["mode"], record["uid"], record["gid"], base64.b64decode(record["aclBase64"], validate=True))
        assert expected == [permissions] and record["directory"] == relative
        assert (record["device"], record["inode"]) == (identity.st_dev, identity.st_ino)
        assert state["installed"] and bool(state["retained"]) == (relative != ".")
        live = os.open(selected, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            current = release.directory_permissions(live, native)
            if edit == "independent":
                assert current[0] == 0o710 and current[3] != permissions[3]
            elif edit == "replacement":
                assert current[0] == 0o750 and (selected / "owner-entry").read_bytes() == b"independent"
            else:
                assert current == permissions
        finally:
            os.close(live)
        assert int(selected.stat().st_mtime) == requested_time
        assets = moved if edit == "replacement" and relative == "." else output
        kept_note = moved / note.relative_to(selected) if edit == "replacement" else note
        assert (assets / "new.zip").exists() and kept_note.samefile(external) and kept_note.read_bytes() == b"private note"
    finally:
        os.close(original)


@pytest.mark.parametrize("failure", ["read", "save"])
def test_publication_reports_failure_to_recover_removed_permissions(tmp_path, monkeypatch, failure):
    from macapp import release

    output, stage = tmp_path / "release", tmp_path / "incoming"
    output.mkdir(mode=0o700)
    stage.mkdir()
    (stage / "new").write_bytes(b"release")
    identity = output.stat()
    rmdir, permissions = os.rmdir, release.directory_permissions
    removed = False
    def remove(name, *, dir_fd=None):
        nonlocal removed
        if dir_fd is not None and os.path.samestat(os.stat(name, dir_fd=dir_fd), identity):
            os.chmod(name, 0o1700, dir_fd=dir_fd)
            rmdir(name, dir_fd=dir_fd)
            removed = True
        else:
            rmdir(name, dir_fd=dir_fd)
    def sample(fd, libc):
        if failure == "read" and removed and os.path.samestat(os.fstat(fd), identity):
            raise OSError("fixture descriptor failure")
        return permissions(fd, libc)
    def write_failure(change, record):
        raise OSError("fixture write failure")
    monkeypatch.setattr(os, "rmdir", remove)
    monkeypatch.setattr(release, "directory_permissions", sample)
    if failure == "save":
        monkeypatch.setattr(release._RemovedDirectoryChanged, "save", write_failure)
    state = {}
    with pytest.raises(OSError, match=f"Release installed. Could not {failure}"):
        release.install_generation(stage, output, "arm64", publication=state)
    assert state["installed"] and (output / "new").is_file()
    assert not state.get("permissions_record") and not list(tmp_path.glob(".native-release-*"))


@pytest.mark.parametrize("reservation", ["ordinary", "occupied", "stable", "unnamed", "changed-after-save",
                                         "changed-before-report",
                                         "acl-parent-before-report", "acl-record-before-report", "acl-error-before-report"])
def test_package_reports_only_owned_recovery_names(tmp_path, monkeypatch, capsys, reservation):
    import build_paths
    import check_package_paths
    from macapp import release

    output, build = tmp_path / "published", tmp_path / "build"
    output.mkdir(mode=0o700)
    identity = output.stat()
    note = output / "note"
    note.write_bytes(b"kept")
    outside = tmp_path / "outside-link"
    outside.hardlink_to(note)
    app = build / "Screen Organizer.app/Contents"
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": VERSION,
                                                     "HomeScreenOrganizerArchitecture": "arm64"}))
    (build / f"home-screen-organizer-{VERSION}.dmg").write_bytes(b"owned unmounted fixture")
    monkeypatch.setattr(build_paths, "read_roots", lambda _: [])
    monkeypatch.setattr(build_paths, "runner_roots", lambda: [])
    monkeypatch.setattr(check_package_paths, "check_archive", lambda *args: None)
    monkeypatch.setattr(release, "clean_copy", shutil.copytree)
    monkeypatch.setattr(release, "verify_image", lambda *args: None)

    # Prepare the possible reservations as ordinary, independent fixture entries.
    staging, protected = (release.private_transaction(tmp_path) for _ in range(2))
    containers = iter((staging, protected))
    monkeypatch.setattr(release, "private_transaction", lambda _: next(containers))
    name = "directory-permissions.json"
    marker = tmp_path / "marker"
    marker.write_bytes(b"fixture marker")
    if reservation == "occupied":
        (protected / name).hardlink_to(marker)
    if reservation == "unnamed":
        reserve = release._reserve_permissions_record
        def provide_stream(directory, chosen, stack):
            return reserve(directory, "unnamed-fixture" if chosen == name else chosen, stack)
        monkeypatch.setattr(release, "_reserve_permissions_record", provide_stream)
    def edit_saved_record():
        with (protected / name).open("r+b") as other:
            assert other.read(1) == b"{"
            other.seek(0)
            other.write(b"!")
    if reservation == "changed-after-save":
        save = release._RemovedDirectoryChanged.save
        def save_then_edit(change, stream):
            save(change, stream)
            edit_saved_record()
        monkeypatch.setattr(release._RemovedDirectoryChanged, "save", save_then_edit)
    if reservation == "changed-before-report" or reservation.startswith("acl-"):
        cleanup = shutil.rmtree
        def cleanup_then_edit(folder):
            cleanup(folder)
            if reservation == "changed-before-report":
                edit_saved_record()
            elif reservation == "acl-error-before-report":
                from homescreen import private_store
                def unreadable(fd):
                    raise OSError("fixture ACL inspection failed")
                monkeypatch.setattr(private_store, "_check_darwin_acl", unreadable)
            else:
                target = protected if reservation == "acl-parent-before-report" else protected / name
                grant = "everyone allow list,search,readattr,readsecurity" if target == protected else "everyone allow read,readattr,readsecurity"
                subprocess.run(["/bin/chmod", "+a", grant, target], check=True, capture_output=True)
        monkeypatch.setattr(shutil, "rmtree", cleanup_then_edit)
    inspect = release._DirectoryCopy.check_removed
    def inspect_removed(directory, libc):
        if reservation not in ("ordinary", "occupied") and directory.parent is None:
            os.fchmod(directory.source, 0o1700)
        inspect(directory, libc)
    monkeypatch.setattr(release._DirectoryCopy, "check_removed", inspect_removed)
    monkeypatch.setattr(sys, "argv", ["release.py", "package", "--arch", "arm64", "--build-dir", str(build),
                                     "--output-dir", str(output)])
    if reservation.startswith(("changed-", "acl-")):
        with pytest.raises(SystemExit) as status:
            release.main()
        result = capsys.readouterr()
        assert status.value.code == 1 and not result.out
        assert "Release installed." in result.err and "Could not verify the saved directory permissions." in result.err
        assert "permissions were saved in" not in result.err
        if reservation.startswith("changed-"):
            assert (protected / name).read_bytes().startswith(b"!")
        else:
            assert json.loads((protected / name).read_bytes())["mode"] == 0o1700
            assert protected.stat().st_mode & 0o777 == 0o700
            assert (protected / name).stat().st_mode & 0o777 == 0o600
        assert (output / f"home-screen-organizer-{VERSION}-macos-arm64.zip").is_file() and note.samefile(outside)
        return
    release.main()
    result = capsys.readouterr()
    assert result.out.strip() == VERSION and note.samefile(outside) and note.read_bytes() == b"kept"
    if reservation == "ordinary":
        assert not protected.exists() and not result.err
    elif reservation == "occupied":
        assert (protected / name).samefile(marker) and marker.stat().st_nlink == 2
        assert os.path.samestat((protected / "previous").stat(), identity) and str(protected) in result.err
        assert "permissions were saved" not in result.err
    else:
        saved = Path(result.err.partition("Previous directory permissions were saved in ")[2].rstrip(".\n"))
        record = json.loads(saved.read_bytes())
        assert saved.parent == protected and saved.stat().st_mode & 0o777 == 0o600
        assert protected.stat().st_mode & 0o777 == 0o700 and record["mode"] == 0o1700
        assert (record["device"], record["inode"]) == (identity.st_dev, identity.st_ino)
        if reservation == "unnamed":
            assert saved.name != name and (protected / "unnamed-fixture").read_bytes() == b""


@pytest.mark.parametrize("final_mode, owner_change, private_record, accepted", [
    (0o700, 0, True, True),
    (0o500, 0, True, False),
    (0o700, 1, True, False),
    (0o500, 1, False, True),
], ids=["stable-private", "mode-changed", "owner-changed", "nonprivate"])
def test_recovery_checks_final_parent_permissions(tmp_path, monkeypatch, final_mode, owner_change,
                                                 private_record, accepted):
    import stat
    from macapp import release

    parent = tmp_path / "recovery"
    parent.mkdir(mode=0o700)
    record = parent / "record"
    payload = b"saved permissions\n"
    record.write_bytes(payload)
    record.chmod(0o600)
    first = parent.lstat()
    # Change just the final parent observation while reading the real record.
    final = list(first)
    final[stat.ST_MODE] = stat.S_IFDIR | final_mode
    final[stat.ST_UID] += owner_change
    samples = iter((first, os.stat_result(final)))
    lstat = Path.lstat

    def sample(path):
        return next(samples) if path == parent else lstat(path)

    monkeypatch.setattr(Path, "lstat", sample)

    assert release._recovery_still_named(record, release.entry_identity(first), release.entry_identity(record.lstat()),
                                         private_record=private_record, payload=payload) is accepted


@pytest.mark.parametrize("fail_at", [1, 2])
def test_release_exchange_records_recovery_before_further_io(tmp_path, monkeypatch, fail_at):
    import ctypes
    import errno
    from macapp import release

    source, destination = tmp_path / "staged", tmp_path / "release"
    source.mkdir()
    destination.mkdir()
    (source / "new.zip").write_bytes(b"release")
    (destination / "note").write_bytes(b"note")
    original = destination.stat()
    state = {}
    native = ctypes.CDLL(None, use_errno=True)
    real_stat = os.fstat
    exchanged = False
    calls = 0
    class Native:
        def __getattr__(self, name):
            return getattr(native, name)
        def renameatx_np(self, *args):
            nonlocal exchanged
            result = native.renameatx_np(*args)
            if args[-1] == 2 and not result:
                exchanged = True
            return result
    def inspect(fd):
        nonlocal calls
        if exchanged:
            calls += 1
            if calls == fail_at:
                assert state["installed"] and state["retained_binding"][2] == release.entry_identity(original)
                raise OSError(errno.EIO, "private fixture fault")
        return real_stat(fd)
    monkeypatch.setattr(release.ctypes, "CDLL", lambda *args, **kwargs: Native())
    monkeypatch.setattr(os, "fstat", inspect)
    with pytest.raises(OSError, match="Release installed. Could not verify"):
        release.install_generation(source, destination, "arm64", publication=state)
    assert (destination / "new.zip").read_bytes() == b"release"
    previous = state["retained"] / "previous"
    assert os.path.samestat(previous.stat(), original) and (previous / "note").read_bytes() == b"note"


@pytest.mark.parametrize("edit", ["stricter-installed", "different-installed", "before-retirement"])
def test_late_directory_changes_preserve_both_owners(tmp_path, monkeypatch, edit):
    import ctypes
    from macapp import release

    output, stage = tmp_path / "release", tmp_path / "incoming"
    notes = output / "notes"
    notes.mkdir(parents=True, mode=0o755)
    (notes / "record").write_bytes(b"original")
    stage.mkdir()
    (stage / "new").write_bytes(b"release")
    original = stage / ".generation"
    system = ctypes.CDLL(None, use_errno=True)
    class Publication:
        def __getattr__(self, name):
            return getattr(system, name)
        def renameatx_np(self, *args):
            status = system.renameatx_np(*args)
            if status == 0 and args[-1] == 2 and edit != "before-retirement":
                (original / "notes").chmod(0o750)
                if edit == "different-installed":
                    notes.rename(output / "first copy")
                    notes.mkdir(mode=0o700)
                notes.chmod(0o700)
            return status
    monkeypatch.setattr(release.ctypes, "CDLL", lambda *args, **kwargs: Publication())
    retire = release.retire_entry
    def before_retirement(*args, **kwargs):
        if edit == "before-retirement" and original.exists():
            original.chmod(0o700)
        return retire(*args, **kwargs)
    monkeypatch.setattr(release, "retire_entry", before_retirement)
    state = {}
    release.install_generation(stage, output, "arm64", publication=state)
    assert state["installed"] and state["retained"] and (output / "new").read_bytes() == b"release"
    kept = state["retained"] / "previous"
    assert (kept / "notes/record").read_bytes() == b"original"
    if edit == "before-retirement":
        assert kept.stat().st_mode & 0o777 == output.stat().st_mode & 0o777 == 0o700
    else:
        assert notes.stat().st_mode & 0o777 == 0o700
        assert (kept / "notes").stat().st_mode & 0o777 == 0o750


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


@pytest.mark.parametrize("prefix", ["-I", "source"])
def test_image_error_note_redacts_token_attached_paths(prefix):
    from macapp.release import image_error_note
    reason = f"hdiutil: {prefix}/private/other-account/file failed".encode()
    error = subprocess.CalledProcessError(1, ["hdiutil", "attach"], stderr=reason)
    note = image_error_note(error, Path("/owned-native-image/release.dmg"), Path("/owned-native-mount/volume"))
    assert "/private/other-account" not in note and "<path>" in note
    assert error.stderr is reason


def test_image_error_note_normalizes_ansi_before_matching_known_root():
    from macapp.release import image_error_note
    image = Path("/private/owned-sensitive-root/release.dmg")
    reason = b"hdiutil: -I/private/owned-sensitive-\x1b[31mroot/file failed"
    error = subprocess.CalledProcessError(1, ["hdiutil", "attach"], output=b"native stdout", stderr=reason)
    note = image_error_note(error, image, Path("/owned-native-mount/volume"))
    assert "/private/owned-sensitive-root" not in note
    assert "-I<native-build>" in note
    assert all(character.isprintable() for character in note) and len(note) <= 560
    assert error.cmd == ["hdiutil", "attach"] and error.returncode == 1
    assert error.output == b"native stdout" and error.stderr is reason


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin ACL contract")
@pytest.mark.parametrize("where,accepted", [("private", True), ("parent", False), ("record", False),
                                            ("deny-only", True), ("inspection-error", False)])
@pytest.mark.parametrize("check_payload", [False, True])
def test_recovery_record_checks_darwin_acls(tmp_path, monkeypatch, where, accepted, check_payload):
    from macapp import release
    from homescreen import private_store
    parent = release.private_transaction(tmp_path)
    record = parent / "record"
    payload = b"saved permissions\n"
    record.write_bytes(payload)
    record.chmod(0o600)
    if where in ("parent", "record", "deny-only"):
        target = record if where == "record" else parent
        grant = {"parent": "everyone allow list,search,readattr,readsecurity",
                 "record": "everyone allow read,readattr,readsecurity", "deny-only": "everyone deny delete"}[where]
        subprocess.run(["/bin/chmod", "+a", grant, target], check=True, capture_output=True)
    elif where == "inspection-error":
        def unreadable(fd):
            raise OSError("fixture ACL inspection failed")
        monkeypatch.setattr(private_store, "_check_darwin_acl", unreadable)
    binding = release.entry_identity(parent.lstat()), release.entry_identity(record.lstat())
    assert release._recovery_still_named(record, *binding, private_record=True,
                                         payload=payload if check_payload else None) is accepted
    assert record.read_bytes() == payload
    assert release._recovery_still_named(record, *binding, payload=payload)


def test_recovery_record_binds_the_open_parent(tmp_path, monkeypatch):
    from macapp import release
    parent = release.private_transaction(tmp_path)
    record = parent / "record"
    record.write_bytes(b"saved permissions")
    record.chmod(0o600)
    binding = release.entry_identity(parent.lstat()), release.entry_identity(record.lstat())
    opened = os.open
    retained = tmp_path / "retained"
    def replace_parent(path, flags, *args, **kwargs):
        if path == parent:
            parent.rename(retained)
            parent.mkdir(mode=0o700)
            (parent / "record").write_bytes(b"saved permissions")
            (parent / "record").chmod(0o600)
        return opened(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", replace_parent)
    assert not release._recovery_still_named(record, *binding, private_record=True, payload=b"saved permissions")
    assert (retained / "record").read_bytes() == b"saved permissions"
