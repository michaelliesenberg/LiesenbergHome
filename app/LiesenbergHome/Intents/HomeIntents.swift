import AppIntents
import Foundation

// Siri & Kurzbefehle: „Hey Siri, Liesenberg Home Esstisch an“, „… Garagentor auf“, „… Rollläden Wohnzimmer runter“.
// Eigene Sätze: Kurzbefehle-App → Neuer Kurzbefehl → Aktion „Liesenberg Home“ wählen → Namen = Sprachbefehl.

// MARK: - Geräte vom Hub als Siri-Objekte

struct DeviceEntity: AppEntity, Identifiable {
    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Gerät")
    static var defaultQuery = DeviceQuery()

    let id: String
    let name: String
    let room: String

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(name)", subtitle: "\(room)")
    }
}

struct DeviceQuery: EntityStringQuery {
    @MainActor
    static func all() -> [(DeviceEntity, Device)] {
        AppServices.shared.home.floors.flatMap { floor in
            floor.rooms.flatMap { room in
                room.devices.filter { $0.kind != .other }.map {
                    (DeviceEntity(id: $0.id, name: $0.name, room: room.name), $0)
                }
            }
        }
    }

    func entities(for identifiers: [String]) async throws -> [DeviceEntity] {
        await MainActor.run { Self.all().map { $0.0 }.filter { identifiers.contains($0.id) } }
    }

    func entities(matching string: String) async throws -> [DeviceEntity] {
        await MainActor.run {
            Self.all().map { $0.0 }.filter {
                $0.name.localizedCaseInsensitiveContains(string) || "\($0.room) \($0.name)".localizedCaseInsensitiveContains(string)
            }
        }
    }

    func suggestedEntities() async throws -> [DeviceEntity] {
        await MainActor.run { Self.all().map { $0.0 } }
    }
}

@MainActor
private func findDevice(for entity: DeviceEntity) throws -> Device {
    guard let d = DeviceQuery.all().first(where: { $0.0.id == entity.id })?.1 else {
        throw ServerError(message: "\(entity.name) gibt es nicht mehr")
    }
    return d
}

@MainActor
private func run(_ entity: DeviceEntity, _ body: [String: Any]) async throws -> Device {
    let d = try findDevice(for: entity)
    try await AppServices.shared.home.command(d, body)
    return d
}

// MARK: - Aktionen

struct TurnOnIntent: AppIntent {
    static var title: LocalizedStringResource = "Einschalten"
    static var description = IntentDescription("Schaltet ein Licht oder Gerät ein.")
    @Parameter(title: "Gerät") var device: DeviceEntity
    static var parameterSummary: some ParameterSummary { Summary("\(\.$device) einschalten") }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let d = try await run(device, ["on": true])
        return .result(dialog: "\(d.name) ist an.")
    }
}

struct TurnOffIntent: AppIntent {
    static var title: LocalizedStringResource = "Ausschalten"
    static var description = IntentDescription("Schaltet ein Licht oder Gerät aus.")
    @Parameter(title: "Gerät") var device: DeviceEntity
    static var parameterSummary: some ParameterSummary { Summary("\(\.$device) ausschalten") }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let d = try await run(device, ["on": false])
        return .result(dialog: "\(d.name) ist aus.")
    }
}

struct SetBrightnessIntent: AppIntent {
    static var title: LocalizedStringResource = "Helligkeit setzen"
    @Parameter(title: "Licht") var device: DeviceEntity
    @Parameter(title: "Prozent", inclusiveRange: (0, 100)) var percent: Int
    static var parameterSummary: some ParameterSummary { Summary("\(\.$device) auf \(\.$percent) % dimmen") }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let d = try await run(device, ["level": percent])
        return .result(dialog: "\(d.name) auf \(percent) %.")
    }
}

enum BlindDirection: String, AppEnum {
    case up, down
    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Richtung")
    static var caseDisplayRepresentations: [BlindDirection: DisplayRepresentation] = [
        .up: "hoch", .down: "runter",
    ]
}

struct MoveBlindIntent: AppIntent {
    static var title: LocalizedStringResource = "Rollladen fahren"
    @Parameter(title: "Fenster") var device: DeviceEntity
    @Parameter(title: "Richtung") var direction: BlindDirection
    static var parameterSummary: some ParameterSummary { Summary("\(\.$device) \(\.$direction)") }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let d = try await run(device, ["move": direction == .up ? "up" : "down"])
        return .result(dialog: "\(d.name) fährt \(direction == .up ? "hoch" : "runter").")
    }
}

// Siri-Sätze erlauben nur EINEN Parameter → Hoch/Runter als eigene Aktionen
struct BlindUpIntent: AppIntent {
    static var title: LocalizedStringResource = "Rollladen hoch"
    @Parameter(title: "Fenster") var device: DeviceEntity
    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let d = try await run(device, ["move": "up"])
        return .result(dialog: "\(d.name) fährt hoch.")
    }
}

struct BlindDownIntent: AppIntent {
    static var title: LocalizedStringResource = "Rollladen runter"
    @Parameter(title: "Fenster") var device: DeviceEntity
    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let d = try await run(device, ["move": "down"])
        return .result(dialog: "\(d.name) fährt runter.")
    }
}

