"""Hausgeräte lernen: Was braucht die Spülmaschine, die Waschmaschine, der Backofen … wirklich?

Prinzip (wie bei den Lichtern, consumers.py): Der Hub kennt den Hausverbrauch aus evcc alle 2 s. evcc rechnet
„homePower" ohne PV, Akku und Wallbox – Sonne, Akku und Autoladen stören die Messung also nicht.
Alle 30 s schaut der Hub, welche Geräte laufen (Home Connect, Miele, LG ThinQ: Zustand „läuft", Wärmepumpe:
Verdichter, Daikin: an/aus).

- Programm-Geräte (Spül-/Waschmaschine, Trockner, Backofen, Kochfeld): Beim Start merkt sich der Hub den
  Hausverbrauch davor (Grundwert). Während des Programms zählt er alles darüber zusammen (Energie), bis das Gerät
  fertig ist. Laufen dabei andere bekannte Geräte an oder aus (Wärmepumpe!), wird deren gelernte Leistung
  herausgerechnet. Ergebnis je Lauf: kWh, mittlere Leistung, Spitze, Dauer – je Gerät und je Programm.
  Miele misst den Verbrauch selbst (ecoFeedback) – dann zählt dieser Wert.
- An/Aus-Geräte (Klimageräte): Sprung im Hausverbrauch beim Ein-/Ausschalten (wie bei den Lichtern).
Aus jedem Lauf wird der Mittelwert genauer (erst normaler Mittelwert, ab dem 4. Lauf gleitend mit 25 %),
Ausreißer (> 3× bzw. < ⅓ des bisherigen Werts) werden verworfen. Gespeichert in DATA_DIR/appliances-learned.json.

Auto-Start (smartstart.py) nimmt daraus die mittlere Leistung als „so viel Sonne muss mindestens da sein".
"""
import json
import os
import statistics
import threading
import time

from hub_config import DATA_DIR

FILE = os.path.join(DATA_DIR, "appliances-learned.json")
TICK = 30                     # s zwischen zwei Abfragen der Geräte
GRACE = 15 * 60               # so lange darf ein Gerät „nicht laufen" (Pause, LG fragt nur jede Minute), ohne dass der Lauf endet
MIN_RUN = 10 * 60             # kürzere Läufe sind zu ungenau
MIN_RUNS = 3                  # ab so vielen Läufen gilt der Wert als gelernt (Auto-Start)
TYPES = {"Dishwasher": "dishwasher", "Washer": "washer", "WasherDryer": "washer", "Dryer": "dryer", "Oven": "oven",
         "Hob": "hob", "CookProcessor": "hob", "Geschirrspüler": "dishwasher", "Waschmaschine": "washer",
         "Waschtrockner": "washer", "Trockner": "dryer", "Backofen": "oven", "Dampfgarer": "oven",
         "Backofen mit Mikrowelle": "oven", "Kochfeld": "hob"}
STEP_TYPES = {"climate"}      # an/aus mit (ungefähr) gleichbleibender Leistung → Sprung messen


def _ema(old, new, n):
    """Mittelwert, der mit jedem Lauf genauer wird: bis 4 Läufe normaler Mittelwert, danach gleitend (25 %)."""
    if old is None:
        return new
    a = max(1.0 / n, 0.25)
    return old + a * (new - old)


