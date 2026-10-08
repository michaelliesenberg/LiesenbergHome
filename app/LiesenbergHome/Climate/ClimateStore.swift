import Foundation
import Observation

/// Daikin-Klimageräte (über den Haus-Server, Daikin Onecta).
struct ClimateUnit: Codable, Identifiable, Equatable {
    let id: String
    var name: String
    var on: Bool
    var mode: String
    var modes: [String]?
    var roomTemperature: Double?
    var outdoorTemperature: Double?
    var target: Double?
    var targetMin: Double?
    var targetMax: Double?
    var targetStep: Double?
    var online: Bool?
    var room: String?

    var modeName: String {
        ["cooling": "Kühlen", "heating": "Heizen", "auto": "Automatik", "dry": "Entfeuchten",
         "fanOnly": "Lüften"][mode] ?? mode
    }
}

struct ClimateState: Codable {
    var configured: Bool
    var loggedIn: Bool
    var error: String?
    var units: [ClimateUnit]
}

@MainActor
@Observable
final class ClimateStore {
    private(set) var state: ClimateState?
    var lastError: String?
    let client: HouseClient

    init(client: HouseClient) { self.client = client }

    var units: [ClimateUnit] { state?.units ?? [] }
    func units(in room: Room) -> [ClimateUnit] { units.filter { $0.room == room.name } }

    func refresh() async {
        do {
            let data = try await client.get("api/daikin")
            state = try JSONDecoder().decode(ClimateState.self, from: data)
            lastError = nil
        } catch {
            lastError = "Klima nicht erreichbar"
        }
    }

    func setOn(_ u: ClimateUnit, _ on: Bool) { send(u, ["on": on]) { $0.on = on } }
    func setMode(_ u: ClimateUnit, _ mode: String) { send(u, ["mode": mode]) { $0.mode = mode } }
    func changeTarget(_ u: ClimateUnit, by delta: Double) {
        var t = (u.target ?? 22) + delta
        if let lo = u.targetMin { t = max(lo, t) }
        if let hi = u.targetMax { t = min(hi, t) }
        send(u, ["target": t]) { $0.target = t }
    }

    private func send(_ u: ClimateUnit, _ body: [String: Any], optimistic: (inout ClimateUnit) -> Void) {
        if let i = state?.units.firstIndex(where: { $0.id == u.id }) { optimistic(&state!.units[i]) }
        Task {
            do {
                let data = try JSONSerialization.data(withJSONObject: body)
                _ = try await client.send("api/daikin/\(u.id)", method: "PUT", body: data)
                lastError = nil
            } catch {
                lastError = "Befehl an Daikin fehlgeschlagen"
                await refresh()
            }
        }
    }
}
