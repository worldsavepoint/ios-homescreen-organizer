"""Native launcher fingerprints reject malformed inline load commands."""
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "macapp"))


def command(kind, body):
    length = (8 + len(body) + 7) // 8 * 8
    return struct.pack("<II", kind, length) + body + bytes(length - 8 - len(body))


def launcher(extra, cpu=0x01000007):
    page = 16384 if cpu == 0x0100000C else 4096
    data = bytearray(page + 16)
    struct.pack_into("<IiiIIIII", data, 0, 0xFEEDFACF, cpu, 0, 2, 2, 72 + len(extra), 0, 0)
    struct.pack_into("<II16sQQQQiiII", data, 32, 0x19, 72, b"__LINKEDIT", page * 4, page, page, 16, 1, 1, 0, 0)
    data[104:104 + len(extra)] = extra
    return data


INLINE = [
    ("uuid", command(0x1B, bytes(16)), 0x01000007),
    ("source-version", command(0x2A, bytes(8)), 0x01000007),
    ("routines", command(0x1A, bytes(64)), 0x01000007),
    ("build-version", command(0x32, struct.pack("<4I", 1, 0, 0, 0)), 0x01000007),
    ("build-tool", command(0x32, struct.pack("<6I", 1, 0, 0, 1, 3, 0)), 0x01000007),
    ("prebound", command(0x10, struct.pack("<3I", 20, 9, 25) + b"name\0\1\0"), 0x01000007),
    ("linker-options", command(0x2D, struct.pack("<I", 2) + b"-x\0value\0"), 0x01000007),
    *[(f"minimum-{kind:x}", command(kind, bytes(8)), 0x01000007) for kind in (0x24, 0x25, 0x2F, 0x30)],
    *[(f"dylib-{kind:x}", command(kind, struct.pack("<4I", 24, 0, 0, 0) + b"name\0"), 0x01000007)
      for kind in (0xC, 0xD, 0x80000018, 0x8000001F, 0x20, 0x80000023)],
    *[(f"string-{kind:x}", command(kind, struct.pack("<I", 12) + b"name\0"), 0x01000007)
      for kind in (0xE, 0xF, 0x12, 0x13, 0x14, 0x15, 0x8000001C)],
    *[(f"thread-{cpu:x}-{kind}", command(kind, struct.pack("<II", flavor, count) + bytes(count * 4)), cpu)
      for cpu, flavor, count in ((0x01000007, 4, 42), (0x0100000C, 6, 68)) for kind in (4, 5)],
]


@pytest.mark.parametrize("name,extra,cpu", INLINE, ids=[item[0] for item in INLINE])
def test_inline_commands_accept_complete_headers_and_reject_eight_byte_stubs(name, extra, cpu):
    from frozen_sources import canonical_launcher_payload
    assert canonical_launcher_payload(launcher(extra, cpu)) == launcher(extra, cpu)
    stub = struct.pack("<II", struct.unpack_from("<I", extra)[0], 8)
    with pytest.raises(ValueError, match="Could not canonicalize the native launcher"):
        canonical_launcher_payload(launcher(stub, cpu))


@pytest.mark.parametrize("extra", [
    command(0x1B, bytes(24)), command(0x2A, bytes(16)), command(0x1A, bytes(72)), command(0x24, bytes(16)),
    command(0x32, struct.pack("<4I", 1, 0, 0, 1)),
    command(0xC, struct.pack("<4I", 8, 0, 0, 0) + b"name\0"),
    command(0xC, struct.pack("<4I", 999, 0, 0, 0) + b"name\0"),
    command(0xE, struct.pack("<I", 12) + b"unterminated"),
    command(0x8000001C, struct.pack("<I", 8) + b"name\0"),
    command(0x10, struct.pack("<3I", 20, 9, 999) + b"name\0\1\0"),
    command(0x2D, struct.pack("<I", 100) + b"-x\0value\0"),
    command(0x2D, struct.pack("<I", 1) + b"-x\0not-padding"),
    command(5, struct.pack("<II", 4, 41) + bytes(42 * 4)),
    command(5, struct.pack("<II", 6, 42) + bytes(42 * 4)),
])
def test_inline_command_lengths_strings_counts_and_thread_flavors_are_bounded(extra):
    from frozen_sources import canonical_launcher_payload
    with pytest.raises(ValueError, match="Could not canonicalize the native launcher"):
        canonical_launcher_payload(launcher(extra))


@pytest.fixture(scope="module", params=["arm64", "x86_64"])
def compiled_launcher(tmp_path_factory, request):
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        pytest.skip("Uses the native Swift compiler and Mach-O signer")
    folder = tmp_path_factory.mktemp("inline-launcher-" + request.param)
    source = folder / "main.swift"
    source.write_text('print("inline command fixture")\n')
    binary = folder / "launcher"
    subprocess.run(["swiftc", "-O", "-target", request.param + "-apple-macosx14.0", "-module-cache-path",
                    folder / "modules", source, "-o", binary], check=True, capture_output=True)
    subprocess.run(["codesign", "--remove-signature", binary], check=True, capture_output=True)
    return binary


@pytest.mark.parametrize("damage", ["uuid-split", "build-tool-count"])
@pytest.mark.parametrize("api", ["payload", "fingerprint"])
def test_compiled_swift_inline_structure_is_validated_before_fingerprinting(compiled_launcher, tmp_path, damage, api):
    from frozen_sources import canonical_launcher_payload, launcher_digest
    assert len(launcher_digest(compiled_launcher)) == 64
    data = bytearray(compiled_launcher.read_bytes())
    commands, offset = {}, 32
    for _ in range(struct.unpack_from("<I", data, 16)[0]):
        kind, length = struct.unpack_from("<II", data, offset)
        commands[kind] = (offset, length)
        offset += length
    if damage == "uuid-split":
        offset, length = commands[0x1B]
        assert length == 24
        data[offset:offset + 24] = struct.pack("<6I", 0x1B, 8, 0x1B, 8, 0x1B, 8)
        struct.pack_into("<I", data, 16, struct.unpack_from("<I", data, 16)[0] + 2)
    else:
        offset, _ = commands[0x32]
        struct.pack_into("<I", data, offset + 20, struct.unpack_from("<I", data, offset + 20)[0] + 1)
    malformed = tmp_path / "malformed-launcher"
    malformed.write_bytes(data)
    with pytest.raises(ValueError, match="launcher"):
        canonical_launcher_payload(data) if api == "payload" else launcher_digest(malformed)
