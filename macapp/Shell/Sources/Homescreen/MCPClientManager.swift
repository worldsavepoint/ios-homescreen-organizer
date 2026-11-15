import Foundation
import Darwin

enum MCPClient: String, CaseIterable, Sendable {
    case codex, claudeCode

    var executableName: String { self == .codex ? "codex" : "claude" }
}

enum MCPAccessMode: String, Sendable {
    case readOnly, applyEnabled
}

enum MCPClientRegistration {
    static let serverID = "ios-homescreen-mcp"

    static func arguments(client: MCPClient, executablePath: String) -> [String] {
        arguments(client: client, executablePath: executablePath, mode: .readOnly)
    }

    static func arguments(client: MCPClient, executablePath: String, mode: MCPAccessMode) -> [String] {
        arguments(client: client, executablePath: executablePath, gates: gates(for: mode))
    }

    fileprivate static func gates(for mode: MCPAccessMode) -> [String: String] {
        mode == .readOnly ? ["HOMESCREEN_READ_ONLY":"1"] : ["HOMESCREEN_READ_ONLY":"0", "HOMESCREEN_ALLOW_APPLY":"1"]
    }

    fileprivate static func arguments(client: MCPClient, executablePath: String, gates: [String: String]) -> [String] {
        var arguments = ["mcp", "add"]
        if client == .codex { arguments += [serverID] }
        else { arguments += ["--scope", "user"] }
        for name in ["HOMESCREEN_READ_ONLY", "HOMESCREEN_ALLOW_APPLY"] {
            if let value = gates[name] { arguments += ["--env", name + "=" + value] }
        }
        // Claude's --env is variadic; the transport option ends that argument group.
        if client == .claudeCode { arguments += ["--transport", "stdio", serverID] }
        return arguments + ["--", executablePath, "mcp"]
    }
}

enum MCPClientAvailability: String, Sendable { case available, notFound, notExecutable }
enum MCPConfigurationState: String, Sendable { case configured, notConfigured, unknown }
enum MCPConnectionState: String, Sendable { case connected, disconnected, unknown }
enum MCPClientScope: String, Sendable { case user, other, unknown }
enum MCPClientOperationCode: String, Error, Sendable {
    case success, notFound, notExecutable, launchFailed, timedOut, commandFailed, invalidResponse
    case snapshotReadFailed, recoveryFailed, unsupportedClient, remoteConflict, recoveryConflict, versionConflict, cancelled, operationBusy
}
enum MCPRegistrationRecovery: String, Sendable { case notNeeded, restored, failed }

struct MCPClientDetection: Sendable {
    let client: MCPClient
    let availability: MCPClientAvailability
    let executablePath: String?
}

struct MCPClientQuery: Sendable {
    let client: MCPClient
    var code: MCPClientOperationCode
    var configuration: MCPConfigurationState
    let connection: MCPConnectionState
    var mode: MCPAccessMode? = nil
    var scope: MCPClientScope = .unknown
    var userMode: MCPAccessMode? = nil
    var userConfiguration: MCPConfigurationState = .unknown
    var command: String? = nil
    var arguments: [String]? = nil
    var localEnabled = false
    var url: String? = nil
    var enabled: Bool? = nil
    var transportEnvironment: [Data: Data] = [:]
    var forwardedNames: Set<Data> = []
}

struct MCPClientRegistrationResult: Sendable {
    let client: MCPClient
    let mode: MCPAccessMode
    let code: MCPClientOperationCode
    let query: MCPClientQuery?
    var recovery: MCPRegistrationRecovery = .notNeeded
    var cleanupPending = false
}

struct MCPCommandResult: Sendable {
    let exitStatus: Int32
    var stdout: String = ""
    var stderr: String = ""
    var failure: MCPClientOperationCode? = nil
}

typealias MCPCommandRunner = (String, [String], [String: String], TimeInterval, String) async -> MCPCommandResult
typealias MCPEntryStoreFactory = (MCPClient, String, [String: String], URL, TimeInterval, @escaping MCPCommandRunner) -> MCPEntryStore

/// Locates executables without invoking a login shell or evaluating paths as code.
struct MCPExecutableLocator {
    let environment: [String: String]
    let homeDirectory: URL
    var systemDirectories = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    var applicationDirectories: [String]? = nil

