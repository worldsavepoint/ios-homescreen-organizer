"""Prepared Cargo SBOMs retain their graph without local build identifiers."""
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "macapp"))
INFO = "cryptography-50.0.2.dist-info/"
SBOM = INFO + "sboms/cryptography-rust.cyclonedx.json"
RECORD = INFO + "RECORD"


def cargo_bom():
    source = "path+file:///private/native-build-marker/cryptography/src/rust"
    root = source + "#cryptography-rust@0.50.2"
    local = source + "/cryptography-cffi#0.50.2"
    registry = "registry+https://github.com/rust-lang/crates.io-index#openssl@0.10.81"
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "serialNumber": "urn:uuid:12345678-1234-1234-1234-123456789abc",
            "metadata": {"timestamp": "2026-01-01T00:00:00Z", "tools": [{"name": "cargo-cyclonedx", "version": "0.5.9"}],
                "component": {"type": "library", "bom-ref": root, "name": "cryptography-rust", "version": "0.50.2",
                    "licenses": [{"expression": "Apache-2.0 OR BSD-3-Clause"}],
                    "purl": "pkg:cargo/cryptography-rust@0.50.2?download_url=file://.",
                    "components": [{"type": "library", "bom-ref": root + " bin-target-0", "name": "cryptography_rust",
                                    "purl": "pkg:cargo/cryptography-rust@0.50.2?download_url=file://.#src/lib.rs"}]}},
            "components": [{"type": "library", "bom-ref": local, "name": "cryptography-cffi", "version": "0.50.2",
                            "purl": "pkg:cargo/cryptography-cffi@0.50.2?download_url=file://cryptography-cffi",
                            "licenses": [{"expression": "Apache-2.0 OR BSD-3-Clause"}]},
                           {"type": "library", "bom-ref": registry, "name": "openssl", "version": "0.10.81",
                            "purl": "pkg:cargo/openssl@0.10.81", "hashes": [{"alg": "SHA-256", "content": "a" * 64}]}],
            "dependencies": [{"ref": root, "dependsOn": [local, registry]}, {"ref": local, "dependsOn": [registry]},
                             {"ref": registry, "dependsOn": []}]}


def component_ids(bom):
    result = {}
    def visit(value):
        if isinstance(value, dict):
            if "bom-ref" in value:
                result[value["bom-ref"]] = value.get("purl")
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(bom)
    return result


def test_portable_cargo_ids_preserve_every_other_field_and_dependency_edge():
    from wheel_sbom import portable_cargo_sbom
    original = cargo_bom()
    encoded = json.dumps(original).encode()
    portable = json.loads(portable_cargo_sbom(encoded))
    mapping = {identifier: purl for identifier, purl in component_ids(original).items() if identifier.startswith("path+file://")}
    # Original purls remain purls; only the opaque identifier positions change.
    for old, new in mapping.items():
        assert new in component_ids(portable)
    expected = json.loads(encoded)
    def replace_ids(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("bom-ref", "ref"):
                    value[key] = mapping.get(item, item)
                elif key == "dependsOn":
                    value[key] = [mapping.get(identifier, identifier) for identifier in item]
                else:
                    replace_ids(item)
        elif isinstance(value, list):
            for item in value:
                replace_ids(item)
    replace_ids(expected)
    assert portable == expected and original == json.loads(encoded)
    assert b"native-build-marker" not in portable_cargo_sbom(encoded)
    assert portable_cargo_sbom(portable_cargo_sbom(encoded)) == portable_cargo_sbom(encoded)


def test_dependency_leaf_without_optional_edges_is_preserved():
    from wheel_sbom import portable_cargo_sbom
    bom = cargo_bom()
    del bom["dependencies"][-1]["dependsOn"]
    portable = json.loads(portable_cargo_sbom(json.dumps(bom).encode()))
    assert portable["dependencies"][-1] == bom["dependencies"][-1]


@pytest.mark.parametrize("damage", ["missing-purl", "absolute-purl", "duplicate-id", "collision", "dangling", "duplicate-key"])
def test_ambiguous_or_incomplete_cargo_graphs_are_rejected(damage):
    from wheel_sbom import portable_cargo_sbom
    bom = cargo_bom()
    if damage == "missing-purl":
        del bom["components"][0]["purl"]
    elif damage == "absolute-purl":
        bom["components"][0]["purl"] = "pkg:cargo/cryptography-cffi@0.50.2?download_url=file:///%70rivate/secret"
    elif damage == "duplicate-id":
        bom["components"].append(dict(bom["components"][0]))
    elif damage == "collision":
        bom["components"][0]["purl"] = bom["metadata"]["component"]["purl"]
    elif damage == "dangling":
        bom["dependencies"][0]["dependsOn"].append("path+file:///private/unaccounted/source#crate")
    encoded = json.dumps(bom).encode()
    if damage == "duplicate-key":
        encoded = encoded.replace(b'"version": 1,', b'"version": 1, "version": 2,', 1)
    with pytest.raises(ValueError, match="SBOM"):
        portable_cargo_sbom(encoded)


def make_wheel(folder):
    wheel = folder / "cryptography-50.0.2-cp312-abi3-macosx_14_0_x86_64.whl"
    files = {"crypto.so": b"unchanged native payload", INFO + "METADATA": b"Name: cryptography\nVersion: 50.0.2\n",
             SBOM: json.dumps(cargo_bom()).encode(), INFO + "licenses/LICENSE": b"unchanged grant"}
    record = io.StringIO(newline="")
    rows = csv.writer(record, lineterminator="\n")
    for name, data in files.items():
        rows.writerow((name, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), len(data)))
    rows.writerow((RECORD, "", ""))
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr(RECORD, record.getvalue())
    return wheel, files


