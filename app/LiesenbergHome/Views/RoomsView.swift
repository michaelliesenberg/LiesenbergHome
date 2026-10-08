import SwiftUI

struct RoomsView: View {
    @Environment(HomeStore.self) private var home
    @State private var floorFilter: String?

    private let columns = Array(repeating: GridItem(.flexible(), spacing: 10), count: 3)

    var body: some View {
        Screen(title: "Räume") {
            if home.floors.isEmpty {
                emptyState
            } else {
                ScrollView(.horizontal) {
                    HStack(spacing: 8) {
                        chip("Alle", selected: floorFilter == nil) { floorFilter = nil }
                        ForEach(home.floors) { f in
                            chip(short(f.name), selected: floorFilter == f.id) { floorFilter = f.id }
                        }
                    }
                }
                .scrollIndicators(.hidden)

                ForEach(home.floors.filter { floorFilter == nil || $0.id == floorFilter }) { floor in
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            SectionLabel(text: floor.name)
                            Spacer()
                            Text("\(floor.rooms.count) Räume").font(.caption).foregroundStyle(Theme.faint)
                        }
                        LazyVGrid(columns: columns, spacing: 10) {
                            ForEach(floor.rooms) { room in
                                NavigationLink(value: room) { RoomTile(room: room) }
                                    .buttonStyle(.plain)
                            }
                        }
                    }
                    .padding(.top, 4)
                }
            }
        }
        .refreshable { await home.syncFromServer(); await home.refresh(ids: allIDs) }
        .task {
            await home.syncFromServer()
            await home.refresh(ids: allIDs)          // „2 an · 21,5°" auf den Kacheln
        }
        .navigationDestination(for: Room.self) { room in
            RoomDetailView(room: room, floor: home.floors.first { $0.rooms.contains(room) }?.name ?? "")
        }
    }

    private var allIDs: [String] {
        home.floors.flatMap(\.rooms).flatMap { $0.lights + $0.heating }.map(\.id)
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 12) {
            Image(systemName: "square.grid.2x2").font(.largeTitle).foregroundStyle(Theme.solar)
            Text("Noch keine Räume").font(.headline)
            Text(home.lastError ?? "Räume und Geräte kommen automatisch vom Hub – aus LUXORliving oder Home Assistant.")
                .font(.footnote).foregroundStyle(Theme.muted)
        }
        .card(padding: 18)
    }

    private func chip(_ t: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(t).font(.system(size: 12, weight: selected ? .bold : .semibold))
                .padding(.horizontal, 12).padding(.vertical, 8)
                .foregroundStyle(selected ? Theme.bg : Theme.text)
                .background(selected ? Theme.text : Theme.card, in: RoundedRectangle(cornerRadius: 12))
        }
        .buttonStyle(.plain)
    }

    private func short(_ floor: String) -> String {
        floor.replacingOccurrences(of: "Obergeschoss", with: "OG")
             .replacingOccurrences(of: "Erdgeschoss", with: "EG")
    }
}

struct RoomTile: View {
    @Environment(HomeStore.self) private var home
    let room: Room

    var body: some View {
        let on = home.lightsOn(in: room)
        let temp = room.heating.compactMap { home.actual($0) }.first
        let active = on > 0
        VStack(alignment: .leading, spacing: 10) {
            Image(systemName: RoomIcon.symbol(for: room.icon ?? "", name: room.name))
                .font(.system(size: 18))
                .foregroundStyle(active ? Theme.solar : Theme.muted)
                .frame(width: 40, height: 40)
                .background(active ? Theme.lightOnBG : Theme.control, in: RoundedRectangle(cornerRadius: 13, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(room.name).font(.system(size: 13, weight: .bold)).lineLimit(2).minimumScaleFactor(0.8)
                Text(status(on: on, temp: temp))
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(active ? Theme.solar : Color(hex: 0x7A808A))
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, minHeight: 104, alignment: .topLeading)
        .background(active ? Color(hex: 0x1E1B14) : Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }

    private func status(on: Int, temp: Double?) -> String {
        var parts: [String] = []
        if on > 0 { parts.append("\(on) an") }
        if let temp { parts.append(String(format: "%.1f°", temp)) }
        return parts.isEmpty ? "\(room.devices.count) Geräte" : parts.joined(separator: " · ")
    }
}

/// Raumsymbole (LUXOR-Icons, sonst nach dem Raumnamen) → SF Symbols
enum RoomIcon {
    static func symbol(for icon: String, name: String = "") -> String {
        switch icon {
        case "Bedroom": return "bed.double"
        case "Bathroom": return "bathtub"
        case "LivingRoom": return "sofa"
        case "DiningRoom": return "fork.knife"
        case "Kitchen": return "cooktop"
        case "WorkRoom", "Office": return "desktopcomputer"
        case "Corridor", "Hallway": return "door.left.hand.open"
        case "Garage": return "car"
        case "Parasol": return "sun.max"
        case "Storeroom", "StorageRoom": return "archivebox"
        case "Garden": return "tree"
        default: break
        }
        let n = name.lowercased()
        let hints: [(String, String)] = [("schlaf", "bed.double"), ("kind", "teddybear"), ("bad", "bathtub"), ("wc", "toilet"),
                                         ("küche", "cooktop"), ("wohn", "sofa"), ("ess", "fork.knife"), ("büro", "desktopcomputer"),
                                         ("arbeit", "desktopcomputer"), ("flur", "door.left.hand.open"), ("garage", "car"),
                                         ("garten", "tree"), ("terrasse", "sun.max"), ("keller", "archivebox")]
        return hints.first { n.contains($0.0) }?.1 ?? "square.split.bottomrightquarter"
    }
}
