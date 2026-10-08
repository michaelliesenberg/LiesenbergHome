import Foundation
import LocalAuthentication

/// Eine gemeinsame Instanz aller Dienste – genutzt von der App UND von Siri/Kurzbefehlen.
@MainActor
final class AppServices {
    static let shared = AppServices()

    let settings: AppSettings
    let client: HouseClient
    let home: HomeStore
    let energy: EnergyStore
    let climate: ClimateStore
    let music: MusicServerStore
    let scenes: SceneStore
    let lock: AppLock

    private init() {
        settings = AppSettings()
        client = HouseClient(settings: settings)
        home = HomeStore(client: client)
        energy = EnergyStore(client: client)
        climate = ClimateStore(client: client)
        music = MusicServerStore(client: client)
        scenes = SceneStore(client: client)
        lock = AppLock(settings: settings)
    }

    /// Einladung einlösen (QR-Code, Link aus Nachricht oder Kamera-App)
    func join(_ url: URL) async throws {
        let (resp, local, remote) = try await client.join(link: url)
        settings.apply(join: resp, local: local, remote: remote)
        didConnect()
    }

    /// Nach dem Verbinden / Wechsel des Zuhauses alles frisch laden
    func didConnect() {
        home.reset()
        client.reset()
        Task {
            await home.syncFromServer()
            await scenes.refresh()
        }
    }

    /// Rolle, Rechte, Name und Adressen vom Hub übernehmen (können sich jederzeit ändern)
    func refreshMe() async {
        guard !settings.demo, let data = try? await client.get("api/me"),
              let me = try? JSONDecoder().decode(MeResponse.self, from: data) else { return }
        settings.apply(me: me)
    }

    func disconnect() {
        settings.disconnect()
        home.reset()
        client.reset()
    }
}

/// App mit Face ID / Code sperren (optional, in den Einstellungen)
@MainActor
@Observable
final class AppLock {
    private(set) var locked: Bool
    private let settings: AppSettings
    private var backgroundedAt: Date?
    private var authenticating = false

    init(settings: AppSettings) {
        self.settings = settings
        locked = settings.faceID
    }

    /// Sperren, wenn die App länger als 30 s im Hintergrund war
    func didEnterBackground() {
        if settings.faceID { backgroundedAt = Date() }
    }

    func willEnterForeground() {
        guard settings.faceID, let t = backgroundedAt else { return }
        backgroundedAt = nil
        if Date().timeIntervalSince(t) > 30 { locked = true }
    }

    func unlock() {
        guard settings.faceID else { locked = false; return }
        guard !authenticating else { return }        // Face-ID-Abfrage läuft schon
        authenticating = true
        LAContext().evaluatePolicy(.deviceOwnerAuthentication, localizedReason: "Zuhause entsperren") { ok, _ in
            Task { @MainActor in
                self.authenticating = false
                if ok { self.locked = false }
            }
        }
    }

    func setEnabled(_ on: Bool) {
        if on {
            let ctx = LAContext()
            ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: "Face ID für die App einschalten") { ok, _ in
                Task { @MainActor in if ok { self.settings.faceID = true } }
            }
        } else {
            settings.faceID = false
            locked = false
        }
    }
}
