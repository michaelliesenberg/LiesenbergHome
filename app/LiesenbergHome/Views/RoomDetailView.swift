import SwiftUI

struct RoomDetailView: View {
    @Environment(HomeStore.self) private var home
    @Environment(ClimateStore.self) private var climate
    @Environment(AppSettings.self) private var settings
    let room: Room
    let floor: String

    var body: some View {
        Screen(title: room.name) {
            if !floor.isEmpty {
                Text(floor).font(.caption.weight(.semibold)).foregroundStyle(Theme.muted)
            }

            ForEach(room.gates) { GateRow(d: $0) }
            ForEach(room.heating) { HeatingRow(f: $0) }
            ForEach(climate.units(in: room)) { ClimateCard(unit: $0) }

            if !room.blinds.isEmpty {
                SectionLabel(text: "Fenster")
                ForEach(room.blinds) { BlindRow(f: $0) }
            }

            if !room.lights.isEmpty {
                HStack {
                    SectionLabel(text: "Licht · \(home.lightsOn(in: room)) an")
                    Spacer()
                    Button("Alle aus") { home.allLightsOff(in: room) }
                        .font(.subheadline.weight(.semibold)).tint(Theme.solar)
                }
                ForEach(room.lights) { LightRow(f: $0) }
            }

            if let err = home.lastError {
                Label(err, systemImage: "exclamationmark.triangle").font(.footnote).foregroundStyle(Theme.heat)
            }
        }
        .refreshable { await home.refresh(room); await climate.refresh() }
        .task { await climate.refresh() }
        .task {
            // alle 5 s aktualisieren, solange der Raum offen ist
            while !Task.isCancelled {
                await home.refresh(room)
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }
}

// MARK: - Licht (Dimmer: ziehen für %, Birne zum Schalten)

struct LightRow: View {
    @Environment(HomeStore.self) private var home
    let f: Device
    @State private var dragLevel: Double?

    var body: some View {
        let level = dragLevel ?? home.level(f)
        let on = level > 0
        HStack(spacing: 12) {
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 12).fill(Theme.card2)
                    if f.kind == .dimmer {
                        RoundedRectangle(cornerRadius: 12)
                            .fill(LinearGradient(colors: [Theme.lightOnBG, Color(hex: 0x5A4520)], startPoint: .leading, endPoint: .trailing))
                            .frame(width: geo.size.width * level / 100)
                    }
                    HStack {
                        Text(f.name).font(.subheadline.weight(.semibold)).lineLimit(1)
                        Spacer()
                        Text(on ? (f.kind == .dimmer ? "\(Int(level)) %" : "An") : "Aus")
                            .font(.system(size: 13, weight: .semibold, design: .rounded)).monospacedDigit()
                            .foregroundStyle(on ? Theme.solar : Theme.faint)
                    }
                    .padding(.horizontal, 12)
                }
                .contentShape(Rectangle())
                .gesture(
                    DragGesture(minimumDistance: 4)
                        .onChanged { v in dragLevel = max(0, min(100, v.location.x / geo.size.width * 100)).rounded() }
                        .onEnded { _ in
                            if let l = dragLevel { home.setLevel(f, percent: l) }
                            dragLevel = nil
                        },
                    including: f.kind == .dimmer ? .all : .subviews)
            }
            .frame(height: 40)

            Button { home.toggle(f) } label: {
                Image(systemName: on ? "lightbulb.fill" : "lightbulb")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(on ? Theme.bg : Theme.muted)
                    .frame(width: 40, height: 40)
                    .background(on ? Theme.solar : Theme.control, in: RoundedRectangle(cornerRadius: 13, style: .continuous))
            }
            .buttonStyle(.plain)
            .sensoryFeedback(.impact(weight: .light), trigger: on)
        }
        .padding(.leading, 8).padding(.vertical, 8).padding(.trailing, 8)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
    }
}

// MARK: - Rollladen / Fenster

struct BlindRow: View {
    @Environment(HomeStore.self) private var home
    let f: Device

    var body: some View {
        let closed = home.closed(f)
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(f.name).font(.subheadline.weight(.semibold))
                    Spacer()
                    Text("\(Int(closed)) % zu").font(.system(size: 13, weight: .semibold, design: .rounded))
                        .monospacedDigit().foregroundStyle(Theme.grid)
                }
                ProgressView(value: closed / 100).tint(Theme.grid)
            }
            control("chevron.up", "\(f.name) hoch") { home.blindUp(f) }
            control("stop.fill", "\(f.name) stopp") { home.blindStop(f) }
            control("chevron.down", "\(f.name) runter") { home.blindDown(f) }
        }
        .card(padding: 12)
    }

    private func control(_ icon: String, _ label: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon).font(.system(size: 15, weight: .bold))
                .frame(width: 40, height: 40)
                .background(Theme.control, in: RoundedRectangle(cornerRadius: 13, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

// MARK: - Heizung (Ist / Soll)

struct HeatingRow: View {
    @Environment(HomeStore.self) private var home
    @Environment(AppSettings.self) private var settings
    let f: Device

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: "thermometer.medium").font(.title3).foregroundStyle(Theme.heat)
                .frame(width: 44, height: 44)
                .background(Color(hex: 0x3A2417), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(home.actual(f).map { String(format: "%.1f°", $0) } ?? "–")
                    .font(.system(size: 26, weight: .semibold, design: .rounded)).monospacedDigit()
                Text("\(f.name) · Soll \(home.target(f).map { String(format: "%.1f°", $0) } ?? "–")")
                    .font(.caption).foregroundStyle(Theme.muted).lineLimit(1)
            }
            Spacer()
            if settings.permissions.climate {
                round("minus") { home.changeTarget(f, by: -0.5) }
                round("plus") { home.changeTarget(f, by: 0.5) }
            }
        }
        .card()
    }

    private func round(_ icon: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon).font(.system(size: 15, weight: .bold))
                .frame(width: 40, height: 40).background(Theme.control, in: Circle())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Tor / Tür (Impuls)

struct GateRow: View {
    @Environment(HomeStore.self) private var home
    let d: Device
    @State private var confirm = false
    @State private var result: String?

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: d.name.localizedCaseInsensitiveContains("tür") || d.name.localizedCaseInsensitiveContains("door")
                  ? "door.left.hand.closed" : "door.garage.closed")
                .font(.title3).foregroundStyle(Theme.solar)
                .frame(width: 44, height: 44)
                .background(Theme.lightOnBG, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(d.name).font(.subheadline.weight(.semibold))
                Text(result ?? "Impuls – wie der Taster").font(.caption)
                    .foregroundStyle(result == nil ? Theme.muted : (result!.contains("✓") ? Theme.battery : Theme.heat))
            }
            Spacer()
            Button("Auslösen") { confirm = true }
                .font(.subheadline.weight(.bold))
                .padding(.horizontal, 14).padding(.vertical, 9)
                .background(Theme.solar, in: Capsule()).foregroundStyle(Theme.bg)
                .buttonStyle(.plain)
        }
        .card()
        .confirmationDialog("\(d.name) auslösen?", isPresented: $confirm, titleVisibility: .visible) {
            Button("Auslösen") {
                Task {
                    do { try await home.trigger(d); result = "ausgelöst ✓" }
                    catch { result = error.localizedDescription }
                    try? await Task.sleep(for: .seconds(4)); result = nil
                }
            }
        }
    }
}
