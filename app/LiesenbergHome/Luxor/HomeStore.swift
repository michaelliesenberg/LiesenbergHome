import Foundation
import Observation

/// Räume & Geräte vom Hub – Zustände lesen, schalten (mit sofortiger Anzeige).
@MainActor
@Observable
final class HomeStore {
    private(set) var structure: HomeStructure?
    private(set) var states: [String: DeviceState] = [:]
    var lastError: String?

    /// Gerade geschaltete Werte bleiben stehen, bis der Hub sie bestätigt (Dimmer blenden einige Sekunden).
    private var pending: [String: (state: DeviceState, until: Date)] = [:]
    let client: HouseClient

    init(client: HouseClient) {
        self.client = client
        structure = Self.loadSaved()
    }

    var floors: [Floor] { structure?.floors ?? [] }

    func reset() {
        structure = nil
        states = [:]
        try? FileManager.default.removeItem(at: Self.saveURL)
    }

    /// Räume vom Hub holen.
    func syncFromServer() async {
        do {
            let s = try JSONDecoder().decode(HomeStructure.self, from: try await client.get("api/home"))
            if s != structure {
                structure = s
                save()
                LiesenbergShortcuts.updateAppShortcutParameters()   // Siri kennt die neuen Gerätenamen
            }
            lastError = nil
        } catch {
            if structure == nil { lastError = error.localizedDescription }
        }
    }

    func device(_ id: String) -> Device? { structure?.allDevices.first { $0.id == id } }

    // MARK: - Zustände

    func refresh(_ room: Room) async { await refresh(ids: room.devices.map(\.id)) }

    func refresh(ids: [String]) async {
        let ids = ids.filter { device($0)?.kind != .gate }
        guard !ids.isEmpty else { return }
        do {
            let path = "api/home/state?ids=" + (ids.joined(separator: ",").addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")
            let fresh = try JSONDecoder().decode([String: DeviceState].self, from: try await client.get(path))
            let now = Date()
            for (id, st) in fresh {
                if let p = pending[id] {
                    if Self.matches(st, p.state) || now > p.until { pending[id] = nil } else { continue }
                }
                states[id] = st
            }
            lastError = nil
        } catch {
            lastError = "Hub nicht erreichbar"
        }
    }

    private static func matches(_ real: DeviceState, _ want: DeviceState) -> Bool {
        func close(_ a: Double?, _ b: Double?, _ tol: Double) -> Bool {
            guard let b else { return true }
            guard let a else { return false }
            return abs(a - b) <= tol
        }
        return (want.on == nil || real.on == want.on) && close(real.level, want.level, 3)
            && close(real.position, want.position, 3) && close(real.target, want.target, 0.2)
    }

    // MARK: - Abgeleitete Werte

    func level(_ d: Device) -> Double {
        let s = states[d.id]
        if d.kind == .dimmer, let l = s?.level { return l.rounded() }
        return (s?.on ?? false) ? 100 : 0
    }
    func isOn(_ d: Device) -> Bool { level(d) > 0 }
    func closed(_ d: Device) -> Double { (states[d.id]?.position ?? 0).rounded() }
    func actual(_ d: Device) -> Double? { states[d.id]?.current }
    func target(_ d: Device) -> Double? { states[d.id]?.target }
    func lightsOn(in room: Room) -> Int { room.lights.filter { isOn($0) }.count }

    // MARK: - Schalten

    func setLevel(_ d: Device, percent: Double) {
        let p = max(0, min(100, percent)).rounded()
        if d.kind == .dimmer {
            hold(d, DeviceState(on: p > 0, level: p))
            send(d, ["level": p])
        } else {
            setOn(d, p > 0)
        }
    }

    func toggle(_ d: Device) { setOn(d, !isOn(d)) }

    func setOn(_ d: Device, _ on: Bool) {
        if d.kind == .dimmer {
            // Einschaltwert kennt nur der Aktor → bis zur Rückmeldung „an" zeigen
            hold(d, DeviceState(on: on, level: on ? nil : 0))
            if on, (states[d.id]?.level ?? 0) == 0 { states[d.id]?.level = 100 }
        } else {
            hold(d, DeviceState(on: on))
        }
        send(d, ["on": on])
    }

    func allLightsOff(in room: Room) { room.lights.filter { isOn($0) }.forEach { setOn($0, false) } }

    func blindUp(_ d: Device) { send(d, ["move": "up"]) }
    func blindDown(_ d: Device) { send(d, ["move": "down"]) }
    func blindStop(_ d: Device) { send(d, ["move": "stop"]) }
    func setClosed(_ d: Device, percent: Double) {
        let p = max(0, min(100, percent)).rounded()
        hold(d, DeviceState(position: p), seconds: 60)
        send(d, ["position": p])
    }

    func changeTarget(_ d: Device, by delta: Double) {
        let new = (target(d) ?? 21) + delta
        hold(d, DeviceState(target: new))
        send(d, ["target": new])
    }

    func trigger(_ d: Device) async throws {
        try await command(d, ["trigger": true])
    }

    func command(_ d: Device, _ body: [String: Any]) async throws {
        let id = d.id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? d.id
        _ = try await client.send("api/home/device/\(id)", method: "POST",
                                  body: try JSONSerialization.data(withJSONObject: body))
    }

    private func hold(_ d: Device, _ want: DeviceState, seconds: Double = 12) {
        var s = states[d.id] ?? DeviceState()
        if let v = want.on { s.on = v }
        if let v = want.level { s.level = v }
        if let v = want.position { s.position = v }
        if let v = want.target { s.target = v }
        states[d.id] = s
        pending[d.id] = (want, Date().addingTimeInterval(seconds))
    }

    private func send(_ d: Device, _ body: [String: Any]) {
        Task {
            do { try await command(d, body); lastError = nil }
            catch {
                pending[d.id] = nil
                lastError = "Befehl nicht angekommen: \(error.localizedDescription)"
            }
        }
    }

    // MARK: - Speichern (schneller Start, auch offline)

    private static var saveURL: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("home-structure.json")
    }

    private func save() {
        guard let structure else { return }
        let url = Self.saveURL
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? JSONEncoder().encode(structure).write(to: url, options: .atomic)
    }

    private static func loadSaved() -> HomeStructure? {
        guard let data = try? Data(contentsOf: saveURL) else { return nil }
        return try? JSONDecoder().decode(HomeStructure.self, from: data)
    }
}
