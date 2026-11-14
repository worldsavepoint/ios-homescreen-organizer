import io
import sys
import sysconfig
import zipfile
from pathlib import Path

import pytest


def test_build_configuration_relocates_complete_path_components(tmp_path, monkeypatch):
    from macapp.frozen_engine import replace_sysconfig

    root = "/build roots/python"
    name = sysconfig._get_sysconfigdata_name()
    values = {"prefix": root, "list": f"{root}/lib {root}", "flags": f"-I{root}/inc",
              "sibling": root + "-tools", "other": "/parent" + root + "/lib",
              "literal": "__PYTHON_PREFIX__", "number": 42, "vendor_flags": "/vendor-I" + root + " /vendor-F" + root + " /vendor-L" + root}
    entries = [("another", "another.py", "PYMODULE"), (name, "original.py", "PYMODULE")]
    cache = {"another": object(), name: object()}
    replace_sysconfig(entries, cache, tmp_path, name, values, root)
    assert list(cache) == ["another"]
    assert entries[0] == ("another", "another.py", "PYMODULE")
    assert entries[1] == (name, str(tmp_path / (name + ".py")), "PYMODULE")
    new = "/runtime path"
    monkeypatch.setattr(sys, "prefix", new)
    scope = {}
    exec(Path(entries[1][1]).read_text(), scope)
    assert scope["build_time_vars"] == {**values, "prefix": new, "list": new + "/lib " + new,
                                        "flags": "-I" + new + "/inc"}


def native_zip(tmp_path, *, config="clean", extra=b"clean", nested=b"clean"):
    from PyInstaller.archive.writers import CArchiveWriter, ZlibArchiveWriter

    name = sysconfig._get_sysconfigdata_name()
    pyz = tmp_path / "modules.pyz"
    ZlibArchiveWriter(str(pyz), [(name, name + ".py", "PYMODULE")],
                      {name: compile("value = " + repr(config), name + ".py", "exec")})
    engine = tmp_path / "engine"
    CArchiveWriter(str(engine), [("archive.pyz", str(pyz), False, "z")], "Python")
    nested_zip = io.BytesIO()
    with zipfile.ZipFile(nested_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("fixture", nested)
    app = tmp_path / "app.zip"
    with zipfile.ZipFile(app, "w", zipfile.ZIP_DEFLATED) as archive:
        base = "Screen Organizer.app/Contents/"
        archive.writestr(base + "MacOS/HomeScreenOrganizer", extra)
        archive.write(engine, base + "Resources/engine/engine")
        archive.writestr(base + "Resources/engine/_internal/base_library.zip", nested_zip.getvalue())
    return app


@pytest.mark.parametrize("location", ["config", "extra", "nested"])
def test_compiled_and_nested_artifact_paths(tmp_path, location):
    pytest.importorskip("PyInstaller")
    from macapp.check_package_paths import check_archive

    marker = "/private/build-fixture"
    path = native_zip(tmp_path, **{location: marker if location == "config" else marker.encode()})
    if location == "config":
        assert marker.encode() not in (tmp_path / "engine").read_bytes()
    with pytest.raises(ValueError, match="build path") as error:
        check_archive(path, [Path(marker)])
    assert marker not in str(error.value)


def test_unrelated_runner_builds_are_allowed(tmp_path):
    pytest.importorskip("PyInstaller")
    from macapp.check_package_paths import check_archive

    archive = native_zip(tmp_path, config="/Users/runner/work/dependency/src")
    check_archive(archive, [Path("/Users/runner/work/app")])


@pytest.mark.parametrize("document", ["home_screen_organizer-1.3.0.dist-info/METADATA",
                                      "home_screen_organizer.egg-info/PKG-INFO"])
def test_analysis_collects_project_version_document(document):
    from macapp.frozen_engine import project_metadata

    dependency = "tool-2.0.dist-info/entry_points.txt"
    files = [document, "home_screen_organizer-1.3.0.dist-info/direct_url.json",
             "home_screen_organizer.egg-info/SOURCES.txt", dependency,
             "home_screen_organizer_plugin-2.0.dist-info/RECORD", "homescreen/web/index.html"]
    data = [(name, "source", "DATA") for name in files]
    assert project_metadata(data, document) == [data[i] for i in (0, 3, 4, 5)]


@pytest.mark.parametrize("kind", ["archive-comment", "member-comment", "extra", "local-extra", "wheel", "extensionless"])
def test_zip_metadata_and_nested_containers(tmp_path, kind):
    import struct
    from macapp.check_package_paths import check_archive

    root = tmp_path / "build-root"
    path = native_zip(tmp_path)
    marker = bytes(root)
    info = zipfile.ZipInfo("entry")
    with zipfile.ZipFile(path, "a") as archive:
        if kind == "archive-comment":
            archive.comment = marker
        elif kind == "member-comment":
            info.comment = marker
        elif kind in ("extra", "local-extra"):
            info.extra = struct.pack("<HH", 4567, len(marker)) + marker
        else:
            nested = io.BytesIO()
            with zipfile.ZipFile(nested, "w", zipfile.ZIP_DEFLATED) as wheel:
                wheel.writestr("data", marker)
            archive.writestr("data.whl" if kind == "wheel" else "data", nested.getvalue())
        archive.writestr(info, "metadata")
    if kind == "local-extra":
        before, after = path.read_bytes().rsplit(marker, 1)
        path.write_bytes(before + b"x" * len(marker) + after)
    with pytest.raises(ValueError, match="build path"):
        check_archive(path, [root])


def test_zip_named_link_and_prefix_siblings(tmp_path):
    import stat
    from macapp.check_package_paths import check_archive

    root = tmp_path / "app"
    path = native_zip(tmp_path, config=str(root) + "-other/lib")
    with zipfile.ZipFile(path, "a") as archive:
        link = zipfile.ZipInfo("stdlib.zip")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "base_library.zip")
    check_archive(path, [root])


