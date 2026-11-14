import Foundation
import Darwin

enum MCPRegistrationPhase: String, Codable {
    case prepared, bootstrapping, bootstrapObserved, versionedWritePending, versionedWriteRejected, bootstrapUndoPending
    case removing, adding, acknowledged, observed, restoring, restoreRemoved, committed, conflicted
}

struct MCPRegistrationRecord: Codable {
    let client: String
    let scope: String
    let server: String
    let file: String
    let prior: MCPOwnedEntry
    let intended: MCPOwnedEntry
    var phase: MCPRegistrationPhase = .prepared
    var bootstrapExpected: MCPOwnedEntry?
    var observed: MCPOwnedEntry?
    var bootstrapVersion: String?
}

/// The fixed record and lock names never come from decoded journal data.
class MCPRegistrationJournal {
    let directory: URL
    let client: MCPClient
    private let lockFD: Int32
    private let dirFD: Int32
    private var recordName: String { client.rawValue + ".json" }

    private static func createDirectory(_ directory: URL) throws {
        try FileManager.default.createDirectory(at: directory.deletingLastPathComponent(), withIntermediateDirectories: true)
        guard let security = filesec_init() else { throw MCPClientOperationCode.snapshotReadFailed }
        defer { filesec_free(security) }
        guard let createdACL = acl_init(0) else { throw MCPClientOperationCode.snapshotReadFailed }
        var acl: acl_t? = createdACL
        defer { acl_free(UnsafeMutableRawPointer(createdACL)) }
        var flags: acl_flagset_t?
        var mode: mode_t = 0o700
        guard acl_get_flagset_np(UnsafeMutableRawPointer(createdACL), &flags) == 0, acl_add_flag_np(flags, ACL_FLAG_NO_INHERIT) == 0,
              filesec_set_property(security, FILESEC_MODE, &mode) == 0,
              filesec_set_property(security, FILESEC_ACL, &acl) == 0 else { throw MCPClientOperationCode.snapshotReadFailed }
        guard mkdirx_np(directory.path, security) == 0 || errno == EEXIST else { throw MCPClientOperationCode.snapshotReadFailed }
    }

    private static func isPrivate(_ fd: Int32, type: mode_t) -> Bool {
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == getuid(), info.st_mode & S_IFMT == type,
              info.st_mode & 0o077 == 0 else { return false }
        guard let acl = acl_get_fd_np(fd, ACL_TYPE_EXTENDED) else { return errno == ENOENT }
        defer { acl_free(UnsafeMutableRawPointer(acl)) }
        var entry: acl_entry_t?
        var cursor = ACL_FIRST_ENTRY.rawValue
        while acl_get_entry(acl, cursor, &entry) == 0 {
            var tag = ACL_UNDEFINED_TAG
            guard acl_get_tag_type(entry, &tag) == 0, tag == ACL_EXTENDED_DENY else { return false }
            cursor = ACL_NEXT_ENTRY.rawValue
        }
        return errno == EINVAL
    }

    init(directory: URL, client: MCPClient) throws {
        self.directory = directory
        self.client = client
        try Self.createDirectory(directory)
        let dir = open(directory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard dir >= 0, Self.isPrivate(dir, type: S_IFDIR) else {
            if dir >= 0 { close(dir) }
            throw MCPClientOperationCode.snapshotReadFailed
        }
        dirFD = dir
        let name = client.rawValue + ".lock"
        var fd = openat(dir, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        if fd < 0 && errno == ENOENT {
            fd = openat(dir, name, O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        }
        guard fd >= 0, Self.isPrivate(fd, type: S_IFREG), flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            if fd >= 0 { close(fd) }
            close(dir)
            throw MCPClientOperationCode.operationBusy
        }
        lockFD = fd
    }

    deinit { close(lockFD); close(dirFD) }

    private func openRecord() throws -> Int32? {
        let fd = openat(dirFD, recordName, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        if fd < 0 && errno == ENOENT { return nil }
        guard fd >= 0, Self.isPrivate(fd, type: S_IFREG) else {
            if fd >= 0 { close(fd) }
            throw MCPClientOperationCode.recoveryFailed
        }
        return fd
    }

    func read() throws -> MCPRegistrationRecord? {
        guard let fd = try openRecord() else { return nil }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? handle.close() }
        var st = stat()
        guard fstat(fd, &st) == 0, st.st_size <= 4 * MCPChildIO.limit, let data = try handle.readToEnd() else {
            throw MCPClientOperationCode.recoveryFailed
        }
        var record = try JSONDecoder().decode(MCPRegistrationRecord.self, from: data)
        guard record.server == MCPClientRegistration.serverID, record.client == client.rawValue, record.scope == "user",
              record.file.hasPrefix("/"), try record.intended.object() != nil else { throw MCPClientOperationCode.recoveryFailed }
        _ = try record.prior.object()
        let bootstrapExpected = try record.bootstrapExpected?.object()
        _ = try record.observed?.object()
        if client == .codex, record.phase == .adding { record.phase = .versionedWritePending }
        if client == .codex, record.phase == .bootstrapObserved, bootstrapExpected == nil {
            // Legacy writers reused this phase immediately before dispatching undo.
            record.phase = .bootstrapUndoPending
        }
        return record
    }

    func save(_ record: MCPRegistrationRecord) throws {
        if let existing = try openRecord() { close(existing) }
        let name = "." + client.rawValue + "-" + UUID().uuidString
        let fd = openat(dirFD, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw MCPClientOperationCode.snapshotReadFailed }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? handle.close(); unlinkat(dirFD, name, 0) }
        guard Self.isPrivate(fd, type: S_IFREG) else { throw MCPClientOperationCode.snapshotReadFailed }
        try handle.write(contentsOf: JSONEncoder().encode(record))
        guard fsync(fd) == 0, renameat(dirFD, name, dirFD, recordName) == 0, fsync(dirFD) == 0 else {
            throw MCPClientOperationCode.snapshotReadFailed
        }
    }

    func clear() throws {
        guard unlinkat(dirFD, recordName, 0) == 0 || errno == ENOENT, fsync(dirFD) == 0 else { throw MCPClientOperationCode.recoveryFailed }
    }
}
