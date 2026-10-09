import SwiftUI
import Charts

struct EnergyView: View {
    @Environment(EnergyStore.self) private var e

    var body: some View {
        Screen(title: "Energie") {
            PowerFlowView()
                .frame(height: 330)

            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Selbstversorgt jetzt").font(.caption.weight(.semibold)).foregroundStyle(Theme.muted)
                    Text(e.online ? "\(Int(e.selfSufficiency * 100)) %" : "–").font(.system(size: 28, weight: .semibold, design: .rounded))
                    ProgressView(value: e.online ? e.selfSufficiency : 0).tint(Theme.solar)
                }
                .card()
                VStack(alignment: .leading, spacing: 8) {
                    Text("Akku reicht bis").font(.caption.weight(.semibold)).foregroundStyle(Theme.muted)
                    Group {
                        if let t = e.batteryEmptyAt { Text(t, format: .dateTime.hour().minute()) }
                        else { Text(e.batteryCharging ? "lädt" : (e.batteryKWh < 0.1 ? "leer" : "–")) }
                    }
                    .font(.system(size: 28, weight: .semibold, design: .rounded))
                    ProgressView(value: e.batterySoc / 100).tint(Theme.battery)
                    Text(String(format: "%.1f von %.1f kWh", e.batteryKWh, e.batteryCapacityKWh))
                        .font(.caption2).foregroundStyle(Theme.muted)
                }
                .card()
            }

            ProductionChart()
            GridChart()
            CostSummaryCard()

            if !e.online {
                Label("Keine Verbindung zum Hub", systemImage: "wifi.slash")
                    .font(.footnote).foregroundStyle(Theme.heat)
            }
        }
    }
}

// MARK: - Haus & Schuppen mit Energiefluss (wie in der Tesla-App)
// Seitenansicht: Pultdach mit Modulen (alle PV-Quellen „Haus"). Ist im Hub eine Quelle als Nebengebäude
// eingetragen, erscheint daneben ein zweites Gebäude (Satteldach, Module beidseitig). Namen = Titel in evcc.

struct PowerFlowView: View {
    @Environment(EnergyStore.self) private var e

    private let panel = Color(hex: 0x1F4E6B)      // Modulblau wie in deiner Skizze
    private let panelLine = Color(hex: 0x0F2D40)

    // Bewusst ohne Dauer-Animation: die hat auf dem Mac alles lahmgelegt.
    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width, h = geo.size.height
            let g: CGFloat = 236                                   // Boden
            // Haus
            let hw = min(170, w * 0.44), hx = w * 0.38
            let x0 = hx - hw / 2, x1 = hx + hw / 2
            let yl = g - 60, yr = g - 92                            // Dach: West niedrig, Ost hoch
            // Schuppen
            let sw = min(104, w * 0.27), sx = w * 0.80
            let s0 = sx - sw / 2, s1 = sx + sw / 2
            let sEave = g - 42, sPeak = g - 70
            // Akku im Haus
            let bat = CGPoint(x: x1 - 20, y: g - 22)

