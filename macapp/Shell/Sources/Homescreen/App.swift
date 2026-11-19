import AppKit
import SwiftUI
import WebKit

@main
struct HomeScreenOrganizerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var engine = Engine()
    @StateObject private var localization = Localization()
    @StateObject private var consent = LaunchConsent()
    @StateObject private var setup = MCPSetupModel()
    @State private var showSetup = false
    @State private var showDisclaimer = false

    var body: some Scene {
        Window(localization.t("app.name"), id: "main") {
            Group {
                if consent.accepted {
                    RootView(openSetup: { showSetup = true })
                } else {
                    ConsentView(consent: consent, onContinue: { engine.start(consent: consent) },
                                onDecline: { NSApplication.shared.terminate(nil) })
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .environmentObject(engine)
            .environmentObject(localization)
            .environmentObject(consent)
            .frame(minWidth: 1000, minHeight: 680)
            .onAppear { delegate.engine = engine; delegate.setup = setup }
            .sheet(isPresented: $showSetup) {
                MCPSetupView(model: setup, consent: consent)
                    .environmentObject(localization)
            }
            .sheet(isPresented: $showDisclaimer) {
                ConsentView(consent: consent, reviewing: true,
                            onContinue: { showDisclaimer = false }, onDecline: {})
                    .environmentObject(localization)
                    .frame(width: 780, height: 650)
            }
        }
        .defaultSize(width: 1360, height: 860)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(after: .toolbar) {
                Button(localization.t("native.reload")) { WebStore.shared.webView?.reload() }
                    .keyboardShortcut("r").disabled(!consent.accepted)
                Button(localization.t("native.restartEngine")) { engine.start(consent: consent) }
                    .keyboardShortcut("r", modifiers: [.command, .shift]).disabled(!consent.accepted)
            }
            CommandMenu(localization.t("native.deviceMenu")) {
                Button(localization.t("native.openDataFolder")) {
                    try? FileManager.default.createDirectory(at: Engine.dataFolder, withIntermediateDirectories: true)
                    NSWorkspace.shared.open(Engine.dataFolder)
                }.disabled(!consent.accepted)
                Button(localization.t("mcp.openSetup")) { showSetup = true }.disabled(!consent.accepted)
                Menu("MCP") {
                    let exports = setup
                    Button(localization.t("mcp.copyLaunch")) { exports.copy(format: "launch") }
                    Button(localization.t("mcp.copyJSON")) { exports.copy(format: "json") }
                    Button(localization.t("mcp.copyCodex")) { exports.copy(format: "toml") }
                    Button(localization.t("mcp.copyCopilot")) { exports.copy(format: "copilot") }
                    Button(localization.t("mcp.copyVSCode")) { exports.copy(format: "vscode") }
                }.disabled(!consent.accepted)
            }
            CommandGroup(replacing: .help) {
                Button(localization.t("consent.disclaimer")) { showDisclaimer = true }.disabled(!consent.accepted)
                Button(localization.t("native.github")) {
                    NSWorkspace.shared.open(URL(string: "https://github.com/worldsavepoint/ios-homescreen-organizer")!)
                }
                Button(localization.t("native.upstream")) {
                    NSWorkspace.shared.open(URL(string: "https://github.com/kats1123/homescreen-mcp")!)
                }
            }
        }
    }

}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var engine: Engine?
    var setup: MCPSetupModel?
    private var waitingForSetup = false
    var terminationReply: @MainActor () -> Void = { NSApplication.shared.reply(toApplicationShouldTerminate: true) }

    @MainActor func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if waitingForSetup { return .terminateLater }
        guard let pending = setup?.prepareToQuit() else { return .terminateNow }
        waitingForSetup = true
        Task { @MainActor in
            await pending.value
            waitingForSetup = false
            terminationReply()
        }
        return .terminateLater
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { engine?.stop() }
    }
}

struct RootView: View {
    @EnvironmentObject private var engine: Engine
    @EnvironmentObject private var localization: Localization
    @EnvironmentObject private var consent: LaunchConsent
    var openSetup: () -> Void