    func locate(_ client: MCPClient) -> String? {
        let home = homeDirectory.path
        let inherited = (environment["PATH"] ?? "").split(separator: ":").map(String.init)
        var directories = inherited + ["\(home)/.local/bin", "\(home)/.npm-global/bin",
            "\(home)/.npm/bin", "\(home)/.bun/bin", "\(home)/.volta/bin", "\(home)/bin"] + systemDirectories
        let nvm = homeDirectory.appendingPathComponent(".nvm/versions/node")
        if let versions = try? FileManager.default.contentsOfDirectory(atPath: nvm.path) {
            directories += versions.sorted().reversed().map { nvm.appendingPathComponent($0).appendingPathComponent("bin").path }
        }
        var candidates = directories.map { URL(fileURLWithPath: $0).appendingPathComponent(client.executableName).path }
        if client == .codex {
            for root in applicationDirectories ?? ["\(home)/Applications", "/Applications"] {
                candidates += ["\(root)/Codex.app/Contents/Resources/codex",
                    "\(root)/Codex.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
                    "\(root)/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"]
            }
        }
        return candidates.first(where: Self.isExecutable)
    }

    static func isExecutable(_ path: String) -> Bool {
        var isDirectory: ObjCBool = false
        return FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory) &&
            !isDirectory.boolValue && FileManager.default.isExecutableFile(atPath: path)
    }
}

struct MCPClientManager {
    private let environment: [String: String]
    private let homeDirectory: URL
    private let timeout: TimeInterval
    private let executableLocator: (MCPClient) -> String?
    private let runner: MCPCommandRunner
    private let storeFactory: MCPEntryStoreFactory
    private let journalDirectory: URL
    private let journalFactory: (URL, MCPClient) throws -> MCPRegistrationJournal

    init(environment: [String: String] = ProcessInfo.processInfo.environment,
         homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser,
         timeout: TimeInterval = 15,
         executableLocator: ((MCPClient) -> String?)? = nil,
         runner: @escaping MCPCommandRunner = MCPProcessRunner.run,
         journalFactory: @escaping (URL, MCPClient) throws -> MCPRegistrationJournal = MCPRegistrationJournal.init,
         storeFactory: @escaping MCPEntryStoreFactory = { MCPEntryStore(client: $0, executable: $1, environment: $2, home: $3, timeout: $4, runner: $5) }) {
        self.environment = environment
        self.homeDirectory = homeDirectory
        self.timeout = max(0.01, timeout)
        self.executableLocator = executableLocator ?? MCPExecutableLocator(environment: environment, homeDirectory: homeDirectory).locate
        self.runner = runner
        self.storeFactory = storeFactory
        self.journalFactory = journalFactory
        self.journalDirectory = homeDirectory.appendingPathComponent("Library/Application Support/homescreen-mcp/registration")
    }

    func detect(client: MCPClient, executablePath: String? = nil) async -> MCPClientDetection {
        let path = executablePath ?? executableLocator(client)
        return MCPClientDetection(client: client, availability: path.map {
            MCPExecutableLocator.isExecutable($0) ? .available : .notExecutable
        } ?? .notFound, executablePath: path)
    }

    private func store(_ client: MCPClient, _ path: String) -> MCPEntryStore {
        var env = environment
        let directory = URL(fileURLWithPath: path).deletingLastPathComponent().path
        env["PATH"] = ([directory] + (env["PATH"].map { [$0] } ?? [])).joined(separator: ":")
        return storeFactory(client, path, env, homeDirectory, timeout, runner)
    }

    func query(client: MCPClient, executablePath: String? = nil) async -> MCPClientQuery {
        let detection = await detect(client: client, executablePath: executablePath)
        guard detection.availability == .available, let path = detection.executablePath else {
            return queryFailure(client, detection.availability == .notFound ? .notFound : .notExecutable)
        }
        let store = store(client, path)
        var result = await effectiveQuery(store)
        if result.code == .success, !Task.isCancelled, let saved = try? await store.read(effective: true) {
            annotate(&result, snapshot: saved)
        }
        return result
    }

