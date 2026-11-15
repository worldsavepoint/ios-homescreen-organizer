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


def test_git_batch_broken_input_pipe_preserves_error_and_reaps_child(tmp_path, monkeypatch):
    import subprocess
    import source_checkout
    commit, _ = git_blob_tree(tmp_path)
    original = subprocess.Popen
    children = []
    def started(args, *a, **kw):
        if "cat-file" in args:
            args = [sys.executable, "-c", 'import os,sys,time; os.close(0); print("closed",flush=True); time.sleep(30)']
            process = original(args, *a, **kw)
            assert process.stdout.readline() == b"closed\n"
            children.append(process)
            return process
        return original(args, *a, **kw)
    monkeypatch.setattr(subprocess, "Popen", started)
    try:
        with pytest.raises(ValueError, match="Git blob"):
            list(source_checkout.tracked_blobs(tmp_path, commit))
        child, = children
        assert child.stdin.closed and child.stdout.closed and child.returncode is not None
    finally:
        for child in children:
            for stream in (child.stdin, child.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            if child.poll() is None:
                child.terminate()
            child.wait()


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


class _PrivateLauncherBytes(bytes):
    def __repr__(self):
        return "<private unsigned native launcher>"

    __str__ = __repr__


def _unsigned_launcher_bytes(executable, copy):
    import shutil
    import subprocess
    shutil.copyfile(executable, copy)
    subprocess.run(["codesign", "--remove-signature", copy], check=True, capture_output=True)
    return _PrivateLauncherBytes(copy.read_bytes())


def _launcher_signing_note(before, after):
    import struct
    from frozen_sources import canonical_launcher_payload
    unavailable = "Native launcher signing diagnostics unavailable"

    def summary(data):
        canonical = canonical_launcher_payload(data)
        header = struct.unpack_from("<IiiIIIII", data)
        offset = 32
        for _ in range(header[4]):
            command, size = struct.unpack_from("<II", data, offset)
            if command == 0x19 and data[offset + 8:offset + 24].rstrip(b"\0") == b"__LINKEDIT":
                linkedit = [offset, *struct.unpack_from("<QQQQ", data, offset + 24)]
            offset += size
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "canonicalSha256": hashlib.sha256(canonical).hexdigest(), "header": list(header),
                "linkedit": linkedit, "commandsSha256": hashlib.sha256(data[32:offset]).hexdigest()}

    try:
        details = {"before": summary(before), "after": summary(after)}
    except (ValueError, struct.error):
        return unavailable
    ranges, count, previous = [], 0, -2
    for offset in range(max(len(before), len(after))):
        if before[offset:offset + 1] == after[offset:offset + 1]:
            continue
        if previous != offset - 1:
            count += 1
            if count <= 8:
                ranges.append([offset, offset + 1])
        elif count <= 8:
            ranges[-1][1] = offset + 1
        previous = offset
    details.update(differenceRanges=ranges, differenceRangeCount=count)
    note = "Native launcher signing diagnostics: " + json.dumps(details, separators=(",", ":"))
    return note if len(note) <= 2048 else unavailable


def _verify_launcher_with_signing_diagnostics(app, before, copy):
    from frozen_sources import verify_local_record
    try:
        verify_local_record(app)
    except ValueError as error:
        try:
            note = _launcher_signing_note(before, _unsigned_launcher_bytes(app / "Contents/MacOS/HomeScreenOrganizer", copy))
        except Exception:
            note = "Native launcher signing diagnostics unavailable"
        error.add_note(note)
        raise