    var body: some View {
        switch engine.state {
        case .starting:
            VStack(spacing: 14) {
                AppMark().frame(width: 56, height: 56)
                ProgressView().controlSize(.small)
                Text(localization.t("native.starting")).foregroundStyle(.secondary)
                LanguagePicker().frame(width: 240)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .ready(let url):
            WebView(url: url, localization: localization, consent: consent, openSetup: openSetup)
                .id(url).ignoresSafeArea()
        case .failed(let failure):
            VStack(alignment: .leading, spacing: 16) {
                Text(localization.t("native.startFailed")).font(.title2.bold())
                Text(localization.t(failure.key, failure.parameters)).textSelection(.enabled)
                if !failure.details.isEmpty { DiagnosticsView(details: failure.details) }
                LanguagePicker()
                Button(localization.t("common.retry")) { engine.start(consent: consent) }.keyboardShortcut(.defaultAction)
            }
            .padding(32)
            .frame(maxWidth: 640, maxHeight: .infinity)
        }
    }
}

struct DiagnosticsView: View {
    @EnvironmentObject private var localization: Localization
    let details: String
    var body: some View {
        DisclosureGroup(localization.t("native.diagnostics")) {
            ScrollView {
                Text(details).font(.system(.footnote, design: .monospaced)).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }.frame(maxHeight: 180)
        }
    }
}

/// The four colored squares, as in the web app's logo.
struct AppMark: View {
    var body: some View {
        GeometryReader { g in
            let s = g.size.width, cell = s * 0.4, r = s * 0.1
            ZStack(alignment: .topLeading) {
                ForEach(0..<4) { i in
                    RoundedRectangle(cornerRadius: r)
                        .fill([Color.orange, .green, .blue, .purple][i])
                        .frame(width: cell, height: cell)
                        .offset(x: i % 2 == 0 ? s * 0.07 : s * 0.53, y: i < 2 ? s * 0.07 : s * 0.53)
                }
            }
        }
    }
}

@MainActor
final class WebStore {
    static let shared = WebStore()
    weak var webView: WKWebView?
}

struct WebView: NSViewRepresentable {
    let url: URL
    @ObservedObject var localization: Localization
    let consent: LaunchConsent
    var openSetup: () -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(origin: url, localization: localization, consent: consent, openSetup: openSetup)
    }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        config.userContentController.add(context.coordinator, name: "screenOrganizer")
        context.coordinator.installBootstrap(config.userContentController)
        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = context.coordinator
        web.uiDelegate = context.coordinator
        web.allowsMagnification = true
        web.setValue(false, forKey: "drawsBackground")
        web.load(URLRequest(url: url))
        WebStore.shared.webView = web
        return web
    }

    func updateNSView(_ web: WKWebView, context: Context) {
        context.coordinator.installBootstrap(web.configuration.userContentController)
        context.coordinator.synchronizeLanguage(web)
    }

    static func dismantleNSView(_ web: WKWebView, coordinator: Coordinator) {
        web.configuration.userContentController.removeScriptMessageHandler(forName: "screenOrganizer")
        web.navigationDelegate = nil
        web.uiDelegate = nil
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        let origin: URL
        let localization: Localization
        let consent: LaunchConsent
        var openSetup: () -> Void
        private var bootstrapPreference: String?
        private var bootstrapLocale: String?
        private var synchronizedPreference: String?
        private var synchronizedLocale: String?
        private var languageRevision = 0
        private var pickerRevision = 0
        private var deliveryPending = false

        init(origin: URL, localization: Localization, consent: LaunchConsent, openSetup: @escaping () -> Void) {
            self.origin = origin
            self.localization = localization
            self.consent = consent
            self.openSetup = openSetup
        }

        func installBootstrap(_ controller: WKUserContentController) {
            guard bootstrapPreference != localization.preference || bootstrapLocale != localization.locale else { return }
            bootstrapPreference = localization.preference
            bootstrapLocale = localization.locale
            let values: [String: Any] = ["native": true, "languageSync": "native-origin",
                "languagePreference": localization.preference, "languageLocale": localization.locale,
                "consentAccepted": consent.accepted]
            let json = String(decoding: try! JSONSerialization.data(withJSONObject: values), as: UTF8.self)
            controller.removeAllUserScripts()
            controller.addUserScript(WKUserScript(source: "window.screenOrganizerHost = \(json);", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }

        func synchronizeLanguage(_ web: WKWebView) {
            guard !web.isLoading, let current = web.url, NavigationPolicy.destination(current, origin: origin) == .embedded,
                  synchronizedPreference != localization.preference || synchronizedLocale != localization.locale else { return }
            let preference = localization.preference
            let resolvedLocale = localization.locale
            // Track requests before awaiting JavaScript so a quick return to the previous
            // language still reaches the web catalog loader's revision check.
            synchronizedPreference = preference
            synchronizedLocale = resolvedLocale
            languageRevision += 1
            let revision = languageRevision
            let pickerAtDispatch = pickerRevision
            deliveryPending = true
            web.callAsyncJavaScript("if (window.ScreenOrganizerI18n) { await window.ScreenOrganizerI18n.setLocale(preference, 'native', resolvedLocale); return true; } return false;",
                arguments: ["preference": preference, "resolvedLocale": resolvedLocale], in: nil, in: .page) { [weak self, weak web] result in
                    guard let self, let web, languageRevision == revision else { return }
                    if pickerRevision != pickerAtDispatch {
                        reconcileLanguage(in: web, revision: revision)
                        return
                    }
                    deliveryPending = false
                    if case .success(let value) = result, value as? Bool == true { return }
                    synchronizedPreference = nil
                    synchronizedLocale = nil
                }
        }

        private func reconcileLanguage(in web: WKWebView, revision: Int) {
            // A picker commit and a host call crossed. Read the document's winner
            // without starting another language change over a newer pending picker.
            let pickerAtRead = pickerRevision
            web.evaluateJavaScript("window.ScreenOrganizerI18n?.preference") { [weak self, weak web] value, _ in
                guard let self, let web, languageRevision == revision else { return }
                if synchronizedPreference != localization.preference || synchronizedLocale != localization.locale {
                    synchronizeLanguage(web)
                    return
                }
                if pickerRevision != pickerAtRead {
                    reconcileLanguage(in: web, revision: revision)
                    return
                }
                deliveryPending = false
                guard let value = value as? String, let owner = localization.recognizedPreference(value) else {
                    synchronizedPreference = nil
                    synchronizedLocale = nil
                    return
                }
                synchronizedPreference = owner
                if localization.preference != owner { localization.setPreference(owner) }
                synchronizedLocale = localization.locale
                if owner == "system" {
                    synchronizedLocale = nil
                    synchronizeLanguage(web)
                }
            }
        }

        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.name == "screenOrganizer", consent.accepted,
                  let action = NativeBridge.action(message.body, frameURL: message.frameInfo.request.url,
                      isMainFrame: message.frameInfo.isMainFrame, origin: origin) else { return }
            switch action {
            case .openMCPSetup: openSetup()
            case .setLanguage(let preference):
                guard let owner = localization.recognizedPreference(preference) else { return }
                // The picker commits its choice before notifying this coordinator.
                pickerRevision += 1
                if deliveryPending, synchronizedPreference != localization.preference || synchronizedLocale != localization.locale,
                   let web = message.webView {
                    synchronizeLanguage(web)
                    return
                }
                synchronizedPreference = owner
                localization.setPreference(owner)
                synchronizedLocale = localization.locale
                if owner == "system", let web = message.webView {
                    synchronizedLocale = nil
                    synchronizeLanguage(web)
                }
            }
        }

        func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
            languageRevision += 1
            deliveryPending = false
            synchronizedPreference = nil
            synchronizedLocale = nil
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            synchronizedPreference = nil
            synchronizedLocale = nil
            synchronizeLanguage(webView)
        }

        // Links to anything but the engine open in the default browser.
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url else { decisionHandler(.cancel); return }
            switch NavigationPolicy.destination(url, origin: origin) {
            case .browser: NSWorkspace.shared.open(url); decisionHandler(.cancel)
            case .embedded: decisionHandler(.allow)
            case .blocked: decisionHandler(.cancel)
            }
        }

        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url, NavigationPolicy.destination(url, origin: origin) != .blocked {
                NSWorkspace.shared.open(url)
            }
            return nil
        }

        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
            let alert = NSAlert()
            alert.messageText = message
            alert.addButton(withTitle: localization.t("common.ok"))
            alert.runModal()
            completionHandler()
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
            let alert = NSAlert()
            alert.messageText = message
            alert.addButton(withTitle: localization.t("common.ok"))
            alert.addButton(withTitle: localization.t("common.cancel"))
            completionHandler(alert.runModal() == .alertFirstButtonReturn)
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }
    }
}
