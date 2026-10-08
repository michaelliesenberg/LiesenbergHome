import SwiftUI
import CoreImage.CIFilterBuiltins

/// Personen & Einladungen (nur Besitzer): einladen per QR-Code/Link, Rolle ändern, entfernen.
struct PeopleView: View {
    @Environment(HouseClient.self) private var client
    @Environment(AppSettings.self) private var settings
    @State private var people: [Person] = []
    @State private var error: String?
    @State private var inviting = false
    @State private var invite: Invite?

    struct Invite: Identifiable { let id = UUID(); let name: String; let link: String }
    private struct PeopleList: Codable { var people: [Person] }
    private struct InviteResponse: Codable { var id: String; var link: String; var expiresInHours: Int? }

    var body: some View {
        List {
            Section {
                ForEach(people) { p in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(p.name).font(.subheadline.weight(.semibold))
                            Text(seen(p)).font(.caption).foregroundStyle(Theme.muted)
                        }
                        Spacer()
                        Menu(p.roleName) {
                            ForEach(["owner", "full", "guest"], id: \.self) { r in
                                Button(Person(id: "", name: "", role: r).roleName) { Task { await setRole(p, r) } }
                            }
                        }
                        .font(.caption.weight(.semibold))
                    }
                    .swipeActions {
                        if p.id != settings.personID {
                            Button("Entfernen", role: .destructive) { Task { await remove(p) } }
                        }
                    }
                }
            } footer: {
                Text("Gäste sehen keine Tore und Türen und können Heizung und Klima nicht verstellen. Entfernen sperrt den Zugang sofort.")
            }
            Section {
                Button { inviting = true } label: { Label("Jemanden einladen", systemImage: "qrcode") }
            }
            if let error { Text(error).font(.footnote).foregroundStyle(Theme.heat) }
        }
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .navigationTitle("Personen")
        .task { await load() }
        .refreshable { await load() }
        .sheet(isPresented: $inviting) {
            InviteForm { name, role in
                inviting = false
                Task { await create(name, role) }
            }
        }
        .sheet(item: $invite) { inv in InviteSheet(invite: inv) }
    }

    private func seen(_ p: Person) -> String {
        guard let t = p.last_seen else { return "noch nicht verbunden" }
        return "zuletzt " + Date(timeIntervalSince1970: t).formatted(.relative(presentation: .named))
    }

    private func load() async {
        do {
            people = try JSONDecoder().decode(PeopleList.self, from: try await client.get("api/people")).people
            error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func create(_ name: String, _ role: String) async {
        do {
            let body = try JSONSerialization.data(withJSONObject: ["name": name, "role": role])
            let r = try JSONDecoder().decode(InviteResponse.self, from: try await client.send("api/people", method: "POST", body: body))
            invite = Invite(name: name, link: r.link)
            await load()
        } catch { self.error = error.localizedDescription }
    }

    private func setRole(_ p: Person, _ role: String) async {
        do {
            _ = try await client.send("api/people/\(p.id)", method: "PUT", body: try JSONSerialization.data(withJSONObject: ["role": role]))
            await load()
        } catch { self.error = error.localizedDescription }
    }

    private func remove(_ p: Person) async {
        do {
            _ = try await client.send("api/people/\(p.id)", method: "DELETE", body: Data())
            await load()
        } catch { self.error = error.localizedDescription }
    }
}

private struct InviteForm: View {
    let done: (String, String) -> Void
    @State private var name = ""
    @State private var role = "guest"
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                TextField("Name, z. B. Laura", text: $name)
                Picker("Rolle", selection: $role) {
                    Text("Gast – Licht, Rollläden, Musik").tag("guest")
                    Text("Vollzugriff – alles außer Personen").tag("full")
                    Text("Besitzer – alles").tag("owner")
                }
                .pickerStyle(.inline)
            }
            .navigationTitle("Einladen")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Abbrechen") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("QR-Code erzeugen") { done(name.trimmingCharacters(in: .whitespaces), role) }
                        .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
        .preferredColorScheme(.dark)
    }
}

/// QR-Code zum Scannen + Link zum Teilen (z. B. per Nachricht)
struct InviteSheet: View {
    let invite: PeopleView.Invite
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            VStack(spacing: 18) {
                Text("Einladung für \(invite.name)").font(.title3.weight(.bold))
                if let img = qr(invite.link) {
                    Image(uiImage: img).interpolation(.none).resizable().scaledToFit()
                        .frame(width: 240, height: 240).padding(12)
                        .background(.white, in: RoundedRectangle(cornerRadius: 16))
                }
                Text("Mit der iPhone-Kamera scannen – die App öffnet sich und verbindet sich mit deinem Zuhause. Gilt 48 Stunden und nur einmal.")
                    .font(.footnote).foregroundStyle(Theme.muted).multilineTextAlignment(.center)
                if let url = URL(string: invite.link) {
                    ShareLink(item: url, message: Text("Deine Einladung für unser Zuhause – mit der App öffnen.")) {
                        Label("Link senden", systemImage: "square.and.arrow.up")
                    }
                    .buttonStyle(.borderedProminent).tint(Theme.solar).foregroundStyle(Theme.bg)
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Theme.bg.ignoresSafeArea())
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Fertig") { dismiss() } } }
        }
        .preferredColorScheme(.dark)
    }

    private func qr(_ s: String) -> UIImage? {
        let f = CIFilter.qrCodeGenerator()
        f.message = Data(s.utf8)
        f.correctionLevel = "M"
        guard let out = f.outputImage?.transformed(by: CGAffineTransform(scaleX: 10, y: 10)),
              let cg = CIContext().createCGImage(out, from: out.extent) else { return nil }
        return UIImage(cgImage: cg)
    }
}
