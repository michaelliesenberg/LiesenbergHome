# App Store – alles zum Kopieren

## Ablauf (ca. 1–2 Stunden, dann 1–3 Tage Prüfung)

1. **GitHub hochladen** (die `git`-Befehle) – die Datenschutz-URL unten muss erreichbar sein, das Repo öffentlich.
2. **App Store Connect** (appstoreconnect.apple.com) → *Apps* → **+** → *Neue App*
   - Plattform: iOS · Name: **Liesenberg Home** · Sprache: Deutsch
   - Bundle-ID: **biz.liesenberg.home** · SKU: `liesenberg-home-1` · Zugriff: Vollständig
3. **Xcode:** oben als Ziel *Any iOS Device (arm64)* wählen → *Product → Archive* → im Organizer **Distribute App** → *App Store Connect* → *Upload*.
   Nach ca. 15 Min. erscheint der Build in App Store Connect (TestFlight). Bei jedem neuen Upload in Xcode unter *General* die **Build**-Nummer um 1 erhöhen.
4. **Screenshots** (Pflicht): im Simulator *iPhone 16 Pro Max* die App starten → **Demo ansehen** → je Bildschirm `⌘S`
   (Home, Energie, Räume, ein Raum, Musik, Szenen). Mindestens 3, höchstens 10.
   Die App ist auch für iPad freigegeben → dann zusätzlich 3 Screenshots im Simulator *iPad Pro 13"*.
   *Einfacher:* In Xcode → Target → *General* → *Supported Destinations* das **iPad entfernen** – dann reichen iPhone-Screenshots (auf Mac und iPad läuft die iPhone-App trotzdem).
5. Texte unten eintragen, **App-Datenschutz** ausfüllen, Build auswählen → **Zur Prüfung einreichen**.

---

## Texte

**Untertitel** (max. 30): `Dein Zuhause in einer App`

**Werbetext** (max. 170):
`Licht, Rollläden, Heizung, Solarstrom, Wallbox, Klima, Hausgeräte und HomePods – übersichtlich in einer App. Kostenlos, ohne Cloud-Konto, über deinen eigenen Hub.`

**Beschreibung:**
```
Liesenberg Home bringt dein ganzes Zuhause in eine schöne, schnelle App – ohne Cloud-Konto beim Entwickler. Die App verbindet sich mit deinem eigenen Hub, einem kleinen Server in deinem Netzwerk (z. B. ein Raspberry Pi). Den Hub gibt es kostenlos und quelloffen auf GitHub.

LICHT, ROLLLÄDEN, HEIZUNG
• Räume und Etagen automatisch aus Theben LUXORliving (KNX) oder Home Assistant
• Licht schalten und dimmen, Rollläden fahren, Raumtemperatur einstellen
• „Zentral aus" mit einem Tipp

ENERGIE
• Live-Energiefluss: Solar, Hausverbrauch, Akku, Netz (über evcc)
• Mehrere Wechselrichter, Tagesverlauf, Tageswerte, Solarprognose
• Wallbox und Auto-Ladestand

SZENEN & ZEITPLÄNE
• Mehrere Geräte auf einen Tipp – z. B. „Abendessen" oder „Garten"
• Zeitpläne nach Uhrzeit oder Sonnenauf- und -untergang, laufen auf dem Hub

ANKOMMEN & WEGFAHREN
• Beim Heimkommen fragt dich dein iPhone, ob das Tor aufgehen soll
• Beim Wegfahren: „Alle Lichter aus?"

KLIMA, HAUSGERÄTE, MUSIK
• Daikin-Klimageräte, Bosch/Siemens-Hausgeräte (Home Connect)
• Apple Music und alle HomePods, Radio-Zeitpläne

FAMILIE & GÄSTE
• Einladung per QR-Code – jede Person mit eigenem Zugang
• Gäste sehen keine Tore und Türen
• Face ID-Sperre, Siri: „Liesenberg Home Esstisch an"

PRIVAT
Keine Werbung, keine Analyse, keine Daten beim Entwickler. Alles bleibt zwischen deinem iPhone und deinem Hub.

Ohne Hub? Tippe auf „Demo ansehen" und probiere alles mit einem Beispielhaus aus.
```

**Schlüsselwörter** (max. 100):
`Smart Home,KNX,LUXORliving,Home Assistant,evcc,Solar,PV,Energie,Wallbox,HomePod,Daikin,Szenen,Hub`

**Support-URL:** `https://github.com/michaelliesenberg/LiesenbergHome/issues`
**Marketing-URL:** `https://github.com/michaelliesenberg/LiesenbergHome`
**Datenschutz-URL:** `https://github.com/michaelliesenberg/LiesenbergHome/blob/main/PRIVACY.md`

**Kategorie:** Lifestyle · zweite: Dienstprogramme
**Alterseinstufung:** alle Fragen „Nein" → 4+
**Preis:** kostenlos · **Verfügbarkeit:** alle Länder (oder nur Deutschland/Österreich/Schweiz, da die App Deutsch ist)
**Copyright:** `2026 Michael Liesenberg`

## App-Datenschutz (Fragebogen)
- „Erfassen Sie Daten?" → **Nein, wir erfassen keine Daten von dieser App.**
  (Standort, Geräte und Befehle bleiben auf dem iPhone bzw. gehen nur an den Hub des Nutzers – nicht an dich.)

## Exportkonformität
Schon erledigt: `ITSAppUsesNonExemptEncryption = NO` steht in der Info.plist (nur HTTPS) – die Frage kommt nicht mehr.

## App-Prüfung (App Review Information)
- **Anmeldung erforderlich:** Nein (Demo-Modus)
- **Notizen:** Text aus `docs/APP_REVIEW.md` (englisch) einfügen
- **Anhang:** Bildschirmvideo (1–2 Min.) mit echtem Hub: Einladung scannen → Räume → Licht → Energie → Szene
- **Kontakt:** Name, Telefon, E-Mail