@pytest.mark.parametrize("changes", [1, 24])
def test_launcher_signing_diagnostics_expose_only_bounded_structure_and_digests(changes):
    import struct
    page = 4096
    before = bytearray(page + 512)
    struct.pack_into("<IiiIIIII", before, 0, 0xFEEDFACF, 0x01000007, 3, 2, 1, 72, 0, 0)
    struct.pack_into("<II16sQQQQiiII", before, 32, 0x19, 72, b"__LINKEDIT", page * 4, page, page, 512, 1, 1, 0, 0)
    marker = b"/private/untrusted-builder/\x1b[31msecret.swift"
    before[200:200 + len(marker)] = marker
    after = bytearray(before)
    struct.pack_into("<Q", after, 64, page * 2)
    for index in range(changes):
        after[page + 2 * index] ^= 1
    note = _launcher_signing_note(before, after)
    assert len(note) <= 2048 and all(character.isprintable() for character in note)
    assert not any(value in note for value in ("/private/", "untrusted-builder", "secret.swift"))
    details = json.loads(note.removeprefix("Native launcher signing diagnostics: "))
    assert details["before"]["linkedit"] == [32, page * 4, page, page, 512]
    assert details["after"]["linkedit"] == [32, page * 4, page * 2, page, 512]
    assert details["before"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert details["before"]["canonicalSha256"] != details["after"]["canonicalSha256"]
    assert details["differenceRangeCount"] == changes + 1
    assert len(details["differenceRanges"]) == min(8, changes + 1)
    assert details["differenceRanges"][0] == [65, 66]


def test_launcher_signing_diagnostics_reject_malformed_bytes_without_echoing_them():
    assert _launcher_signing_note(b"/private/secret-header\x1b[31m", b"bad") == "Native launcher signing diagnostics unavailable"


@pytest.mark.parametrize("diagnostic_failure", [False, True])
def test_launcher_signing_failure_traceback_keeps_payload_bytes_private(tmp_path, diagnostic_failure):
    import struct
    import subprocess
    marker = b"/private/byte-marker-for-pytest/payload-secret.swift"
    payload = bytearray(4096 + 512)
    struct.pack_into("<IiiIIIII", payload, 0, 0xFEEDFACF, 0x01000007, 3, 2, 1, 72, 0, 0)
    struct.pack_into("<II16sQQQQiiII", payload, 32, 0x19, 72, b"__LINKEDIT", 16384, 4096, 4096, 512, 1, 1, 0, 0)
    payload[-len(marker):] = marker
    app = tmp_path / "fixture.app"
    launcher = app / "Contents/MacOS/HomeScreenOrganizer"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(payload)
    resources = app / "Contents/Resources"
    resources.mkdir()
    (resources / "LOCAL-BUILD.json").write_text(json.dumps({"launcher": {"algorithm": "unchanged", "sha256": "0" * 64}}))
    probe = tmp_path / "test_failure_probe.py"
    probe.write_text(f'''import importlib.util
from pathlib import Path
import subprocess
spec = importlib.util.spec_from_file_location("launcher_diagnostic_probe", {str(Path(__file__).resolve())!r})
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
import frozen_sources

def test_binding_failure(tmp_path, monkeypatch):
    app = Path("fixture.app")
    monkeypatch.setattr(frozen_sources, "launcher_digest", lambda executable: "a" * 64)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)
    before = helpers._unsigned_launcher_bytes(app / "Contents/MacOS/HomeScreenOrganizer", tmp_path / "before")
    if {diagnostic_failure!r}:
        def invalid_note(before, after):
            raise ValueError(before[-100:].decode("ascii", "replace"))
        monkeypatch.setattr(helpers, "_launcher_signing_note", invalid_note)
    helpers._verify_launcher_with_signing_diagnostics(app, before, tmp_path / "after")
''')
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--showlocals", "--tb=long", probe.name],
                            cwd=tmp_path, capture_output=True, text=True)
    rendered = result.stdout + result.stderr
    assert result.returncode == 1 and "1 failed" in rendered
    assert "The signed native launcher differs from the local build record" in rendered
    assert "Native launcher signing diagnostics" in rendered
    assert "payload-secret.swift" not in rendered and "byte-marker-for-pytest" not in rendered


def test_native_workflow_returns_to_success_gated_package_builds():
    workflow = (ROOT / ".github/workflows/tests.yml").read_text()
    native = workflow.split("\n  test:\n", 1)[1].split("\n  assemble:\n", 1)[0]
    assert "Diagnose Intel package after failed tests" not in native
    assert "failure()" not in native and "continue-on-error" not in native
    assert native.index(".venv/bin/python -m pytest -q tests") < native.index("      - name: Build and verify the Mac app")
    for title in ("Build and verify the Mac app", "Upload matching native source inputs", "Upload installable archives"):
        step = native.split("      - name: " + title + "\n", 1)[1].split("\n      - ", 1)[0]
        assert "        if:" not in step
    assert "    needs: test\n" in workflow.split("\n  assemble:\n", 1)[1]


def test_build_recipe_changes_are_recorded_without_git(tmp_path, monkeypatch):
    import importlib.util
    import frozen_sources
    project = tmp_path / "project"
    swift = project / "macapp/Shell/Sources/Homescreen"
    swift.mkdir(parents=True)
    (project / "macapp/Shell/Package.swift").write_text("// package\n")
    (swift / "App.swift").write_text("// launcher\n")
    recipe = project / "macapp/make_icon.py"
    recipe.write_bytes((ROOT / "macapp/make_icon.py").read_bytes())
    build = project / "macapp/build.sh"
    build.write_text("# original recipe\n")
    baseline = {"project/" + p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in project.rglob("*") if p.is_file()}
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps({"files": baseline}))
    record = tmp_path / "LOCAL-BUILD.json"
    monkeypatch.setattr(frozen_sources, "launcher_digest", lambda executable: "f" * 64)
    def capture():
        record.write_text(json.dumps({"projectFiles": {}, "engine": "e" * 64}))
        frozen_sources.finalize_local_record(project, manifest, record, tmp_path / "launcher")
        return json.loads(record.read_bytes())
    def icon_bytes():
        spec = importlib.util.spec_from_file_location("local_icon_recipe", recipe)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.draw(32).tobytes()
    assert capture()["projectChanges"] == {"modified": {}, "added": {}, "removed": []}
    original_icon = icon_bytes()
    recipe.write_text(recipe.read_text().replace("(255, 159, 10)", "(0, 0, 0)"))
    build.write_text("# modified recipe\n")
    assert icon_bytes() != original_icon
    changed = capture()
    assert changed["projectChanges"]["modified"] == {
        "macapp/build.sh": hashlib.sha256(build.read_bytes()).hexdigest(),
        "macapp/make_icon.py": hashlib.sha256(recipe.read_bytes()).hexdigest()}
    assert changed["engine"] == "e" * 64


