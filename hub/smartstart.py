"""Smart-Start (Stufe 2): Wasch-, Spül- und Trockenmaschine automatisch zur besten Zeit starten.

Ablauf: Gerät beladen, Programm wählen, am Gerät „Fernstart" aktivieren → in der App „Automatisch starten".
Der Planer schätzt die beste Zeit (Prognose). Gestartet wird aber nach der ECHTEN Sonne: im Fenster ±2 h um diese
Zeit, sobald 5 Minuten lang genug Überschuss da ist (Einspeisung + Akku-Laden ≥ Leistung des Geräts). Kommt bis
zum Fensterende nicht genug Sonne, startet das Gerät spätestens dann (bzw. so, dass „fertig bis …" klappt).
Start über Home Connect, Miele (MobileStart) oder LG ThinQ (Fernstart). Aufträge liegen in DATA_DIR/smartstart.json (übersteht Neustarts).
"""
import json
import os
import threading
import time

from hub_config import DATA_DIR

FILE = os.path.join(DATA_DIR, "smartstart.json")
PROFILE = {"Washer": "washer", "WasherDryer": "washer", "Dishwasher": "dishwasher", "Dryer": "dryer"}
WATTS = {"washer": 800, "dishwasher": 700, "dryer": 1500}          # mittlere Leistung (wie planner.APPLIANCES)
MINUTES = {"washer": 120, "dishwasher": 150, "dryer": 120}
WINDOW = 2 * 3600                  # ± 2 Stunden um die geschätzte beste Zeit
SUSTAIN = 10                       # 10 Messungen à 30 s = 5 Minuten genug Überschuss


def label(a):
    """Name mit Marke („Geschirrspüler · Bosch"), damit zwei gleiche Geräte unterscheidbar sind"""
    name, brand = a.get("name") or a.get("typeText") or "Gerät", a.get("brand")
    return f"{name} · {brand}" if brand and brand.lower() not in name.lower() else name


class SmartStart:
    def __init__(self, homeconnect, plan_fn, miele=None):
        self.hc = homeconnect
        self.miele = miele                # Miele: Start über MobileStart (processAction 1)
        self.lg = None                    # LG ThinQ (wird in house_server gesetzt)
        self.surplus_fn = None            # → aktueller Überschuss in W (Einspeisung + Akku-Laden), in house_server gesetzt
        self._surplus = []
        self.plan_fn = plan_fn            # (profile_id, deadline) → {"start", "end", "reason", "saving", ...}
        self._lock = threading.Lock()
        self.jobs = self._load()
        threading.Thread(target=self._loop, daemon=True).start()

    def _load(self):
        try:
            with open(FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.jobs, f)
        os.replace(tmp, FILE)

    def devices(self):
        out = []
        apps = list(self.hc.list())
        if self.miele is not None:
            apps += [dict(a, remoteStart=a.get("mobileStart", False)) for a in self.miele.list()]
        if self.lg is not None:
            apps += self.lg.list()
        for a in apps:
            prof = PROFILE.get(a.get("type"))
            if prof:
                out.append({"id": a["id"], "name": label(a), "brand": a.get("brand"), "type": a.get("type"),
                            "profile": prof, "connected": a.get("connected", False),
                            "remoteStart": a.get("remoteStart", False),
                            "state": a.get("stateText"), "job": self.jobs.get(a["id"])})
        return out

    def schedule(self, ha_id, deadline=None, by=""):
        dev = next((d for d in self.devices() if d["id"] == ha_id), None)
        if not dev:
            raise ValueError("Gerät nicht gefunden")
        if not dev["connected"]:
            raise ValueError(f"{dev['name']} ist offline")
        if not dev["remoteStart"]:
            raise ValueError(f"Bitte am Gerät „{dev['name']}“ den Fernstart aktivieren")
        best = self.plan_fn(dev["profile"], deadline)
        now = time.time()
        planned = best["start"]
        ws = max(now, planned - WINDOW)
        we = planned + WINDOW
        if deadline:                                          # muss rechtzeitig fertig werden
            we = min(we, deadline - MINUTES.get(dev["profile"], 120) * 60)
        we = max(we, ws)
        job = {"haId": ha_id, "name": dev["name"], "start": planned, "end": best["end"],
               "windowStart": int(ws), "windowEnd": int(we), "needW": WATTS.get(dev["profile"], 800),
               "reason": best["reason"], "saving": best["saving"], "deadline": deadline,
               "status": "geplant", "by": by, "created": int(now)}
        with self._lock:
            self.jobs[ha_id] = job
            self._save()
        return self.jobs.get(ha_id, job)

    def cancel(self, ha_id):
        with self._lock:
            self.jobs.pop(ha_id, None)
            self._save()

    def _run_job(self, ha_id, why=None):
        job = self.jobs.get(ha_id)
        if not job or job.get("status") != "geplant":
            return
        if why:
            job["reason"] = why
        try:
            if ha_id.startswith("lg:") and self.lg is not None:
                self.lg.start(ha_id)                    # startet das am Gerät gewählte Programm (Fernstart)
                prog = None
            elif ha_id.startswith("miele:") and self.miele is not None:
                self.miele.command(ha_id, "start")      # startet das am Gerät gewählte Programm (MobileStart)
                prog = None
            else:
                prog = self.hc.start_selected(ha_id)
            job.update(status="gestartet", program=prog, startedAt=int(time.time()))
            print(f"[SMARTSTART] {job['name']} gestartet ({prog})", flush=True)
        except Exception as e:
            job.update(status="fehler", error=str(e)[:200])
            print(f"[SMARTSTART] {job['name']}: {e}", flush=True)
        with self._lock:
            self._save()

    def _loop(self):
        while True:
            time.sleep(30)
            now = time.time()
            try:
                w = self.surplus_fn() if self.surplus_fn else None
            except Exception:
                w = None
            self._surplus = (self._surplus + [w if w is not None else 0])[-SUSTAIN:]
            for ha_id, job in list(self.jobs.items()):
                if job.get("status") == "geplant":
                    ws, we = job.get("windowStart", job["start"]), job.get("windowEnd", job["start"])
                    if now < ws:
                        continue
                    need = job.get("needW") or 800
                    if len(self._surplus) >= SUSTAIN and min(self._surplus) >= need:
                        self._run_job(ha_id, f"Sonnenüberschuss {min(self._surplus) / 1000:.1f} kW".replace(".", ","))
                    elif now >= we:
                        self._run_job(ha_id, "spätestens am Fensterende – zu wenig Sonne")
                # erledigte/fehlgeschlagene Aufträge nach 12 h aufräumen
                elif job.get("status") != "geplant" and now - job.get("start", now) > 12 * 3600:
                    with self._lock:
                        self.jobs.pop(ha_id, None)
                        self._save()
