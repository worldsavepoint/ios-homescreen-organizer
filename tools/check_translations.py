#!/usr/bin/env python3
"""Validate manifest registration, source coverage and named placeholders."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

LOCALES = Path(__file__).resolve().parents[1] / "homescreen/web/locales"
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def product_display_name(code):
    language, _, rest = code.partition("-")
    if language == "zh":
        script = rest.partition("-")[0]
        if script == "Hans":
            return "屏幕整理"
        if script == "Hant":
            return "螢幕整理"
    return "Screen Organizer"


def read_json(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)


def canonical_locales(languages):
    values = set()
    for item in languages:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("code"), str):
            values.add(item["code"])
        if isinstance(item.get("aliases"), list):
            values.update(alias for alias in item["aliases"] if isinstance(alias, str))
    node = shutil.which("node")
    if node is None:
        raise ValueError("Node.js is required for browser-compatible locale validation")
    script = r'''
const values = JSON.parse(require("fs").readFileSync(0, "utf8"));
console.log(JSON.stringify(Object.fromEntries(values.map(value => {
  try { return [value, Intl.getCanonicalLocales(value.replace(/_/g, "-"))[0] || null]; }
  catch (_) { return [value, null]; }
}))));
'''
    try:
        result = subprocess.run([node, "-e", script], input=json.dumps(sorted(values)),
                                capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Node.js locale canonicalization failed") from error
    return json.loads(result.stdout)


def validate(folder):
    errors = []
    manifest = read_json(folder / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("default") != "en":
        return ["manifest default must be en"]
    languages = manifest.get("languages")
    if not isinstance(languages, list) or not languages:
        return ["manifest languages must be a nonempty list"]
    canonical = canonical_locales(languages)
    codes, aliases = set(), {}
    for item in languages:
        if not isinstance(item, dict):
            errors.append("language metadata must be an object")
            continue
        code = item.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)*", code):
            errors.append("language code must be a locale identifier")
            continue
        if code in codes:
            errors.append(f"duplicate language registration: {code}")
        if canonical.get(code) != code:
            errors.append(f"{code}: language code must use canonical form {canonical.get(code)}")
        codes.add(code)
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            errors.append(f"{code}: language name must be a nonempty string")
        values = item.get("aliases")
        if not isinstance(values, list) or not values:
            errors.append(f"{code}: aliases must be a nonempty list")
            continue
        for alias in [code, *values]:
            if not isinstance(alias, str) or not canonical.get(alias):
                errors.append(f"{code}: invalid language alias")
                continue
            normalized = canonical[alias]
            if normalized in aliases and aliases[normalized] != code:
                errors.append(f"{code}: canonical alias {alias} ({normalized}) already registered to {aliases[normalized]}")
            aliases[normalized] = code
    if "en" not in codes:
        errors.append("English source catalog must be registered")
    files = {path.stem for path in folder.glob("*.json") if path.name != "manifest.json"}
    for code in sorted(codes - files):
        errors.append(f"{code}: missing registered catalog")
    for code in sorted(files - codes):
        errors.append(f"{code}: unregistered catalog")
    source = read_json(folder / "en.json")
    if not isinstance(source, dict):
        return errors + ["English source catalog must be an object"]
    for code in sorted(codes & files):
        catalog = read_json(folder / f"{code}.json")
        if not isinstance(catalog, dict):
            errors.append(f"{code}: catalog must be an object")
            continue
        expected_name = product_display_name(code)
        if catalog.get("app.name") != expected_name:
            errors.append(f"{code}: app.name display name must be {expected_name}")
        if catalog.get("app.subtitle") != "for iOS":
            errors.append(f"{code}: app.subtitle compatibility subtitle must be for iOS")
        for key in sorted(source.keys() - catalog.keys()):
            errors.append(f"{code}: missing key {key}")
        for key in sorted(catalog.keys() - source.keys()):
            errors.append(f"{code}: extra key {key}")
        for key, value in catalog.items():
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{code}: {key} must be a nonempty string")
            elif key in source and isinstance(source[key], str):
                if set(PLACEHOLDER.findall(value)) != set(PLACEHOLDER.findall(source[key])):
                    errors.append(f"{code}: {key} placeholder mismatch")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--locales", type=Path, default=LOCALES, help="Catalog directory")
    args = parser.parse_args()
    try:
        errors = validate(args.locales)
    except (OSError, ValueError, TypeError) as error:
        errors = [str(error)]
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Translation catalogs are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
