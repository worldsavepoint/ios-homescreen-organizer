import Darwin
import Foundation

/// A canonical JSON object (including unknown fields) or an observed missing key.
enum MCPOwnedEntry: Equatable, Codable {
    case absent
    case present(String)

    init(_ object: Any?) throws {
        guard let object else { self = .absent; return }
        guard let dictionary = object as? NSDictionary else { throw MCPClientOperationCode.snapshotReadFailed }
        self = .present(try MCPJSON.text(dictionary))
    }

    private enum CodingKeys: String, CodingKey { case absent, present }
    private enum ValueKeys: String, CodingKey { case value = "_0" }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        if container.contains(.absent) { self = .absent; return }
        let value = try container.nestedContainer(keyedBy: ValueKeys.self, forKey: .present)
        let json = try value.decode(String.self, forKey: .value)
        try self.init(MCPJSON.decode(Data(json.utf8)))
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .absent: _ = container.nestedContainer(keyedBy: ValueKeys.self, forKey: .absent)
        case .present(let json):
            var value = container.nestedContainer(keyedBy: ValueKeys.self, forKey: .present)
            try value.encode(json, forKey: .value)
        }
    }

    private static func validateWritable(_ dictionary: NSDictionary) throws {
        // Merging touches the owned root and its environment as Swift dictionaries.
        // Distinct Unicode scalar spellings can collapse to one Swift key.
        for fields in [dictionary, dictionary["env"] as? NSDictionary].compactMap({ $0 }) {
            guard let keys = fields.allKeys as? [String], Set(keys).count == fields.count else {
                throw MCPClientOperationCode.snapshotReadFailed
            }
        }
    }

    static func == (left: Self, right: Self) -> Bool {
        switch (left, right) {
        case (.absent, .absent): return true
        case let (.present(a), .present(b)): return a.utf8.elementsEqual(b.utf8)
        default: return false
        }
    }

    func dictionary() throws -> NSDictionary? {
        guard case .present(let json) = self else { return nil }
        guard let dictionary = try MCPJSON.decode(Data(json.utf8)) as? NSDictionary else {
            throw MCPClientOperationCode.snapshotReadFailed
        }
        return dictionary
    }

    func object() throws -> [String: Any]? {
        guard let raw = try dictionary() else { return nil }
        try Self.validateWritable(raw)
        guard let value = raw as? [String: Any] else { throw MCPClientOperationCode.snapshotReadFailed }
        return value
    }

    static func fields(in container: [String: Any], table: String) throws -> NSDictionary? {
        guard let value = container[table] else { return nil }
        guard let entries = value as? NSDictionary else { throw MCPClientOperationCode.snapshotReadFailed }
        guard let object = entries[MCPClientRegistration.serverID] else { return nil }
        guard let fields = object as? NSDictionary else { throw MCPClientOperationCode.snapshotReadFailed }
        return fields
    }

    static func from(_ container: [String: Any], table: String) throws -> Self {
        guard let fields = try fields(in: container, table: table) else { return .absent }
        try validateWritable(fields)
        return try Self(fields)
    }
}

func mcpBytesEqual(_ left: String?, _ right: String?) -> Bool {
    left.map { Data($0.utf8) } == right.map { Data($0.utf8) }
}

func mcpArgumentsEqual(_ left: [String]?, _ right: [String]?) -> Bool {
    left.map { $0.map { Data($0.utf8) } } == right.map { $0.map { Data($0.utf8) } }
}

/// Configuration protocols preserve environment names with distinct UTF-8 spellings.
func mcpEnvironment(_ object: Any?) throws -> [Data: Data] {
    guard let object, !(object is NSNull) else { return [:] }
    guard let fields = object as? NSDictionary else { throw MCPClientOperationCode.invalidResponse }
    var values = [Data: Data]()
    for (name, raw) in fields {
        guard let key = name as? String, let value = raw as? String else { throw MCPClientOperationCode.invalidResponse }
        values[Data(key.utf8)] = Data(value.utf8)
    }
    return values
}

struct MCPUserSnapshot {
    let file: String
    let entry: MCPOwnedEntry
    var version: String? = nil
    var scope: MCPClientScope = .unknown
    var effectiveEntry: MCPOwnedEntry? = nil
}

enum MCPCodexWriteError: Error {
    case notDispatched(MCPClientOperationCode)
    case rejectedBeforePersistence
    case versionRefused
    case terminalVersionRefusal(MCPClientOperationCode)
    case changedEntry(MCPOwnedEntry)
}