def local_input_record(project, tmp_path, monkeypatch, baseline, selected=None):
    import frozen_sources
    files = {"project/" + name: value for name, value in baseline.items()}
    manifest = {"files": files}
    if selected is not None:
        inventory = tmp_path / "inventory.json"
        inventory.write_text(json.dumps({"projectInputs": {name: baseline[name] for name in selected}}))
        files["inventory.json"] = hashlib.sha256(inventory.read_bytes()).hexdigest()
        manifest["architectures"] = {"arm64": {"inventory": "inventory.json"}}
    manifest_path = tmp_path / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest))
    record = tmp_path / "LOCAL-BUILD.json"
    record.write_text(json.dumps({"arch": "arm64", "projectFiles": {}, "engine": "e" * 64}))
    monkeypatch.setattr(frozen_sources, "launcher_digest", lambda _: "f" * 64)
    frozen_sources.finalize_local_record(project, manifest_path, record, tmp_path / "launcher")
    return json.loads(record.read_bytes())


@pytest.mark.parametrize("name", ["requirements-build.lock", "requirements-build-x86_64.lock",
                                    "requirements-cryptography-x86_64.lock", "setup.py", "setup.cfg", "MANIFEST.in", "README.md"])
@pytest.mark.parametrize("change", ["modified", "removed"])
def test_local_record_tracks_root_recipe_and_metadata_changes(tmp_path, monkeypatch, name, change):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nreadme="README.md"\nlicense-files=["LICENSE"]\n')
    (project / name).write_text("# original\n")
    baseline = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in project.iterdir()}
    if change == "removed":
        (project / name).unlink()
    else:
        (project / name).write_text("# changed\n")
    record = local_input_record(project, tmp_path, monkeypatch, baseline)
    changes = record["projectChanges"]
    if change == "removed":
        assert name in changes["removed"] and name not in record["projectFiles"]
    else:
        assert changes["modified"][name] == hashlib.sha256((project / name).read_bytes()).hexdigest()


def test_local_record_follows_actual_backend_readme_and_license_inputs(tmp_path, monkeypatch):
    import contextlib
    import io
    from setuptools import build_meta
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
    (project / "LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())
    (project / "README.md").write_text("Original readme\n")
    (project / "homescreen").mkdir()
    (project / "homescreen/__init__.py").write_text("")
    baseline = {p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in project.rglob("*") if p.is_file()}
    monkeypatch.chdir(project)
    def metadata(name):
        folder = tmp_path / name
        folder.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            dist = build_meta.prepare_metadata_for_build_wheel(str(folder))
        return {p.relative_to(folder / dist).as_posix(): p.read_bytes() for p in (folder / dist).rglob("*") if p.is_file()}
    original = metadata("before")
    (project / "NOTICE").write_text("Unused explicit notice\n")
    assert metadata("unused") == original
    normal = local_input_record(project, tmp_path, monkeypatch, baseline)
    assert "NOTICE" not in normal["projectFiles"]
    assert normal["projectChanges"] == {"modified": {}, "added": {}, "removed": []}
    (project / "README.md").write_text("Modified readme\n")
    assert metadata("readme")["METADATA"] != original["METADATA"]
    changed = local_input_record(project, tmp_path, monkeypatch, baseline)
    assert "README.md" in changed["projectChanges"]["modified"]
    config = (project / "pyproject.toml").read_text().replace('readme = "README.md"', 'readme = "DESCRIPTION.md"')
    (project / "pyproject.toml").write_text(config.replace('license-files = ["LICENSE"]\n', ''))
    additions = {"DESCRIPTION.md": "Alternate description\n", "COPYING": "Copy grant\n", "AUTHORS": "Credit\n",
                 "LICENSE-recipient": "Additional grant\n", "setup.py": "from setuptools import setup\nsetup()\n",
                 "setup.cfg": "[metadata]\n", "MANIFEST.in": "include homescreen/resource.txt\n"}
    for name, text in additions.items():
        (project / name).write_text(text)
    (project / "homescreen/resource.txt").write_text("Editable resource\n")
    actual = metadata("defaults")
    assert b"Alternate description" in actual["METADATA"]
    for name in ("LICENSE", "LICENSE-recipient", "COPYING", "AUTHORS", "NOTICE"):
        assert actual["licenses/" + name] == (project / name).read_bytes()
    changed = local_input_record(project, tmp_path, monkeypatch, baseline)
    assert set(additions) | {"NOTICE"} <= changed["projectFiles"].keys()
    assert "homescreen/resource.txt" in changed["projectChanges"]["added"]


@pytest.mark.parametrize("captured", [False, True])
def test_replaced_readme_deletion_uses_captured_baseline_selection(tmp_path, monkeypatch, captured):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nreadme="README.md"\nlicense-files=[]\n')
    (project / "README.md").write_text("Original\n")
    baseline = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in project.iterdir()}
    (project / "README.md").unlink()
    (project / "DESCRIPTION.md").write_text("Replacement\n")
    (project / "pyproject.toml").write_text('[project]\nreadme={file="DESCRIPTION.md",content-type="text/markdown"}\nlicense-files=[]\n')
    record = local_input_record(project, tmp_path, monkeypatch, baseline, ["README.md", "pyproject.toml"] if captured else None)
    assert set(record["projectChanges"]["modified"]) == {"pyproject.toml"}
    assert set(record["projectChanges"]["added"]) == {"DESCRIPTION.md"}
    assert record["projectChanges"]["removed"] == (["README.md"] if captured else [])


