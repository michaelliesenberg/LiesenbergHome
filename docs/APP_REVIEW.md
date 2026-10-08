# Hinweise für die App-Store-Prüfung (Text für „App Review Information → Notes")

**English (for the reviewer):**

Liesenberg Home is a free companion app for a self-hosted home hub (a small server the user runs on a Raspberry Pi in their own home network, source code: https://github.com/michaelliesenberg/LiesenbergHome). The hub connects the user's own smart-home installation (Theben LUXORliving/KNX or Home Assistant, evcc solar/EV charging, Daikin air conditioning, Bosch/Siemens appliances, HomePods).

Because the app only works with the user's own hardware at home, we cannot provide a demo account. The app therefore contains a **fully functional built-in demo mode**:

1. Launch the app → tap **"Demo ansehen"** at the bottom of the welcome screen.
2. All features work with a simulated home: rooms and lights (tap to switch, drag to dim), blinds, heating, energy flow and solar chart, scenes with schedules, people/invitations, music speakers.
3. To leave the demo: Home → Einstellungen → "Demo beenden".

We kindly request approval to use this demo mode in lieu of a demo account (App Review Guideline 2.1), as access to a real hub would require physical hardware in a private home. A short screen recording of the app connected to a real hub is attached.

No account is created with us, no data is sent to the developer; privacy policy: https://github.com/michaelliesenberg/LiesenbergHome/blob/main/PRIVACY.md

**Checkliste vor dem Einreichen**
- [ ] Bildschirmvideo (1–2 Min.): Einladung scannen → Räume → Licht schalten → Energie → Szene
- [ ] Datenschutz-URL in App Store Connect: Link oben
- [ ] App-Datenschutz („Nutzung von Daten"): *Keine Daten erfasst*
- [ ] Kategorie: Lifestyle oder Dienstprogramme · Preis: kostenlos
- [ ] Exportkonformität: nur Standard-Verschlüsselung (HTTPS) → „Ja, aber ausgenommen"
