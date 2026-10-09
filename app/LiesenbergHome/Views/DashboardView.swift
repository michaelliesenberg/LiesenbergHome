import SwiftUI

struct DashboardView: View {
    @Environment(EnergyStore.self) private var energy
    @Environment(HomeStore.self) private var home
    @Environment(HouseClient.self) private var client
    @Environment(AppSettings.self) private var settings

    var body: some View {
        Screen(title: settings.homeName.isEmpty ? "Zuhause" : settings.homeName) {
            HStack {
                Text(Date.now, format: .dateTime.weekday(.wide).day().month(.wide))
                    .font(.subheadline).foregroundStyle(Theme.muted)
                Spacer()
                Label(client.connection.rawValue, systemImage: client.connection == .offline ? "wifi.slash" : (client.connection == .unauthorized ? "key" : "dot.radiowaves.left.and.right"))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(client.connection == .offline || client.connection == .unauthorized ? Theme.heat : Theme.muted)
            }

            HubUpdateBanner()

            NavigationLink { EnergyView() } label: {
                HStack(spacing: 0) {
                    stat("Solar", kw(energy.pv), Theme.solar)
                    stat("Haus", kw(energy.home), Theme.text)
                    stat("Akku", "\(Int(energy.batterySoc)) %", Theme.battery)
                    stat(energy.feedingIn ? "Einspeisung" : "Netz", kw(energy.grid), Theme.grid)
                }
                .card()
            }
            .buttonStyle(.plain)

            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                tile("Auto laden", "bolt.car", Theme.solar) { CarView() }
                tile("Klima", "air.conditioner.horizontal", Theme.grid) { ClimateView() }
                tile("Geräte", "washer", Theme.battery) { AppliancesView() }
                tile("Einstellungen", "gearshape", Theme.muted) { SettingsView() }
            }

            if home.structure != nil {
                ScenesSection().padding(.top, 6)
            } else if let err = home.lastError {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Noch keine Räume").font(.headline).foregroundStyle(Theme.solar)
                    Text(err).font(.footnote).foregroundStyle(Theme.muted)
                    if settings.isOwner {
                        Text("Auf der Einrichtungsseite des Hubs (Einstellungen → Hub einrichten) die Geräte-Quelle wählen.")
                            .font(.footnote).foregroundStyle(Theme.muted)
                    }
                }
                .card()
            }
        }
        .task(id: client.connection) {
            if home.structure == nil, client.connection == .local || client.connection == .remote || client.connection == .demo {
                await home.syncFromServer()
            }
        }
    }

    private func stat(_ label: String, _ value: String, _ color: Color) -> some View {
        VStack(spacing: 4) {
            Text(value).font(.system(size: 20, weight: .semibold, design: .rounded)).monospacedDigit().foregroundStyle(color)
            Text(label).font(.caption2.weight(.semibold)).foregroundStyle(Theme.muted)
        }
        .frame(maxWidth: .infinity)
    }

    private func tile<D: View>(_ title: String, _ icon: String, _ color: Color, @ViewBuilder dest: @escaping () -> D) -> some View {
        NavigationLink(destination: dest) {
            VStack(alignment: .leading, spacing: 18) {
                Image(systemName: icon).font(.title3).foregroundStyle(color)
                Text(title).font(.subheadline.weight(.bold)).foregroundStyle(Theme.text)
            }
            .card()
        }
        .buttonStyle(.plain)
    }
}
