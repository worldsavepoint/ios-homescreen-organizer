"""Native launch and bridge behavior using isolated catalogs and synthetic children."""
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "macapp/Shell/Sources/Homescreen"


@pytest.fixture(scope="module")
def native_binary(tmp_path_factory):
    if shutil.which("swiftc") is None:
        pytest.skip("Swift compiler unavailable")
    for name in ("Localization.swift", "ConsentView.swift", "MCPSetupView.swift"):
        assert (SOURCES / name).is_file(), f"Native onboarding requires {name}"
    folder = tmp_path_factory.mktemp("native-onboarding")
    # App.swift supplies AppMark and WebView. Rename only its entry-point annotation.
    app = folder / "App.swift"
    app.write_text((SOURCES / "App.swift").read_text().replace("@main\n", ""))
    menu = re.search(r'Menu\("MCP"\) \{(.*?)\n\s*\}.disabled', app.read_text(), re.S).group(1)
    menu_binding = menu.strip().splitlines()[0]
    menu_actions = re.findall(r'Button\(localization.t\("[^"]+"\)\) \{ (.*?) \}', menu)
    assert len(menu_actions) == 5
    menu_cases = []
    for action in menu_actions:
        format_name = re.search(r'format: "([^"]+)"', action).group(1)
        menu_cases.append(f'case "{format_name}": ' + action.replace(f'format: "{format_name}"', f'format: "{format_name}", to: board'))
    menu_harness = menu_binding + '\nswitch format {\n' + '\n'.join(menu_cases) + '\ndefault: fatalError("format")\n}'
    panel_source = (SOURCES / "MCPSetupView.swift").read_text()
    panel_action = re.search(r'Button\(localization.t\(copied.*?\)\) \{\s*(copied = .*?)\n\s*\}', panel_source, re.S).group(1)
    registration_label = re.search(r'Button\(localization.t\((.*?)\)\) \{\s*Task \{ await model.register', panel_source).group(1)
    registration_action = re.search(r'Button\(localization.t\(.*?\)\) \{\s*(Task \{ await model.register\(client, consent: consent\) \})', panel_source).group(1)
    panel = folder / "MCPSetupView.swift"
    panel.write_text(panel_source + '''
extension MCPSetupView {
 @MainActor func fixtureCopy(format: String, board: NSPasteboard) -> Bool {
  let exportFormat = format
  var copied = false
  ''' + panel_action.replace('format: exportFormat', 'format: exportFormat, to: board') + '''
  return copied
 }
 @MainActor func fixtureRegistrationKey(_ client: MCPClient) -> String {
  let query = model.queries[client]
  return ''' + registration_label + '''
 }
 @MainActor func fixtureRegister(_ client: MCPClient) -> Task<Void,Never> {
  ''' + registration_action + '''
 }
}
''')
    harness = folder / "Harness.swift"
    harness.write_text(r'''
import AppKit
import SwiftUI
import WebKit
final class TemporaryPreferences: LanguagePreferences {
 let path: URL
 init(_ path: URL) { self.path = path }
 func string(forKey key: String) -> String? {
  guard let data = try? Data(contentsOf:path), let values = try? JSONDecoder().decode([String:String].self,from:data) else { return nil }
  return values[key]
 }
 func set(_ value: Any?, forKey key: String) {
  try! JSONEncoder().encode([key:value as! String]).write(to:path)
 }
}
final class MemoryPreferences: LanguagePreferences {
 var values: [String:String] = [:]
 func string(forKey key: String) -> String? { values[key] }
 func set(_ value: Any?, forKey key: String) { values[key] = value as? String }
}
@MainActor func accepted() -> LaunchConsent {
 let c = LaunchConsent()
 c.observeEnd(CGRect(x:0,y:20,width:20,height:2),viewport:CGSize(width:600,height:300))
 c.setAgreed(true); assert(c.accept())
 return c
}
final class TestEntry {
 var entry = MCPOwnedEntry.absent
 var revision = 0
 var beforeWrite: () async throws -> Void = {}
 func store(client: MCPClient, path: String, env: [String:String], home: URL, timeout: TimeInterval, runner: @escaping MCPCommandRunner) -> MCPEntryStore {
  MCPEntryStore(client:client,executable:path,environment:env,home:home,timeout:timeout,runner:runner,
    readOverride:{ MCPUserSnapshot(file:home.appendingPathComponent("fixture").path,entry:self.entry,
                                   version:String(self.revision),scope:.user,effectiveEntry:self.entry) },
    preflightOverride:{},writeOverride:{ snapshot, desired in
     guard snapshot.version == String(self.revision) else { throw MCPClientOperationCode.versionConflict }
     try await self.beforeWrite()
     self.entry = desired
     self.revision += 1
     return String(self.revision)
    })
 }
}
actor Gate {
 var started = false
 var release: CheckedContinuation<Void,Never>?
 func hold() async { await withCheckedContinuation { release = $0; started = true } }
 func waitForStart() async { while !started { await Task.yield() } }
 func resume() { release?.resume(); release = nil }
}
@main struct Harness {
 @MainActor static func main() async throws {
  let directory = URL(fileURLWithPath: CommandLine.arguments[2])
  let defaults = TemporaryPreferences(directory.appendingPathComponent("preferences.json"))
  let l = Localization(catalogDirectory: directory, defaults: defaults, preferredLanguages: ["ja-JP", "zh-HK"])
  switch CommandLine.arguments[1] {
  case "localization":
   assert(l.locale == "zh-Hant")
   assert(l.t("app.name") == "螢幕整理")
   let cases = ["zh_Hant_CN":"zh-Hant", "zh-Hans-TW":"zh-Hans", "zh-SG":"zh-Hans", "zh-x-hant":"zh-Hans", "fr-CA":"fr", "pt-BR":"pt", "xx":"en", "bad--code":"en"]
   for (input, expected) in cases { assert(l.resolveLocale(input) == expected, input) }
   for (value, expected) in ["fra":"fr","fre":"fr","spa":"es","zh-TW":"zh-Hant","zh-Hant-HK":"zh-Hant-HK","gsw_CH":"de","de-1901":"de-1901","sh":"sr-Latn"] {
    l.setPreference(value); assert(l.preference == expected, value)
    assert(defaults.string(forKey:"screenOrganizer.language") == expected)
   }
   l.setPreference("zh-Hant-HK"); assert(l.t("common.cancel") == "Regional cancel")
   l.setPreference("zh-TW")
   let reopened = Localization(catalogDirectory:directory,defaults:defaults,preferredLanguages:["en"])
   assert(reopened.locale == "zh-Hant" && reopened.preference == "zh-Hant")
   defaults.set("zh_TW",forKey:"screenOrganizer.language")
   let fromAlias = Localization(catalogDirectory:directory,defaults:defaults,preferredLanguages:["en"])
   assert(fromAlias.preference == "zh-Hant")
   assert(l.recognizedPreference("en-a-foo-a-bar") == nil)
   for tag in ["zh-Hant-MO", "zh-Hant-HK-u-nu-hanidec", "zh-Hant-MO-u-nu-hanidec"] {
    assert(l.resolveLocale(tag) == "zh-Hant-HK", tag)
   }
   l.setPreference("fr")
   assert(l.t("common.cancel") == "Annuler")
   assert(l.t("app.name") == "Screen Organizer")
   assert(l.t("test.fallback", ["name":"{count}", "count":"42"]) == "Hello {count}; 42; {missing}")
   assert(l.t("missing.key") == "missing.key")
   assert(defaults.string(forKey: "screenOrganizer.language") == "fr")
   l.setPreference("unapproved")
   assert(l.preference == "system" && l.locale == "zh-Hant")
  case "japanese": assert(l.locale == "ja" && l.t("common.cancel") == "キャンセル")
  case "contributor": assert(l.languages.contains { $0.code == l.locale })
  case "exports":
   let engine = "/Applications/Fixture 引用.app/engine"
   for mode in [MCPAccessMode.readOnly,.applyEnabled] {
    let setup = MCPSetup(executablePath:engine,mode:mode)
    for (format,root,tools) in [("vscode","servers",false),("copilot","mcpServers",true)] {
     let board = NSPasteboard.withUniqueName()
     assert(setup.copy(format:format,to:board))
     let text = board.string(forType:.string)!
     board.releaseGlobally()
     let payload = try JSONSerialization.jsonObject(with:Data(text.utf8)) as! [String:[String:[String:Any]]]
     assert(Array(payload.keys) == [root])
     let entry = payload[root]!["ios-homescreen-mcp"]!
     assert(entry["type"] as? String == "stdio" && entry["command"] as? String == engine)
     assert(entry["args"] as? [String] == ["mcp"])
     assert(tools ? entry["tools"] as? [String] == ["*"] : entry["tools"] == nil)
     let gates = mode == .readOnly ? ["HOMESCREEN_READ_ONLY":"1"] : ["HOMESCREEN_READ_ONLY":"0","HOMESCREEN_ALLOW_APPLY":"1"]
     assert(entry["env"] as? [String:String] == gates)
    }
   }
  case "menuExports":
   let board = NSPasteboard.withUniqueName()
   defer { board.releaseGlobally() }
   let setup = MCPSetupModel()
   let panel = MCPSetupView(model:setup,consent:LaunchConsent())
   func menuCopy(_ format: String) {
    NATIVE_R2_MENU_ACTIONS
   }
   for mode in [MCPAccessMode.applyEnabled,.readOnly,.applyEnabled] {
    setup.mode = mode
    for format in ["launch","json","toml","vscode","copilot"] {
     menuCopy(format)
     let menuBytes = board.string(forType:.string)!
     assert(panel.fixtureCopy(format:format,board:board))
     let panelBytes = board.string(forType:.string)!
     assert(menuBytes.utf8.elementsEqual(panelBytes.utf8), format + " " + mode.rawValue)
     let apply = mode == .applyEnabled
     if format == "launch" {
      assert(menuBytes.contains(apply ? "HOMESCREEN_ALLOW_APPLY=1" : "HOMESCREEN_READ_ONLY=1"))
     } else if format == "toml" {
      assert(menuBytes.contains("HOMESCREEN_READ_ONLY = \"" + (apply ? "0" : "1") + "\""))
      assert(menuBytes.contains("HOMESCREEN_ALLOW_APPLY") == apply)
     } else {
      let root = try JSONSerialization.jsonObject(with:Data(menuBytes.utf8)) as! [String:[String:[String:Any]]]
      let entry = root[format == "vscode" ? "servers" : "mcpServers"]![MCPClientRegistration.serverID]!
      let gates = apply ? ["HOMESCREEN_READ_ONLY":"0","HOMESCREEN_ALLOW_APPLY":"1"] : ["HOMESCREEN_READ_ONLY":"1"]
      assert(entry["env"] as? [String:String] == gates)
     }
    }
   }
  case "registrationLabels":
   let executable = CommandLine.arguments[3]
   let c = accepted()
   for client in MCPClient.allCases {
    for project in [false,true] {
     for exists in [false,true] {
      var user = exists ? try MCPOwnedEntry(["command":"/old","args":["mcp"],"env":["HOMESCREEN_READ_ONLY":"1"]]) : .absent
      let active = try MCPOwnedEntry(["command":"/project","args":["mcp"],"env":["HOMESCREEN_READ_ONLY":"0","HOMESCREEN_ALLOW_APPLY":"1"]])
      var commands: [[String]] = []
      var revision = 0
      var unreadable = false
      let manager = MCPClientManager(environment:[:],homeDirectory:directory,executableLocator:{ _ in executable },runner:{ _,args,_,_,_ in
       commands.append(args)
       if args[1] == "get" {
        let effective = project ? active : user
        guard let object = try? effective.object() else { return MCPCommandResult(exitStatus:1,stdout:"No server named ios-homescreen-mcp") }
        if args.contains("--json") {
         let response: [String:Any] = ["name":MCPClientRegistration.serverID,"enabled":true,"transport":object.merging(["type":"stdio"]) { _,new in new }]
         return MCPCommandResult(exitStatus:0,stdout:String(decoding:try! JSONSerialization.data(withJSONObject:response),as:UTF8.self))
        }
        return MCPCommandResult(exitStatus:0,stdout:"ios-homescreen-mcp:\nScope: " + (project ? "Project" : "User") + "\nCommand: " + (object["command"] as! String) + "\nArgs: mcp\nHOMESCREEN_READ_ONLY=1")
       }
       assert(args.contains("--scope") && args[args.firstIndex(of:"--scope")!+1] == "user")
       if args[1] == "remove" { user = .absent }
       else if args[1] == "add-json" { user = try! MCPOwnedEntry(JSONSerialization.jsonObject(with:Data(args.last!.utf8))) }
       else { user = try! MCPOwnedEntry(["command":executable,"args":["mcp"],"env":["HOMESCREEN_READ_ONLY":"1"]]) }
       return MCPCommandResult(exitStatus:0)
      },storeFactory:{ target,path,env,home,timeout,runner in
       MCPEntryStore(client:target,executable:path,environment:env,home:home,timeout:timeout,runner:runner,
        readOverride:{
         if unreadable { throw MCPClientOperationCode.snapshotReadFailed }
         return MCPUserSnapshot(file:home.appendingPathComponent("label-fixture").path,entry:user,version:String(revision),scope:project ? .other : .user,effectiveEntry:project ? active : user)
        },
        preflightOverride:{},writeOverride:{ snapshot,desired in
         assert(snapshot.version == String(revision))
         user = desired; revision += 1; return String(revision)
        })
      })
      let model = MCPSetupModel(manager:manager,enginePath:executable)
      let panel = MCPSetupView(model:model,consent:c)
      assert(panel.fixtureRegistrationKey(client) == "mcp.register")
      await model.refresh(consent:c)
      assert(panel.fixtureRegistrationKey(client) == (exists ? "mcp.update" : "mcp.register"))
      unreadable = true
      await model.refresh(consent:c)
      assert(panel.fixtureRegistrationKey(client) == (project || exists ? "mcp.update" : "mcp.register"))
      unreadable = false
      await model.refresh(consent:c)
      await panel.fixtureRegister(client).value
      assert(model.registrations[client]?.code == .success, String(describing:model.registrations[client]))
      let saved = try user.object()
      assert(saved?["command"] as? String == executable)
      if client == .codex { assert(revision == 1) }
      else { assert(commands.contains { $0.count > 3 && $0[2] == "--scope" && $0[3] == "user" }) }
     }
    }
   }
  case "fallback":
   l.setPreference("fr")
   assert(l.locale == "fr" && l.t("common.cancel") == "Cancel")
  case "consent":
   let c = LaunchConsent()
   assert(!c.accepted && !c.canContinue)
   c.setAgreed(true); assert(!c.accept())
   c.observeEnd(CGRect(x: 0,y: 800,width: 20,height: 2), viewport: CGSize(width: 600,height: 300))
   assert(!c.hasReadToEnd)
   c.observeEnd(.zero, viewport: .zero); assert(!c.hasReadToEnd)
   c.observeEnd(CGRect(x: 0,y: -10,width: 20,height: 2), viewport: CGSize(width: 600,height: 300))
   assert(!c.hasReadToEnd)
   c.observeEnd(CGRect(x: 0,y: 298,width: 600,height: 2), viewport: CGSize(width: 600,height: 300))
   assert(c.hasReadToEnd && !c.canContinue)
   c.setAgreed(true); assert(c.canContinue)
   c.languageChanged(); assert(!c.hasReadToEnd && !c.agreed && !c.accept())
   c.observeEnd(CGRect(x: 0,y: 298,width: 600,height: 2), viewport: CGSize(width: 600,height: 300))
   c.setAgreed(true); assert(c.accept() && c.accepted)
   assert(!c.accept())
   c.languageChanged(); assert(c.accepted)
   assert(!LaunchConsent().accepted)
   let declined = LaunchConsent(); declined.decline()
   declined.observeEnd(CGRect(x: 0,y: 20,width: 20,height: 2), viewport: CGSize(width: 600,height: 300))
   declined.setAgreed(true); assert(!declined.accept())
  case "consent-view-refresh":
   NSApplication.shared.setActivationPolicy(.accessory)
   let preferences = MemoryPreferences()
   var hostLanguages = ["ja-JP", "de-DE", "fr-FR"]
   let localization = Localization(catalogDirectory:directory,defaults:preferences,preferredLanguageProvider:{hostLanguages})
   let consent = LaunchConsent()
   let view = NSHostingView(rootView:ConsentView(consent:consent,onContinue:{},onDecline:{}).environmentObject(localization))
   let window = NSWindow(contentRect:NSRect(x:0,y:0,width:900,height:650),styleMask:[.titled],backing:.buffered,defer:false)
   window.contentView = view
   window.orderFront(nil)
   defer { window.orderOut(nil) }
   let long = CommandLine.arguments[3] == "long"
   func settle() async throws {
    view.layoutSubtreeIfNeeded()
    try await Task.sleep(nanoseconds:150_000_000)
    view.layoutSubtreeIfNeeded()
   }
   func scrollView(_ node: NSView) -> NSScrollView? {
    if let scroll = node as? NSScrollView { return scroll }
    return node.subviews.compactMap(scrollView).first
   }
   func agree() async throws {
    if long {
     let scroll = scrollView(view)!
     let end = max(0, scroll.documentView!.frame.height - scroll.contentView.bounds.height)
     assert(end > 100)
     scroll.contentView.scroll(to:NSPoint(x:0,y:end))
     scroll.reflectScrolledClipView(scroll.contentView)
     try await settle()
     assert(scroll.contentView.bounds.minY > 100)
     consent.observeEnd(CGRect(x:0,y:20,width:20,height:2),viewport:CGSize(width:600,height:300))
    } else {
     assert(consent.hasReadToEnd, "visible short content can complete reading")
    }
    consent.setAgreed(true)
    assert(consent.canContinue)
   }
   func resetState(_ label: String) {
    assert(!consent.agreed && !consent.canContinue, label)
    if long {
     assert(!consent.hasReadToEnd, "the replacement long text still needs reading: " + label)
     assert(abs(scrollView(view)!.contentView.bounds.minY) < 1, "new language starts at the top: " + label)
    } else {
     assert(consent.hasReadToEnd, "visible new short text can complete reading: " + label)
    }
   }
   try await settle()
   assert(localization.preference == "system" && localization.locale == "de")
   for code in ["fr", "en", "zh-Hans", "zh-Hant", "es", "de", "ru", "pt"] {
    try await agree()
    hostLanguages = ["ja-JP", code]
    NotificationCenter.default.post(name:NSLocale.currentLocaleDidChangeNotification,object:nil)
    try await settle()
    assert(localization.locale == code && localization.preference == "system")
    assert(preferences.string(forKey:"screenOrganizer.language") == "system")
    resetState("System " + code)
   }
   try await agree()
   localization.setPreference("pt")
   try await settle()
   resetState("explicit same locale")
   try await agree()
   localization.setPreference("system")
   try await settle()
   resetState("System same locale")
   try await agree()
   assert(consent.accept())
   hostLanguages = ["en"]
   NotificationCenter.default.post(name:NSLocale.currentLocaleDidChangeNotification,object:nil)
   try await settle()
   assert(consent.accepted)
  case "bridge":
   let origin = URL(string: "http://127.0.0.1:8765/#token=secret")!
   let message: [String:Any] = ["action":"openMCPSetup"]
   assert(NativeBridge.action(message, frameURL: origin, isMainFrame: true, origin: origin) == .openMCPSetup)
   for bad in ["http://127.0.0.1:8766/", "http://localhost:8765/", "https://127.0.0.1:8765/", "http://user@127.0.0.1:8765/", "file:///tmp/editor"] {
    assert(NativeBridge.action(message, frameURL: URL(string:bad), isMainFrame:true, origin:origin) == nil)
   }
   assert(NativeBridge.action(message, frameURL:origin,isMainFrame:false,origin:origin) == nil)
   assert(NativeBridge.action(["action":"run","command":"anything"],frameURL:origin,isMainFrame:true,origin:origin) == nil)
   assert(NativeBridge.action(["action":"setLanguage","preference":"fr"],frameURL:origin,isMainFrame:true,origin:origin) == .setLanguage("fr"))
   assert(NativeBridge.action(["action":"setLanguage","preference":5],frameURL:origin,isMainFrame:true,origin:origin) == nil)
  case "webkit":
   NSApplication.shared.setActivationPolicy(.accessory)
   l.setPreference("en")
   let c = LaunchConsent()
   c.observeEnd(CGRect(x:0,y:20,width:20,height:2),viewport:CGSize(width:600,height:300))
   c.setAgreed(true); assert(c.accept())
   let origin = URL(string:"http://127.0.0.1:47985/")!
   var opens = 0
   let coordinator = WebView.Coordinator(origin:origin,localization:l,consent:c,openSetup:{opens += 1})
   let config = WKWebViewConfiguration()
   config.websiteDataStore = .nonPersistent()
   config.userContentController.add(coordinator,name:"screenOrganizer")
   coordinator.installBootstrap(config.userContentController)
   let web = WKWebView(frame:CGRect(x:0,y:0,width:600,height:400),configuration:config)
   web.navigationDelegate = coordinator
   // An in-memory page with no source routes or network resources.
   web.loadHTMLString("""
   <button id="setup" onclick="window.webkit.messageHandlers.screenOrganizer.postMessage({action:'openMCPSetup'})">MCP</button>
   <script>
   window.boot=JSON.parse(JSON.stringify(window.screenOrganizerHost));window.draft={name:'Kept draft'};window.calls=[];
   window.ScreenOrganizerI18n={setLocale:async preference=>{
     calls.push(preference);if(preference==='fr')await new Promise(resolve=>window.releaseFrench=resolve);
   }};
   </script>
   """,baseURL:origin)
   for _ in 0..<100 {
    if (try? await web.evaluateJavaScript("typeof window.boot === 'object' && calls.length > 0")) as? Bool == true { break }
    try await Task.sleep(nanoseconds:50_000_000)
   }
   let bootJSON = try await web.evaluateJavaScript("JSON.stringify(window.boot)") as! String
   let boot = try JSONSerialization.jsonObject(with:Data(bootJSON.utf8)) as! [String:Any]
   assert(boot["native"] as? Bool == true && boot["consentAccepted"] as? Bool == true && boot["languagePreference"] as? String == "en")
   _ = try await web.evaluateJavaScript("document.getElementById('setup').click(); true")
   try await Task.sleep(nanoseconds:100_000_000)
   assert(opens == 1)
   _ = try await web.evaluateJavaScript("window.webkit.messageHandlers.screenOrganizer.postMessage({action:'setLanguage',preference:'de'}); true")
   try await Task.sleep(nanoseconds:100_000_000)
   assert(l.preference == "de")
   for (input,want) in [("zh_TW","zh-Hant"),("en-a-foo-a-bar","zh-Hant"),("de","de")] {
    _ = try await web.callAsyncJavaScript("window.webkit.messageHandlers.screenOrganizer.postMessage({action:'setLanguage',preference:value});return true",
        arguments:["value":input],in:nil,in:.page)
    for _ in 0..<100 where l.preference != want { try await Task.sleep(nanoseconds:10_000_000) }
    assert(l.preference == want)
   }
   l.setPreference("fr"); coordinator.synchronizeLanguage(web)
   try await Task.sleep(nanoseconds:100_000_000)
   l.setPreference("en"); coordinator.synchronizeLanguage(web)
   try await Task.sleep(nanoseconds:100_000_000)
   _ = try await web.evaluateJavaScript("window.releaseFrench(); true")
   let calls = try await web.evaluateJavaScript("JSON.stringify(calls)") as? String
   assert(calls == "[\"en\",\"fr\",\"en\"]")
   let draft = try await web.evaluateJavaScript("window.draft.name") as? String
   assert(draft == "Kept draft")
   config.userContentController.removeScriptMessageHandler(forName:"screenOrganizer")
  case "system-locale":
   NSApplication.shared.setActivationPolicy(.accessory)
   let preferences = MemoryPreferences()
   var hostLanguages = ["ja-JP","de-DE","fr-FR"]
   let system = Localization(catalogDirectory:directory,defaults:preferences,preferredLanguageProvider:{hostLanguages})
   let origin = URL(string:"http://127.0.0.1:47985/")!
   let coordinator = WebView.Coordinator(origin:origin,localization:system,consent:accepted(),openSetup:{})
   let config = WKWebViewConfiguration()
   config.websiteDataStore = .nonPersistent()
   config.userContentController.add(coordinator,name:"screenOrganizer")
   coordinator.installBootstrap(config.userContentController)
   let web = WKWebView(frame:.zero,configuration:config)
   web.navigationDelegate = coordinator
   web.loadHTMLString(try String(contentsOfFile:CommandLine.arguments[3],encoding:.utf8),baseURL:origin)
   func waitFor(_ script: String) async throws {
    for _ in 0..<200 {
     if (try? await web.evaluateJavaScript(script)) as? Bool == true { return }
     try await Task.sleep(nanoseconds:10_000_000)
    }
    fatalError("System locale fixture timed out: " + script)
   }
   try await waitFor("window.fixtureReady === true")
   let boot = try await web.evaluateJavaScript("window.screenOrganizerHost.languageLocale") as? String
   assert(boot == "de", "bootstrap must include the host-resolved System locale")
   try await waitFor("document.documentElement.lang === 'de' && ScreenOrganizerI18n.preference === 'system'")
   assert(system.locale == "de" && preferences.string(forKey:"screenOrganizer.language") == "system")
   for code in ["en", "system"] {
    _ = try await web.callAsyncJavaScript("await chooseLanguage(code);return true",arguments:["code":code],in:nil,in:.page)
    for _ in 0..<100 where system.preference != code { try await Task.sleep(nanoseconds:10_000_000) }
   }
   try await waitFor("ScreenOrganizerI18n.preference === 'system' && ScreenOrganizerI18n.locale === 'de'")
   assert(system.locale == "de")
   hostLanguages = ["ja-JP","fr-FR","de-DE"]
   NotificationCenter.default.post(name:NSLocale.currentLocaleDidChangeNotification,object:nil)
   for _ in 0..<100 where system.locale != "fr" { try await Task.sleep(nanoseconds:10_000_000) }
   assert(system.preference == "system" && system.locale == "fr")
   coordinator.installBootstrap(config.userContentController)
   coordinator.synchronizeLanguage(web)
   try await waitFor("ScreenOrganizerI18n.locale === 'fr' && document.querySelector('#language-select').value === 'system' && document.querySelector('#consent-language-select').value === 'system'")
   assert(preferences.string(forKey:"screenOrganizer.language") == "system")
   system.setPreference("en")
   hostLanguages = ["de-DE","ja-JP"]
   NotificationCenter.default.post(name:NSLocale.currentLocaleDidChangeNotification,object:nil)
   try await Task.sleep(nanoseconds:20_000_000)
   assert(system.locale == "en")
   coordinator.synchronizeLanguage(web)
   try await waitFor("ScreenOrganizerI18n.preference === 'en'")
   _ = try await web.evaluateJavaScript("chooseLanguage('system');true")
   try await waitFor("ScreenOrganizerI18n.locale === 'de' && ScreenOrganizerI18n.preference === 'system'")
   assert(system.locale == "de")
   _ = try await web.evaluateJavaScript("holdCatalog('pt');true")
   hostLanguages = ["pt-BR","fr-FR"]
   NotificationCenter.default.post(name:NSLocale.currentLocaleDidChangeNotification,object:nil)
   for _ in 0..<100 where system.locale != "pt" { try await Task.sleep(nanoseconds:10_000_000) }
   coordinator.synchronizeLanguage(web)
   try await waitFor("typeof releaseCatalog === 'function'")
   _ = try await web.evaluateJavaScript("chooseLanguage('en');true")
   try await waitFor("ScreenOrganizerI18n.preference === 'en'")
   _ = try await web.evaluateJavaScript("releaseCatalog();true")
   try await waitFor("ScreenOrganizerI18n.locale === 'en' && !ScreenOrganizerI18n.pending")
   for _ in 0..<100 where system.preference != "en" { try await Task.sleep(nanoseconds:10_000_000) }
   assert(system.preference == "en" && system.locale == "en")
   hostLanguages = ["de-DE","ja-JP"]
   system.setPreference("system")
   coordinator.installBootstrap(config.userContentController)
   web.loadHTMLString(try String(contentsOfFile:CommandLine.arguments[3],encoding:.utf8),baseURL:origin)
   try await waitFor("window.fixtureReady === true && ScreenOrganizerI18n.locale === 'de' && ScreenOrganizerI18n.preference === 'system'")
   config.userContentController.removeScriptMessageHandler(forName:"screenOrganizer")
  case "language-origin":
   NSApplication.shared.setActivationPolicy(.accessory)
   let origin = URL(string:"http://127.0.0.1:47985/")!
   l.setPreference("de")
   let coordinator = WebView.Coordinator(origin:origin,localization:l,consent:accepted(),openSetup:{})
   let config = WKWebViewConfiguration()
   config.websiteDataStore = .nonPersistent()
   config.userContentController.add(coordinator,name:"screenOrganizer")
   coordinator.installBootstrap(config.userContentController)
   let web = WKWebView(frame:.zero,configuration:config)
   web.navigationDelegate = coordinator
   let fixture = try String(contentsOfFile:CommandLine.arguments[3],encoding:.utf8)
   web.loadHTMLString(fixture,baseURL:origin)
   func waitFor(_ script: String) async throws {
    for _ in 0..<200 {
     if (try? await web.evaluateJavaScript(script)) as? Bool == true { return }
     try await Task.sleep(nanoseconds:10_000_000)
    }
    fatalError("WebKit language fixture timed out: " + script)
   }
   func choose(_ code: String) async throws {
    _ = try await web.callAsyncJavaScript("await chooseLanguage(code);return true",
     arguments:["code":code],in:nil,in:.page)
    for _ in 0..<100 where l.preference != code { try await Task.sleep(nanoseconds:10_000_000) }
    assert(l.preference == code)
   }
   try await waitFor("window.fixtureReady === true")
   let capability = try await web.evaluateJavaScript("window.screenOrganizerHost.languageSync || ''") as! String
   assert(capability == "native-origin")
   try await waitFor("calls.some(c=>c[0]==='de' && c[1]==='native')")
   if let scenario = CommandLine.arguments.dropFirst(4).first {
    func native(_ code: String) { l.setPreference(code); coordinator.synchronizeLanguage(web) }
    func enqueuePicker(_ code: String) {
     web.callAsyncJavaScript("await chooseLanguage(code); return true",arguments:["code":code],in:nil,in:.page) { _ in }
    }
    func drain() async throws {
     for _ in 0..<3 { _ = try await web.evaluateJavaScript("true") }
    }
    func expect(_ code: String, hostCalls: [String]) async throws {
     try await waitFor("ScreenOrganizerI18n.preference === '\(code)' && !ScreenOrganizerI18n.pending")
     try await drain()
     coordinator.synchronizeLanguage(web)
     try await drain()
     let state = try await web.evaluateJavaScript("""
      [ScreenOrganizerI18n.preference, localStorage.getItem('screenOrganizer.language'), document.documentElement.lang,
       document.querySelector('#language-select').value, document.querySelector('#consent-language-select').value]
      """) as! [String]
     let host = try await web.evaluateJavaScript("calls.filter(c=>c[1]==='native').map(c=>c[0])") as! [String]
     let draft = try await web.evaluateJavaScript("document.querySelector('#folder-name').value") as! String
     let actual = [l.preference, defaults.string(forKey:"screenOrganizer.language")!] + state
     assert(actual == Array(repeating:code,count:7), "\(scenario): \(actual)")
     assert(host == hostCalls, "\(scenario): \(host)")
     assert(draft == "Kept draft")
    }
    try await drain()
    _ = try await web.evaluateJavaScript("calls.length=0;document.querySelector('#folder-name').value='Kept draft'")
    let expectedPickers: [String]
    switch scenario {
    case "browser-first", "failure-after-commit", "same-owner":
     expectedPickers = scenario == "same-owner" ? ["pt","fr"] : ["fr"]
     if scenario == "failure-after-commit" {
      _ = try await web.evaluateJavaScript("afterNextHost=()=>{throw Error('Completion failed')};true")
     }
     enqueuePicker(scenario == "same-owner" ? "pt" : "fr")
     native("pt")
     try await expect("pt",hostCalls:["pt"])
     if scenario == "same-owner" {
      try await choose("fr")
      try await expect("fr",hostCalls:["pt"])
     }
    case "unreadable-owner":
     expectedPickers = ["fr"]
     _ = try await web.evaluateJavaScript("""
      afterNextHost=()=>{
        const api=ScreenOrganizerI18n, prior=Object.getOwnPropertyDescriptor(api,'preference');
        Object.defineProperty(api,'preference',{configurable:true,get(){
          Object.defineProperty(api,'preference',prior); return undefined;
        }});
      };true
      """)
     enqueuePicker("fr")
     native("pt")
     try await waitFor("document.documentElement.lang==='pt'")
     try await drain()
     coordinator.synchronizeLanguage(web)
     try await expect("fr",hostCalls:["pt","fr"])
    case "host-first", "newer-picker":
     expectedPickers = scenario == "newer-picker" ? ["fr","es"] : ["fr"]
     _ = try await web.evaluateJavaScript("holdCatalog('pt');true")
     native("pt")
     try await waitFor("typeof releaseCatalog === 'function'")
     try await choose("fr")
     if scenario == "newer-picker" {
      _ = try await web.evaluateJavaScript("window.finishHost=releaseCatalog;releaseCatalog=null;holdCatalog('es');true")
      enqueuePicker("es")
      try await waitFor("typeof releaseCatalog === 'function'")
      _ = try await web.evaluateJavaScript("finishHost();true")
      try await drain()
     }
     _ = try await web.evaluateJavaScript("releaseCatalog();true")
     try await expect(scenario == "newer-picker" ? "es" : "fr",hostCalls:["pt"])
    case "failure-before-commit", "retry":
     expectedPickers = scenario == "retry" ? [] : ["fr"]
     _ = try await web.evaluateJavaScript("beforeNextHost=()=>{throw Error('Delivery failed')};true")
     if scenario == "failure-before-commit" { enqueuePicker("fr") }
     native("pt")
     try await waitFor("calls.some(c=>c[0]==='pt' && c[1]==='native')")
     try await drain()
     if scenario == "retry" {
      let beforeRetry = try await web.evaluateJavaScript("ScreenOrganizerI18n.preference") as! String
      assert(beforeRetry == "de" && l.preference == "pt")
      coordinator.synchronizeLanguage(web)
      try await expect("pt",hostCalls:["pt","pt"])
     } else { try await expect("fr",hostCalls:["pt"]) }
    case "unrendered-native", "native-before-message":
     expectedPickers = ["fr"]
     _ = try await web.evaluateJavaScript("afterNextHost=()=>new Promise(r=>window.finishHost=r);true")
     enqueuePicker("fr")
     native("pt")
     if scenario == "native-before-message" { l.setPreference("es") }
     try await waitFor("typeof finishHost === 'function'")
     if scenario == "unrendered-native" {
      assert(l.preference == "fr")
      l.setPreference("es")
     }
     _ = try await web.evaluateJavaScript("finishHost();true")
     try await expect("es",hostCalls:["pt","es"])
    case "picker-during-read":
     expectedPickers = ["fr","de"]
     _ = try await web.evaluateJavaScript("afterNextHost=()=>new Promise(r=>window.finishHost=r);true")
     enqueuePicker("fr")
     native("pt")
     try await waitFor("typeof finishHost === 'function'")
     // Start a real cached picker while WebKit reads the crossed delivery's winner.
     _ = try await web.evaluateJavaScript("""
      const api=ScreenOrganizerI18n, prior=Object.getOwnPropertyDescriptor(api,'preference');
      Object.defineProperty(api,'preference',{configurable:true,get(){
        Object.defineProperty(api,'preference',prior); const value=prior.get.call(api);
        chooseLanguage('de'); return value;
      }});
      finishHost();true
      """)
     try await expect("de",hostCalls:["pt"])
    case "navigation":
     expectedPickers = []
     _ = try await web.evaluateJavaScript("afterNextHost=()=>new Promise(()=>{});true")
     enqueuePicker("fr")
     native("pt")
     try await waitFor("document.documentElement.lang==='pt'")
     assert(l.preference == "fr")
     l.setPreference("es")
     coordinator.installBootstrap(config.userContentController)
     let replacement = fixture.replacingOccurrences(of:"<body>",with:"<body id='next-document'>")
     web.loadHTMLString(replacement,baseURL:origin)
     try await waitFor("document.body.id==='next-document' && window.fixtureReady === true && calls.some(c=>c[1]==='native')")
     _ = try await web.evaluateJavaScript("document.querySelector('#folder-name').value='Kept draft'")
     try await expect("es",hostCalls:["es"])
    default: fatalError(scenario)
    }
    let pickers = try await web.evaluateJavaScript("pickerNotifications") as! [String]
    assert(pickers == expectedPickers, scenario)
    let traffic = try await web.evaluateJavaScript("JSON.stringify(unexpectedRequests)") as! String
    assert(traffic == "[]")
    config.userContentController.removeScriptMessageHandler(forName:"screenOrganizer")
    break
   }
   for coalesced in [true,false] {
    _ = try await web.evaluateJavaScript("calls.length=0")
    try await choose("fr")
    if !coalesced { coordinator.synchronizeLanguage(web) }
    try await choose("de")
    coordinator.synchronizeLanguage(web)
    l.setPreference("fr")
    coordinator.synchronizeLanguage(web)
    try await waitFor("document.documentElement.lang==='fr' && document.querySelector('#language-select').value==='fr' && localStorage.getItem('screenOrganizer.language')==='fr'")
    let nativeCalls = try await web.evaluateJavaScript("JSON.stringify(calls.filter(c=>c[1]!=='picker'))") as! String
    assert(nativeCalls == #"[["fr","native","fr"]]"#,nativeCalls)
    l.setPreference("de")
    coordinator.synchronizeLanguage(web)
    try await waitFor("document.documentElement.lang==='de'")
   }
   // A failed, older delivery must not invalidate a newer delivery of the same language.
   _ = try await web.evaluateJavaScript("""
   const previous = ScreenOrganizerI18n.setLocale;
   ScreenOrganizerI18n.setLocale = (code, source) => {
     ScreenOrganizerI18n.setLocale = previous;
     return new Promise((_, reject) => window.failOldDelivery = reject);
   };
   true
   """)
   l.setPreference("es")
   coordinator.synchronizeLanguage(web)
   try await waitFor("typeof failOldDelivery === 'function'")
   l.setPreference("de")
   coordinator.synchronizeLanguage(web)
   l.setPreference("es")
   coordinator.synchronizeLanguage(web)
   try await waitFor("document.documentElement.lang==='es'")
   _ = try await web.evaluateJavaScript("calls.length=0; failOldDelivery(Error('Older delivery failed')); true")
   try await Task.sleep(nanoseconds:100_000_000)
   coordinator.synchronizeLanguage(web)
   let repeated = try await web.evaluateJavaScript("JSON.stringify(calls)") as! String
   assert(repeated == "[]")
   let unexpected = try await web.evaluateJavaScript("JSON.stringify(unexpectedRequests)") as! String
   assert(unexpected == "[]")
   config.userContentController.removeScriptMessageHandler(forName:"screenOrganizer")
  case "refresh":
   let executable = CommandLine.arguments[3]
   var queryFailure: MCPClientOperationCode?
   var addFailure = false
   var calledPaths: [String] = []
   let storage = TestEntry()
   storage.beforeWrite = { if addFailure { throw MCPClientOperationCode.commandFailed } }
   let manager = MCPClientManager(environment: [:], homeDirectory: directory, executableLocator: { _ in executable }, runner: { path, args, _, _, _ in
    calledPaths.append(path)
    if args[1] == "get" {
     if let failure = queryFailure { return MCPCommandResult(exitStatus:1,failure:failure) }
     let response: [String:Any] = ["name":"ios-homescreen-mcp","enabled":true,"transport":["type":"stdio","command":executable,"args":["mcp"],"env":["HOMESCREEN_READ_ONLY":"1"]]]
     return MCPCommandResult(exitStatus:0,stdout:String(decoding:try! JSONSerialization.data(withJSONObject:response),as:UTF8.self))
    }
    return MCPCommandResult(exitStatus:1)
   },storeFactory:storage.store)
   let model = MCPSetupModel(manager:manager,enginePath:executable)
   let c = LaunchConsent()
   c.observeEnd(CGRect(x:0,y:20,width:20,height:2),viewport:CGSize(width:600,height:300)); c.setAgreed(true); assert(c.accept())
   assert(model.installationKey(for:.codex) == "common.unknown")
   for failure in [MCPClientOperationCode.timedOut, .commandFailed] {
    queryFailure = nil
    await model.register(.codex,consent:c)
    assert(model.registrations[.codex]?.code == .success)
    assert(model.feedback(for:.codex)?.primaryKey == "mcp.registered")
    queryFailure = failure
    await model.refresh(consent:c)
    assert(model.registrations[.codex] == nil, "Refresh must replace the old registration result")
    assert(model.queries[.codex]?.code == failure)
    assert(model.queries[.codex]?.configuration == .unknown && model.queries[.codex]?.connection == .unknown)
    let feedback = model.feedback(for:.codex)!
    assert(feedback.primaryKey == (failure == .timedOut ? "mcp.timeout" : "mcp.operationFailed"))
    assert(feedback.details == failure.rawValue && feedback.recoveryKey == nil)
    assert(l.t(feedback.primaryKey) != feedback.primaryKey)
   }
   queryFailure = nil
   addFailure = true
   await model.register(.codex,consent:c)
   assert(model.registrations[.codex]?.code == .commandFailed)
   assert(model.queries[.codex]?.code == .success)
   assert(model.feedback(for:.codex)?.primaryKey == "mcp.operationFailed")
   assert(model.feedback(for:.codex)?.details == "commandFailed · restored")
   await model.refresh(consent:c)
   assert(model.registrations[.codex] == nil && model.queries[.codex]?.configuration == .configured)
   assert(model.feedback(for:.codex) == nil)
   addFailure = false
   await model.register(.codex,consent:c)
   queryFailure = .timedOut
   let selected = executable + "-selected"
   await model.choose(selected,for:.codex,consent:c)
   assert(calledPaths.contains(selected))
   assert(model.installationKey(for:.codex) == "mcp.installed")
   assert(model.detections[.codex]?.executablePath == selected)
   assert(model.registrations[.codex] == nil && model.queries[.codex]?.code == .timedOut)
   assert(model.feedback(for:.codex)?.primaryKey == "mcp.timeout" && model.feedback(for:.codex)?.details == "timedOut")
   queryFailure = nil
   await model.refresh(consent:c)
   assert(model.queries[.codex]?.configuration == .configured && model.queries[.codex]?.connection == .unknown)
   assert(model.checkResult == nil)
   assert(model.feedback(for:.codex) == nil)
   await model.choose(directory.path,for:.codex,consent:c)
   assert(model.installationKey(for:.codex) == "mcp.notExecutable")
   let missingManager = MCPClientManager(environment:[:],homeDirectory:directory,executableLocator:{_ in nil})
   let missing = MCPSetupModel(manager:missingManager,enginePath:executable)
   await missing.refresh(consent:c)
   assert(missing.installationKey(for:.codex) == "mcp.notInstalled")
  case "recovery":
   let executable = CommandLine.arguments[3]
   for recoverySucceeds in [true, false] {
    var snapshot: String? = #"{"command":"/original/engine","args":["mcp"],"env":{"HOMESCREEN_READ_ONLY":"1"}}"#
    let manager = MCPClientManager(environment:[:],homeDirectory:directory,executableLocator:{ _ in executable },runner:{ _,args,_,_,_ in
     switch args[1] {
     case "get":
      return snapshot == nil ? MCPCommandResult(exitStatus:1,stdout:"No server named ios-homescreen-mcp") :
       MCPCommandResult(exitStatus:0,stdout:"ios-homescreen-mcp:\nScope: User\nCommand: /original/engine\nStatus: disconnected\nHOMESCREEN_READ_ONLY=1")
     case "remove": snapshot = nil; return MCPCommandResult(exitStatus:0)
     case "add-json":
      if recoverySucceeds { snapshot = args.last }
      return MCPCommandResult(exitStatus:recoverySucceeds ? 0 : 1)
     default: return MCPCommandResult(exitStatus:1,failure:.timedOut)
     }
    },storeFactory:{ client,path,env,home,timeout,runner in
     MCPEntryStore(client:client,executable:path,environment:env,home:home,timeout:timeout,runner:runner,readOverride:{
      let entry = try snapshot.map { try JSONSerialization.jsonObject(with:Data($0.utf8)) }
      return try MCPUserSnapshot(file:home.appendingPathComponent("fixture").path,entry:MCPOwnedEntry(entry))
     })
    })
    let model = MCPSetupModel(manager:manager,enginePath:executable)
    let c = LaunchConsent()
    c.observeEnd(CGRect(x:0,y:20,width:20,height:2),viewport:CGSize(width:600,height:300)); c.setAgreed(true); assert(c.accept())
    await model.register(.claudeCode,consent:c)
    let feedback = model.feedback(for:.claudeCode)!
    assert(feedback.primaryKey == "mcp.timeout")
    assert(feedback.recoveryKey == (recoverySucceeds ? "mcp.restored" : "mcp.recoveryFailed"))
    assert(feedback.details == (recoverySucceeds ? "timedOut · restored" : "timedOut · failed"))
    await model.refresh(consent:c)
    if recoverySucceeds { assert(model.registrations[.claudeCode] == nil && model.feedback(for:.claudeCode) == nil) }
    else { assert(model.registrations[.claudeCode]?.recovery == .failed && model.feedback(for:.claudeCode)?.recoveryKey == "mcp.recoveryFailed") }
    assert(model.checkResult == nil)
   }
  case "panel":
   let executable = CommandLine.arguments[3]
   let marker = CommandLine.arguments[4]
   var calls: [[String]] = []
   let gate = Gate()
   let storage = TestEntry()
   storage.beforeWrite = { await gate.hold() }
   let manager = MCPClientManager(environment: [:], homeDirectory: directory, executableLocator: { _ in executable }, runner: { _, args, _, _, _ in
    calls.append(args)
    if args[1] == "get" {
     let response: [String:Any] = ["name":"ios-homescreen-mcp","enabled":true,"transport":["type":"stdio","command":executable,"args":["mcp"],"env":["HOMESCREEN_READ_ONLY":"1"]]]
     return MCPCommandResult(exitStatus:0,stdout:String(decoding:try! JSONSerialization.data(withJSONObject:response),as:UTF8.self))
    }
    return MCPCommandResult(exitStatus:0)
   },storeFactory:storage.store)
   let model = MCPSetupModel(manager:manager,serviceCheck:MCPServiceCheck(timeout:10,environment:["MARKER":marker]),enginePath:executable)
   let c = LaunchConsent()
   await model.refresh(consent:c); await model.register(.codex,consent:c); await model.check(consent:c)
   assert(calls.isEmpty && model.checkResult == nil && !FileManager.default.fileExists(atPath:marker))
   c.observeEnd(CGRect(x:0,y:20,width:20,height:2),viewport:CGSize(width:600,height:300))
   c.setAgreed(true); assert(c.accept())
   let first = Task { await model.register(.codex,consent:c) }
   await gate.waitForStart()
   assert(model.busy)
   model.dismissPanel()
   let delegate = AppDelegate()
   delegate.setup = model
   var replies = 0
   delegate.terminationReply = { replies += 1 }
   assert(delegate.applicationShouldTerminate(NSApplication.shared) == .terminateLater)
   assert(delegate.applicationShouldTerminate(NSApplication.shared) == .terminateLater)
   assert(replies == 0)
   await model.register(.codex,consent:c)
   await model.check(consent:c)
   await gate.resume()
   await first.value
   while replies == 0 { await Task.yield() }; assert(replies == 1)
   assert(storage.revision == 1)
   assert(!model.busy && model.registrations[.codex]?.code == .success)
   assert(model.queries[.codex]?.configuration == .configured && model.queries[.codex]?.connection == .unknown)
   assert(model.checkResult == nil)
   await model.check(consent:c)
   assert(model.checkResult?.code == .passed && FileManager.default.fileExists(atPath:marker), String(describing:model.checkResult?.code))
   assert(model.queries[.codex]?.connection == .unknown)
  case "cancelInspection":
   let executable = CommandLine.arguments[3]
   let marker = CommandLine.arguments[4]
   let manager = MCPClientManager(environment:["MARKER":marker],homeDirectory:directory,timeout:20,executableLocator:{ _ in executable })
   let model = MCPSetupModel(manager:manager,serviceCheck:MCPServiceCheck(timeout:20,environment:["MARKER":marker]),enginePath:executable)
   let c = accepted()
   for action in ["refresh","check"] {
    let job = Task { if action == "refresh" { await model.refresh(consent:c) } else { await model.check(consent:c) } }
    while !FileManager.default.fileExists(atPath:marker) { await Task.yield() }
    model.dismissPanel()
    await job.value
    assert(!model.busy && model.checkResult == nil && model.detections[.claudeCode] == nil)
    let log = try String(contentsOfFile:marker).components(separatedBy:"|")
    assert(kill(Int32(log[0])!,0) < 0 && errno == ESRCH)
    if log.count == 2 { assert(!FileManager.default.fileExists(atPath:log[1])) }
    try FileManager.default.removeItem(atPath:marker)
   }
   let token = MCPChildCancellation()
   token.cancel()
   do { _ = try token.launch(executable,arguments:[], environment:["MARKER":marker],cwd:directory.path,stdin:.nullDevice,stdout:.nullDevice,stderr:.nullDevice); assertionFailure("cancelled child started") } catch MCPClientOperationCode.cancelled {}
   assert(!FileManager.default.fileExists(atPath:marker))
  case "engine":
   let executable = URL(fileURLWithPath: CommandLine.arguments[3])
   let marker = CommandLine.arguments[4]
   let c = LaunchConsent()
   let e = Engine(executableURL: executable, environment: ["MARKER":marker,"HOMESCREEN_DATA":directory.appendingPathComponent("data").path])
   e.start(consent:c)
   try await Task.sleep(nanoseconds:100_000_000)
   assert(!FileManager.default.fileExists(atPath:marker))
   c.observeEnd(CGRect(x:0,y:20,width:20,height:2),viewport:CGSize(width:600,height:300))
   c.setAgreed(true); assert(c.accept())
   e.start(consent:c)
   for _ in 0..<100 {
    if FileManager.default.fileExists(atPath:marker) { break }
    try await Task.sleep(nanoseconds:20_000_000)
   }
   e.stop()
   assert(FileManager.default.fileExists(atPath:marker), String(describing:e.state))
  default: fatalError("unknown harness mode")
  }
  print("passed")
 }
}
'''.replace("NATIVE_R2_MENU_ACTIONS", menu_harness))
    binary = folder / "native-tests"
    compiled = subprocess.run(["swiftc", "-module-cache-path", str(folder / "modules"),
                               *[str(p) for p in SOURCES.glob("*.swift") if p.name not in {"App.swift", "MCPSetupView.swift"}],
                               str(app), str(panel), str(harness), "-o", str(binary)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    return binary


@pytest.fixture
def catalogs(tmp_path):
    target = tmp_path / "catalogs"
    shutil.copytree(ROOT / "homescreen/web/locales", target)
    # Locale fallback assumptions belong to this fixed manifest.
    codes = {"en":["en"],"zh-Hans":["zh","zh-CN","zh-SG"],"zh-Hant":["zh-HK","zh-TW"],
             "zh-Hant-HK":["zh-Hant-HK","zh-Hant-MO"],"de-1901":["de-1901"],"sr-Latn":["sr-Latn"],"fr":["fr"],"pt":["pt"],"de":["gsw_CH"],"es":["es"]}
    (target/"manifest.json").write_text(json.dumps({"languages":[
        {"code":code,"name":code,"aliases":aliases} for code,aliases in codes.items()]}))
    (target/"zh-Hant-HK.json").write_text(json.dumps({"common.cancel":"Regional cancel"}))
    en = json.loads((target / "en.json").read_text())
    en["test.fallback"] = "Hello {name}; {count}; {missing}"
    (target / "en.json").write_text(json.dumps(en))
    return target


@pytest.mark.parametrize("mode", ["localization", "consent", "bridge", "webkit", "exports", "menuExports"])
def test_native_contracts(native_binary, catalogs, mode):
    result = subprocess.run([str(native_binary), mode, str(catalogs)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("length", ["long", "short"])
def test_hosted_consent_resets_for_system_locale_and_preference_changes(native_binary, tmp_path, length):
    catalogs = tmp_path / "consent-catalogs"
    shutil.copytree(ROOT / "homescreen/web/locales", catalogs)
    paragraphs = ["affiliation", "trademarks", "deviceInterfaces", "backups", "warranty", "clientData"]
    for path in catalogs.glob("*.json"):
        if path.name == "manifest.json":
            continue
        catalog = json.loads(path.read_text())
        for key in paragraphs:
            catalog["consent." + key] = catalog["consent." + key] * 25 if length == "long" else "Visible text."
        path.write_text(json.dumps(catalog))
    result = subprocess.run([str(native_binary), "consent-view-refresh", str(catalogs), length],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_registration_action_uses_user_scope_and_configuration(native_binary,catalogs):
    result = subprocess.run([str(native_binary),"registrationLabels",str(catalogs),sys.executable],capture_output=True,text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_native_catalog_falls_back_to_english(native_binary, catalogs):
    (catalogs / "fr.json").unlink()
    result = subprocess.run([str(native_binary), "fallback", str(catalogs)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["engine", "panel"])
def test_child_start_order_and_serialized_panel(native_binary, catalogs, tmp_path, mode):
    child = tmp_path / "engine"
    child.write_text('#!/bin/sh\nprintf accepted > "$MARKER"\n')
    if mode == "panel":
        child.write_text(f"#!{sys.executable}\n" + '''import json,os,sys
from pathlib import Path
Path(os.environ["MARKER"]).write_text("started")
for line in sys.stdin:
    message = json.loads(line)
    if message["method"] == "initialize":
        result = {"protocolVersion":"2025-11-25","capabilities":{"tools":{}},"serverInfo":{"name":"ios-homescreen-mcp","version":"1"}}
    elif message["method"] == "tools/list":
        result = {"tools":[{"name":name,"inputSchema":{"type":"object"}} for name in ["read_phone","show_layout","move","diff"]]}
    else: continue
    print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":result}),flush=True)
''')
    child.chmod(0o755)
    result = subprocess.run([str(native_binary), mode, str(catalogs), str(child), str(tmp_path / "started")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["refresh", "recovery"])
def test_refresh_replaces_registration_results(native_binary, catalogs, tmp_path, mode):
    executable = tmp_path / "synthetic-client"
    executable.write_text("#!/bin/sh\nexit 99\n")
    executable.chmod(0o755)
    shutil.copy(executable, str(executable) + "-selected")
    result = subprocess.run([str(native_binary), mode, str(catalogs), str(executable)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

def test_native_contributor_language_selection(native_binary, catalogs):
    # Fixed baselines keep this experiment independent of future contributions.
    baseline = catalogs.parent / "contribution"
    baseline.mkdir()
    registrations = [{"code":"en","name":"English","aliases":["en"]},
                     {"code":"zh-Hant","name":"繁體中文","aliases":["zh-Hant","zh-HK"]},
                     {"code":"ja","name":"日本語","aliases":["ja"]}]
    (baseline/"manifest.json").write_text(json.dumps({"default":"en","languages":registrations}))
    for code in ("en","zh-Hant"):
        shutil.copy(ROOT/"homescreen/web/locales"/(code+".json"),baseline/(code+".json"))
    japanese = json.loads((baseline/"en.json").read_text())
    japanese["common.cancel"] = "キャンセル"
    (baseline/"ja.json").write_text(json.dumps(japanese, ensure_ascii=False))
    subprocess.run([sys.executable,str(ROOT/"tools/check_translations.py"),"--locales",str(baseline)],check=True)
    subprocess.run([str(native_binary),"japanese",str(baseline)], check=True)
    shutil.copy(ROOT/"homescreen/web/locales/manifest.json", catalogs/"manifest.json")
    subprocess.run([str(native_binary),"contributor",str(catalogs)], check=True)

def test_panel_dismissal_cancels_and_reaps_inspections(native_binary,catalogs,tmp_path):
    child = tmp_path/"inspection"
    child.write_text(f"#!{sys.executable}\n" + '''import os,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
if os.fork(): time.sleep(6); os._exit(0)
marker = Path(os.environ["MARKER"])
tmp = marker.with_suffix(".tmp")
tmp.write_text(str(os.getpid())+"|"+os.environ.get("HOMESCREEN_DATA","/no-fixture-data"))
tmp.rename(marker)
time.sleep(60)
''')
    child.chmod(0o755)
    result = subprocess.run([str(native_binary),"cancelInspection",str(catalogs),str(child),str(tmp_path/"started")],
                            capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr


def run_webkit_language(native_binary, catalogs, tmp_path, *scenario, mode="language-origin"):
    web = ROOT / "homescreen/web"
    # All resources are embedded, including the production picker and locale engine.
    files = {path.name: json.loads(path.read_text()) for path in catalogs.glob("*.json")}
    files["manifest.json"]["default"] = "en"
    data = json.dumps({"/static/locales/" + key: value for key, value in files.items()})
    fetch_catalog = """
    window.calls = [];
    window.unexpectedRequests = [];
    const files = CATALOG_DATA;
    let heldCatalog, releaseCatalog;
    const holdCatalog = code => heldCatalog = '/static/locales/' + code + '.json';
    window.fetch = async (url, options = {}) => {
      if ((options.method || 'GET') === 'GET' && Object.hasOwn(files, url)) {
        if (url === heldCatalog) {
          heldCatalog = null;
          await new Promise(resolve => releaseCatalog = resolve);
        }
        return {ok: true, json: async () => files[url]};
      }
      unexpectedRequests.push(String(url));
      throw Error('Unexpected request');
    };
    """.replace("CATALOG_DATA", data)
    record_calls = """
    const selectLocale = ScreenOrganizerI18n.setLocale;
    const pickerNotifications = [];
    ScreenOrganizerI18n.onTransition(({phase, source}) => {
      if (phase === 'settle' && source === 'picker') pickerNotifications.push(ScreenOrganizerI18n.preference);
    });
    let beforeNextHost, afterNextHost;
    ScreenOrganizerI18n.setLocale = async (...args) => {
      calls.push(args);
      if (args[1] !== 'native') return selectLocale(...args);
      const before = beforeNextHost, after = afterNextHost;
      beforeNextHost = afterNextHost = null;
      before?.();
      const value = await selectLocale(...args);
      if (after) await after();
      return value;
    };
    const chooseLanguage = code => {
      const picker = document.querySelector('#language-select');
      picker.value = code;
      return picker.onchange({target: picker});
    };
    """
    scripts = {
        "i18n.js": fetch_catalog + (web / "i18n.js").read_text(),
        "onboarding.js": record_calls + (web / "onboarding.js").read_text(),
        "app.js": "ScreenOrganizerOnboarding.ready.then(() => window.fixtureReady = true)",
    }
    html = (web / "index.html").read_text().replace(
        '<link rel="stylesheet" href="/static/app.css">',
        """<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; img-src data:">""",
    )
    for name, script in scripts.items():
        html = html.replace(f'<script src="/static/{name}"></script>', f"<script>{script}</script>")
    fixture = tmp_path / "language.html"
    fixture.write_text(html)
    result = subprocess.run([str(native_binary), mode, str(catalogs), str(fixture), *scenario],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_webkit_delivers_genuine_native_language_after_browser_round_trip(native_binary, catalogs, tmp_path):
    run_webkit_language(native_binary, catalogs, tmp_path)


def test_webkit_system_uses_full_host_language_list(native_binary, catalogs, tmp_path):
    run_webkit_language(native_binary, catalogs, tmp_path, mode="system-locale")


@pytest.mark.parametrize("scenario", [
    "browser-first", "host-first", "newer-picker", "same-owner", "failure-before-commit",
    "failure-after-commit", "retry", "unreadable-owner", "unrendered-native", "native-before-message",
    "picker-during-read", "navigation",
])
def test_webkit_reconciles_crossed_language_choices(native_binary, catalogs, tmp_path, scenario):
    run_webkit_language(native_binary, catalogs, tmp_path, scenario)
