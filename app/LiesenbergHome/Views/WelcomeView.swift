import SwiftUI
import VisionKit
import Network

/// Erster Start: mit einem Zuhause verbinden (Einladung scannen / Link) – oder die Demo ansehen.
struct WelcomeView: View {
    @Environment(AppSettings.self) private var settings
    @State private var scanning = false
    @State private var busy = false
    @State private var error: String?
    @State private var finder = HubFinder()
    @Environment(\.openURL) private var openURL

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Image(systemName: "house.fill").font(.system(size: 44)).foregroundStyle(Theme.solar).padding(.top, 40)
                Text("Willkommen").font(.system(size: 34, weight: .bold))
                Text("Verbinde die App mit deinem Zuhause. Du brauchst dafür eine Einladung – als QR-Code oder Link vom Besitzer des Hubs.")
                    .foregroundStyle(Theme.muted)

                if DataScannerViewController.isSupported {
                    big("Einladung scannen", "qrcode.viewfinder", Theme.solar) { scanning = true }
                }
                big("Einladungslink einfügen", "doc.on.clipboard", Theme.grid) { pasteLink() }

                if busy { ProgressView("Verbinde …").tint(Theme.solar) }
                if let error { Label(error, systemImage: "exclamationmark.triangle").font(.footnote).foregroundStyle(Theme.heat) }

                SectionLabel(text: "Eigenen Hub einrichten").padding(.top, 14)
                VStack(alignment: .leading, spacing: 10) {
                    if finder.hubs.isEmpty {
                        HStack(spacing: 10) {
                            ProgressView().tint(Theme.muted)
                            Text("Suche Hub im WLAN …").font(.subheadline).foregroundStyle(Theme.muted)
                        }
                    }
                    ForEach(finder.hubs) { hub in
                        Button { if let u = hub.setupURL { openURL(u) } } label: {
                            HStack {
                                Image(systemName: "server.rack").foregroundStyle(Theme.battery)
                                VStack(alignment: .leading) {
                                    Text(hub.name).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                                    Text(hub.address ?? "…").font(.caption).foregroundStyle(Theme.muted)
                                }
                                Spacer()
                                Text("Einrichten").font(.caption.weight(.bold)).foregroundStyle(Theme.solar)
                            }
                        }
                        .buttonStyle(.plain)
                        // Ältere App-Version: Schlüssel ist schon im Schlüsselbund → direkt verbinden
                        if !settings.key.isEmpty, let addr = hub.address {
                            Button { reconnect("http://\(addr)") } label: {
                                Label("Mit diesem iPhone verbinden", systemImage: "link").font(.subheadline.weight(.semibold))
                            }
                            .tint(Theme.solar).disabled(busy)
                        }
                    }
                    Text("Noch keinen Hub? Er läuft auf einem Raspberry Pi in deinem Netzwerk und verbindet LUXORliving oder Home Assistant, evcc, Daikin, Home Connect und HomePods. Auf der Einrichtungsseite des Hubs bekommst du deinen QR-Code.")
                        .font(.footnote).foregroundStyle(Theme.muted)
                    Link("Anleitung: Hub installieren", destination: URL(string: "https://github.com/michaelliesenberg/LiesenbergHome#readme")!)
                        .font(.footnote.weight(.semibold)).tint(Theme.solar)
                }
                .card()

                Button {
                    settings.startDemo()
                    AppServices.shared.didConnect()
                } label: {
                    Label("Demo ansehen", systemImage: "play.circle").font(.subheadline.weight(.semibold))
                }
                .tint(Theme.muted)
                .padding(.top, 6)
            }
            .frame(maxWidth: 520, alignment: .leading)
            .padding(.horizontal, 24)
            .frame(maxWidth: .infinity)
        }
        .background(Theme.bg.ignoresSafeArea())
        .sheet(isPresented: $scanning) {
            QRScanner { code in
                scanning = false
                if let url = URL(string: code) { join(url) } else { error = "Das ist kein Einladungs-Code." }
            }
            .ignoresSafeArea()
        }
        .onAppear { finder.start() }
        .onDisappear { finder.stop() }
    }

    private func big(_ title: String, _ icon: String, _ color: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Image(systemName: icon).font(.title2).foregroundStyle(color).frame(width: 34)
                Text(title).font(.headline).foregroundStyle(Theme.text)
                Spacer()
                Image(systemName: "chevron.right").foregroundStyle(Theme.faint)
            }
            .card(padding: 18)
        }
        .buttonStyle(.plain)
        .disabled(busy)
    }

    private func pasteLink() {
        let text = UIPasteboard.general.url?.absoluteString ?? UIPasteboard.general.string ?? ""
        guard let url = URL(string: text.trimmingCharacters(in: .whitespacesAndNewlines)), url.scheme == "liesenberghome" else {
            error = "In der Zwischenablage ist kein Einladungslink. Bitte den Link aus der Nachricht kopieren."
            return
        }
        join(url)
    }

    private func reconnect(_ local: String) {
        busy = true; error = nil
        Task {
            settings.localURL = local
            await AppServices.shared.refreshMe()
            if settings.personID.isEmpty {
                settings.localURL = ""
                error = "Der gespeicherte Schlüssel passt nicht – bitte eine Einladung scannen."
            } else {
                AppServices.shared.didConnect()
            }
            busy = false
        }
    }

    private func join(_ url: URL) {
        busy = true; error = nil
        Task {
            do { try await AppServices.shared.join(url) }
            catch { self.error = error.localizedDescription }
            busy = false
        }
    }
}

