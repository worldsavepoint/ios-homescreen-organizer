"""Check a trusted native release ZIP for local build paths."""
import argparse
import codecs
import io
import json
from importlib.util import MAGIC_NUMBER
import marshal
import os
from pathlib import Path
import plistlib
import re
import stat
import struct
import subprocess
import sys
import sysconfig
import tempfile
from types import CodeType
from xml.etree import ElementTree
import zipfile

ENGINE = "Screen Organizer.app/Contents/Resources/engine/engine"


class BuildPathError(ValueError):
    pass


class PackageMetadataError(ValueError):
    pass


def check_zip_metadata(archive):
    """Inspect central and local metadata independently of compressed payload bytes."""
    def extra_fields(extra):
        offset = 0
        while offset < len(extra):
            if len(extra) - offset < 4:
                raise ValueError("Could not verify native ZIP metadata")
            kind, size = struct.unpack_from("<HH", extra, offset)
            offset += 4
            if offset + size > len(extra):
                raise ValueError("Could not verify native ZIP metadata")
            if (kind in (0x7875, 0x756E, 0x000D, 0x4453)
                    or kind == 0x7855 and size > 0 or kind == 0x5855 and size > 8):
                raise PackageMetadataError("Native ZIP contains numeric ownership metadata")
            offset += size

    for info in archive.infolist():
        if any(part == "__MACOSX" or part.startswith("._") for part in info.filename.split("/")):
            raise PackageMetadataError("Native ZIP contains provenance metadata members")
        extra_fields(info.extra)
        archive.fp.seek(info.header_offset)
        header = archive.fp.read(30)
        if len(header) != 30 or header[:4] != b"PK\x03\x04":
            raise ValueError("Could not verify native ZIP metadata")
        name_length, extra_length = struct.unpack_from("<HH", header, 26)
        archive.fp.read(name_length)
        extra = archive.fp.read(extra_length)
        if len(extra) != extra_length:
            raise ValueError("Could not verify native ZIP metadata")
        extra_fields(extra)


