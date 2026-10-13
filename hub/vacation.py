"""Urlaubsmodus: Anwesenheit vortäuschen – abends gehen ausgewählte Lichter zufällig an und aus.

Nur Lichter (keine Geräte, kein Tor, keine Heizung). Schaltet ausschließlich, solange der Modus in der App
eingeschaltet ist und der Zeitraum läuft. Am Ende des Abends schaltet der Hub die Lichter aus, die er selbst
eingeschaltet hat – sonst nichts. Jeder Schaltvorgang steht im Protokoll (GET /api/vacation).
"""
import json
import os
import random
import threading
import time
from datetime import date, datetime, timedelta

from hub_config import DATA_DIR

FILE = os.path.join(DATA_DIR, "vacation.json")
MAX_PARALLEL = 2


class Vacation:
    def __init__(self, home_fn, sun_fn, push=None):
        """home_fn → Backend (structure/command), sun_fn(day) → Sonnenuntergang (datetime)"""
        self.home_fn, self.sun_fn, self.push = home_fn, sun_fn, push
        self._lock = threading.Lock()
        self.cfg = self._load()
        self.plan = []            # [{did, name, on: ts, off: ts, done_on, done_off}]
        self.plan_day = None
        self.ours = set(self.cfg.get("ours") or [])   # Lichter, die der Hub eingeschaltet hat (übersteht Neustart)
        self.log = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _load(self):
        try:
            with open(FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"on": False, "lights": [], "end": "23:00", "offset": 15}

    def _save(self):
        tmp = FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.cfg, f)
        os.replace(tmp, FILE)

    # ---- Einstellungen
    def status(self):
        c = dict(self.cfg)
        c["active"] = self._active_today()
        c["tonight"] = [{"name": p["name"], "on": int(p["on"]), "off": int(p["off"])} for p in self.plan] if self.plan_day == date.today() else []
        c["log"] = self.log[-30:]
        return c

    def set(self, body, by=""):
        with self._lock:
            c = self.cfg
            if "lights" in body:
                c["lights"] = [str(x) for x in body["lights"]][:20]
            if "from" in body:
                c["from"] = body["from"] or None
            if "until" in body:
                c["until"] = body["until"] or None
            for k in ("from", "until"):
                if c.get(k):
                    date.fromisoformat(c[k])
            if "end" in body:
                hh, mm = (int(x) for x in str(body["end"]).split(":"))
                if not (20 <= hh <= 23 or (hh == 0 and mm == 0)):
                    raise ValueError("Ende zwischen 20 und 24 Uhr")
                c["end"] = f"{hh:02d}:{mm:02d}"
            if "on" in body:
                c["on"] = bool(body["on"])
                self._note(("Urlaubsmodus an" if c["on"] else "Urlaubsmodus aus") + (f" ({by})" if by else ""))
                if not c["on"]:
                    self.plan, self.plan_day = [], None
            if c["on"] and not c.get("lights"):
                raise ValueError("Bitte mindestens ein Licht wählen")
            self._save()
        if not self.cfg["on"]:
            self._all_off()
        return self.status()

    def _active_today(self):
        c, today = self.cfg, date.today()
        if not c.get("on"):
            return False
        if c.get("from") and today < date.fromisoformat(c["from"]):
            return False
        if c.get("until") and today > date.fromisoformat(c["until"]):
            return False
        return True

    # ---- Ablauf
    def _note(self, text):
        self.log.append({"t": int(time.time()), "text": text})
        self.log = self.log[-100:]
        print(f"[URLAUB] {text}", flush=True)

    def _window(self, day):
        sunset = self.sun_fn(day) or datetime(day.year, day.month, day.day, 18, 30)
        start = sunset + timedelta(minutes=int(self.cfg.get("offset") or 15))
        hh, mm = (int(x) for x in self.cfg.get("end", "23:00").split(":"))
        end = datetime(day.year, day.month, day.day, hh, mm) if hh else datetime(day.year, day.month, day.day) + timedelta(days=1)
        return start, max(end, start + timedelta(minutes=60))

    def _make_plan(self, day):
        """Zufälliger, aber plausibler Abend: jedes Licht 1–3 Mal 15–75 Minuten, höchstens 2 gleichzeitig."""
        home = self.home_fn()
        names = {}
        try:
            from devices import device_index
            names = {k: v.get("name") for k, v in device_index(home.structure()).items() if v.get("kind") in ("light", "dimmer")}
        except Exception:
            pass
        start, end = self._window(day)
        span = (end - start).total_seconds()
        plan = []
        for did in self.cfg.get("lights") or []:
            if did not in names:
                continue
            for _ in range(random.randint(1, 3)):
                dur = random.randint(15, 75) * 60
                if span <= dur:
                    continue
                on = start.timestamp() + random.uniform(0, span - dur)
                if sum(1 for p in plan if p["on"] < on + dur and on < p["off"]) >= MAX_PARALLEL:
                    continue
                if any(p["did"] == did and p["on"] < on + dur + 600 and on < p["off"] + 600 for p in plan):
                    continue
                plan.append({"did": did, "name": names[did], "on": on, "off": on + dur})
        plan.sort(key=lambda p: p["on"])
        self.plan, self.plan_day = plan, day
        self._note(f"Plan für heute Abend: {len(plan)} Schaltungen zwischen {start:%H:%M} und {end:%H:%M}")

    def _switch(self, did, on, name):
        try:
            self.home_fn().command(did, {"on": on})
            (self.ours.add if on else self.ours.discard)(did)
            with self._lock:
                self.cfg["ours"] = sorted(self.ours)
                self._save()
            self._note(f"{name} {'an' if on else 'aus'}")
        except Exception as e:
            self._note(f"{name}: {e}")

    def _all_off(self):
        for did in list(self.ours):
            self._switch(did, False, next((p["name"] for p in self.plan if p["did"] == did), did))

    def _loop(self):
        time.sleep(60)
        while True:
            try:
                self._tick()
            except Exception as e:
                print(f"[URLAUB] {e}", flush=True)
            time.sleep(30)

    def _tick(self):
        today = date.today()
        if not self._active_today():
            if self.ours:
                self._all_off()
            if self.cfg.get("on") and self.cfg.get("until") and today > date.fromisoformat(self.cfg["until"]):
                with self._lock:                         # Zeitraum vorbei → Modus aus
                    self.cfg["on"] = False
                    self._save()
                self._note("Urlaubsmodus beendet (Zeitraum vorbei)")
                if self.push is not None:
                    self.push.send("smart", "Urlaubsmodus beendet", "Willkommen zurück – die Lichtsimulation ist aus.")
            return
        if self.plan_day != today and datetime.now().hour >= 12:
            self._make_plan(today)
        now = time.time()
        for p in self.plan:
            if not p.get("done_on") and p["on"] <= now < p["off"]:
                p["done_on"] = True
                self._switch(p["did"], True, p["name"])
            if p.get("done_on") and not p.get("done_off") and now >= p["off"]:
                p["done_off"] = True
                # nur aus, wenn kein anderer Eintrag dasselbe Licht gerade an haben will
                if not any(q is not p and q["did"] == p["did"] and q["on"] <= now < q["off"] for q in self.plan):
                    self._switch(p["did"], False, p["name"])
        _, end = self._window(today)
        if now >= end.timestamp() and self.ours:
            self._all_off()
