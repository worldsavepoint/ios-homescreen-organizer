import Foundation
import JavaScriptCore
import SwiftUI

protocol LanguagePreferences {
    func string(forKey key: String) -> String?
    func set(_ value: Any?, forKey key: String)
}
extension UserDefaults: LanguagePreferences {}

@MainActor
final class Localization: ObservableObject {
    struct Language: Decodable, Identifiable {
        let code: String
        let name: String
        let aliases: [String]
        var id: String { code }
    }
    private struct Manifest: Decodable {
        let languages: [Language]
    }

    @Published private(set) var preference: String
    @Published private(set) var locale = "en"
    let languages: [Language]
    private let directory: URL
    private let defaults: LanguagePreferences
    private let preferredLanguages: () -> [String]
    private var systemLocaleObserver: NSObjectProtocol?
    private let english: [String: String]
    // Use the system ECMA-402 implementation shared by WebKit and our catalog
    // validator. Foundation canonicalizes several distinct tags differently.
    private static let canonicalize = JSContext()!.evaluateScript("""
        (value) => {
            try { return Intl.getCanonicalLocales(value.replace(/_/g, "-"))[0] }
            catch { return null }
        }
        """)!
    private var catalog: [String: String] = [:]

    init(catalogDirectory: URL = Bundle.main.resourceURL!.appendingPathComponent("Localization"),
         defaults: LanguagePreferences = UserDefaults.standard, preferredLanguages: [String]? = nil,
         preferredLanguageProvider: @escaping () -> [String] = { Locale.preferredLanguages }) {
        directory = catalogDirectory
        self.defaults = defaults
        if let preferredLanguages { self.preferredLanguages = { preferredLanguages } }
        else { self.preferredLanguages = preferredLanguageProvider }
        let manifest = try? JSONDecoder().decode(Manifest.self, from: Data(contentsOf: directory.appendingPathComponent("manifest.json")))
        languages = manifest?.languages ?? [Language(code: "en", name: "English", aliases: ["en"])]
        english = Self.load("en", from: directory)
        preference = "system"
        setPreference(defaults.string(forKey: "screenOrganizer.language") ?? "system")
        systemLocaleObserver = NotificationCenter.default.addObserver(forName: NSLocale.currentLocaleDidChangeNotification,
            object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor [weak self] in self?.refreshSystemLocale() }
            }
    }

    deinit {
        if let systemLocaleObserver { NotificationCenter.default.removeObserver(systemLocaleObserver) }
    }

    private func refreshSystemLocale() {
        guard preference == "system" else { return }
        let resolved = resolveLocale(preference)
        guard resolved != locale else { return }
        locale = resolved
        catalog = Self.load(locale, from: directory)
    }

    func setPreference(_ value: String) {
        preference = recognizedPreference(value) ?? "system"
        locale = resolveLocale(preference)
        catalog = Self.load(locale, from: directory)
        defaults.set(preference, forKey: "screenOrganizer.language")
    }

    func resolveLocale(_ value: String) -> String {
        if value != "system" { return match(value) ?? "en" }
        return preferredLanguages().compactMap(match).first ?? "en"
    }

    func recognizedPreference(_ value: String) -> String? {
        value == "system" ? "system" : match(value)
    }

    private func match(_ value: String) -> String? {
        func normalized(_ raw: String) -> String? {
            guard let result = Self.canonicalize.call(withArguments: [raw]), result.isString else { return nil }
            return result.toString().lowercased()
        }
        guard let tag = normalized(value) else { return nil }
        if let exact = languages.first(where: { normalized($0.code) == tag }) { return exact.code }
        let parts = tag.split(separator: "-").map(String.init)
        let owners = languages.map { language in (language.code, (language.aliases + [language.code]).compactMap(normalized)) }
        var candidates: [String] = []
        candidates += stride(from: parts.count, through: 1, by: -1).map { parts.prefix($0).joined(separator: "-") }
        for candidate in candidates {
            if let owner = owners.first(where: { $0.1.contains(candidate) }) { return owner.0 }
        }
        return nil
    }

    func t(_ key: String, _ parameters: [String: String] = [:]) -> String {
        let text = catalog[key] ?? english[key] ?? key
        let pattern = try! NSRegularExpression(pattern: "\\{([A-Za-z_][A-Za-z0-9_]*)\\}")
        let source = text as NSString
        var result = text
        for match in pattern.matches(in: text, range: NSRange(location: 0, length: source.length)).reversed() {
            guard let value = parameters[source.substring(with: match.range(at: 1))],
                  let range = Range(match.range, in: result) else { continue }
            result.replaceSubrange(range, with: value)
        }
        return result
    }

    private static func load(_ code: String, from directory: URL) -> [String: String] {
        (try? JSONDecoder().decode([String: String].self, from: Data(contentsOf: directory.appendingPathComponent(code + ".json")))) ?? [:]
    }
}

struct LanguagePicker: View {
    @EnvironmentObject private var localization: Localization
    var body: some View {
        Picker(localization.t("common.language"), selection: Binding(get: { localization.preference }, set: localization.setPreference)) {
            Text(localization.t("common.system")).tag("system")
            ForEach(localization.languages) { language in Text(language.name).tag(language.code) }
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}
