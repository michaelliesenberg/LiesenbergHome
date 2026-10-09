import SwiftUI

// MARK: - Auto laden (evcc-Ladepunkte)

struct CarView: View {
    @Environment(EnergyStore.self) private var e

    var body: some View {
        Screen(title: "Auto laden") {
            if e.loadpoints.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Image(systemName: "bolt.car").font(.largeTitle).foregroundStyle(Theme.solar)
                    Text("Noch keine Wallbox in evcc").font(.headline)
                    Text("Sobald Wallbox und Auto in evcc eingerichtet sind, erscheinen sie hier automatisch – mit Lademodus und Ladeziel.")
                        .font(.footnote).foregroundStyle(Theme.muted)
                }
                .card(padding: 18)
            }
            ForEach(e.loadpoints) { lp in
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Text(lp.vehicle).font(.title3.weight(.bold))
                        Spacer()
                        Text(lp.title).font(.caption).foregroundStyle(Theme.muted)
                    }
                    HStack(alignment: .firstTextBaseline) {
                        Text(lp.soc.map { "\(Int($0)) %" } ?? "–").font(.system(size: 44, weight: .semibold, design: .rounded))
                        if let t = lp.targetSoc { Text("→ \(Int(t)) %").foregroundStyle(Theme.muted) }
                    }
                    ProgressView(value: (lp.soc ?? 0) / 100).tint(Theme.solar)
                    Label(lp.charging ? "lädt mit \(kw(lp.chargePower)) kW" : (lp.connected ? "angesteckt" : "nicht angesteckt"),
                          systemImage: lp.charging ? "bolt.fill" : "powerplug")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(lp.charging ? Theme.solar : Theme.muted)
                    Text("Modus: \(modeName(lp.mode))").font(.caption).foregroundStyle(Theme.muted)
                }
                .card(padding: 16)
            }
        }
    }

    private func modeName(_ m: String) -> String {
        ["off": "Aus", "pv": "Solar", "minpv": "Min+PV", "now": "Schnell"][m] ?? m
    }
}

// MARK: - Klima (Daikin Onecta über den Haus-Server)

struct ClimateView: View {
    @Environment(ClimateStore.self) private var climate

    var body: some View {
        Screen(title: "Klima") {
            if let st = climate.state, st.loggedIn {
                if climate.units.isEmpty {
                    Text("Noch keine Geräte – der Hub fragt Daikin alle 10 Minuten ab.")
                        .font(.footnote).foregroundStyle(Theme.muted).card()
                }
                ForEach(climate.units) { ClimateCard(unit: $0, showRoom: true) }
                if let e = st.error { Text("Daikin: \(e)").font(.caption).foregroundStyle(Theme.heat) }
            } else {
                ComingSoon(icon: "air.conditioner.horizontal", color: Theme.grid,
                           title: "Daikin verbinden",
                           text: "Auf der Einrichtungsseite des Hubs unter „Klima“ einmal mit Daikin anmelden. Danach erscheinen alle Klimageräte hier und in ihren Räumen.")
            }
            if let e = climate.lastError { Text(e).font(.footnote).foregroundStyle(Theme.heat) }
        }
        .task { await climate.refresh() }
        .refreshable { await climate.refresh() }
    }
}