@pytest.mark.parametrize("pattern", ["grants/*.txt", "grants/**/*.txt"])
def test_declared_dynamic_metadata_files_and_removed_license_globs(tmp_path, monkeypatch, pattern):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nlicense-files=["' + pattern + '"]\n'
        '[tool.setuptools.dynamic]\nreadme={file=["INTRO.md", "DETAILS.md"]}\n')
    (project / "grants").mkdir()
    for name in ("INTRO.md", "DETAILS.md", "grants/COPY.txt"):
        (project / name).write_text("Original\n")
    baseline = {p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in project.rglob("*") if p.is_file()}
    (project / "grants/COPY.txt").unlink()
    (project / "DETAILS.md").write_text("Modified details\n")
    record = local_input_record(project, tmp_path, monkeypatch, baseline)
    assert record["projectChanges"]["removed"] == ["grants/COPY.txt"]
    assert set(record["projectChanges"]["modified"]) == {"DETAILS.md"}


def test_recursive_input_selection_matches_real_backend_and_legacy_removal(tmp_path, monkeypatch):
    import contextlib
    import io
    from setuptools import build_meta
    from frozen_sources import local_root_inputs, project_root_inputs
    project = tmp_path / "project"
    project.mkdir()
    config = b'[project]\nname="example"\nversion="1.0"\nlicense="MIT"\nlicense-files=["grants/**/*.txt"]\n[tool.setuptools]\npy-modules=["example"]\n'
    files = {"pyproject.toml": config, "example.py": b"value=1\n", "grants/direct.txt": b"direct grant\n",
             "grants/deep/nested.txt": b"nested grant\n", "grants/.hidden.txt": b"hidden grant\n",
             "grants/.private/hidden.txt": b"hidden subtree\n", "grants/unused.txt~": b"editor backup\n"}
    for name, data in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    output = tmp_path / "metadata"
    output.mkdir()
    monkeypatch.chdir(project)
    with contextlib.redirect_stdout(io.StringIO()):
        dist = build_meta.prepare_metadata_for_build_wheel(str(output))
    licenses = output / dist / "licenses"
    actual = {path.relative_to(licenses).as_posix(): path.read_bytes() for path in licenses.rglob("*") if path.is_file()}
    assert actual == {name: files[name] for name in ("grants/direct.txt", "grants/deep/nested.txt")}
    expected = {"pyproject.toml", "grants/direct.txt", "grants/deep/nested.txt"}
    assert local_root_inputs(project) == expected
    assert project_root_inputs(files, files.__getitem__) == expected
    baseline = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    (project / "grants/direct.txt").unlink()
    record = local_input_record(project, tmp_path, monkeypatch, baseline)
    assert record["projectChanges"]["removed"] == ["grants/direct.txt"]


@pytest.mark.parametrize("pattern,expected", [
    ("**/*.txt", {"root.txt", "grants/direct.txt", "grants/deep/nested.txt"}),
    ("grants/**/**/*.txt", {"grants/direct.txt", "grants/deep/nested.txt"}),
    ("grants/**/n?sted.[t]xt", {"grants/deep/nested.txt"}),
    ("grants/**/.hidden.txt", {"grants/.hidden.txt"}),
    ("grants/.private/**/*.txt", {"grants/.private/secret.txt"}),
    ("grants/[.]private/*.txt", set()),
])
def test_trusted_recursive_patterns_keep_component_and_hidden_rules(pattern, expected):
    from frozen_sources import project_root_inputs
    names = {"root.txt", "grants/direct.txt", "grants/deep/nested.txt", "grants/.hidden.txt", "grants/.private/secret.txt"}
    files = {name: b"grant\n" for name in names}
    files["pyproject.toml"] = ('[project]\nlicense-files=["' + pattern + '"]\n').encode()
    assert project_root_inputs(files, files.__getitem__) == expected | {"pyproject.toml"}


@pytest.mark.parametrize("recursive", [False, True])
def test_source_verifier_accepts_real_recursive_capture(tmp_path, monkeypatch, recursive):
    import io
    import subprocess
    import tarfile
    import zipfile
    import source_fixture
    from frozen_sources import local_root_inputs
    from source_archive import payload_tar, verify_tar
    from source_inputs import json_bytes, sha256
    original = source_fixture.fixture_checkout
    def checkout(root, version):
        _, inputs, assets, runtime = original(root, version)
        pattern = "grants/**/*.txt" if recursive else "grants/*.txt"
        (root / "pyproject.toml").write_text('[project]\nversion="' + version + '"\nlicense-files=["' + pattern + '"]\n')
        (root / "grants/deep").mkdir(parents=True)
        (root / "grants/direct.txt").write_bytes(b"direct grant\n")
        (root / "grants/deep/nested.txt").write_bytes(b"nested grant\n")
        subprocess.run(["git", "-C", root, "add", "--", "pyproject.toml", "grants"], check=True)
        subprocess.run(["git", "-C", root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                        "-c", "commit.gpgsign=false", "commit", "-qm", "Configure fixture grants"], check=True)
        commit = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
        return commit, inputs, assets, runtime
    monkeypatch.setattr(source_fixture, "fixture_checkout", checkout)
    release, root = tmp_path / "release", tmp_path / "trusted"
    release.mkdir()
    commit = source_fixture.release_source(release, "1.3.0", root)
    archive = next(release.glob("*-source.tar.gz"))
    with tarfile.open(archive) as source:
        entries = {member.name: source.extractfile(member).read() for member in source}
    manifest = json.loads(entries.pop("MANIFEST.json"))
    captured = {name: sha256(root / name) for name in local_root_inputs(root)}
    for arch in ("arm64", "x86_64"):
        name = "inventories/" + arch + ".json"
        volume = json.loads(entries[name])
        volume["projectInputs"] = captured
        entries[name] = json_bytes(volume)
        native = release / f"home-screen-organizer-1.3.0-macos-{arch}.zip"
        with zipfile.ZipFile(native) as source:
            records = [(info, source.read(info)) for info in source.infolist()]
        with zipfile.ZipFile(native, "w") as output:
            for info, data in records:
                output.writestr(info, entries[name] if info.filename.endswith("/SourceInventory.json") else data)
        manifest["architectures"][arch]["zip"] = sha256(native)
    payload_tar(archive, ((name, 0o644, io.BytesIO(data), len(data)) for name, data in sorted(entries.items())),
                {key: value for key, value in manifest.items() if key != "files"})
    verify_tar(archive, "1.3.0", commit, release=True, root=root, repository="example/project")


