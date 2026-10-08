import Foundation

/// Ein erfundenes Haus direkt in der App – beantwortet dieselben Anfragen wie ein echter Hub.
/// Für die App-Store-Prüfung und zum Ausprobieren ohne Hardware.
actor DemoHub {
    static let shared = DemoHub()

    private var states: [String: [String: Any]]
    private var scenes: [[String: Any]]
    private var people: [[String: Any]]
    private var climateOn = true
    private var climateTarget = 22.0
    private let structure: Any

    private init() {
        func parse(_ s: String) -> Any { (try? JSONSerialization.jsonObject(with: Data(s.utf8))) ?? [:] }
        structure = parse(Self.structureJSON)
        states = parse(Self.statesJSON) as? [String: [String: Any]] ?? [:]
        scenes = parse(Self.scenesJSON) as? [[String: Any]] ?? []
        people = parse(Self.peopleJSON) as? [[String: Any]] ?? []
    }

    // MARK: - Anfragen

    func handle(path: String, method: String, body: Data?) async throws -> Data {
        try? await Task.sleep(for: .milliseconds(120))          // wie ein echter Hub im WLAN
        let comps = URLComponents(string: "/" + path)
        let p = comps?.path ?? path
        let query = { (n: String) in comps?.queryItems?.first(where: { $0.name == n })?.value }
        let json = body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]

        switch (method, p) {
        case ("GET", "/api/health"):
            return Data(#"{"ok":true}"#.utf8)
        case ("GET", "/api/me"):
            var me: [String: Any] = [:]
            me["person"] = people.first ?? [:]
            me["permissions"] = ["edit": true, "people": true, "restricted": true, "climate": true]
            me["home"] = ["name": "Demo-Haus", "backend": "demo"]
            return try out(me)
        case ("GET", "/api/home"):
            return try out(structure)
        case ("GET", "/api/home/state"):
            let ids = (query("ids") ?? "").split(separator: ",").map(String.init)
            var res: [String: Any] = [:]
            for id in ids { if let st = states[id] { res[id] = st } }
            return try out(res)
        case ("POST", "/api/home/central-off"):
            for k in Array(states.keys) where states[k]?["on"] != nil {
                states[k]?["on"] = false
                if states[k]?["level"] != nil { states[k]?["level"] = 0.0 }
            }
            return ok()
        case ("POST", _) where p.hasPrefix("/api/home/device/"):
            let id = String(p.dropFirst("/api/home/device/".count)).removingPercentEncoding ?? ""
            var st: [String: Any] = states[id] ?? [:]
            if let on = json["on"] as? Bool {
                st["on"] = on
                if st["level"] != nil { st["level"] = on ? 100.0 : 0.0 }
            }
            if let l = json["level"] as? Double { st["level"] = l; st["on"] = l > 0 }
            if let pos = json["position"] as? Double { st["position"] = pos }
            if let mv = json["move"] as? String, mv != "stop" { st["position"] = mv == "down" ? 100.0 : 0.0 }
            if let t = json["target"] as? Double { st["target"] = t }
            states[id] = st
            return ok()

        case ("GET", "/api/scenes"):
            return try out(["scenes": scenes] as [String: Any])
        case ("PUT", "/api/scenes"):
            var s = json
            if (s["id"] as? String ?? "").isEmpty { s["id"] = String(UUID().uuidString.prefix(8)).lowercased() }
            scenes.removeAll { ($0["id"] as? String) == (s["id"] as? String) }
            scenes.append(s)
            return try out(s)
        case ("DELETE", _) where p.hasPrefix("/api/scenes/"):
            let id = String(p.dropFirst("/api/scenes/".count))
            scenes.removeAll { ($0["id"] as? String) == id }
            return ok()
        case ("POST", _) where p.hasPrefix("/api/scenes/") && p.hasSuffix("/run"):
            let id = String(p.dropFirst("/api/scenes/".count).dropLast("/run".count))
            let stop = query("phase") == "stop"
            let actions = scenes.first { ($0["id"] as? String) == id }?["actions"] as? [[String: Any]] ?? []
            for a in actions {
                guard let fid = a["fid"] as? String else { continue }
                var st: [String: Any] = states[fid] ?? [:]
                if stop {
                    if st["on"] != nil { st["on"] = false; if st["level"] != nil { st["level"] = 0.0 } }
                } else {
                    if let l = a["level"] as? Double { st["level"] = l; st["on"] = l > 0 }
                    if let on = a["on"] as? Bool { st["on"] = on }
                    if let pos = a["position"] as? Double { st["position"] = pos }
                    if let t = a["target"] as? Double { st["target"] = t }
                }
                states[fid] = st
            }
            var r: [String: Any] = ["ok": true, "errors": [String]()]
            r["phase"] = stop ? "stop" : "start"
            r["at"] = Self.hhmm()
            return try out(r)

        case ("GET", "/api/energy"):
            return try out(energy())
        case ("GET", "/api/daikin"):
            var unit: [String: Any] = ["id": "d1", "name": "Wohnzimmer", "mode": "cooling", "room": "Wohnzimmer"]
            unit["on"] = climateOn
            unit["modes"] = ["cooling", "heating", "auto", "fanOnly"]
            unit["roomTemperature"] = 24.6
            unit["target"] = climateTarget
            unit["targetStep"] = 0.5
            var res: [String: Any] = ["configured": true, "loggedIn": true]
            res["units"] = [unit]
            return try out(res)
        case ("PUT", _) where p.hasPrefix("/api/daikin/"):
            if let on = json["on"] as? Bool { climateOn = on }
            if let t = json["target"] as? Double { climateTarget = t }
            return ok()
        case ("GET", "/api/appliances"):
            return Data(Self.appliancesJSON.utf8)
        case ("GET", "/api/music"):
            return Data(Self.musicJSON.utf8)
        case (_, _) where p.hasPrefix("/api/music"):
            return ok()
        case ("GET", "/api/people"):
            return try out(["people": people] as [String: Any])
        case ("POST", "/api/people"):
            var person: [String: Any] = ["id": "p\(people.count + 1)"]
            person["name"] = json["name"] as? String ?? "Gast"
            person["role"] = json["role"] as? String ?? "guest"
            people.append(person)
            var r: [String: Any] = ["link": "liesenberghome://join?c=DEMO&n=Demo-Haus"]
            r["id"] = person["id"]
            r["expiresInHours"] = 48
            return try out(r)
        case ("DELETE", _) where p.hasPrefix("/api/people/"):
            let id = String(p.dropFirst("/api/people/".count))
            people.removeAll { ($0["id"] as? String) == id }
            return ok()
        case ("PUT", _) where p.hasPrefix("/api/people/"):
            let id = String(p.dropFirst("/api/people/".count))
            if let i = people.firstIndex(where: { ($0["id"] as? String) == id }), let role = json["role"] as? String {
                people[i]["role"] = role
            }
            return ok()
        default:
            throw ServerError(message: "Im Demo-Haus nicht verfügbar")
        }
    }

    private func out(_ obj: Any) throws -> Data { try JSONSerialization.data(withJSONObject: obj) }
    private func ok() -> Data { Data(#"{"ok":true}"#.utf8) }

    private static func hhmm() -> String {
        let f = DateFormatter(); f.dateFormat = "HH:mm"; return f.string(from: .now)
    }

    /// Sonnenkurve für heute – im Format von evcc /api/state
    private func energy() -> [String: Any] {
        let cal = Calendar.current
        let now = Date()
        func sun(_ d: Date) -> Double {
            let h = Double(cal.component(.hour, from: d)) + Double(cal.component(.minute, from: d)) / 60
            return max(0, sin(.pi * (h - 6.5) / 13)) * 7200
        }
        let start = cal.startOfDay(for: now)
        let series: [[Double]] = stride(from: 0, through: 24 * 3600, by: 900).map {
            let t = start.addingTimeInterval(Double($0))
            return [t.timeIntervalSince1970, sun(t)]
        }
        let pv = sun(now)
        let home = 650.0
        let battery = pv > home ? -min(pv - home, 3000) : min(home - pv, 2500)   // − laden / + entladen
        let grid = home - pv - battery

        let pvHouse: [String: Any] = ["title": "Haus", "power": pv * 0.7]
        let pvCarport: [String: Any] = ["title": "Carport", "power": pv * 0.3]
        let bat: [String: Any] = ["power": battery, "soc": 64.0, "capacity": 10.0]
        var lp: [String: Any] = ["title": "Garage", "vehicleTitle": "Elektroauto", "mode": "pv"]
        lp["chargePower"] = 0.0
        lp["vehicleSoc"] = 58.0
        lp["limitSoc"] = 80.0
        lp["connected"] = true
        lp["charging"] = false
        let today: [String: Any] = ["energy": 46000.0]
        let solar: [String: Any] = ["today": today, "timeseries": series]
        let forecast: [String: Any] = ["solar": solar]

        var s: [String: Any] = [:]
        s["pvPower"] = pv
        s["pv"] = [pvHouse, pvCarport]
        s["homePower"] = home
        s["grid"] = ["power": grid]
        s["battery"] = bat
        s["loadpoints"] = [lp]
        s["forecast"] = forecast
        return s
    }

    // MARK: - Demo-Daten

    private static let structureJSON = """
    {"name": "Demo-Haus", "backend": "demo", "floors": [
      {"id": "og", "name": "Obergeschoss", "rooms": [
        {"id": "schlafen", "name": "Schlafzimmer", "icon": "Bedroom", "devices": [
          {"id": "demo:sz-decke", "name": "Deckenlicht", "kind": "dimmer", "restricted": false},
          {"id": "demo:sz-lese", "name": "Leselampe", "kind": "light", "restricted": false},
          {"id": "demo:sz-fenster", "name": "Fenster", "kind": "blind", "restricted": false},
          {"id": "demo:sz-heizung", "name": "Heizung", "kind": "heating", "restricted": false}]},
        {"id": "bad", "name": "Bad", "icon": "Bathroom", "devices": [
          {"id": "demo:bad-spiegel", "name": "Spiegel", "kind": "dimmer", "restricted": false},
          {"id": "demo:bad-heizung", "name": "Fußboden", "kind": "heating", "restricted": false}]}]},
      {"id": "eg", "name": "Erdgeschoss", "rooms": [
        {"id": "wohnen", "name": "Wohnzimmer", "icon": "LivingRoom", "devices": [
          {"id": "demo:wz-spots", "name": "Spots", "kind": "dimmer", "restricted": false},
          {"id": "demo:wz-stehlampe", "name": "Stehlampe", "kind": "light", "restricted": false},
          {"id": "demo:wz-fenster", "name": "Terrasse", "kind": "blind", "restricted": false},
          {"id": "demo:wz-heizung", "name": "Heizung", "kind": "heating", "restricted": false}]},
        {"id": "kueche", "name": "Küche", "icon": "Kitchen", "devices": [
          {"id": "demo:k-insel", "name": "Kochinsel", "kind": "dimmer", "restricted": false},
          {"id": "demo:k-arbeit", "name": "Arbeitsplatte", "kind": "light", "restricted": false}]},
        {"id": "garage", "name": "Garage", "icon": "Garage", "devices": [
          {"id": "demo:g-tor", "name": "Garagentor", "kind": "gate", "restricted": true},
          {"id": "demo:g-licht", "name": "Licht", "kind": "light", "restricted": false}]},
        {"id": "garten", "name": "Garten", "icon": "Garden", "devices": [
          {"id": "demo:garten", "name": "Wegbeleuchtung", "kind": "light", "restricted": false}]}]}]}
    """

    private static let statesJSON = """
    {"demo:wz-spots": {"on": true, "level": 60}, "demo:k-insel": {"on": true, "level": 100},
     "demo:sz-decke": {"on": false, "level": 0}, "demo:bad-spiegel": {"on": false, "level": 0},
     "demo:wz-stehlampe": {"on": true}, "demo:sz-lese": {"on": false}, "demo:k-arbeit": {"on": false},
     "demo:g-licht": {"on": false}, "demo:garten": {"on": false},
     "demo:wz-fenster": {"position": 0}, "demo:sz-fenster": {"position": 100},
     "demo:wz-heizung": {"current": 21.9, "target": 21.5}, "demo:sz-heizung": {"current": 19.6, "target": 19.0},
     "demo:bad-heizung": {"current": 23.1, "target": 23.0}}
    """

    private static let scenesJSON = """
    [{"id": "abend", "name": "Abend", "icon": "moon.stars",
      "actions": [{"fid": "demo:wz-spots", "name": "Spots", "level": 30}, {"fid": "demo:wz-fenster", "name": "Terrasse", "position": 100}],
      "schedule": {"enabled": false, "days": [0, 1, 2, 3, 4, 5, 6]}},
     {"id": "garten", "name": "Garten", "icon": "tree",
      "actions": [{"fid": "demo:garten", "name": "Wegbeleuchtung", "on": true}],
      "schedule": {"enabled": true, "days": [0, 1, 2, 3, 4, 5, 6],
                   "start": {"type": "sunset", "offset": -15}, "stop": {"type": "time", "time": "23:00"}},
      "today": {"start": "18:25", "stop": "23:00"}}]
    """

    private static let peopleJSON = """
    [{"id": "p1", "name": "Du", "role": "owner"}, {"id": "p2", "name": "Partner", "role": "full"},
     {"id": "p3", "name": "Gast", "role": "guest"}]
    """

    private static let appliancesJSON = """
    {"configured": true, "loggedIn": true, "appliances": [
      {"id": "a1", "name": "Geschirrspüler", "brand": "Bosch", "typeText": "Geschirrspüler", "connected": true,
       "state": "Run", "stateText": "Läuft", "program": "Eco 50°", "remaining": 4980, "progress": 38},
      {"id": "a2", "name": "Waschmaschine", "brand": "Siemens", "typeText": "Waschmaschine", "connected": true,
       "state": "Finished", "stateText": "Fertig"}]}
    """

    private static let musicJSON = """
    {"speakers": [
      {"id": "s1", "name": "Küche", "model": "HomePod mini", "state": "spielt", "playing": true,
       "title": "Morgenradio", "artist": "Demo FM", "volume": 25, "online": true},
      {"id": "s2", "name": "Wohnzimmer", "model": "HomePod", "state": "bereit", "playing": false, "volume": 30, "online": true}],
     "schedules": [], "radio": ["Demo FM"], "error": null}
    """
}
