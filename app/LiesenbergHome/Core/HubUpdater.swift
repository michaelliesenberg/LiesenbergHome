import Foundation
import Observation
import SwiftUI

/// Hub-Version prüfen und – mit Besitzer- oder Vollzugriff – das Update direkt aus der App starten.
@MainActor
@Observable
final class HubUpdater {
    /// Diese App-Version braucht mindestens diesen Hub
    static let requiredHub = "2.2.0"

    private(set) var installed: String?
    private(set) var latest: String?
    private(set) var updateAvailable = false
    private(set) var updating = false
    private(set) var message: String?
    private var checkedAt = Date.distantPast

    let client: HouseClient
    init(client: HouseClient) { self.client = client }

    /// Hub älter als von der App benötigt?
    var hubTooOld: Bool {
        guard let installed else { return false }
        return Self.compare(installed, Self.requiredHub) < 0
    }

    private struct VersionInfo: Decodable { let installed: String; let latest: String?; let updateAvailable: Bool }

    func check(force: Bool = false) async {
        guard force || Date().timeIntervalSince(checkedAt) > 3600 else { return }
        checkedAt = Date()
        if let data = try? await client.get("api/hub/version"),
           let v = try? JSONDecoder().decode(VersionInfo.self, from: data) {
            installed = v.installed; latest = v.latest; updateAvailable = v.updateAvailable
        } else if let data = try? await client.get("api/health"),
                  let h = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            // Hub vor 2.2 kennt /api/hub/version noch nicht
            installed = h["version"] as? String ?? "2.0"
        }
    }

    /// Update starten und warten, bis der Hub mit der neuen Version wieder da ist (max. ~2 Min.)
    func update() async {
        guard !updating else { return }
        updating = true
        message = "Hub wird aktualisiert …"
        let before = installed
        do {
            _ = try await client.send("api/hub/update", method: "POST", body: Data())
        } catch let e as ServerError {
            // Hub hat geantwortet und abgelehnt (z. B. Hub vor 2.3: nur Besitzer) – nicht zwei Minuten warten
            message = e.message
            updating = false
            return
        } catch {
            // Der Hub startet dabei neu – ein abgebrochener Aufruf ist normal; Fehler zeigt erst die Prüfung unten
        }
        for _ in 0..<24 {
            try? await Task.sleep(for: .seconds(5))
            await check(force: true)
            if let i = installed, i != before {
                message = "Hub ist jetzt auf Version \(i)."
                updating = false
                Task { try? await Task.sleep(for: .seconds(8)); self.message = nil }
                return
            }
        }
        message = "Das Update hat nicht geklappt – auf der Einrichtungsseite unter System → Protokoll nachsehen."
        updating = false
    }

    static func compare(_ a: String, _ b: String) -> Int {
        let x = a.split(separator: ".").map { Int($0) ?? 0 }, y = b.split(separator: ".").map { Int($0) ?? 0 }
        for i in 0..<max(x.count, y.count) {
            let l = i < x.count ? x[i] : 0, r = i < y.count ? y[i] : 0
            if l != r { return l < r ? -1 : 1 }
        }
        return 0
    }
}

/// Hinweis oben auf dem Home-Bildschirm (nur wenn es etwas zu tun gibt)
struct HubUpdateBanner: View {
    @Environment(HubUpdater.self) private var updater
    @Environment(AppSettings.self) private var settings

    var body: some View {
        if !settings.demo, updater.hubTooOld || (canUpdate && updater.updateAvailable) || updater.updating || updater.message != nil {
            HStack(spacing: 12) {
                Image(systemName: updater.updating ? "arrow.triangle.2.circlepath" : "arrow.down.circle.fill")
                    .font(.title3).foregroundStyle(Theme.solar)
                    .symbolEffect(.pulse, isActive: updater.updating)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.subheadline.weight(.bold))
                    Text(subtitle).font(.caption).foregroundStyle(Theme.muted)
                }
                Spacer()
                if canUpdate && !updater.updating && (updater.updateAvailable || updater.hubTooOld) {
                    Button("Aktualisieren") { Task { await updater.update() } }
                        .font(.caption.weight(.bold)).buttonStyle(.borderedProminent).tint(Theme.solar).foregroundStyle(Theme.bg)
                }
            }
            .card(padding: 12)
        }
    }

    /// Besitzer und Vollzugriff (Gäste nicht) – so wie der Hub ab 2.3 prüft
    private var canUpdate: Bool { settings.permissions.edit }

    private var title: String {
        if updater.updating { return "Hub wird aktualisiert" }
        if let m = updater.message, !updater.updateAvailable { return m }
        if updater.hubTooOld { return "Hub bitte aktualisieren" }
        return "Hub-Update verfügbar"
    }

    private var subtitle: String {
        let from = updater.installed ?? "?"
        let to = updater.latest ?? HubUpdater.requiredHub
        if updater.updating { return "Dauert etwa eine Minute – die App verbindet sich danach von selbst." }
        if updater.hubTooOld && !canUpdate { return "Diese App braucht Hub \(HubUpdater.requiredHub) – bitte den Besitzer fragen." }
        return "Version \(from) → \(to)"
    }
}