def test_default_license_inputs_ignore_directories_and_editor_backups(tmp_path):
    from frozen_sources import local_root_inputs
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "LICENSE").write_text("Grant\n")
    (tmp_path / "NOTICE~").write_text("Unused editor backup\n")
    (tmp_path / "LICENSES").mkdir()
    assert local_root_inputs(tmp_path) == {"pyproject.toml", "LICENSE"}


def test_setup_legacy_license_file_is_recorded_with_explicit_project_grants(tmp_path, monkeypatch):
    import contextlib
    import io
    from setuptools import build_meta
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname="example"\nversion="1.0"\nlicense="MIT"\n'
        'license-files=["LICENSE"]\n[tool.setuptools]\npy-modules=["example"]\n')
    (project / "example.py").write_text("value=1\n")
    (project / "LICENSE").write_text("Original grant\n")
    (project / "CREDITS.txt").write_text("Additional credit\n")
    (project / "setup.cfg").write_text("[metadata]\nlicense_file=CREDITS.txt\n")
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    monkeypatch.chdir(project)
    with contextlib.redirect_stdout(io.StringIO()):
        name = build_meta.prepare_metadata_for_build_wheel(str(metadata))
    assert (metadata / name / "licenses/CREDITS.txt").read_bytes() == b"Additional credit\n"
    record = local_input_record(project, tmp_path, monkeypatch, {})
    assert record["projectChanges"]["added"]["CREDITS.txt"] == hashlib.sha256(b"Additional credit\n").hexdigest()


def test_captured_root_map_is_validated_against_trusted_project(tmp_path):
    import io
    import tarfile
    import zipfile
    from source_fixture import release_source
    from source_archive import payload_tar, verify_tar
    from source_inputs import json_bytes, sha256
    release, checkout = tmp_path / "release", tmp_path / "checkout"
    release.mkdir()
    commit = release_source(release, "1.3.0", checkout)
    archive = next(release.glob("*-source.tar.gz"))
    with tarfile.open(archive) as source:
        entries = {member.name: source.extractfile(member).read() for member in source}
    manifest = json.loads(entries.pop("MANIFEST.json"))
    expected = {name: sha256(checkout / name) for name in ("LICENSE", "pyproject.toml")}
    def write(selected):
        for arch in ("arm64", "x86_64"):
            name = "inventories/" + arch + ".json"
            volume = json.loads(entries[name])
            volume["projectInputs"] = selected
            entries[name] = json_bytes(volume)
            native = release / f"home-screen-organizer-1.3.0-macos-{arch}.zip"
            with zipfile.ZipFile(native) as source:
                records = [(info, source.read(info)) for info in source.infolist()]
            with zipfile.ZipFile(native, "w") as output:
                for info, data in records:
                    output.writestr(info, entries[name] if info.filename.endswith("/SourceInventory.json") else data)
            manifest["architectures"][arch]["zip"] = sha256(native)
        payload_tar(archive, ((name, 0o644, io.BytesIO(data), len(data)) for name, data in sorted(entries.items())),
                    {key: value for key, value in manifest.items() if key != "files"})
    write(expected)
    verify_tar(archive, "1.3.0", commit, release=True, root=checkout, repository="example/project")
    write({**expected, "LICENSE": "f" * 64})
    with pytest.raises(ValueError, match="project input inventory differs"):
        verify_tar(archive, "1.3.0", commit, release=True, root=checkout, repository="example/project")
    for malformed in ([], {"LICENSE": None}):
        write(malformed)
        with pytest.raises(ValueError, match="project input inventory"):
            verify_tar(archive, "1.3.0", commit, release=True, root=checkout, repository="example/project")


