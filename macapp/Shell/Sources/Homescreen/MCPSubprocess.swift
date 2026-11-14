import Darwin
import Foundation

/// Owns one child's lifetime. Cancellation before launch is latched and cancellation
/// during I/O wakes poll. Only the worker closes descriptors and reaps the child.
final class MCPChildCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    private var group: pid_t?
    private let wake = Pipe()
    var descriptor: Int32 { wake.fileHandleForReading.fileDescriptor }

    func launch(_ path: String, arguments: [String], environment: [String: String], cwd: String,
                stdin: FileHandle, stdout: FileHandle, stderr: FileHandle) throws -> pid_t {
        lock.lock()
        defer { lock.unlock() }
        if cancelled { throw MCPClientOperationCode.cancelled }
        var attributes: posix_spawnattr_t?
        var actions: posix_spawn_file_actions_t?
        func checked(_ result: Int32) throws {
            if result != 0 { throw MCPClientOperationCode.launchFailed }
        }
        try checked(posix_spawnattr_init(&attributes))
        defer { posix_spawnattr_destroy(&attributes) }
        try checked(posix_spawn_file_actions_init(&actions))
        defer { posix_spawn_file_actions_destroy(&actions) }
        // The child becomes its group leader atomically, before an npm launcher can fork.
        try checked(posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT)))
        try checked(posix_spawnattr_setpgroup(&attributes, 0))
        try checked(posix_spawn_file_actions_addchdir_np(&actions, cwd))
        for (index, handle) in [stdin, stdout, stderr].enumerated() {
            if handle.fileDescriptor >= 0 {
                try checked(posix_spawn_file_actions_adddup2(&actions, handle.fileDescriptor, Int32(index)))
            } else {
                try checked(posix_spawn_file_actions_addopen(&actions, Int32(index), "/dev/null", index == 0 ? O_RDONLY : O_WRONLY, 0))
            }
        }
        var argv = ([path] + arguments).map { strdup($0) } + [nil]
        var envp = environment.map { strdup($0.key + "=" + $0.value) } + [nil]
        defer { argv.forEach { free($0) }; envp.forEach { free($0) } }
        guard argv.dropLast().allSatisfy({ $0 != nil }), envp.dropLast().allSatisfy({ $0 != nil }) else {
            throw MCPClientOperationCode.launchFailed
        }
        var pid: pid_t = 0
        try checked(posix_spawn(&pid, path, &actions, &attributes, &argv, &envp))
        group = pid
        return pid
    }

    func cancel() {
        lock.lock()
        defer { lock.unlock() }
        guard !cancelled else { return }
        cancelled = true
        if let group { kill(-group, SIGKILL) }
        try? wake.fileHandleForWriting.write(contentsOf: Data([1]))
    }

    /// Keep the leader unreaped until its group has been signalled so its PID
    /// cannot be recycled. Orphaned descendants are collected by launchd.
    @discardableResult func reap(_ pid: pid_t) -> Int32 {
        lock.lock()
        group = nil
        kill(-pid, SIGKILL)
        lock.unlock()
        var status: Int32 = 0
        while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
        while kill(-pid, 0) == 0 { usleep(1000) }
        let signal = status & 0x7f
        return signal == 0 ? (status >> 8) & 255 : 128 + signal
    }
}

/// Inspect only the members used to authorize a Codex write. Foundation has
/// already validated the JSON grammar, but its dictionaries discard duplicates.
private struct CodexWriteReplyMembers {
    private enum Section {
        case envelope, error, reason, result

        var controls: Set<String> {
            switch self {
            case .envelope: return ["id", "method", "error", "result"]
            case .error: return ["code", "message", "data"]
            case .reason: return ["config_write_error_code"]
            case .result: return ["status", "filePath", "version"]
            }
        }
    }

    private let bytes: Data
    private var index: Data.Index

    init(_ line: Data) {
        bytes = line
        index = line.startIndex
    }

