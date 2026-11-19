import Foundation
import Security
import CryptoKit

/// Request and response validation for local server startup.
enum EngineSecurity {
    static func makeToken() throws -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        guard status == errSecSuccess else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
        return bytes.map { String(format: "%02x", $0) }.joined()
    }

    static func healthRequest(base: URL, challenge: String) -> URLRequest {
        var request = URLRequest(url: base.appendingPathComponent("v1/health"))
        request.setValue(challenge, forHTTPHeaderField: "X-Homescreen-Challenge")
        request.timeoutInterval = 1
        request.cachePolicy = .reloadIgnoringLocalCacheData
        return request
    }

    static func isHealthy(data: Data, response: URLResponse, base: URL,
                          secret: String, challenge: String) -> Bool {
        guard let response = response as? HTTPURLResponse,
              response.statusCode == 200,
              response.url == base.appendingPathComponent("v1/health"),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["app"] as? String == "homescreen",
              let ready = object["ready"] as? NSNumber,
              CFGetTypeID(ready) == CFBooleanGetTypeID(), ready.boolValue,
              let proof = object["proof"] as? String,
              let proofBytes = decodeHex(proof), proofBytes.count == 32 else { return false }
        return HMAC<SHA256>.isValidAuthenticationCode(proofBytes,
            authenticating: Data(challenge.utf8), using: SymmetricKey(data: Data(secret.utf8)))
    }

    private static func decodeHex(_ value: String) -> Data? {
        let characters = Array(value.utf8)
        guard characters.count == 64 else { return nil }
        var result = Data()
        for index in stride(from: 0, to: characters.count, by: 2) {
            guard let byte = UInt8(String(decoding: characters[index...index + 1], as: UTF8.self), radix: 16)
                else { return nil }
            result.append(byte)
        }
        return result
    }

    static func pageURL(base: URL, token: String) -> URL {
        var parts = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        parts.fragment = "token=" + token
        return parts.url!
    }
}

enum NavigationPolicy {
    enum Destination { case embedded, browser, blocked }

    static func destination(_ url: URL, origin: URL) -> Destination {
        guard let scheme = url.scheme?.lowercased(), ["http", "https"].contains(scheme),
              url.host != nil, url.user == nil, url.password == nil else { return .blocked }
        if scheme == origin.scheme, url.host == origin.host, url.port == origin.port {
            return .embedded
        }
        return .browser
    }
}

/// Only the active editor's main frame may request these two native UI actions.
enum NativeBridge {
    enum Action: Equatable { case openMCPSetup, setLanguage(String) }

    static func action(_ body: Any, frameURL: URL?, isMainFrame: Bool, origin: URL) -> Action? {
        guard isMainFrame, let frameURL,
              NavigationPolicy.destination(frameURL, origin: origin) == .embedded,
              let message = body as? [String: Any] else { return nil }
        switch message["action"] as? String {
        case "openMCPSetup": return .openMCPSetup
        case "setLanguage":
            guard let preference = message["preference"] as? String else { return nil }
            return .setLanguage(preference)
        default: return nil
        }
    }
}

/// Keep readiness checks on the chosen loopback origin.
final class HealthSessionDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