/// Tore & Türöffner: nur bei entsperrtem iPhone (wie in Apples Home-App).
struct TriggerGateIntent: AppIntent {
    static var title: LocalizedStringResource = "Tor / Tür auslösen"
    static var description = IntentDescription("Öffnet oder schließt ein Tor. Funktioniert nur bei entsperrtem iPhone.")
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication
    @Parameter(title: "Tor") var device: DeviceEntity
    static var parameterSummary: some ParameterSummary { Summary("\(\.$device) auslösen") }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let d = try findDevice(for: device)
        try await AppServices.shared.home.command(d, d.kind == .gate ? ["trigger": true] : ["on": true])
        return .result(dialog: "\(d.name) ausgelöst.")
    }
}

// MARK: - Fertige Siri-Sätze (ohne Einrichtung)

struct LiesenbergShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: TurnOnIntent(), phrases: [
            "\(.applicationName) \(\.$device) an",
            "\(.applicationName) \(\.$device) einschalten",
            "Schalte \(\.$device) mit \(.applicationName) ein",
        ], shortTitle: "Einschalten", systemImageName: "lightbulb.fill")
        AppShortcut(intent: TurnOffIntent(), phrases: [
            "\(.applicationName) \(\.$device) aus",
            "\(.applicationName) \(\.$device) ausschalten",
            "Schalte \(\.$device) mit \(.applicationName) aus",
        ], shortTitle: "Ausschalten", systemImageName: "lightbulb")
        AppShortcut(intent: TriggerGateIntent(), phrases: [
            "\(.applicationName) \(\.$device) auf",
            "\(.applicationName) \(\.$device) öffnen",
        ], shortTitle: "Tor öffnen", systemImageName: "door.garage.open")
        AppShortcut(intent: BlindUpIntent(), phrases: [
            "\(.applicationName) \(\.$device) hoch",
        ], shortTitle: "Rollladen hoch", systemImageName: "blinds.horizontal.open")
        AppShortcut(intent: BlindDownIntent(), phrases: [
            "\(.applicationName) \(\.$device) runter",
        ], shortTitle: "Rollladen runter", systemImageName: "blinds.horizontal.closed")
        AppShortcut(intent: SetBrightnessIntent(), phrases: [
            "\(.applicationName) \(\.$device) dimmen",
        ], shortTitle: "Dimmen", systemImageName: "slider.horizontal.3")
        AppShortcut(intent: CentralOffIntent(), phrases: [
            "\(.applicationName) alles aus",
            "\(.applicationName) Zentral aus",
        ], shortTitle: "Zentral aus", systemImageName: "power")
        AppShortcut(intent: RunSceneIntent(), phrases: [
            "\(.applicationName) Szene \(\.$scene)",
            "\(.applicationName) \(\.$scene) starten",
        ], shortTitle: "Szene starten", systemImageName: "sparkles")
        AppShortcut(intent: StopSceneIntent(), phrases: [
            "\(.applicationName) Szene \(\.$scene) beenden",
        ], shortTitle: "Szene aus", systemImageName: "stop.circle")
    }
}

// MARK: - Szenen & Zentral aus

struct SceneEntity: AppEntity, Identifiable {
    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Szene")
    static var defaultQuery = SceneQuery()
    let id: String
    let name: String
    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(name)") }
}

struct SceneQuery: EntityStringQuery {
    @MainActor
    static func all() async -> [SceneEntity] {
        let store = AppServices.shared.scenes
        if store.scenes.isEmpty { await store.refresh() }
        return store.scenes.compactMap { s in s.id.map { SceneEntity(id: $0, name: s.name) } }
    }
    func entities(for identifiers: [String]) async throws -> [SceneEntity] {
        await Self.all().filter { identifiers.contains($0.id) }
    }
    func entities(matching string: String) async throws -> [SceneEntity] {
        await Self.all().filter { $0.name.localizedCaseInsensitiveContains(string) }
    }
    func suggestedEntities() async throws -> [SceneEntity] { await Self.all() }
}

struct RunSceneIntent: AppIntent {
    static var title: LocalizedStringResource = "Szene starten"
    @Parameter(title: "Szene") var scene: SceneEntity
    static var parameterSummary: some ParameterSummary { Summary("Szene \(\.$scene) starten") }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let store = AppServices.shared.scenes
        if store.scenes.isEmpty { await store.refresh() }
        guard let s = store.scenes.first(where: { $0.id == scene.id }) else {
            throw ServerError(message: "Szene \(scene.name) gibt es nicht mehr")
        }
        let ok = await store.run(s)
        guard ok else { throw ServerError(message: "Szene \(scene.name) hat nicht geklappt") }
        return .result(dialog: "\(scene.name) ist an.")
    }
}

struct StopSceneIntent: AppIntent {
    static var title: LocalizedStringResource = "Szene ausschalten"
    @Parameter(title: "Szene") var scene: SceneEntity

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let store = AppServices.shared.scenes
        if store.scenes.isEmpty { await store.refresh() }
        guard let s = store.scenes.first(where: { $0.id == scene.id }) else {
            throw ServerError(message: "Szene \(scene.name) gibt es nicht mehr")
        }
        let ok = await store.run(s, stop: true)
        guard ok else { throw ServerError(message: "Szene \(scene.name) hat nicht geklappt") }
        return .result(dialog: "\(scene.name) ist aus.")
    }
}

struct CentralOffIntent: AppIntent {
    static var title: LocalizedStringResource = "Zentral aus"
    static var description = IntentDescription("Schaltet alle Lichter aus – wie der Zentral-aus-Taster.")

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let ok = await AppServices.shared.scenes.centralOff()
        guard ok else { throw ServerError(message: "Zentral aus hat nicht geklappt") }
        return .result(dialog: "Alles aus.")
    }
}
