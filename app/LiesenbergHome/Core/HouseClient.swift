import Foundation
import Network
import Observation

/// Spricht mit dem Hub (Raspberry Pi).
/// Zuhause direkt, unterwegs über die öffentliche Adresse (Tailscale o. Ä.). Bei jedem Netzwechsel
/// werden beide Wege GLEICHZEITIG probiert – der schnellere gewinnt und wird gemerkt.
@Observable
final class HouseClient {
    enum Connection: String {
        case unknown = "…", local = "Zuhause", remote = "Unterwegs", offline = "Offline",
             unauthorized = "Zugang ungültig", demo = "Demo"
    }
    enum Route { case local, remote }

    private(set) var connection: Connection = .unknown
    private var preferred: Route? {
        didSet { UserDefaults.standard.set(preferred == .remote ? "remote" : "local", forKey: "lastRoute") }
    }
    private var onWiFi = true
    private let settings: AppSettings
    private let session: URLSession
    private let monitor = NWPathMonitor()

    init(settings: AppSettings) {
        self.settings = settings
        let cfg = URLSessionConfiguration.default
        cfg.waitsForConnectivity = false
        cfg.timeoutIntervalForRequest = 15
        session = URLSession(configuration: cfg)
        preferred = UserDefaults.standard.string(forKey: "lastRoute") == "remote" ? .remote : .local

        // Netzwechsel (WLAN ↔ Mobil, anderer Access Point) → sofort neu entscheiden
        monitor.pathUpdateHandler = { [weak self] path in
            guard let self else { return }
            self.onWiFi = path.usesInterfaceType(.wifi) || path.usesInterfaceType(.wiredEthernet)
            Task { await self.probe() }
        }
        monitor.start(queue: DispatchQueue(label: "net-monitor"))
    }

    // MARK: - API

    func energyState() async throws -> [String: Any] {
        let data = try await request("api/energy")
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
        return (obj["result"] as? [String: Any]) ?? obj
    }

    func get(_ path: String) async throws -> Data { try await request(path) }
    func send(_ path: String, method: String, body: Data) async throws -> Data {
        try await request(path, method: method, body: body)
    }

    // MARK: - Einladung

    /// Einladungslink (liesenberghome://join?c=…&n=…&l=…&r=…) einlösen: erst im Heimnetz, dann unterwegs.
    func join(link: URL) async throws -> (JoinResponse, local: String?, remote: String?) {
        guard let comps = URLComponents(url: link, resolvingAgainstBaseURL: false),
              let code = comps.queryItems?.first(where: { $0.name == "c" })?.value else {
            throw ServerError(message: "Das ist kein gültiger Einladungslink")
        }
        let q = { (n: String) in comps.queryItems?.first(where: { $0.name == n })?.value }
        let local = q("l"), remote = q("r")
        let body = try JSONSerialization.data(withJSONObject: ["code": code, "device": "iPhone"])
        var lastError: Error = ServerError(message: "Hub nicht erreichbar – bist du im selben WLAN?")
        for base in [local, remote].compactMap({ $0 }).filter({ !$0.isEmpty }) {
            guard let url = URL(string: base.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/api/join") else { continue }
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.httpBody = body
            req.timeoutInterval = base == local ? 4 : 10
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            do {
                let (data, resp) = try await session.data(for: req)
                let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
                if status == 200 { return (try JSONDecoder().decode(JoinResponse.self, from: data), local, remote) }
                let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
                throw ServerError(message: detail ?? "HTTP \(status)")
            } catch let e as ServerError {
                throw e                       // Hub erreicht, Einladung abgelehnt → nicht weiter probieren
            } catch {
                lastError = error
            }
        }
        throw lastError
    }

    /// Bei Wechsel des Zuhauses (oder Demo) neu entscheiden
    func reset() {
        preferred = nil
        connection = .unknown
        Task { await probe() }
    }

    // MARK: - Wegwahl

    private func base(_ r: Route) -> String {
        (r == .local ? settings.localURL : settings.remoteURL).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }

    /// Beide Wege gleichzeitig anfragen (kleiner Health-Check) – der erste gültige gewinnt.
    @discardableResult
    func probe() async -> Route? {
        if settings.demo { await MainActor.run { connection = .demo }; return .local }
        var routes: [Route] = [.remote]
        if onWiFi { routes.insert(.local, at: 0) }   // im Mobilfunk ist der Pi direkt nie erreichbar
        let winner: Route? = await withTaskGroup(of: Route?.self) { group in
            for r in routes where !base(r).isEmpty {
                group.addTask { [self] in
                    var req = self.makeRequest(r, path: "api/health", method: "GET", body: nil, contentType: "")
                    req.timeoutInterval = r == .local ? 1.5 : 5
                    guard let result = try? await self.session.data(for: req),
                          (result.1 as? HTTPURLResponse)?.statusCode == 200 else { return nil }
                    return r
                }
            }
            for await r in group { if let r { group.cancelAll(); return r } }
            return nil
        }
        await MainActor.run {
            if let winner {
                preferred = winner
                if connection != .unauthorized { connection = winner == .local ? .local : .remote }
            } else {
                connection = .offline
            }
        }
        return winner
    }

    private func makeRequest(_ r: Route, path: String, method: String, body: Data?, contentType: String) -> URLRequest {
        var req = URLRequest(url: URL(string: base(r) + "/" + path) ?? URL(string: "http://invalid.local")!)
        req.httpMethod = method
        req.httpBody = body
        if body != nil { req.setValue(contentType, forHTTPHeaderField: "Content-Type") }
        let key = settings.key
        if !key.isEmpty { req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization") }
        return req
    }

    private func request(_ path: String, method: String = "GET", body: Data? = nil,
                         contentType: String = "application/json") async throws -> Data {
        if settings.demo {
            await MainActor.run { connection = .demo }
            return try await DemoHub.shared.handle(path: path, method: method, body: body)
        }
        // 1. zuletzt erfolgreicher Weg  2. falls der scheitert: beide parallel prüfen und den Sieger nehmen
        var route = preferred ?? .local
        if route == .local && !onWiFi { route = .remote }
        for attempt in 0..<2 {
            var req = makeRequest(route, path: path, method: method, body: body, contentType: contentType)
            // LUXOR liest Datenpunkte nacheinander, HomePods brauchen etwas – großzügig, der Health-Check bleibt kurz
            req.timeoutInterval = route == .local ? 20 : 25
            do {
                let (data, resp) = try await session.data(for: req)
                guard let http = resp as? HTTPURLResponse else { throw URLError(.badServerResponse) }
                if http.statusCode == 401 {
                    await MainActor.run { connection = .unauthorized }
                    throw ServerError(message: "Dein Zugang gilt nicht mehr – bitte neu einladen lassen")
                }
                await MainActor.run {
                    preferred = route
                    connection = route == .local ? .local : .remote
                }
                guard (200..<300).contains(http.statusCode) else {
                    // Server erreicht, aber Fehler → nicht über den anderen Weg wiederholen
                    let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
                    throw ServerError(message: detail ?? "HTTP \(http.statusCode)")
                }
                return data
            } catch let e as ServerError {
                throw e
            } catch {
                guard attempt == 0, let next = await probe(), next != route || method == "GET" else { throw error }
                route = next
            }
        }
        throw URLError(.cannotConnectToHost)
    }
}

/// Der Haus-Server hat geantwortet, aber einen Fehler gemeldet.
struct ServerError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}
