import AppKit
import SwiftUI

@MainActor
final class MCPSetupModel: ObservableObject {
    @Published private(set) var detections: [MCPClient: MCPClientDetection] = [:]
    @Published private(set) var queries: [MCPClient: MCPClientQuery] = [:]
    @Published private(set) var registrations: [MCPClient: MCPClientRegistrationResult] = [:]
    @Published private(set) var checkResult: MCPServiceCheckResult?
    @Published private(set) var busy = false
    @Published var mode: MCPAccessMode = .readOnly
    private var inspectionTask: Task<Void, Never>?
    private(set) var mutationTask: Task<Void, Never>?
    private var generation = UUID()
    private var selectedPaths: [MCPClient: String] = [:]
    let enginePath: String
    private let manager: MCPClientManager
    private let serviceCheck: MCPServiceCheck

    init(manager: MCPClientManager = MCPClientManager(), serviceCheck: MCPServiceCheck = MCPServiceCheck(),
         enginePath: String = Engine.executable.path) {
        self.manager = manager
        self.serviceCheck = serviceCheck
        self.enginePath = enginePath
    }

    var exportConfiguration: MCPSetup { MCPSetup(executablePath: enginePath, mode: mode) }

    @discardableResult
    func copy(format: String, to pasteboard: NSPasteboard = .general) -> Bool {
        exportConfiguration.copy(format: format, to: pasteboard)
    }

    func registrationActionKey(for client: MCPClient) -> String {
        let query = queries[client]
        let state = query?.scope == .other && query?.userConfiguration != .unknown
            ? query?.userConfiguration : query?.configuration
        return state == .configured ? "mcp.update" : "mcp.register"
    }

    struct Feedback {
        let primaryKey: String
        var recoveryKey: String? = nil
        var details: String? = nil
    }

    func feedback(for client: MCPClient) -> Feedback? {
        if let result = registrations[client] {
            return Feedback(primaryKey: result.code == .success ? "mcp.registered" : operationKey(result.code),
                recoveryKey: result.cleanupPending ? "mcp.cleanupPending" : recoveryKey(result.recovery),
                details: result.code == .success ? nil : "\(result.code.rawValue) · \(result.recovery.rawValue)")
        }
        guard let query = queries[client], query.code != .success && query.code != .notFound else { return nil }
        return Feedback(primaryKey: operationKey(query.code), details: query.code.rawValue)
    }

    private func recoveryKey(_ recovery: MCPRegistrationRecovery) -> String? {
        switch recovery {
        case .notNeeded: return nil
        case .restored: return "mcp.restored"
        case .failed: return "mcp.recoveryFailed"
        }
    }

    private func operationKey(_ code: MCPClientOperationCode) -> String {
        switch code {
        case .unsupportedClient: return "mcp.updateCodex"
        case .remoteConflict: return "mcp.remoteConflict"
        case .recoveryConflict: return "mcp.recoveryConflict"
        case .timedOut: return "mcp.timeout"
        case .notFound: return "mcp.notInstalled"
        case .notExecutable: return "mcp.notExecutable"
        case .snapshotReadFailed: return "mcp.snapshotFailed"
        case .recoveryFailed: return "mcp.recoveryFailed"
        case .invalidResponse: return "mcp.invalidResponse"
        default: return "mcp.operationFailed"
        }
    }

    func dismissPanel() {
        generation = UUID()
        inspectionTask?.cancel()
        inspectionTask = nil
        busy = mutationTask != nil
    }

    /// Return the still-running task so AppKit can wait for cleanup or recovery.
    func prepareToQuit() -> Task<Void, Never>? {
        let pending = mutationTask ?? inspectionTask
        dismissPanel()
        return pending
    }

    private func inspect(_ operation: @escaping @MainActor () async -> Void) async {
        guard !busy, !Task.isCancelled else { return }
        let id = UUID()
        generation = id
        busy = true
        let task = Task { if !Task.isCancelled { await operation() } }
        inspectionTask = task
        await withTaskCancellationHandler { await task.value } onCancel: { task.cancel() }
        if generation == id {
            inspectionTask = nil
            busy = false
        }
    }

    private func mutate(_ operation: @escaping @MainActor () async -> Void) async {
        busy = true
        let task = Task { await operation() }
        mutationTask = task
        await task.value
        mutationTask = nil
        busy = false
    }

