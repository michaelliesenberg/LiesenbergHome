import Foundation

// Haus-Struktur vom Hub (GET /api/home) – gleich, ob LUXORliving oder Home Assistant dahintersteckt.

struct HomeStructure: Codable, Equatable {
    var name: String?
    var backend: String?
    var floors: [Floor]

    var roomCount: Int { floors.reduce(0) { $0 + $1.rooms.count } }
    var allDevices: [Device] { floors.flatMap(\.rooms).flatMap(\.devices) }
}

struct Floor: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var rooms: [Room]
}

struct Room: Codable, Identifiable, Equatable, Hashable {
    var id: String
    var name: String
    var icon: String?
    var devices: [Device]
}

struct Device: Codable, Identifiable, Equatable, Hashable {
    enum Kind: String, Codable {
        case light, dimmer, blind, heating, gate, other
        init(from decoder: Decoder) throws {
            self = Kind(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .other
        }
    }

    var id: String           // "lx:…" (LUXOR) · "ha:light.kueche" (Home Assistant) · "demo:…"
    var name: String
    var kind: Kind
    var restricted: Bool?    // Tore/Türen – Gäste sehen sie nicht

    var isLight: Bool { kind == .light || kind == .dimmer }
}

/// Zustand eines Geräts (GET /api/home/state)
struct DeviceState: Codable, Equatable {
    var on: Bool?
    var level: Double?       // 0…100
    var position: Double?    // 0 = offen … 100 = zu
    var current: Double?     // °C
    var target: Double?      // °C
}

extension Room {
    var lights: [Device] { devices.filter(\.isLight) }
    var blinds: [Device] { devices.filter { $0.kind == .blind } }
    var heating: [Device] { devices.filter { $0.kind == .heating } }
    var gates: [Device] { devices.filter { $0.kind == .gate } }
}

/// Was die angemeldete Person darf (GET /api/me)
struct Permissions: Codable, Equatable {
    var edit: Bool
    var people: Bool
    var restricted: Bool
    var climate: Bool

    static let guest = Permissions(edit: false, people: false, restricted: false, climate: false)
    static let owner = Permissions(edit: true, people: true, restricted: true, climate: true)
}

struct Person: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var role: String
    var created: Double?
    var last_seen: Double?

    var roleName: String { ["owner": "Besitzer", "full": "Vollzugriff", "guest": "Gast"][role] ?? role }
}

struct HomeInfo: Codable, Equatable {
    var name: String?
    var local: String?
    var remote: String?
    var backend: String?
    var version: String?
}

struct MeResponse: Codable {
    var person: Person
    var permissions: Permissions
    var home: HomeInfo
}

struct JoinResponse: Codable {
    var key: String
    var person: Person
    var permissions: Permissions
    var home: HomeInfo
}
