import Foundation
import Darwin

enum MCPServiceCheckCode: String, Sendable {
    case passed, launchFailed, timedOut, protocolError, unexpectedServer, missingTools, cancelled
}

struct MCPServiceCheckResult: Sendable {
    let code: MCPServiceCheckCode
    var tools: Set<String> = []
}

/// Checks protocol startup and discovery using one read-only temporary-data child.
struct MCPServiceCheck {
    var timeout: TimeInterval = 45
    var environment: [String: String] = ProcessInfo.processInfo.environment

    private static func validTool(_ entry: [String: Any]) -> Bool {
        guard let name = entry["name"] as? String, !name.isEmpty,
              let schema = entry["inputSchema"] as? [String: Any], schema["type"] as? String == "object" else { return false }
        if let dialect = schema["$schema"], !(dialect is String) { return false }
        if let properties = schema["properties"], !(properties is [String: [String: Any]]) { return false }
        if let required = schema["required"], !(required is [String]) { return false }
        return true
    }

    func run(executablePath: String) async -> MCPServiceCheckResult {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("homescreen-mcp-check-\(UUID().uuidString)")
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        } catch { return MCPServiceCheckResult(code: .launchFailed) }
        defer { try? FileManager.default.removeItem(at: directory) }
        var childEnvironment = environment
        childEnvironment["HOMESCREEN_DATA"] = directory.path
        childEnvironment["HOMESCREEN_READ_ONLY"] = "1"
        childEnvironment.removeValue(forKey: "HOMESCREEN_ALLOW_APPLY")
        do {
            return try await MCPSubprocess.session(executablePath, ["mcp"], childEnvironment, max(0.01, timeout)) { io in
                func send(_ message: [String: Any]) throws {
                    try io.send(["jsonrpc": "2.0"].merging(message) { _, value in value })
                }
                try send(["id": 1, "method": "initialize", "params": ["protocolVersion": "2025-11-25",
                    "capabilities": [:], "clientInfo": ["name": "home-screen-organizer-startup-check", "version": "1.0.0"]]])
                let initialization = try io.receive(1, mcp: true)
                guard initialization["protocolVersion"] as? String == "2025-11-25",
                      let capabilities = initialization["capabilities"] as? [String: Any], capabilities["tools"] is [String: Any],
                      let info = initialization["serverInfo"] as? [String: Any], let version = info["version"] as? String,
                      !version.isEmpty, let name = info["name"] as? String, !name.isEmpty else { throw MCPClientOperationCode.invalidResponse }
                guard name == MCPClientRegistration.serverID else { return MCPServiceCheckResult(code: .unexpectedServer) }
                try send(["method": "notifications/initialized"])
                try send(["id": 2, "method": "tools/list", "params": [:]])
                let result = try io.receive(2, mcp: true)
                guard let entries = result["tools"] as? [[String: Any]], entries.allSatisfy(Self.validTool) else { throw MCPClientOperationCode.invalidResponse }
                let tools = Set(entries.compactMap { $0["name"] as? String })
                let required: Set<String> = ["read_phone", "show_layout", "move", "diff"]
                return MCPServiceCheckResult(code: required.isSubset(of: tools) ? .passed : .missingTools, tools: tools)
            }
        } catch MCPClientOperationCode.timedOut { return MCPServiceCheckResult(code: .timedOut) }
        catch MCPClientOperationCode.launchFailed { return MCPServiceCheckResult(code: .launchFailed) }
        catch MCPClientOperationCode.cancelled { return MCPServiceCheckResult(code: .cancelled) }
        catch { return MCPServiceCheckResult(code: .protocolError) }
    }
}