class ApplianceLearner:
    def __init__(self, house_buf_fn, sources_fn, start=True):
        """house_buf_fn() → [(zeit, Hausverbrauch W)] der letzten Minuten (consumers.buf).
        sources_fn() → [{id, name, type, brand, on, program, meterKWh?, knownW?, learn?}]"""
        self.house_buf_fn, self.sources_fn = house_buf_fn, sources_fn
        self._lock = threading.Lock()
        self.data = self._load()                    # id → gelernte Werte (siehe _finish)
        self.runs = {}                              # id → laufende Messung
        self.state = {}                             # id → {on, src, since}
        self.pending_steps = []                     # Sprungmessungen, die noch auf Werte „danach" warten
        self.pre = {}                               # id → Hausverbrauch kurz vor dem Start (letzte Runde)
        self.stats = {"runs": 0, "short": 0, "stale": 0, "range": 0, "outlier": 0, "steps": 0}
        self._last_tick = None
        if start:
            threading.Thread(target=self._loop, daemon=True).start()

    # ---- Datei
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

    # ---- Hilfen
    def _samples(self, t0, t1):
        return [(t, w) for t, w in list(self.house_buf_fn() or []) if t0 <= t <= t1]

    def known_w(self, did, src=None):
        """Bekannte Leistung eines Geräts (zum Herausrechnen, wenn es während eines anderen Laufs schaltet)."""
        if src and src.get("knownW"):
            return float(src["knownW"])
        e = self.data.get(did) or {}
        return e.get("watts") or e.get("stepW")

    def _loop(self):
        time.sleep(45)
        while True:
            try:
                self.tick()
            except Exception as ex:
                print(f"[GERÄTE-LERNEN] {type(ex).__name__}: {ex}", flush=True)
            time.sleep(TICK)

    # ---- eine Runde
    def tick(self, now=None):
        now = now or time.time()
        prev_tick = self._last_tick or now - TICK
        self._last_tick = now
        try:
            sources = {s["id"]: s for s in (self.sources_fn() or []) if s.get("id")}
        except Exception:
            sources = {}
        buf_ok = bool(self._samples(now - 90, now))

        # 1) laufende Programme weiterzählen (alles über dem Grundwert, andere Geräte herausgerechnet)
        for did, run in list(self.runs.items()):
            self._integrate(run, now, sources)

        # 2) Zustandswechsel erkennen
        for did, src in sources.items():
            on = bool(src.get("on"))
            st = self.state.get(did)
            typ = src.get("type")
            if st is None:                           # erste Runde nach Hub-Start: Lauf mittendrin → nicht messen
                self.state[did] = {"on": on, "since": now, "lastOn": now if on else None}
                continue
            if on:
                st["lastOn"] = now
            if on and not st["on"]:
                st.update(on=True, since=now)
                if did in self.runs:                 # war nur kurz pausiert → weiterzählen
                    self.runs[did]["paused"] = None
                elif src.get("learn", True) and typ in STEP_TYPES:
                    self._step_start(did, src, True, prev_tick, now)
                elif src.get("learn", True) and buf_ok:
                    self._run_start(did, src, prev_tick, now, sources)
            elif not on and st["on"]:
                st.update(on=False, since=now)
                if src.get("learn", True) and typ in STEP_TYPES:
                    self._step_start(did, src, False, prev_tick, now)
                elif did in self.runs:
                    self.runs[did]["paused"] = st.get("lastOn") or now
            if did in self.runs:
                run = self.runs[did]
                if src.get("program"):
                    run["program"] = src["program"]
                if src.get("meterKWh") is not None and on:
                    run["meterKWh"] = src["meterKWh"]
            if not on and did not in self.runs:      # Grundwert für den nächsten Start
                pre = [w for _, w in self._samples(now - 120, now)]
                self.pre[did] = statistics.median(pre) if pre else None
            st["on"] = on

        # 3) Programme beenden (Gerät steht länger als GRACE oder ist aus der Liste verschwunden)
        for did, run in list(self.runs.items()):
            src = sources.get(did)
            paused = run.get("paused")
            if src is None and now - run["lastData"] > GRACE:
                self.runs.pop(did, None)
            elif paused and now - paused > GRACE:
                self.runs.pop(did, None)
                self._finish(did, run, paused)

        # 4) Sprungmessungen auswerten
        for p in list(self.pending_steps):
            if now >= p["t"] + 150:
                self.pending_steps.remove(p)
                self._step_finish(p)

    # ---- Programm-Läufe
    def _others_on(self, sources, exclude):
        return {d: bool(s.get("on")) for d, s in sources.items() if d != exclude}

    def _run_start(self, did, src, prev_tick, now, sources):
        base = self.pre.get(did)
        if base is None:
            pre = [w for _, w in self._samples(prev_tick - 120, prev_tick)]
            base = statistics.median(pre) if pre else None
        if base is None:
            self.stats["stale"] += 1
            return
        self.runs[did] = {"start": prev_tick, "base": base, "joules": 0.0, "lastT": prev_tick, "lastData": now,
                          "peak": 0.0, "program": src.get("program"), "meterKWh": None, "paused": None,
                          "others": self._others_on(sources, did), "name": src.get("name"), "type": src.get("type"),
                          "brand": src.get("brand"), "gaps": 0}
        print(f"[GERÄTE-LERNEN] {src.get('name')} läuft – Grundwert {base:.0f} W", flush=True)

    def _correction(self, run, sources):
        """Andere Geräte, die seit dem Start an- (+) bzw. ausgegangen (−) sind, mit ihrer bekannten Leistung."""
        corr = 0.0
        for d, was_on in run["others"].items():
            s = sources.get(d)
            if s is None:
                continue
            now_on = bool(s.get("on"))
            if now_on != was_on:
                w = self.known_w(d, s) or 0
                corr += w if now_on else -w
        return corr

    def _integrate(self, run, now, sources):
        if run.get("paused"):                      # in der Pause zählt nichts mehr dazu
            run["lastT"] = now
            return
        pts = self._samples(run["lastT"] + 1e-6, now)
        if not pts:
            if now - run["lastT"] > 120:          # evcc liefert nichts → Lauf unbrauchbar
                run["gaps"] += 1
            run["lastT"] = now
            return
        corr = self._correction(run, sources)
        excess = [max(0.0, w - run["base"] - corr) for _, w in pts]
        mean = sum(excess) / len(excess)
        run["joules"] += mean * (now - run["lastT"])
        run["peak"] = max(run["peak"], mean)
        run["lastT"], run["lastData"] = now, now

    def _finish(self, did, run, end):
        dur = end - run["start"]
        if dur < MIN_RUN:
            self.stats["short"] += 1
            return
        if run["gaps"] > 3:
            self.stats["stale"] += 1
            return
        kwh = run["joules"] / 3.6e6
        src = "Hausverbrauch"
        if isinstance(run.get("meterKWh"), (int, float)) and run["meterKWh"] > 0.02:
            kwh, src = float(run["meterKWh"]), "Gerät"     # Miele misst selbst – genauer
        if not 0.03 <= kwh <= 12:
            self.stats["range"] += 1
            return
        self.learn_run(did, run.get("name"), run.get("type"), run.get("brand"), run.get("program"), kwh,
                       dur / 60, run.get("peak") or None, src)

    def learn_run(self, did, name, typ, brand, program, kwh, minutes, peak_w=None, source="Hausverbrauch"):
        """Einen fertigen Lauf übernehmen (auch für Tests)."""
        watts = kwh * 1000 / (minutes / 60)
        with self._lock:
            e = self.data.setdefault(did, {"runs": 0, "programs": {}, "history": []})
            if e.get("runs", 0) >= MIN_RUNS and e.get("kwh"):
                if not e["kwh"] / 3 <= kwh <= e["kwh"] * 3:
                    self.stats["outlier"] += 1
                    print(f"[GERÄTE-LERNEN] {name}: {kwh:.2f} kWh verworfen (Ausreißer, sonst {e['kwh']:.2f})",
                          flush=True)
                    return False
            n = e.get("runs", 0) + 1
            e.update(name=name or e.get("name"), type=typ or e.get("type"), brand=brand or e.get("brand"), runs=n,
                     kwh=_ema(e.get("kwh"), kwh, n), minutes=_ema(e.get("minutes"), minutes, n),
                     watts=_ema(e.get("watts"), watts, n), source=source, updated=int(time.time()))
            if peak_w:
                e["peakW"] = _ema(e.get("peakW"), peak_w, n)
            e["history"] = (e.get("history", []) + [{"t": int(time.time()), "kwh": round(kwh, 3),
                                                     "min": round(minutes), "w": round(watts),
                                                     "prog": program, "src": source}])[-20:]
            if program:
                p = e["programs"].setdefault(program, {"runs": 0})
                pn = p["runs"] + 1
                p.update(runs=pn, kwh=_ema(p.get("kwh"), kwh, pn), minutes=_ema(p.get("minutes"), minutes, pn),
                         watts=_ema(p.get("watts"), watts, pn))
            self._save()
        self.stats["runs"] += 1
        print(f"[GERÄTE-LERNEN] {name}{f' ({program})' if program else ''}: {kwh:.2f} kWh in {minutes:.0f} min "
              f"= Ø {watts:.0f} W ({source}, {n} Läufe)", flush=True)
        return True

    # ---- An/Aus-Geräte (Sprung)
    def _step_start(self, did, src, turned_on, prev_tick, now):
        self.pending_steps.append({"id": did, "on": turned_on, "t": now, "before_t": prev_tick,
                                   "name": src.get("name"), "type": src.get("type"), "brand": src.get("brand")})

    def _step_finish(self, p):
        before = [w for _, w in self._samples(p["before_t"] - 90, p["before_t"])]
        after = [w for _, w in self._samples(p["t"] + 60, p["t"] + 150)]
        if len(before) < 3 or len(after) < 3:
            self.stats["stale"] += 1
            return
        if statistics.pstdev(before) > 300 or statistics.pstdev(after) > 450:
            self.stats["range"] += 1
            return
        d = statistics.median(after) - statistics.median(before)
        w = d if p["on"] else -d
        if not 50 <= w <= 6000:
            self.stats["range"] += 1
            return
        self.learn_step(p["id"], p.get("name"), p.get("type"), p.get("brand"), w)

    def learn_step(self, did, name, typ, brand, watts):
        with self._lock:
            e = self.data.setdefault(did, {"runs": 0, "programs": {}, "history": []})
            e["steps"] = (e.get("steps", []) + [round(watts)])[-25:]
            e.update(name=name or e.get("name"), type=typ or e.get("type"), brand=brand or e.get("brand"),
                     stepW=statistics.median(e["steps"]), updated=int(time.time()))
            self._save()
        self.stats["steps"] += 1
        print(f"[GERÄTE-LERNEN] {name}: {watts:.0f} W beim Schalten ({len(e['steps'])} Messungen)", flush=True)

    # ---- Auswertung
    @staticmethod
    def confidence(e):
        """0…1: wie sicher der Wert ist (Anzahl Läufe und wie stark sie schwanken)."""
        hist = [h["kwh"] for h in e.get("history", [])]
        n = e.get("runs", 0) or len(e.get("steps", []))
        if n == 0:
            return 0.0
        vals = hist if len(hist) >= 2 else e.get("steps", [])
        cv = statistics.pstdev(vals) / statistics.mean(vals) if len(vals) >= 2 and statistics.mean(vals) else 0.5
        return round(max(0.05, min(1.0, n / 8)) * max(0.3, 1 - cv), 2)

    def need(self, did, program=None):
        """Für Auto-Start: mittlere Leistung (W), kWh und Dauer – nur wenn genug gelernt, sonst None."""
        e = self.data.get(did) or {}
        p = (e.get("programs") or {}).get(program) if program else None
        if p and p.get("runs", 0) >= MIN_RUNS:
            return {"watts": round(p["watts"]), "kwh": round(p["kwh"], 2), "minutes": round(p["minutes"]),
                    "runs": p["runs"], "program": program}
        if e.get("runs", 0) >= MIN_RUNS:
            return {"watts": round(e["watts"]), "kwh": round(e["kwh"], 2), "minutes": round(e["minutes"]),
                    "runs": e["runs"], "program": None}
        return None

    def report(self, sources=None):
        """Einträge für /api/consumers (gleiches Format wie die Lichter, plus Felder für Hausgeräte)."""
        sources = sources or {}
        out = []
        for did, e in self.data.items():
            runs, steps = e.get("runs", 0), e.get("steps") or []
            if not runs and not steps:
                continue
            src = sources.get(did) or {}
            on = bool(src.get("on")) if src else None
            watts = e.get("watts") if runs else e.get("stepW")
            item = {"id": did, "name": src.get("name") or e.get("name"), "kind": "appliance", "type": e.get("type"),
                    "brand": e.get("brand"), "watts": round(watts or 0), "samples": runs or len(steps), "on": on,
                    "nowWatts": round(watts or 0) if on else 0, "confidence": self.confidence(e),
                    "learnedFrom": e.get("source") or "Hausverbrauch"}
            if runs:
                item.update(kwhPerRun=round(e["kwh"], 2), minutes=round(e["minutes"]),
                            peakW=round(e["peakW"]) if e.get("peakW") else None,
                            programs=sorted([{"name": k, "runs": v["runs"], "kwh": round(v["kwh"], 2),
                                              "watts": round(v["watts"]), "minutes": round(v["minutes"])}
                                             for k, v in (e.get("programs") or {}).items()], key=lambda x: -x["runs"]))
            else:
                item["learnedFrom"] = "Schalten"
            out.append(item)
        return out
