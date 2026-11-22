"""Tor-Pico (W5500-LAN-Pico an der Torsteuerung) als echte Rückmeldung fürs Tor – NUR LESEN.

Befehle (öffnen / Start-Stop / Fußgänger) gehen weiter über KNX/LUXOR wie bisher. Der Hub fragt am Pico nur
GET <url>/status ab (etwa jede Sekunde) – nie /command/… oder /test/….

Pico-Firmware 2.0.x liefert (gate_control.status()):
  {"version": "2.0.3", "position": "open|closed|between|opening|closing|error_both_limits",
   "fully_open": bool, "fully_closed": bool, "motor": "opening"|"closing"|"both?"|null,
   "photocell_blocked": bool, "last_cmd": …, "raw": {"gp9": 0/1, …}, "pedestrian_busy": bool, …}

Daraus macht der Hub:
  position: open | closed | between | error | unknown      motion: opening | closing | stopped | unknown
  photocell: clear | blocked
und führt damit die Torstellung (gate_ped.GateModel) nach: an einer Endlage ist die Stellung exakt (auch nach langer
Zeit nicht „veraltet“, solange der Pico online ist), bei laufendem Motor ist die Richtung bekannt – auch wenn der
Handsender oder ein Taster das Tor bewegt hat. Fußgänger-Ablauf, „Tor zu“ und Windalarm planen dann mit der echten
Stellung. Ist der Pico offline oder unklar (z. B. beide Endlagen), gilt die bisherige Schätzung.

Einstellung in config.json (Standard aus – andere Installationen haben keinen Tor-Pico):
  "gate_pico": {"enabled": true, "url": "http://192.168.178.54", "gate": "sued", "use_for_gate": false,
                "push_photocell": false}
  enabled = nur abfragen und unter /api/gate/pico zeigen (Test). Erst use_for_gate = true lässt die Rückmeldung in die
  Torstellung (Fußgänger, Tor zu, Windalarm) einfließen – Michael 21.11.: erst testen, nichts ersetzen.
"""
import json
import threading
import time
import urllib.request

DEFAULT = {"enabled": False, "url": "", "gate": "sued", "interval": 1.0, "timeout": 2.0, "push_photocell": False,
           "use_for_gate": False}
FRESH = 5.0              # so lange gilt die letzte Antwort als aktuell
OFFLINE_AFTER = 3        # so viele Fehlversuche hintereinander → offline
BACKOFF = (1, 2, 5, 10, 30)
EVENTS = 30

TEXT = {"open": "Offen", "closed": "Geschlossen", "between": "Zwischenstellung", "error": "Rückmeldung unklar",
        "unknown": "unbekannt"}
MOTION_TEXT = {"opening": "Öffnet…", "closing": "Schließt…"}


