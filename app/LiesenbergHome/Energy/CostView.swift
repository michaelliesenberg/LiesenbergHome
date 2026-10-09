import SwiftUI
import Charts

// MARK: - Netz im Tagesverlauf: Bezug nach oben, Einspeisung nach unten

struct GridChart: View {
    @Environment(EnergyStore.self) private var e

    private var range: ClosedRange<Date> { ProductionChart.range(for: e.historyDay) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Netz").font(.subheadline.weight(.semibold))
                Spacer()
                Text(String(format: "↓ %.1f  ↑ %.1f kWh", e.dayTotals.import, e.dayTotals.export))
                    .font(.caption.weight(.semibold)).monospacedDigit().foregroundStyle(Theme.muted)
            }
            Chart {
                ForEach(e.gridImport.filter { range.contains($0.time) }) { s in
                    AreaMark(x: .value("Zeit", s.time), y: .value("kW", s.watts / 1000), series: .value("Art", "Bezug"))
                        .foregroundStyle(Theme.heat.opacity(0.55))
                        .interpolationMethod(.monotone)
                }
                ForEach(e.gridExport.filter { range.contains($0.time) }) { s in
                    AreaMark(x: .value("Zeit", s.time), y: .value("kW", -s.watts / 1000), series: .value("Art", "Einspeisung"))
                        .foregroundStyle(Theme.grid.opacity(0.55))
                        .interpolationMethod(.monotone)
                }
                RuleMark(y: .value("0", 0)).foregroundStyle(Theme.line)
            }
            .chartXScale(domain: range)
            .chartPlotStyle { $0.clipped() }
            .chartXAxis {
                AxisMarks(values: .stride(by: .hour, count: 6)) { _ in
                    AxisGridLine().foregroundStyle(Theme.line)
                    AxisValueLabel(format: .dateTime.hour(), anchor: .top).foregroundStyle(Theme.muted)
                }
            }
            .chartYAxis(.hidden)
            .frame(height: 90)
            .overlay {
                if e.gridImport.isEmpty { Text("Noch keine Aufzeichnung").font(.caption).foregroundStyle(Theme.muted) }
            }
            HStack(spacing: 14) {
                legend(Theme.heat, "Bezug")
                legend(Theme.grid, "Einspeisung")
            }
        }
        .card()
    }

    private func legend(_ c: Color, _ t: String) -> some View {
        HStack(spacing: 6) {
            RoundedRectangle(cornerRadius: 2).fill(c.opacity(0.7)).frame(width: 12, height: 8)
            Text(t).font(.caption2).foregroundStyle(Theme.muted)
        }
    }
}

// MARK: - Kostenübersicht Tag / Woche / Monat

struct CostSummaryCard: View {
    @Environment(EnergyStore.self) private var e
    @Environment(AppSettings.self) private var settings
    @State private var editing = false

    var body: some View {
        @Bindable var store = e
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Kosten").font(.subheadline.weight(.semibold))
                Spacer()
                if settings.permissions.edit {
                    Button { editing = true } label: { Label("Preise", systemImage: "eurosign.circle") }
                        .font(.caption.weight(.semibold)).foregroundStyle(Theme.solar)
                }
            }
            Picker("Zeitraum", selection: Binding(get: { e.summaryPeriod }, set: { e.setPeriod($0) })) {
                ForEach(EnergyStore.Period.allCases) { Text($0.title).tag($0) }
            }
            .pickerStyle(.segmented)

            HStack(spacing: 6) {
                Button { e.shiftSummary(-1) } label: { Image(systemName: "chevron.left").frame(width: 30, height: 30) }
                    .buttonStyle(.plain).foregroundStyle(Theme.muted)
                Text(periodTitle).font(.subheadline.weight(.semibold))
                Button { e.shiftSummary(1) } label: { Image(systemName: "chevron.right").frame(width: 30, height: 30) }
                    .buttonStyle(.plain).foregroundStyle(e.summaryIsCurrent ? Theme.faint : Theme.muted)
                    .disabled(e.summaryIsCurrent)
                Spacer()
            }

