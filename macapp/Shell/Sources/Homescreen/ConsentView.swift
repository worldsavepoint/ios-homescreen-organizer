import SwiftUI

/// Acceptance lasts only for this process. Reading requires positive, visible end geometry.
@MainActor
final class LaunchConsent: ObservableObject {
    @Published private(set) var accepted = false
    @Published private(set) var hasReadToEnd = false
    @Published private(set) var agreed = false
    @Published private(set) var declined = false

    var canContinue: Bool { hasReadToEnd && agreed && !declined && !accepted }

    func observeEnd(_ end: CGRect, viewport: CGSize) {
        guard !declined, viewport.width > 0, viewport.height > 0, end.width > 0, end.height > 0,
              end.minX >= 0, end.maxX <= viewport.width + 1,
              end.minY >= 0, end.maxY <= viewport.height + 1 else { return }
        hasReadToEnd = true
    }

    func setAgreed(_ value: Bool) { agreed = value && hasReadToEnd && !declined }
    @discardableResult func accept() -> Bool {
        guard canContinue else { return false }
        accepted = true
        return true
    }
    func decline() { declined = true; agreed = false }
    func languageChanged() {
        guard !accepted else { return }
        hasReadToEnd = false
        agreed = false
    }
}

private struct ConsentLanguage: Hashable {
    let preference: String
    let locale: String
}

private struct ConsentEnd: Equatable {
    let language: ConsentLanguage
    let frame: CGRect
    let viewport: CGSize
}

private struct ConsentEndKey: PreferenceKey {
    static var defaultValue: ConsentEnd? = nil
    static func reduce(value: inout ConsentEnd?, nextValue: () -> ConsentEnd?) { value = nextValue() ?? value }
}

struct ConsentView: View {
    @EnvironmentObject private var localization: Localization
    @ObservedObject var consent: LaunchConsent
    @State private var observedLanguage: ConsentLanguage?
    var reviewing = false
    var onContinue: () -> Void
    var onDecline: () -> Void
    private let paragraphs = ["affiliation", "trademarks", "deviceInterfaces", "backups", "warranty", "clientData"]
    private var language: ConsentLanguage {
        ConsentLanguage(preference: localization.preference, locale: localization.locale)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(spacing: 14) {
                AppMark().frame(width: 48, height: 48).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 3) {
                    Text(localization.t("app.name")).font(.title2.bold())
                    Text(localization.t("app.subtitle")).foregroundStyle(.secondary)
                }
                Spacer(minLength: 24)
                LanguagePicker().frame(maxWidth: 250)
            }
            Text(localization.t(reviewing ? "consent.disclaimer" : "consent.title")).font(.title.bold())
            GeometryReader { viewport in
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        ForEach(paragraphs, id: \.self) { key in
                            Text(localization.t("consent." + key))
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(GeometryReader { text in
                                    if key == paragraphs.last {
                                        let frame = text.frame(in: .named("consentViewport"))
                                        Color.clear.preference(key: ConsentEndKey.self,
                                            value: ConsentEnd(language: language,
                                                frame: CGRect(x: frame.minX, y: frame.maxY - 2, width: frame.width, height: 2),
                                                viewport: viewport.size))
                                    }
                                })
                        }
                    }
                    .padding(20)
                }
                .id(language)
                .focusable()
                .accessibilityLabel(localization.t("consent.disclaimer"))
                .coordinateSpace(name: "consentViewport")
                .background(.background, in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(.separator))
                .onPreferenceChange(ConsentEndKey.self) { end in
                    guard let end, end.language == language else { return }
                    if observedLanguage != end.language {
                        consent.languageChanged()
                        observedLanguage = end.language
                    }
                    consent.observeEnd(end.frame, viewport: end.viewport)
                }
            }
            .frame(minHeight: 180)
            if reviewing {
                HStack { Spacer(); Button(localization.t("common.close"), action: onContinue).keyboardShortcut(.cancelAction) }
            } else {
                if !consent.hasReadToEnd {
                    Text(localization.t("consent.readToEnd")).font(.callout).foregroundStyle(.secondary)
                }
                Toggle(localization.t("consent.agree"), isOn: Binding(get: { consent.agreed }, set: consent.setAgreed))
                    .disabled(!consent.hasReadToEnd)
                HStack {
                    Button(localization.t("consent.decline")) { consent.decline(); onDecline() }.keyboardShortcut(.cancelAction)
                    Spacer()
                    Button(localization.t("consent.continue")) { if consent.accept() { onContinue() } }
                        .keyboardShortcut(.defaultAction).disabled(!consent.canContinue)
                }
            }
        }
        .padding(32)
        .frame(maxWidth: 820, maxHeight: 720)
    }
}
