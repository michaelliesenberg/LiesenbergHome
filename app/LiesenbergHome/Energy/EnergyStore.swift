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
    struct PVSource: Identifiable {
        let id: Int
        let title: String
        let power: Double
        /// vom Hub (Einrichtung → Energie): Nebengebäude statt Haus; ältere Hubs liefern nichts → Haus
        let otherBuilding: Bool
    }
    /// Wechselrichter (Reihenfolge wie in evcc)
    private(set) var pvSources: [PVSource] = []
    private(set) var home: Double = 0
    private(set) var grid: Double = 0
    private(set) var battery: Double = 0
    private(set) var batterySoc: Double = 0
    private(set) var batteryCapacityKWh: Double = 16.56
    private(set) var loadpoints: [Loadpoint] = []
    private(set) var forecast: [Sample] = []
    private(set) var forecastTodayKWh: Double?
    /// Verlauf vom Hub (jede Minute aufgezeichnet) für den gewählten Tag
    private(set) var produced: [Sample] = []
    private(set) var consumed: [Sample] = []
    /// Netz im Tagesverlauf: Bezug (positiv) und Einspeisung (positiv gezählt)
    private(set) var gridImport: [Sample] = []
    private(set) var gridExport: [Sample] = []
    private(set) var dayTotals = DayTotals()
    private(set) var days: [DayTotals] = []
    /// gezeigter Tag (Mitternacht); heute = Live-Ansicht mit Prognose
    var historyDay: Date = Calendar.current.startOfDay(for: Date())
    var showingToday: Bool { Calendar.current.isDateInToday(historyDay) }
    /// true, solange der Nutzer „heute" anschaut (nicht in alten Tagen blättert)
    private var followToday = true

    struct DayTotals: Codable, Identifiable {
        var date: String = ""
        var pv: Double = 0
        var home: Double = 0
        var `import`: Double = 0
        var export: Double = 0
        var car: Double? = 0
        var id: String { date }
        var day: Date? { EnergyStore.dayFmt.date(from: date) }
    }
    private struct HistoryRow: Codable { let t: Double; let pv: Double?; let home: Double?; let grid: Double? }
    private struct HistoryResponse: Codable { let date: String; let samples: [HistoryRow]; let totals: DayTotals }
    private struct DaysResponse: Codable { let days: [DayTotals] }

    static let dayFmt: DateFormatter = {
        let f = DateFormatter(); f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd"; return f
    }()
    private(set) var updated: Date?
    private(set) var online = false

    let client: HouseClient
    private var pollTask: Task<Void, Never>?

    init(client: HouseClient) {
        self.client = client
    }

    func start() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            var tick = 0
            while !Task.isCancelled {
                await self?.refresh()
                // Verlauf: beim Start und danach jede Minute (der Hub zeichnet minütlich auf)
                if tick % 20 == 0 { await self?.loadHistory(); await self?.loadSummary() }
                tick += 1
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

    /// Erzeugt am gezeigten Tag (vom Hub gerechnet)
    var producedTodayKWh: Double { dayTotals.pv }

    // MARK: - Verlauf vom Hub

    func loadHistory() async {
        let ds = Self.dayFmt.string(from: historyDay)
        guard let data = try? await client.get("api/energy/history?date=\(ds)"),
              let h = try? JSONDecoder().decode(HistoryResponse.self, from: data), h.date == ds else { return }
        produced = h.samples.map { Sample(time: Date(timeIntervalSince1970: $0.t), watts: $0.pv ?? 0) }
        consumed = h.samples.map { Sample(time: Date(timeIntervalSince1970: $0.t), watts: $0.home ?? 0) }
        gridImport = h.samples.map { Sample(time: Date(timeIntervalSince1970: $0.t), watts: max($0.grid ?? 0, 0)) }
        gridExport = h.samples.map { Sample(time: Date(timeIntervalSince1970: $0.t), watts: max(-($0.grid ?? 0), 0)) }
        dayTotals = h.totals
        if let d = try? await client.get("api/energy/days?limit=31"),
           let r = try? JSONDecoder().decode(DaysResponse.self, from: d) { days = r.days }
    }

    func showDay(_ d: Date) {
        let day = Calendar.current.startOfDay(for: min(d, Date()))
        guard day != historyDay else { return }
        historyDay = day
        followToday = Calendar.current.isDateInToday(day)
        produced = []; consumed = []; gridImport = []; gridExport = []; dayTotals = DayTotals()
        Task { await loadHistory() }
    }

    func shiftDay(_ by: Int) {
        if let d = Calendar.current.date(byAdding: .day, value: by, to: historyDay) { showDay(d) }
    }

    // MARK: - Kostenübersicht (rechnet der Hub nach den Strompreisen des Hauses)

    enum Period: String, CaseIterable, Identifiable {
        case day, week, month
        var id: String { rawValue }
        var title: String { ["day": "Tag", "week": "Woche", "month": "Monat"][rawValue]! }
        var component: Calendar.Component { ["day": .day, "week": .weekOfYear, "month": .month][rawValue]! }
    }

    struct Prices: Codable, Equatable {
        var `import`: Double?
        var export: Double?
        var base_month: Double?
        var isSet: Bool { `import` != nil }
    }

    struct Summary: Decodable {
        struct Totals: Decodable { let pv: Double; let home: Double; let `import`: Double; let export: Double; let selfUse: Double }
        struct Costs: Decodable { let `import`: Double; let export: Double; let saved: Double; let base: Double; let balance: Double }
        struct Bucket: Decodable, Identifiable {
            let hour: Int?
            let date: String?
            let pv: Double; let home: Double; let `import`: Double; let export: Double
            var id: String { date ?? "h\(hour ?? 0)" }
        }
        let period: String
        let start: String
        let end: String
        let totals: Totals
        let buckets: [Bucket]
        let prices: Prices
        let costs: Costs?
    }

    var summaryPeriod: Period = .day
    var summaryDay: Date = Calendar.current.startOfDay(for: Date())
    private(set) var summary: Summary?
    private(set) var prices = Prices()
    private(set) var summaryError: String?

    var summaryIsCurrent: Bool {
        Calendar.current.isDate(summaryDay, equalTo: Date(), toGranularity: summaryPeriod.component)
    }

    func loadSummary() async {
        let ds = Self.dayFmt.string(from: summaryDay)
        do {
            let data = try await client.get("api/energy/summary?period=\(summaryPeriod.rawValue)&date=\(ds)")
            let s = try JSONDecoder().decode(Summary.self, from: data)
            summary = s; prices = s.prices; summaryError = nil
        } catch {
            summaryError = (error as? ServerError)?.message ?? "Kostenübersicht braucht Hub 2.4"
        }
    }

    func setPeriod(_ p: Period) {
        summaryPeriod = p
        summaryDay = Calendar.current.startOfDay(for: Date())
        summary = nil
        Task { await loadSummary() }
    }

    func shiftSummary(_ by: Int) {
        guard let d = Calendar.current.date(byAdding: summaryPeriod.component, value: by, to: summaryDay), d <= Date() else { return }
        summaryDay = d
        summary = nil
        Task { await loadSummary() }
    }

    func savePrices(_ p: Prices) async throws {
        let body = try JSONEncoder().encode(p)
        let data = try await client.send("api/prices", method: "PUT", body: body)
        prices = (try? JSONDecoder().decode(Prices.self, from: data)) ?? p
        await loadSummary()
    }

    // MARK: - evcc-JSON lesen

    private func apply(_ s: [String: Any]) {
        pv = num(s["pvPower"]) ?? 0
        pvSources = ((s["pv"] as? [[String: Any]]) ?? []).enumerated().map { i, src in
            PVSource(id: i, title: src["title"] as? String ?? "", power: num(src["power"]) ?? 0,
                     otherBuilding: src["site"] as? String == "other")
        }
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

        updated = Date()
        // Wer „heute" anschaut, springt um Mitternacht automatisch auf den neuen Tag
        if followToday && !showingToday {
            historyDay = Calendar.current.startOfDay(for: Date())
            produced = []; consumed = []; gridImport = []; gridExport = []; dayTotals = DayTotals()
            Task { await loadHistory() }
        }
    }

    private func num(_ v: Any?) -> Double? {
        if let n = v as? NSNumber { return n.doubleValue }
        if let s = v as? String { return Double(s) }
        return nil
    }

    // MARK: Gebäude mit PV (für das Bild)
    /// Zuordnung je Quelle kommt vom Hub („site"): alle Haus-Quellen zusammen aufs Hausdach,
    /// das zweite Gebäude nur, wenn mindestens eine Quelle dort eingetragen ist. Namen: „title" in evcc.
    var housePV: [PVSource] { pvSources.filter { !$0.otherBuilding } }
    var shedPV: [PVSource] { pvSources.filter(\.otherBuilding) }
    var hasSecondBuilding: Bool { !shedPV.isEmpty }
    var pvHouse: Double { pvSources.isEmpty ? pv : housePV.reduce(0) { $0 + $1.power } }
    var pvShed: Double { shedPV.reduce(0) { $0 + $1.power } }
    var pvHouseTitle: String { housePV.count == 1 ? short(housePV[0].title, fallback: "Dach") : "Dach" }
    var pvShedTitle: String { short(shedPV.count == 1 ? shedPV[0].title : nil, fallback: "Nebengebäude") }
    /// Mehrere Wechselrichter auf dem Haus → einzeln unter dem Dach-Label zeigen
    var housePVBreakdown: [PVSource] { housePV.count > 1 ? housePV : [] }

    func short(_ t: String?, fallback: String) -> String {
        guard var t, !t.isEmpty else { return fallback }
        t = t.replacingOccurrences(of: "PV ", with: "")
        return t.count > 18 ? String(t.prefix(17)) + "…" : t
    }
}
