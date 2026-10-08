import SwiftUI

/// „Zentral aus" + Szenen-Kacheln (Home-Tab). Tippen = Szene starten, lange drücken = Ausschalten/Bearbeiten.
struct ScenesSection: View {
    @Environment(SceneStore.self) private var store
    @Environment(HomeStore.self) private var home
    @Environment(AppSettings.self) private var settings
    @State private var editing: HomeScene?
    @State private var confirmDelete: HomeScene?

    private let cols = [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                SectionLabel(text: "Szenen")
                Spacer()
                if settings.permissions.edit {
                    Button { editing = HomeScene() } label: {
                        Label("Neu", systemImage: "plus").font(.subheadline.weight(.semibold))
                    }
                    .disabled(home.structure == nil)
                }
            }
            LazyVGrid(columns: cols, spacing: 10) {
                centralTile
                ForEach(store.scenes, id: \.stableID) { s in
                    SceneTile(scene: s, flash: store.flash[s.stableID]) {
                        Task { await store.run(s) }
                    }
                    .contextMenu {
                        Button { Task { await store.run(s) } } label: { Label("Starten", systemImage: "play.fill") }
                        Button { Task { await store.run(s, stop: true) } } label: { Label("Ausschalten", systemImage: "stop.fill") }
                        if settings.permissions.edit {
                            Button { editing = s } label: { Label("Bearbeiten", systemImage: "pencil") }
                            Button(role: .destructive) { confirmDelete = s } label: { Label("Löschen", systemImage: "trash") }
                        }
                    }
                }
            }
            if let e = store.lastError {
                Text(e).font(.caption).foregroundStyle(Theme.heat)
            }
        }
        .sheet(item: Binding(get: { editing.map(EditBox.init) }, set: { editing = $0?.scene })) { box in
            SceneEditor(scene: box.scene) { editing = nil }
                .environment(store).environment(home)
        }
        .confirmationDialog("Szene löschen?", isPresented: Binding(get: { confirmDelete != nil }, set: { if !$0 { confirmDelete = nil } }),
                            presenting: confirmDelete) { s in
            Button("„\(s.name)“ löschen", role: .destructive) { Task { await store.delete(s) } }
        }
        .task {
            while !Task.isCancelled {
                await store.refresh()
                try? await Task.sleep(for: .seconds(30))
            }
        }
    }

    private var centralTile: some View {
        Button { Task { await store.centralOff() } } label: {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    Image(systemName: "power").font(.title3).foregroundStyle(Theme.heat)
                    Spacer()
                    if let f = store.flash["central"] { Text(f).font(.caption.weight(.bold)).foregroundStyle(Theme.battery) }
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("Zentral aus").font(.subheadline.weight(.bold)).foregroundStyle(Theme.text)
                    Text("alle Lichter").font(.caption).foregroundStyle(Theme.muted)
                }
            }
            .card()
        }
        .buttonStyle(.plain)
    }
}

private struct EditBox: Identifiable {
    let scene: HomeScene
    var id: String { scene.stableID }
}

struct SceneTile: View {
    let scene: HomeScene
    let flash: String?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    Image(systemName: scene.icon).font(.title3).foregroundStyle(Theme.solar)
                    Spacer()
                    if let flash {
                        Text(flash).font(.caption.weight(.bold))
                            .foregroundStyle(flash.contains("✓") || flash == "…" ? Theme.battery : Theme.heat).lineLimit(1)
                    } else if scene.schedule.enabled {
                        Image(systemName: "clock").font(.caption).foregroundStyle(Theme.muted)
                    }
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(scene.name).font(.subheadline.weight(.bold)).foregroundStyle(Theme.text).lineLimit(1)
                    Text(scene.scheduleText ?? "\(scene.actions.count) Geräte")
                        .font(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                }
            }
            .card()
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Bearbeiten

struct SceneEditor: View {
    @Environment(SceneStore.self) private var store
    @Environment(HomeStore.self) private var home
    @State var scene: HomeScene
    let done: () -> Void
    @State private var picking = false

    /// fid → (Gerät, Raum) aus dem Projekt
    private var lookup: [String: (Device, String)] {
        var d: [String: (Device, String)] = [:]
        for fl in home.floors { for r in fl.rooms { for f in r.devices { d[f.id] = (f, r.name) } } }
        return d
    }

