import Foundation
import Observation

/// Mit welchem Zuhause (Hub) die App verbunden ist. Kommt aus der Einladung (QR-Code / Link) –
/// in der App selbst sind keine Adressen eingebaut.
@Observable
final class AppSettings {
    private let d = UserDefaults.standard

    var homeName: String { didSet { d.set(homeName, forKey: "homeName") } }
    /// Hub im Heimnetz, z. B. http://192.168.1.20:8080
    var localURL: String { didSet { d.set(localURL, forKey: "localURL") } }
    /// Hub von unterwegs, z. B. https://mein-hub.tailxxxx.ts.net
    var remoteURL: String { didSet { d.set(remoteURL, forKey: "remoteURL") } }
    var personName: String { didSet { d.set(personName, forKey: "personName") } }
    var personID: String { didSet { d.set(personID, forKey: "personID") } }
    var role: String { didSet { d.set(role, forKey: "role") } }
    var permissions: Permissions {
        didSet { d.set(try? JSONEncoder().encode(permissions), forKey: "permissions") }
    }
    /// Demo-Haus ohne echten Hub (für Apple-Prüfung und zum Ausprobieren)
    var demo: Bool { didSet { d.set(demo, forKey: "demo") } }
    /// App beim Öffnen mit Face ID / Code entsperren
    var faceID: Bool { didSet { d.set(faceID, forKey: "faceID") } }

    /// Persönlicher Schlüssel – nur im Schlüsselbund.
    var key: String {
        get { Keychain.get("hubKey") ?? "" }
        set { Keychain.set(newValue, for: "hubKey"); keyVersion += 1 }
    }
    private(set) var keyVersion = 0          // damit SwiftUI Änderungen am Schlüssel bemerkt

    var isConnected: Bool {
        _ = keyVersion
        return demo || (!key.isEmpty && (!localURL.isEmpty || !remoteURL.isEmpty))
    }
    var isOwner: Bool { role == "owner" }

    init() {
        homeName = d.string(forKey: "homeName") ?? ""
        localURL = d.string(forKey: "localURL") ?? ""
        remoteURL = d.string(forKey: "remoteURL") ?? ""
        personName = d.string(forKey: "personName") ?? ""
        personID = d.string(forKey: "personID") ?? ""
        role = d.string(forKey: "role") ?? "owner"
        permissions = (d.data(forKey: "permissions")).flatMap { try? JSONDecoder().decode(Permissions.self, from: $0) } ?? .owner
        demo = d.bool(forKey: "demo")
        faceID = d.bool(forKey: "faceID")
        // Ältere App-Version: gemeinsamer App-Schlüssel → als persönlicher Schlüssel weiterverwenden
        if Keychain.get("hubKey") == nil, let old = Keychain.get("appToken"), !old.isEmpty {
            Keychain.set(old, for: "hubKey")
        }
    }

    /// Nach erfolgreicher Einladung
    func apply(join: JoinResponse, local: String?, remote: String?) {
        key = join.key
        apply(me: MeResponse(person: join.person, permissions: join.permissions, home: join.home))
        if let local, !local.isEmpty, localURL.isEmpty { localURL = local }
        if let remote, !remote.isEmpty, remoteURL.isEmpty { remoteURL = remote }
        demo = false
    }

    /// Angaben vom Hub übernehmen (Name, Rolle, Adressen können sich ändern)
    func apply(me: MeResponse) {
        personName = me.person.name
        personID = me.person.id
        role = me.person.role
        permissions = me.permissions
        if let n = me.home.name, !n.isEmpty { homeName = n }
        if let l = me.home.local, !l.isEmpty { localURL = l }
        if let r = me.home.remote, !r.isEmpty { remoteURL = r }
    }

    func startDemo() {
        demo = true
        homeName = "Demo-Haus"
        personName = "Du"
        role = "owner"
        permissions = .owner
    }

    /// Zuhause trennen (Schlüssel löschen)
    func disconnect() {
        key = ""
        Keychain.set("", for: "appToken")
        demo = false
        homeName = ""; localURL = ""; remoteURL = ""; personName = ""; personID = ""; role = "guest"
        permissions = .guest
    }
}
