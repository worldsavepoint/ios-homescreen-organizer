import Foundation

/// Retains JSON number kinds while dictionaries are merged and journaled.
private final class MCPJSONNumber {
    let text: String

    init(_ token: String) throws {
        let real = token.contains(".") || token.contains("e") || token.contains("E")
        if real {
            let canonical = try Self.real(token)
            guard let value = Double(token), value.isFinite,
                  try Self.real(String(value)) == canonical else { throw MCPClientOperationCode.snapshotReadFailed }
            text = canonical
        } else {
            text = token == "-0" ? "0" : token
        }
    }

    private static func real(_ token: String) throws -> String {
        let negative = token.hasPrefix("-")
        let unsigned = negative ? String(token.dropFirst()) : token
        let parts = unsigned.lowercased().split(separator: "e", omittingEmptySubsequences: false)
        guard parts.count <= 2, let scale = parts.count == 2 ? Int(parts[1]) : 0 else {
            throw MCPClientOperationCode.snapshotReadFailed
        }
        let decimal = parts[0].split(separator: ".", omittingEmptySubsequences: false)
        var digits = decimal.joined()
        let subtraction = scale.subtractingReportingOverflow(decimal.count == 2 ? decimal[1].count : 0)
        guard !subtraction.overflow else { throw MCPClientOperationCode.snapshotReadFailed }
        var exponent = subtraction.partialValue
        while digits.first == "0" { digits.removeFirst() }
        if digits.isEmpty { return (negative ? "-" : "") + "0e0" }
        while digits.last == "0" {
            digits.removeLast()
            let addition = exponent.addingReportingOverflow(1)
            guard !addition.overflow else { throw MCPClientOperationCode.snapshotReadFailed }
            exponent = addition.partialValue
        }
        return (negative ? "-" : "") + digits + "e" + String(exponent)
    }
}

enum MCPJSON {
    static func decode(_ data: Data, validated: Bool = false) throws -> Any {
        if !validated { _ = try JSONSerialization.jsonObject(with: data, options: .fragmentsAllowed) }
        var parser = Parser(bytes: Array(data))
        return try parser.value()
    }

    static func text(_ value: Any) throws -> String {
        if let number = value as? MCPJSONNumber { return number.text }
        if value is NSNull { return "null" }
        if let string = value as? String {
            return String(decoding: try JSONSerialization.data(withJSONObject: string,
                options: [.fragmentsAllowed, .withoutEscapingSlashes]), as: UTF8.self)
        }
        if let number = value as? NSNumber {
            if CFGetTypeID(number) == CFBooleanGetTypeID() { return number.boolValue ? "true" : "false" }
            let kind = String(cString: number.objCType)
            return try MCPJSONNumber(kind == "d" || kind == "f" ? String(number.doubleValue) : number.stringValue).text
        }
        if let fields = value as? NSDictionary, let keys = fields.allKeys as? [String] {
            let sorted = keys.sorted { $0.utf8.lexicographicallyPrecedes($1.utf8) }
            return try "{" + sorted.map { key in
                try text(key) + ":" + text(fields.object(forKey: key as NSString)!)
            }.joined(separator: ",") + "}"
        }
        if let array = value as? [Any] { return try "[" + array.map(text).joined(separator: ",") + "]" }
        throw MCPClientOperationCode.snapshotReadFailed
    }

    private struct Parser {
        let bytes: [UInt8]
        var index = 0

        mutating func whitespace() {
            while index < bytes.count, [9, 10, 13, 32].contains(bytes[index]) { index += 1 }
        }

        mutating func string() throws -> String {
            let start = index
            index += 1
            while index < bytes.count {
                let byte = bytes[index]
                index += 1
                if byte == 34 {
                    guard let string = try JSONSerialization.jsonObject(with: Data(bytes[start..<index]), options: .fragmentsAllowed) as? String else {
                        throw MCPClientOperationCode.snapshotReadFailed
                    }
                    return string
                }
                if byte == 92 { index += 1 }
            }
            throw MCPClientOperationCode.snapshotReadFailed
        }

        mutating func value() throws -> Any {
            whitespace()
            guard index < bytes.count else { throw MCPClientOperationCode.snapshotReadFailed }
            switch bytes[index] {
            case 34: return try string()
            case 123:
                let fields = NSMutableDictionary()
                index += 1
                whitespace()
                if bytes[index] == 125 { index += 1; return fields.copy() as! NSDictionary }
                while true {
                    let key = try string()
                    whitespace()
                    index += 1 // colon, validated by JSONSerialization
                    let member = try value()
                    fields.setObject(member, forKey: key as NSString)
                    whitespace()
                    let delimiter = bytes[index]
                    index += 1
                    if delimiter == 125 { return fields.copy() as! NSDictionary }
                    whitespace()
                }
            case 91:
                var array: [Any] = []
                index += 1
                whitespace()
                if bytes[index] == 93 { index += 1; return array }
                while true {
                    array.append(try value())
                    whitespace()
                    let delimiter = bytes[index]
                    index += 1
                    if delimiter == 93 { return array }
                }
            case 116: index += 4; return true
            case 102: index += 5; return false
            case 110: index += 4; return NSNull()
            default:
                let start = index
                while index < bytes.count, ![9, 10, 13, 32, 44, 93, 125].contains(bytes[index]) { index += 1 }
                return try MCPJSONNumber(String(decoding: bytes[start..<index], as: UTF8.self))
            }
        }
    }
}