    mutating func validate() throws {
        try inspect(.envelope)
    }

    private mutating func whitespace() {
        while index < bytes.endIndex {
            switch bytes[index] {
            case 9, 10, 13, 32: index += 1
            default: return
            }
        }
    }

    private mutating func consume(_ byte: UInt8) -> Bool {
        whitespace()
        guard index < bytes.endIndex, bytes[index] == byte else { return false }
        index += 1
        return true
    }

    private mutating func string() throws -> Range<Data.Index> {
        let start = index
        guard consume(0x22) else { throw MCPClientOperationCode.invalidResponse }
        while index < bytes.endIndex {
            let byte = bytes[index]
            index += 1
            if byte == 0x22 { return start..<index }
            if byte == 0x5c { index += 1 }
        }
        throw MCPClientOperationCode.invalidResponse
    }

    private mutating func inspect(_ section: Section) throws {
        guard consume(0x7b) else { throw MCPClientOperationCode.invalidResponse }
        let controls = section.controls
        var unused = controls
        if consume(0x7d) { return }
        repeat {
            whitespace()
            let key = try string()
            let name = try JSONSerialization.jsonObject(with: bytes[key], options: .fragmentsAllowed) as? String
            guard let name, consume(0x3a) else { throw MCPClientOperationCode.invalidResponse }
            if controls.contains(name), unused.remove(name) == nil { throw MCPClientOperationCode.invalidResponse }
            whitespace()
            guard index < bytes.endIndex else { throw MCPClientOperationCode.invalidResponse }
            if bytes[index] == 0x7b {
                switch (section, name) {
                case (.envelope, "error"): try inspect(.error)
                case (.envelope, "result"): try inspect(.result)
                case (.error, "data"): try inspect(.reason)
                default: try skipValue()
                }
            } else { try skipValue() }
        } while consume(0x2c)
        guard consume(0x7d) else { throw MCPClientOperationCode.invalidResponse }
    }

    private mutating func skipValue() throws {
        var depth = 0
        while index < bytes.endIndex {
            switch bytes[index] {
            case 0x22: _ = try string()
            case 0x7b, 0x5b: depth += 1; index += 1
            case 0x7d, 0x5d:
                if depth == 0 { return }
                depth -= 1
                index += 1
            case 0x2c where depth == 0: return
            default: index += 1
            }
        }
    }
}

struct MCPChildIO {
    let cancellation: MCPChildCancellation
    let input: FileHandle
    let output: FileHandle
    let deadline: TimeInterval
    var pending = Data()
    var received = 0
    static let limit = 8 * 1024 * 1024

    func wait(_ fd: Int32, events: Int16) throws {
        while true {
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            guard remaining > 0 else { throw MCPClientOperationCode.timedOut }
            var descriptors = [pollfd(fd: fd, events: events, revents: 0),
                               pollfd(fd: cancellation.descriptor, events: Int16(POLLIN), revents: 0)]
            let ready = poll(&descriptors, 2, Int32(min(remaining * 1000 + 1, Double(Int32.max))))
            if ready < 0 && errno == EINTR { continue }
            guard ready > 0 else { throw ready == 0 ? MCPClientOperationCode.timedOut : .invalidResponse }
            guard descriptors[1].revents == 0 else { throw MCPClientOperationCode.cancelled }
            return
        }
    }

    func send(_ object: [String: Any], onFirstWrite: (() -> Void)? = nil) throws {
        try send(JSONSerialization.data(withJSONObject: object), onFirstWrite: onFirstWrite)
    }