def test_prepared_wheel_preserves_members_and_updates_verified_record_before_lock(tmp_path):
    import os
    from check_package_paths import check_archive, BuildPathError
    from test_prepare_intel import command
    from wheel_sbom import rewrite_cryptography_wheel
    wheel, files = make_wheel(tmp_path)
    blocked = [Path("/private/native-build-marker")]
    with pytest.raises(BuildPathError) as leaked:
        check_archive(wheel, blocked, engine=None)
    assert SBOM in leaked.value.__notes__[0]
    rewrite_cryptography_wheel(wheel)
    check_archive(wheel, blocked, engine=None)
    with zipfile.ZipFile(wheel) as archive:
        assert set(archive.namelist()) == set(files) | {RECORD}
        for name, data in files.items():
            if name != SBOM:
                assert archive.read(name) == data
        rows = list(csv.reader(io.StringIO(archive.read(RECORD).decode())))
        assert len(rows) == len(files) + 1
        for name, value, size in rows:
            if name == RECORD:
                assert value == size == ""
            else:
                data = archive.read(name)
                assert size == str(len(data))
                assert value == "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    script = (ROOT / "macapp/prepare_intel.sh").read_text()
    assert script.index('"$ROOT/macapp/wheel_sbom.py" "$WORK/wheels"') < script.index('"$BUILD_PYTHON" - "$WORK/wheels"')
    commands = tmp_path / "commands"
    commands.mkdir()
    command(commands, "lipo", "echo x86_64")
    command(commands, "otool", 'if [[ "$1" == "-l" ]]; then echo "cmd LC_BUILD_VERSION"; echo "minos 14.0"; fi')
    notice = tmp_path / "NOTICE"
    notice.write_text("grant")
    output = tmp_path / "prepared"
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    subprocess.run([sys.executable, "-", tmp_path, output, ROOT / "requirements-build-x86_64.lock", notice],
                   input=body, text=True, check=True, capture_output=True, env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"]})
    value = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert f"cryptography==50.0.2 --hash=sha256:{value}" in (output / "requirements-build.lock").read_text()
    assert (output / "wheels" / wheel.name).read_bytes() == wheel.read_bytes()


@pytest.mark.parametrize("damage", ["hash", "size", "duplicate", "signed"])
def test_unverified_wheel_is_never_rewritten(tmp_path, damage):
    from wheel_sbom import rewrite_cryptography_wheel
    wheel, _ = make_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as source:
        files = [(info, source.read(info)) for info in source.infolist()]
    with zipfile.ZipFile(wheel, "w") as archive:
        for info, data in files:
            if info.filename == RECORD:
                if damage == "hash":
                    data = data.replace(b"sha256=", b"sha512=", 1)
                elif damage == "size":
                    rows = list(csv.reader(io.StringIO(data.decode())))
                    rows[0][2] = "999999"
                    stream = io.StringIO(newline="")
                    csv.writer(stream).writerows(rows)
                    data = stream.getvalue().encode()
            archive.writestr(info, data)
        if damage == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate"):
                archive.writestr(SBOM, b"duplicate")
        if damage == "signed":
            archive.writestr(RECORD + ".jws", b"signed original record")
    before = wheel.read_bytes()
    with pytest.raises(ValueError, match="wheel"):
        rewrite_cryptography_wheel(wheel)
    assert wheel.read_bytes() == before
