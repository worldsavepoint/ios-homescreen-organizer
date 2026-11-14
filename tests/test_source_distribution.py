"""Native distributions supply the exact editable inputs and copyright notices."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "macapp"))


def test_source_lock_covers_known_frozen_owners():
    lock = ROOT / "macapp/sources/python.json"
    assert lock.is_file(), "The release needs component source pins"
    packages = json.loads(lock.read_text())
    assert packages["h11"]["version"] == "0.16.0"
    assert packages["pymobiledevice3"]["source"]["sha256"] == "097eefb68b9f04f33d564c122a78b7d2d7d71437c0a438f0a086954e755ccd29"
    assert packages["pyinstaller-hooks-contrib"]["wheels"]["x86_64"]
    assert packages["pylzss"]["license"] == "LGPL-3.0-only"
    assert "asttokens" not in packages


def test_cached_input_must_match_digest_and_size(tmp_path):
    import source_inputs
    content = b"verified source\n"
    digest = hashlib.sha256(content).hexdigest()
    pin = {"url": "https://example.org/releases/project.tar.gz", "size": len(content), "sha256": digest}
    (tmp_path / (digest + "-project.tar.gz")).write_bytes(content)
    assert source_inputs.fetch(pin, tmp_path, offline=True).read_bytes() == content
    (tmp_path / (digest + "-project.tar.gz")).write_bytes(b"x" * len(content))
    with pytest.raises(ValueError, match="verified source"):
        source_inputs.fetch(pin, tmp_path, offline=True)


@pytest.mark.parametrize("name", ["/outside", "a/../../outside", "a//b", r"a\b", ".git/config", "a\nline"])
def test_public_names_reject_unsafe_members(name):
    import source_inputs
    with pytest.raises(ValueError):
        source_inputs.relative_name(name)



def test_exact_wheel_record_ownership_uses_full_path(tmp_path):
    from frozen_sources import installed_files
    from importlib.metadata import distributions
    for name in ("first", "second"):
        info = tmp_path / (name + "-1.0.dist-info")
        info.mkdir()
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: " + name + "\nVersion: 1.0\n")
        (info / "RECORD").write_text(name + "/shared.py,,\n")
        (tmp_path / name).mkdir()
        (tmp_path / name / "shared.py").write_text(name)
    files = installed_files(distributions(path=[str(tmp_path)]))
    assert files[tmp_path / "first/shared.py"][:2] == ("first", "1.0")
    assert files[tmp_path / "second/shared.py"][:2] == ("second", "1.0")
    (tmp_path / "second-1.0.dist-info/RECORD").write_text("first/shared.py,,\n")
    with pytest.raises(ValueError, match="Multiple owners"):
        installed_files(distributions(path=[str(tmp_path)]))


def test_notices_and_overlays_compare_exact_wheel_bytes(tmp_path):
    import io
    import tarfile
    import zipfile
    from frozen_sources import wheel_material
    wheel = tmp_path / "demo.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("demo/payload.py", "x = 2\n")
        z.writestr("demo/identical.py", "x = 1\n")
        z.writestr("demo-1.0.dist-info/licenses/a/LICENSE", b"First upstream notice\n")
        z.writestr("demo-1.0.dist-info/licenses/b/LICENSE", b"Second upstream notice\n")
    source = tmp_path / "demo.tar.gz"
    with tarfile.open(source, "w:gz") as tar:
        for name in ("payload", "identical"):
            info = tarfile.TarInfo("demo-1.0/src/demo/" + name + ".py")
            info.size = len(b"x = 1\n")
            tar.addfile(info, io.BytesIO(b"x = 1\n"))
    notice = tmp_path / "notices"
    overlays = tmp_path / "overlays"
    with zipfile.ZipFile(wheel) as z:
        wheel_material("demo", z, source, {"demo/payload.py", "demo/identical.py"}, notice, overlays)
    assert (notice / "demo/demo-1.0.dist-info/licenses/a/LICENSE").read_bytes() == b"First upstream notice\n"
    assert (notice / "demo/demo-1.0.dist-info/licenses/b/LICENSE").read_bytes() == b"Second upstream notice\n"
    assert (overlays / "demo/demo/payload.py").read_bytes() == b"x = 2\n"
    assert not (overlays / "demo/demo/identical.py").exists()



def test_native_lock_contains_build_sources_and_pinned_interpreters():
    native_file = ROOT / "macapp/sources/native.json"
    assert native_file.is_file(), "Native libraries need pinned source and provider inputs"
    native = json.loads(native_file.read_text())
    table = {entry["id"]: entry for entry in native["assets"]}
    for name in ("avif/aom", "avif/dav1d", "avif/libyuv", "pbs-sources/uuid", "pbs/recipe", "pillow/multibuild"):
        assert table[name]["size"] > 0
    assert table["openssl/4.0.3"]["sha256"] == "325b5c806167c13b40b1ffeadfe0248197c00eccc4cf123ec1e28d2d2fd216d9"
    assert {v["role"] for v in table.values()} >= {"cargo-source", "interpreter-source", "native-build-recipe"}
    assert native["runtime"]["arm64"]["libpython"] == "16d39098fb247e8386e112396a6ebccbcc89586155af7adce89c2f5428287ba9"
    assert native["runtime"]["x86_64"]["libpython"] == "baf4fc3516b40b2f1a48522543bad9762a11890e23830bee9eeb9c2f51d16c25"



def test_release_selects_editable_pure_chardet_on_both_architectures():
    uri = "https://files.pythonhosted.org/packages/cc/75/88a3eb36264375ff951bd2f9397c409727ab6aa6a18e1bd933851571d421/chardet-7.7.0-py3-none-any.whl"
    checksum = "11cf9b719ea3675a2a7d8a3071bbe4d7ea24b26bfe39a51588d515ddd05cacad"
    for name in ("requirements-build.lock", "requirements-build-x86_64.lock"):
        assert f"chardet @ {uri} --hash=sha256:{checksum}" in (ROOT / name).read_text().splitlines()



def test_source_archive_rejects_unsafe_metadata_and_rebuilds_reproducibly(tmp_path):
    import io
    import tarfile
    from source_archive import payload_tar, verify_tar

    def inputs():
        yield "project/macapp/build.sh", 0o755, io.BytesIO(b"build"), 5
        yield "sources/h11.tar.gz", 0o644, io.BytesIO(b"source"), 6
    hashes = []
    for index in range(2):
        output = tmp_path / str(index)
        payload_tar(output, inputs(), {"version": "1.3.0", "commit": "a" * 40, "architectures": {}, "inputs": {}})
        verify_tar(output, version="1.3.0", commit="a" * 40)
        hashes.append(hashlib.sha256(output.read_bytes()).hexdigest())
        with tarfile.open(output) as archive:
            assert archive.extractfile("project/macapp/build.sh").read() == b"build"
            assert archive.getmember("project/macapp/build.sh").mode == 0o755
    assert hashes[0] == hashes[1]
    with pytest.raises(ValueError):
        verify_tar(output, version="1.3.0", commit="b" * 40)
    with pytest.raises(ValueError):
        payload_tar(tmp_path / "unsafe", [("../outside", 0o644, io.BytesIO(b"x"), 1)], {})



@pytest.mark.parametrize("change", ["uname", "comment", "manifest", "arch", "oversize", "trailing", "gzip",
                                    "gzip-name", "gzip-comment", "gzip-extra", "encoding"])
def test_publication_rejects_private_archive_envelope(tmp_path, change):
    import gzip
    import io
    import tarfile
    from source_archive import verify_tar
    from source_fixture import release_source
    from source_inputs import json_bytes

    release = tmp_path / "release"
    release.mkdir()
    root = tmp_path / "checkout"
    commit = release_source(release, "1.3.0", root)
    path = next(release.glob("*-source.tar.gz"))
    verify_tar(path, "1.3.0", commit, release=True, root=root, repository="example/project")
    with tarfile.open(path) as original:
        rows = [(member, original.extractfile(member).read()) for member in original]
    with path.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0, compresslevel=1) as compressed:
        if change == "oversize":
            header = tarfile.TarInfo("././@PaxHeader")
            header.type, header.size = tarfile.XHDTYPE, 3 * 1024**2
            compressed.write(header.tobuf())
        else:
            with tarfile.open(mode="w|", fileobj=compressed) as archive:
                for member, data in rows:
                    if member.name == "README.md" and change == "uname":
                        member.uname = "private-builder"
                    if member.name == "README.md" and change == "comment":
                        member.pax_headers = {"comment": "private fixture note"}
                    if member.name == "MANIFEST.json" and change in ("manifest", "arch", "encoding"):
                        manifest = json.loads(data)
                        if change == "manifest":
                            manifest["builder"] = "private fixture note"
                        if change == "arch":
                            manifest["architectures"]["arm64"]["builder"] = "private fixture note"
                        data = json_bytes(manifest) if change != "encoding" else json.dumps(manifest, indent=2).encode()
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            if change == "trailing":
                compressed.write(b"private trailing record")
    if change == "gzip":
        with path.open("ab") as raw, gzip.GzipFile(filename="private-builder", fileobj=raw, mode="wb") as compressed:
            compressed.write(b"")
    fields = {"gzip-name": (8, b"private-file\x00"), "gzip-comment": (16, b"private-comment\x00"),
              "gzip-extra": (4, b"\x07\x00private")}
    if change in fields:
        raw = bytearray(path.read_bytes())
        raw[3], raw[10:10] = fields[change]
        path.write_bytes(raw)
    with pytest.raises(ValueError, match="source|Source"):
        verify_tar(path, "1.3.0", commit, release=True, root=root, repository="example/project")


def test_editable_component_extracts_under_one_directory(tmp_path):
    import io
    import tarfile
    from rebuild_sources import unpack_source
    archive = tmp_path / "editable.tar"
    with tarfile.open(archive, "w") as tar:
        for name in ("demo-1/setup.py", "demo-1/src/demo.py"):
            member = tarfile.TarInfo(name)
            member.size = len(b"x = 1\n")
            tar.addfile(member, io.BytesIO(b"x = 1\n"))
    output = tmp_path / "editable"
    unpack_source(archive, output)
    assert (output / "src/demo.py").read_text() == "x = 1\n"
    with tarfile.open(archive, "w") as tar:
        link = tarfile.TarInfo("demo-1/escape")
        link.type, link.linkname = tarfile.SYMTYPE, "../../outside"
        tar.addfile(link)
    with pytest.raises(ValueError):
        unpack_source(archive, tmp_path / "bad")


def test_local_refreeze_records_actual_substitution_and_stays_outside_release(tmp_path):
    from rebuild_sources import local_selections
    original = tmp_path / "original.lock"
    edited = tmp_path / "edited.lock"
    original.write_text("# h11-0.16.0-py3-none-any.whl\nh11==0.16.0 --hash=sha256:" + "a" * 64 + "\n")
    edited.write_text(original.read_text().replace("a" * 64, "b" * 64))
    assert local_selections(original, edited, {"h11"}) == {"h11": {"upstreamWheel": "a" * 64, "wheel": "b" * 64}}
    edited.write_text(edited.read_text().replace("h11", "other"))
    with pytest.raises(ValueError, match="release component"):
        local_selections(original, edited, {"h11"})



def test_signed_inventory_and_source_header_are_required(tmp_path):
    from source_archive import check_zip
    from test_mcp_bundle import assets
    folder = assets(tmp_path)
    package = folder / "home-screen-organizer-1.3.0-macos-arm64.zip"
    altered = {"arch": "arm64", "version": "1.3.0"}
    with pytest.raises(ValueError, match="signed source inventory"):
        check_zip(package, altered)
    pins = json.loads((ROOT / "macapp/sources/python.json").read_bytes())
    assert "pylzss-0.3.9/src/include/py3c.h" in pins["pylzss"]["notices"]


def test_local_extract_skips_git_pointer_and_checks_promoted_root(tmp_path):
    import io
    import tarfile
    from rebuild_sources import unpack_source
    path = tmp_path / "source.tar"
    with tarfile.open(path, "w") as tar:
        for name, data in [("project/source.py", b"x = 1"), ("project/suite/.git", b"gitdir: ../.git/modules/suite\n")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    unpack_source(path, tmp_path / "good")
    assert (tmp_path / "good/source.py").is_file()
    assert not (tmp_path / "good/suite/.git").exists()
    with tarfile.open(path, "w") as tar:
        link = tarfile.TarInfo("project/untrusted")
        link.type, link.linkname = tarfile.SYMTYPE, "../promoted-outside"
        tar.addfile(link)
    with pytest.raises(ValueError, match="link"):
        unpack_source(path, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_provider_matches_actual_collected_stdlib(tmp_path):
    import io
    import tarfile
    from native_sources import verify_provider_files
    path = tmp_path / "runtime.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("python/lib/python3.12/stat.py")
        info.size = 7
        tar.addfile(info, io.BytesIO(b"trusted"))
    verify_provider_files(path, {"lib/python3.12/stat.py": hashlib.sha256(b"trusted").hexdigest()})
    with pytest.raises(ValueError, match="Python"):
        verify_provider_files(path, {"lib/python3.12/stat.py": hashlib.sha256(b"changed").hexdigest()})


def test_official_freeze_exports_clean_commit_and_excludes_ignored_files(tmp_path):
    import subprocess
    from source_checkout import export_project
    tree = tmp_path / "tree"
    tree.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", tree, *args], text=True).strip()
    git("init", "-q")
    (tree / "module.py").write_text("original")
    (tree / ".gitignore").write_text("local.py\n")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "-c", "commit.gpgsign=false", "commit", "-qm", "Fixture")
    (tree / "local.py").write_text("ignored")
    destination = tmp_path / "release"
    assert export_project(tree, destination) == git("rev-parse", "HEAD")
    assert (destination / "module.py").read_text() == "original" and not (destination / "local.py").exists()
    (tree / "module.py").write_text("edited")
    with pytest.raises(ValueError, match="clean release"):
        export_project(tree, tmp_path / "invalid")


def test_local_project_can_be_refrozen_with_unmodified_dependency_lock(tmp_path):
    from rebuild_sources import local_selections
    lock = tmp_path / "original.lock"
    lock.write_text("# h11-0.16.0-py3-none-any.whl\nh11==0.16.0 --hash=sha256:" + "a" * 64 + "\n")
    assert local_selections(lock, lock, {"h11"}) == {}


def test_local_install_lock_retains_wheel_identity_without_download_location(tmp_path):
    import subprocess
    from wheelhouse import entries

    wheel = "h11-0.16.0-py3-none-any.whl"
    value = hashlib.sha256(b"locally modified wheel").hexdigest()
    private = (tmp_path / "private build" / wheel).as_uri()
    download = tmp_path / "download.lock"
    download.write_text(f"# {wheel}\nh11 @ {private} --hash=sha256:{value}\n")
    output = tmp_path / "install.lock"
    subprocess.run([sys.executable, ROOT / "macapp/wheelhouse.py", "--install-lock", output, download], check=True)
    assert list(entries(output)) == [(wheel, value)]
    assert output.read_text() == f"# {wheel}\nh11==0.16.0 --hash=sha256:{value}\n"


def test_git_export_ignores_local_archive_attributes(tmp_path):
    import subprocess
    from source_checkout import export_project
    def git(*args):
        return subprocess.check_output(["git", "-C", tmp_path, *args], text=True).strip()
    git("init", "-q")
    (tmp_path / "tracked").write_text("original blob\n")
    git("add", "tracked")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "-c", "commit.gpgsign=false", "commit", "-qm", "Fixture")
    (tmp_path / ".git/info/attributes").write_text("tracked export-ignore\n")
    output = tmp_path.parent / (tmp_path.name + "-export")
    export_project(tmp_path, output)
    assert (output / "tracked").read_text() == "original blob\n"


def test_owner_map_reads_one_metadata_document_per_distribution(tmp_path):
    from importlib.metadata import PathDistribution
    from frozen_sources import installed_files

    documents = []
    class RecordedDistribution(PathDistribution):
        def read_text(self, filename):
            if filename == "METADATA":
                documents.append(self._path.name)
            return super().read_text(filename)

    packages = []
    for owner in ("first", "second"):
        metadata = tmp_path / f"{owner}-1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(f"Name: {owner}\nVersion: 1.0\n")
        files = [f"{owner}/shared.py", f"{owner}/other.py", f"{owner}/data"]
        (tmp_path / owner).mkdir()
        for name in files:
            (tmp_path / name).write_bytes(b"owned input")
        (metadata / "RECORD").write_text("".join(name + ",,\n" for name in files))
        packages.append(RecordedDistribution(metadata))
    result = installed_files(packages)
    assert result == {tmp_path / owner / name: (owner, "1.0", f"{owner}/{name}")
                      for owner in ("first", "second") for name in ("shared.py", "other.py", "data")}
    assert documents == ["first-1.0.dist-info", "second-1.0.dist-info"]


@pytest.mark.parametrize("format", ["zip", "tar"])
@pytest.mark.parametrize("damage", [None, "missing", "changed", "duplicate", "link", "large"])
def test_selected_source_notices_are_verified_in_one_archive_pass(tmp_path, monkeypatch, format, damage):
    import io
    import stat
    import tarfile
    import zipfile
    import source_inputs

    records = [("first/LICENSE", b"first notice"), ("second/LICENSE", b"second notice")]
    expected = {name: hashlib.sha256(data).hexdigest() for name, data in records}
    path = tmp_path / ("source." + format)
    if damage == "missing":
        records.pop()
    elif damage == "changed":
        records[-1] = (records[-1][0], b"changed")
    elif damage == "duplicate":
        records.append(records[-1])
    if format == "zip":
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in records:
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFLNK | 0o777 if damage == "link" else stat.S_IFREG | 0o644) << 16
                if name in archive.NameToInfo:
                    with pytest.warns(UserWarning, match="Duplicate name"):
                        archive.writestr(info, data)
                else:
                    archive.writestr(info, data)
    else:
        with tarfile.open(path, "w") as archive:
            for name, data in records:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                if damage == "link":
                    info.type, info.linkname = tarfile.SYMTYPE, "other"
                    archive.addfile(info)
                else:
                    archive.addfile(info, io.BytesIO(data))
    if damage == "large":
        monkeypatch.setattr(source_inputs, "BLOCK", 0)
    opens = []
    original = zipfile.ZipFile if format == "zip" else tarfile.open
    def tracked(*args, **kwargs):
        opens.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(zipfile if format == "zip" else tarfile, "ZipFile" if format == "zip" else "open", tracked)
    if damage:
        with pytest.raises(ValueError):
            source_inputs.source_members(path, expected)
    else:
        assert source_inputs.source_members(path, expected) == dict(records)
        assert opens == [path]
        assert source_inputs.source_member(path, "second/LICENSE") == b"second notice"


@pytest.mark.parametrize("declared,normalized", [
    ("1.3.0", "1.3.0"), ("1.3.0-alpha.2", "1.3.0a2"),
    ("1.3.0-beta.1", "1.3.0b1"), ("1.3.0-rc.3", "1.3.0rc3"),
])
def test_capture_uses_declared_release_spelling_after_real_wheel_metadata(tmp_path, declared, normalized):
    import subprocess
    import zipfile
    from importlib.metadata import distributions
    from frozen_sources import project_release_version, distribution_notice
    from source_archive import check_zip, validate_inventory
    from source_fixture import release_source

    project = tmp_path / "editable"
    project.mkdir()
    (project / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools", "wheel"]\n'
        'build-backend = "setuptools.build_meta"\n[project]\nname = "home-screen-organizer"\n'
        f'version = "{declared}"\n[tool.setuptools]\npy-modules = ["example"]\n')
    (project / "example.py").write_text("value = 1\n")
    wheels = tmp_path / "wheels"
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-build-isolation",
                    "--wheel-dir", wheels, project], check=True, capture_output=True)
    wheel, = wheels.glob("*.whl")
    installed = tmp_path / "installed"
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--target", installed, wheel],
                   check=True, capture_output=True)
    package, = distributions(path=[str(installed)])
    assert package.version == normalized
    selected = project_release_version(project, package.version)
    assert selected == declared
    with pytest.raises(ValueError, match="version"):
        project_release_version(project, "9.0.0")
    release = tmp_path / "release"
    release.mkdir()
    checkout = tmp_path / "checkout"
    release_source(release, selected, checkout)
    packages = json.loads((checkout / "macapp/sources/python.json").read_bytes())
    native = json.loads((checkout / "macapp/sources/native.json").read_bytes())
    for arch in ("arm64", "x86_64"):
        archive = release / f"home-screen-organizer-{declared}-macos-{arch}.zip"
        with zipfile.ZipFile(archive) as zipped:
            inventory = json.loads(zipped.read("Screen Organizer.app/Contents/Resources/SourceInventory.json"))
        validate_inventory(inventory, arch, declared, packages, native)
        assert check_zip(archive, inventory) == distribution_notice("example/project", declared)
        assert f"/v{declared}/".encode() in check_zip(archive, inventory)


def git_blob_tree(tmp_path):
    import subprocess
    def git(*args):
        return subprocess.check_output(["git", "-C", tmp_path, *args]).strip()
    git("init", "-q")
    files = {"empty": (b"", 0o644), "space name": (b"\x00\xff\r\n", 0o644), "executable": (b"run", 0o755)}
    for name, (data, mode) in files.items():
        (tmp_path / name).write_bytes(data)
        (tmp_path / name).chmod(mode)
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "-c", "commit.gpgsign=false", "commit", "-qm", "Fixture")
    return git("rev-parse", "HEAD").decode(), files


def test_git_export_streams_verified_blobs_through_one_child_and_reaps_early_exit(tmp_path, monkeypatch):
    import subprocess
    import source_checkout
    commit, files = git_blob_tree(tmp_path)
    original = subprocess.Popen
    children = []
    def started(args, *a, **kw):
        process = original(args, *a, **kw)
        if "cat-file" in args:
            children.append((args, process))
        return process
    monkeypatch.setattr(subprocess, "Popen", started)
    assert {name: (data, mode) for name, mode, data in source_checkout.tracked_blobs(tmp_path, commit)} == files
    assert len(children) == 1 and "--batch" in children[0][0]
    assert children[0][1].poll() == 0
    iterator = source_checkout.tracked_blobs(tmp_path, commit)
    next(iterator)
    iterator.close()
    assert len(children) == 2 and children[-1][1].poll() is not None


@pytest.mark.parametrize("damage", ["missing", "id", "type", "size", "truncated", "separator", "trailing", "exit"])
def test_git_export_rejects_invalid_batch_frames(tmp_path, monkeypatch, damage):
    import subprocess
    import source_checkout
    commit, files = git_blob_tree(tmp_path)
    values = {subprocess.check_output(["git", "-C", tmp_path, "rev-parse", f"{commit}:{name}"]).strip(): data
              for name, (data, _) in files.items()}
    responder = tmp_path / "responder.py"
    responder.write_text("values = " + repr(values) + "\n" + '''import sys
damage = sys.argv[1]
while request := sys.stdin.buffer.readline():
    oid = request.strip()
    if damage == "missing":
        sys.stdout.buffer.write(oid + b" missing\\n")
    else:
        identity = b"0" * len(oid) if damage == "id" else oid
        kind = b"tree" if damage == "type" else b"blob"
        payload = values[oid]
        size = str(len(payload) + (1 if damage == "size" else 0)).encode()
        sys.stdout.buffer.write(identity + b" " + kind + b" " + size + b"\\n")
        if damage == "truncated":
            sys.stdout.buffer.flush()
            break
        sys.stdout.buffer.write(payload + (b"x" if damage == "separator" else b"\\n"))
    sys.stdout.buffer.flush()
if damage == "trailing":
    sys.stdout.buffer.write(b"unexpected")
sys.exit(7 if damage == "exit" else 0)
''')
    original = subprocess.Popen
    def started(args, *a, **kw):
        if "cat-file" in args:
            args = [sys.executable, responder, damage]
        return original(args, *a, **kw)
    monkeypatch.setattr(subprocess, "Popen", started)
    with pytest.raises(ValueError, match="Git blob"):
        list(source_checkout.tracked_blobs(tmp_path, commit))


def test_wheel_records_retain_original_direct_requirements():
    from wheelhouse import records
    selected = [record for record in records(ROOT / "requirements-build.lock") if record[0].startswith("chardet-")]
    assert selected == [("chardet-7.7.0-py3-none-any.whl",
        "chardet @ https://files.pythonhosted.org/packages/cc/75/88a3eb36264375ff951bd2f9397c409727ab6aa6a18e1bd933851571d421/chardet-7.7.0-py3-none-any.whl",
        "11cf9b719ea3675a2a7d8a3071bbe4d7ea24b26bfe39a51588d515ddd05cacad")]


def test_documented_install_selects_edited_wheel_and_original_direct_pure_wheel(tmp_path):
    import os
    import platform
    import subprocess
    import zipfile
    from rebuild_sources import local_wheel, selected_lock
    from wheelhouse import records

    candidates = tmp_path / "original candidates"
    candidates.mkdir()
    def original_wheel(owner, version, tag, value):
        path = candidates / f"{owner}-{version}-{tag}.whl"
        with zipfile.ZipFile(path, "w") as archive:
            prefix = f"{owner}-{version}.dist-info/"
            archive.writestr(owner + ".py", f"VALUE = {value!r}\n")
            archive.writestr(prefix + "METADATA", f"Metadata-Version: 2.1\nName: {owner}\nVersion: {version}\n")
            archive.writestr(prefix + "WHEEL", f"Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: {tag}\n")
            archive.writestr(prefix + "RECORD", "".join(name + ",,\n" for name in archive.namelist()) + prefix + "RECORD,,\n")
        return path
    h11 = original_wheel("h11", "0.16.0", "py3-none-any", "original")
    chardet = original_wheel("chardet", "7.7.0", "py3-none-any", "pure selection")
    original_wheel("chardet", "7.7.0", "py3-none-macosx_11_0_" + platform.machine(), "native decoy")
    root = tmp_path / "unpacked"
    project = root / "project"
    project.mkdir(parents=True)
    original_lock = project / "requirements-build.lock"
    pure_requirement = "chardet @ " + chardet.as_uri()
    original_lock.write_text(f"# {h11.name}\nh11==0.16.0 --hash=sha256:{hashlib.sha256(h11.read_bytes()).hexdigest()}\n"
                             f"# {chardet.name}\n{pure_requirement} --hash=sha256:{hashlib.sha256(chardet.read_bytes()).hexdigest()}\n")
    source = tmp_path / "editable source"
    source.mkdir()
    (source / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools", "wheel"]\n'
        'build-backend = "setuptools.build_meta"\n[project]\nname = "h11"\nversion = "0.16.0"\n'
        '[tool.setuptools]\npy-modules = ["h11"]\n')
    (source / "h11.py").write_text('VALUE = "edited selection"\n')
    wheels = tmp_path / "local wheels"
    lock = tmp_path / "local-requirements.lock"
    local_wheel(root, source, "arm64", wheels, lock)
    parsed = list(records(lock))
    assert parsed[1][1] == pure_requirement
    assert parsed[0][1] == "h11 @ " + (wheels / h11.name).as_uri()
    assert parsed[0][2] == hashlib.sha256((wheels / h11.name).read_bytes()).hexdigest()
    assert selected_lock(records(lock), wheels, {"h11"}) == lock.read_text()
    environment = tmp_path / "fresh environment"
    subprocess.run([sys.executable, "-m", "venv", environment], check=True, capture_output=True)
    python = environment / "bin/python"
    subprocess.run([python, "-m", "pip", "install", "--require-hashes", "--only-binary=:all:", "--no-index",
                    "--find-links", candidates, "--find-links", wheels, "-r", lock],
                   check=True, capture_output=True, env={**os.environ, "PIP_CONFIG_FILE": "/dev/null"})
    result = subprocess.check_output([python, "-c", "import h11,chardet; print(h11.VALUE); print(chardet.VALUE)"], text=True)
    assert result.splitlines() == ["edited selection", "pure selection"]
    locations = subprocess.check_output([python, "-c", 'import importlib.metadata as m,json; print(json.dumps({n:json.loads(m.distribution(n).read_text("direct_url.json")) for n in ("h11","chardet")}))'], text=True)
    installed = json.loads(locations)
    for owner, wheel in (("h11", wheels / h11.name), ("chardet", chardet)):
        assert installed[owner]["url"] == wheel.as_uri()
        assert installed[owner]["archive_info"]["hashes"]["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    portable = tmp_path / "portable.lock"
    subprocess.run([sys.executable, ROOT / "macapp/wheelhouse.py", lock, "--install-lock", portable], check=True)
    assert "file:" not in portable.read_text() and str(tmp_path) not in portable.read_text()