struct ClimateCard: View {
    @Environment(ClimateStore.self) private var climate
    @Environment(AppSettings.self) private var settings
    let unit: ClimateUnit
    var showRoom = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Image(systemName: "air.conditioner.horizontal").foregroundStyle(unit.on ? Theme.grid : Theme.muted)
                VStack(alignment: .leading, spacing: 2) {
                    Text(unit.name).font(.subheadline.weight(.bold))
                    Text((unit.on ? unit.modeName : "Aus") + (showRoom ? (unit.room.map { " · \($0)" } ?? "") : ""))
                        .font(.caption.weight(.semibold)).foregroundStyle(unit.on ? Theme.grid : Theme.muted)
                }
                Spacer()
                Toggle("", isOn: Binding(get: { unit.on }, set: { climate.setOn(unit, $0) }))
                    .labelsHidden().tint(Theme.grid)
            }
            HStack {
                Text("Raum \(unit.roomTemperature.map { String(format: "%.1f°", $0) } ?? "–")")
                    .font(.caption).foregroundStyle(Theme.muted)
                Spacer()
                Button { climate.changeTarget(unit, by: -(unit.targetStep ?? 0.5)) } label: {
                    Image(systemName: "minus").frame(width: 36, height: 36).background(Theme.control, in: Circle())
                }.buttonStyle(.plain)
                Text(unit.target.map { String(format: "%.1f°", $0) } ?? "–")
                    .font(.system(size: 24, weight: .semibold, design: .rounded)).monospacedDigit()
                    .foregroundStyle(unit.on ? Theme.grid : Theme.faint)
                    .frame(minWidth: 70)
                Button { climate.changeTarget(unit, by: unit.targetStep ?? 0.5) } label: {
                    Image(systemName: "plus").frame(width: 36, height: 36).background(Theme.control, in: Circle())
                }.buttonStyle(.plain)
            }
            if let modes = unit.modes, modes.count > 1 {
                ScrollView(.horizontal) {
                    HStack(spacing: 6) {
                        ForEach(modes, id: \.self) { m in
                            let u = ClimateUnit(id: unit.id, name: "", on: true, mode: m)
                            Button(u.modeName) { climate.setMode(unit, m) }
                                .font(.caption.weight(.semibold))
                                .padding(.horizontal, 10).padding(.vertical, 7)
                                .foregroundStyle(m == unit.mode ? Theme.bg : Theme.text)
                                .background(m == unit.mode ? Theme.grid : Theme.control, in: Capsule())
                                .buttonStyle(.plain)
                        }
                    }
                }.scrollIndicators(.hidden)
            }
        }
        .card()
        .disabled(!settings.permissions.climate)
    }
}

// MARK: - Geräte (Bosch/Siemens Home Connect über den Haus-Server)

struct Appliance: Codable, Identifiable {
    let id: String
    var name: String?
    var brand: String?
    var typeText: String?
    var connected: Bool?
    var stateText: String?
    var state: String?
    var program: String?
    var remaining: Double?
    var progress: Double?
    var door: String?
    var fridgeTemp: Double?
    var freezerTemp: Double?
}

struct AppliancesState: Codable {
    var configured: Bool
    var loggedIn: Bool
    var error: String?
    var appliances: [Appliance]
}

struct AppliancesView: View {
    @Environment(HouseClient.self) private var client
    @State private var st: AppliancesState?

    var body: some View {
        Screen(title: "Geräte") {
            if let st, st.loggedIn {
                if st.appliances.isEmpty {
                    Text("Noch keine Geräte – kommt gleich vom Hub.").font(.footnote).foregroundStyle(Theme.muted).card()
                }
                ForEach(st.appliances) { ApplianceCard(a: $0) }
                if let e = st.error { Text("Home Connect: \(e)").font(.caption).foregroundStyle(Theme.heat) }
            } else {
                ComingSoon(icon: "washer", color: Theme.battery,
                           title: "Home Connect verbinden",
                           text: "Auf der Einrichtungsseite des Hubs unter „Hausgeräte“ einmal mit Home Connect anmelden. Danach erscheinen Waschmaschine, Trockner, Geschirrspüler, Backofen und Kühlschrank hier – mit Status und Restzeit.")
            }
        }
        .task {
            while !Task.isCancelled {
                if let d = try? await client.get("api/appliances") {
                    st = try? JSONDecoder().decode(AppliancesState.self, from: d)
                }
                try? await Task.sleep(for: .seconds(10))
            }
        }
        .refreshable {
            if let d = try? await client.get("api/appliances") { st = try? JSONDecoder().decode(AppliancesState.self, from: d) }
        }
    }
}

struct ApplianceCard: View {
    let a: Appliance