            if let s = e.summary {
                bars(s)
                rows(s)
            } else if let err = e.summaryError {
                Text(err).font(.caption).foregroundStyle(Theme.muted)
            } else {
                ProgressView().frame(maxWidth: .infinity)
            }
        }
        .card()
        .task { if e.summary == nil { await e.loadSummary() } }
        .sheet(isPresented: $editing) { PriceEditor(initial: e.prices).environment(e) }
    }

    private var periodTitle: String {
        let d = e.summaryDay, cal = Calendar.current
        switch e.summaryPeriod {
        case .day:
            if cal.isDateInToday(d) { return "Heute" }
            if cal.isDateInYesterday(d) { return "Gestern" }
            return d.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated))
        case .week:
            if e.summaryIsCurrent { return "Diese Woche" }
            return "KW \(cal.component(.weekOfYear, from: d))"
        case .month:
            return d.formatted(.dateTime.month(.wide).year())
        }
    }

    // Balken: Bezug nach oben, Einspeisung nach unten (Tag: je Stunde, sonst je Tag)
    private func bars(_ s: EnergyStore.Summary) -> some View {
        Chart {
            ForEach(s.buckets) { b in
                BarMark(x: .value("Zeit", label(b)), y: .value("kWh", b.import))
                    .foregroundStyle(Theme.heat.opacity(0.75))
                BarMark(x: .value("Zeit", label(b)), y: .value("kWh", -b.export))
                    .foregroundStyle(Theme.grid.opacity(0.75))
            }
            RuleMark(y: .value("0", 0)).foregroundStyle(Theme.line)
        }
        .chartXAxis {
            AxisMarks(values: axisValues(s)) { _ in
                AxisValueLabel().foregroundStyle(Theme.muted)
            }
        }
        .chartYAxis(.hidden)
        .frame(height: 110)
    }

    private func label(_ b: EnergyStore.Summary.Bucket) -> String {
        if let h = b.hour { return String(format: "%02d", h) }
        guard let ds = b.date, let d = EnergyStore.dayFmt.date(from: ds) else { return "" }
        return e.summaryPeriod == .week ? d.formatted(.dateTime.weekday(.abbreviated)) : d.formatted(.dateTime.day())
    }

    private func axisValues(_ s: EnergyStore.Summary) -> [String] {
        let all = s.buckets.map(label)
        switch e.summaryPeriod {
        case .day: return all.enumerated().filter { $0.offset % 6 == 0 }.map(\.element)
        case .week: return all
        case .month: return all.enumerated().filter { $0.offset % 5 == 0 }.map(\.element)
        }
    }

    @ViewBuilder
    private func rows(_ s: EnergyStore.Summary) -> some View {
        let t = s.totals, c = s.costs
        VStack(spacing: 8) {
            row("Netzbezug", kwh(t.import), c.map { "− " + euro($0.import) }, Theme.heat)
            row("Einspeisung", kwh(t.export), c.map { "+ " + euro($0.export) }, Theme.grid)
            row("Eigenverbrauch", kwh(t.selfUse), c.map { "gespart " + euro($0.saved) }, Theme.solar)
            if let c, c.base > 0 { row("Grundgebühr", "", "− " + euro(c.base), Theme.muted) }
            Divider().overlay(Theme.line)
            if let c {
                HStack {
                    Text("Saldo").font(.subheadline.weight(.bold))
                    Spacer()
                    Text((c.balance >= 0 ? "+ " : "− ") + euro(abs(c.balance)))
                        .font(.system(size: 18, weight: .semibold, design: .rounded)).monospacedDigit()
                        .foregroundStyle(c.balance >= 0 ? Theme.battery : Theme.heat)
                }
                Text("Saldo = Vergütung − Netzbezug − Grundgebühr. Ohne Solar hättest du zusätzlich \(euro(c.saved)) gezahlt.")
                    .font(.caption2).foregroundStyle(Theme.muted)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                Button { editing = true } label: {
                    Label("Preise eintragen, um Kosten und Gewinn zu sehen", systemImage: "eurosign.circle")
                        .font(.caption.weight(.semibold))
                }
                .foregroundStyle(Theme.solar)
                .disabled(!settings.permissions.edit)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private func row(_ title: String, _ amount: String, _ money: String?, _ color: Color) -> some View {
        HStack {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(title).font(.subheadline)
            Spacer()
            Text(amount).font(.subheadline).monospacedDigit().foregroundStyle(Theme.muted)
                .lineLimit(1).minimumScaleFactor(0.8)
            if let money {
                Text(money).font(.subheadline.weight(.semibold)).monospacedDigit()
                    .lineLimit(1).fixedSize()
                    .frame(minWidth: 96, alignment: .trailing)
            }
        }
    }

    private func kwh(_ v: Double) -> String { String(format: "%.1f kWh", v) }
    private func euro(_ v: Double) -> String { v.formatted(.currency(code: "EUR").locale(Locale(identifier: "de_DE"))) }
}

// MARK: - Preise bearbeiten (gilt fürs ganze Haus, liegt auf dem Hub)

struct PriceEditor: View {
    @Environment(EnergyStore.self) private var e
    @Environment(\.dismiss) private var dismiss
    let initial: EnergyStore.Prices
    @State private var imp = ""
    @State private var exp = ""
    @State private var base = ""
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    field("Bezugspreis", "0,32", "€/kWh", $imp)
                    field("Einspeisevergütung", "0,08", "€/kWh", $exp)
                    field("Grundgebühr (optional)", "12,50", "€/Monat", $base)
                } footer: {
                    Text("Gilt für alle im Haus. Daraus rechnet der Hub Kosten, Vergütung und was du durch die Sonne sparst.")
                }
                if let error { Text(error).foregroundStyle(Theme.heat) }
            }
            .navigationTitle("Strompreise")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Abbrechen") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Sichern") { Task { await save() } }.disabled(saving)
                }
            }
            .onAppear {
                imp = fmt(initial.import); exp = fmt(initial.export); base = fmt(initial.base_month)
            }
        }
    }

    private func field(_ title: String, _ placeholder: String, _ unit: String, _ text: Binding<String>) -> some View {
        HStack {
            Text(title)
            Spacer()
            TextField(placeholder, text: text).keyboardType(.decimalPad).multilineTextAlignment(.trailing).frame(width: 90)
            Text(unit).foregroundStyle(Theme.muted).frame(width: 64, alignment: .leading)
        }
    }

    private func fmt(_ v: Double?) -> String {
        guard let v else { return "" }
        return v.formatted(.number.precision(.fractionLength(0...4)).locale(Locale(identifier: "de_DE")))
    }

    private func parse(_ s: String) -> Double? {
        let t = s.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: ".")
        return t.isEmpty ? nil : Double(t)
    }

    private func save() async {
        saving = true; defer { saving = false }
        for s in [imp, exp, base] where !s.trimmingCharacters(in: .whitespaces).isEmpty && parse(s) == nil {
            error = "„\(s)“ ist keine Zahl"; return
        }
        do {
            try await e.savePrices(.init(import: parse(imp), export: parse(exp), base_month: parse(base)))
            dismiss()
        } catch {
            self.error = (error as? ServerError)?.message ?? error.localizedDescription
        }
    }
}
