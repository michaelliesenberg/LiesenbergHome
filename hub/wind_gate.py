"""Windalarm → Tor zu (Michael 17.11.).

Die KNX-Wetterstation meldet Windalarm (IP1-Datenpunkte 2 „Windalarm 1“ und 3 „Windalarm 2“, Gruppenadressen 10953/10954,
dieselben, auf die die Jalousien und Markisen mit „Teilnahme Windalarm“ hochfahren). Sobald einer davon angeht:

  1. Mitteilung an alle (Abo wx_gust „Sturmböen“): starker Wind, Tor wird geschlossen.
  2. Tor zu – Michaels schnellster Weg, egal ob Fußgänger-Spalt, halb offen oder in Fahrt:
       „öffnen“ → 1,5 s → Start/Stop (hält an) → 2 s → Start/Stop (fährt zu, weil zuletzt aufwärts) – dann stehen lassen.
     Nur wenn die Schätzung sagt „steht ganz offen“, reicht ein Start/Stop (dort wirkt „öffnen“ nicht, Michaels Folge
     würde das Tor nur kurz zu- und wieder anhalten lassen).
  3. Merken: hat der Wind das Tor geschlossen und seitdem hat niemand das Tor über Hub oder Wandtaster bewegt,
     beim nächsten Windalarm nicht noch einmal (Handsender sieht der Hub nicht).

Nach dem Windalarm geht das Tor nicht wieder auf. Einstellung in config.json → "wind_gate":
  {"enabled": true, "gates": ["sued"]}     # sued = Tor innen (Michael 17.11.: nur Süd), nord = Tor außen
Zustand in DATA_DIR/wind-gate.json.
"""
import json
import os
import threading
import time

from hub_config import DATA_DIR
import gate_ped

FILE = os.path.join(DATA_DIR, "wind-gate.json")
ALARM_DPS = (2, 3)                       # Wetterstation 1 C1/C2 „Windalarm“ (house_server._WEATHER_DP)
POLL = 5.0
QUIET = 1800.0                           # Alarm flattert, Tor schon zu → höchstens alle 30 min eine Mitteilung
STEPS = [{"pulse": "open"}, {"wait": 1.5}, {"pulse": "step"}, {"wait": 2.0}, {"pulse": "step"}]
DEFAULT = {"enabled": True, "gates": ["sued"]}


def plan(est, memo_seen):
    """→ (Schritte, Begründung). memo_seen = Zeitstempel 'seen' der Schätzung direkt nach dem letzten Wind-Schließen."""
    if memo_seen is not None and est.get("seen") == memo_seen:
        return [], "schon beim letzten Windalarm geschlossen, seitdem nicht bewegt"
    if est["known"] and est["dir"] == 0 and est["pos"] is not None:
        if est["pos"] <= 0.01 and est.get("age") is not None and est["age"] <= gate_ped.STALE:
            return [], "ist gerade zugefahren"
        if est["pos"] >= 0.99:
            return [{"pulse": "step"}], "steht ganz offen → Start/Stop fährt zu"
    if est["dir"] == -1 and est["known"]:
        return [], "fährt schon zu"
    return list(STEPS), "öffnen, anhalten, zufahren"


