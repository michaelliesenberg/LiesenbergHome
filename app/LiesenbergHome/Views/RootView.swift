import SwiftUI

struct RootView: View {
    @Environment(AppSettings.self) private var settings
    @Environment(EnergyStore.self) private var energy
    @Environment(HomeStore.self) private var home
    @Environment(AppLock.self) private var lock
    @Environment(\.scenePhase) private var phase
    /// Face ID nur einmal pro Rückkehr automatisch fragen (die Abfrage selbst macht die App kurz inaktiv)
    @State private var autoPrompt = true

    var body: some View {
        Group {
            if settings.isConnected {
                tabs
            } else {
                WelcomeView()
            }
        }
        .overlay {
            if lock.locked && settings.isConnected { LockScreen() }
        }
        .onChange(of: phase, initial: true) { _, p in
            switch p {
            case .active:
                if autoPrompt {
                    autoPrompt = false
                    lock.willEnterForeground()
                    if lock.locked { lock.unlock() }
                }
                guard settings.isConnected else { return }
                energy.start()
                Task {
                    await AppServices.shared.refreshMe()
                    await home.syncFromServer()
                }
            case .background:
                autoPrompt = true
                lock.didEnterBackground()
                energy.stop()
            default:
                break
            }
        }
        // Nach dem ersten Verbinden (Einladung/Demo) sofort Energie laden – nicht erst beim nächsten App-Wechsel
        .onChange(of: settings.isConnected) { _, connected in
            connected ? energy.start() : energy.stop()
        }
    }

    private var tabs: some View {
        TabView {
            NavigationStack { DashboardView() }
                .tabItem { Label("Home", systemImage: "house") }
            NavigationStack { EnergyView() }
                .tabItem { Label("Energie", systemImage: "bolt") }
            NavigationStack { RoomsView() }
                .tabItem { Label("Räume", systemImage: "square.grid.2x2") }
            NavigationStack { MusicView() }
                .tabItem { Label("Musik", systemImage: "music.note") }
        }
        .background(Theme.bg)
    }
}

/// Gesperrt (Face ID) – nichts vom Zuhause sichtbar, bis entsperrt
struct LockScreen: View {
    @Environment(AppLock.self) private var lock
    @Environment(AppSettings.self) private var settings

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: "lock.fill").font(.system(size: 40)).foregroundStyle(Theme.solar)
            Text(settings.homeName.isEmpty ? "Gesperrt" : settings.homeName).font(.title2.weight(.bold))
            Button("Entsperren") { lock.unlock() }
                .buttonStyle(.borderedProminent).tint(Theme.solar).foregroundStyle(Theme.bg)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg.ignoresSafeArea())
    }
}

/// Gemeinsamer dunkler Hintergrund für alle Screens.
struct Screen<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        ScrollView {
            // Auf Mac/iPad nicht in die Breite ziehen – Layout wie auf dem iPhone, mittig
            VStack(alignment: .leading, spacing: 14) { content }
                .frame(maxWidth: 520, alignment: .leading)
                .padding(.horizontal, 20)
                .padding(.bottom, 24)
                .frame(maxWidth: .infinity)
        }
        .scrollIndicators(.hidden)
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle(title)
        .toolbarBackground(Theme.bg, for: .navigationBar)
    }
}
