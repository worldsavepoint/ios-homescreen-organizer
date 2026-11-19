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
    private struct NumberLiteral { let text: String }

    static func decode(_ data: Data, validated: Bool = false, deferNumbers: Bool = false) throws -> Any {
        if !validated && !deferNumbers { _ = try JSONSerialization.jsonObject(with: data, options: .fragmentsAllowed) }
        var parser = Parser(bytes: Array(data), deferNumbers: deferNumbers)
        let result = try parser.value(depth: 0)
        parser.whitespace()
        guard parser.index == parser.bytes.count else { throw MCPClientOperationCode.snapshotReadFailed }
        return result
    }

    static func text(_ value: Any) throws -> String {
        if let number = value as? NumberLiteral {
            // Only numbers in the selected entry must be representable by the strict reader.
            _ = try JSONSerialization.jsonObject(with: Data(number.text.utf8), options: .fragmentsAllowed)
            return try MCPJSONNumber(number.text).text
        }
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
        let deferNumbers: Bool
        var index = 0

        mutating func whitespace() {
            while index < bytes.count, [9, 10, 13, 32].contains(bytes[index]) { index += 1 }
        }

        mutating func consume(_ byte: UInt8) -> Bool {
            guard index < bytes.count, bytes[index] == byte else { return false }
            index += 1
            return true
        }

        mutating func digits() -> Bool {
            let start = index
            while index < bytes.count, (48...57).contains(bytes[index]) { index += 1 }
            return index != start
        }

        mutating func number() throws -> Any {
            let start = index
            _ = consume(45) // minus
            guard consume(48) || digits() else { throw MCPClientOperationCode.snapshotReadFailed }
            if consume(46) && !digits() { throw MCPClientOperationCode.snapshotReadFailed }
            if consume(101) || consume(69) {
                _ = consume(43) || consume(45)
                guard digits() else { throw MCPClientOperationCode.snapshotReadFailed }
            }
            let token = String(decoding: bytes[start..<index], as: UTF8.self)
            if deferNumbers { return NumberLiteral(text: token) }
            return try MCPJSONNumber(token)
        }

        mutating func string() throws -> String {
            let start = index
            guard consume(34) else { throw MCPClientOperationCode.snapshotReadFailed }
            while index < bytes.count {
                let byte = bytes[index]
                index += 1
                guard byte >= 32 else { throw MCPClientOperationCode.snapshotReadFailed }
                if byte == 34 {
                    guard let string = try JSONSerialization.jsonObject(with: Data(bytes[start..<index]), options: .fragmentsAllowed) as? String else {
                        throw MCPClientOperationCode.snapshotReadFailed
                    }
                    return string
                }
                if byte == 92 {
                    guard index < bytes.count, [34, 47, 92, 98, 102, 110, 114, 116, 117].contains(bytes[index]) else {
                        throw MCPClientOperationCode.snapshotReadFailed
                    }
                    index += 1
                }
            }
            throw MCPClientOperationCode.snapshotReadFailed
        }

        mutating func value(depth: Int) throws -> Any {
            whitespace()
            guard index < bytes.count, depth <= 512 else { throw MCPClientOperationCode.snapshotReadFailed }
            switch bytes[index] {
            case 34: return try string()
            case 123:
                let fields = NSMutableDictionary()
                index += 1
                whitespace()
                if consume(125) { return fields.copy() as! NSDictionary }
                while true {
                    let key = try string()
                    whitespace()
                    guard consume(58) else { throw MCPClientOperationCode.snapshotReadFailed }
                    let member = try value(depth: depth + 1)
                    fields.setObject(member, forKey: key as NSString)
                    whitespace()
                    if consume(125) { return fields.copy() as! NSDictionary }
                    guard consume(44) else { throw MCPClientOperationCode.snapshotReadFailed }
                    whitespace()
                }
            case 91:
                var array: [Any] = []
                index += 1
                whitespace()
                if consume(93) { return array }
                while true {
                    array.append(try value(depth: depth + 1))
                    whitespace()
                    if consume(93) { return array }
                    guard consume(44) else { throw MCPClientOperationCode.snapshotReadFailed }
                }
            case 116, 102, 110:
                let first = bytes[index]
                let literal = first == 116 ? "true" : first == 102 ? "false" : "null"
                guard bytes[index...].starts(with: literal.utf8) else { throw MCPClientOperationCode.snapshotReadFailed }
                index += literal.utf8.count
                if first == 110 { return NSNull() }
                return first == 116
            default: return try number()
            }
        }
    }
}
