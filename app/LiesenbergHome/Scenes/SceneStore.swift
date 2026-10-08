import Foundation
import Observation

// Szenen liegen auf dem Pi (scenes.json) – der Pi führt auch die Zeitpläne aus,
// d. h. „Garten 18:30 an, 23:00 aus" läuft auch, wenn das iPhone aus ist.

struct SceneTime: Codable, Equatable {
    enum Kind: String, Codable, CaseIterable { case time, sunset, sunrise }
    var type: Kind = .time
    var time: String? = "18:00"     // nur bei .time
    var offset: Int? = 0            // Minuten ±, nur bei Sonne

    var text: String {
        switch type {
        case .time: return time ?? "–"
        case .sunset, .sunrise:
            let base = type == .sunset ? "Sonnenuntergang" : "Sonnenaufgang"
            guard let o = offset, o != 0 else { return base }
            return "\(base) \(o > 0 ? "+" : "−")\(abs(o)) Min"
        }
    }
}

struct SceneSchedule: Codable, Equatable {
    var enabled: Bool = false
    var days: [Int] = [0, 1, 2, 3, 4, 5, 6]     // 0 = Montag
    var start: SceneTime? = nil
    var stop: SceneTime? = nil

    static let dayNames = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    var daysText: String {
        let d = days.sorted()
        if d.count == 7 { return "täglich" }
        if d == [0, 1, 2, 3, 4] { return "Mo–Fr" }
        if d == [5, 6] { return "Sa, So" }
        return d.map { Self.dayNames[$0] }.joined(separator: ", ")
    }
}

/// Was ein Gerät in der Szene tun soll. Je nach Gerätetyp ist nur ein Feld gesetzt.
struct SceneAction: Codable, Equatable, Identifiable {
    var fid: String
    var name: String?
    var on: Bool?           // Licht/Schalter
    var level: Double?      // Dimmer 0…100 (0 = aus)
    var position: Double?   // Rollladen 0 = offen … 100 = zu
    var target: Double?     // Heizung °C
    var id: String { fid }
}

struct SceneRun: Codable, Equatable {
    var phase: String
    var at: String
    var ok: Bool
    var errors: [String]?
}

struct HomeScene: Codable, Identifiable, Equatable {
    var id: String?
    var name: String = ""
    var icon: String = "sparkles"
    var actions: [SceneAction] = []
    var schedule = SceneSchedule()
    var today: [String: String]?      // vom Pi: heutige Start/Stopp-Uhrzeit (Sonne schon ausgerechnet)
    var lastRun: SceneRun?

    var stableID: String { id ?? "neu" }

    var scheduleText: String? {
        guard schedule.enabled, schedule.start != nil || schedule.stop != nil else { return nil }
        let start = today?["start"] ?? schedule.start?.text
        let stop = today?["stop"] ?? schedule.stop?.text
        let span = [start, stop].compactMap { $0 }.joined(separator: "–")
        return "\(span) · \(schedule.daysText)"
    }

    static let icons = ["sparkles", "tree", "sun.max", "moon.stars", "sofa", "fork.knife",
                        "bed.double", "film", "party.popper", "house", "lightbulb", "blinds.horizontal.closed"]
}

private struct SceneList: Codable { var scenes: [HomeScene] }

@MainActor
@Observable
final class SceneStore {
    private(set) var scenes: [HomeScene] = []
    var lastError: String?
    /// Kurzes Feedback auf der Kachel (✓ oder Fehler) – Szene-ID → Text
    var flash: [String: String] = [:]
    let client: HouseClient

    init(client: HouseClient) { self.client = client }

    func refresh() async {
        do {
            let fresh = try JSONDecoder().decode(SceneList.self, from: try await client.get("api/scenes")).scenes
            let namesChanged = fresh.map(\.name) != scenes.map(\.name)
            scenes = fresh
            lastError = nil
            if namesChanged { LiesenbergShortcuts.updateAppShortcutParameters() }   // Siri kennt neue Szenen
        } catch {
            lastError = error.localizedDescription
        }
    }

    func save(_ s: HomeScene) async {
        var s = s
        s.today = nil; s.lastRun = nil
        do {
            _ = try await client.send("api/scenes", method: "PUT", body: try JSONEncoder().encode(s))
            await refresh()
        } catch { lastError = "Speichern fehlgeschlagen: \(error.localizedDescription)" }
    }

    func delete(_ s: HomeScene) async {
        guard let id = s.id else { return }
        _ = try? await client.send("api/scenes/\(id)", method: "DELETE", body: Data())
        await refresh()
    }