struct MCPEntryStore {
    let client: MCPClient
    let executable: String
    let environment: [String: String]
    let home: URL
    let timeout: TimeInterval
    let runner: MCPCommandRunner
    var cwd: String = FileManager.default.currentDirectoryPath
    var readOverride: (() async throws -> MCPUserSnapshot)? = nil
    var preflightOverride: (() async throws -> Void)? = nil
    var writeOverride: ((MCPUserSnapshot, MCPOwnedEntry) async throws -> String)? = nil

    func command(_ args: [String]) async throws {
        let result = await runner(executable, args, environment, timeout, cwd)
        if let failure = result.failure { throw failure }
        guard result.exitStatus == 0 else { throw MCPClientOperationCode.commandFailed }
    }

    private var codexFile: URL {
        (environment["CODEX_HOME"].flatMap { $0.hasPrefix("/") ? URL(fileURLWithPath: $0) : nil }
            ?? home.appendingPathComponent(".codex")).appendingPathComponent("config.toml").standardizedFileURL
    }

    private func exists(_ file: URL) throws -> Bool {
        var info = stat()
        if lstat(file.path, &info) == 0 { return true }
        if errno == ENOENT { return false }
        throw MCPClientOperationCode.snapshotReadFailed
    }

    /// Verify the selected binary's own schema, including the versioned restore method,
    /// before launching a CLI add that might need compensation.
    func preflight() async throws {
        if let preflightOverride { try await preflightOverride(); return }
        guard client == .codex else { return }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("mcp-schema-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let result = await runner(executable, ["app-server", "generate-json-schema", "--out", directory.path], environment, timeout, cwd)
        if let failure = result.failure { throw failure }
        guard result.exitStatus == 0,
              let data = try? Data(contentsOf: directory.appendingPathComponent("ClientRequest.json"), options: .mappedIfSafe),
              data.count <= MCPChildIO.limit, let schema = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let defs = schema["definitions"] as? [String: [String: Any]], let variants = schema["oneOf"] as? [[String: Any]] else {
            throw MCPClientOperationCode.unsupportedClient
        }
        func resolve(_ raw: Any?) -> [String: Any] {
            guard let value = raw as? [String: Any] else { return [:] }
            guard let ref = value["$ref"] as? String, ref.hasPrefix("#/definitions/") else { return value }
            return defs[String(ref.dropFirst("#/definitions/".count))] ?? [:]
        }
        func fields(_ method: String) -> [String: Any] {
            for variant in variants {
                let props = variant["properties"] as? [String: Any]
                if resolve(props?["method"])["enum"] as? [String] == [method] {
                    return resolve(props?["params"])["properties"] as? [String: Any] ?? [:]
                }
            }
            return [:]
        }
        let read = fields("config/read"), batch = fields("config/batchWrite")
        let edit = resolve(resolve(batch["edits"])["items"])["properties"] as? [String: Any]
        guard read["includeLayers"] != nil, read["cwd"] != nil, batch["filePath"] != nil, batch["expectedVersion"] != nil,
              edit?["value"] != nil, edit?["keyPath"] != nil, let strategies = resolve(edit?["mergeStrategy"])["enum"] as? [String],
              strategies.contains("replace") else { throw MCPClientOperationCode.unsupportedClient }
    }

    func read(effective: Bool = false, passive: Bool = false) async throws -> MCPUserSnapshot {
        if let readOverride { return try await readOverride() }
        if client == .claudeCode {
            let override = environment["CLAUDE_CONFIG_DIR"].flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0) }
            let legacy = (override ?? home.appendingPathComponent(".claude")).appendingPathComponent(".config.json")
            let customOAuth = !(environment["CLAUDE_CODE_CUSTOM_OAUTH_URL"] ?? "").isEmpty
            let url = try exists(legacy) ? legacy : (override ?? home).appendingPathComponent(customOAuth ? ".claude-custom-oauth.json" : ".claude.json")
            guard try exists(url) else { return MCPUserSnapshot(file: url.path, entry: .absent) }
            guard let root = try MCPJSON.decode(Data(contentsOf: url), deferNumbers: passive) as? [String: Any] else {
                throw MCPClientOperationCode.snapshotReadFailed
            }
            return try MCPUserSnapshot(file: url.path, entry: .from(root, table: "mcpServers"))
        }
        let result = try await MCPSubprocess.session(executable, ["app-server"], environment, timeout) { io in
            try initialize(&io)
            try io.send(["id": 2, "method": "config/read", "params": ["includeLayers": true, "cwd": effective ? cwd as Any : NSNull()]])
            return try io.receive(2, mcp: false, preserveNumbers: true)
        }
        guard let layers = result["layers"] as? [[String: Any]] else { throw MCPClientOperationCode.unsupportedClient }
        var user: MCPUserSnapshot?
        var sawUser = false
        let key = "mcp_servers." + MCPClientRegistration.serverID
        let origins = result["origins"] as? NSDictionary ?? [:]
        let relevant = origins.allKeys.compactMap { $0 as? String }.filter { $0 == key || $0.hasPrefix(key + ".") }
        var projectEntries: [String: NSDictionary] = [:]
        let userOrigin = !relevant.isEmpty && relevant.allSatisfy {
            guard let origin = origins[$0] as? [String: Any], let source = origin["name"] as? [String: Any] else { return false }
            return source["type"] as? String == "user" && source["file"] as? String == codexFile.path &&
                (source["profile"] == nil || source["profile"] is NSNull)
        }
        for layer in layers {
            guard let name = layer["name"] as? [String: Any], let kind = name["type"] as? String else { throw MCPClientOperationCode.unsupportedClient }
            if kind == "user" { sawUser = true }
            if let disabled = layer["disabledReason"], !(disabled is NSNull) { continue }
            if let profile = name["profile"], !(profile is NSNull) { continue }
            guard kind == "user" || kind == "project" else { continue }
            guard let config = layer["config"] as? [String: Any] else { throw MCPClientOperationCode.unsupportedClient }
            if kind == "project" {
                let fields = try MCPOwnedEntry.fields(in: config, table: "mcp_servers")
                if let folder = name["dotCodexFolder"] as? String, let fields { projectEntries[folder] = fields }
                continue
            }
            let entry = try MCPOwnedEntry.from(config, table: "mcp_servers")
            guard user == nil, let file = name["file"] as? String, file.hasPrefix("/"),
                  URL(fileURLWithPath: file).standardizedFileURL.path == codexFile.path,
                  let version = layer["version"] as? String, !version.isEmpty else { throw MCPClientOperationCode.unsupportedClient }
            if case .present(let json) = entry, json.contains("\"<redacted>\"") || json.contains("\"[REDACTED]\"") {
                throw MCPClientOperationCode.unsupportedClient
            }
            user = MCPUserSnapshot(file: file, entry: entry, version: version)
        }
        if user == nil {
            guard !sawUser, try !exists(codexFile) else { throw MCPClientOperationCode.unsupportedClient }
            user = MCPUserSnapshot(file: codexFile.path, entry: .absent)
        }
        var snapshot = user!
        if effective {
            guard let config = result["config"] as? [String: Any] else { throw MCPClientOperationCode.invalidResponse }
            let fields = try MCPOwnedEntry.fields(in: config, table: "mcp_servers")
            snapshot.effectiveEntry = try MCPOwnedEntry(fields)
            let saved = try snapshot.entry.object() ?? [:], active = fields ?? [:]
            var changedPaths: [[String]] = []
            let defaults: [String: Any] = ["enabled": true, "environment_id": "local", "args": [String](), "env_vars": [String]()]
            func registration(_ fields: NSDictionary) throws -> MCPOwnedEntry {
                let values = fields.mutableCopy() as! NSMutableDictionary
                for (name, value) in defaults where values[name] == nil { values[name] = value }
                return try MCPOwnedEntry(values)
            }
            // Compare registration semantics, ignoring spelling differences between
            // layer TOML defaults and the typed effective configuration.
            for field in ["command", "args", "url", "enabled", "environment_id", "env_vars"] {
                let a = saved[field] ?? defaults[field], b = active[field] ?? defaults[field]
                if try MCPOwnedEntry(["v": a ?? NSNull()]) != MCPOwnedEntry(["v": b ?? NSNull()]) {
                    changedPaths.append([key + "." + field, key])
                }
            }
            let lhs = try mcpEnvironment(saved["env"]), rhs = try mcpEnvironment(active["env"])
            let environmentPath = key + ".env"
            for name in Set(lhs.keys).union(rhs.keys) where lhs[name] != rhs[name] {
                // A variable name can contain periods. It is a single key in the environment table.
                changedPaths.append([environmentPath + "." + String(decoding: name, as: UTF8.self), environmentPath, key])
            }
            let projectExplainsChange = !changedPaths.isEmpty && changedPaths.allSatisfy { paths in
                for path in paths {
                    guard let raw = origins[path] else { continue }
                    guard let entry = raw as? [String: Any], let origin = entry["name"] as? [String: Any],
                          origin["type"] as? String == "project", let file = origin["dotCodexFolder"] as? String else { return false }
                    return projectEntries[file] != nil
                }
                return false
            }
            let completeProject: Bool
            if changedPaths.isEmpty, let table = origins[key] as? [String: Any],
               let source = table["name"] as? [String: Any], source["type"] as? String == "project",
               let folder = source["dotCodexFolder"] as? String, let project = projectEntries[folder],
               fields != nil, try registration(project) == registration(active) {
                completeProject = relevant.allSatisfy { path in
                    guard let entry = origins[path] as? [String: Any], let origin = entry["name"] as? [String: Any] else { return false }
                    return origin["type"] as? String == "project" && origin["dotCodexFolder"] as? String == folder
                }
            } else { completeProject = false }
            snapshot.scope = projectExplainsChange || completeProject ? .other : (userOrigin ? .user : .unknown)
        }
        return snapshot
    }

    private func initialize(_ io: inout MCPChildIO) throws {
        try io.send(["id": 1, "method": "initialize", "params": ["clientInfo": [
            "name": "screen_organizer", "title": "Screen Organizer", "version": "1.3.0"]]])
        let handshake = try io.receive(1, mcp: false)
        guard handshake["userAgent"] is String else { throw MCPClientOperationCode.unsupportedClient }
        try io.send(["method": "initialized", "params": [:]])
    }

    @discardableResult func replaceCodex(_ snapshot: MCPUserSnapshot, with desired: MCPOwnedEntry, retryUnrelated: Bool = true) async throws -> String {
        do { return try await write(snapshot, desired) }
        catch MCPCodexWriteError.versionRefused {
            guard retryUnrelated else { throw MCPCodexWriteError.terminalVersionRefusal(.recoveryConflict) }
            let fresh: MCPUserSnapshot
            do { fresh = try await read() }
            catch { throw MCPCodexWriteError.terminalVersionRefusal(error as? MCPClientOperationCode ?? .snapshotReadFailed) }
            guard fresh.file == snapshot.file else { throw MCPCodexWriteError.terminalVersionRefusal(.recoveryConflict) }
            guard fresh.entry == snapshot.entry else { throw MCPCodexWriteError.changedEntry(fresh.entry) }
            guard fresh.version != nil, fresh.version != snapshot.version else { throw MCPCodexWriteError.terminalVersionRefusal(.recoveryConflict) }
            do { return try await write(fresh, desired) }
            catch MCPCodexWriteError.versionRefused { throw MCPCodexWriteError.terminalVersionRefusal(.recoveryConflict) }
            catch MCPCodexWriteError.notDispatched(let code) { throw MCPCodexWriteError.terminalVersionRefusal(code) }
        }
    }

    private func write(_ snapshot: MCPUserSnapshot, _ prior: MCPOwnedEntry) async throws -> String {
        guard let version = snapshot.version else { throw MCPCodexWriteError.notDispatched(.recoveryConflict) }
        if let writeOverride {
            do { return try await writeOverride(snapshot, prior) }
            catch MCPClientOperationCode.versionConflict { throw MCPCodexWriteError.versionRefused }
        }
        // The session throws for setup or an unsent batch. Once bytes reached the
        // pipe, return the exchange's own outcome, including an indeterminate error.
        let reply: Result<[String: Any], Error>
        do {
            let edit: [String: Any] = ["keyPath": "mcp_servers." + MCPClientRegistration.serverID,
                                       "mergeStrategy": "replace", "value": try prior.object() as Any? ?? NSNull()]
            let request = try MCPJSON.text(["id": 2, "method": "config/batchWrite",
                "params": ["edits": [edit], "expectedVersion": version, "filePath": snapshot.file]])
            reply = try await MCPSubprocess.session(executable, ["app-server"], environment, timeout) { io in
                try initialize(&io)
                var started = false
                do {
                    try io.send(Data(request.utf8)) { started = true }
                    return .success(try io.receive(2, mcp: false, codexBatchWrite: true))
                } catch where started { return .failure(error) }
            }
        } catch { throw MCPCodexWriteError.notDispatched(error as? MCPClientOperationCode ?? .snapshotReadFailed) }
        let result = try reply.get()
        guard ["ok", "okOverridden"].contains(result["status"] as? String ?? ""),
              result["filePath"] as? String == snapshot.file, let revision = result["version"] as? String, !revision.isEmpty else { throw MCPClientOperationCode.recoveryFailed }
        return revision
    }
}