    func refresh(consent: LaunchConsent) async {
        guard consent.accepted, !busy, !Task.isCancelled else { return }
        let panel = generation
        registrations.removeAll()
        for client in MCPClient.allCases where manager.hasPending(client) {
            await mutate {
                self.registrations[client] = await self.manager.recover(client: client, executablePath: self.selectedPaths[client])
            }
            guard generation == panel, !Task.isCancelled else { return }
        }
        await inspect {
            for client in MCPClient.allCases {
                guard !Task.isCancelled else { return }
                let path = self.selectedPaths[client]
                let detection = await self.manager.detect(client: client, executablePath: path)
                guard !Task.isCancelled else { return }
                self.detections[client] = detection
                let query = await self.manager.query(client: client, executablePath: path)
                guard !Task.isCancelled else { return }
                self.queries[client] = query
            }
        }
    }

    func choose(_ path: String, for client: MCPClient, consent: LaunchConsent) async {
        guard consent.accepted, !busy else { return }
        selectedPaths[client] = path
        await refresh(consent: consent)
    }

    func register(_ client: MCPClient, consent: LaunchConsent) async {
        guard consent.accepted, !busy else { return }
        await mutate {
            let path = self.selectedPaths[client]
            let result = await self.manager.register(client: client, executablePath: path, enginePath: self.enginePath, mode: self.mode)
            self.registrations[client] = result
            if let query = result.query { self.queries[client] = query }
            else { self.queries[client] = await self.manager.query(client: client, executablePath: path) }
        }
    }

    func check(consent: LaunchConsent) async {
        guard consent.accepted, !busy else { return }
        await inspect {
            let result = await self.serviceCheck.run(executablePath: self.enginePath)
            if !Task.isCancelled { self.checkResult = result }
        }
    }

    func installationKey(for client: MCPClient) -> String {
        switch detections[client]?.availability {
        case .available: return "mcp.installed"
        case .notFound: return "mcp.notInstalled"
        case .notExecutable: return "mcp.notExecutable"
        case nil: return "common.unknown"
        }
    }
}

