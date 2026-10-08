import SwiftUI

@main
struct LiesenbergHomeApp: App {
    private let s = AppServices.shared

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(s.settings)
                .environment(s.client)
                .environment(s.home)
                .environment(s.energy)
                .environment(s.climate)
                .environment(s.music)
                .environment(s.scenes)
                .environment(s.lock)
                .preferredColorScheme(.dark)
                .tint(Theme.solar)
                // Einladung: liesenberghome://join?… (QR-Code mit der Kamera-App oder Link aus einer Nachricht)
                .onOpenURL { url in
                    guard url.scheme == "liesenberghome", url.host == "join" else { return }
                    Task {
                        do { try await s.join(url) }
                        catch { s.home.lastError = "Einladung: \(error.localizedDescription)" }
                    }
                }
        }
    }
}
