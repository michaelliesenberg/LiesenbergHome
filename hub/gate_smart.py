"""Tor Süd / Tor Nord „zu“ – schlau, mit der geschätzten Torstellung aus gate_ped.GateModel.

Jedes Tor hat zwei LUXOR-Impuls-Ausgänge: „öffnen“ (fährt immer auf, bei offenem Tor wirkungslos) und „schließen“
(eigentlich Start/Stop/Zu: stoppt eine Fahrt, aus dem Stand fährt es andersherum als zuletzt).

  zu:  aus der Schätzung – offen → ein Start/Stop-Impuls; fährt auf → stoppen und zufahren; schon zu oder fährt
       schon zu → nichts; Stellung unsicher (lange kein Hub-Befehl, Handsender, Zentral aus) → vermutlich zu, nichts.

Für Szenen und Siri gibt es je Tor ein Gerät (Typ Tor, nur mit Tor-Recht): gate:<key>:zu.
„Auf“ braucht es nicht (Michael 31.10.): der „öffnen“-Ausgang wirkt immer richtig.
Einstellung in gate-smart.json (wird beim ersten Mal aus den LUXOR-Namen „Tor außen/innen öffnen/schließen“ belegt):

  {"gates": {"sued": {"name": "Tor Süd", "open": "lx:…", "step": "lx:…", "travel": 40}, "nord": {…}},
   "scenes": true}       # Szenen „Tor Süd zu“, „Tor Nord zu“ einmal angelegt
"""
import json
import os
import threading
import time

from hub_config import DATA_DIR
import gate_ped

FILE = os.path.join(DATA_DIR, "gate-smart.json")
PREFIX = "gate:"
# Welche LUXOR-Tore Süd und Nord sind (Name enthält dieses Wort + „öffnen“/„schließen“)
SIDES = {"nord": ("Tor Nord", "außen"), "sued": ("Tor Süd", "innen")}     # Michael 2.11.: Nord = außen, Süd = innen


def plan_close(est, travel, stale=gate_ped.STALE):
    """„Tor zu“ aus der Schätzung → (Impulse/Wartezeiten, Begründung)."""
    pos, d, age = est["pos"], est["dir"], est.get("age")
    if d == -1:
        return [], "fährt schon zu"
    if d == 1:
        return [{"pulse": "step"}, {"wait": 1.0}, {"pulse": "step"}], "fährt auf → anhalten, zufahren"
    if not est["known"] or age is None or age > stale:
        # Michael 2.11.: unsicher → annehmen, dass es schon zu ist (meistens so; wie beim Fußgänger-Ablauf).
        # Früher: erst ganz auf, dann zu – öffnete geschlossene Tore unnötig.
        return [], "Stellung unsicher → vermutlich schon zu, nichts tun"
    if pos <= 0.01:
        return [], "ist schon zu"
    if est["last"] == 1 or pos >= 0.99:
        return [{"pulse": "step"}], f"steht bei {pos:.0%} → zufahren"
    # steht nach einer Zufahrt halb offen: Start/Stop würde auffahren → öffnen, anhalten, zufahren
    return ([{"pulse": "open"}, {"wait": 1.0}, {"pulse": "step"}, {"wait": 1.0}, {"pulse": "step"}],
            f"steht bei {pos:.0%} → anhalten, zufahren")


