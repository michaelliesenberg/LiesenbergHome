"""Verbraucher lernen: Was braucht welches Licht/Gerät?

Prinzip: Der Hub liest den Hausverbrauch aus evcc alle 2 s in einen Ringpuffer. Wird ein Gerät über den Hub
geschaltet (App, Szene, Siri, Zentral aus, Zeitplan), vergleicht er den Verbrauch 10 s vorher mit 5–15 s
nachher. Seit 2.7.2 fragt der Hub außerdem laufend den Zustand aller Lichter ab (LUXOR/IP1/KNX bzw. Home
Assistant): Schaltet jemand am Wandtaster, wird diese Änderung genauso gemessen. Die Differenz ist der Verbrauch des Geräts (bei Dimmern hochgerechnet auf 100 %). Unruhige Messungen
(z. B. Wasserkocher springt gleichzeitig an) werden verworfen; aus vielen Schaltvorgängen wird der Median.

Grundlast: niedrigster 15-Minuten-Mittelwert nachts (0–5 Uhr) der letzten 7 Tage aus der Energie-Aufzeichnung.
"""
import json
import os
import statistics
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta

import requests

from hub_config import DATA_DIR

FILE = os.path.join(DATA_DIR, "consumers.json")
SAMPLE = 2.0


class Consumers:
    def __init__(self, evcc_url_fn, energy_log):
        self.evcc_url_fn, self.log = evcc_url_fn, energy_log
        self.buf = deque(maxlen=150)              # (zeit, hausverbrauch W) – 5 Minuten
        self.cmds = deque(maxlen=50)              # Zeitpunkte aller Schaltbefehle (auch am Taster erkannte)
        self.recent = {}                          # did → Zeitpunkt des letzten Befehls über den Hub
        self._lock = threading.Lock()
        self.data = self._load()                   # did → {name, kind, samples: [W bei 100 %], updated}
        self.stats = {"ok": 0, "group": 0, "stale": 0, "noisy": 0, "range": 0}   # Auswertung seit Hub-Start
        threading.Thread(target=self._sampler, daemon=True).start()

    def _load(self):
        try:
            with open(FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, FILE)

    def _sampler(self):
        while True:
            url = (self.evcc_url_fn() or "").rstrip("/")
            if url:
                try:
                    s = requests.get(url + "/api/state", timeout=3).json()
                    s = s.get("result", s)
                    hp = s.get("homePower")
                    if hp is not None:
                        self.buf.append((time.time(), float(hp)))
                except Exception:
                    pass
            time.sleep(SAMPLE)

    def _window(self, t0, t1):
        return [w for t, w in list(self.buf) if t0 <= t <= t1]

    # ---- Lernen
    def note_command(self, did=None):
        """Schaltbefehl über den Hub merken (stört gleichzeitige Messungen, Taster-Erkennung ignoriert ihn)."""
        now = time.time()
        self.cmds.append(now)
        if did:
            self.recent[did] = now

    def observe(self, did, dev, cmd, prev_level=None):
        """Nach einem Schaltbefehl aufrufen (läuft im Hintergrund)."""
        self.note_command(did)                      # auch andere Geräte merken (stören die Messung)
        kind = dev.get("kind")
        if kind not in ("light", "dimmer"):
            return
        if "on" in cmd:
            change = 1.0 if cmd["on"] else -1.0
        elif "level" in cmd and kind == "dimmer":
            new = float(cmd["level"]) / 100
            old = (prev_level if prev_level is not None else (0.0 if new > 0 else 1.0))
            change = new - old
        else:
            return
        if abs(change) < 0.2:                     # kleine Dimmschritte sind zu ungenau
            return
        t0 = time.time()
        threading.Thread(target=self._measure, args=(did, dev, change, t0, t0), daemon=True).start()

    def _updates(self, t0, t1):
        """Echte evcc-Aktualisierungen im Zeitraum: evcc rechnet nur alle 10–30 s neu (Einstellung „interval"),
        der Hub fragt alle 2 s – gleiche Werte hintereinander sind dieselbe Messung. → [(erstmals gesehen, W)]"""
        out, last = [], None
        for t, w in list(self.buf):
            if w != last:
                if t0 <= t <= t1:
                    out.append((t, w))
                last = w
        return out

    def _measure(self, did, dev, change, t_before, t_after, source="App"):
        """t_before: zuletzt noch alter Zustand, t_after: Änderung erkannt (bei Befehlen identisch).

        Vorher = die letzten zwei evcc-Werte vor dem Schalten, nachher = die ersten zwei Werte, die evcc nach dem
        Schalten neu berechnet hat (bis zu 75 s warten). So klappt es auch mit evcc-Intervall 30 s."""
        time.sleep(max(0.0, t_after + 75 - time.time()))
        # Mehrere Geräte gleichzeitig geschaltet (Szene, Zentral aus, zwei Taster)? Dann nicht zuzuordnen.
        if sum(1 for t in list(self.cmds) if t_before - 3 <= t <= t_after + 40) > 1:
            self.stats["group"] += 1
            return
        before = [w for _, w in self._updates(t_before - 70, t_before)][-2:]
        after = [w for _, w in self._updates(t_after + 3, t_after + 75)][:2]
        if not before or not after:
            self.stats["stale"] += 1
            return
        if (len(before) == 2 and abs(before[0] - before[1]) > 60) or (len(after) == 2 and abs(after[0] - after[1]) > 60):
            self.stats["noisy"] += 1
            return                                 # zu unruhig – andere Verbraucher haben sich bewegt
        delta = statistics.median(after) - statistics.median(before)
        watts = delta / change
        if not 1 <= watts <= 2500:
            self.stats["range"] += 1
            return
        self.stats["ok"] += 1
        with self._lock:
            e = self.data.setdefault(did, {"samples": []})
            e.update(name=dev.get("name"), kind=dev.get("kind"), updated=int(time.time()))
            e["samples"] = (e["samples"] + [round(watts, 1)])[-25:]
            self._save()
        print(f"[VERBRAUCH] {dev.get('name')}: {watts:.0f} W gemessen ({source}, {len(e['samples'])} Messungen)",
              flush=True)

    # ---- Schalten am Wandtaster erkennen
    def watch(self, lights_fn, states_fn, interval=3.0):
        """Zustände aller Lichter laufend abfragen. lights_fn() → {did: dev}, states_fn([did]) → {did: {on, level}}.
        Änderungen, die nicht vom Hub kamen (Taster, KNX, LUXOR-App), laufen in dieselbe Messung."""
        threading.Thread(target=self._watcher, args=(lights_fn, states_fn, interval), daemon=True).start()

    @staticmethod
    def _amount(dev, st):
        """Anteil 0…1, wie weit das Licht an ist."""
        if not st.get("on"):
            return 0.0
        if dev.get("kind") == "dimmer" and st.get("level") is not None:
            return max(0.0, min(1.0, float(st["level"]) / 100))
        return 1.0

    def _watcher(self, lights_fn, states_fn, interval):
        last = {}                                   # did → (anteil, zuletzt so gesehen)
        lights, lights_at = {}, 0.0
        while True:
            t_round = time.time()
            try:
                if not self.evcc_url_fn() or not self.buf or t_round - self.buf[-1][0] > 30:
                    last.clear()                    # ohne Hausverbrauch nichts zu lernen
                    time.sleep(30)
                    continue
                if t_round - lights_at > 60:
                    lights, lights_at = lights_fn() or {}, t_round
                answered = 0
                for did, dev in lights.items():
                    st = (states_fn([did]) or {}).get(did)
                    now = time.time()
                    if st is None:                  # kein lesbarer Zustand (oder IP1 hängt) → überspringen
                        last.pop(did, None)
                        continue
                    answered += 1
                    amount = self._amount(dev, st)
                    prev = last.get(did)
                    last[did] = (amount, now)
                    if prev is None or abs(amount - prev[0]) < 1e-6:
                        continue
                    if now - self.recent.get(did, 0) < 30:
                        continue                    # kam vom Hub – wird schon über observe() gemessen
                    self.cmds.append(now)
                    change = amount - prev[0]
                    if abs(change) < 0.2 or now - prev[1] > 15:
                        continue                    # zu kleiner Dimmschritt bzw. Zeitpunkt zu ungenau
                    print(f"[VERBRAUCH] {dev.get('name')} am Taster geschaltet ({prev[0]:.0%} → {amount:.0%})",
                          flush=True)
                    threading.Thread(target=self._measure, args=(did, dev, change, prev[1], now, "Taster"),
                                     daemon=True).start()
                if lights and not answered:         # IP1/Home Assistant antwortet gar nicht → später wieder
                    time.sleep(30)
            except Exception as e:
                print(f"[VERBRAUCH] Zustandsabfrage: {e}", flush=True)
                last.clear()
                lights_at = 0.0
                time.sleep(30)
            time.sleep(max(0.5, interval - (time.time() - t_round)))

    # ---- Auswertung
    def baseline(self):
        """Grundlast: kleinster 15-Min-Mittelwert nachts (0–5 Uhr) der letzten 7 Tage."""
        best = None
        for i in range(1, 8):
            rows = [r for r in self.log.rows(date.today() - timedelta(days=i))
                    if datetime.fromtimestamp(r["t"]).hour < 5 and r.get("home") is not None]
            for k in range(0, max(len(rows) - 14, 0)):
                avg = sum(r["home"] for r in rows[k:k + 15]) / 15
                best = avg if best is None or avg < best else best
        return round(best) if best is not None else None

    def report(self, states=None, home_now=None):
        out = []
        for did, e in self.data.items():
            if not e.get("samples"):
                continue
            w = statistics.median(e["samples"])
            st = (states or {}).get(did) or {}
            on = st.get("on")
            level = st.get("level")
            now = (w * (level / 100 if level is not None else 1)) if on else 0
            out.append({"id": did, "name": e.get("name"), "kind": e.get("kind"), "watts": round(w),
                        "samples": len(e["samples"]), "on": on, "nowWatts": round(now)})
        out.sort(key=lambda x: -x["watts"])
        known = sum(x["nowWatts"] for x in out)
        base = self.baseline()
        return {"baseline": base, "homeNow": round(home_now) if home_now is not None else None,
                "knownNow": round(known), "devices": out, "learning": dict(self.stats),
                "rest": round(home_now - known - (base or 0)) if home_now is not None else None}
