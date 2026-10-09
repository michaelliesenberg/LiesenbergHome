import Foundation
import CoreLocation
import UserNotifications
import AVFoundation
import UIKit
import Observation

/// Einstellungen für Ankommen & Wegfahren (nur auf diesem iPhone gespeichert).
struct ArrivalConfig: Codable, Equatable {
    var enabled = false
    var lat: Double?
    var lon: Double?
    /// „Angekommen", sobald man näher als das ist
    var nearRadius: Double = 200
    /// Erst scharf, wenn man vorher weiter weg war (verhindert Auslösen beim Spazierengehen ums Haus)
    var farRadius: Double = 1000
    var gateID: String?
    var arrivalSceneID: String?
    /// Ohne Nachfrage öffnen – aber nur, wenn das iPhone gerade mit CarPlay verbunden ist
    var autoInCar = false
    /// Beim Wegfahren fragen „Alles aus?"
    var leaveNotify = false
    var leaveSceneID: String?

    var hasHome: Bool { lat != nil && lon != nil }
    var center: CLLocationCoordinate2D? {
        guard let lat, let lon else { return nil }
        return CLLocationCoordinate2D(latitude: lat, longitude: lon)
    }
}

/// Erkennt Ankommen/Wegfahren über iOS-Regionen (funktioniert auch bei geschlossener App)
/// und bietet per Mitteilung an, das Tor zu öffnen bzw. Szenen zu starten.
@MainActor
@Observable
final class ArrivalManager: NSObject {
    private(set) var config: ArrivalConfig
    private(set) var authorization: CLAuthorizationStatus
    private(set) var notificationsAllowed = false
    private(set) var lastEvent: String?
    /// Die letzten Ereignisse (neueste zuerst) – zum Nachvollziehen, was iOS gemeldet hat
    private(set) var events: [String] = []
    private(set) var locating = false
    /// „Genauer Standort" aus → iOS liefert keine Bereichs-Meldungen
    private(set) var preciseLocationOff = false

    private let manager = CLLocationManager()
    private let d = UserDefaults.standard
    private static let nearID = "home.near", farID = "home.far"
    private static let catArrive = "ARRIVAL", catLeave = "LEAVE"
    private static let actGate = "OPEN_GATE", actScene = "RUN_SCENE", actOff = "ALL_OFF", actLeaveScene = "LEAVE_SCENE"

    /// War man seit dem letzten Ankommen weiter als farRadius weg?
    private var armed: Bool {
        get { d.bool(forKey: "arrival.armed") }
        set { d.set(newValue, forKey: "arrival.armed") }
    }
    /// Wann man den Nahbereich zuletzt verlassen hat (zweiter Weg zum Scharfschalten)
    private var leftNearAt: Date? {
        get { d.object(forKey: "arrival.leftNearAt") as? Date }
        set { d.set(newValue, forKey: "arrival.leftNearAt") }
    }
    private var lastArrival: Date? {
        get { d.object(forKey: "arrival.lastArrival") as? Date }
        set { d.set(newValue, forKey: "arrival.lastArrival") }
    }
    /// Mindestens so lange aus dem Nahbereich weg, damit Heimkommen zählt (auch ohne 1-km-Meldung)
    private static let minAway: TimeInterval = 10 * 60

    override init() {
        config = (UserDefaults.standard.data(forKey: "arrival.config"))
            .flatMap { try? JSONDecoder().decode(ArrivalConfig.self, from: $0) } ?? ArrivalConfig()
        authorization = CLLocationManager().authorizationStatus
        lastEvent = UserDefaults.standard.string(forKey: "arrival.lastEvent")
        events = UserDefaults.standard.stringArray(forKey: "arrival.events") ?? []
        super.init()
        preciseLocationOff = manager.accuracyAuthorization == .reducedAccuracy
        manager.delegate = self
        UNUserNotificationCenter.current().delegate = self
        registerCategories()
        Task { await refreshNotificationStatus() }
        if config.enabled { startMonitoring() }
    }

    // MARK: - Einstellungen

    func update(_ change: (inout ArrivalConfig) -> Void) {
        var c = config
        change(&c)
        guard c != config else { return }
        config = c
        d.set(try? JSONEncoder().encode(c), forKey: "arrival.config")
        if c.enabled && c.hasHome { startMonitoring() } else { stopMonitoring() }
    }

    func setEnabled(_ on: Bool) {
        if on {
            manager.requestAlwaysAuthorization()
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { ok, _ in
                Task { @MainActor in self.notificationsAllowed = ok }
            }
        }
        update { $0.enabled = on }
    }

    /// Aktuellen Standort als „Zuhause" übernehmen (am besten vor der Haustür)
    func useCurrentLocationAsHome() {
        locating = true
        if authorization == .notDetermined { manager.requestAlwaysAuthorization() }
        manager.desiredAccuracy = kCLLocationAccuracyBest
        manager.requestLocation()
    }

    func refreshNotificationStatus() async {
        let s = await UNUserNotificationCenter.current().notificationSettings()
        notificationsAllowed = s.authorizationStatus == .authorized || s.authorizationStatus == .provisional
    }