            ZStack {
                RadialGradient(colors: [Theme.card2, Theme.bg], center: .center, startRadius: 10, endRadius: w * 0.6)
                    .clipShape(RoundedRectangle(cornerRadius: 26, style: .continuous))

                // Solar → Dächer
                flow(from: CGPoint(x: hx, y: 104), to: CGPoint(x: hx, y: (yl + yr) / 2 - 12), color: Theme.solar,
                     active: e.pvHouse > 30, reverse: false)
                if e.hasSecondBuilding {
                    flow(from: CGPoint(x: sx, y: 104), to: CGPoint(x: sx, y: sPeak - 10), color: Theme.solar,
                         active: e.pvShed > 30, reverse: false)
                }
                // Netz, Akku, Verbrauch (unten)
                flow(from: CGPoint(x: x0, y: g - 14), to: CGPoint(x: 52, y: h - 58), color: Theme.grid,
                     active: abs(e.grid) > 50, reverse: e.grid > 0)   // Bezug: Pfeil zum Haus
                flow(from: CGPoint(x: bat.x, y: g), to: CGPoint(x: w - 52, y: h - 58), color: Theme.battery,
                     active: abs(e.battery) > 50, reverse: e.battery > 0)
                flow(from: CGPoint(x: hx - 20, y: g), to: CGPoint(x: hx - 20, y: h - 58), color: Theme.text,
                     active: e.home > 50, reverse: false)

                // Boden
                Rectangle().fill(Theme.line).frame(width: w - 48, height: 1.5).position(x: w / 2, y: g)

                // Haus: Wände + Pultdach
                Path { p in
                    p.move(to: CGPoint(x: x0, y: g)); p.addLine(to: CGPoint(x: x0, y: yl))
                    p.addLine(to: CGPoint(x: x1, y: yr)); p.addLine(to: CGPoint(x: x1, y: g)); p.closeSubpath()
                }
                .fill(Theme.card).overlay(Path { p in
                    p.move(to: CGPoint(x: x0, y: g)); p.addLine(to: CGPoint(x: x0, y: yl))
                    p.addLine(to: CGPoint(x: x1, y: yr)); p.addLine(to: CGPoint(x: x1, y: g))
                }.stroke(Color(hex: 0x3A3E46), lineWidth: 1.5))
                panels(from: CGPoint(x: x0 - 6, y: yl + 1), to: CGPoint(x: x1 + 6, y: yr - 1), count: 8, active: e.pvHouse > 30)
                // Fenster & Tür
                RoundedRectangle(cornerRadius: 2).fill(Color(hex: 0x2A2F38)).frame(width: 22, height: 38)
                    .position(x: x0 + 34, y: g - 19)
                ForEach(0..<2) { i in
                    RoundedRectangle(cornerRadius: 2).fill(Color(hex: 0x2A2F38)).frame(width: 26, height: 20)
                        .position(x: x0 + 72 + CGFloat(i) * 36, y: g - 46)
                }

                if e.hasSecondBuilding {
                    // Schuppen: Wände + Satteldach, Module beidseitig (Nord/Süd)
                    Path { p in
                        p.move(to: CGPoint(x: s0, y: g)); p.addLine(to: CGPoint(x: s0, y: sEave))
                        p.addLine(to: CGPoint(x: sx, y: sPeak)); p.addLine(to: CGPoint(x: s1, y: sEave))
                        p.addLine(to: CGPoint(x: s1, y: g)); p.closeSubpath()
                    }
                    .fill(Theme.card).overlay(Path { p in
                        p.move(to: CGPoint(x: s0, y: g)); p.addLine(to: CGPoint(x: s0, y: sEave))
                        p.addLine(to: CGPoint(x: sx, y: sPeak)); p.addLine(to: CGPoint(x: s1, y: sEave))
                        p.addLine(to: CGPoint(x: s1, y: g))
                    }.stroke(Color(hex: 0x3A3E46), lineWidth: 1.5))
                    panels(from: CGPoint(x: s0 - 5, y: sEave + 1), to: CGPoint(x: sx - 2, y: sPeak + 1), count: 3, active: e.pvShed > 30)
                    panels(from: CGPoint(x: sx + 2, y: sPeak + 1), to: CGPoint(x: s1 + 5, y: sEave + 1), count: 3, active: e.pvShed > 30)
                    RoundedRectangle(cornerRadius: 2).fill(Color(hex: 0x2A2F38)).frame(width: 26, height: 30)
                        .position(x: sx, y: g - 15)
                }

                // Akku an der Wand
                RoundedRectangle(cornerRadius: 3).fill(Color(hex: 0x123326))
                    .overlay(RoundedRectangle(cornerRadius: 3).stroke(Theme.battery, lineWidth: 1.2))
                    .overlay(alignment: .bottom) {
                        RoundedRectangle(cornerRadius: 1).fill(Theme.battery)
                            .frame(width: 8, height: max(2, 32 * e.batterySoc / 100)).padding(.bottom, 3)
                    }
                    .frame(width: 14, height: 38).position(bat)

                // Beschriftung
                label("SOLAR", kw(e.pv), Theme.solar, big: true).position(x: w / 2, y: 30)
                label(e.pvHouseTitle.uppercased(), kw(e.pvHouse), Theme.solar, small: true,
                      sub: e.housePVBreakdown.isEmpty ? nil : e.housePVBreakdown.map { kw($0.power) }.joined(separator: " + "))
                    .position(x: hx, y: e.housePVBreakdown.isEmpty ? 84 : 80)
                if e.hasSecondBuilding {
                    label(e.pvShedTitle.uppercased(), kw(e.pvShed), Theme.solar, small: true).position(x: sx, y: 84)
                }
                label("NETZ", kw(e.grid), Theme.grid, sub: e.feedingIn ? "Einspeisung" : (e.grid > 50 ? "Bezug" : nil))
                    .position(x: 52, y: h - 30)
                label("VERBRAUCH", kw(e.home), Theme.text).position(x: hx - 20, y: h - 30)
                label("AKKU", kw(e.battery), Theme.battery,
                      sub: "\(e.batteryCharging ? "lädt · " : (e.battery > 50 ? "entlädt · " : ""))\(Int(e.batterySoc)) %")
                    .position(x: w - 52, y: h - 30)
            }
        }
    }

    /// Modulreihe als schräges Band entlang der Dachlinie a→b, mit Trennlinien.
    private func panels(from a: CGPoint, to b: CGPoint, count: Int, active: Bool) -> some View {
        let t: CGFloat = 7   // Dicke
        let band = Path { p in
            p.move(to: a); p.addLine(to: b)
            p.addLine(to: CGPoint(x: b.x, y: b.y - t)); p.addLine(to: CGPoint(x: a.x, y: a.y - t)); p.closeSubpath()
        }
        let dividers = Path { p in
            for i in 1..<count {
                let f = CGFloat(i) / CGFloat(count)
                let x = a.x + (b.x - a.x) * f, y = a.y + (b.y - a.y) * f
                p.move(to: CGPoint(x: x, y: y)); p.addLine(to: CGPoint(x: x, y: y - t))
            }
        }
        return ZStack {
            band.fill(panel)
            dividers.stroke(panelLine, lineWidth: 1)
            band.stroke(active ? Theme.solar : panelLine, lineWidth: active ? 1.2 : 1)
        }
    }

    /// Linie a→b (Kurve). `reverse` = Strom fließt von b nach a. Pfeil zeigt zum Ziel.
    private func flow(from a: CGPoint, to b: CGPoint, color: Color, active: Bool, reverse: Bool) -> some View {
        let control = CGPoint(x: b.x, y: a.y)
        let tip = reverse ? a : b
        let back = control
        let angle = atan2(tip.y - back.y, tip.x - back.x)
        return ZStack {
            Path { p in
                p.move(to: a)
                p.addQuadCurve(to: b, control: control)
            }
            .stroke(active ? color : Theme.line, style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
            if active {
                Path { p in
                    let len: CGFloat = 9, spread: CGFloat = .pi / 7
                    p.move(to: tip)
                    p.addLine(to: CGPoint(x: tip.x - len * cos(angle - spread), y: tip.y - len * sin(angle - spread)))
                    p.move(to: tip)
                    p.addLine(to: CGPoint(x: tip.x - len * cos(angle + spread), y: tip.y - len * sin(angle + spread)))
                }
                .stroke(color, style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
            }
        }
    }

    private func label(_ title: String, _ value: String, _ color: Color, big: Bool = false, small: Bool = false,
                       sub: String? = nil) -> some View {
        VStack(spacing: 1) {
            Text(title).font(.system(size: small ? 10 : 11, weight: .semibold)).kerning(0.6).foregroundStyle(Theme.muted)
            (Text(value).font(.system(size: big ? 24 : (small ? 16 : 20), weight: .semibold, design: .rounded))
             + Text(" kW").font(.system(size: small ? 10 : 12, weight: .semibold)))
                .foregroundStyle(color).monospacedDigit()
            if let sub, !sub.isEmpty { Text(sub).font(.system(size: 11)).foregroundStyle(Theme.muted) }
        }
        .fixedSize()
    }
}

// MARK: - Tagesverlauf als Linie

struct ProductionChart: View {
    @Environment(EnergyStore.self) private var e

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Tag wählen: ‹ Heute ›
            HStack(spacing: 6) {
                Button { e.shiftDay(-1) } label: { Image(systemName: "chevron.left").frame(width: 30, height: 30) }
                    .buttonStyle(.plain).foregroundStyle(Theme.muted)
                Text(dayTitle).font(.subheadline.weight(.semibold))
                Button { e.shiftDay(1) } label: { Image(systemName: "chevron.right").frame(width: 30, height: 30) }
                    .buttonStyle(.plain).foregroundStyle(e.showingToday ? Theme.faint : Theme.muted)
                    .disabled(e.showingToday)
                Spacer()
                Text(String(format: "%.1f kWh", e.producedTodayKWh))
                    .font(.system(size: 16, weight: .semibold, design: .rounded)).foregroundStyle(Theme.solar)
            }
            Chart {
                if e.showingToday {
                    ForEach(e.forecast.filter { dayRange.contains($0.time) }) { s in
                        LineMark(x: .value("Zeit", s.time), y: .value("kW", s.watts / 1000), series: .value("Art", "Prognose"))
                            .foregroundStyle(Theme.faint)
                            .lineStyle(StrokeStyle(lineWidth: 2, dash: [4, 4]))
                            .interpolationMethod(.catmullRom)
                    }
                }
                ForEach(e.consumed.filter { dayRange.contains($0.time) }) { s in
                    LineMark(x: .value("Zeit", s.time), y: .value("kW", s.watts / 1000), series: .value("Art", "Verbrauch"))
                        .foregroundStyle(Theme.text.opacity(0.55))
                        .lineStyle(StrokeStyle(lineWidth: 1.2))
                        .interpolationMethod(.monotone)
                }
                ForEach(e.produced.filter { dayRange.contains($0.time) }) { s in
                    AreaMark(x: .value("Zeit", s.time), y: .value("kW", s.watts / 1000))
                        .foregroundStyle(LinearGradient(colors: [Theme.solar.opacity(0.35), Theme.solar.opacity(0)],
                                                        startPoint: .top, endPoint: .bottom))
                        .interpolationMethod(.monotone)
                    LineMark(x: .value("Zeit", s.time), y: .value("kW", s.watts / 1000), series: .value("Art", "Erzeugt"))
                        .foregroundStyle(Theme.solar)
                        .lineStyle(StrokeStyle(lineWidth: 2.5, lineCap: .round))
                        .interpolationMethod(.monotone)
                }
                if e.showingToday, let last = e.produced.last {
                    PointMark(x: .value("Zeit", last.time), y: .value("kW", last.watts / 1000))
                        .foregroundStyle(Theme.solar).symbolSize(50)
                }
            }
            // Werte außerhalb 5–22 Uhr wegfiltern und den Plot beschneiden – sonst zeichnet Swift Charts sie
            // über den Kartenrand hinaus (Nacht-Nullwerte links, Prognose rechts) und der Graph wirkt verschoben
            .chartXScale(domain: dayRange)
            .chartPlotStyle { $0.clipped() }
            .chartXAxis {
                AxisMarks(values: .stride(by: .hour, count: 6)) { _ in
                    AxisGridLine().foregroundStyle(Theme.line)
                    AxisValueLabel(format: .dateTime.hour(), anchor: .top).foregroundStyle(Theme.muted)
                }
            }
            .chartYAxis(.hidden)
            .frame(height: 110)
            .overlay {
                if e.produced.isEmpty {
                    Text(e.showingToday ? "Der Hub zeichnet ab jetzt jede Minute auf." : "Für diesen Tag gibt es keine Aufzeichnung.")
                        .font(.caption).foregroundStyle(Theme.muted)
                }
            }

            HStack(spacing: 14) {
                legend(Theme.solar, "Erzeugt", dashed: false)
                legend(Theme.text.opacity(0.55), "Verbrauch", dashed: false)
                if e.showingToday { legend(Theme.faint, "Prognose", dashed: true) }
                Spacer()
                if e.showingToday, let fc = e.forecastTodayKWh {
                    Text(String(format: "Prognose %.0f kWh", fc)).font(.caption2).foregroundStyle(Theme.muted)
                } else if e.dayTotals.home > 0 {
                    Text(String(format: "Verbrauch %.1f kWh", e.dayTotals.home)).font(.caption2).foregroundStyle(Theme.muted)
                }
            }
        }
        .card()

        if e.days.count > 1 { DaysChart() }
    }

    private var dayTitle: String {
        let cal = Calendar.current
        if cal.isDateInToday(e.historyDay) { return "Heute" }
        if cal.isDateInYesterday(e.historyDay) { return "Gestern" }
        return e.historyDay.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated))
    }

    var dayRange: ClosedRange<Date> { Self.range(for: e.historyDay) }

    static func range(for d: Date) -> ClosedRange<Date> {
        let cal = Calendar.current
        let start = cal.date(bySettingHour: 5, minute: 0, second: 0, of: d) ?? d
        let end = cal.date(bySettingHour: 22, minute: 0, second: 0, of: d) ?? d
        return start...end
    }

    private func legend(_ c: Color, _ t: String, dashed: Bool) -> some View {
        HStack(spacing: 6) {
            Capsule().fill(c).frame(width: 14, height: 2.5).opacity(dashed ? 0.7 : 1)
            Text(t).font(.caption2).foregroundStyle(Theme.muted)
        }
    }
}

