import SwiftUI

/// Alle Lautsprecher im Haus (vom Hub) + Zeitpläne.
struct SpeakersSection: View {
    @Environment(MusicServerStore.self) private var music
    @State private var editing: MusicSchedule?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                SectionLabel(text: "Alle Lautsprecher")
                Spacer()
                Button { Task { await music.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.plain).foregroundStyle(Theme.muted)
            }
            if music.speakers.isEmpty {
                Text(music.lastError ?? "Suche Lautsprecher …").font(.footnote).foregroundStyle(Theme.muted).card()
            }
            ForEach(music.speakers) { SpeakerRow(s: $0) }

            HStack {
                SectionLabel(text: "Zeitpläne")
                Spacer()
                Button {
                    editing = MusicSchedule(devices: music.speakers.prefix(1).map(\.id))
                } label: { Label("Neu", systemImage: "plus").font(.subheadline.weight(.semibold)) }
                    .tint(Theme.grid)
            }
            .padding(.top, 6)
            if music.schedules.isEmpty {
                Text("Z. B. „Mo–Fr 6:45 Küche: Radio“ oder „22:00 alles aus“.")
                    .font(.footnote).foregroundStyle(Theme.muted).card()
            }
            ForEach(music.schedules, id: \.stableID) { s in
                Button { editing = s } label: { ScheduleRow(s: s) }.buttonStyle(.plain)
            }
            Text("Eine bestimmte Apple-Music-Playlist zu einer Uhrzeit starten kann nur Apples Home-App (Automation → Tageszeit → Audio).")
                .font(.caption2).foregroundStyle(Theme.faint)
        }
        .sheet(item: Binding(get: { editing.map { IdentifiedSchedule(s: $0) } },
                             set: { editing = $0?.s })) { wrapper in
            ScheduleEditor(schedule: wrapper.s) { editing = nil }
                .environment(music)
        }
        .task {
            while !Task.isCancelled {
                await music.refresh()
                try? await Task.sleep(for: .seconds(15))
            }
        }
    }
}

private struct IdentifiedSchedule: Identifiable { let s: MusicSchedule; var id: String { s.id ?? "neu" } }

struct SpeakerRow: View {
    @Environment(MusicServerStore.self) private var music
    let s: Speaker
    @State private var vol: Double?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 12) {
                Image(systemName: (s.model ?? "").contains("Mini") ? "homepodmini.fill" : "homepod.fill")
                    .font(.title3).foregroundStyle(s.playing == true ? Theme.grid : Theme.muted)
                    .frame(width: 40, height: 40).background(Theme.control, in: RoundedRectangle(cornerRadius: 12))
                VStack(alignment: .leading, spacing: 2) {
                    Text(s.name ?? "Lautsprecher").font(.subheadline.weight(.bold))
                    Text(s.playing == true ? [s.title, s.artist].compactMap { $0 }.joined(separator: " · ") : (s.state ?? "–"))
                        .font(.caption).foregroundStyle(s.playing == true ? Theme.grid : Theme.muted).lineLimit(1)
                }
                Spacer()
                Button { music.command(s, "toggle") } label: {
                    Image(systemName: s.playing == true ? "pause.fill" : "play.fill")
                        .frame(width: 40, height: 40).background(Theme.control, in: Circle())
                }.buttonStyle(.plain)
            }
            HStack(spacing: 10) {
                Image(systemName: "speaker.fill").font(.caption).foregroundStyle(Theme.muted)
                Slider(value: Binding(get: { vol ?? s.volume ?? 0 }, set: { vol = $0 }), in: 0...100,
                       onEditingChanged: { editing in
                           if !editing, let v = vol { music.command(s, "volume", value: v.rounded()); vol = nil }
                       })
                .tint(Theme.grid)
                Image(systemName: "speaker.wave.3.fill").font(.caption).foregroundStyle(Theme.muted)
            }
        }
        .card(padding: 12)
        .opacity(s.online == false ? 0.5 : 1)
    }
}

struct ScheduleRow: View {
    @Environment(MusicServerStore.self) private var music
    let s: MusicSchedule

    private var speakerNames: String {
        s.devices.compactMap { id in music.speakers.first { $0.id == id }?.name }.joined(separator: ", ")
    }

    var body: some View {
        HStack(spacing: 12) {
            Text(s.time).font(.system(size: 22, weight: .semibold, design: .rounded)).monospacedDigit()
                .foregroundStyle(s.enabled ? Theme.text : Theme.faint)
            VStack(alignment: .leading, spacing: 2) {
                Text(s.name.isEmpty ? s.actionText : s.name).font(.subheadline.weight(.bold))
                Text("\(s.daysText) · \(speakerNames)")
                    .font(.caption).foregroundStyle(Theme.muted).lineLimit(1)
            }
            Spacer()
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.faint)
        }
        .card(padding: 12)
    }
}

struct ScheduleEditor: View {
    @Environment(MusicServerStore.self) private var music
    @State var schedule: MusicSchedule
    let done: () -> Void