    @discardableResult
    func run(_ s: HomeScene, stop: Bool = false) async -> Bool {
        guard let id = s.id else { return false }
        flash[id] = "…"
        do {
            let data = try await client.send("api/scenes/\(id)/run?phase=\(stop ? "stop" : "start")", method: "POST", body: Data())
            let r = try JSONDecoder().decode(SceneRun.self, from: data)
            flash[id] = r.ok ? (stop ? "aus ✓" : "✓") : (r.errors?.first ?? "Fehler")
            clearFlash(id)
            return r.ok
        } catch {
            flash[id] = "nicht angekommen"
            clearFlash(id)
            return false
        }
    }

    @discardableResult
    func centralOff() async -> Bool {
        flash["central"] = "…"
        do {
            _ = try await client.send("api/home/central-off", method: "POST", body: Data())
            flash["central"] = "✓"
            clearFlash("central")
            return true
        } catch {
            flash["central"] = "nicht angekommen"
            clearFlash("central")
            return false
        }
    }

    private func clearFlash(_ id: String) {
        Task { try? await Task.sleep(for: .seconds(3)); flash[id] = nil }
    }
}

// MARK: - Nachsichtig dekodieren
// Eine ältere oder von Hand bearbeitete Szene auf dem Hub darf nicht die ganze Liste kaputt machen:
// fehlende Felder bekommen Standardwerte. (In Extensions, damit die normalen Initialisierer bleiben.)

extension SceneTime {
    private enum K: String, CodingKey { case type, time, offset }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: K.self)
        type = (try? c.decodeIfPresent(Kind.self, forKey: .type)) ?? .time
        time = try? c.decodeIfPresent(String.self, forKey: .time)
        offset = (try? c.decodeIfPresent(Int.self, forKey: .offset)) ?? 0
    }
}

extension SceneSchedule {
    private enum K: String, CodingKey { case enabled, days, start, stop }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: K.self)
        enabled = (try? c.decodeIfPresent(Bool.self, forKey: .enabled)) ?? false
        days = (try? c.decodeIfPresent([Int].self, forKey: .days)) ?? [0, 1, 2, 3, 4, 5, 6]
        start = try? c.decodeIfPresent(SceneTime.self, forKey: .start)
        stop = try? c.decodeIfPresent(SceneTime.self, forKey: .stop)
    }
}

extension SceneAction {
    private enum K: String, CodingKey { case fid, id, name, on, level, position, target }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: K.self)
        fid = (try? c.decodeIfPresent(String.self, forKey: .fid)) ?? (try? c.decodeIfPresent(String.self, forKey: .id)) ?? ""
        name = try? c.decodeIfPresent(String.self, forKey: .name)
        on = try? c.decodeIfPresent(Bool.self, forKey: .on)
        level = try? c.decodeIfPresent(Double.self, forKey: .level)
        position = try? c.decodeIfPresent(Double.self, forKey: .position)
        target = try? c.decodeIfPresent(Double.self, forKey: .target)
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: K.self)
        try c.encode(fid, forKey: .fid)
        try c.encodeIfPresent(name, forKey: .name)
        try c.encodeIfPresent(on, forKey: .on)
        try c.encodeIfPresent(level, forKey: .level)
        try c.encodeIfPresent(position, forKey: .position)
        try c.encodeIfPresent(target, forKey: .target)
    }
}

extension HomeScene {
    private enum K: String, CodingKey { case id, name, icon, actions, schedule, today, lastRun }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: K.self)
        id = try? c.decodeIfPresent(String.self, forKey: .id)
        name = (try? c.decodeIfPresent(String.self, forKey: .name)) ?? "Szene"
        icon = (try? c.decodeIfPresent(String.self, forKey: .icon)) ?? "sparkles"
        actions = ((try? c.decodeIfPresent([SceneAction].self, forKey: .actions)) ?? []).filter { !$0.fid.isEmpty }
        schedule = (try? c.decodeIfPresent(SceneSchedule.self, forKey: .schedule)) ?? SceneSchedule()
        today = try? c.decodeIfPresent([String: String].self, forKey: .today)
        lastRun = try? c.decodeIfPresent(SceneRun.self, forKey: .lastRun)
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: K.self)
        try c.encodeIfPresent(id, forKey: .id)
        try c.encode(name, forKey: .name)
        try c.encode(icon, forKey: .icon)
        try c.encode(actions, forKey: .actions)
        try c.encode(schedule, forKey: .schedule)
    }
}
