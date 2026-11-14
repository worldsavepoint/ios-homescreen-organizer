"""Shared translations resolve languages, format values and validate contributions."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "homescreen/web/locales"


def test_catalogs_cover_supported_languages():
    assert (LOCALES / "manifest.json").is_file(), "Shared locale manifest is required"
    manifest = json.loads((LOCALES / "manifest.json").read_text())
    assert manifest["default"] == "en"
    assert {"en", "zh-Hans", "zh-Hant", "es", "de", "fr", "ru", "pt"} <= {
        item["code"] for item in manifest["languages"]
    }
    source = json.loads((LOCALES / "en.json").read_text())
    for language in manifest["languages"]:
        catalog = json.loads((LOCALES / (language["code"] + ".json")).read_text())
        assert catalog.keys() == source.keys()
        assert all(isinstance(value, str) and value.strip() for value in catalog.values())


def run_browser(body, setup=""):
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node unavailable")
    source = ROOT / "homescreen/web/i18n.js"
    assert source.is_file(), "Shared browser translator is required"
    harness = r'''
const fs = require("fs"), vm = require("vm"), assert = require("assert/strict");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const manifest = {default: "en", languages: [
 {code:"en",name:"English",aliases:["en"]},
 {code:"zh-Hans",name:"简体中文",aliases:["zh","zh-CN","zh-SG"]},
 {code:"zh-Hant",name:"繁體中文",aliases:["zh-TW","zh-HK","zh-MO"]},
 ...["es","de","fr","ru","pt"].map(code => ({code,name:code,aliases:[code]}))
]};
const catalogs = {
 "en": {hello:"Hello {name}", missing:"English fallback", html:"{name}", empty:"English empty"},
 "de": {hello:"Hallo {name}", html:"{name}", empty:""},
 "fr": {hello:"Bonjour {name}"}
};
const stored = new Map(), calls = [];
const context = {URL, Intl, Date, console,
 navigator:{languages:["de-DE"]},
 localStorage:{getItem:key=>stored.get(key) || null, setItem:(key,value)=>stored.set(key,value)},
 fetch:async (url,opts)=>{ const file = String(url).split("/").pop(); calls.push(file);
   assert.equal(opts.method,undefined);assert.equal(url,"/static/locales/"+file);
   assert.ok(file==="manifest.json" || manifest.languages.some(l=>l.code+".json"===file));
   return {ok:true,json:async()=>file === "manifest.json" ? manifest : (catalogs[file.slice(0,-5)] || {})}; }
};
context.window = context;
eval(input.setup);
vm.createContext(context); vm.runInContext(input.source, context);
(async()=>{ const i18n = context.ScreenOrganizerI18n; await i18n.ready;
''' + body + r'''
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([executable, "-e", harness], input=json.dumps({"source": source.read_text(), "setup": setup}),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_locale_resolution_uses_script_region_and_ordered_preferences():
    run_browser('''
for (const [request, expected] of [["zh-Hant-HK","zh-Hant"],["zh-CN","zh-Hans"],
 ["zh_TW","zh-Hant"],["ZH-hans-cn","zh-Hans"],["zh-SG","zh-Hans"],
 ["pt-BR","pt"],["ru-RU","ru"],["zz-ZZ","en"]]) {
 assert.equal(i18n.resolveLocale(request, []), expected, request);
}
assert.equal(i18n.resolveLocale("system", ["zz-ZZ","fr-CA","de"]), "fr");
assert.equal(i18n.resolveLocale("system", ["zh-Hant-HK","en"]), "zh-Hant");
assert.equal(i18n.resolveLocale("system", ["zz-ZZ"]), "en");
assert.equal(i18n.locale, "de");
''')


def test_catalog_fallback_interpolation_and_formatters():
    run_browser('''
assert.equal(i18n.t("hello", {name:"Synthetic Folder"}), "Hallo Synthetic Folder");
assert.equal(i18n.t("missing"), "English fallback");
assert.equal(i18n.t("unknown"), "unknown");
assert.equal(i18n.t("empty"), "");
assert.equal(i18n.t("hello"), "Hallo {name}");
assert.equal(i18n.t("html", {name:"<b>data</b> $&"}), "<b>data</b> $&");
assert.equal(i18n.formatNumber(1234.5), "1.234,5");
assert.equal(i18n.formatDate(new Date(2026, 0, 2)), "2.1.2026");
assert.ok(calls.every(file => /^(manifest|en|de)\\.json$/.test(file)));
''')


def test_language_changes_persist_and_notify_after_catalog_ready():
    run_browser('''
const observed = [];
const unsubscribe = i18n.onChange(() => observed.push([i18n.locale, i18n.t("hello",{name:"X"})]));
await i18n.setLocale("fr");
assert.deepEqual(observed, [["fr", "Bonjour X"]]);
assert.equal(i18n.preference, "fr");
assert.ok([...stored.values()].includes("fr"));
unsubscribe();
await i18n.setLocale("en");
assert.equal(observed.length, 1);
await i18n.setLocale("system");
assert.equal(i18n.preference, "system");
assert.equal(i18n.locale, "de");
''')


def test_saved_language_preference_is_restored_on_startup():
    run_browser('''
assert.equal(i18n.preference, "fr");
assert.equal(i18n.locale, "fr");
assert.equal(i18n.t("hello", {name:"X"}), "Bonjour X");
''', 'stored.set("screenOrganizer.language", "fr");')


def test_translation_works_when_preference_storage_is_unavailable():
    run_browser('''
await i18n.setLocale("fr");
assert.equal(i18n.locale, "fr");
assert.equal(i18n.t("hello", {name:"X"}), "Bonjour X");
''', '''context.localStorage = {
getItem(){throw new Error("blocked");}, setItem(){throw new Error("blocked");}};''')


def test_dom_translation_uses_text_and_declared_attributes():
    run_browser('''
const make = attributes => ({attributes, textContent:"", matches:()=>true,
 getAttribute(name){return this.attributes[name] ?? null;},
 setAttribute(name,value){this.attributes[name]=value;}, querySelectorAll:()=>[]});
const root = make({"data-i18n":"missing", "data-i18n-title":"hello",
 "data-i18n-placeholder":"missing", "data-i18n-aria-label":"missing"});
i18n.translateDOM(root);
assert.equal(root.textContent,"English fallback");
assert.equal(root.attributes.title,"Hallo {name}");
assert.equal(root.attributes.placeholder,"English fallback");
assert.equal(root.attributes["aria-label"],"English fallback");
''')


def write_catalogs(folder, translation):
    folder.mkdir(exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps({"default":"en", "languages":[
        {"code":"en","name":"English","aliases":["en"]},
        {"code":"de","name":"Deutsch","aliases":["de"]}]}))
    identity = {"app.name":"Screen Organizer", "app.subtitle":"for iOS"}
    (folder / "en.json").write_text(json.dumps({**identity, "greeting":"Hello {name}"}))
    (folder / "de.json").write_text(json.dumps({**identity, **translation}))


@pytest.mark.parametrize("translation,diagnostic", [
    ({"greeting":"Hallo {person}"}, "placeholder"),
    ({"greeting":7}, "string"),
    ({}, "missing"),
    ({"greeting":"Hallo {name}","extra":"x"}, "extra"),
])
def test_validator_rejects_incomplete_or_invalid_catalogs(tmp_path, translation, diagnostic):
    checker = ROOT / "tools/check_translations.py"
    assert checker.is_file(), "Translation validation command is required"
    write_catalogs(tmp_path, translation)
    result = subprocess.run([sys.executable, str(checker), "--locales", str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert diagnostic in result.stderr.lower()


def test_validator_checks_registration_and_accepts_valid_catalogs(tmp_path):
    checker = ROOT / "tools/check_translations.py"
    assert checker.is_file(), "Translation validation command is required"
    write_catalogs(tmp_path, {"greeting":"Hallo {name}"})
    command = [sys.executable, str(checker), "--locales", str(tmp_path)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    (tmp_path / "fr.json").write_text('{}')
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert "unregistered" in result.stderr.lower()


@pytest.mark.parametrize("key,value,diagnostic", [
    ("app.subtitle", "für iOS", "subtitle"),
    ("app.name", "Organizer", "display name"),
])
def test_validator_enforces_product_identity(tmp_path, key, value, diagnostic):
    write_catalogs(tmp_path, {"greeting":"Hallo {name}", key:value})
    result = subprocess.run([sys.executable, str(ROOT / "tools/check_translations.py"),
                             "--locales", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert diagnostic in result.stderr.lower()


@pytest.mark.parametrize("registrations", [
    [("he", ["he"]), ("iw", ["iw"])],
    [("he", ["he"]), ("id", ["id", "iw"])],
])
def test_validator_rejects_canonical_registration_collisions(tmp_path, registrations):
    write_catalogs(tmp_path, {"greeting":"Hallo {name}"})
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for code, aliases in registrations:
        manifest["languages"].append({"code":code, "name":code, "aliases":aliases})
        (tmp_path / f"{code}.json").write_text((tmp_path / "en.json").read_text())
    manifest_path.write_text(json.dumps(manifest))
    result = subprocess.run([sys.executable, str(ROOT / "tools/check_translations.py"),
                             "--locales", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "canonical" in result.stderr.lower()


def test_canonical_legacy_aliases_select_distinct_registered_catalogs(tmp_path):
    write_catalogs(tmp_path, {"greeting":"Hallo {name}"})
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for code, alias in [("he", "iw"), ("id", "in")]:
        manifest["languages"].append({"code":code, "name":code, "aliases":[code, alias]})
        (tmp_path / f"{code}.json").write_text((tmp_path / "en.json").read_text())
    manifest_path.write_text(json.dumps(manifest))
    result = subprocess.run([sys.executable, str(ROOT / "tools/check_translations.py"),
                             "--locales", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    run_browser('''
assert.equal(i18n.resolveLocale("iw-IL", []), "he");
assert.equal(i18n.resolveLocale("in-ID", []), "id");
await i18n.setLocale("he");
assert.equal(i18n.locale, "he");
assert.equal(i18n.t("hello", {name:"X"}), "Hebrew X");
await i18n.setLocale("id");
assert.equal(i18n.locale, "id");
assert.equal(i18n.t("hello", {name:"X"}), "Indonesian X");
''', '''manifest.languages.push(
 {code:"he",name:"Hebrew",aliases:["he","iw"]},
 {code:"id",name:"Indonesian",aliases:["id","in"]});
catalogs.he = {hello:"Hebrew {name}"}; catalogs.id = {hello:"Indonesian {name}"};''')


def test_translation_command_accepts_shipped_catalogs():
    checker = ROOT / "tools/check_translations.py"
    assert checker.is_file(), "Translation validation command is required"
    result = subprocess.run([sys.executable, str(checker)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_unavailable_translation_uses_english_and_can_retry():
    run_browser('''
const originalFetch = context.fetch;
context.fetch = async url => String(url).endsWith("fr.json") ? {ok:false} : originalFetch(url);
await i18n.setLocale("fr");
assert.equal(i18n.t("hello", {name:"X"}), "Hello X");
context.fetch = originalFetch;
await i18n.setLocale("fr");
assert.equal(i18n.t("hello", {name:"X"}), "Bonjour X");
''')


def test_latest_language_selection_wins_catalog_loading_race():
    run_browser('''
const originalFetch = context.fetch;
let release;
context.fetch = url => String(url).endsWith("fr.json")
 ? new Promise(resolve => {release = () => resolve({ok:true,json:async()=>catalogs.fr});})
 : originalFetch(url);
const first = i18n.setLocale("fr");
await new Promise(resolve => setImmediate(resolve));
await i18n.setLocale("en");
release(); await first;
assert.equal(i18n.locale, "en");
assert.equal(i18n.preference, "en");
assert.equal(i18n.t("hello", {name:"X"}), "Hello X");
''')


@pytest.mark.parametrize("mutation,diagnostic", [
    (lambda path: (path / "de.json").unlink(), "missing registered"),
    (lambda path: (path / "de.json").write_text('{"greeting":"A","greeting":"B"}'), "duplicate"),
    (lambda path: (path / "manifest.json").write_text(json.dumps({
        "default":"en", "languages":[
            {"code":"en","name":"English","aliases":["en"]},
            {"code":"de","name":"Deutsch","aliases":["en"]}]})), "alias"),
])
def test_validator_rejects_registration_and_duplicate_key_errors(tmp_path, mutation, diagnostic):
    write_catalogs(tmp_path, {"greeting":"Hallo {name}"})
    mutation(tmp_path)
    result = subprocess.run([sys.executable, str(ROOT / "tools/check_translations.py"),
                             "--locales", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert diagnostic in result.stderr.lower()


def test_package_data_includes_registered_catalogs():
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = config["tool"]["setuptools"]["package-data"]["homescreen"]
    packaged = {path.relative_to(ROOT / "homescreen").as_posix()
                for pattern in patterns for path in (ROOT / "homescreen").glob(pattern)
                if path.is_file()}
    manifest = json.loads((LOCALES / "manifest.json").read_text())
    for name in ["manifest", *(item["code"] for item in manifest["languages"])]:
        resource = f"web/locales/{name}.json"
        assert resource in packaged


def test_registered_regional_catalog_aliases_and_saved_owner():
    run_browser('''
assert.equal(i18n.preference, 'zh-Hant');
for (const [value, expected] of [['fra','fr'],['fre','fr'],['spa','es'],['zh_TW','zh-Hant'],
 ['zh-Hans-TW','zh-Hans'],['zh-Hant-CN','zh-Hant'],['zh-Hant-HK','zh-Hant-HK'],
 ['gsw_CH','de'],['de-1901','de-1901'],['sh','sr-Latn'],['invalid_tag$','system']]) {
 await i18n.setLocale(value);
 assert.equal(i18n.preference, expected, value);
 if (expected==='zh-Hant-HK') assert.equal(i18n.t('hello'),'Regional');
}
''', '''
stored.set('screenOrganizer.language','zh_TW');
manifest.languages.find(l=>l.code==='de').aliases.push('gsw_CH');
for(const code of ['zh-Hant-HK','de-1901','sr-Latn']) manifest.languages.push({code,name:code,aliases:[]});
catalogs['zh-Hant-HK']={hello:'Regional'};
''')


def test_regional_alias_and_unicode_extensions_precede_script_fallback():
    run_browser('''
for(const value of ['zh-Hant-HK','zh-Hant-MO','zh-Hant-HK-u-nu-hanidec','zh-Hant-MO-u-nu-hanidec'])
 assert.equal(i18n.resolveLocale(value,[]),'zh-Hant-HK',value);
for(const [value,want] of [['zh-Hant-TW','zh-Hant'],['zh-Hans-TW','zh-Hans'],['zh-Hant-CN','zh-Hant'],['zh_TW','zh-Hant'],['gsw-CH-u-nu-latn','de']])
 assert.equal(i18n.resolveLocale(value,[]),want,value);
''', '''
manifest.languages.push({code:'zh-Hant-HK',name:'Regional',aliases:['zh-Hant-MO']});
manifest.languages.find(l=>l.code==='de').aliases.push('gsw-CH');
''')


def test_native_system_locale_uses_host_resolution_and_refreshes_without_preference_change():
    run_browser('''
assert.equal(i18n.preference,'system');assert.equal(i18n.locale,'de');
await i18n.setLocale('system','native','fr');
assert.equal(i18n.preference,'system');assert.equal(i18n.locale,'fr');
assert.equal(stored.get('screenOrganizer.language'),'system');
await i18n.setLocale('de','picker');await i18n.setLocale('system','native','fr');
assert.equal(i18n.preference,'system');assert.equal(i18n.locale,'fr');
''', '''
context.navigator.languages=['ja-JP'];
context.screenOrganizerHost={native:true,languageSync:'native-origin',languagePreference:'system',languageLocale:'de'};
''')


def test_delayed_native_system_catalog_does_not_override_newer_picker():
    run_browser('''
let release;const original=context.fetch;
context.fetch=url=>url.endsWith('/fr.json')?new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>catalogs.fr});}):original(url);
const old=i18n.setLocale('system','native','fr');await new Promise(resolve=>setImmediate(resolve));
assert.equal(typeof release,'function','native System resolution starts the French catalog');
await i18n.setLocale('en','picker');release();await old;
assert.equal(i18n.preference,'en');assert.equal(i18n.locale,'en');
''', '''context.screenOrganizerHost={native:true,languageSync:'native-origin',languagePreference:'system',languageLocale:'de'};''')