    /// Ältere Szenen (nur LUXOR) speichern die Geräte-ID ohne „lx:"
    static func normalized(_ a: SceneAction) -> SceneAction {
        var a = a
        if !a.fid.contains(":") { a.fid = "lx:" + a.fid }
        return a
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Name, z. B. Garten", text: $scene.name)
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(HomeScene.icons, id: \.self) { icon in
                                Button { scene.icon = icon } label: {
                                    Image(systemName: icon).frame(width: 38, height: 38)
                                        .foregroundStyle(scene.icon == icon ? Theme.bg : Theme.text)
                                        .background(scene.icon == icon ? Theme.solar : Theme.control, in: Circle())
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                }

                Section {
                    ForEach($scene.actions) { $a in
                        if let hit = lookup[a.fid] {
                            ActionRow(action: $a, function: hit.0, room: hit.1)
                        } else {
                            Text("\(a.name ?? a.fid) – nicht mehr im Projekt").foregroundStyle(Theme.heat)
                        }
                    }
                    .onDelete { scene.actions.remove(atOffsets: $0) }
                    Button { picking = true } label: { Label("Geräte auswählen", systemImage: "plus.circle") }
                } header: {
                    Text("Geräte (\(scene.actions.count))")
                } footer: {
                    Text("Tippen auf die Kachel stellt alle Geräte so ein. „Ausschalten“ (lange drücken oder Zeitplan-Ende) schaltet die beteiligten Lichter wieder aus.")
                }

                Section("Zeitplan") {
                    Toggle("Automatisch", isOn: $scene.schedule.enabled)
                    if scene.schedule.enabled {
                        DayPicker(days: $scene.schedule.days)
                        TimeSpec(title: "Einschalten", spec: $scene.schedule.start)
                        TimeSpec(title: "Ausschalten", spec: $scene.schedule.stop)
                    }
                }

                if scene.id != nil {
                    Section {
                        Button("Jetzt testen") { Task { await store.save(scene); await store.run(scene) } }
                        Button("Szene löschen", role: .destructive) { Task { await store.delete(scene); done() } }
                    }
                }
            }
            .navigationTitle(scene.id == nil ? "Neue Szene" : scene.name)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Abbrechen", action: done) }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Sichern") { Task { await store.save(scene); done() } }
                        .disabled(scene.name.trimmingCharacters(in: .whitespaces).isEmpty || scene.actions.isEmpty)
                }
            }
            .sheet(isPresented: $picking) {
                DevicePicker(selected: Set(scene.actions.map(\.fid))) { picked in
                    // bestehende Einstellungen behalten, neue mit sinnvollen Vorgaben anlegen
                    let old = Dictionary(scene.actions.map { ($0.fid, $0) }, uniquingKeysWith: { a, _ in a })
                    scene.actions = picked.compactMap { fid in
                        if let a = old[fid] { return a }
                        guard let hit = lookup[fid] else { return nil }
                        return Self.defaultAction(for: hit.0)
                    }
                    picking = false
                }
                .environment(home)
            }
        }
        .onAppear { scene.actions = scene.actions.map { Self.normalized($0) } }
        .preferredColorScheme(.dark)
    }

    static func defaultAction(for f: Device) -> SceneAction {
        var a = SceneAction(fid: f.id, name: f.name)
        switch f.kind {
        case .dimmer: a.level = 100
        case .light: a.on = true
        case .blind: a.position = 100
        case .heating: a.target = 21
        case .gate, .other: a.on = true
        }
        return a
    }
}

private struct ActionRow: View {
    @Binding var action: SceneAction
    let function: Device
    let room: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                VStack(alignment: .leading, spacing: 1) {
                    Text(function.name).font(.subheadline.weight(.semibold))
                    Text(room).font(.caption).foregroundStyle(Theme.muted)
                }
                Spacer()
                switch function.kind {
                case .light, .other, .gate:
                    Toggle("", isOn: Binding(get: { action.on ?? true }, set: { action.on = $0 })).labelsHidden()
                case .dimmer:
                    Text((action.level ?? 100) == 0 ? "aus" : "\(Int(action.level ?? 100)) %").monospacedDigit().foregroundStyle(Theme.muted)
                case .blind:
                    Text(blindText).foregroundStyle(Theme.muted)
                case .heating:
                    Stepper(String(format: "%.1f °C", action.target ?? 21),
                            value: Binding(get: { action.target ?? 21 }, set: { action.target = $0 }), in: 15...28, step: 0.5)
                        .fixedSize()
                }
            }
            if function.kind == .dimmer {
                Slider(value: Binding(get: { action.level ?? 100 }, set: { action.level = $0.rounded() }), in: 0...100, step: 5)
            } else if function.kind == .blind {
                Picker("", selection: Binding(get: { action.position ?? 100 }, set: { action.position = $0 })) {
                    Text("Auf").tag(0.0); Text("Halb").tag(50.0); Text("Zu").tag(100.0)
                }
                .pickerStyle(.segmented)
            }
        }
        .padding(.vertical, 2)
    }

    private var blindText: String {
        switch action.position ?? 100 { case 0: return "auf"; case 100: return "zu"; default: return "\(Int(action.position ?? 0)) % zu" }
    }
}