struct MCPSetupView: View {
    @EnvironmentObject private var localization: Localization
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var model: MCPSetupModel
    @ObservedObject var consent: LaunchConsent
    @State private var exportFormat = "json"
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 12) {
                AppMark().frame(width: 40, height: 40).accessibilityHidden(true)
                Text(localization.t("mcp.title")).font(.title2.bold())
                Spacer()
                LanguagePicker().frame(maxWidth: 240)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Text(localization.t("mcp.intro"))
                    Text(localization.t("mcp.lifecycle")).foregroundStyle(.secondary)
                    ForEach(MCPClient.allCases, id: \.self) { client in clientCard(client) }
                    GroupBox {
                        VStack(alignment: .leading, spacing: 12) {
                            HStack {
                                Text(localization.t("mcp.startupCheck")).font(.headline)
                                Spacer()
                                Button(localization.t("mcp.checkService")) { Task { await model.check(consent: consent) } }
                                    .disabled(model.busy || !consent.accepted)
                            }
                            Text(localization.t(model.checkResult.map { $0.code == .passed ? "mcp.checkPassed" : "mcp.checkFailed" } ?? "mcp.notChecked"))
                            if let result = model.checkResult, result.code != .passed {
                                Text(localization.t(result.code == .timedOut ? "mcp.timeout" : "mcp.serviceError"))
                                DiagnosticsView(details: "\(result.code.rawValue)\n" + result.tools.sorted().joined(separator: "\n"))
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(8)
                    }
                    GroupBox(localization.t("mcp.advanced")) {
                        VStack(alignment: .leading, spacing: 14) {
                            Picker(localization.t("mcp.mode"), selection: $model.mode) {
                                Text(localization.t("mcp.readOnlyMode")).tag(MCPAccessMode.readOnly)
                                Text(localization.t("mcp.applyMode")).tag(MCPAccessMode.applyEnabled)
                            }.disabled(model.busy)
                            Text(localization.t("mcp.modeHint")).font(.callout).foregroundStyle(.secondary)
                            LabeledContent(localization.t("mcp.executable")) { Text(model.enginePath).textSelection(.enabled) }
                            LabeledContent(localization.t("mcp.transport"), value: "stdio")
                            Picker(localization.t("mcp.configuration"), selection: $exportFormat) {
                                Text("Cursor / Claude JSON").tag("json")
                                Text("Codex TOML").tag("toml")
                                Text(localization.t("mcp.copyVSCode")).tag("vscode")
                                Text(localization.t("mcp.copyCopilot")).tag("copilot")
                                Text(localization.t("mcp.copyLaunch")).tag("launch")
                            }
                            ScrollView(.horizontal) {
                                Text(exportText).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                            }.background(.background, in: RoundedRectangle(cornerRadius: 6))
                            Button(localization.t(copied ? "common.copied" : "common.copy")) {
                                copied = setup.copy(format: exportFormat)
                            }
                        }.padding(8)
                    }
                }.padding(.trailing, 8)
            }
            HStack {
                Button(localization.t("mcp.refresh")) { Task { await model.refresh(consent: consent) } }
                    .disabled(model.busy || !consent.accepted)
                if model.busy { ProgressView().controlSize(.small); Text(localization.t("common.working")).foregroundStyle(.secondary) }
                Spacer()
                Button(localization.t("common.close")) { dismiss() }.keyboardShortcut(.cancelAction)
            }
        }
        .padding(24).frame(minWidth: 680, idealWidth: 780, maxWidth: 920, minHeight: 580, idealHeight: 760, maxHeight: 860)
        .onDisappear { model.dismissPanel() }
        .task { await model.refresh(consent: consent) }
        .onChange(of: exportText) { _, _ in copied = false }
    }

    private var setup: MCPSetup { model.exportConfiguration }
    private var exportText: String { setup.text(format: exportFormat) }

    @ViewBuilder private func clientCard(_ client: MCPClient) -> some View {
        let detection = model.detections[client]
        let query = model.queries[client]
        let feedback = model.feedback(for: client)
        let name = localization.t("mcp." + client.rawValue)
        GroupBox {
            VStack(alignment: .leading, spacing: 12) {
                Text(name).font(.headline)
                LabeledContent(localization.t("mcp.installation"), value: localization.t(model.installationKey(for: client)))
                if let path = detection?.executablePath {
                    LabeledContent(localization.t("mcp.executable")) {
                        Text(path).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    }
                }
                if let query, query.scope == .other {
                    LabeledContent(localization.t("mcp.userRegistration"), value: localization.t(configurationKey(query.userConfiguration)))
                    if let mode = query.userMode {
                        LabeledContent(localization.t("mcp.mode"), value: localization.t(mode == .readOnly ? "mcp.readOnlyMode" : "mcp.applyMode"))
                    }
                    Text(localization.t("mcp.projectConfiguration")).font(.subheadline.bold())
                }
                LabeledContent(localization.t("mcp.configuration"), value: localization.t(configurationKey(query?.configuration)))
                if let mode = query?.mode {
                    LabeledContent(localization.t("mcp.mode"), value: localization.t(mode == .readOnly ? "mcp.readOnlyMode" : "mcp.applyMode"))
                }
                LabeledContent(localization.t("mcp.connection"), value: localization.t(connectionKey(query)))
                if detection?.availability == .notFound {
                    Text(localization.t("mcp.clientMissing", ["client": name])).foregroundStyle(.secondary)
                    Link(localization.t("mcp.installGuide"), destination: URL(string: client == .codex ? "https://developers.openai.com/codex/cli/" : "https://code.claude.com/docs/en/setup")!)
                }
                HStack {
                    Button(localization.t(model.registrationActionKey(for: client))) {
                        Task { await model.register(client, consent: consent) }
                    }.disabled(model.busy || !consent.accepted || detection?.availability != .available)
                    Button(localization.t("mcp.chooseExecutable")) {
                        let panel = NSOpenPanel()
                        panel.canChooseDirectories = false
                        panel.allowsMultipleSelection = false
                        if panel.runModal() == .OK, let url = panel.url { Task { await model.choose(url.path, for: client, consent: consent) } }
                    }.disabled(model.busy || !consent.accepted)
                }
                if let feedback {
                    Text(localization.t(feedback.primaryKey, ["client": name]))
                    if let key = feedback.recoveryKey { Text(localization.t(key)) }
                    if let details = feedback.details { DiagnosticsView(details: details) }
                }
            }.frame(maxWidth: .infinity, alignment: .leading).padding(8)
        }
    }

    private func configurationKey(_ state: MCPConfigurationState?) -> String {
        switch state {
        case .configured: return "mcp.configured"
        case .notConfigured: return "mcp.notConfigured"
        default: return "common.unknown"
        }
    }
    private func connectionKey(_ query: MCPClientQuery?) -> String {
        switch query?.connection {
        case .connected: return "mcp.connected"
        case .disconnected: return "mcp.disconnected"
        default: return "mcp.connectionUnknown"
        }
    }
}
