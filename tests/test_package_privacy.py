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


@pytest.mark.parametrize("location", ["config", "extra", "nested"])
def test_complete_local_app_uses_the_compressed_archive_path_reader(tmp_path, location):
    from macapp.check_package_paths import check_app
    marker = tmp_path / "recipient source"
    path = native_zip(tmp_path, **{location: str(marker) if location == "config" else bytes(marker)})
    if location == "config":
        assert bytes(marker) not in (tmp_path / "engine").read_bytes()
    with zipfile.ZipFile(path) as archive:
        archive.extractall(tmp_path / "local-output")
    app = tmp_path / "local-output/Screen Organizer.app"
    with pytest.raises(ValueError, match="build path") as error:
        check_app(app, [marker])
    assert str(tmp_path) not in str(error.value)
    with pytest.raises(ValueError, match="build path"):
        check_app(app, [marker.resolve()])


def test_complete_local_app_allows_an_unrelated_prefix_sibling(tmp_path):
    from macapp.check_package_paths import check_app
    root = tmp_path / "recipient"
    path = native_zip(tmp_path, config=str(root) + "-other/lib")
    with zipfile.ZipFile(path) as archive:
        archive.extractall(tmp_path / "local-output")
    check_app(tmp_path / "local-output/Screen Organizer.app", [root])


@pytest.mark.parametrize("kind", ["native", "nested", "pyz", "comment", "raw", "raw-invalid"])
def test_privacy_failure_reports_safe_location_without_losing_raw_findings(tmp_path, kind):
    from macapp.check_package_paths import BuildPathError, check_archive
    root = tmp_path / "private-marker"
    path = native_zip(tmp_path, config=str(root) if kind == "pyz" else "clean",
                      nested=bytes(root) if kind == "nested" else b"clean")
    if kind == "native":
        with zipfile.ZipFile(path, "a", zipfile.ZIP_STORED) as archive:
            archive.writestr("Screen Organizer.app/Contents/Resources/engine/_internal/component.so", bytes(root))
    elif kind == "comment":
        with zipfile.ZipFile(path, "a") as archive:
            archive.comment = bytes(root)
    elif kind == "raw":
        path.write_bytes(path.read_bytes() + bytes(root))
    elif kind == "raw-invalid":
        path.write_bytes(b"invalid archive " + bytes(root))
    with pytest.raises(BuildPathError) as caught:
        check_archive(path, [root])
    assert str(caught.value) == "Release payload contains a local build path"
    note, = caught.value.__notes__
    expected = {"native": "ZIP!Screen Organizer.app/Contents/Resources/engine/_internal/component.so",
                "nested": "base_library.zip!fixture", "pyz": "PYZ!" + sysconfig._get_sysconfigdata_name(),
                "comment": "ZIP metadata", "raw": "archive bytes", "raw-invalid": "archive bytes"}[kind]
    assert expected in note and "path rule: recorded-build-root" in note
    assert "private-marker" not in note and str(tmp_path) not in note


@pytest.mark.parametrize("name", ["/private-marker/absolute", "Screen Organizer.app/../escape",
                                 "Screen Organizer.app/control\n::error::value", "Screen Organizer.app/秘密"])
def test_privacy_attribution_redacts_untrusted_member_labels(tmp_path, name):
    from macapp.check_package_paths import BuildPathError, check_archive
    root = tmp_path / "private-marker"
    path = native_zip(tmp_path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(name, bytes(root))
    with pytest.raises(BuildPathError) as caught:
        check_archive(path, [root])
    note, = caught.value.__notes__
    assert "<redacted member>" in note
    assert name not in note and "::error::" not in note and "private-marker" not in note
    assert all(character.isprintable() for character in note)


@pytest.mark.parametrize("rule", ["builder-home", "runner-temp", "interpreter-prefix"])
def test_privacy_attribution_names_the_rule_without_its_prefix(tmp_path, monkeypatch, rule):
    from macapp.check_package_paths import BuildPathError, check_archive
    root = tmp_path / "private-marker"
    if rule == "builder-home":
        monkeypatch.setenv("HOME", str(root))
    elif rule == "runner-temp":
        monkeypatch.setenv("RUNNER_TEMP", str(root))
    else:
        monkeypatch.setattr(sys, "prefix", str(root))
    with pytest.raises(BuildPathError) as caught:
        check_archive(native_zip(tmp_path, extra=bytes(root)), [root])
    note, = caught.value.__notes__
    assert "path rule: " + rule in note and str(root) not in note


def test_deferred_privacy_failure_suppresses_untrusted_zip_parser_context(tmp_path):
    import traceback
    from macapp.check_package_paths import BuildPathError, check_archive
    root = tmp_path / "blocked-root"
    central = "safe-" + "x" * 59
    private = "/private/owned-review-account/data".ljust(len(central), "x")
    path = tmp_path / "malformed.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(central, b"clean")
    path.write_bytes(path.read_bytes().replace(central.encode(), private.encode(), 1) + bytes(root))
    with pytest.raises(BuildPathError) as caught:
        check_archive(path, [root], engine=None)
    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__suppress_context__ is True
    assert private not in rendered and "BadZipFile:" not in rendered
    assert str(caught.value) == "Release payload contains a local build path"
    assert caught.value.__notes__ == ["Package location: archive bytes; path rule: recorded-build-root"]


def test_nested_privacy_note_is_bounded_keeps_leaf_and_retains_one_fallback(tmp_path, monkeypatch):
    from macapp import check_package_paths as scanner
    original = scanner.BuildPathError
    allocations = []
    class CountedError(original):
        def __init__(self, *args):
            allocations.append(1)
            super().__init__(*args)
    monkeypatch.setattr(scanner, "BuildPathError", CountedError)
    root = tmp_path / "blocked-root"
    def packed(name, data):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(name, data)
        return stream.getvalue()
    data = packed("useful-leaf.txt", bytes(root) + b"/child\0")
    for depth in range(12):
        data = packed(f"level{depth}-" + "a" * 440 + ".zip", data)
    path = tmp_path / "nested.zip"
    path.write_bytes(data)
    with pytest.raises(original) as caught:
        scanner.check_archive(path, [root], engine=None)
    note, = caught.value.__notes__
    assert (len(note) <= 1024, len(allocations)) == (True, 2)
    assert "<truncated>" in note
    assert "!useful-leaf.txt; path rule: recorded-build-root" in note
    assert str(root) not in note and all(character.isprintable() for character in note)