def wide_text(data):
    for bom, encoding in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                          (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                return None
    return None


def is_xml_plist(data):
    markup = data.lstrip(b" \t\r\n\0")
    if not markup.startswith((b"<", codecs.BOM_UTF8, codecs.BOM_UTF32_BE, codecs.BOM_UTF32_LE,
                              codecs.BOM_UTF16_BE, codecs.BOM_UTF16_LE)):
        return False
    try:
        wide = wide_text(data)
        stream = io.StringIO(wide) if wide is not None else io.BytesIO(data)
        _, root = next(ElementTree.iterparse(stream, events=("start",)))
        return root.tag == "plist"
    except (ElementTree.ParseError, LookupError, StopIteration):
        return False


def check_archive(path, blocked, *, engine=ENGINE):
    try:
        from PyInstaller import __version__
        from PyInstaller.archive.readers import CArchiveReader
        if __version__ != "6.22.3":
            raise ImportError
    except ImportError:
        raise ValueError("Use the locked build environment to verify the archive") from None

    roots = {os.fsencode(form) for root in blocked for form in (root.absolute(), root.resolve())}
    # A directory can be followed by a slash, a string terminator or a token delimiter.
    patterns = [re.compile(re.escape(root) + rb"(?=$|[/\x00-\x20\x7f\"':,;=])") for root in roots]

    def check(data):
        if any(pattern.search(data) for pattern in patterns):
            raise BuildPathError("Release payload contains a local build path")

    def check_value(value):
        if isinstance(value, str):
            try:
                check(os.fsencode(value))
            except UnicodeEncodeError:
                check(value.encode(errors="surrogatepass"))
        elif isinstance(value, bytes):
            check(value)
        elif isinstance(value, CodeType):
            check_value(value.co_filename)
            check_value(value.co_consts)
        elif isinstance(value, dict):
            for key, item in value.items():
                check_value(key)
                check_value(item)
        elif isinstance(value, (list, tuple, frozenset)):
            for item in value:
                check_value(item)

    def check_payload(name, data, regular):
        check(data)
        if regular:
            lower = name.lower()
            if lower.endswith(".pyc") and data.startswith(MAGIC_NUMBER) and any(root in data for root in roots):
                check_value(marshal.loads(data[16:]))
            elif data.startswith(b"bplist00"):
                check_value(plistlib.loads(data, fmt=plistlib.FMT_BINARY))
            elif lower.endswith(".strings") or lower.endswith(".plist") or is_xml_plist(data):
                wide = wide_text(data)
                if wide is not None:
                    check_value(wide)
                if lower.endswith(".strings") or data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
                    result = subprocess.run(["/usr/bin/plutil", "-convert", "binary1", "-o", "-", "--", "-"],
                                            input=data, capture_output=True, check=True, timeout=10)
                    check_value(plistlib.loads(result.stdout))
                else:
                    check_value(plistlib.loads(data, fmt=plistlib.FMT_XML))
            stream = io.BytesIO(data)
            if name.lower().endswith((".zip", ".whl")) or zipfile.is_zipfile(stream):
                with zipfile.ZipFile(stream) as nested:
                    check_zip(nested)

    def check_extra(extra):
        check(extra)
        offset = 0
        while len(extra) - offset >= 4:
            size, = struct.unpack_from("<H", extra, offset + 2)
            offset += 4
            check(extra[offset:offset + size])
            offset += size

    def check_zip(archive):
        check(archive.comment)
        for info in archive.infolist():
            check(info.filename.encode())
            check(info.comment)
            check_extra(info.extra)
            # The local header can carry different extra fields than the central directory.
            archive.fp.seek(info.header_offset)
            header = archive.fp.read(30)
            name_length, extra_length = struct.unpack_from("<HH", header, 26)
            check(archive.fp.read(name_length))
            check_extra(archive.fp.read(extra_length))
            kind = stat.S_IFMT(info.external_attr >> 16)
            check_payload(info.filename, archive.read(info), kind in (0, stat.S_IFREG) and not info.is_dir())

    def check_engine(path):
        module = sysconfig._get_sysconfigdata_name()
        found = False
        package = CArchiveReader(str(path))
        for option in package.options:
            check(option.encode())
        for name, entry in package.toc.items():
            check(name.encode())
            data = package.extract(name)
            check_payload(name, data, entry[-1] in ("x", "Z"))
            if entry[-1] in ("s", "m", "M") and any(root in data for root in roots):
                check_value(marshal.loads(data))
            if entry[-1] == "z":
                pyz = package.open_embedded_archive(name)
                for member in pyz.toc:
                    check(member.encode())
                    raw = pyz.extract(member, raw=True) or b""
                    check(raw)
                    if any(root in raw for root in roots):
                        check_value(pyz.extract(member))
                if module in pyz.toc:
                    if pyz.extract(module).co_filename != module + ".py":
                        raise ValueError
                    found = True
        if not found:
            raise ValueError

    try:
        check(path.read_bytes())
        with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory() as folder:
            check_zip_metadata(archive)
            check_zip(archive)
            if engine is not None:
                executable = Path(folder) / "engine"
                executable.write_bytes(archive.read(engine))
                check_engine(executable)
    except (BuildPathError, PackageMetadataError):
        raise
    except (Exception, SystemExit):
        # Archive readers include their input filename in parsing errors.
        raise ValueError("Could not verify the native release archive") from None


def check_app(app, blocked):
    from release import check_tree_metadata, native_zip
    try:
        check_tree_metadata(app)
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "local-app.zip"
            native_zip(app, archive)
            check_archive(archive, [*blocked, Path(folder)])
    except (BuildPathError, PackageMetadataError):
        raise
    except (Exception, SystemExit):
        raise ValueError("Could not verify the local native app") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-build", type=Path)
    parser.add_argument("--intel", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("build_roots", type=Path, nargs="+")
    args = parser.parse_args()
    from build_paths import intel_inputs, read_roots, runner_roots
    try:
        roots = [*args.build_roots, Path(sys.prefix), Path(sys.base_prefix), Path(sys.executable).absolute().parent, *runner_roots()]
        if args.local_build:
            roots.extend(Path(root) for root in json.loads(args.local_build.read_bytes())["buildRoots"])
        if args.intel:
            roots.extend(read_roots(args.intel, intel_inputs(args.intel)))
        if args.archive.is_dir():
            check_app(args.archive, roots)
        else:
            check_archive(args.archive, roots)
    except (OSError, TypeError, KeyError, json.JSONDecodeError):
        parser.exit(1, "Could not verify native build locations\n")
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    print("Native archive build-path check passed")


if __name__ == "__main__":
    main()
