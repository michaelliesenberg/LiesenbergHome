"""Fußgänger-Modus fürs Tor: feste Abfolge von Tor-Impulsen mit Wartezeiten, ausgeführt auf dem Hub.

Das Tor hat keine Rückmeldung (kein Endschalter) – darum läuft alles nach Zeit, genau wie man es von Hand drücken würde.
Die Abfolge steht in gate-pedestrian.json und ist einstellbar (Besitzer), z. B.:

  {"devices": {"open": "lx:<Tor außen öffnen>", "step": "lx:<Start/Stop/Auf>"},
   "steps": [{"pulse": "open"}, {"wait": 15}, {"pulse": "step"}, {"wait": 3.5}, {"pulse": "step"}]}

Schutz:
  • Es läuft immer nur eine Abfolge; währenddessen lehnt der Hub andere Tor-Befehle ab (busy()).
  • Schlägt ein Impuls fehl, wird abgebrochen (keine weiteren Impulse) und eine Mitteilung geschickt.
  • „Abbrechen“ beendet nur das Warten – es wird kein zusätzlicher Impuls gesendet (was ein Impuls bewirkt,
    hängt von der Torstellung ab; das soll der Mensch am Tor entscheiden).
"""
import json
import os
import threading
import time

from hub_config import DATA_DIR

FILE = os.path.join(DATA_DIR, "gate-pedestrian.json")
MAX_WAIT = 120          # keine Wartezeit länger als 2 Minuten
MAX_STEPS = 12


def _default():
    return {"devices": {}, "steps": [{"pulse": "open"}, {"wait": 15}, {"pulse": "step"}, {"wait": 3.5}, {"pulse": "step"}],
            "confirmed": False}


def clean(cfg):
    """Nur erlaubte Felder, Zeiten begrenzen."""
    cfg = cfg if isinstance(cfg, dict) else {}
    devs = {k: str(v) for k, v in (cfg.get("devices") or {}).items() if k in ("open", "step", "close", "stop") and v}
    steps = []
    for s in (cfg.get("steps") or [])[:MAX_STEPS]:
        if isinstance(s, dict) and s.get("pulse") in ("open", "step", "close", "stop"):
            steps.append({"pulse": s["pulse"]})
        elif isinstance(s, dict) and isinstance(s.get("wait"), (int, float)):
            steps.append({"wait": round(max(0.2, min(MAX_WAIT, float(s["wait"]))), 1)})
    trig = str(cfg.get("trigger") or "") or None          # Gerät, dessen Einschalten den Ablauf startet (z. B. „Pedestrien“)
    return {"devices": devs, "steps": steps, "confirmed": bool(cfg.get("confirmed")), "trigger": trig}


class Pedestrian:
    def __init__(self, command_fn, notify_fn=None, sleep_fn=time.sleep):
        self.command_fn, self.notify_fn, self.sleep_fn = command_fn, notify_fn, sleep_fn
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self.state = {"running": False, "step": None, "last": None}

    def load(self):
        try:
            with open(FILE) as f:
                return clean(json.load(f))
        except (OSError, ValueError):
            return clean(_default())

    def save(self, cfg):
        cfg = clean(cfg)
        with open(FILE + ".tmp", "w") as f:
            json.dump(cfg, f, indent=1)
        os.replace(FILE + ".tmp", FILE)
        return cfg

    def busy(self):
        return self._lock.locked()

    def problem(self, cfg=None):
        """Warum (noch) nicht startbar – oder None."""
        cfg = cfg or self.load()
        if not cfg["confirmed"]:
            return "Fußgänger-Ablauf noch nicht bestätigt (Einstellungen → Tor)"
        if not cfg["steps"]:
            return "Kein Ablauf eingestellt"
        for s in cfg["steps"]:
            if "pulse" in s and not cfg["devices"].get(s["pulse"]):
                return f"Tor-Befehl „{s['pulse']}“ ist keinem Gerät zugeordnet"
        return None

    def start(self, who=""):
        cfg = self.load()
        err = self.problem(cfg)
        if err:
            raise ValueError(err)
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Fußgänger-Ablauf läuft schon")
        self._cancel.clear()
        total = sum(s.get("wait", 0) for s in cfg["steps"])
        self.state = {"running": True, "step": 0, "by": who, "started": time.time(), "total": total, "last": self.state.get("last")}
        threading.Thread(target=self._run, args=(cfg, who), daemon=True).start()
        return {"started": True, "seconds": total}

    def cancel(self):
        self._cancel.set()

    # ---- Auslöser: Einschalten eines Geräts (Taster „Garage T4“ / Szene schaltet „Pedestrien“ an)
    def watch(self, read_fn, interval=0.5):
        """read_fn(device_id) → True/False/None. Startet den Ablauf bei „aus → an“ (nicht beim automatischen Ausgehen,
        nicht beim Hub-Start, wenn er schon an ist). Läuft schon ein Ablauf, wird das Einschalten ignoriert."""
        def loop():
            prev, prev_id = None, None
            while True:
                try:
                    trig = self.load().get("trigger")
                    if trig != prev_id:
                        prev, prev_id = None, trig
                    if trig:
                        on = read_fn(trig)
                        if on is True and prev is False:
                            if self.busy():
                                print("[TOR] Fußgänger-Taster: Ablauf läuft schon – ignoriert", flush=True)
                            elif self.problem():
                                print(f"[TOR] Fußgänger-Taster: {self.problem()}", flush=True)
                            else:
                                self.start("Taster/Pedestrien")
                        if on is not None:
                            prev = on
                except Exception as e:
                    print(f"[TOR] Fußgänger-Taster: {e}", flush=True)
                    time.sleep(5)
                self.sleep_fn(interval)
        threading.Thread(target=loop, daemon=True).start()

    def _run(self, cfg, who):
        result = "ok"
        try:
            for i, s in enumerate(cfg["steps"]):
                self.state["step"] = i
                if self._cancel.is_set():
                    result = "abgebrochen"
                    break
                if "pulse" in s:
                    did = cfg["devices"][s["pulse"]]
                    try:
                        self.command_fn(did, {"trigger": True})
                    except Exception as e:
                        result = f"Fehler bei Schritt {i + 1} ({s['pulse']}): {e}"
                        break
                else:
                    end = time.time() + s["wait"]
                    while time.time() < end and not self._cancel.is_set():
                        self.sleep_fn(min(0.1, max(0.0, end - time.time())))
        finally:
            self.state.update({"running": False, "step": None, "last": {"at": time.time(), "result": result, "by": who}})
            print(f"[TOR] Fußgänger ({who}): {result}", flush=True)
            if result != "ok" and self.notify_fn:
                try:
                    self.notify_fn("Tor: Fußgänger-Ablauf " + ("abgebrochen" if result == "abgebrochen" else "fehlgeschlagen"),
                                   result + " – bitte am Tor nachsehen.")
                except Exception:
                    pass
            self._lock.release()
