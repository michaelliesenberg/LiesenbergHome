# Datenschutzerklärung – Liesenberg Home

*Stand: Oktober 2026*

**Kurz:** Die App spricht nur mit deinem eigenen Hub. Der Entwickler erhält keinerlei Daten.

## Verantwortlicher
Michael Liesenberg, Deutschland – Kontakt über die [Issues-Seite des Projekts](https://github.com/michaelliesenberg/LiesenbergHome/issues).

## Welche Daten verarbeitet die App?
- **Verbindung zu deinem Hub:** Adresse des Hubs, dein persönlicher Schlüssel (im iOS-Schlüsselbund), Name und Rolle, die der Besitzer des Hubs für dich festgelegt hat.
- **Zustände und Befehle** deiner Geräte (Licht, Rollläden, Heizung, Energie, Klima, Hausgeräte, Musik). Diese Daten gehen ausschließlich zwischen der App und deinem Hub hin und her.
- **Standort (nur wenn du „Ankommen & Wegfahren" einschaltest):** iOS meldet der App, wenn du den Bereich um dein Zuhause betrittst oder verlässt. Die Position deines Zuhauses und diese Ereignisse bleiben auf deinem iPhone; an den Hub geht nur der daraus folgende Befehl (z. B. „Tor öffnen"), nie dein Standort.
- **Mitteilungen:** lokal auf dem iPhone erzeugt (z. B. „Willkommen zu Hause – Tor öffnen?"), ohne Push-Server.
- **Kamera:** nur zum Scannen des Einladungs-QR-Codes; es werden keine Bilder gespeichert oder übertragen.
- **Face ID / Gerätecode:** wird von iOS geprüft; die App erhält nur „entsperrt ja/nein".
- **Apple Music:** Wiedergabe über Apples MusicKit auf deinem Gerät, nach deiner Zustimmung.
- **Siri / Kurzbefehle:** Geräte- und Szenennamen werden iOS bekannt gemacht, damit Sprachbefehle funktionieren.

## Android-App
Für die Android-App gilt dasselbe, mit diesen Unterschieden:
- **Schlüssel:** verschlüsselt im Android-Keystore statt im iOS-Schlüsselbund.
- **Hub-Suche:** die App sucht den Hub im eigenen WLAN (mDNS); dabei verlässt nichts dein Netzwerk.
- **Kamera:** nur zum Scannen des Einladungs-QR-Codes; es werden keine Bilder gespeichert oder übertragen.
- **Fingerabdruck / PIN:** wird von Android geprüft; die App erhält nur „entsperrt ja/nein".
- **Kein Standort, keine Mitteilungen, kein Siri/Apple Music** – diese Funktionen gibt es in der Android-App nicht.

## Was die App NICHT tut
- keine Übertragung an Server des Entwicklers (es gibt keine),
- keine Analyse- oder Tracking-Werkzeuge, keine Werbung,
- kein Benutzerkonto beim Entwickler.

## Der Hub
Der Hub läuft auf deiner Hardware unter deiner Kontrolle. Er speichert seine Einstellungen und Zugangsdaten lokal. Je nach Einrichtung verbindet er sich mit Diensten, die **du** auswählst (z. B. Daikin Onecta, Bosch/Siemens Home Connect, Tailscale); dafür gelten die Datenschutzbestimmungen dieser Anbieter.

## Löschen
App löschen entfernt alle Daten auf dem iPhone bzw. Android-Gerät. Der Besitzer des Hubs kann deinen Zugang jederzeit entfernen; dein Schlüssel ist danach wertlos.
