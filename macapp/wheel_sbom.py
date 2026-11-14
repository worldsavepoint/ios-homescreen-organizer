"""Keep prepared cryptography SBOM identifiers independent of the build directory."""
import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit
import zipfile

INFO = "cryptography-50.0.2.dist-info/"
SBOM = INFO + "sboms/cryptography-rust.cyclonedx.json"
RECORD = INFO + "RECORD"


def portable_cargo_sbom(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def relative(value):
        return (value and not PurePosixPath(value).is_absolute() and ".." not in PurePosixPath(value).parts
                and "\\" not in value and all(character.isprintable() for character in value))

    try:
        bom = json.loads(data, object_pairs_hook=unique)
        if bom["bomFormat"] != "CycloneDX" or bom["specVersion"] != "1.5":
            raise ValueError
        identifiers, mapping, components = set(), {}, []

        def collect(value):
            if isinstance(value, dict):
                if "bom-ref" in value:
                    identifier = value["bom-ref"]
                    if not isinstance(identifier, str) or not identifier or identifier in identifiers:
                        raise ValueError
                    identifiers.add(identifier)
                    components.append(value)
                    if identifier.startswith("path+file://"):
                        purl = value["purl"]
                        if not isinstance(purl, str) or not purl.startswith("pkg:cargo/"):
                            raise ValueError
                        url = urlsplit(purl)
                        download, = parse_qs(url.query, strict_parsing=True)["download_url"]
                        if (not download.startswith("file://") or not relative(download.removeprefix("file://"))
                                or url.fragment and not relative(unquote(url.fragment))):
                            raise ValueError
                        mapping[identifier] = purl
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(bom)
        if (not identifiers or "bom-ref" not in bom["metadata"]["component"]
                or len({mapping.get(identifier, identifier) for identifier in identifiers}) != len(identifiers)):
            raise ValueError
        for dependency in bom["dependencies"]:
            if dependency["ref"] not in identifiers or not isinstance(dependency.get("dependsOn", []), list):
                raise ValueError
            if any(identifier not in identifiers for identifier in dependency.get("dependsOn", [])):
                raise ValueError
        if not mapping:
            return data
        for component in components:
            component["bom-ref"] = mapping.get(component["bom-ref"], component["bom-ref"])
        for dependency in bom["dependencies"]:
            dependency["ref"] = mapping.get(dependency["ref"], dependency["ref"])
            if "dependsOn" in dependency:
                dependency["dependsOn"] = [mapping.get(identifier, identifier) for identifier in dependency["dependsOn"]]
        return (json.dumps(bom, indent=2, ensure_ascii=False) + "\n").encode()
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ValueError("Could not make the Cargo SBOM portable") from None


def record_hash(data):
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def rewrite_cryptography_wheel(wheel):
    try:
        if wheel.is_symlink() or not wheel.is_file():
            raise ValueError
        with zipfile.ZipFile(wheel) as archive:
            comment = archive.comment
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (len(names) != len(set(names)) or SBOM not in names or RECORD not in names
                    or any(name in names for name in (RECORD + ".jws", RECORD + ".p7s"))):
                raise ValueError
            for name in names:
                if (name.startswith("/") or "\\" in name or ".." in PurePosixPath(name).parts
                        or not all(character.isprintable() for character in name)):
                    raise ValueError
            files = {entry.filename: archive.read(entry) for entry in entries}
        rows = list(csv.reader(io.StringIO(files[RECORD].decode())))
        if (any(len(row) != 3 for row in rows) or len({row[0] for row in rows}) != len(rows)
                or {row[0] for row in rows} != {entry.filename for entry in entries if not entry.is_dir()}):
            raise ValueError
        for name, value, size in rows:
            if name == RECORD:
                if value or size:
                    raise ValueError
            elif value != record_hash(files[name]) or size != str(len(files[name])):
                raise ValueError
        portable = portable_cargo_sbom(files[SBOM])
        if portable == files[SBOM]:
            return
        files[SBOM] = portable
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        for name, _, _ in rows:
            writer.writerow((name, "", "") if name == RECORD else (name, record_hash(files[name]), len(files[name])))
        files[RECORD] = stream.getvalue().encode()
        with tempfile.NamedTemporaryFile(dir=wheel.parent, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with zipfile.ZipFile(temporary, "w") as archive:
                archive.comment = comment
                for entry in entries:
                    archive.writestr(entry, files[entry.filename])
            temporary.chmod(stat.S_IMODE(wheel.stat().st_mode))
            temporary.replace(wheel)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, KeyError, TypeError, ValueError, UnicodeError, zipfile.BadZipFile):
        raise ValueError("Could not verify the cryptography wheel and its SBOM") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    try:
        wheel = args.wheel
        if wheel.is_dir():
            wheel, = wheel.glob("cryptography-*.whl")
        rewrite_cryptography_wheel(wheel)
    except (OSError, ValueError):
        parser.exit(1, "Could not prepare the cryptography wheel and its SBOM\n")


if __name__ == "__main__":
    main()
