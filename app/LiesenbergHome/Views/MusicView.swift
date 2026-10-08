import SwiftUI
import MusicKit
import AVKit

/// Apple Music mit deinem Account (MusicKit) + Ausgabe auf HomePods per AirPlay.
struct MusicView: View {
    @State private var auth = MusicAuthorization.currentStatus
    @State private var playlists: MusicItemCollection<Playlist> = []
    @State private var recent: MusicItemCollection<RecentlyPlayedMusicItem> = []
    @State private var error: String?
    @ObservedObject private var state = ApplicationMusicPlayer.shared.state
    @ObservedObject private var queue = ApplicationMusicPlayer.shared.queue

    private let player = ApplicationMusicPlayer.shared

    var body: some View {
        Screen(title: "Musik") {
            switch auth {
            case .authorized:
                nowPlaying
                SectionLabel(text: "Lautsprecher")
                speakers
                if !playlists.isEmpty {
                    SectionLabel(text: "Deine Playlists")
                    grid(Array(playlists.prefix(9)).map { p in Tile(id: p.id.rawValue, title: p.name, artwork: p.artwork) { await play(p) } })
                }
                if !recent.isEmpty {
                    SectionLabel(text: "Zuletzt gehört")
                    grid(Array(recent.prefix(6)).map { item in Tile(id: item.id.rawValue, title: item.title, artwork: item.artwork) { await play(item) } })
                }
            case .notDetermined:
                connectCard
            default:
                Text("Zugriff auf Apple Music ist in den Einstellungen deaktiviert (Einstellungen → Datenschutz → Medien & Apple Music).")
                    .font(.footnote).foregroundStyle(Theme.muted).card()
            }
            if let error { Text(error).font(.footnote).foregroundStyle(Theme.heat) }

            // Alle HomePods im Haus (über den Pi) + Zeitpläne
            SpeakersSection().padding(.top, 8)
        }
        .task(id: "\(auth)") { if case .authorized = auth { await loadLibrary() } }
    }

    // MARK: Teile

    private var connectCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Image(systemName: "music.note").font(.largeTitle).foregroundStyle(Theme.grid)
            Text("Mit Apple Music verbinden").font(.headline)
            Text("Deine Playlists und Musik direkt hier abspielen – auf dem iPhone oder auf den HomePods.")
                .font(.footnote).foregroundStyle(Theme.muted)
            Button("Verbinden") { Task { auth = await MusicAuthorization.request() } }
                .buttonStyle(.borderedProminent).tint(Theme.grid).foregroundStyle(Theme.bg)
        }
        .card(padding: 18)
    }

    private var nowPlaying: some View {
        let entry = queue.currentEntry
        return VStack(spacing: 14) {
            HStack(spacing: 14) {
                Group {
                    if let art = entry?.artwork { ArtworkImage(art, width: 84, height: 84) }
                    else { Image(systemName: "music.note").font(.title).foregroundStyle(Theme.grid) }
                }
                .frame(width: 84, height: 84)
                .background(Color(hex: 0x2B3A55))
                .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                VStack(alignment: .leading, spacing: 3) {
                    Text(entry?.title ?? "Nichts läuft").font(.headline).lineLimit(1)
                    Text(entry?.subtitle ?? "Wähle unten eine Playlist").font(.footnote).foregroundStyle(Theme.muted).lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            HStack(spacing: 34) {
                button("backward.fill", 22) { Task { try? await player.skipToPreviousEntry() } }
                Button {
                    Task {
                        if state.playbackStatus == .playing { player.pause() } else { try? await player.play() }
                    }
                } label: {
                    Image(systemName: state.playbackStatus == .playing ? "pause.fill" : "play.fill")
                        .font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.bg)
                        .frame(width: 60, height: 60).background(Theme.text, in: Circle())
                }
                .buttonStyle(.plain)
                button("forward.fill", 22) { Task { try? await player.skipToNextEntry() } }
            }
        }
        .card(padding: 16)
    }

    /// AirPlay-Auswahl (HomePods, Gruppen). Die Lautstärke regelt dann der HomePod.
    private var speakers: some View {
        HStack(spacing: 12) {
            AirPlayButton().frame(width: 44, height: 44)
            VStack(alignment: .leading, spacing: 2) {
                Text("HomePods auswählen").font(.subheadline.weight(.semibold))
                Text("Mehrere antippen, um gemeinsam abzuspielen").font(.caption).foregroundStyle(Theme.muted)
            }
            Spacer()
        }
        .card(padding: 12)
    }

    private func grid(_ items: [Tile]) -> some View {
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 10), count: 3), spacing: 12) {
            ForEach(items) { item in
                Button { Task { await item.action() } } label: {
                    VStack(alignment: .leading, spacing: 6) {
                        Group {
                            if let art = item.artwork { ArtworkImage(art, width: 110, height: 110) }
                            else { Color(hex: 0x1D2A3F) }
                        }
                        .frame(maxWidth: .infinity).aspectRatio(1, contentMode: .fit)
                        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                        Text(item.title).font(.caption.weight(.semibold)).lineLimit(1)
                    }
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func button(_ icon: String, _ size: CGFloat, _ action: @escaping () -> Void) -> some View {
        Button(action: action) { Image(systemName: icon).font(.system(size: size)).frame(width: 44, height: 44) }
            .buttonStyle(.plain)
    }

    // MARK: Daten

    private func loadLibrary() async {
        do {
            playlists = try await MusicLibraryRequest<Playlist>().response().items
            recent = try await MusicRecentlyPlayedContainerRequest().response().items
        } catch {
            self.error = "Apple Music: \(error.localizedDescription)"
        }
    }

    private func play(_ playlist: Playlist) async {
        player.queue = [playlist]
        do { try await player.play() } catch { self.error = error.localizedDescription }
    }

    private func play(_ item: RecentlyPlayedMusicItem) async {
        switch item {
        case .album(let a): player.queue = [a]
        case .playlist(let p): player.queue = [p]
        case .station(let s): player.queue = [s]
        @unknown default: return
        }
        do { try await player.play() } catch { self.error = error.localizedDescription }
    }
}

struct Tile: Identifiable {
    let id: String
    let title: String
    let artwork: Artwork?
    let action: () async -> Void
}

struct AirPlayButton: UIViewRepresentable {
    func makeUIView(context: Context) -> AVRoutePickerView {
        let v = AVRoutePickerView()
        v.tintColor = UIColor(Theme.muted)
        v.activeTintColor = UIColor(Theme.grid)
        v.prioritizesVideoDevices = false
        return v
    }
    func updateUIView(_ uiView: AVRoutePickerView, context: Context) {}
}