@pytest.mark.parametrize("directory", ["Features", "build", "dist", "nested/build", "nested/dist", "__pycache__"])
def test_added_swift_sources_keep_ordinary_directory_names(tmp_path, monkeypatch, directory):
    import frozen_sources
    project = tmp_path / "project"
    sources = project / "macapp/Shell/Sources/Homescreen"
    sources.mkdir(parents=True)
    main = sources / "main.swift"
    main.write_text('print(Feature.value)\n')
    package = project / "macapp/Shell/Package.swift"
    package.write_text('// swift-tools-version:5.9\nimport PackageDescription\n')
    baseline = {"project/" + path.relative_to(project).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (main, package)}
    extra = sources / directory / "Feature.swift"
    extra.parent.mkdir(parents=True)
    extra.write_text('enum Feature { static let value = "recipient edit" }\n')
    for name in ("macapp/Shell/.build/debug/Generated.swift", "macapp/Shell/.swiftpm/workspace-state.json",
                 "macapp/__pycache__/frozen_sources.cpython-312.pyc"):
        generated = project / name
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(b"generated build data")
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps({"files": baseline}))
    record = tmp_path / "LOCAL-BUILD.json"
    record.write_text(json.dumps({"projectFiles": {}, "engine": "e" * 64}))
    monkeypatch.setattr(frozen_sources, "launcher_digest", lambda path: "f" * 64)
    frozen_sources.finalize_local_record(project, manifest, record, tmp_path / "launcher")
    result = json.loads(record.read_bytes())
    name = extra.relative_to(project).as_posix()
    assert result["projectChanges"] == {"modified": {}, "added": {name: hashlib.sha256(extra.read_bytes()).hexdigest()}, "removed": []}
    assert set(result["projectFiles"]) == {"macapp/Shell/Package.swift", "macapp/Shell/Sources/Homescreen/main.swift", name}


@pytest.mark.parametrize("directory", ["build", "dist"])
def test_added_swift_source_links_are_rejected_in_ordinary_directories(tmp_path, monkeypatch, directory):
    import frozen_sources
    project = tmp_path / "project"
    source = project / "macapp/Shell/Sources/Homescreen" / directory / "Feature.swift"
    source.parent.mkdir(parents=True)
    target = tmp_path / "external.swift"
    target.write_text('print("external input")\n')
    source.symlink_to(target)
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps({"files": {}}))
    record = tmp_path / "LOCAL-BUILD.json"
    record.write_text(json.dumps({"projectFiles": {}}))
    monkeypatch.setattr(frozen_sources, "launcher_digest", lambda path: "f" * 64)
    with pytest.raises(ValueError, match="regular local project inputs"):
        frozen_sources.finalize_local_record(project, manifest, record, tmp_path / "launcher")