    private var running: Bool { a.state == "Run" || a.state == "DelayedStart" }
    private var color: Color {
        switch a.state {
        case "Run": return Theme.battery
        case "Finished": return Theme.grid
        case "DelayedStart": return Theme.solar
        case "Error", "ActionRequired": return Theme.heat
        default: return Theme.muted
        }
    }
    private var icon: String {
        switch a.typeText ?? "" {
        case "Waschmaschine", "Waschtrockner": return "washer"
        case "Trockner": return "dryer"
        case "Geschirrspüler": return "dishwasher"
        case "Backofen": return "oven"
        case "Kühlschrank", "Gefrierschrank": return "refrigerator"
        case "Kaffeevollautomat": return "cup.and.saucer"
        default: return "powerplug"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 12) {
                Image(systemName: icon).font(.system(size: 20)).foregroundStyle(color)
                    .frame(width: 44, height: 44).background(Theme.control, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text(a.name ?? a.typeText ?? "Gerät").font(.subheadline.weight(.bold))
                    Text([a.brand, a.program].compactMap { $0 }.joined(separator: " · "))
                        .font(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    Text(a.connected == false ? "Offline" : (a.stateText ?? "–"))
                        .font(.caption.weight(.bold)).foregroundStyle(color)
                    if running, let r = a.remaining, r > 0 {
                        Text("noch \(Int(r) / 3600):\(String(format: "%02d", (Int(r) % 3600) / 60))")
                            .font(.caption2).foregroundStyle(Theme.muted).monospacedDigit()
                    } else if let f = a.fridgeTemp {
                        Text(String(format: "%.0f°", f) + (a.freezerTemp.map { String(format: " / %.0f°", $0) } ?? ""))
                            .font(.caption2).foregroundStyle(Theme.muted)
                    }
                }
            }
            if running, let p = a.progress {
                ProgressView(value: p / 100).tint(color)
            }
        }
        .card()
    }
}

struct ComingSoon: View {
    let icon: String, color: Color, title: String, text: String
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Image(systemName: icon).font(.largeTitle).foregroundStyle(color)
            Text(title).font(.headline)
            Text(text).font(.footnote).foregroundStyle(Theme.muted)
            Text("Einrichtung im Hub").font(.caption.weight(.semibold)).foregroundStyle(color)
        }
        .card(padding: 18)
    }
}

// MARK: - Einstellungen

struct SettingsView: View {
    @Environment(AppSettings.self) private var settings
    @Environment(HouseClient.self) private var client
    @Environment(HomeStore.self) private var home
    @Environment(AppLock.self) private var lock
    @Environment(\.openURL) private var openURL
    @State private var confirmDisconnect = false

    var body: some View {
        Form {
            Section("Zuhause") {
                LabeledContent("Name", value: settings.homeName.isEmpty ? "–" : settings.homeName)
                LabeledContent("Verbindung", value: client.connection.rawValue)
                    .foregroundStyle([.local, .remote, .demo].contains(client.connection) ? Theme.battery : Theme.text)
                LabeledContent("Du", value: "\(settings.personName) · \(Person(id: "", name: "", role: settings.role).roleName)")
                if let s = home.structure {
                    LabeledContent("Räume", value: "\(s.roomCount) · \(s.allDevices.count) Geräte")
                }
            }

            if settings.isOwner {
                Section {
                    NavigationLink { PeopleView() } label: { Label("Personen & Einladungen", systemImage: "person.2") }
                    if !settings.demo, let url = setupURL {
                        Button { openURL(url) } label: { Label("Hub einrichten", systemImage: "gearshape.2") }
                    }
                } footer: {
                    Text("Die Einrichtungsseite des Hubs öffnet sich im Browser – nur im Heimnetz.")
                }
            }

            if !settings.demo {
                Section {
                    NavigationLink { ArrivalSettingsView() } label: { Label("Ankommen & Wegfahren", systemImage: "location.circle") }
                } footer: {
                    Text("Beim Heimkommen fragt dein iPhone, ob das Tor aufgehen soll – auch wenn die App geschlossen ist.")
                }
            }

            Section {
                Toggle("Mit Face ID entsperren", isOn: Binding(get: { settings.faceID }, set: { lock.setEnabled($0) }))
            } footer: {
                Text("Tore und Türen lassen sich über Siri ohnehin nur bei entsperrtem iPhone öffnen.")
            }

            Section("Adressen") {
                LabeledContent("Im Heimnetz", value: settings.localURL.isEmpty ? "–" : settings.localURL)
                LabeledContent("Unterwegs", value: settings.remoteURL.isEmpty ? "nicht eingerichtet" : settings.remoteURL)
            }

            Section {
                Button(settings.demo ? "Demo beenden" : "Von diesem Zuhause abmelden", role: .destructive) {
                    if settings.demo { AppServices.shared.endDemo() } else { confirmDisconnect = true }
                }
            }
        }
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .navigationTitle("Einstellungen")
        .confirmationDialog("Von \(settings.homeName) abmelden?", isPresented: $confirmDisconnect, titleVisibility: .visible) {
            Button("Abmelden", role: .destructive) { AppServices.shared.disconnect() }
        } message: {
            Text("Danach brauchst du eine neue Einladung.")
        }
    }

    private var setupURL: URL? {
        settings.localURL.isEmpty ? nil : URL(string: settings.localURL.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/setup")
    }
}