    // MARK: - Regionen

    private func startMonitoring() {
        stopMonitoring()
        guard let c = config.center, CLLocationManager.isMonitoringAvailable(for: CLCircularRegion.self) else { return }
        let maxR = manager.maximumRegionMonitoringDistance
        let near = CLCircularRegion(center: c, radius: min(config.nearRadius, maxR), identifier: Self.nearID)
        let far = CLCircularRegion(center: c, radius: min(config.farRadius, maxR), identifier: Self.farID)
        for r in [near, far] { r.notifyOnEntry = true; r.notifyOnExit = true; manager.startMonitoring(for: r) }
        manager.requestState(for: far)          // schon unterwegs? → gleich scharf schalten
        manager.requestState(for: near)
        record("Überwache Zuhause (\(Int(config.nearRadius)) m / \(Int(config.farRadius)) m)")
    }

    private func stopMonitoring() {
        for r in manager.monitoredRegions where [Self.nearID, Self.farID].contains(r.identifier) {
            manager.stopMonitoring(for: r)
        }
    }

    private func handle(regionID: String, entered: Bool) {
        switch (regionID, entered) {
        case (Self.farID, false):                       // mehr als 1 km weg → beim Zurückkommen auslösen
            armed = true
            record("iOS: weiter als \(Int(config.farRadius)) m weg → scharf")
        case (Self.farID, true):
            record("iOS: wieder näher als \(Int(config.farRadius)) m")
        case (Self.nearID, true):
            // Scharf über die 1-km-Meldung ODER weil man mindestens 10 Min. aus dem Nahbereich weg war
            let awayLongEnough = leftNearAt.map { Date().timeIntervalSince($0) >= Self.minAway } ?? false
            let recently = lastArrival.map { Date().timeIntervalSince($0) < 5 * 60 } ?? false
            record("iOS: näher als \(Int(config.nearRadius)) m" + (armed ? " (scharf)" : awayLongEnough ? " (war > 10 Min. weg)" : " – nicht scharf"))
            if (armed || awayLongEnough) && !recently {
                armed = false
                lastArrival = Date()
                arrive()
            }
        case (Self.nearID, false):
            leftNearAt = Date()
            record("iOS: Nahbereich verlassen")
            if config.leaveNotify || config.leaveSceneID != nil { leave() }
        default:
            break
        }
    }

    // MARK: - Ankommen / Wegfahren

    private func arrive() {
        let services = AppServices.shared
        let gate = config.gateID.flatMap { services.home.device($0) }
        let canGate = gate != nil && services.settings.permissions.restricted
        if config.autoInCar && Self.inCar() {
            record("Angekommen (CarPlay) – automatisch")
            runInBackground {
                if canGate, let gate { try? await services.home.trigger(gate) }
                await self.runScene(self.config.arrivalSceneID)
            }
            var done: [String] = []
            if canGate, let gate { done.append("\(gate.name) geöffnet") }
            if let n = sceneName(config.arrivalSceneID) { done.append("„\(n)“ gestartet") }
            notify(Self.catArrive, title: "Willkommen zu Hause", body: done.joined(separator: " · "), actions: false)
            return
        }
        record("Angekommen – Mitteilung")
        var parts: [String] = []
        if canGate, let gate { parts.append("\(gate.name) öffnen?") }
        if let n = sceneName(config.arrivalSceneID) { parts.append("Szene „\(n)“ starten?") }
        guard !parts.isEmpty else { record("Angekommen – aber kein Tor und keine Szene gewählt"); return }
        notify(Self.catArrive, title: "Willkommen zu Hause", body: parts.joined(separator: " "), actions: true)
    }

    private func leave() {
        record("Weggefahren")
        if config.leaveNotify {
            let body = sceneName(config.leaveSceneID).map { "Szene „\($0)“ starten oder alle Lichter aus?" } ?? "Alle Lichter ausschalten?"
            notify(Self.catLeave, title: "Unterwegs", body: body, actions: true)
        } else if config.leaveSceneID != nil {
            runInBackground { await self.runScene(self.config.leaveSceneID) }
        }
    }

    private func runScene(_ id: String?) async {
        guard let id else { return }
        let store = AppServices.shared.scenes
        if store.scenes.isEmpty { await store.refresh() }
        if let s = store.scenes.first(where: { $0.id == id }) { _ = await store.run(s) }
    }

    private func sceneName(_ id: String?) -> String? {
        guard let id else { return nil }
        return AppServices.shared.scenes.scenes.first { $0.id == id }?.name ?? "Szene"
    }

    /// CarPlay verbunden? (Auto-Bluetooth allein reicht nicht – das wären auch Kopfhörer)
    static func inCar() -> Bool {
        AVAudioSession.sharedInstance().currentRoute.outputs.contains { $0.portType == .carAudio }
    }

