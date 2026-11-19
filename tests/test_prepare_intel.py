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


@pytest.mark.parametrize("cached", ["missing", "changed", "symlink"])
def test_intel_app_notice_comes_from_authenticated_capture(tmp_path, cached):
    import sys
    from source_fixture import fixture_checkout

    checkout = tmp_path / "project"
    fixture_checkout(checkout, "1.3.0")
    cache = checkout / "build/dependencies/x86_64"
    cache.mkdir(parents=True)
    untrusted = cache / "OPENSSL-LICENSE.txt"
    if cached == "changed":
        untrusted.write_bytes(b"unrelated cache notice")
    elif cached == "symlink":
        untrusted.symlink_to(tmp_path / "unrelated")
        (tmp_path / "unrelated").write_bytes(b"unrelated cache notice")
    stage = tmp_path / "capture"
    notice = stage / "ThirdPartyLicenses/native/openssl/4.0.3/openssl-4.0.3/LICENSE.txt"
    notice.parent.mkdir(parents=True)
    notice.write_bytes(b"authenticated OpenSSL grant")
    output = tmp_path / "app-notice"
    args = [sys.executable, checkout / "macapp/source_archive.py", "intel-notice", "--inputs", stage, "--output", output]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == b"authenticated OpenSSL grant"
    notice.unlink()
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode != 0
    notice.symlink_to(tmp_path / "unrelated")
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode != 0


def test_changed_additional_intel_notice_is_rejected(tmp_path):
    import json
    import zipfile
    from source_fixture import release_source
    from source_archive import check_zip

    release_source(tmp_path, "1.3.0", tmp_path / "checkout")
    archive = tmp_path / "home-screen-organizer-1.3.0-macos-x86_64.zip"
    with zipfile.ZipFile(archive) as source:
        files = [(info, source.read(info)) for info in source.infolist()]
        inventory = json.loads(source.read("Screen Organizer.app/Contents/Resources/SourceInventory.json"))
    check_zip(archive, inventory)
    with zipfile.ZipFile(archive, "w") as target:
        for info, data in files:
            target.writestr(info, b"changed extra notice" if info.filename.endswith("/OPENSSL-LICENSE.txt") else data)
    with pytest.raises(ValueError, match="OpenSSL"):
        check_zip(archive, inventory)


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
    import sys

    commands = tmp_path / "bin"
    commands.mkdir()
    command(commands, "uname", 'if [[ "$1" == "-s" ]]; then echo Darwin; else echo x86_64; fi\n')
    command(commands, "sysctl", "echo 0\n")
    python = command(commands, "python", '''if [[ "$1" == -c ]]; then exit 0; fi
exec "$TEST_PYTHON" "$TEST_SUPPLIER" "$@"
''')
    supplier = tmp_path / "source-response.py"
    supplier.write_text('''import io, runpy, sys, urllib.request
class Response(io.BytesIO):
    def geturl(self):
        return "https://example.invalid/source"
urllib.request.urlopen = lambda *args, **kwargs: Response(b"tampered")
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
''')
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
                            env={**os.environ, "PATH": f"{commands}:{os.environ['PATH']}",
                                 "TEST_PYTHON": sys.executable, "TEST_SUPPLIER": str(supplier),
                                 "HOMESCREEN_SOURCE_CACHE": str(tmp_path / "source-cache")},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "Incorrect source digest" in result.stderr
    assert not marker.exists()
    assert not output.exists()