// MARK: - QR-Scanner (VisionKit)

struct QRScanner: UIViewControllerRepresentable {
    let found: (String) -> Void

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let vc = DataScannerViewController(recognizedDataTypes: [.barcode(symbologies: [.qr])],
                                           qualityLevel: .fast, isHighlightingEnabled: true)
        vc.delegate = context.coordinator
        return vc
    }
    func updateUIViewController(_ vc: DataScannerViewController, context: Context) {
        // erst starten, wenn die Ansicht auf dem Bildschirm ist
        if !vc.isScanning { try? vc.startScanning() }
    }
    func makeCoordinator() -> Coordinator { Coordinator(found: found) }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let found: (String) -> Void
        private var done = false
        init(found: @escaping (String) -> Void) { self.found = found }

        func dataScanner(_ s: DataScannerViewController, didAdd items: [RecognizedItem], allItems: [RecognizedItem]) {
            for case .barcode(let b) in items {
                guard !done, let v = b.payloadStringValue, v.hasPrefix("liesenberghome://") else { continue }
                done = true
                s.stopScanning()
                found(v)
            }
        }
    }
}

// MARK: - Hub im WLAN finden (Bonjour: _liesenberghome._tcp)

@MainActor
@Observable
final class HubFinder {
    struct Hub: Identifiable {
        let id: String
        let name: String
        var address: String?
        var setupURL: URL? { address.flatMap { URL(string: "http://\($0)/setup") } }
    }

    private(set) var hubs: [Hub] = []
    private var browser: NWBrowser?
    private var resolvers: [NWConnection] = []

    func start() {
        guard browser == nil else { return }
        let b = NWBrowser(for: .bonjour(type: "_liesenberghome._tcp", domain: nil), using: .tcp)
        b.browseResultsChangedHandler = { [weak self] results, _ in
            Task { @MainActor in self?.update(results) }
        }
        b.start(queue: .main)
        browser = b
    }

    func stop() {
        browser?.cancel(); browser = nil
        resolvers.forEach { $0.cancel() }; resolvers = []
    }

    private func update(_ results: Set<NWBrowser.Result>) {
        for r in results {
            guard case let .service(name, _, _, _) = r.endpoint, !hubs.contains(where: { $0.id == name }) else { continue }
            hubs.append(Hub(id: name, name: name.replacingOccurrences(of: "Liesenberg Home Hub ", with: "Hub ")))
            resolve(r.endpoint, id: name)
        }
    }

    /// Adresse herausfinden: kurz verbinden und die IPv4-Adresse ablesen
    private func resolve(_ endpoint: NWEndpoint, id: String) {
        let params = NWParameters.tcp
        if let ip = params.defaultProtocolStack.internetProtocol as? NWProtocolIP.Options { ip.version = .v4 }
        let c = NWConnection(to: endpoint, using: params)
        c.stateUpdateHandler = { [weak self] state in
            guard case .ready = state, case let .hostPort(host, port)? = c.currentPath?.remoteEndpoint else { return }
            let h = "\(host)".components(separatedBy: "%").first ?? "\(host)"
            Task { @MainActor in
                if let i = self?.hubs.firstIndex(where: { $0.id == id }) { self?.hubs[i].address = "\(h):\(port)" }
                c.cancel()
            }
        }
        c.start(queue: .main)
        resolvers.append(c)
    }
}