@pytest.mark.skipif(sys.platform != "darwin", reason="Uses the native Swift compiler and Mach-O signer")
def test_swift_only_changes_and_canonical_launcher_are_sealed_into_local_record(tmp_path):
    import plistlib
    import subprocess
    from frozen_sources import finalize_local_record, launcher_digest

    root = tmp_path / "editable project"
    target = root / "macapp/Shell/Sources/Homescreen"
    target.mkdir(parents=True)
    package = root / "macapp/Shell/Package.swift"
    package.write_text('// swift-tools-version:5.9\nimport PackageDescription\n')
    main = target / "main.swift"
    main.write_text('print("original launcher")\n')
    original = tmp_path / "original-launcher"
    subprocess.run(["swiftc", "-O", "-module-cache-path", tmp_path / "module-cache", main, "-o", original],
                   check=True, capture_output=True)
    original_digest = launcher_digest(original)
    baseline = {"project/macapp/Shell/Package.swift": hashlib.sha256(package.read_bytes()).hexdigest(),
                "project/macapp/Shell/Sources/Homescreen/main.swift": hashlib.sha256(main.read_bytes()).hexdigest(),
                "project/macapp/Shell/Sources/Homescreen/Removed.swift": "a" * 64,
                "project/homescreen/module.py": "b" * 64}
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps({"files": baseline}))
    main.write_text('print(Added.value)\n')
    added = target / "Added.swift"
    added.write_text('enum Added { static let value = "modified native launcher" }\n')
    app = tmp_path / "Screen Organizer.app"
    launcher = app / "Contents/MacOS/HomeScreenOrganizer"
    launcher.parent.mkdir(parents=True)
    subprocess.run(["swiftc", "-O", "-module-cache-path", tmp_path / "module-cache", main, added, "-o", launcher],
                   check=True, capture_output=True)
    subprocess.run(["/usr/bin/strip", "-S", launcher], check=True, capture_output=True)
    resources = app / "Contents/Resources"
    resources.mkdir()
    record = resources / "LOCAL-BUILD.json"
    record.write_text(json.dumps({"upstreamCommit": "c" * 40, "arch": "arm64", "components": {},
                                  "projectFiles": {"homescreen/module.py": "b" * 64},
                                  "engine": "e" * 64, "editableOverlayHashes": {"cpython/config.py": "d" * 64}}))
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.example.local-native",
        "CFBundleExecutable": launcher.name, "CFBundlePackageType": "APPL"}))
    finalize_local_record(root, manifest, record, launcher)
    data = json.loads(record.read_bytes())
    assert data["projectFiles"]["macapp/Shell/Sources/Homescreen/main.swift"] == hashlib.sha256(main.read_bytes()).hexdigest()
    assert data["projectChanges"] == {
        "modified": {"macapp/Shell/Sources/Homescreen/main.swift": hashlib.sha256(main.read_bytes()).hexdigest()},
        "added": {"macapp/Shell/Sources/Homescreen/Added.swift": hashlib.sha256(added.read_bytes()).hexdigest()},
        "removed": ["macapp/Shell/Sources/Homescreen/Removed.swift"]}
    assert data["launcher"]["sha256"] != original_digest
    assert data["engine"] == "e" * 64 and data["editableOverlayHashes"] == {"cpython/config.py": "d" * 64}
    assert str(tmp_path).encode() not in record.read_bytes()
    before_signing = record.read_bytes()
    unsigned_before = _unsigned_launcher_bytes(launcher, tmp_path / "launcher-before-signing")
    subprocess.run(["codesign", "--force", "--sign", "-", app], check=True, capture_output=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", app], check=True, capture_output=True)
    assert record.read_bytes() == before_signing
    _verify_launcher_with_signing_diagnostics(app, unsigned_before, tmp_path / "launcher-after-signing")
    assert launcher_digest(launcher) == data["launcher"]["sha256"]
    subprocess.run(["swiftc", "-O", "-module-cache-path", tmp_path / "module-cache", target / "main.swift", added,
                    "-o", original], check=True, capture_output=True)
    # A distinct executable must fail even with unchanged Python/component evidence.
    main.write_text('print("another launcher")\n')
    subprocess.run(["swiftc", "-O", "-module-cache-path", tmp_path / "module-cache", main, "-o", launcher],
                   check=True, capture_output=True)
    with pytest.raises(ValueError, match="launcher") as tampered:
        _verify_launcher_with_signing_diagnostics(app, unsigned_before, tmp_path / "launcher-after-tampering")
    assert len(tampered.value.__notes__) == 1
    assert tampered.value.__notes__[0].startswith("Native launcher signing diagnostics: ")
    assert len(tampered.value.__notes__[0]) <= 2048
    assert str(tmp_path) not in tampered.value.__notes__[0]


@pytest.mark.skipif(sys.platform != "darwin", reason="Builds and loads a native Darwin C extension")
def test_native_wheel_rebuilds_stale_objects_and_remaps_private_compiler_paths(tmp_path, monkeypatch):
    import csv
    import base64
    import io
    import os
    import platform
    import subprocess
    import zipfile
    from rebuild_sources import local_wheel
    from build_paths import read_roots

    source = tmp_path / "private-marker-editable source"
    source.mkdir()
    (source / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n')
    (source / "setup.py").write_text('from setuptools import setup,Extension\nsetup(name="r2native",version="1.0",ext_modules=[Extension("r2native",["native.c"])])\n')
    (source / "native.c").write_text('''#include <Python.h>
#ifndef R2_VALUE
#define R2_VALUE 7
#endif
static PyObject *value(PyObject *self, PyObject *args) { return Py_BuildValue("is", R2_VALUE, __FILE__); }
static PyMethodDef methods[] = {{"value", value, METH_NOARGS, "fixture"}, {NULL, NULL, 0, NULL}};
static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "r2native", NULL, -1, methods};
PyMODINIT_FUNC PyInit_r2native(void) { return PyModule_Create(&module); }
''')
    stale = tmp_path / "stale wheels"
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", stale, source],
                   check=True, capture_output=True)
    old, = stale.glob("*.whl")
    assert any((source / "build").rglob("*.o"))
    root = tmp_path / "source release"
    (root / "project").mkdir(parents=True)
    arch = platform.machine()
    lock = root / ("project/requirements-build.lock" if arch == "arm64" else "project/build/dependencies/x86_64/requirements-build.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"# {old.name}\nr2native==1.0 --hash=sha256:{hashlib.sha256(old.read_bytes()).hexdigest()}\n")
    monkeypatch.setenv("CFLAGS", "-g -DR2_VALUE=41")
    monkeypatch.setenv("LDFLAGS", "-Wl,-headerpad_max_install_names")
    wheels = tmp_path / "replacement wheels"
    local_wheel(root, source, arch, wheels, tmp_path / "local.lock")
    wheel, = wheels.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        extension, = (name for name in archive.namelist() if name.endswith(".so"))
        assert b"private-marker-editable" not in archive.read(extension)
        assert bytes(tmp_path) not in archive.read(extension)
        record, = (name for name in archive.namelist() if name.endswith(".dist-info/RECORD"))
        for name, checksum, size in csv.reader(io.StringIO(archive.read(record).decode())):
            if checksum:
                assert checksum == "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(archive.read(name)).digest()).decode().rstrip("=")
                assert len(archive.read(name)) == int(size)
        extracted = tmp_path / "extension"
        archive.extractall(extracted)
    result = subprocess.check_output([sys.executable, "-c", "import r2native,json; print(json.dumps(r2native.value()))"],
                                    env={**os.environ, "PYTHONPATH": str(extracted)}, text=True)
    value, filename = json.loads(result)
    assert value == 41 and "private-marker-editable" not in filename
    assert source in read_roots(wheels, [wheel])


@pytest.mark.parametrize("cpu,page", [(0x0100000C, 16384), (0x01000007, 4096)])
@pytest.mark.parametrize("damage", [None, "cpu", "header", "commands", "segment", "bounds", "signature"])
def test_canonical_launcher_changes_only_bounded_linkedit_alignment(cpu, page, damage):
    import struct
    from frozen_sources import canonical_launcher_payload
    payload = bytearray(page + 7)
    struct.pack_into("<IiiIIIII", payload, 0, 0xFEEDFACF, cpu, 0, 2, 1, 72, 0, 0)
    struct.pack_into("<II16sQQQQiiII", payload, 32, 0x19, 72, b"__LINKEDIT", page * 4, page * 2, page, 7, 1, 1, 0, 0)
    payload[-7:] = b"payload"
    if damage:
        offset, value = {"cpu": (4, 12), "header": (0, 0), "commands": (20, 80),
                         "segment": (36, 64), "bounds": (80, page * 4), "signature": (32, 0x1D)}[damage]
        struct.pack_into("<I", payload, offset, value)
        with pytest.raises(ValueError, match="launcher"):
            canonical_launcher_payload(payload)
    else:
        expected = bytearray(payload + bytes(-len(payload) % 16))
        struct.pack_into("<Q", expected, 64, page)
        struct.pack_into("<Q", expected, 80, 16)
        assert canonical_launcher_payload(payload) == expected
        assert canonical_launcher_payload(expected) == expected
        expected[-1] ^= 1
        assert canonical_launcher_payload(expected) != canonical_launcher_payload(payload)


def test_canonical_launcher_appends_alignment_without_discarding_original_bytes():
    import struct
    from frozen_sources import canonical_launcher_payload
    page = 4096
    before = bytearray(page + 24)
    struct.pack_into("<IiiIIIII", before, 0, 0xFEEDFACF, 0x01000007, 3, 2, 2, 96, 0, 0)
    struct.pack_into("<II16sQQQQiiII", before, 32, 0x19, 72, b"__LINKEDIT", page * 4, page, page, 24, 1, 1, 0, 0)
    struct.pack_into("<6I", before, 104, 2, 24, page, 1, page + 16, 8)
    before[-24:] = b"meaningful bytes" + b"strings\0"
    after = bytearray(before + bytes(8))
    struct.pack_into("<Q", after, 64, page * 2)
    struct.pack_into("<Q", after, 80, 32)
    canonical = canonical_launcher_payload(before)
    assert len(canonical) == page + 32
    assert canonical[page:page + 24] == before[page:]
    assert canonical[-8:] == bytes(8)
    assert canonical == canonical_launcher_payload(after)
    after[-1] = 1
    assert canonical != canonical_launcher_payload(after)
    after = bytearray(before)
    after[page] ^= 1
    assert canonical != canonical_launcher_payload(after)


@pytest.mark.parametrize("reference", ["symbols", "strings", "dynamic-symbols", "dyld-info", "linkedit-data",
                                        "section", "relocations", "note", "entrypoint"])
def test_canonical_launcher_never_restores_truncated_referenced_zero_bytes(reference):
    import struct
    from frozen_sources import canonical_launcher_payload
    page, file_size, sections = 4096, 8, 0
    if reference == "symbols":
        file_size = 16
        extra = struct.pack("<6I", 2, 24, page, 1, 0, 0)
    elif reference == "strings":
        extra = struct.pack("<6I", 2, 24, 0, 0, page, file_size)
    elif reference == "dynamic-symbols":
        fields = [0] * 18
        fields[12:14] = [page, 2]
        extra = struct.pack("<20I", 0xB, 80, *fields)
    elif reference == "dyld-info":
        extra = struct.pack("<12I", 0x80000022, 48, page, file_size, *([0] * 8))
    elif reference == "linkedit-data":
        extra = struct.pack("<4I", 0x80000033, 16, page, file_size)
    elif reference == "note":
        extra = struct.pack("<II16sQQ", 0x31, 40, b"owner", page, file_size)
    elif reference == "entrypoint":
        extra = struct.pack("<IIQQ", 0x80000028, 24, page + file_size - 1, 0)
    else:
        sections = 1
        extra = struct.pack("<16s16sQQ8I", b"data", b"__LINKEDIT", page * 4,
                            file_size if reference == "section" else 0, page, 0,
                            page if reference == "relocations" else 0, 1 if reference == "relocations" else 0, 0, 0, 0, 0)
    segment_size = 72 + sections * 80
    payload = bytearray(page + file_size)
    struct.pack_into("<IiiIIIII", payload, 0, 0xFEEDFACF, 0x01000007, 3, 2,
                     1 if sections else 2, 72 + len(extra), 0, 0)
    struct.pack_into("<II16sQQQQiiII", payload, 32, 0x19, segment_size, b"__LINKEDIT",
                     page * 4, page, page, file_size, 1, 1, sections, 0)
    payload[104:104 + len(extra)] = extra
    canonical_launcher_payload(payload)
    truncated = payload[:-1]
    struct.pack_into("<Q", truncated, 80, file_size - 1)
    with pytest.raises(ValueError, match="launcher"):
        canonical_launcher_payload(truncated)


def test_native_compiler_keeps_supplied_rust_flags_and_remaps_tool_roots(tmp_path, monkeypatch):
    from rebuild_sources import compiler_environment
    cargo = tmp_path / "Cargo tools with spaces"
    rustup = tmp_path / "Rust tools with spaces"
    monkeypatch.setenv("CARGO_HOME", str(cargo))
    monkeypatch.setenv("RUSTUP_HOME", str(rustup))
    monkeypatch.setenv("RUSTFLAGS", "-C opt-level=2")
    monkeypatch.setenv("ARCHFLAGS", "-arch x86_64")
    monkeypatch.delenv("CARGO_ENCODED_RUSTFLAGS", raising=False)
    selected = compiler_environment([tmp_path / "editable source"], "x86_64")
    flags = selected["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
    assert flags[:2] == ["-C", "opt-level=2"]
    assert f"--remap-path-prefix={cargo}=." in flags
    assert f"--remap-path-prefix={rustup}=." in flags
    assert selected["ARCHFLAGS"] == "-arch x86_64"
    monkeypatch.setenv("CARGO_ENCODED_RUSTFLAGS", "")
    assert compiler_environment([tmp_path], "x86_64")["CARGO_ENCODED_RUSTFLAGS"].startswith("--remap-path-prefix=")