@pytest.mark.parametrize("supply", ["verified", "oversized", "short", "wrong-digest", "unavailable"])
def test_intel_native_source_supplier_authenticates_before_use(tmp_path, monkeypatch, supply):
    import hashlib
    import io
    import json
    from macapp import source_inputs

    project = tmp_path / "project"
    locks = project / "macapp/sources"
    locks.mkdir(parents=True)
    pins = {}
    for name in ("openssl-4.0.3.tar.gz", "cryptography-50.0.2.tar.gz"):
        data = name.encode()
        pins[name] = {"url": "https://example.invalid/" + name, "size": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()}
    openssl, crypto = pins.values()
    (locks / "native.json").write_text(json.dumps({"assets": [{"id": "openssl/4.0.3", **openssl}]}))
    (locks / "python.json").write_text(json.dumps({"cryptography": {"source": crypto}}))
    (project / "requirements-cryptography-x86_64.lock").write_text(
        f"cryptography @ {crypto['url']} --hash=sha256:{crypto['sha256']}\n")
    monkeypatch.setattr(source_inputs, "LOCKS", locks)
    cache = tmp_path / "cache"
    monkeypatch.setenv("HOMESCREEN_SOURCE_CACHE", str(cache))
    calls = []
    class Response(io.BytesIO):
        def geturl(self):
            return "https://example.invalid/immutable-source"
        def read(self, size):
            assert 0 < size <= source_inputs.BLOCK
            return super().read(size)
    def supply_source(url, timeout):
        name = url.rsplit("/", 1)[1]
        calls.append(name)
        data = name.encode()
        if name.startswith("cryptography"):
            if supply == "unavailable":
                raise OSError("fixture source unavailable")
            data = {"verified": data, "oversized": data + b"!", "short": data[:-1],
                    "wrong-digest": b"x" * len(data)}[supply]
        return Response(data)
    monkeypatch.setattr(source_inputs, "urlopen", supply_source)
    output = tmp_path / "build inputs"
    output.mkdir()
    if supply == "verified":
        source_inputs.prepare_intel_sources(output, "4.0.3")
        sdist = output / "cryptography-50.0.2.tar.gz"
        assert sdist.read_bytes() == sdist.name.encode()
        assert (output / "cryptography.lock").read_text() == (
            f"cryptography @ {sdist.as_uri()} --hash=sha256:{crypto['sha256']}\n")
        cached = tmp_path / "from-cache"
        cached.mkdir()
        source_inputs.prepare_intel_sources(cached, "4.0.3")
        assert (cached / sdist.name).read_bytes() == sdist.read_bytes()
    else:
        with pytest.raises((OSError, ValueError), match="unavailable|digest|size"):
            source_inputs.prepare_intel_sources(output, "4.0.3")
        assert not (output / "cryptography.lock").exists()
        assert [p.name for p in cache.iterdir()] == [openssl["sha256"] + "-openssl-4.0.3.tar.gz"]
    assert calls == list(pins)


