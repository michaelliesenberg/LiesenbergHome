"""Fußgänger-Modus fürs Tor: feste Abfolge von Tor-Impulsen mit Wartezeiten, ausgeführt auf dem Hub.

Das Tor hat keine Rückmeldung (kein Endschalter) – darum läuft alles nach Zeit, genau wie man es von Hand drücken würde.
Die Abfolge steht in gate-pedestrian.json und ist einstellbar (Besitzer), z. B.:

  {"devices": {"open": "lx:<Tor außen öffnen>", "step": "lx:<Start/Stop/Auf>"},
   "steps": [{"pulse": "open"}, {"wait": 15}, {"pulse": "step"}, {"wait": 3.5}, {"pulse": "step"}]}

Torstellung schätzen (ab 2.44.0, „smart“, Standard an):
  Jeder Tor-Impuls, der über den Hub geht (App, Szene, Ankommen, Fußgänger-Ablauf), läuft durch GateModel.
  Aus Befehl + Zeit (volle Fahrt „travel“ = 40 s) schätzt der Hub, wo das Tor steht. Damit plant der Fußgänger-
  Ablauf direkt: Tor zu → nur kurz auf; Tor ganz offen → gleich zufahren (statt auf/Stop/Zu, was bei offenem Tor
  das Zufahren sofort wieder stoppte); unbekannt (z. B. Handsender, den der Hub nicht sieht) → erst sicher ganz
  auf, dann zu, dann Spalt. Die eingestellte Abfolge liefert nur noch die Spaltzeit (Warten nach dem letzten „öffnen“).

Schutz:
  • Es läuft immer nur eine Abfolge; währenddessen lehnt der Hub andere Tor-Befehle ab (busy()).
  • Schlägt ein Impuls fehl, wird abgebrochen (keine weiteren Impulse) und eine Mitteilung geschickt.
  • Während Panik (LUXOR) startet nichts – Panik schaltet „Pedestrien“ mit ein (23.10.: Tor ging dadurch auf).
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
POS_FILE = os.path.join(DATA_DIR, "gate-position.json")
TRAVEL = 40.0           # Sekunden für eine volle Fahrt (zu → ganz offen), Michael: „nach 40 s ist es offen“
MARGIN = 5.0            # Sicherheitszuschlag beim Zufahren


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
    travel = cfg.get("travel")
    travel = round(max(5.0, min(MAX_WAIT, float(travel))), 1) if isinstance(travel, (int, float)) else TRAVEL
    return {"devices": devs, "steps": steps, "confirmed": bool(cfg.get("confirmed")), "trigger": trig,
            "smart": cfg.get("smart") is not False, "travel": travel}


def gap_seconds(steps):
    """Spaltzeit = Warten nach dem letzten „öffnen“, wenn danach noch ein Impuls (Stopp) kommt – sonst 3,5 s."""
    last = max((i for i, s in enumerate(steps) if s.get("pulse") == "open"), default=None)
    if last is not None and last + 2 < len(steps) and "wait" in steps[last + 1] and "pulse" in steps[last + 2]:
        return steps[last + 1]["wait"]
    return 3.5


class GateModel:
    """Geschätzte Torstellung ohne Endschalter – nur aus Impulsen und Zeit.

    pos: 0 = zu … 1 = ganz offen, None = unbekannt.  dir: +1 fährt auf, -1 fährt zu, 0 steht, None = unbekannt.
    last: zuletzt gefahrene Richtung (für Start/Stop-Impulse: nach Stopp fährt das Tor andersherum).
    Impuls-Logik des Antriebs: „öffnen“ fährt immer auf (bei ganz offen: nichts); „step“ (Start/Stop/Auf) stoppt eine
    Fahrt, aus dem Stand fährt es andersherum als zuletzt – aus ganz offen zu, aus zu auf."""

    def __init__(self, travel_fn=lambda: TRAVEL, path=POS_FILE, clock=time.time):
        self.travel_fn, self.path, self.clock = travel_fn, path, clock
        self._lock = threading.RLock()
        self.s = {"pos0": None, "dir": None, "since": clock(), "last": None, "by": None}
        try:
            with open(path) as f:
                d = json.load(f)
            if isinstance(d, dict) and "dir" in d:
                self.s.update({k: d.get(k) for k in ("pos0", "dir", "since", "last", "by")})
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            with open(self.path + ".tmp", "w") as f:
                json.dump(self.s, f)
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            pass

    def _settle(self, now):
        """Fahrt zu Ende gerechnet? → als stehend an der Endlage merken."""
        s, T = self.s, self.travel_fn()
        if s["dir"] in (1, -1):
            el = max(0.0, now - (s["since"] or now))
            if s["pos0"] is None:
                if el >= T:                                   # volle Fahrtzeit vorbei → sicher in der Endlage
                    s.update(pos0=1.0 if s["dir"] > 0 else 0.0, last=s["dir"], dir=0, since=now)
            else:
                p = s["pos0"] + s["dir"] * el / T
                if (s["dir"] > 0 and p >= 1) or (s["dir"] < 0 and p <= 0):
                    s.update(pos0=1.0 if s["dir"] > 0 else 0.0, last=s["dir"], dir=0, since=now)

    def _pos(self, now):
        s = self.s
        if s["pos0"] is None:
            return None
        if s["dir"] in (1, -1):
            return max(0.0, min(1.0, s["pos0"] + s["dir"] * (now - s["since"]) / self.travel_fn()))
        return s["pos0"]

    def estimate(self, now=None):
        with self._lock:
            now = self.clock() if now is None else now
            self._settle(now)
            s, T = self.s, self.travel_fn()
            pos = self._pos(now)
            left = None                                       # Sekunden bis zur Endlage, falls in Fahrt
            if s["dir"] in (1, -1):
                el = now - s["since"]
                left = max(0.0, T - el) if pos is None else (1 - pos if s["dir"] > 0 else pos) * T
            known = pos is not None and s["dir"] is not None
            return {"pos": None if pos is None else round(pos, 3), "dir": s["dir"], "last": s["last"],
                    "left": None if left is None else round(left, 1), "known": known, "by": s["by"]}

    def observe(self, role, by="", now=None):
        """Ein Impuls ist rausgegangen (Rolle open/step/close/stop)."""
        with self._lock:
            now = self.clock() if now is None else now
            self._settle(now)
            s = self.s
            pos, d = self._pos(now), s["dir"]
            if role == "open":
                new = 0 if pos is not None and pos >= 1 and d == 0 else 1
            elif role == "close":
                new = 0 if pos is not None and pos <= 0 and d == 0 else -1
            elif role == "stop":
                new = 0
            elif role == "step":
                if d in (1, -1):
                    new = 0
                elif d == 0 and pos is not None and pos >= 1:
                    new = -1
                elif d == 0 and pos is not None and pos <= 0:
                    new = 1
                elif d == 0 and s["last"] in (1, -1):
                    new = -s["last"]
                else:
                    new = None                                # weiß nicht, was der Impuls bewirkt hat
            else:
                return
            if d in (1, -1):
                s["last"] = d
            if new is None:
                s.update(pos0=None, dir=None, since=now)
            else:
                if new in (1, -1) and d == new:
                    pos0, since = s["pos0"], s["since"]       # fährt schon so – nichts ändert sich
                else:
                    pos0, since = pos, now
                s.update(pos0=pos0, dir=new, since=since)
                if new in (1, -1):
                    s["last"] = new
            s["by"] = by or s["by"]
            self._save()

    def forget(self):
        with self._lock:
            self.s.update(pos0=None, dir=None, since=self.clock(), last=None)
            self._save()


def plan(est, travel, gap):
    """Fußgänger-Abfolge aus der geschätzten Torstellung → (Schritte, Begründung).
    Ziel immer: Tor zu, dann „gap“ Sekunden auf, Stopp."""
    T, M = travel, MARGIN
    tail = [{"pulse": "open"}, {"wait": gap}, {"pulse": "step"}]
    pos, d, left = est["pos"], est["dir"], est["left"]

    def w(x):
        return {"wait": round(max(0.2, min(MAX_WAIT, x)), 1)}

    def close_from_open(extra=0.0):
        return [{"pulse": "step"}, w(T + M + extra)] + tail
    # fährt gerade auf (auch bei unbekannter Stellung) und ist bald oben → abwarten, dann direkt zu
    if d == 1 and left is not None and (left <= 6 or not est["known"]):
        return [w(left + 1)] + close_from_open(), f"Tor fährt auf, in {left:.0f} s offen → abwarten, dann zu"
    if not est["known"]:
        # sicher: erst ganz auf (öffnen wirkt immer), dann zu, dann Spalt
        return [{"pulse": "open"}, w(T + 3)] + close_from_open(), "Torstellung unbekannt → erst ganz auf, dann zu"
    if d == 0 and pos <= 0.01:
        return list(tail), "Tor zu → direkt Spalt öffnen"
    if d == 0 and pos >= 0.99:
        return close_from_open(), "Tor ganz offen → direkt zufahren"
    if d == -1 and left is not None and left <= 6:
        return [w(left + 1)] + tail, f"Tor fährt zu, in {left:.0f} s zu → abwarten, dann Spalt"
    if d == 0 and est["last"] == 1:                           # steht nach Auffahrt → Start/Stop fährt zu
        return [{"pulse": "step"}, w(pos * T + M)] + tail, f"Tor steht bei {pos:.0%} → zufahren"
    # sonst (steht nach Zufahrt, fährt auf/zu): öffnen → Stopp → zu (wie der bisherige Ablauf)
    return ([{"pulse": "open"}, {"wait": 1.0}, {"pulse": "step"}, {"wait": 1.0}, {"pulse": "step"},
             w(min(1.0, pos + 3 / T) * T + M)] + tail, f"Tor bei {pos:.0%} → anhalten, zufahren")


class Pedestrian:
    def __init__(self, command_fn, notify_fn=None, sleep_fn=time.sleep, blocked_fn=None, model=None):
        self.command_fn, self.notify_fn, self.sleep_fn = command_fn, notify_fn, sleep_fn
        self.model = model or GateModel(lambda: self.load()["travel"])
        self.blocked_fn = blocked_fn          # → Grund, warum gerade gar nichts starten darf (z. B. Panik), oder None
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
        old = self.load()
        for k in ("smart", "travel"):                 # ältere Apps kennen die Felder nicht → beim Speichern behalten
            if isinstance(cfg, dict) and k not in cfg:
                cfg[k] = old[k]
        cfg = clean(cfg)
        with open(FILE + ".tmp", "w") as f:
            json.dump(cfg, f, indent=1)
        os.replace(FILE + ".tmp", FILE)
        return cfg

    def role_of(self, did, cfg=None):
        cfg = cfg or self.load()
        return next((r for r, d in cfg["devices"].items() if d == did), None)

    def observe_command(self, did, by=""):
        """Jeder Tor-Impuls über den Hub (App, Szene, Ankommen, eigener Ablauf) → Schätzung nachführen."""
        role = self.role_of(did)
        if role:
            self.model.observe(role, by)
            e = self.model.estimate()
            print(f"[TOR] Impuls {role} → geschätzt {_fmt(e)}", flush=True)

    def busy(self):
        return self._lock.locked()

    def problem(self, cfg=None):
        """Warum (noch) nicht startbar – oder None."""
        cfg = cfg or self.load()
        blocked = self.blocked_fn() if self.blocked_fn else None
        if blocked:
            return blocked
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
        why = "eingestellte Abfolge"
        if cfg["smart"]:
            est = self.model.estimate()
            steps, why = plan(est, cfg["travel"], gap_seconds(cfg["steps"]))
            cfg = {**cfg, "steps": steps}
            print(f"[TOR] Fußgänger: geschätzt {_fmt(est)} – {why}", flush=True)
        total = sum(s.get("wait", 0) for s in cfg["steps"])
        now = time.time()
        # Laufzustand für die App-Animation: Schritte, aktueller Schritt und seit wann (Hub-Uhrzeit)
        self.state = {"running": True, "run": int(now * 1000), "step": 0, "stepStarted": now, "by": who, "started": now,
                      "total": total, "steps": cfg["steps"], "why": why, "last": self.state.get("last")}
        threading.Thread(target=self._run, args=(cfg, who), daemon=True).start()
        return {"started": True, "seconds": total, "why": why}

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
                            self.sleep_fn(1)          # Panik schaltet „Pedestrien“ mit ein – Panik-Meldung abwarten
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
                self.state["stepStarted"] = time.time()
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
            self.state.update({"running": False, "step": None,
                               "last": {"at": time.time(), "result": result, "by": who, "run": self.state.get("run")}})
            print(f"[TOR] Fußgänger ({who}): {result}", flush=True)
            if result.startswith("Fehler"):
                self.model.forget()               # Impuls fehlgeschlagen → ob er wirkte, weiß der Hub nicht
            if result != "ok" and self.notify_fn:
                try:
                    self.notify_fn("Tor: Fußgänger-Ablauf " + ("abgebrochen" if result == "abgebrochen" else "fehlgeschlagen"),
                                   result + " – bitte am Tor nachsehen.")
                except Exception:
                    pass
            self._lock.release()


def _fmt(e):
    if not e["known"]:
        return "unbekannt" + (" (fährt auf)" if e["dir"] == 1 else "")
    what = {1: "fährt auf", -1: "fährt zu", 0: "steht"}[e["dir"]]
    return f"{e['pos']:.0%} offen, {what}"