    func send(_ data: Data, onFirstWrite: (() -> Void)? = nil) throws {
        var bytes = data
        bytes.append(10)
        guard bytes.count <= Self.limit else { throw MCPClientOperationCode.invalidResponse }
        let fd = input.fileDescriptor
        try bytes.withUnsafeBytes { buffer in
            var offset = 0
            while offset < buffer.count {
                try wait(fd, events: Int16(POLLOUT))
                let count = write(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if count < 0 && (errno == EINTR || errno == EAGAIN) { continue }
                guard count > 0 else { throw MCPClientOperationCode.invalidResponse }
                if offset == 0 { onFirstWrite?() }
                offset += count
            }
        }
    }

    mutating func receive(_ id: Int, mcp: Bool, codexBatchWrite: Bool = false, preserveNumbers: Bool = false) throws -> [String: Any] {
        var scanIndex = pending.startIndex
        while true {
            if let newline = pending[scanIndex...].firstIndex(of: 10) {
                let line = pending.prefix(upTo: newline)
                pending.removeSubrange(...newline)
                scanIndex = pending.startIndex
                guard let message = try JSONSerialization.jsonObject(with: line) as? [String: Any],
                      !mcp || message["jsonrpc"] as? String == "2.0" else { throw MCPClientOperationCode.invalidResponse }
                if message["id"] == nil, message["method"] is String { continue }
                guard let number = message["id"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                      number == NSNumber(value: id), message["method"] == nil else {
                    throw MCPClientOperationCode.invalidResponse
                }
                if codexBatchWrite {
                    var members = CodexWriteReplyMembers(line)
                    try members.validate()
                }
                if let failure = message["error"] {
                    guard !mcp, let error = failure as? [String: Any], message["result"] == nil else {
                        throw MCPClientOperationCode.invalidResponse
                    }
                    if codexBatchWrite {
                        guard let code = error["code"] as? Int, error["message"] is String else {
                            throw MCPClientOperationCode.invalidResponse
                        }
                        if code == -32601 { throw MCPClientOperationCode.unsupportedClient }
                        if code == -32600, let data = error["data"] as? [String: Any] {
                            switch data["config_write_error_code"] as? String {
                            case "configValidationError", "configRequirementReadonly": throw MCPCodexWriteError.rejectedBeforePersistence
                            case "configVersionConflict": throw MCPCodexWriteError.versionRefused
                            default: break
                            }
                        }
                        throw MCPClientOperationCode.commandFailed
                    }
                    if error["code"] as? Int == -32601 { throw MCPClientOperationCode.unsupportedClient }
                    if error["message"] as? String == "Configuration was modified since last read. Fetch latest version and retry." {
                        throw MCPClientOperationCode.versionConflict
                    }
                    throw MCPClientOperationCode.commandFailed
                }
                guard let result = message["result"] as? [String: Any] else { throw MCPClientOperationCode.invalidResponse }
                if preserveNumbers {
                    guard let typed = try MCPJSON.decode(Data(line), validated: true) as? [String: Any],
                          let result = typed["result"] as? [String: Any] else { throw MCPClientOperationCode.invalidResponse }
                    return result
                }
                return result
            }
            scanIndex = pending.endIndex
            try wait(output.fileDescriptor, events: Int16(POLLIN))
            var bytes = [UInt8](repeating: 0, count: 65536)
            let count = read(output.fileDescriptor, &bytes, bytes.count)
            if count < 0 && errno == EINTR { continue }
            guard count > 0, received + count <= Self.limit else { throw MCPClientOperationCode.invalidResponse }
            received += count
            pending.append(contentsOf: bytes.prefix(count))
        }
    }
}

enum MCPSubprocess {
    static func work<T>(_ operation: @escaping (MCPChildCancellation) throws -> T) async throws -> T {
        let token = MCPChildCancellation()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                DispatchQueue.global(qos: .userInitiated).async {
                    continuation.resume(with: Result { try operation(token) })
                }
            }
        } onCancel: { token.cancel() }
    }

