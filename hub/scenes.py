#!/usr/bin/env python3
"""Szenen auf dem Hub: mehrere Geräte mit einem Tipp (oder per Zeitplan) schalten.

Eine Szene:
{
  "id": "a1b2c3d4", "name": "Garten", "icon": "tree",
  "actions": [ {"fid": "lx:<id> | ha:light.x", "on": true, "level": 60, "position": 0, "target": 21} ],
  "schedule": {
     "enabled": true, "days": [0,1,2,3,4,5,6],          # 0 = Montag
     "start": {"type": "time"|"sunset"|"sunrise", "time": "18:30", "offset": 0},   # oder null
     "stop":  {"type": "time"|"sunset"|"sunrise", "time": "23:00", "offset": 0}    # oder null
  }
}
Start = Aktionen ausführen. Stopp = beteiligte Lichter/Schalter wieder aus
(Rollläden und Heizung bleiben beim Stopp unverändert).
"""
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, date, timezone

from hub_config import DATA_DIR
SCENES_FILE = os.environ.get("SCENES_FILE", os.path.join(DATA_DIR, "scenes.json"))


# ------------------------------------------------------------------ Sonnenstand
def _sun_time(day: date, rising: bool):
    """Sonnenauf-/-untergang (lokale Zeit) nach NOAA, auf ~1 Minute genau."""
    import hub_config
    LAT = float(hub_config.get("location.lat", 51.16))     # Standort für Sonnenauf-/-untergang (Einrichtung)
    LON = float(hub_config.get("location.lon", 10.45))
    n = day.timetuple().tm_yday
    lng_hour = LON / 15
    t = n + ((6 if rising else 18) - lng_hour) / 24
    m = 0.9856 * t - 3.289
    l = (m + 1.916 * math.sin(math.radians(m)) + 0.020 * math.sin(math.radians(2 * m)) + 282.634) % 360
    ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l)))) % 360
    ra = (ra + (math.floor(l / 90) * 90 - math.floor(ra / 90) * 90)) / 15
    sin_dec = 0.39782 * math.sin(math.radians(l))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_h = (math.cos(math.radians(90.833)) - sin_dec * math.sin(math.radians(LAT))) / (cos_dec * math.cos(math.radians(LAT)))
    if not -1 <= cos_h <= 1:
        return None
    h = (360 - math.degrees(math.acos(cos_h))) if rising else math.degrees(math.acos(cos_h))
    ut = (h / 15 + ra - 0.06571 * t - 6.622 - lng_hour) % 24
    utc = datetime(day.year, day.month, day.day) + timedelta(hours=ut)
    # UTC → lokale Zeit (inkl. Sommerzeit des Pi)
    return utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def resolve(spec, day: date):
    """Zeitpunkt (lokal, ohne Zeitzone) für einen Start/Stopp-Eintrag an einem Tag."""
    if not spec:
        return None
    kind = spec.get("type", "time")
    if kind in ("sunset", "sunrise"):
        base = _sun_time(day, rising=(kind == "sunrise"))
        return base + timedelta(minutes=int(spec.get("offset") or 0)) if base else None
    try:
        hh, mm = (int(x) for x in str(spec.get("time", "")).split(":")[:2])
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, hh, mm)


