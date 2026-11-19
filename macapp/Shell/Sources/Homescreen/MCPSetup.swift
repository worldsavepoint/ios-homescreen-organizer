import AppKit

/// Stdio configuration for the engine included in this app bundle; read-only by default.
struct MCPSetup {
    let executablePath: String
    var mode: MCPAccessMode = .readOnly

    var launchCommand: String {
        let quotedPath = "'" + executablePath.replacingOccurrences(of: "'", with: "'\"'\"'") + "'"
        let environment = mode == .readOnly ? "HOMESCREEN_READ_ONLY=1" : "HOMESCREEN_READ_ONLY=0 HOMESCREEN_ALLOW_APPLY=1"
        return "\(environment) \(quotedPath) mcp"
    }

    var cursorClaudeJSON: String {
        json(["mcpServers": ["ios-homescreen-mcp": server]])
    }

    var codexTOML: String {
        let command = json(executablePath, options: [.fragmentsAllowed, .withoutEscapingSlashes])
            .replacingOccurrences(of: "\u{7f}", with: "\\u007f")
        return """
        [mcp_servers.ios-homescreen-mcp]
        environment_id = "local"
        command = \(command)
        args = ["mcp"]

        [mcp_servers.ios-homescreen-mcp.env]
        HOMESCREEN_READ_ONLY = "\(mode == .readOnly ? "1" : "0")"
        """
        + (mode == .applyEnabled ? "\nHOMESCREEN_ALLOW_APPLY = \"1\"" : "")
    }

    var editorJSON: String {
        json(["servers": [MCPClientRegistration.serverID: server.merging(["type": "stdio"]) { _, value in value }]])
    }

    // Older callers used vscodeJSON for Copilot CLI's user configuration.
    var vscodeJSON: String { copilotJSON }

    var copilotJSON: String {
        var configuration = server
        configuration["type"] = "stdio"
        configuration["tools"] = ["*"]
        return json(["mcpServers": ["ios-homescreen-mcp": configuration]])
    }

    func text(format: String) -> String {
        switch format {
        case "toml": return codexTOML
        case "vscode": return editorJSON
        case "copilot": return copilotJSON
        case "launch": return launchCommand
        default: return cursorClaudeJSON
        }
    }

    @MainActor @discardableResult
    func copy(format: String, to pasteboard: NSPasteboard = .general) -> Bool {
        pasteboard.clearContents()
        return pasteboard.setString(text(format: format), forType: .string)
    }

    private var server: [String: Any] {
        ["command": executablePath, "args": ["mcp"], "env": mode == .readOnly
            ? ["HOMESCREEN_READ_ONLY": "1"] : ["HOMESCREEN_READ_ONLY": "0", "HOMESCREEN_ALLOW_APPLY": "1"]]
    }

    private func json(_ value: Any, options: JSONSerialization.WritingOptions = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]) -> String {
        // These values contain only strings, arrays and dictionaries, all valid JSON values.
        let data = try! JSONSerialization.data(withJSONObject: value, options: options)
        return String(decoding: data, as: UTF8.self)
    }
}