@pytest.mark.parametrize("hosted, clean", [("github-hosted", True), ("self-hosted", False), ("", False)])
def test_archive_home_by_runner_type(tmp_path, monkeypatch, hosted, clean):
    import subprocess
    from macapp import check_package_paths as scanner

    folder = tmp_path / "personal"
    archive = native_zip(tmp_path, config=str(folder / "another-place"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", hosted)
    monkeypatch.setenv("HOME", str(folder))
    result = subprocess.run([sys.executable, scanner.__file__, str(archive), str(tmp_path / "checkout")],
                            capture_output=True)
    assert (result.returncode == 0) == clean
    assert bytes(folder) not in result.stderr


@pytest.mark.parametrize("missing", [True, False])
def test_malformed_archive_diagnostic(tmp_path, missing):
    import subprocess
    from macapp import check_package_paths as scanner

    path = native_zip(tmp_path)
    with zipfile.ZipFile(path, "w" if missing else "a") as archive:
        archive.writestr("bad.zip", b"wrong")
    result = subprocess.run([sys.executable, scanner.__file__, str(path), str(tmp_path)], capture_output=True)
    assert result.returncode != 0
    assert result.stderr and b"Traceback" not in result.stderr
    assert bytes(tmp_path) not in result.stderr


@pytest.mark.parametrize("format", ["bootstrap", "pyc", "xml", "binary", "xml-data"])
@pytest.mark.parametrize("suffix", ["", "/src", "-other"])
def test_serialized_root_values(tmp_path, format, suffix):
    import plistlib
    import py_compile
    from PyInstaller.archive.writers import CArchiveWriter
    from macapp.check_package_paths import ENGINE, check_archive

    root = tmp_path / "private"
    value = str(root) + suffix
    path = native_zip(tmp_path)
    source = tmp_path / "fixture.py"
    source.write_text(f"def nested(): return ({value!r}, 'next')\n")
    if format == "bootstrap":
        engine = tmp_path / "engine"
        CArchiveWriter(str(engine), [("archive.pyz", str(tmp_path / "modules.pyz"), False, "z"),
                                     ("bootstrap", str(source), True, "s")], "Python")
        with zipfile.ZipFile(path, "w") as archive:
            archive.write(engine, ENGINE)
    elif format == "pyc":
        pyc = tmp_path / "module.pyc"
        py_compile.compile(str(source), str(pyc), "fixture.py", doraise=True)
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w", zipfile.ZIP_DEFLATED) as library:
            library.write(pyc, "module.pyc")
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("library.zip", nested.getvalue())
    else:
        payload = [{"item": value.encode() if format == "xml-data" else value}]
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("Info.plist", plistlib.dumps(payload, fmt=plistlib.FMT_BINARY
                                                          if format == "binary" else plistlib.FMT_XML))
    if suffix == "-other":
        check_archive(path, [root])
    else:
        with pytest.raises(ValueError, match="build path"):
            check_archive(path, [root])


@pytest.mark.parametrize("prefix,encoding", [
    ('<?xml version="1.0"?>', "utf-8-sig"),
    ("", "utf-8"),
    ('<?xml version="1.0"?>' + "<!--" + "x" * 1500 + "-->", "utf-8"),
    (" \n<!--localization--><?build fixture?>", "utf-8"),
    ("<!--localization-->", "utf-16"),
])
@pytest.mark.parametrize("suffix", ["", "/child", "-sibling"])
def test_localized_xml_plist_data(tmp_path, prefix, encoding, suffix):
    import base64
    from macapp.check_package_paths import check_archive

    root = tmp_path / "build-root"
    data = base64.b64encode((str(root) + suffix).encode()).decode()
    xml = f'{prefix}<plist version="1.0"><dict><key>source</key><data>{data}</data></dict></plist>'
    packaged = native_zip(tmp_path)
    with zipfile.ZipFile(packaged, "a") as archive:
        archive.writestr("Screen Organizer.app/Contents/Resources/en.lproj/InfoPlist.strings", xml.encode(encoding))
        archive.writestr("Localizable.strings", '"label" = "Open";\n')
        archive.writestr("document.xml", '<?xml version="1.0"?><document><!-- <plist -->Hello</document>')
    if suffix == "-sibling":
        check_archive(packaged, [root])
    else:
        with pytest.raises(ValueError, match="build path"):
            check_archive(packaged, [root])


@pytest.mark.parametrize("kind", ["utf32-le", "utf32-be", "table-le", "table-be"])
@pytest.mark.parametrize("suffix", ["", "/file", "-sibling"])
def test_wide_native_localization(tmp_path, kind, suffix):
    import base64
    import codecs
    import plistlib
    import subprocess
    from macapp.check_package_paths import check_archive

    root = tmp_path / "private-root"
    value = str(root) + suffix
    if kind.startswith("table"):
        escaped = value.replace("/", r"\U002f")
        text = '"label" = "' + escaped + '";'
        little = kind.endswith("le")
        raw = (codecs.BOM_UTF16_LE if little else codecs.BOM_UTF16_BE) + text.encode("utf-16-le" if little else "utf-16-be")
        expected = value
    else:
        little = kind.endswith("le")
        encoded = base64.b64encode(value.encode()).decode()
        text = ('<?xml version="1.0" encoding="UTF-32' + ("LE" if little else "BE") + '"?>'
                '<!-- resource --><plist version="1.0"><dict><key>label</key><data>' + encoded + '</data></dict></plist>')
        raw = (codecs.BOM_UTF32_LE if little else codecs.BOM_UTF32_BE) + text.encode("utf-32-le" if little else "utf-32-be")
        expected = value.encode()
    logical = subprocess.run(["/usr/bin/plutil", "-convert", "binary1", "-o", "-", "--", "-"],
                             input=raw, check=True, capture_output=True)
    assert plistlib.loads(logical.stdout)["label"] == expected
    path = native_zip(tmp_path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("Screen Organizer.app/Contents/Resources/en.lproj/InfoPlist.strings", raw)
    if suffix == "-sibling":
        check_archive(path, [root])
    else:
        with pytest.raises(ValueError, match="build path"):
            check_archive(path, [root])


def test_wide_plist_at_unlabeled_path_and_embedded_entity(tmp_path):
    import base64
    import codecs
    import plistlib
    import subprocess
    from macapp.check_package_paths import check_archive
    root = tmp_path / "private-fixture"
    encoded = base64.b64encode(str(root).encode()).decode()
    xml = f"<!--fixture--><plist><dict><key>x</key><data>{encoded}</data></dict></plist>"
    path = native_zip(tmp_path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("assets/translation", codecs.BOM_UTF32_BE + xml.encode("utf-32-be"))
    with pytest.raises(ValueError, match="build path"):
        check_archive(path, [root])
    canary = tmp_path / "temporary-canary"
    canary.write_text("fixture-only external entity")
    declaration = ('<!DOCTYPE plist [<!ENTITY external SYSTEM "' + canary.as_uri()
                   + '">]><plist><dict><key>v</key><string>&external;</string></dict></plist>')
    result = subprocess.run(["/usr/bin/plutil", "-convert", "binary1", "-o", "-", "--", "-"],
                            input=declaration.encode(), capture_output=True, timeout=5)
    assert result.returncode or plistlib.loads(result.stdout)["v"] != canary.read_text()


@pytest.mark.parametrize("field,data", [
    (0x5855, b"\0" * 12), (0x7855, b"\0" * 4), (0x7875, b"\x01\x04\x01\0\0\0\x04\x02\0\0\0"),
    (0x000D, b"\0" * 12), (0x756E, b"\0" * 14),
])
@pytest.mark.parametrize("location", ["both", "local-only"])
def test_native_zip_rejects_numeric_ownership_in_both_header_sets(tmp_path, field, data, location):
    import struct
    from macapp.check_package_paths import check_archive

    path = native_zip(tmp_path)
    info = zipfile.ZipInfo("entry")
    extra = struct.pack("<HH", field, len(data)) + data
    info.extra = extra
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(info, b"ordinary bytes")
    if location == "local-only":
        raw = path.read_bytes()
        before, after = raw.rsplit(extra, 1)
        # Unknown padding leaves the central header free of owner-bearing records.
        path.write_bytes(before + struct.pack("<HH", 0x9999, len(data)) + b"\0" * len(data) + after)
    with pytest.raises(ValueError, match="ownership"):
        check_archive(path, [tmp_path / "unrelated"])


@pytest.mark.parametrize("name", ["__MACOSX/._Screen Organizer.app", "Screen Organizer.app/Contents/._payload"])
def test_native_zip_rejects_appledouble_metadata(tmp_path, name):
    from macapp.check_package_paths import check_archive
    path = native_zip(tmp_path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(name, b"unrelated provenance origin")
    with pytest.raises(ValueError, match="metadata"):
        check_archive(path, [tmp_path / "unrelated"])