private struct DayPicker: View {
    @Binding var days: [Int]
    var body: some View {
        HStack(spacing: 6) {
            ForEach(0..<7, id: \.self) { d in
                let on = days.contains(d)
                Button(SceneSchedule.dayNames[d]) {
                    if on { days.removeAll { $0 == d } } else { days.append(d) }
                }
                .font(.caption.weight(.bold)).frame(maxWidth: .infinity).padding(.vertical, 8)
                .foregroundStyle(on ? Theme.bg : Theme.text)
                .background(on ? Theme.solar : Theme.control, in: Capsule())
                .buttonStyle(.plain)
            }
        }
    }
}

/// Start/Stopp: keine · Uhrzeit · Sonnenuntergang/-aufgang ± Minuten
private struct TimeSpec: View {
    let title: String
    @Binding var spec: SceneTime?

    private enum Mode: String, CaseIterable { case none = "–", time = "Uhrzeit", sunset = "Sonnenunterg.", sunrise = "Sonnenaufg." }

    private var mode: Binding<Mode> {
        Binding(get: {
            switch spec?.type { case .none: return .none; case .time: return .time; case .sunset: return .sunset; case .sunrise: return .sunrise }
        }, set: { m in
            switch m {
            case .none: spec = nil
            case .time: spec = SceneTime(type: .time, time: spec?.time ?? (title == "Einschalten" ? "18:00" : "23:00"), offset: 0)
            case .sunset: spec = SceneTime(type: .sunset, time: nil, offset: spec?.offset ?? 0)
            case .sunrise: spec = SceneTime(type: .sunrise, time: nil, offset: spec?.offset ?? 0)
            }
        })
    }

    private var date: Binding<Date> {
        Binding(get: {
            let p = (spec?.time ?? "18:00").split(separator: ":").compactMap { Int($0) }
            return Calendar.current.date(bySettingHour: p.first ?? 18, minute: p.last ?? 0, second: 0, of: .now) ?? .now
        }, set: { d in
            let c = Calendar.current.dateComponents([.hour, .minute], from: d)
            spec?.time = String(format: "%02d:%02d", c.hour ?? 18, c.minute ?? 0)
        })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Picker(title, selection: mode) {
                ForEach(Mode.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }
            if spec?.type == .time {
                DatePicker("Uhrzeit", selection: date, displayedComponents: .hourAndMinute)
            } else if spec != nil {
                Stepper(offsetText, value: Binding(get: { spec?.offset ?? 0 }, set: { spec?.offset = $0 }), in: -120...120, step: 15)
            }
        }
    }

    private var offsetText: String {
        let o = spec?.offset ?? 0
        return o == 0 ? "genau dann" : "\(abs(o)) Min \(o < 0 ? "vorher" : "danach")"
    }
}

/// Geräte aus allen Räumen auswählen (gruppiert wie in LUXORliving)
private struct DevicePicker: View {
    @Environment(HomeStore.self) private var home
    @State var selected: Set<String>
    let done: ([String]) -> Void

    var body: some View {
        NavigationStack {
            List {
                ForEach(home.floors) { floor in
                    ForEach(floor.rooms.filter { !$0.devices.filter(\.usable).isEmpty }) { room in
                        Section("\(room.name) · \(floor.name)") {
                            ForEach(room.devices.filter(\.usable)) { f in
                                Button {
                                    if selected.contains(f.id) { selected.remove(f.id) } else { selected.insert(f.id) }
                                } label: {
                                    HStack {
                                        Image(systemName: f.kindIcon).frame(width: 24).foregroundStyle(Theme.muted)
                                        Text(f.name).foregroundStyle(Theme.text)
                                        Spacer()
                                        if selected.contains(f.id) { Image(systemName: "checkmark").foregroundStyle(Theme.solar) }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("Geräte (\(selected.count))")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Fertig") {
                        // Reihenfolge: wie im Haus (Etage → Raum → Gerät)
                        let all = home.floors.flatMap(\.rooms).flatMap(\.devices).map(\.id)
                        done(all.filter { selected.contains($0) })
                    }
                }
            }
        }
        .preferredColorScheme(.dark)
    }
}

extension Device {
    /// Tore werden nie über Szenen ausgelöst
    var usable: Bool { kind != .other && kind != .gate }
    var kindIcon: String {
        switch kind {
        case .dimmer, .light: return "lightbulb"
        case .blind: return "blinds.horizontal.closed"
        case .heating: return "thermometer.medium"
        case .gate: return "door.garage.closed"
        case .other: return "square"
        }
    }
}