    private func effectiveQuery(_ store: MCPEntryStore) async -> MCPClientQuery {
        let client = store.client
        let arguments = ["mcp", "get", MCPClientRegistration.serverID] + (client == .codex ? ["--json"] : [])
        let result = await runner(store.executable, arguments, store.environment, timeout, store.cwd)
        if let failure = result.failure { return queryFailure(client, failure) }
        if result.exitStatus != 0 {
            let text = (result.stdout + result.stderr).lowercased()
            let absent = text.contains("no mcp server found") || text.contains("no mcp server named") ||
                text.contains("no server named") || text.contains("not found with name")
            return MCPClientQuery(client: client, code: absent ? .success : .commandFailed,
                configuration: absent ? .notConfigured : .unknown, connection: .unknown)
        }
        if client == .codex {
            guard let data = result.stdout.data(using: .utf8),
                  let entry = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  entry["name"] as? String == MCPClientRegistration.serverID,
                  let transport = entry["transport"] as? [String: Any],
                  let kind = transport["type"] as? String,
                  let enabled = entry["enabled"] as? NSNumber, CFGetTypeID(enabled) == CFBooleanGetTypeID() else {
                return queryFailure(client, .invalidResponse)
            }
            let local = kind == "stdio"
            guard local ? transport["command"] is String : (["streamable_http", "sse"].contains(kind) && transport["url"] is String) else {
                return queryFailure(client, .invalidResponse)
            }
            var forwarded: Set<Data> = []
            guard let environment = try? mcpEnvironment(transport["env"]) else { return queryFailure(client, .invalidResponse) }
            if let names = transport["env_vars"], !(names is NSNull) {
                guard let options = try? forwardedOptions(names) else { return queryFailure(client, .invalidResponse) }
                forwarded = Set(options.map { Data($0.name.utf8) })
            }
            return MCPClientQuery(client: client, code: .success, configuration: .configured, connection: .unknown,
                command: transport["command"] as? String, arguments: transport["args"] as? [String], url: transport["url"] as? String, enabled: enabled.boolValue,
                transportEnvironment: environment, forwardedNames: forwarded)
        }
        let plainOutput = result.stdout.replacingOccurrences(of: "\u{1b}\\[[0-?]*[ -/]*[@-~]", with: "", options: .regularExpression)
        let lines = plainOutput.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespaces) }
        guard lines.contains("\(MCPClientRegistration.serverID):"), lines.contains(where: { $0.hasPrefix("Command:") || $0.hasPrefix("URL:") }) else {
            return queryFailure(client, .invalidResponse)
        }
        let status = lines.first(where: { $0.hasPrefix("Status:") })?.dropFirst("Status:".count)
            .trimmingCharacters(in: .whitespaces).lowercased() ?? ""
        let connection: MCPConnectionState = status.contains("failed") || status.contains("disconnected") ||
            status.contains("not connected") || status.contains("needs authentication") ||
            ["connection error", "✘ connection error"].contains(status) ? .disconnected :
            (["connected", "✓ connected", "✔ connected"].contains(status) ? .connected : .unknown)
        func field(_ key: String) -> String? {
            lines.first(where: { $0.hasPrefix(key + ":") }).map { $0.dropFirst(key.count + 1).trimmingCharacters(in: .whitespaces) }
        }
        let label = field("Scope")?.lowercased().split(whereSeparator: { $0 == " " || $0 == "(" }).first
        let scope: MCPClientScope = label == "user" ? .user : (label == "project" || label == "local" ? .other : .unknown)
        let env = Dictionary(lines.compactMap { line -> (String, String)? in
            let pair = line.split(separator: "=", maxSplits: 1).map(String.init)
            return pair.count == 2 && pair[0].hasPrefix("HOMESCREEN_") ? (pair[0], pair[1]) : nil
        }, uniquingKeysWith: { _, last in last })
        return MCPClientQuery(client: client, code: .success, configuration: .configured, connection: connection,
            mode: accessMode(["env": env]), scope: scope, command: field("Command"),
            arguments: field("Args")?.split(separator: " ").map(String.init),
            localEnabled: field("Command") != nil && ["stdio", nil].contains(field("Type")?.lowercased()))
    }

    private func queryFailure(_ client: MCPClient, _ code: MCPClientOperationCode) -> MCPClientQuery {
        MCPClientQuery(client: client, code: code, configuration: .unknown, connection: .unknown)
    }

    private func forwardedOptions(_ value: Any) throws -> [(name: String, option: Any)] {
        guard let options = value as? [Any] else { throw MCPClientOperationCode.snapshotReadFailed }
        return try options.map { option in
            guard let name = (option as? String) ?? (option as? [String: Any])?["name"] as? String,
                  !name.isEmpty else { throw MCPClientOperationCode.snapshotReadFailed }
            return (name, option)
        }
    }

    private let gateNames: Set<String> = ["HOMESCREEN_READ_ONLY", "HOMESCREEN_ALLOW_APPLY"]

    private func accessMode(_ entry: [String: Any]) -> MCPAccessMode? {
        guard let env = entry["env"] as? [String: String] else { return nil }
        if let forwarded = entry["env_vars"], !(forwarded is NSNull) {
            guard let options = try? forwardedOptions(forwarded), options.allSatisfy({ !gateNames.contains($0.name) }) else { return nil }
        }
        if env["HOMESCREEN_READ_ONLY"] == "1", [nil, "0"].contains(env["HOMESCREEN_ALLOW_APPLY"]) { return .readOnly }
        if env["HOMESCREEN_READ_ONLY"] == "0", env["HOMESCREEN_ALLOW_APPLY"] == "1" { return .applyEnabled }
        return nil
    }

    private func localMode(_ entry: [String: Any]) -> MCPAccessMode? {
        if let enabled = entry["enabled"] {
            guard let number = enabled as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID(), number.boolValue else { return nil }
        }
        guard entry["environment_id"] == nil || entry["environment_id"] as? String == "local",
              entry["url"] == nil, entry["command"] is String,
              entry["type"] == nil || entry["type"] as? String == "stdio" else { return nil }
        return accessMode(entry)
    }

    private func annotate(_ query: inout MCPClientQuery, snapshot: MCPUserSnapshot) {
        let object = try? snapshot.entry.object()
        query.userConfiguration = object == nil ? .notConfigured : .configured
        query.userMode = object.flatMap(localMode)
        if query.client == .claudeCode, query.scope == .user, query.configuration == .configured {
            guard let object else {
                query.code = .invalidResponse; query.configuration = .unknown; query.localEnabled = false; query.mode = nil
                return
            }
            query.command = object["command"] as? String
            query.arguments = object["args"] as? [String]
            query.url = object["url"] as? String
            query.localEnabled = query.command != nil && query.url == nil &&
                (object["type"] == nil || object["type"] as? String == "stdio") &&
                (object["enabled"] == nil || object["enabled"] as? Bool == true)
            query.mode = query.localEnabled ? localMode(object) : nil
        }
        guard query.client == .codex, let observed = snapshot.effectiveEntry else { return }
        guard let fields = try? observed.dictionary(), let active = fields as? [String: Any] else {
            query.localEnabled = false; query.mode = nil; return
        }
        let enabled = active["enabled"] as? Bool ?? true
        let local = active["url"] == nil
        let activeEnv = try? mcpEnvironment(fields["env"])
        let activeForwarded = (try? forwardedOptions(active["env_vars"] ?? []).map(\.name)) ?? []
        let agrees = query.enabled == enabled && mcpBytesEqual(query.command, active["command"] as? String) &&
            mcpArgumentsEqual(query.arguments, active["args"] as? [String]) && mcpBytesEqual(query.url, active["url"] as? String) &&
            (!local || (query.transportEnvironment == activeEnv && query.forwardedNames == Set(activeForwarded.map { Data($0.utf8) })))
        if !agrees {
            query.code = .invalidResponse
            query.configuration = .unknown
            query.localEnabled = false
            query.mode = nil
            return
        }
        query.scope = snapshot.scope
        query.localEnabled = local && enabled && (active["environment_id"] == nil || active["environment_id"] as? String == "local")
        query.mode = query.localEnabled ? localMode(active) : nil
    }

    private func checkLaunchEnvelope(_ args: [String], store: MCPEntryStore) throws {
        let values = [store.executable] + args + store.environment.map { $0.key + "=" + $0.value }
        let bytes = values.reduce(0) { $0 + $1.utf8.count + 1 }
        // Darwin also puts argv/env pointers, executable and interpreter names,
        // alignment and auxiliary strings on the exec stack.
        let reserve = (values.count + 2) * MemoryLayout<UnsafeRawPointer>.size + Int(PATH_MAX) * 2 + 4096
        guard !values.contains(where: { $0.utf8.contains(0) }), bytes + reserve < sysconf(_SC_ARG_MAX) else {
            throw MCPClientOperationCode.launchFailed
        }
    }

    private func isRemote(_ entry: MCPOwnedEntry) throws -> Bool {
        guard let object = try entry.object() else { return false }
        return object["url"] != nil || object["command"] as? String == nil ||
            (object["type"] != nil && object["type"] as? String != "stdio")
    }

    private func mergedEntry(client: MCPClient, prior: MCPOwnedEntry, enginePath: String, gates: [String: String]) throws -> MCPOwnedEntry {
        var object = try prior.object() ?? [:]
        if client == .codex { object["environment_id"] = "local" }
        if object["url"] != nil {
            for field in ["url", "http_headers", "env_http_headers", "bearer_token_env_var", "http_headers_helper", "scopes", "oauth_resource", "oauth", "auth"] {
                object.removeValue(forKey: field)
            }
        }
        if object["type"] != nil { object["type"] = "stdio" }
        if object["enabled"] != nil { object["enabled"] = true }
        object["command"] = enginePath
        object["args"] = ["mcp"]
        var env: [String: String] = [:]
        if let existing = object["env"] {
            guard let values = existing as? [String: String] else { throw MCPClientOperationCode.snapshotReadFailed }
            env = values
        }
        env.removeValue(forKey: "HOMESCREEN_ALLOW_APPLY")
        env.merge(gates) { _, new in new }
        object["env"] = env
        if let forwarded = object["env_vars"] {
            object["env_vars"] = try forwardedOptions(forwarded).filter { !gateNames.contains($0.name) }.map(\.option)
        }
        return try MCPOwnedEntry(object)
    }

    private func matches(_ current: MCPOwnedEntry, intended: MCPOwnedEntry, client: MCPClient) throws -> Bool {
        if current == intended { return true }
        guard client == .claudeCode, var value = try intended.object(), value["type"] == nil else { return false }
        value["type"] = "stdio"
        return try current == MCPOwnedEntry(value)
    }

    private func ownsClaudePostimage(_ entry: MCPOwnedEntry, record: MCPRegistrationRecord) throws -> Bool {
        if record.phase == .removing { return entry == .absent }
        return try record.phase == .adding && (entry == .absent || matches(entry, intended: record.intended, client: .claudeCode))
    }

    private func matchesBootstrap(_ entry: MCPOwnedEntry, record: MCPRegistrationRecord) throws -> Bool {
        // Earlier journals used the final requested gates for the first CLI entry.
        guard var expected = try (record.bootstrapExpected ?? record.intended).object() else { return false }
        expected.removeValue(forKey: "environment_id")
        if try entry == MCPOwnedEntry(expected) { return true }
        expected["environment_id"] = "local"
        return try entry == MCPOwnedEntry(expected)
    }

    private struct JournalSaveFailure: Error {}
    private struct PriorRestored: Error {}

    private func savePhase(_ phase: MCPRegistrationPhase, record: inout MCPRegistrationRecord,
                           journal: MCPRegistrationJournal) throws {
        var next = record
        next.phase = phase
        do { try journal.save(next) } catch { throw JournalSaveFailure() }
        record = next
    }

    private func markConflict(_ record: inout MCPRegistrationRecord, journal: MCPRegistrationJournal,
                              code: MCPClientOperationCode = .recoveryConflict) throws -> Never {
        if journal.client == .codex, record.phase != .conflicted {
            try savePhase(.conflicted, record: &record, journal: journal)
        }
        throw code
    }

    private func changedEntry(_ entry: MCPOwnedEntry, record: inout MCPRegistrationRecord,
                              journal: MCPRegistrationJournal) throws -> Never {
        // Settle this observation before a later read can see a different value.
        if journal.client == .codex, entry == record.prior {
            do { try journal.clear() } catch { throw JournalSaveFailure() }
            throw PriorRestored()
        }
        try markConflict(&record, journal: journal)
    }

    private func restore(_ record: inout MCPRegistrationRecord, journal: MCPRegistrationJournal, store: MCPEntryStore) async throws {
        if record.phase == .committed { try journal.clear(); return }
        let current = try await store.read()
        guard current.file == record.file else { throw MCPClientOperationCode.recoveryConflict }
        if current.entry == record.prior { try journal.clear(); return }
        let permitted: Bool
        switch record.phase {
        case .prepared, .committed, .conflicted, .bootstrapUndoPending: permitted = false
        case .acknowledged: permitted = current.entry == record.intended
        case .versionedWritePending:
            // A content hash can recur after an applied write and an external edit.
            permitted = current.entry == record.intended && current.entry != record.observed
        case .bootstrapObserved, .versionedWriteRejected:
            permitted = store.client == .codex && record.prior == .absent && current.entry == record.observed &&
                record.bootstrapVersion != nil && current.version == record.bootstrapVersion
        case .bootstrapping:
            permitted = try store.client == .codex && record.prior == .absent && current.version != nil &&
                matchesBootstrap(current.entry, record: record)
            if permitted {
                if record.bootstrapExpected == nil { record.bootstrapExpected = record.intended }
                record.observed = current.entry
                record.bootstrapVersion = current.version
                try savePhase(.bootstrapObserved, record: &record, journal: journal)
                try await restore(&record, journal: journal, store: store)
                return
            }
        case .removing: permitted = current.entry == .absent
        case .adding:
            permitted = try store.client == .claudeCode && (current.entry == .absent || matches(current.entry, intended: record.intended, client: store.client))
        case .observed, .restoring: permitted = current.entry == record.observed ||
                (record.phase == .restoring && store.client == .claudeCode && current.entry == .absent)
        case .restoreRemoved: permitted = current.entry == .absent
        }
        guard permitted else { try markConflict(&record, journal: journal) }
        if store.client == .claudeCode, case .present(let json) = record.prior {
            try checkLaunchEnvelope(["mcp", "add-json", "--scope", "user", MCPClientRegistration.serverID, json], store: store)
        }
        let strictBootstrap = record.phase == .bootstrapObserved || record.phase == .versionedWriteRejected
        record.observed = current.entry
        try savePhase(strictBootstrap ? .bootstrapUndoPending : .restoring, record: &record, journal: journal)
        if store.client == .codex {
            do { try await store.replaceCodex(current, with: record.prior, retryUnrelated: !strictBootstrap) }
            catch MCPCodexWriteError.changedEntry(let entry) {
                if entry == record.prior { try journal.clear(); return }
                try markConflict(&record, journal: journal)
            } catch MCPCodexWriteError.terminalVersionRefusal(let code) {
                try savePhase(.conflicted, record: &record, journal: journal)
                let fresh = try await store.read()
                guard fresh.file == record.file, fresh.entry == record.prior else { throw code }
                try journal.clear()
                return
            } catch MCPCodexWriteError.notDispatched(let code) {
                throw code
            } catch MCPCodexWriteError.rejectedBeforePersistence { throw MCPClientOperationCode.commandFailed }
        } else {
            if current.entry != .absent {
                guard try !isRemote(current.entry) else { throw MCPClientOperationCode.remoteConflict }
                try await store.command(["mcp", "remove", "--scope", "user", MCPClientRegistration.serverID])
            }
            let removed = try await store.read()
            guard removed.file == record.file, removed.entry == .absent else { throw MCPClientOperationCode.recoveryConflict }
            try savePhase(.restoreRemoved, record: &record, journal: journal)
            if case .present(let json) = record.prior {
                try await store.command(["mcp", "add-json", "--scope", "user", MCPClientRegistration.serverID, json])
            }
        }
        let after = try await store.read()
        guard after.file == record.file else { throw MCPClientOperationCode.recoveryConflict }
        guard after.entry == record.prior else {
            if store.client == .codex { try markConflict(&record, journal: journal) }
            throw MCPClientOperationCode.recoveryFailed
        }
        try journal.clear()
    }

    func hasPending(_ client: MCPClient) -> Bool {
        var info = stat()
        return lstat(journalDirectory.appendingPathComponent(client.rawValue + ".json").path, &info) == 0 || errno != ENOENT
    }

    func recover(client: MCPClient, executablePath: String?) async -> MCPClientRegistrationResult? {
        guard hasPending(client) else { return nil }
        let detection = await detect(client: client, executablePath: executablePath)
        guard detection.availability == .available, let path = detection.executablePath else {
            return MCPClientRegistrationResult(client: client, mode: .readOnly, code: .recoveryFailed, query: nil, recovery: .failed)
        }
        let store = store(client, path)
        do {
            let journal = try journalFactory(journalDirectory, client)
            guard var record = try journal.read() else { return nil }
            try await restore(&record, journal: journal, store: store)
            return nil
        } catch {
            return MCPClientRegistrationResult(client: client, mode: .readOnly, code: error as? MCPClientOperationCode ?? .recoveryFailed, query: nil, recovery: .failed)
        }
    }

    func register(client: MCPClient, executablePath: String? = nil, enginePath: String,
                  mode: MCPAccessMode = .readOnly) async -> MCPClientRegistrationResult {
        func result(_ code: MCPClientOperationCode, _ query: MCPClientQuery? = nil,
                    _ recovery: MCPRegistrationRecovery = .notNeeded) -> MCPClientRegistrationResult {
            MCPClientRegistrationResult(client: client, mode: mode, code: code, query: query, recovery: recovery)
        }
        guard enginePath.hasPrefix("/") else { return result(.invalidResponse) }
        let detection = await detect(client: client, executablePath: executablePath)
        guard detection.availability == .available, let path = detection.executablePath else {
            return result(detection.availability == .notFound ? .notFound : .notExecutable)
        }
        let store = store(client, path)
        do {
            let journal = try journalFactory(journalDirectory, client)
            if var pending = try journal.read() {
                do { try await restore(&pending, journal: journal, store: store) }
                catch { return result(error as? MCPClientOperationCode ?? .recoveryFailed, nil, .failed) }
            }
            try await store.preflight()
            let snapshot = try await store.read()
            if client == .claudeCode {
                guard try !isRemote(snapshot.entry) else { return result(.remoteConflict) }
                let previous = await effectiveQuery(store)
                guard previous.code == .success else { return result(previous.code, previous) }
                if previous.configuration == .configured, previous.scope == .user, snapshot.entry == .absent { return result(.snapshotReadFailed, previous) }
            }
            let env = MCPClientRegistration.gates(for: mode)
            let base = try MCPOwnedEntry(["command": enginePath, "args": ["mcp"], "env": env])
            let intended = try mergedEntry(client: client, prior: snapshot.entry, enginePath: enginePath, gates: env)
            var add = MCPClientRegistration.arguments(client: client, executablePath: enginePath, mode: mode)
            if client == .claudeCode {
                let jsonPrefix = ["mcp", "add-json", "--scope", "user", MCPClientRegistration.serverID]
                if intended != base, case .present(let json) = intended { add = jsonPrefix + [json] }
                try checkLaunchEnvelope(add, store: store)
                if case .present(let json) = snapshot.entry { try checkLaunchEnvelope(jsonPrefix + [json], store: store) }
            }
            var record = MCPRegistrationRecord(client: client.rawValue, scope: "user", server: MCPClientRegistration.serverID,
                                               file: snapshot.file, prior: snapshot.entry, intended: intended)
            try journal.save(record)
            var awaitingPostimage = false
            do {
                if client == .claudeCode && snapshot.entry != .absent {
                    let beforeRemove = try await store.read()
                    guard beforeRemove.file == snapshot.file, beforeRemove.entry == snapshot.entry else {
                        throw MCPClientOperationCode.recoveryConflict
                    }
                    try savePhase(.removing, record: &record, journal: journal)
                    awaitingPostimage = true
                    try await store.command(["mcp", "remove", "--scope", "user", MCPClientRegistration.serverID])
                    awaitingPostimage = false
                }
                var before = try await store.read()
                guard before.file == snapshot.file else { throw MCPClientOperationCode.recoveryConflict }
                guard before.entry == (client == .claudeCode ? .absent : snapshot.entry) else {
                    try markConflict(&record, journal: journal)
                }
                if client == .codex && before.version == nil {
                    // The CLI may omit the executor. Keep Apply disabled until the
                    // versioned write installs both the local executor and final gates.
                    guard before.entry == .absent else { throw MCPClientOperationCode.recoveryConflict }
                    var gates = MCPClientRegistration.gates(for: .readOnly)
                    if mode == .applyEnabled { gates["HOMESCREEN_ALLOW_APPLY"] = "0" }
                    record.bootstrapExpected = try MCPOwnedEntry(["command": enginePath, "args": ["mcp"], "env": gates])
                    add = MCPClientRegistration.arguments(client: client, executablePath: enginePath, gates: gates)
                    try savePhase(.bootstrapping, record: &record, journal: journal)
                    var commandError: Error?
                    do { try await store.command(add) } catch { commandError = error }
                    before = try await store.read()
                    guard before.file == record.file else { throw MCPClientOperationCode.recoveryConflict }
                    if before.entry != .absent {
                        guard let revision = before.version else { throw MCPClientOperationCode.recoveryConflict }
                        record.observed = before.entry
                        record.bootstrapVersion = revision
                        try savePhase(.bootstrapObserved, record: &record, journal: journal)
                    }
                    if let commandError { throw commandError }
                    guard try matchesBootstrap(before.entry, record: record) else { throw MCPClientOperationCode.invalidResponse }
                }
                let acknowledged: String?
                if client == .codex {
                    try savePhase(.versionedWritePending, record: &record, journal: journal)
                    do { acknowledged = try await store.replaceCodex(before, with: intended) }
                    catch MCPCodexWriteError.rejectedBeforePersistence {
                        try savePhase(.versionedWriteRejected, record: &record, journal: journal)
                        throw MCPClientOperationCode.commandFailed
                    } catch MCPCodexWriteError.changedEntry(let entry) {
                        try changedEntry(entry, record: &record, journal: journal)
                    } catch MCPCodexWriteError.terminalVersionRefusal(let code), MCPCodexWriteError.notDispatched(let code) {
                        try markConflict(&record, journal: journal, code: code)
                    }
                    try savePhase(.acknowledged, record: &record, journal: journal)
                } else {
                    try savePhase(.adding, record: &record, journal: journal)
                    awaitingPostimage = true
                    try await store.command(add)
                    acknowledged = nil
                }
                let written = try await store.read()
                awaitingPostimage = false
                guard written.file == snapshot.file else { throw MCPClientOperationCode.recoveryConflict }
                if let acknowledged, written.version != acknowledged, written.entry != intended {
                    try changedEntry(written.entry, record: &record, journal: journal)
                }
                if client == .claudeCode, try !ownsClaudePostimage(written.entry, record: record) {
                    try markConflict(&record, journal: journal)
                }
                record.observed = written.entry
                try savePhase(.observed, record: &record, journal: journal)
                guard try matches(written.entry, intended: intended, client: client), let raw = try written.entry.object(), localMode(raw) == mode,
                      mcpBytesEqual(raw["command"] as? String, enginePath), mcpArgumentsEqual(raw["args"] as? [String], ["mcp"]) else {
                    throw MCPClientOperationCode.invalidResponse
                }
                var verification = await effectiveQuery(store)
                if client == .codex && verification.code == .success {
                    let context = try await store.read(effective: true)
                    guard context.file == written.file else { throw MCPClientOperationCode.recoveryConflict }
                    guard context.entry == written.entry else { try changedEntry(context.entry, record: &record, journal: journal) }
                    annotate(&verification, snapshot: context)
                } else { annotate(&verification, snapshot: written) }
                guard verification.code == .success else { throw verification.code }
                guard verification.configuration == .configured, verification.scope == .other ||
                        (verification.localEnabled && mcpBytesEqual(verification.command, enginePath) && mcpArgumentsEqual(verification.arguments, ["mcp"]) &&
                         verification.mode == mode) else { throw MCPClientOperationCode.invalidResponse }
                let final = try await store.read()
                guard final.file == written.file else { throw MCPClientOperationCode.recoveryConflict }
                guard final.entry == written.entry else { try changedEntry(final.entry, record: &record, journal: journal) }
                try savePhase(.committed, record: &record, journal: journal)
                var success = result(.success, verification)
                do { try journal.clear() } catch { success.cleanupPending = true }
                return success
            } catch is PriorRestored {
                return result(.recoveryConflict, await query(client: client, executablePath: path), .restored)
            } catch is JournalSaveFailure {
                // The rename may have succeeded before the directory sync failed.
                return result(.snapshotReadFailed, await query(client: client, executablePath: path), .failed)
            } catch {
                let failure = error as? MCPClientOperationCode ?? .snapshotReadFailed
                do {
                    if awaitingPostimage, failure != .recoveryConflict {
                        let post = try await store.read()
                        guard post.file == record.file else { throw MCPClientOperationCode.recoveryConflict }
                        guard try ownsClaudePostimage(post.entry, record: record) else {
                            throw MCPClientOperationCode.recoveryConflict
                        }
                        record.observed = post.entry
                        try savePhase(.observed, record: &record, journal: journal)
                    }
                    try await restore(&record, journal: journal, store: store)
                } catch {
                    return result(failure, await query(client: client, executablePath: path), .failed)
                }
                return result(failure, await query(client: client, executablePath: path), .restored)
            }
        } catch { return result(error as? MCPClientOperationCode ?? .snapshotReadFailed) }
    }
}
