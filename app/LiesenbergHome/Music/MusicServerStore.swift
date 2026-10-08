import Foundation
import Observation

/// HomePods & AirPlay-Lautsprecher über den Haus-Server (pyatv) – inkl. Zeitplänen.
struct Speaker: Codable, Identifiable, Equatable {
    let id: String
    var name: String?
    var model: String?
    var state: String?
    var playing: Bool?
    var title: String?
    var artist: String?
    var volume: Double?
    var online: Bool?
}

struct MusicSchedule: Codable, Identifiable, Equatable {
    var id: String?
    var name: String = ""
    var time: String = "07:00"
    var days: [Int] = [0, 1, 2, 3, 4]         // 0 = Montag
    var devices: [String] = []
    var action: String = "radio"              // radio | play | pause
    var station: String? = nil
    var volume: Double? = 25
    var enabled: Bool = true

    var stableID: String { id ?? UUID().uuidString }
    static let dayNames = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    var daysText: String {
        if days.sorted() == [0, 1, 2, 3, 4] { return "Mo–Fr" }
        if days.sorted() == [5, 6] { return "Sa, So" }
        if days.count == 7 { return "täglich" }
        return days.sorted().map { Self.dayNames[$0] }.joined(separator: ", ")
    }
    var actionText: String {
        switch action {
        case "radio": return station ?? "Radio"
        case "play": return "Weiterspielen"
        default: return "Ausschalten"
        }
    }
}

private struct MusicState: Codable {
    var speakers: [Speaker]
    var schedules: [MusicSchedule]
    var radio: [String]
    var error: String?
}

@MainActor
@Observable
final class MusicServerStore {
    private(set) var speakers: [Speaker] = []
    private(set) var schedules: [MusicSchedule] = []
    private(set) var stations: [String] = []
    var lastError: String?
    let client: HouseClient

    init(client: HouseClient) { self.client = client }

    func refresh() async {
        do {
            let st = try JSONDecoder().decode(MusicState.self, from: try await client.get("api/music"))
            speakers = st.speakers; schedules = st.schedules; stations = st.radio
            lastError = st.error
        } catch {
            lastError = error.localizedDescription
        }
    }

    func command(_ s: Speaker, _ action: String, value: Any? = nil) {
        if action == "volume", let v = value as? Double, let i = speakers.firstIndex(of: s) { speakers[i].volume = v }
        Task {
            var body: [String: Any] = ["action": action]
            if let value { body["value"] = value }
            do {
                _ = try await client.send("api/music/speaker/\(s.id)", method: "POST",
                                          body: try JSONSerialization.data(withJSONObject: body))
                try? await Task.sleep(for: .seconds(2))
                await refresh()
            } catch { lastError = error.localizedDescription }
        }
    }

    func save(_ s: MusicSchedule) async {
        do {
            _ = try await client.send("api/music/schedules", method: "PUT", body: try JSONEncoder().encode(s))
            await refresh()
        } catch { lastError = error.localizedDescription }
    }

    func delete(_ s: MusicSchedule) async {
        guard let id = s.id else { return }
        _ = try? await client.send("api/music/schedules/\(id)", method: "DELETE", body: Data())
        await refresh()
    }
}
