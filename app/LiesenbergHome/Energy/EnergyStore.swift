import Foundation
import Observation

/// Live-Energiedaten aus evcc (über den Haus-Server).
@MainActor
@Observable
final class EnergyStore {
    struct Sample: Codable, Identifiable {
        var id: Date { time }
        let time: Date
        let watts: Double
    }

    struct Loadpoint: Identifiable {
        let id: Int
        let title: String
        let vehicle: String
        let mode: String
        let chargePower: Double
        let soc: Double?
        let targetSoc: Double?
        let connected: Bool
        let charging: Bool
    }

    // Leistungen in Watt (Netz: + Bezug / − Einspeisung, Akku: + Entladen / − Laden – wie evcc)
    private(set) var pv: Double = 0
    /// Leistung je Wechselrichter (Reihenfolge wie in evcc)
    private(set) var pvParts: [Double] = []
    private(set) var pvTitles: [String] = []
    private(set) var home: Double = 0
    private(set) var grid: Double = 0
    private(set) var battery: Double = 0
    private(set) var batterySoc: Double = 0
    private(set) var batteryCapacityKWh: Double = 16.56
    private(set) var loadpoints: [Loadpoint] = []
    private(set) var forecast: [Sample] = []
    private(set) var forecastTodayKWh: Double?
    private(set) var produced: [Sample] = []
    private(set) var updated: Date?
    private(set) var online = false

    let client: HouseClient
    private var pollTask: Task<Void, Never>?

    init(client: HouseClient) {
        self.client = client
        produced = Self.loadToday()
    }

    func start() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    func stop() { pollTask?.cancel(); pollTask = nil }

    func refresh() async {
        do {
            let s = try await client.energyState()
            apply(s)
            online = true
        } catch {
            online = false
        }
    }

    // MARK: - Abgeleitet

    var batteryCharging: Bool { battery < -50 }
    var feedingIn: Bool { grid < -50 }
    var batteryKWh: Double { batterySoc / 100 * batteryCapacityKWh }

    /// Anteil des Hausverbrauchs, der gerade aus Sonne + Akku kommt.
    var selfSufficiency: Double {
        guard home > 0 else { return 1 }
        return max(0, min(1, (home - max(0, grid)) / home))
    }

    /// Wann der Akku beim aktuellen Verbrauch leer wäre.
    var batteryEmptyAt: Date? {
        let draw = max(home - pv, 0)
        guard draw > 100, batteryKWh > 0.1 else { return nil }
        return Date().addingTimeInterval(batteryKWh * 1000 / draw * 3600)
    }

    var producedTodayKWh: Double {
        guard produced.count > 1 else { return 0 }
        var wh = 0.0
        for (a, b) in zip(produced, produced.dropFirst()) {
            let h = b.time.timeIntervalSince(a.time) / 3600
            if h < 0.25 { wh += (a.watts + b.watts) / 2 * h }
        }
        return wh / 1000
    }

    // MARK: - evcc-JSON lesen

    private func apply(_ s: [String: Any]) {
        pv = num(s["pvPower"]) ?? 0
        pvParts = (s["pv"] as? [[String: Any]])?.compactMap { num($0["power"]) } ?? []
        pvTitles = (s["pv"] as? [[String: Any]])?.map { ($0["title"] as? String) ?? "" } ?? []
        home = num(s["homePower"]) ?? 0
        grid = num((s["grid"] as? [String: Any])?["power"]) ?? num(s["gridPower"]) ?? 0
        // evcc ≥ 0.300: "battery": {power, soc, capacity}; ältere: batteryPower/batterySoc
        let bat = s["battery"] as? [String: Any]
        battery = num(bat?["power"]) ?? num(s["batteryPower"]) ?? 0
        batterySoc = num(bat?["soc"]) ?? num(s["batterySoc"]) ?? batterySoc
        if let cap = num(bat?["capacity"]) ?? num(s["batteryCapacity"]), cap > 0 { batteryCapacityKWh = cap }

        if let lps = s["loadpoints"] as? [[String: Any]] {
            loadpoints = lps.enumerated().map { i, lp in
                Loadpoint(id: i,
                          title: lp["title"] as? String ?? "Wallbox",
                          vehicle: lp["vehicleTitle"] as? String ?? lp["vehicleName"] as? String ?? "Auto",
                          mode: lp["mode"] as? String ?? "off",
                          chargePower: num(lp["chargePower"]) ?? 0,
                          soc: num(lp["vehicleSoc"]),
                          targetSoc: num(lp["effectiveLimitSoc"]) ?? num(lp["limitSoc"]),
                          connected: lp["connected"] as? Bool ?? false,
                          charging: lp["charging"] as? Bool ?? false)
            }
        }

        if let fc = (s["forecast"] as? [String: Any])?["solar"] as? [String: Any] {
            forecastTodayKWh = num((fc["today"] as? [String: Any])?["energy"]).map { $0 / 1000 }
            // Zeitreihe: [[unixzeit, watt], …] (aktuell) oder [{ts, val}, …] (älter)
            if let ts = fc["timeseries"] as? [Any] {
                let iso = ISO8601DateFormatter()
                let cal = Calendar.current
                forecast = ts.compactMap { p -> Sample? in
                    var t: Date?, v: Double?
                    if let pair = p as? [Any], pair.count >= 2 {
                        t = num(pair[0]).map { Date(timeIntervalSince1970: $0) }
                        v = num(pair[1])
                    } else if let obj = p as? [String: Any] {
                        t = (obj["ts"] as? String).flatMap(iso.date(from:))
                        v = num(obj["val"])
                    }
                    guard let t, let v, cal.isDateInToday(t) else { return nil }
                    return Sample(time: t, watts: v)
                }
            }
        }

        let now = Date()
        updated = now
        if let last = produced.last, !Calendar.current.isDate(last.time, inSameDayAs: now) { produced = [] }
        if produced.last.map({ now.timeIntervalSince($0.time) >= 60 }) ?? true {
            produced.append(Sample(time: now, watts: pv))
            Self.saveToday(produced)
        }
    }

    private func num(_ v: Any?) -> Double? {
        if let n = v as? NSNumber { return n.doubleValue }
        if let s = v as? String { return Double(s) }
        return nil
    }

    // MARK: - Tagesverlauf lokal merken

    private static var todayURL: URL {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("pv-today.json")
    }
    private static func saveToday(_ s: [Sample]) { try? JSONEncoder().encode(s).write(to: todayURL) }
    private static func loadToday() -> [Sample] {
        guard let d = try? Data(contentsOf: todayURL),
              let s = try? JSONDecoder().decode([Sample].self, from: d),
              let first = s.first, Calendar.current.isDateInToday(first.time) else { return [] }
        return s
    }

    // MARK: Gebäude mit PV (für das Bild)
    /// Erste PV-Quelle in evcc = Hauptdach, alle weiteren = zweites Gebäude. Namen: „title" in evcc.
    var hasSecondBuilding: Bool { pvParts.count > 1 }
    var pvHouse: Double { pvParts.first ?? pv }
    var pvShed: Double { pvParts.dropFirst().reduce(0, +) }
    var pvHouseTitle: String { short(pvTitles.first, fallback: "Dach") }
    var pvShedTitle: String { short(pvTitles.count > 1 ? pvTitles[1] : nil, fallback: "Nebengebäude") }

    private func short(_ t: String?, fallback: String) -> String {
        guard var t, !t.isEmpty else { return fallback }
        t = t.replacingOccurrences(of: "PV ", with: "")
        return t.count > 18 ? String(t.prefix(17)) + "…" : t
    }
}
