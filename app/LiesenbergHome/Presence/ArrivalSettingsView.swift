import SwiftUI
import CoreLocation
import MapKit

/// Einstellungen → Ankommen & Wegfahren
struct ArrivalSettingsView: View {
    @Environment(ArrivalManager.self) private var arrival
    @Environment(HomeStore.self) private var home
    @Environment(SceneStore.self) private var scenes
    @Environment(AppSettings.self) private var settings

    private var gates: [Device] { (home.structure?.allDevices ?? []).filter { $0.kind == .gate } }
    private var c: ArrivalConfig { arrival.config }

    var body: some View {
        Form {
            Section {
                Toggle("Ankommen & Wegfahren", isOn: Binding(get: { c.enabled }, set: { arrival.setEnabled($0) }))
            } footer: {
                Text("Wenn du mindestens 10 Minuten weg warst (oder weiter als \(Int(c.farRadius)) m) und zurückkommst, fragt dich eine Mitteilung, ob das Tor aufgehen soll. Wer nur ums Haus spaziert, löst nichts aus.")
            }

            if c.enabled {
                Section("Zuhause") {
                    if let center = c.center {
                        Map(initialPosition: .region(MKCoordinateRegion(center: center, latitudinalMeters: c.farRadius * 2.4,
                                                                         longitudinalMeters: c.farRadius * 2.4))) {
                            MapCircle(center: center, radius: c.farRadius).foregroundStyle(Theme.grid.opacity(0.10))
                                .stroke(Theme.grid.opacity(0.6), lineWidth: 1)
                            MapCircle(center: center, radius: c.nearRadius).foregroundStyle(Theme.solar.opacity(0.25))
                                .stroke(Theme.solar, lineWidth: 1.5)
                            Marker("Zuhause", systemImage: "house.fill", coordinate: center).tint(Theme.solar)
                        }
                        .frame(height: 200)
                        .listRowInsets(EdgeInsets())
                        .id("\(center.latitude),\(center.longitude),\(c.nearRadius),\(c.farRadius)")
                    }
                    Button {
                        arrival.useCurrentLocationAsHome()
                    } label: {
                        Label(arrival.locating ? "Suche Standort …" : (c.hasHome ? "Hier ist mein Zuhause (neu setzen)" : "Hier ist mein Zuhause"),
                              systemImage: "location.fill")
                    }
                    .disabled(arrival.locating)
                    Picker("Angekommen ab", selection: Binding(get: { c.nearRadius }, set: { v in arrival.update { $0.nearRadius = v } })) {
                        Text("150 m").tag(150.0); Text("200 m").tag(200.0); Text("300 m").tag(300.0); Text("500 m").tag(500.0)
                    }
                    Picker("Unterwegs ab", selection: Binding(get: { c.farRadius }, set: { v in arrival.update { $0.farRadius = v } })) {
                        Text("1 km").tag(1000.0); Text("2 km").tag(2000.0); Text("5 km").tag(5000.0)
                    }
                }

                Section {
                    if settings.permissions.restricted && !gates.isEmpty {
                        Picker("Tor", selection: Binding(get: { c.gateID ?? "" }, set: { v in arrival.update { $0.gateID = v.isEmpty ? nil : v } })) {
                            Text("keins").tag("")
                            ForEach(gates) { Text($0.name).tag($0.id) }
                        }
                    }
                    Picker("Szene", selection: Binding(get: { c.arrivalSceneID ?? "" }, set: { v in arrival.update { $0.arrivalSceneID = v.isEmpty ? nil : v } })) {
                        Text("keine").tag("")
                        ForEach(scenes.scenes.filter { $0.id != nil }, id: \.stableID) { Text($0.name).tag($0.id ?? "") }
                    }
                    if settings.permissions.restricted && c.gateID != nil {
                        Toggle("Im Auto ohne Nachfrage öffnen", isOn: Binding(get: { c.autoInCar }, set: { v in arrival.update { $0.autoInCar = v } }))
                    }
                } header: {
                    Text("Beim Ankommen")
                } footer: {
                    Text(c.autoInCar
                         ? "Ohne Nachfrage nur, wenn dein iPhone gerade mit CarPlay verbunden ist. Zu Fuß kommt immer die Mitteilung."
                         : "Die Mitteilung hat Knöpfe „Tor öffnen“ und „Szene starten“. Das Tor geht nur bei entsperrtem iPhone auf (Face ID).")
                }

                Section {
                    Toggle("Fragen: „Alle Lichter aus?“", isOn: Binding(get: { c.leaveNotify }, set: { v in arrival.update { $0.leaveNotify = v } }))
                    Picker("Szene beim Wegfahren", selection: Binding(get: { c.leaveSceneID ?? "" }, set: { v in arrival.update { $0.leaveSceneID = v.isEmpty ? nil : v } })) {
                        Text("keine").tag("")
                        ForEach(scenes.scenes.filter { $0.id != nil }, id: \.stableID) { Text($0.name).tag($0.id ?? "") }
                    }
                } header: {
                    Text("Beim Wegfahren")
                } footer: {
                    Text(c.leaveNotify ? "Beim Verlassen des Nahbereichs kommt eine Mitteilung mit „Alle Lichter aus“ (und der Szene)."
                                       : "Ohne Nachfrage wird beim Wegfahren nur die gewählte Szene gestartet.")
                }

                Section {
                    LabeledContent("Standort", value: authText)
                        .foregroundStyle(arrival.authorization == .authorizedAlways ? Theme.battery : Theme.heat)
                    if arrival.preciseLocationOff {
                        Label("„Genauer Standort“ ist aus – damit meldet iOS kein Ankommen. In den iPhone-Einstellungen → Liesenberg Home → Standort einschalten.",
                              systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote).foregroundStyle(Theme.heat)
                    }
                    LabeledContent("Mitteilungen", value: arrival.notificationsAllowed ? "erlaubt" : "aus")
                        .foregroundStyle(arrival.notificationsAllowed ? Theme.battery : Theme.heat)
                    if arrival.authorization != .authorizedAlways || !arrival.notificationsAllowed || arrival.preciseLocationOff {
                        Button("iPhone-Einstellungen öffnen") {
                            if let u = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(u) }
                        }
                    }
                    Button { arrival.testArrival() } label: { Label("Ankommen jetzt testen", systemImage: "play.circle") }
                } header: {
                    Text("Status")
                } footer: {
                    Text("„Testen“ schickt sofort die Mitteilung wie beim Heimkommen – so siehst du, ob Mitteilung und Tor-Knopf funktionieren.")
                }

                if !arrival.events.isEmpty {
                    Section("Was iOS gemeldet hat") {
                        ForEach(Array(arrival.events.enumerated()), id: \.offset) { _, e in
                            Text(e).font(.caption).foregroundStyle(Theme.muted)
                        }
                    }
                }
            }
        }
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .navigationTitle("Ankommen & Wegfahren")
        .task {
            await arrival.refreshNotificationStatus()
            if scenes.scenes.isEmpty { await scenes.refresh() }
        }
    }

    private var authText: String {
        switch arrival.authorization {
        case .authorizedAlways: return "immer erlaubt"
        case .authorizedWhenInUse: return "nur beim Benutzen – bitte „Immer“"
        case .denied, .restricted: return "nicht erlaubt"
        default: return "noch nicht gefragt"
        }
    }
}