@pytest.mark.parametrize("reply", ["once", "persistent", "permanent", "short", "digest", "size"])
def test_authenticated_download_retries_only_transient_failures(tmp_path, monkeypatch, reply):
    import hashlib
    import io
    from urllib.error import HTTPError
    from macapp import source_inputs

    value = b"authentic bytes"
    pin = {"url": "https://example.invalid/archive", "size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    calls, waits = [], []
    class Response(io.BytesIO):
        def geturl(self):
            return pin["url"]
    def supply(url, timeout):
        calls.append(url)
        if reply == "persistent" or reply == "once" and len(calls) == 1:
            raise HTTPError(url, 503, "temporarily unavailable", {}, None)
        if reply == "permanent":
            raise HTTPError(url, 404, "missing", {}, None)
        return Response({"once": value, "short": value[:-1], "digest": b"x" * len(value), "size": value + b"x"}[reply])
    monkeypatch.setattr(source_inputs, "urlopen", supply)
    monkeypatch.setattr(source_inputs.time, "sleep", waits.append)
    cache = tmp_path / "cache"
    stage = io.BytesIO()
    if reply == "once":
        blob = source_inputs.fetch(pin, cache, staged=stage, retry_transient=True)
        assert blob.read_bytes() == stage.getvalue() == value
        assert len(calls) == 2 and waits == [1]
    else:
        with pytest.raises((HTTPError, ValueError)):
            source_inputs.fetch(pin, cache, retry_transient=True)
        assert not list(cache.iterdir())
        assert len(calls) == (4 if reply == "persistent" else 1)
        assert waits == ([1, 2, 4] if reply == "persistent" else [])


@pytest.mark.parametrize("change", ["unchanged", "longer", "renamed", "directory"])
def test_cache_staging_uses_one_bounded_regular_descriptor(tmp_path, monkeypatch, change):
    import hashlib
    import io
    from macapp import source_inputs

    payload = b"pinned source data"
    pin = {"url": "https://example.invalid/source", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    candidate = tmp_path / (pin["sha256"] + "-source")
    if change == "directory":
        candidate.mkdir()
    else:
        candidate.write_bytes(payload)
    identity = candidate.stat()
    fstat, fdopen = os.fstat, os.fdopen
    reads, samples, offsets = [], [], []
    def sample(fd):
        result = fstat(fd)
        if os.path.samestat(result, identity) and not samples:
            samples.append(fd)
            if change == "longer":
                with candidate.open("ab") as stream:
                    stream.write(b"x" * 100)
            if change == "renamed":
                candidate.rename(tmp_path / "original")
                candidate.write_bytes(b"x" * len(payload))
        return result
    def measured(fd, *args, **kwargs):
        stream = fdopen(fd, *args, **kwargs)
        if os.path.samestat(fstat(fd), identity):
            read = stream.read
            def bounded(amount):
                assert 0 < amount <= len(payload)
                data = read(amount)
                reads.append(len(data))
                offsets.append(os.lseek(fd, 0, os.SEEK_CUR))
                return data
            stream.read = bounded
        return stream
    monkeypatch.setattr(os, "fstat", sample)
    monkeypatch.setattr(os, "fdopen", measured)
    staged = io.BytesIO()
    if change in ("directory", "longer"):
        with pytest.raises(ValueError, match="Missing verified"):
            source_inputs.fetch(pin, tmp_path, offline=True, staged=staged)
    else:
        assert source_inputs.fetch(pin, tmp_path, offline=True, staged=staged) == candidate
        assert staged.getvalue() == payload
    assert len(samples) == 1
    assert sum(reads) == (0 if change == "directory" else len(payload) + int(change == "longer"))
    if offsets:
        assert offsets[-1] == len(payload) + int(change == "longer")


@pytest.mark.parametrize("headers, waits, succeeds", [
    (["9"], [9], True), (["Tue, 14 Nov 2023 22:13:29 GMT"], [9], True),
    (["Tuesday, 14-Nov-23 22:13:29 GMT"], [9], True), (["Tue Nov 14 22:13:29 2023"], [9], True),
    (["Tue, 14 Nov 2023 22:15:00 UNKNOWN"], [1], True),
    (["Tue, 14 Nov 2023 22:15:00"], [1], True),
    (["Tue, 14 Nov 2023 22:13:19 GMT"], [1], True), (["invalid"], [1], True),
    (["45"], [45], True), (["46"], [], False), (["30", "16"], [30], False),
])
def test_retry_after_fits_the_remaining_wait_budget(tmp_path, monkeypatch, headers, waits, succeeds):
    import hashlib
    import io
    from urllib.error import HTTPError
    from macapp import source_inputs

    value = b"locked"
    pin = {"url": "https://example.invalid/file", "size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    delays, requests = [], []
    class Response(io.BytesIO):
        def geturl(self):
            return pin["url"]
    def supplier(url, timeout):
        index = len(requests)
        requests.append(sum(delays))
        if index < len(headers):
            raise HTTPError(url, 503, "temporarily unavailable", {"Retry-After": headers[index]}, None)
        return Response(value)
    monkeypatch.setattr(source_inputs, "urlopen", supplier)
    monkeypatch.setattr(source_inputs.time, "time", lambda: 1700000000 + sum(delays))
    monkeypatch.setattr(source_inputs.time, "sleep", delays.append)
    if succeeds:
        staged = io.BytesIO()
        result = source_inputs.fetch(pin, tmp_path, staged=staged, retry_transient=True)
        assert result.read_bytes() == staged.getvalue() == value
    else:
        with pytest.raises(HTTPError):
            source_inputs.fetch(pin, tmp_path, retry_transient=True)
        assert not list(tmp_path.iterdir())
    assert delays == waits and len(requests) == len(waits) + 1


@pytest.mark.parametrize("case", ["refresh", "offline", "incorrect-source", "same-size"])
def test_unreadable_owned_cache_entry_requires_verified_replacement(tmp_path, monkeypatch, case):
    import errno
    import hashlib
    import io
    from macapp import source_inputs

    value = b"immutable"
    pin = {"url": "https://example.invalid/file", "size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    candidate = tmp_path / (pin["sha256"] + "-file")
    candidate.write_bytes(b"incorrect" if case == "same-size" else b"short")
    candidate.chmod(0)
    original = candidate.stat()
    calls = []
    class Response(io.BytesIO):
        def geturl(self):
            return pin["url"]
    def supplier(url, timeout):
        calls.append(url)
        return Response(b"different" if case == "incorrect-source" else value)
    native_open = os.open
    def open_file(path, flags, *args, **kwargs):
        if path == candidate and flags & os.O_ACCMODE == os.O_RDONLY and candidate.stat().st_mode & 0o777 == 0:
            raise PermissionError(errno.EACCES, "unreadable fixture")
        return native_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(source_inputs, "urlopen", supplier)
    monkeypatch.setattr(os, "open", open_file)
    if case == "refresh":
        staged = io.BytesIO()
        result = source_inputs.fetch(pin, tmp_path, staged=staged)
        assert result.read_bytes() == staged.getvalue() == value and not os.path.samestat(original, result.stat())
    else:
        with pytest.raises(PermissionError if case == "same-size" else ValueError):
            source_inputs.fetch(pin, tmp_path, offline=case == "offline")
        assert os.path.samestat(original, candidate.stat()) and candidate.stat().st_mode & 0o777 == 0
    assert len(calls) == int(case in ("refresh", "incorrect-source"))


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
