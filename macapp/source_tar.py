"""Read the public source tar format with bounded headers and streaming file hashes."""
import hashlib
import io
import re
import tarfile
import zlib

from source_inputs import BLOCK, relative_name

ZERO = bytes(tarfile.BLOCKSIZE)
GZIP_HEADER = bytes.fromhex("1f8b08000000000004ff")


class SourceGzip(io.RawIOBase):
    """Read one normalized gzip member, including its checksum and end marker."""

    def __init__(self, source):
        self.source = source
        self.decoder = zlib.decompressobj(31)
        self.pending = source.read(len(GZIP_HEADER))
        if self.pending != GZIP_HEADER:
            raise ValueError("Invalid source gzip header")

    def readable(self):
        return True

    def readinto(self, buffer):
        while not self.decoder.eof:
            if not self.pending:
                self.pending = self.source.read(64 * 1024)
                if not self.pending:
                    raise ValueError("Truncated source gzip stream")
            try:
                chunk = self.decoder.decompress(self.pending, len(buffer))
            except zlib.error:
                raise ValueError("Invalid source gzip stream") from None
            self.pending = self.decoder.unconsumed_tail
            if chunk:
                buffer[:len(chunk)] = chunk
                return len(chunk)
        if self.decoder.unused_data or self.source.read(1):
            raise ValueError("Unexpected data after the source gzip member")
        return 0


def canonical_members(path, capture, limit):
    """Yield names, modes, digests and the requested small metadata files."""
    total = count = 0
    extended = b""
    long_name = None
    with path.open("rb") as raw, io.BufferedReader(SourceGzip(raw)) as stream:
        while True:
            block = stream.read(512)
            if block == ZERO:
                # TarFile writes two end blocks and rounds the stream to a full record.
                remaining = 512 + (-(total + 1024) % tarfile.RECORDSIZE)
                if (extended or total + 512 + remaining > 4 * limit
                        or stream.read(remaining) != bytes(remaining) or stream.read(1)):
                    raise ValueError("Invalid source archive end record")
                return
            try:
                header = tarfile.TarInfo.frombuf(block, "ascii", "strict")
            except (tarfile.HeaderError, UnicodeError, ValueError):
                raise ValueError("Invalid source archive header") from None
            padding = -header.size % 512
            total += 512 + header.size + padding
            if not 0 <= header.size <= limit or total > 4 * limit:
                raise ValueError("Source archive exceeds its unpacked size limit")
            if header.type == tarfile.XHDTYPE:
                if extended or not 0 < header.size <= 4096:
                    raise ValueError("Invalid source archive extended header size")
                data = stream.read(header.size)
                match = re.fullmatch(rb"([0-9]{1,4}) path=([^\0\n]+)\n", data)
                if not match or int(match[1]) != header.size:
                    raise ValueError("Invalid source archive extended path")
                long_name = match[2].decode("utf-8")
                extended = block + data + stream.read(padding)
                continue
            count += 1
            if header.type != tarfile.REGTYPE or header.mode not in (0o644, 0o755) or count > 100000:
                raise ValueError("Invalid source archive file header")
            name = relative_name(long_name or header.name)
            expected = tarfile.TarInfo(name)
            expected.mode, expected.size = header.mode, header.size
            if extended + block != expected.tobuf(format=tarfile.PAX_FORMAT):
                raise ValueError("Unexpected source archive header metadata")
            extended, long_name = b"", None
            if name in capture and header.size > capture[name]:
                raise ValueError("Invalid source metadata size")
            content = bytearray() if name in capture else None
            digest = hashlib.sha256()
            remaining = header.size
            while remaining:
                chunk = stream.read(min(remaining, BLOCK))
                if not chunk:
                    raise ValueError("Truncated source archive member")
                remaining -= len(chunk)
                digest.update(chunk)
                if content is not None:
                    content.extend(chunk)
            if stream.read(padding) != bytes(padding):
                raise ValueError("Invalid source archive padding")
            yield name, header.mode, digest.hexdigest(), content