/// Tageswerte der letzten zwei Wochen – antippen zeigt den Tag oben im Graphen
struct DaysChart: View {
    @Environment(EnergyStore.self) private var e

    private var recent: [EnergyStore.DayTotals] {
        Array(e.days.prefix(14)).filter { $0.day != nil }.reversed()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Letzte Tage").font(.subheadline.weight(.semibold))
                Spacer()
                let sum = recent.reduce(0) { $0 + $1.pv }
                Text(String(format: "%.0f kWh", sum)).font(.caption.weight(.semibold)).foregroundStyle(Theme.muted)
            }
            HStack(alignment: .bottom, spacing: 4) {
                let maxPV = max(recent.map(\.pv).max() ?? 1, 1)
                ForEach(recent) { d in
                    let selected = d.day.map { Calendar.current.isDate($0, inSameDayAs: e.historyDay) } ?? false
                    Button { if let day = d.day { e.showDay(day) } } label: {
                        VStack(spacing: 4) {
                            Text(String(format: "%.0f", d.pv)).font(.system(size: 9, weight: .semibold)).monospacedDigit()
                                .foregroundStyle(selected ? Theme.solar : Theme.muted)
                            RoundedRectangle(cornerRadius: 3)
                                .fill(selected ? Theme.solar : Theme.solar.opacity(0.45))
                                .frame(height: max(3, 70 * d.pv / maxPV))
                            Text(d.day.map { $0.formatted(.dateTime.day()) } ?? "")
                                .font(.system(size: 9)).foregroundStyle(Theme.faint)
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.plain)
                }
            }
            .frame(height: 100, alignment: .bottom)
        }
        .card()
    }
}
