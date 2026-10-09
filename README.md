# Liesenberg Home

**Dein Zuhause in einer App – Licht, Rollläden, Heizung, Energie, Klima, Hausgeräte und Musik.**
Kostenlos, ohne Cloud-Konto beim Hersteller der App: Alles läuft über einen kleinen **Hub** in deinem eigenen Netzwerk (z. B. ein Raspberry Pi).

> *English:* A free iOS app plus a self-hosted hub (Raspberry Pi) for LUXORliving/KNX or Home Assistant, evcc, Daikin Onecta, Bosch/Siemens Home Connect and HomePods. The app and hub UI are currently German.

| | |
|---|---|
| **Geräte** | Theben **LUXORliving** (IP1) oder **Home Assistant** – Etagen, Räume, Licht, Dimmer, Rollläden, Heizung, Tore |
| **Energie** | **evcc** – PV (mehrere Wechselrichter), Hausverbrauch, Akku, Netz, Wallbox, Prognose; der Hub zeichnet jede Minute auf (Tagesverlauf, Tageswerte) |
| **Klima** | **Daikin** (Onecta) |
| **Hausgeräte** | **Bosch / Siemens** (Home Connect) |
| **Musik** | Apple Music in der App, alle **HomePods/AirPlay**-Lautsprecher, Radio-Zeitpläne |
| **Szenen** | Mehrere Geräte auf einen Tipp, mit Zeitplan (auch Sonnenauf-/-untergang), läuft auf dem Hub |
| **Siri** | „Liesenberg Home Esstisch an", „… alles aus", „… Szene Garten" |
| **Personen** | Einladung per QR-Code, eigener Schlüssel pro Person, Gastzugang ohne Tore/Türen |

---

## 1. Hub installieren (Raspberry Pi)

Raspberry Pi OS (64-bit, Bookworm oder neuer) – im selben Netzwerk wie deine Geräte. Dann **eine Zeile** im Terminal des Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/michaelliesenberg/LiesenbergHome/main/hub/install.sh | sudo bash
```

Am Ende zeigt das Skript die Adresse der **Einrichtungsseite**, z. B. `http://raspberrypi.local:8080/setup`.
Zum Aktualisieren dieselbe Zeile erneut ausführen – oder auf der Einrichtungsseite unter *System → Aktualisieren*.

## 2. Einrichten im Browser

Auf der Einrichtungsseite (nur aus dem Heimnetz erreichbar):

1. **Name & Hub-Passwort** festlegen.
2. **Geräte:** LUXORliving (IP1-Adresse, Benutzer, Passwort, `.lxp` aus LUXORplug hochladen) **oder** Home Assistant (Adresse + *Langlebiger Zugriffstoken* aus deinem HA-Profil).
3. **Energie:** evcc wird automatisch gesucht.
4. **Fernzugriff:** *Tailscale* mit einem Klick (kostenloses Konto) – danach erreicht die App den Hub von überall über `https://…ts.net`. Alternativ eine eigene Domain (siehe `hub/caddy-https.sh`).
5. **Klima / Hausgeräte (optional):** Daikin und Home Connect verlangen je eine kostenlose eigene Entwickler-App – die Seite führt dich Schritt für Schritt durch.
6. **Personen:** QR-Code mit dem iPhone scannen → die App ist verbunden. Weitere Personen einladen: *Vollzugriff* oder *Gast*.

## 3. App

- Aus dem App Store laden (kommt bald) oder selbst bauen: `app/LiesenbergHome.xcodeproj` in Xcode 16 öffnen → *Signing & Capabilities* → eigenes Team und eigene Bundle-ID → ▶︎.
- Ohne Hub: **„Demo ansehen"** auf dem Startbildschirm.

## Sicherung & Umzug

Auf der Einrichtungsseite unter **System → Sicherung herunterladen** gibt es eine Datei mit allem, was der Hub braucht
(Einstellungen, Zugangsdaten, Personen, Szenen, LUXOR-Projekt, Anmeldungen bei Daikin/Home Connect, evcc, Caddy).
Geht die SD-Karte kaputt: Pi neu aufsetzen, Hub mit der Zeile oben installieren und auf der Willkommensseite
**„Sicherung einspielen"** wählen – alle Apps funktionieren danach ohne neue Einladung weiter.
Die Sicherung enthält Zugangsdaten: nur privat aufbewahren.

**Automatisch in Google Drive:** Unter *System → Sicherung in Google Drive* einmal mit [rclone](https://rclone.org) verbinden
(`rclone authorize "drive"` am Computer, Token einfügen) – dann lädt der Hub jede Nacht um 3:30 eine Sicherung hoch.

## Rollen

| | Besitzer | Vollzugriff | Gast |
|---|:-:|:-:|:-:|
| Licht, Rollläden, Musik, Szenen starten | ✓ | ✓ | ✓ |
| Tore & Türen | ✓ | ✓ | – |
| Heizung & Klima verstellen | ✓ | ✓ | nur ansehen |
| Szenen & Zeitpläne bearbeiten | ✓ | ✓ | – |
| Hub aktualisieren | ✓ | ✓ | – |
| Personen einladen / entfernen, Einrichtung | ✓ | – | – |

Einladungen sind Einmal-Codes (48 h gültig). Auf dem Hub liegen nur Prüfsummen der Schlüssel. Entfernen sperrt sofort.

## Datenschutz

Die App sendet Daten ausschließlich an **deinen** Hub. Es gibt keinen Server des Entwicklers, keine Analyse, keine Werbung. Details: [PRIVACY.md](PRIVACY.md).

## Aufbau

```
hub/      Python-Hub (FastAPI) – install.sh, Einrichtungsseite, Geräte-Anbindungen
app/      iOS-App (SwiftUI, iOS 17+)
docs/     Hinweise für die App-Store-Prüfung
```

## Hinweise

- LUXORliving: getestet mit dem Theben IP1. Die KNX-Gruppenadressen aus der `.lxp` werden automatisch auf die REST-Datenpunkte des IP1 abgebildet.
- Home Assistant: ab Version 2024.4 (Etagen). Angezeigt werden Lichter, Schalter, Abdeckungen, Klima und Schlösser, die einem Bereich zugeordnet sind.
- Garagentore, Tore und Türöffner sind Gästen nie zugänglich und lösen per Siri nur bei entsperrtem iPhone aus.

Fragen, Fehler, Wünsche: [Issues](https://github.com/michaelliesenberg/LiesenbergHome/issues).