    static func session<T>(_ path: String, _ arguments: [String], _ environment: [String: String], _ timeout: TimeInterval,
                           _ body: @escaping (inout MCPChildIO) throws -> T) async throws -> T {
        try await work { token in
            let input = Pipe(), output = Pipe()
            let pid = try token.launch(path, arguments: arguments, environment: environment,
                                       cwd: FileManager.default.currentDirectoryPath,
                                       stdin: input.fileHandleForReading, stdout: output.fileHandleForWriting, stderr: .nullDevice)
            defer { token.reap(pid) }
            try? input.fileHandleForReading.close()
            try? output.fileHandleForWriting.close()
            let fd = input.fileHandleForWriting.fileDescriptor
            _ = fcntl(fd, F_SETNOSIGPIPE, 1)
            _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK)
            var io = MCPChildIO(cancellation: token, input: input.fileHandleForWriting,
                                output: output.fileHandleForReading, deadline: ProcessInfo.processInfo.systemUptime + timeout)
            return try body(&io)
        }
    }
}

enum MCPProcessRunner {
    static func run(executablePath: String, arguments: [String], environment: [String: String], timeout: TimeInterval, cwd: String) async -> MCPCommandResult {
        do {
            return try await MCPSubprocess.work { token in
                let stdout = Pipe(), stderr = Pipe()
                let ended = Pipe()
                let pid = try token.launch(executablePath, arguments: arguments, environment: environment, cwd: cwd,
                                           stdin: .nullDevice, stdout: stdout.fileHandleForWriting, stderr: stderr.fileHandleForWriting)
                let waiter = DispatchGroup()
                waiter.enter()
                // Observe exit without consuming status. The worker always owns waitpid.
                DispatchQueue.global(qos: .userInitiated).async {
                    var info = siginfo_t()
                    while waitid(P_PID, id_t(pid), &info, WEXITED | WNOWAIT) < 0 && errno == EINTR {}
                    try? ended.fileHandleForWriting.write(contentsOf: Data([1]))
                    waiter.leave()
                }
                var reaped = false
                defer {
                    if !reaped { token.reap(pid) }
                    waiter.wait()
                }
                try? stdout.fileHandleForWriting.close()
                try? stderr.fileHandleForWriting.close()
                var descriptors = [stdout.fileHandleForReading, stderr.fileHandleForReading, ended.fileHandleForReading].map { pollfd(fd: $0.fileDescriptor, events: Int16(POLLIN), revents: 0) }
                descriptors.append(pollfd(fd: token.descriptor, events: Int16(POLLIN), revents: 0))
                var output = [Data(), Data()]
                let deadline = ProcessInfo.processInfo.systemUptime + timeout
                while descriptors.prefix(3).contains(where: { $0.fd >= 0 }) {
                    let left = deadline - ProcessInfo.processInfo.systemUptime
                    guard left > 0 else { throw MCPClientOperationCode.timedOut }
                    let ready = poll(&descriptors, 4, Int32(min(left * 1000 + 1, Double(Int32.max))))
                    if ready < 0 && errno == EINTR { continue }
                    guard ready > 0 else { throw ready == 0 ? MCPClientOperationCode.timedOut : .commandFailed }
                    if descriptors[3].revents != 0 { throw MCPClientOperationCode.cancelled }
                    if descriptors[2].revents != 0 { descriptors[2].fd = -1 }
                    for i in 0..<2 where descriptors[i].revents != 0 {
                        var bytes = [UInt8](repeating: 0, count: 65536)
                        let count = read(descriptors[i].fd, &bytes, bytes.count)
                        if count < 0 && errno == EINTR { continue }
                        guard count >= 0, output[i].count + count <= MCPChildIO.limit else { throw MCPClientOperationCode.invalidResponse }
                        if count == 0 { descriptors[i].fd = -1 }
                        else { output[i].append(contentsOf: bytes.prefix(count)) }
                    }
                }
                let status = token.reap(pid)
                reaped = true
                return MCPCommandResult(exitStatus: status, stdout: String(decoding: output[0], as: UTF8.self),
                                        stderr: String(decoding: output[1], as: UTF8.self))
            }
        } catch { return MCPCommandResult(exitStatus: -1, failure: error as? MCPClientOperationCode ?? .commandFailed) }
    }
}
