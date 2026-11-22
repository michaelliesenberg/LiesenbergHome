"""„Wenn alle weg sind, saugen" – eine Einstellung fürs ganze Haus, für alle Saugroboter (Roborock, Vorwerk …).

Sobald alle angemeldeten Personen (App meldet Ankommen/Wegfahren an /api/presence) weg sind und es seit
`delayMin` Minuten so bleibt, startet der Hub die gewählten Roboter (leere Liste = alle).
Regeln:
- nur im Zeitfenster `from`–`to` (Standard 08:00–20:00). Gehen alle schon um 7 Uhr, startet es um 8 Uhr,
  falls dann noch alle weg sind.
- jeder Roboter höchstens einmal am Tag (über diese Funktion).
- ein Roboter wird ausgelassen, wenn er schon saugt, nicht in der Station ist, einen Fehler hat, offline ist
  oder der Akku unter `minBattery` liegt.
- Zeitpläne haben Vorrang: hat ein Zeitplan den Roboter heute schon gestartet oder wartet er gerade darauf,
  dass alle weg sind, lässt diese Funktion ihn aus (der Zeitplan kennt Räume/Saugstufe).
- optional `returnHome`: kommt jemand nach Hause, fahren die hier gestarteten Roboter zur Station.
Funktioniert mit jeder Anbindung, die in vacuums.Vacuums steckt (status/command mit „start“/„dock“).
Gespeichert in DATA_DIR/away_clean.json.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta

from hub_config import DATA_DIR

FILE = os.path.join(DATA_DIR, "away_clean.json")
DEFAULTS = {"enabled": False, "robots": [], "from": "08:00", "to": "20:00",
            "delayMin": 10, "minBattery": 50, "returnHome": False}


def _hm(s, fallback):
    try:
        hh, mm = (str(s).split(":") + ["0"])[:2]
        return f"{max(0, min(23, int(hh))):02d}:{max(0, min(59, int(mm))):02d}"
    except (TypeError, ValueError):
        return fallback


class AwayClean:
    def __init__(self, vacuum, push, presence_fn, schedules=None, legacy_on=False, start_thread=True):
        """presence_fn() → True (jemand zuhause), False (alle weg), None (unbekannt)"""
        self.vac, self.push, self.presence, self.schedules = vacuum, push, presence_fn, schedules
        self._lock = threading.Lock()
        d = self._load()
        self.cfg = self.clean(dict(DEFAULTS, **(d.get("cfg") or {})))
        if not d:                                     # erstes Mal: alten Schalter (pro Anbindung) übernehmen
            self.cfg["enabled"] = bool(legacy_on)
        self.runs = d.get("runs") or {}               # duid → {"date", "state": started|skipped, "at", "reason"}
        self.away_since = d.get("awaySince")          # Unix-Zeit, seit der alle weg sind (None = jemand da/unbekannt)
        self.last_note = d.get("lastNote")            # kurzer Text für die App („Heute 10:12 gestartet: …“)
        if start_thread:
            threading.Thread(target=self._loop, daemon=True).start()

    # ------------------------------------------------------------------ Speicher
    def _load(self):
        try:
            with open(FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"cfg": self.cfg, "runs": self.runs, "awaySince": self.away_since,
                       "lastNote": self.last_note}, f)
        os.replace(tmp, FILE)

    # ------------------------------------------------------------------ API
    @staticmethod
    def clean(c):
        return {
            "enabled": bool(c.get("enabled")),
            "robots": [str(x) for x in (c.get("robots") or [])][:20],
            "from": _hm(c.get("from"), "08:00"),
            "to": _hm(c.get("to"), "20:00"),
            "delayMin": max(0, min(120, int(c.get("delayMin") if c.get("delayMin") is not None else 10))),
            "minBattery": max(10, min(100, int(c.get("minBattery") or 50))),
            "returnHome": bool(c.get("returnHome")),
        }

    def get(self):
        st = self.vac.status or {}
        home = self.presence()
        return dict(self.cfg, presenceKnown=home is not None, allAway=home is False,
                    awaySince=self.away_since, lastNote=self.last_note,
                    devices=[{"id": d, "name": s.get("name") or "Saugroboter", "kind": s.get("kind"),
                              "today": (self.runs.get(d) or {}).get("state")
                              if (self.runs.get(d) or {}).get("date") == datetime.now().strftime("%Y-%m-%d") else None}
                             for d, s in sorted(st.items(), key=lambda x: x[1].get("name") or "")])

    def set(self, body, by=""):
        with self._lock:
            self.cfg = self.clean(dict(self.cfg, **{k: v for k, v in (body or {}).items() if k in DEFAULTS}))
            self._save()
        names = "alle" if not self.cfg["robots"] else ", ".join(
            (self.vac.status.get(d) or {}).get("name") or d for d in self.cfg["robots"])
        print(f"[ALLE-WEG] {by}: {'an' if self.cfg['enabled'] else 'aus'} ({names}, "
              f"{self.cfg['from']}–{self.cfg['to']}, nach {self.cfg['delayMin']} Min)", flush=True)
        return self.get()

    def set_enabled(self, on):
        """alter Endpunkt PUT /api/vacuum/auto-away"""
        return self.set({"enabled": bool(on)})

    # ------------------------------------------------------------------ Anwesenheit
    def on_presence(self, all_away):
        """vom Hub bei jeder Meldung der App; der eigentliche Start passiert in _tick (nach der Wartezeit)"""
        with self._lock:
            if all_away and not self.away_since:
                self.away_since = time.time()
                self._save()
            elif not all_away and self.away_since:
                self.away_since = None
                self._save()
                came_home = True
            else:
                came_home = False
        if not all_away and came_home:
            self._send_home()
        if all_away:
            self._tick()

    def _send_home(self):
        if not self.cfg["enabled"] or not self.cfg["returnHome"]:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        for duid, run in list(self.runs.items()):
            if run.get("date") != today or run.get("state") != "started" or run.get("docked"):
                continue
            st = (self.vac.status or {}).get(duid) or {}
            if not st.get("cleaning") or st.get("state") == "returning_home":
                continue
            try:
                self.vac.command(duid, "dock")
                run["docked"] = int(time.time())
                self._save()
                self._send(f"🏠 {st.get('name') or 'Saugroboter'} fährt zur Station", "Jemand ist nach Hause gekommen.")
                print(f"[ALLE-WEG] {st.get('name')}: zur Station (jemand ist da)", flush=True)
            except Exception as e:
                print(f"[ALLE-WEG] zur Station: {e}", flush=True)

    # ------------------------------------------------------------------ Ablauf
    def _send(self, title, body):
        if self.push is not None and getattr(self.push, "configured", False):
            self.push.send("vacuum", title, body)

    def _loop(self):
        time.sleep(30)
        while True:
            try:
                self._tick()
            except Exception as e:
                print(f"[ALLE-WEG] {e}", flush=True)
            time.sleep(30)

    def _window(self, now):
        f, t = (now.replace(hour=int(x[:2]), minute=int(x[3:]), second=0, microsecond=0)
                for x in (self.cfg["from"], self.cfg["to"]))
        return f <= now < t if f < t else (now >= f or now < t)      # auch über Mitternacht

    def _schedule_owns(self, duid, today):
        """Zeitplan hat den Roboter heute schon gestartet oder wartet gerade auf „alle weg“"""
        if not self.schedules:
            return False
        for s in getattr(self.schedules, "items", []):
            if s.get("duid") != duid or not s.get("enabled"):
                continue
            run = (getattr(self.schedules, "runs", {}) or {}).get(s["id"]) or {}
            if run.get("date") == today and run.get("state") in ("started", "waiting", "warned"):
                return True
        return False

    def _tick(self, now=None):
        if not self.cfg["enabled"]:
            return
        now = now or datetime.now()
        home = self.presence()
        if home is not False:
            if self.away_since:                     # z. B. Meldung älter als 7 Tage → unbekannt
                with self._lock:
                    self.away_since = None
                    self._save()
            return
        if not self.away_since:                     # Hub neu gestartet, während alle weg sind
            with self._lock:
                self.away_since = time.time()
                self._save()
        if now.timestamp() - self.away_since < self.cfg["delayMin"] * 60 or not self._window(now):
            return
        today = now.strftime("%Y-%m-%d")
        st_all = self.vac.status or {}
        wanted = self.cfg["robots"] or list(st_all)
        started, skipped = [], []
        for duid in wanted:
            run = self.runs.get(duid) or {}
            if run.get("date") == today:
                continue                            # heute schon gestartet/ausgelassen
            st = st_all.get(duid)
            if st is None:
                continue                            # (noch) nicht bekannt – nächster Durchlauf
            name = st.get("name") or "Saugroboter"
            why = None
            if self._schedule_owns(duid, today):
                why = "Zeitplan"
            elif st.get("cleaning"):
                why = "saugt schon"
            elif not st.get("online"):
                why = "nicht erreichbar"
            elif st.get("error"):
                why = str(st["error"])[:80]
            elif not st.get("docked"):
                why = "nicht in der Station"
            elif (st.get("battery") or 0) < self.cfg["minBattery"]:
                why = f"Akku unter {self.cfg['minBattery']} %"
            if why:
                self.runs[duid] = {"date": today, "state": "skipped", "reason": why, "at": int(time.time())}
                if why not in ("Zeitplan", "saugt schon"):
                    skipped.append(f"{name} ({why})")
                print(f"[ALLE-WEG] {name} ausgelassen: {why}", flush=True)
                continue
            try:
                self.vac.command(duid, "start")
                self.runs[duid] = {"date": today, "state": "started", "at": int(time.time())}
                started.append(name)
                print(f"[ALLE-WEG] {name} gestartet", flush=True)
            except Exception as e:
                self.runs[duid] = {"date": today, "state": "skipped", "reason": str(e)[:120], "at": int(time.time())}
                skipped.append(f"{name} ({str(e)[:60]})")
                print(f"[ALLE-WEG] {name}: Start fehlgeschlagen: {e}", flush=True)
        if not started and not skipped:
            return
        with self._lock:
            self.runs = {d: r for d, r in self.runs.items() if r.get("date") == today}
            self.last_note = (f"{now:%d.%m. %H:%M} " + (f"gestartet: {', '.join(started)}" if started else "")
                              + ("; " if started and skipped else "") + (f"ausgelassen: {', '.join(skipped)}" if skipped else ""))
            self._save()
        if started:
            self._send(f"🧹 {', '.join(started)} {'startet' if len(started) == 1 else 'starten'}",
                       "Alle sind weg – es wird gesaugt." + (f" Ausgelassen: {', '.join(skipped)}." if skipped else ""))
        else:
            self._send("🧹 Saugen ausgelassen", f"Alle sind weg, aber: {', '.join(skipped)}.")