    private var timeBinding: Binding<Date> {
        Binding(get: {
            let p = schedule.time.split(separator: ":").compactMap { Int($0) }
            return Calendar.current.date(bySettingHour: p.first ?? 7, minute: p.last ?? 0, second: 0, of: .now) ?? .now
        }, set: { d in
            let c = Calendar.current.dateComponents([.hour, .minute], from: d)
            schedule.time = String(format: "%02d:%02d", c.hour ?? 7, c.minute ?? 0)
        })
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Name (optional)", text: $schedule.name)
                    DatePicker("Uhrzeit", selection: timeBinding, displayedComponents: .hourAndMinute)
                    HStack(spacing: 6) {
                        ForEach(0..<7, id: \.self) { d in
                            let on = schedule.days.contains(d)
                            Button(MusicSchedule.dayNames[d]) {
                                if on { schedule.days.removeAll { $0 == d } } else { schedule.days.append(d) }
                            }
                            .font(.caption.weight(.bold)).frame(maxWidth: .infinity).padding(.vertical, 8)
                            .foregroundStyle(on ? Theme.bg : Theme.text)
                            .background(on ? Theme.grid : Theme.control, in: Capsule())
                            .buttonStyle(.plain)
                        }
                    }
                    Toggle("Aktiv", isOn: $schedule.enabled)
                }
                Section("Was passiert") {
                    Picker("Aktion", selection: $schedule.action) {
                        Text("Radio abspielen").tag("radio")
                        Text("Weiterspielen").tag("play")
                        Text("Ausschalten").tag("pause")
                    }
                    if schedule.action == "radio" {
                        Picker("Sender", selection: Binding(get: { schedule.station ?? music.stations.first ?? "" },
                                                            set: { schedule.station = $0 })) {
                            ForEach(music.stations, id: \.self) { Text($0).tag($0) }
                        }
                    }
                    if schedule.action != "pause" {
                        HStack {
                            Text("Lautstärke")
                            Slider(value: Binding(get: { schedule.volume ?? 25 }, set: { schedule.volume = $0.rounded() }),
                                   in: 0...100)
                            Text("\(Int(schedule.volume ?? 25)) %").monospacedDigit().foregroundStyle(Theme.muted)
                        }
                    }
                }
                Section("Lautsprecher") {
                    ForEach(music.speakers) { sp in
                        let on = schedule.devices.contains(sp.id)
                        Button {
                            if on { schedule.devices.removeAll { $0 == sp.id } } else { schedule.devices.append(sp.id) }
                        } label: {
                            HStack {
                                Text(sp.name ?? sp.id)
                                Spacer()
                                if on { Image(systemName: "checkmark").foregroundStyle(Theme.grid) }
                            }
                        }
                        .foregroundStyle(Theme.text)
                    }
                }
                if schedule.id != nil {
                    Section {
                        Button("Zeitplan löschen", role: .destructive) {
                            Task { await music.delete(schedule); done() }
                        }
                    }
                }
            }
            .navigationTitle(schedule.id == nil ? "Neuer Zeitplan" : "Zeitplan")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Abbrechen", action: done) }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Sichern") { Task { await music.save(schedule); done() } }
                        .disabled(schedule.devices.isEmpty || schedule.days.isEmpty)
                }
            }
        }
        .preferredColorScheme(.dark)
    }
}


/// „Läuft zu Hause": was gerade auf HomePods / AirPlay-Lautsprechern spielt – egal, wer es gestartet hat
struct HomePlayingCard: View {
    @Environment(MusicServerStore.self) private var music

    private var playing: [Speaker] { music.speakers.filter { $0.playing == true } }

    var body: some View {
        if !playing.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                SectionLabel(text: "Läuft zu Hause")
                ForEach(groups, id: \.key) { g in
                    HStack(spacing: 12) {
                        Image(systemName: "waveform").font(.title3).foregroundStyle(Theme.grid)
                            .symbolEffect(.variableColor.iterative, options: .repeating)
                            .frame(width: 44, height: 44).background(Theme.control, in: RoundedRectangle(cornerRadius: 12))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(g.first.title ?? "Unbekannter Titel").font(.subheadline.weight(.bold)).lineLimit(1)
                            Text([g.first.artist, g.first.app].compactMap { $0 }.joined(separator: " · "))
                                .font(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                            Label(g.names, systemImage: "homepod.fill").font(.caption2.weight(.semibold))
                                .foregroundStyle(Theme.grid).lineLimit(1)
                        }
                        Spacer()
                        Button { g.speakers.forEach { music.command($0, "pause") } } label: {
                            Image(systemName: "pause.fill").frame(width: 40, height: 40).background(Theme.control, in: Circle())
                        }
                        .buttonStyle(.plain)
                    }
                    .card(padding: 12)
                }
            }
        }
    }

    /// Gleicher Titel auf mehreren Lautsprechern (Gruppe) → eine Zeile
    private struct PlayGroup { let key: String; let speakers: [Speaker]
        var first: Speaker { speakers[0] }
        var names: String { speakers.compactMap(\.name).joined(separator: ", ") }
    }
    private var groups: [PlayGroup] {
        var order: [String] = []
        var dict: [String: [Speaker]] = [:]
        for s in playing {
            let k = "\(s.title ?? "")|\(s.artist ?? "")"
            if dict[k] == nil { order.append(k) }
            dict[k, default: []].append(s)
        }
        return order.map { PlayGroup(key: $0, speakers: dict[$0]!) }
    }
}