    /// iOS weckt die App bei Regionen nur kurz – Netzwerk-Befehle als Hintergrundaufgabe zu Ende bringen
    private func runInBackground(_ work: @escaping @MainActor () async -> Void) {
        var id = UIBackgroundTaskIdentifier.invalid
        id = UIApplication.shared.beginBackgroundTask(withName: "arrival") { UIApplication.shared.endBackgroundTask(id) }
        Task { @MainActor in
            await work()
            UIApplication.shared.endBackgroundTask(id)
        }
    }

    private func record(_ s: String) {
        let text = "\(Date().formatted(date: .abbreviated, time: .shortened)): \(s)"
        lastEvent = text
        d.set(text, forKey: "arrival.lastEvent")
        events = Array(([text] + events).prefix(15))
        d.set(events, forKey: "arrival.events")
    }

    /// Zum Ausprobieren: tut so, als wärst du gerade angekommen (Mitteilung bzw. CarPlay-Automatik)
    func testArrival() {
        record("Test: Ankommen ausgelöst")
        arrive()
    }

    // MARK: - Mitteilungen

    private func registerCategories() {
        // Tor nur bei entsperrtem iPhone (.authenticationRequired) – wie bei Siri
        let gate = UNNotificationAction(identifier: Self.actGate, title: "Tor öffnen", options: [.authenticationRequired])
        let scene = UNNotificationAction(identifier: Self.actScene, title: "Szene starten", options: [])
        let off = UNNotificationAction(identifier: Self.actOff, title: "Alle Lichter aus", options: [])
        let leaveScene = UNNotificationAction(identifier: Self.actLeaveScene, title: "Szene starten", options: [])
        UNUserNotificationCenter.current().setNotificationCategories([
            UNNotificationCategory(identifier: Self.catArrive, actions: [gate, scene], intentIdentifiers: []),
            UNNotificationCategory(identifier: Self.catLeave, actions: [off, leaveScene], intentIdentifiers: []),
        ])
    }

    private func notify(_ category: String, title: String, body: String, actions: Bool) {
        let c = UNMutableNotificationContent()
        c.title = title
        c.body = body
        c.sound = .default
        if actions { c.categoryIdentifier = category }
        c.interruptionLevel = .active
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: category, content: c, trigger: nil))
    }

    fileprivate func perform(action: String) async {
        let services = AppServices.shared
        switch action {
        case Self.actGate:
            if let id = config.gateID, let gate = services.home.device(id), services.settings.permissions.restricted {
                try? await services.home.trigger(gate)
                record("\(gate.name) per Mitteilung geöffnet")
            }
        case Self.actScene:
            await runScene(config.arrivalSceneID)
        case Self.actOff:
            _ = await services.scenes.centralOff()
            record("Alle Lichter aus (Mitteilung)")
        case Self.actLeaveScene:
            await runScene(config.leaveSceneID)
        default:
            break
        }
    }
}

// MARK: - Standort-Ereignisse

extension ArrivalManager: CLLocationManagerDelegate {
    nonisolated func locationManagerDidChangeAuthorization(_ m: CLLocationManager) {
        let s = m.authorizationStatus
        let reduced = m.accuracyAuthorization == .reducedAccuracy
        Task { @MainActor in self.authorization = s; self.preciseLocationOff = reduced }
    }

    nonisolated func locationManager(_ m: CLLocationManager, didEnterRegion region: CLRegion) {
        let id = region.identifier
        Task { @MainActor in self.handle(regionID: id, entered: true) }
    }

    nonisolated func locationManager(_ m: CLLocationManager, didExitRegion region: CLRegion) {
        let id = region.identifier
        Task { @MainActor in self.handle(regionID: id, entered: false) }
    }

    nonisolated func locationManager(_ m: CLLocationManager, didDetermineState state: CLRegionState, for region: CLRegion) {
        let id = region.identifier
        Task { @MainActor in
            // Beim Einschalten schon unterwegs → scharf, damit das erste Heimkommen zählt
            if id == Self.farID, state == .outside { self.armed = true; self.record("Start: unterwegs → scharf") }
            if id == Self.nearID, state == .outside, self.leftNearAt == nil { self.leftNearAt = Date() }
        }
    }

    nonisolated func locationManager(_ m: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let loc = locations.last else { return }
        Task { @MainActor in
            self.locating = false
            self.update { $0.lat = loc.coordinate.latitude; $0.lon = loc.coordinate.longitude }
            self.record("Zuhause festgelegt (± \(Int(loc.horizontalAccuracy)) m)")
        }
    }

    nonisolated func locationManager(_ m: CLLocationManager, didFailWithError error: Error) {
        Task { @MainActor in self.locating = false }
    }

    nonisolated func locationManager(_ m: CLLocationManager, monitoringDidFailFor region: CLRegion?, withError error: Error) {
        let msg = error.localizedDescription
        Task { @MainActor in self.record("Fehler bei der Überwachung: \(msg)") }
    }
}

// MARK: - Antworten auf Mitteilungen

extension ArrivalManager: UNUserNotificationCenterDelegate {
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let action = response.actionIdentifier
        Task { @MainActor in
            await self.perform(action: action)
            completionHandler()
        }
    }
}