# ------------------------------------------------------------------ Szenen
class Scenes:
    def __init__(self, home_fn):
        self.home_fn = home_fn          # liefert das aktive Geräte-Backend (LUXOR, Home Assistant, Demo)
        self.scenes = self._load()
        self.last_run = {}              # id → {"phase", "at", "ok", "errors"}
        self._fired = set()
        threading.Thread(target=self._scheduler, daemon=True).start()

    # ---- Speicher
    def _load(self):
        try:
            with open(SCENES_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _save(self):
        tmp = SCENES_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.scenes, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SCENES_FILE)

    def list(self):
        today = date.today()
        out = []
        for s in self.scenes:
            s = dict(s)
            sch = s.get("schedule") or {}
            nxt = {}
            for phase in ("start", "stop"):
                t = resolve(sch.get(phase), today)
                if t:
                    nxt[phase] = t.strftime("%H:%M")
            s["today"] = nxt
            s["lastRun"] = self.last_run.get(s["id"])
            out.append(s)
        return out

    def upsert(self, s):
        s = dict(s)
        s["id"] = s.get("id") or uuid.uuid4().hex[:8]
        s.pop("today", None)
        s.pop("lastRun", None)
        self.scenes = [x for x in self.scenes if x["id"] != s["id"]] + [s]
        self.scenes.sort(key=lambda x: x.get("name", "").lower())
        self._save()
        return s

    def delete(self, sid):
        self.scenes = [x for x in self.scenes if x["id"] != sid]
        self._save()

    # ---- Ausführen
    @staticmethod
    def _did(a):
        fid = a.get("fid") or a.get("id") or ""
        return fid if ":" in fid else "lx:" + fid          # alte Szenen (nur LUXOR) ohne Präfix

    @staticmethod
    def _cmd(kind, a, phase):
        """Szenen-Eintrag → Gerätebefehl (siehe devices.py)."""
        if phase == "stop":
            return {"on": False} if kind in ("light", "dimmer") else None
        if kind == "dimmer":
            if a.get("on") is False or a.get("level") == 0:
                return {"on": False}
            return {"level": a["level"]} if a.get("level") is not None else {"on": True}
        if kind == "light":
            return {"on": bool(a.get("on", True))}
        if kind == "blind" and a.get("position") is not None:
            return {"position": a["position"]}
        if kind == "heating" and a.get("target") is not None:
            return {"target": a["target"]}
        return None                                          # Tore werden von Szenen nie ausgelöst

    def run(self, sid, phase="start"):
        s = next((x for x in self.scenes if x["id"] == sid), None)
        if not s:
            raise KeyError(sid)
        from devices import device_index
        home, idx, errors = self.home_fn(), {}, []
        if home is None:
            errors.append("Kein Haus eingerichtet")
        else:
            try:
                idx = device_index(home.structure())
            except Exception as e:
                errors.append(f"Geräte nicht erreichbar: {e}")
        for a in s.get("actions", []):
            did = self._did(a)
            d = idx.get(did)
            if not d:
                errors.append(f"{a.get('name') or did}: nicht mehr vorhanden")
                continue
            cmd = self._cmd(d["kind"], a, phase)
            if not cmd:
                continue
            try:
                home.command(did, cmd)
            except Exception as e:
                errors.append(f"{d['name']}: {e}")
        self.last_run[sid] = {"phase": phase, "at": datetime.now().strftime("%H:%M"),
                              "ok": not errors, "errors": errors[:5]}
        print(f"[SZENE] {s.get('name')} {phase}: {'ok' if not errors else errors}", flush=True)
        return self.last_run[sid]

    # ---- Zeitplan
    def _scheduler(self):
        while True:
            try:
                now = datetime.now()
                today = now.date()
                for s in list(self.scenes):
                    sch = s.get("schedule") or {}
                    if not sch.get("enabled") or now.weekday() not in sch.get("days", range(7)):
                        continue
                    for phase in ("start", "stop"):
                        t = resolve(sch.get(phase), today)
                        key = (s["id"], phase, today.isoformat())
                        # bis zu 2 Minuten nach dem Zeitpunkt noch auslösen (z. B. nach Neustart)
                        if t and t <= now < t + timedelta(minutes=2) and key not in self._fired:
                            self._fired.add(key)
                            threading.Thread(target=self.run, args=(s["id"], phase), daemon=True).start()
                if len(self._fired) > 500:
                    self._fired = {k for k in self._fired if k[2] == today.isoformat()}
            except Exception as e:
                print(f"[SZENE] Zeitplan-Fehler: {e}", flush=True)
            time.sleep(20)