class WindGate:
    def __init__(self, gates, read_fn, push_fn, cfg_fn, sleep_fn=time.sleep, clock=time.time):
        self.gates, self.read_fn, self.push_fn, self.cfg_fn = gates, read_fn, push_fn, cfg_fn
        self.sleep_fn, self.clock = sleep_fn, clock
        self._lock = threading.Lock()
        self.s = {"alarm": None, "since": None, "closed": {}, "last": None, "pushed": None}
        try:
            with open(FILE) as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.s.update({k: d[k] for k in ("since", "closed", "last", "pushed") if k in d})
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            with open(FILE + ".tmp", "w") as f:
                json.dump(self.s, f, ensure_ascii=False)
            os.replace(FILE + ".tmp", FILE)
        except OSError:
            pass

    def cfg(self):
        c = dict(DEFAULT)
        try:
            c.update(self.cfg_fn() or {})
        except Exception:
            pass
        return c

    def read_alarm(self):
        """→ True/False, None = nicht lesbar."""
        vals = []
        for dp in ALARM_DPS:
            try:
                vals.append(self.read_fn(dp))
            except Exception:
                vals.append(None)
        if any(v for v in vals if v is not None):
            return True
        return False if any(v is not None for v in vals) else None

    # ---- Ablauf
    def check(self, alarm):
        """Neuer Messwert. Nur die steigende Flanke (auch nach Hub-Neustart mit Alarm an) löst aus."""
        prev = self.s["alarm"]
        if alarm is None:
            return None
        self.s["alarm"] = alarm
        if alarm and prev is not True:
            self.s["since"] = self.clock()
            self._save()
            print("[WIND] Windalarm der Wetterstation", flush=True)
            if self.cfg().get("enabled", True):
                return self.trigger("Windalarm")
            print("[WIND] Tor-Automatik aus (wind_gate.enabled)", flush=True)
        elif not alarm and prev:
            print("[WIND] Windalarm vorbei – Tor bleibt, wie es ist", flush=True)
        return None

    def trigger(self, by="Windalarm"):
        """Mitteilung + Tore schließen (läuft im Hintergrund). → {key: Begründung}"""
        gates = self.gates.gates()
        keys = [k for k in self.cfg().get("gates", []) if k in gates]
        out = {}
        todo = []
        for k in keys:
            g = gates[k]
            est = self.gates.model(g).estimate()
            memo = (self.s["closed"].get(k) or {}).get("seen")
            steps, why = plan({**est, "seen": self.gates.model(g).s.get("seen")}, memo)
            out[k] = why
            print(f"[WIND] {g['name']}: geschätzt {gate_ped._fmt(est)} – {why}", flush=True)
            if steps:
                todo.append((k, g, steps))
        names_do = [g["name"] for _, g, _ in todo]
        names_ok = [gates[k]["name"] for k in keys if k not in [t[0] for t in todo]]
        body = "Die Wetterstation meldet Windalarm, Jalousien fahren hoch."
        if names_do:
            body += f" {' und '.join(names_do)} wird geschlossen."
        elif names_ok:
            body += f" {' und '.join(names_ok)} ist schon zu."
        last = self.s.get("last") or {}
        if todo or self.clock() - (last.get("pushed") or 0) > QUIET:
            try:
                self.push_fn("Starker Wind", body)
                self.s["pushed"] = self.clock()
            except Exception as e:
                print(f"[WIND] Mitteilung: {e}", flush=True)
        else:
            print("[WIND] Tor schon zu, Mitteilung vor Kurzem – keine neue", flush=True)
        self.s["last"] = {"at": self.clock(), "by": by, "result": out, "pushed": self.s.get("pushed")}
        self._save()
        for k, g, steps in todo:
            threading.Thread(target=self._close, args=(k, g, steps), daemon=True).start()
        return out

    def _close(self, k, g, steps):
        with self._lock:
            for _ in range(60):                         # Fußgänger-Ablauf am selben Tor erst fertig laufen lassen
                if not (self.gates.busy_fn and self.gates.busy_fn(g)):
                    break
                self.sleep_fn(2)
            lock = self.gates._locks.setdefault(k, threading.Lock())
            if not lock.acquire(timeout=120):
                print(f"[WIND] {g['name']}: Tor ist belegt – nicht geschlossen", flush=True)
                return
            try:
                for s in steps:
                    if "pulse" in s:
                        self.gates.command_fn(g[s["pulse"]], {"trigger": True})   # durch den Hub → Schätzung
                    else:
                        self.sleep_fn(s["wait"])
                seen = self.gates.model(g).s.get("seen")
                self.s["closed"][k] = {"at": self.clock(), "seen": seen}
                self._save()
                print(f"[WIND] {g['name']}: zu ({len([s for s in steps if 'pulse' in s])} Impulse)", flush=True)
            except Exception as e:
                print(f"[WIND] {g['name']}: {e}", flush=True)
            finally:
                lock.release()

    def loop(self, interval=POLL):
        def run():
            time.sleep(20)
            while True:
                try:
                    self.check(self.read_alarm())
                except Exception as e:
                    print(f"[WIND] {e}", flush=True)
                time.sleep(interval)
        threading.Thread(target=run, daemon=True).start()

    def info(self):
        return {"alarm": self.s["alarm"], "since": self.s["since"], "config": self.cfg(), "last": self.s["last"],
                "closed": self.s["closed"]}