def _fetch(url, timeout):
    req = urllib.request.Request(url.rstrip("/") + "/status", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def derive(st):
    """Pico-/status → {position, motion, photocell}."""
    st = st if isinstance(st, dict) else {}
    o, c = st.get("fully_open"), st.get("fully_closed")
    raw_pos = st.get("position")
    if o is None and c is None:                       # ältere Firmware ohne die Einzelfelder
        o, c = raw_pos == "open", raw_pos == "closed"
    if raw_pos == "error_both_limits" or (o and c):
        position = "error"
    elif o:
        position = "open"
    elif c:
        position = "closed"
    elif raw_pos in ("between", "opening", "closing"):
        position = "between"
    else:
        position = "unknown"
    m = st.get("motor")
    if m is None and raw_pos in ("opening", "closing"):
        m = raw_pos
    motion = m if m in ("opening", "closing") else "stopped" if m is None else "unknown"
    pb = st.get("photocell_blocked")
    photocell = None if pb is None else ("blocked" if pb else "clear")
    return {"position": position, "motion": motion, "photocell": photocell}


def feedback_of(d):
    """Abgeleiteter Zustand → Rückmeldung fürs GateModel {"pos": 0/1/None, "dir": 1/-1/0} oder None (unklar)."""
    if d["position"] == "error" or d["motion"] == "unknown":
        return None
    if d["motion"] == "opening":
        return {"pos": None, "dir": 1}
    if d["motion"] == "closing":
        return {"pos": None, "dir": -1}
    if d["position"] == "open":
        return {"pos": 1.0, "dir": 0}
    if d["position"] == "closed":
        return {"pos": 0.0, "dir": 0}
    if d["position"] == "between":
        return {"pos": None, "dir": 0}
    return None


def text_of(d):
    if d["motion"] in MOTION_TEXT:
        return MOTION_TEXT[d["motion"]]
    return TEXT.get(d["position"], "unbekannt")


class GatePico:
    def __init__(self, cfg_fn, model_fn, push_fn=None, fetch=_fetch, clock=time.time, sleep_fn=time.sleep):
        """cfg_fn() → config.json „gate_pico“; model_fn(gate_key) → (GateModel, Tor-Name) oder (None, None)."""
        self.cfg_fn, self.model_fn, self.push_fn = cfg_fn, model_fn, push_fn
        self.fetch, self.clock, self.sleep_fn = fetch, clock, sleep_fn
        self._lock = threading.RLock()
        self.status = None            # letzte Pico-Antwort (roh)
        self.derived = None
        self.last_ok = None
        self.fails = 0
        self.error = None
        self.last_change = None
        self.events = []
        self._model = None
        self._closing_at = None       # zuletzt „schließt“ gesehen (für die Lichtschranken-Mitteilung)

    def cfg(self):
        c = dict(DEFAULT)
        try:
            c.update(self.cfg_fn() or {})
        except Exception:
            pass
        return c

    def active(self):
        c = self.cfg()
        return bool(c.get("enabled") and c.get("url"))

    def online(self, now=None):
        now = self.clock() if now is None else now
        return bool(self.active() and self.last_ok and self.fails < OFFLINE_AFTER and now - self.last_ok <= FRESH)

    def feedback(self):
        """Für GateModel.estimate(): echte Stellung, solange der Pico online ist und eine klare Antwort hat."""
        with self._lock:
            if not self.online() or not self.derived:
                return None
            return feedback_of(self.derived)

    # ---- Abfragen
    def _attach(self):
        """Rückmeldung an die Torstellung des eingestellten Tors hängen (Fußgänger-Ablauf und Tor Süd/Nord teilen sie)."""
        try:
            model, _ = self.model_fn(self.cfg().get("gate") or "sued")
        except Exception:
            model = None
        if model is not self._model:
            if self._model is not None and getattr(self._model, "feedback", None) == self.feedback:
                self._model.feedback = None
            self._model = model
            if model is not None:
                model.feedback = self.feedback
        return model

    def poll_once(self):
        """Eine Abfrage. → True bei Erfolg."""
        c = self.cfg()
        if not self.active():
            if self._model is not None:
                self._model.feedback = None
                self._model = None
            return False
        if c.get("use_for_gate"):
            model = self._attach()
        else:
            model = None
            if self._model is not None:                  # nur Test: Torstellung bleibt wie ohne Pico
                if getattr(self._model, "feedback", None) == self.feedback:
                    self._model.feedback = None
                self._model = None
        try:
            st = self.fetch(c["url"], float(c.get("timeout") or 2.0))
            if not isinstance(st, dict):
                raise ValueError("keine JSON-Antwort")
        except Exception as e:
            with self._lock:
                was = self.online()
                self.fails += 1
                self.error = str(e)[:160]
                if was and not self.online():
                    self._event("offline", self.error)
                    print(f"[TOR-PICO] offline: {self.error}", flush=True)
            return False
        now = self.clock()
        with self._lock:
            was_online = self.online(now)
            prev = self.derived
            d = derive(st)
            self.status, self.derived, self.last_ok, self.fails, self.error = st, d, now, 0, None
            if not was_online:
                self._event("online", f"Firmware {st.get('version') or '?'}")
                print(f"[TOR-PICO] online, Firmware {st.get('version')}: {text_of(d)}", flush=True)
            if d["motion"] == "closing":
                self._closing_at = now
            changed = prev is None or (prev["position"], prev["motion"]) != (d["position"], d["motion"])
            if changed:
                self.last_change = now
                if prev is not None:
                    self._event("position", text_of(d))
                    print(f"[TOR-PICO] {text_of(d)}", flush=True)
                fb = feedback_of(d)
                if model is not None and fb is not None:
                    try:
                        model.sync(fb["pos"], fb["dir"], "Tor-Pico")
                    except Exception as e:
                        print(f"[TOR-PICO] Torstellung: {e}", flush=True)
            if prev is not None and prev.get("photocell") != d["photocell"] and d["photocell"]:
                self._event("photocell", "Lichtschranke unterbrochen" if d["photocell"] == "blocked" else "Lichtschranke frei")
                if d["photocell"] == "blocked":
                    closing = self._closing_at is not None and now - self._closing_at <= 3.0
                    print(f"[TOR-PICO] Lichtschranke unterbrochen{' beim Zufahren' if closing else ''}", flush=True)
                    if closing and c.get("push_photocell") and self.push_fn:
                        try:
                            self.push_fn("Tor: Lichtschranke unterbrochen",
                                         "Beim Zufahren war etwas in der Lichtschranke – das Tor hat angehalten.")
                        except Exception as e:
                            print(f"[TOR-PICO] Mitteilung: {e}", flush=True)
        return True

    def _event(self, kind, text):
        self.events.append({"at": self.clock(), "kind": kind, "text": text})
        del self.events[:-EVENTS]

    def loop(self):
        def run():
            while True:
                ok = False
                try:
                    ok = self.poll_once()
                except Exception as e:
                    print(f"[TOR-PICO] {e}", flush=True)
                c = self.cfg()
                if not self.active():
                    wait = 10.0
                elif ok or self.fails == 0:
                    wait = max(0.5, float(c.get("interval") or 1.0))
                else:
                    wait = float(BACKOFF[min(len(BACKOFF) - 1, max(0, self.fails - 1))])
                self.sleep_fn(wait)
        threading.Thread(target=run, daemon=True, name="gate-pico").start()

    # ---- für die App
    def info(self, estimate=None, gate_name=None, devices=None):
        with self._lock:
            c = self.cfg()
            if not self.active():
                return {"enabled": False}
            on = self.online()
            d = self.derived or {"position": "unknown", "motion": "unknown", "photocell": None}
            st = self.status or {}
            out = {"enabled": True, "online": on, "gate": c.get("gate"), "gateName": gate_name, "devices": devices or [],
                   "url": c.get("url"), "error": self.error, "lastOk": self.last_ok, "lastChange": self.last_change,
                   "now": self.clock(), "events": list(self.events[-10:]), "estimate": estimate,
                   "pushPhotocell": bool(c.get("push_photocell")), "useForGate": bool(c.get("use_for_gate"))}
            if on:
                out.update({"position": d["position"], "motion": d["motion"], "photocell": d["photocell"],
                            "text": text_of(d), "version": st.get("version"), "raw": st.get("raw"),
                            "fullyOpen": st.get("fully_open"), "fullyClosed": st.get("fully_closed"),
                            "motor": st.get("motor"), "picoPosition": st.get("position"),
                            "pedestrianBusy": st.get("pedestrian_busy"), "lastCmd": st.get("last_cmd")})
            else:
                out.update({"position": "unknown", "motion": "unknown", "photocell": None, "text": "Tor-Pico offline",
                            "version": st.get("version")})
            return out
