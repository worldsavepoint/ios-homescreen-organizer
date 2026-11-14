"""Intel dependency preparation rejects unsupported hosts and unverified sources."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "macapp/prepare_intel.sh"


def command(folder, name, body):
    path = folder / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)
    return path


def test_generated_intel_lock_parses_with_the_selected_backend_wheel(tmp_path):
    import hashlib
    import sys
    import zipfile
    from macapp.wheelhouse import entries

    commands = tmp_path / "bin"
    commands.mkdir()
    command(commands, "lipo", "echo x86_64")
    command(commands, "otool", 'if [[ "$1" == "-l" ]]; then echo "cmd LC_BUILD_VERSION"; echo "minos 14.0"; fi')
    wheels = tmp_path / "built"
    wheels.mkdir()
    name = "cryptography-50.0.2-cp312-abi3-macosx_14_0_x86_64.whl"
    with zipfile.ZipFile(wheels / name, "w") as archive:
        archive.writestr("crypto.so", b"fixture")
        archive.writestr("cryptography-50.0.2.dist-info/METADATA", "Name: cryptography\nVersion: 50.0.2\n")
    notice = tmp_path / "LICENSE"
    notice.write_text("fixture")
    output = tmp_path / "prepared"
    output_step = SCRIPT.read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    subprocess.run([sys.executable, "-", wheels, output, ROOT / "requirements-build-x86_64.lock", notice],
                   input=output_step, text=True, check=True, capture_output=True,
                   env={**os.environ, "PATH": f"{commands}:{os.environ['PATH']}"})
    generated = output / "requirements-build.lock"
    install = tmp_path / "install.lock"
    subprocess.run([sys.executable, ROOT / "macapp/wheelhouse.py", generated, "--install-lock", install], check=True)
    expected = [("maturin-1.14.1-py3-none-macosx_10_12_x86_64.whl",
                 "f3306078070c1508fd715b9116070cbcaff5959024272a9f1e6f5cb29768b86c"),
                (name, hashlib.sha256((wheels / name).read_bytes()).hexdigest())]
    for lock in (generated, install):
        selected = [(wheel, value) for wheel, value in entries(lock)
                    if wheel.split("-")[0] in ("maturin", "cryptography")]
        assert selected == expected


@pytest.mark.parametrize("system,architecture,translated", [
    ("Darwin", "arm64", "0"),
    ("Linux", "x86_64", "0"),
    ("Darwin", "x86_64", "1"),
])
def test_unsupported_host_stops_before_downloads(tmp_path, system, architecture, translated):
    commands = tmp_path / "bin"
    commands.mkdir()
    command(commands, "uname", f'if [[ "$1" == "-s" ]]; then echo {system}; else echo {architecture}; fi\n')
    command(commands, "sysctl", f"echo {translated}\n")
    marker = tmp_path / "download-attempt"
    command(commands, "curl", f'touch "{marker}"\nexit 97\n')
    output = tmp_path / "prepared"
    result = subprocess.run(["bash", str(SCRIPT), "python3.12", str(output)],
                            env={**os.environ, "PATH": f"{commands}:{os.environ['PATH']}"},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "native Intel macOS" in result.stderr
    assert not marker.exists()
    assert not output.exists()


def test_openssl_checksum_failure_stops_before_source_execution(tmp_path):
    commands = tmp_path / "bin"
    commands.mkdir()
    command(commands, "uname", 'if [[ "$1" == "-s" ]]; then echo Darwin; else echo x86_64; fi\n')
    command(commands, "sysctl", "echo 0\n")
    python = command(commands, "python", "exit 0\n")
    command(commands, "rustup", "exit 0\n")
    command(commands, "curl", '''while [[ "$#" -gt 0 ]]; do
  if [[ "$1" == "--output" ]]; then printf tampered > "$2"; exit 0; fi
  shift
done
exit 98
''')
    marker = tmp_path / "archive-executed"
    command(commands, "tar", f'touch "{marker}"\nexit 96\n')
    output = tmp_path / "prepared"
    result = subprocess.run(["bash", str(SCRIPT), str(python), str(output)],
                            env={**os.environ, "PATH": f"{commands}:{os.environ['PATH']}"},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "checksum" in (result.stdout + result.stderr).lower()
    assert not marker.exists()
    assert not output.exists()


def test_reused_intel_paths_follow_the_prepared_wheel(tmp_path):
    import shutil
    from macapp.build_paths import intel_inputs, read_roots, write_record

    prepared = tmp_path / "dependencies"
    wheel = prepared / "wheels/cryptography-test.whl"
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"compiled")
    (prepared / "requirements-build.lock").write_bytes(b"locked")
    with pytest.raises(ValueError):
        read_roots(prepared, intel_inputs(prepared))
    original = tmp_path / "system-temp/build with spaces"
    write_record(prepared, [original], intel_inputs(prepared))
    reused = tmp_path / "another-checkout"
    shutil.copytree(prepared, reused)
    assert original in read_roots(reused, intel_inputs(reused))
    (reused / "wheels" / wheel.name).write_bytes(b"different")
    with pytest.raises(ValueError):
        read_roots(reused, intel_inputs(reused))


@pytest.mark.parametrize("cached", ["file", "symlink", "hardlink"])
def test_preparation_replaces_stale_cryptography_wheels(tmp_path, cached):
    import hashlib
    import sys
    import zipfile
    from macapp.build_paths import intel_inputs, read_roots

    commands = tmp_path / "bin"
    commands.mkdir()
    command(commands, "lipo", "echo x86_64")
    command(commands, "otool", 'if [[ "$1" == "-l" ]]; then echo "Load command 0"; echo "cmd LC_BUILD_VERSION"; echo "minos 14.0"; fi')
    wheels = tmp_path / "new"
    wheels.mkdir()
    name = "cryptography-50.0.2-cp312-abi3-macosx_14_0_x86_64.whl"
    with zipfile.ZipFile(wheels / name, "w") as package:
        package.writestr("crypto.so", b"fixture")
        package.writestr("cryptography-50.0.2.dist-info/METADATA", "Name: cryptography\nVersion: 50.0.2\n")
    output = tmp_path / "cache"
    target = output / "wheels"
    target.mkdir(parents=True)
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"keep original")
    if cached == "symlink":
        (target / name).symlink_to(unrelated)
    elif cached == "hardlink":
        (target / name).hardlink_to(unrelated)
    else:
        (target / name).write_bytes(b"stale")
    (target / "cryptography-older.whl").write_bytes(b"old tag")
    (target / "other.whl").write_bytes(b"keep")
    lock = tmp_path / "pins"
    lock.write_text("# fixture pins\n")
    license = tmp_path / "LICENSE"
    license.write_text("fixture")
    output_step = SCRIPT.read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    env = {**os.environ, "PATH": str(commands) + ":" + os.environ["PATH"]}
    subprocess.run([sys.executable, "-", wheels, output, lock, license], input=output_step,
                   text=True, check=True, env=env, capture_output=True)
    assert sorted(p.name for p in target.iterdir()) == sorted([name, "other.whl"])
    assert unrelated.read_bytes() == b"keep original"
    assert not (target / name).is_symlink() and not (target / name).samefile(unrelated)
    assert (target / name).read_bytes() == (wheels / name).read_bytes()
    digest = hashlib.sha256((target / name).read_bytes()).hexdigest()
    assert f"cryptography==50.0.2 --hash=sha256:{digest}" in (output / "requirements-build.lock").read_text()
    provenance = [sys.executable, SCRIPT.with_name("build_paths.py")]
    subprocess.run([*provenance, "intel-record", output, wheels], check=True)
    subprocess.run([*provenance, "intel-check", output], check=True)
    assert wheels in read_roots(output, intel_inputs(output))
    (target / name).unlink()
    (target / name).symlink_to(wheels / name)
    for action in ("intel-check", "intel-record"):
        result = subprocess.run([*provenance, action, output], capture_output=True, text=True)
        assert result.returncode != 0
        assert result.stderr == "Prepare a fresh native build to record its locations\n"