class SmartGates:
    def __init__(self, command_fn, struct_fn=None, busy_fn=None, sleep_fn=time.sleep):
        self.command_fn, self.struct_fn, self.sleep_fn = command_fn, struct_fn, sleep_fn
        self.busy_fn = busy_fn                 # → True, wenn der Fußgänger-Ablauf läuft (gleiches Tor nicht stören)
        self._locks = {}
        self._sent = {}                        # Ausgang → Zeit des letzten Hub-Impulses (eigene Impulse beim Mitlesen ignorieren)

    # ---- Einstellung
    def load(self):
        try:
            with open(FILE) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        cfg.setdefault("gates", {})
        if not cfg["gates"] and self.struct_fn:
            found = self._detect()
            if found:
                cfg["gates"] = found
                self.save(cfg)
                print(f"[TOR] Tor Süd/Nord aus LUXOR-Namen belegt: {found}", flush=True)
        return cfg

    def save(self, cfg):
        with open(FILE + ".tmp", "w") as f:
            json.dump(cfg, f, indent=1, ensure_ascii=False)
        os.replace(FILE + ".tmp", FILE)

    def _detect(self):
        from devices import device_index
        try:
            idx = device_index(self.struct_fn())
        except Exception:
            return {}
        out = {}
        for key, (name, word) in SIDES.items():
            g = {"name": name, "travel": gate_ped.TRAVEL}
            for did, d in idx.items():
                n = d["name"].lower()
                if d["kind"] == "gate" and word in n and "tor" in n:
                    if "öffnen" in n:
                        g["open"] = did
                    elif "schließen" in n:
                        g["step"] = did
            if g.get("open") and g.get("step"):
                out[key] = g
        return out

    def gates(self):
        return {k: g for k, g in self.load()["gates"].items() if g.get("open") and g.get("step")}

    def model(self, g):
        return gate_ped.model_for(g["open"], lambda: float(g.get("travel") or gate_ped.TRAVEL))

    # ---- Schätzung nachführen (jeder Tor-Impuls über den Hub)
    def observe(self, did, by=""):
        """→ True, wenn did zu einem Tor Süd/Nord gehört (dann ist die Schätzung schon nachgeführt)."""
        for g in self.gates().values():
            role = "open" if did == g["open"] else "step" if did == g["step"] else None
            if role:
                self._sent[did] = time.time()
                m = self.model(g)
                m.observe(role, by)
                print(f"[TOR] {g['name']}: Impuls {role} → geschätzt {gate_ped._fmt(m.estimate())}", flush=True)
                return True
        return False

    # ---- Wandtaster / LUXOR-Szenen mitlesen (7.11.: Status-Datenpunkt geht pro Druck ~0,5–1 s auf an)
    def watch(self, read_fn, interval=0.25, own_window=3.0):
        """read_fn(device_id) → True/False/None (StatusOnOff). Jede steigende Flanke an einem Tor-Ausgang, die nicht
        von einem Hub-Impuls kommt, ist ein Tastendruck am Haus → Schätzung nachführen. Den Handsender sieht man so nicht."""
        def loop():
            prev = {}
            while True:
                try:
                    for g in self.gates().values():
                        for role in ("open", "step"):
                            did = g[role]
                            on = read_fn(did)
                            if on is True and prev.get(did) is False:
                                if time.time() - self._sent.get(did, 0) > own_window:
                                    m = self.model(g)
                                    m.observe(role, "Taster")
                                    print(f"[TOR] {g['name']}: Taster {role} → geschätzt {gate_ped._fmt(m.estimate())}",
                                          flush=True)
                            if on is not None:
                                prev[did] = on
                except Exception as e:
                    print(f"[TOR] Taster mitlesen: {e}", flush=True)
                    time.sleep(5)
                time.sleep(interval)
        threading.Thread(target=loop, daemon=True).start()

    def note_sent(self, did):
        """Der Hub schickt gleich selbst einen Impuls an did (vor dem Schreiben aufrufen)."""
        self._sent[did] = time.time()

    def forget_all(self):
        for g in self.gates().values():
            self.model(g).forget()

    # ---- Geräte für Szenen/Siri
    def virtual(self):
        """[(Raum-Gerät „öffnen“, virtuelles Gerät)] – kommen in den Raum des Tors."""
        out = []
        for key, g in self.gates().items():
            out.append((g["open"], {"id": f"{PREFIX}{key}:zu", "name": f"{g['name']} zu",
                                    "kind": "gate", "restricted": True}))
        return out

    def command(self, vid, by=""):
        try:
            _, key, act = vid.split(":")
            g = self.gates()[key]
        except (ValueError, KeyError):
            raise KeyError(vid)
        if act != "zu":
            raise KeyError(vid)
        if self.busy_fn and self.busy_fn(g):
            raise RuntimeError("Fußgänger-Ablauf läuft gerade – bitte warten")
        lock = self._locks.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            raise RuntimeError(f"{g['name']}: läuft schon")
        m = self.model(g)
        est = m.estimate()
        steps, why = plan_close(est, float(g.get("travel") or gate_ped.TRAVEL))
        print(f"[TOR] {g['name']} {act} ({by or 'Befehl'}): geschätzt {gate_ped._fmt(est)} – {why}", flush=True)
        if not any("wait" in s for s in steps):
            try:
                self._run(g, steps)
            finally:
                lock.release()
        else:
            def bg():
                try:
                    self._run(g, steps)
                except Exception as e:
                    print(f"[TOR] {g['name']} {act}: {e}", flush=True)
                finally:
                    lock.release()
            threading.Thread(target=bg, daemon=True).start()
        return {"ok": True, "why": why, "seconds": sum(s.get("wait", 0) for s in steps)}

    def _run(self, g, steps):
        for s in steps:
            if "pulse" in s:
                self.command_fn(g[s["pulse"]], {"trigger": True})     # läuft durch den Hub → observe()
            else:
                self.sleep_fn(s["wait"])

    # ---- Szenen „Tor Süd zu“, „Tor Nord zu“ einmal anlegen (danach frei änderbar oder löschbar)
    def seed_scenes(self, scenes):
        cfg = self.load()
        if cfg.get("scenes") or not self.gates():
            return False
        names = {s.get("name", "").strip().lower() for s in scenes.scenes}
        icon = next((s.get("icon") for s in scenes.scenes if "tor" in s.get("name", "").lower() and s.get("icon")), None)
        for _, v in self.virtual():
            if v["name"].lower() not in names:
                sc = {"name": v["name"], "actions": [{"fid": v["id"], "name": v["name"], "on": True}]}
                if icon:
                    sc["icon"] = icon
                scenes.upsert(sc)
                print(f"[TOR] Szene „{v['name']}“ angelegt", flush=True)
        cfg["scenes"] = True
        self.save(cfg)
        return True
