"""Zeitpläne für den Saugroboter (Roborock) – laufen im Hub, also auch bei geschlossener App.

Jeder Zeitplan: Wochentage, Uhrzeit, Räume (leer = ganze Wohnung), Saugstufe, Wasser/Wischen, Durchgänge.
Option „nur wenn niemand zuhause": ist jemand da, wartet der Hub bis zu X Stunden (oder lässt den Termin aus).
5 Minuten vorher kommt eine Mitteilung mit „Abbrechen", danach „startet jetzt"; „fertig" meldet roborock_vac.
Es gibt keine Zeitpläne ab Werk – nur, was der Besitzer selbst anlegt (das ist sein ausdrückliches OK).
Gespeichert in DATA_DIR/vacuum_schedules.json.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

from hub_config import DATA_DIR

FILE = os.path.join(DATA_DIR, "vacuum_schedules.json")
WARN = 300                         # 5 Minuten vorher Bescheid geben
DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


class VacuumSchedules:
    def __init__(self, vacuum, push, presence_fn):
        """presence_fn() → True (jemand zuhause), False (alle weg) oder None (unbekannt)"""
        self.vac, self.push, self.presence = vacuum, push, presence_fn
        self._lock = threading.Lock()
        d = self._load()
        self.items = d.get("items", [])
        self.runs = d.get("runs", {})          # id → {"date", "state": warned|waiting|started|skipped|cancelled, ...}
        threading.Thread(target=self._loop, daemon=True).start()

    # ------------------------------------------------------------------ Speicher
    def _load(self):
        try:
            with open(FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"items": self.items, "runs": self.runs}, f)
        os.replace(tmp, FILE)

    # ------------------------------------------------------------------ API
    @staticmethod
    def _clean(s):
        days = sorted({int(d) for d in (s.get("days") or []) if 0 <= int(d) <= 6})
        hh, mm = (str(s.get("time") or "10:00").split(":") + ["0"])[:2]
        hh, mm = max(0, min(23, int(hh))), max(0, min(59, int(mm)))
        if not days:
            raise ValueError("Mindestens einen Wochentag wählen")
        return {
            "id": s.get("id") or uuid.uuid4().hex[:8],
            "name": (s.get("name") or "").strip()[:40] or "Saugen",
            "enabled": bool(s.get("enabled", True)),
            "duid": s.get("duid"),
            "days": days, "time": f"{hh:02d}:{mm:02d}",
            "rooms": [int(r) for r in (s.get("rooms") or [])],
            "fan": s.get("fan") or None, "water": s.get("water") or None,
            "repeat": max(1, min(3, int(s.get("repeat") or 1))),
            "awayOnly": bool(s.get("awayOnly", False)),
            "awayMode": "skip" if s.get("awayMode") == "skip" else "postpone",
            "maxDelayH": max(1, min(12, int(s.get("maxDelayH") or 4))),
        }

    def list(self):
        out = []
        for s in self.items:
            n = self.next_run(s)
            out.append(dict(s, next=n, run=self.runs.get(s["id"])))
        return out

    def upsert(self, s, by=""):
        c = self._clean(s)
        if not c["duid"]:
            raise ValueError("Kein Saugroboter gewählt")
        with self._lock:
            self.items = [x for x in self.items if x["id"] != c["id"]] + [c]
            self.runs.pop(c["id"], None)
            self._save()
        print(f"[ZEITPLAN] {by}: „{c['name']}“ {'/'.join(DAYS[d] for d in c['days'])} {c['time']} gespeichert", flush=True)
        return c

    def delete(self, sid):
        with self._lock:
            self.items = [x for x in self.items if x["id"] != sid]
            self.runs.pop(sid, None)
            self._save()

    def skip_next(self, sid, by=""):
        """„Abbrechen" aus der Mitteilung oder der App: den nächsten/aktuellen Termin auslassen"""
        s = next((x for x in self.items if x["id"] == sid), None)
        if not s:
            raise KeyError(sid)
        due = self._due_today(s) or self._next_due(s)
        with self._lock:
            run = self.runs.get(sid) or {}
            if run.get("state") == "started" and run.get("date") == due.strftime("%Y-%m-%d"):
                raise ValueError("Läuft schon – in der App auf „Pause“ oder „Zur Station“ tippen")
            self.runs[sid] = {"date": due.strftime("%Y-%m-%d"), "state": "cancelled", "by": by, "at": int(time.time())}
            self._save()
        print(f"[ZEITPLAN] „{s['name']}“ am {due:%d.%m. %H:%M} abgebrochen ({by})", flush=True)
        return {"ok": True, "skipped": int(due.timestamp())}

    # ------------------------------------------------------------------ Zeit
    @staticmethod
    def _at(s, day):
        hh, mm = map(int, s["time"].split(":"))
        return day.replace(hour=hh, minute=mm, second=0, microsecond=0)

    def _due_today(self, s):
        now = datetime.now()
        if now.weekday() in s["days"]:
            t = self._at(s, now)
            # noch relevant: bis 5 Min vorher / Wartezeit wegen Anwesenheit
            if t + timedelta(hours=s["maxDelayH"] if s["awayOnly"] else 0, minutes=15) >= now - timedelta(seconds=1):
                return t
        return None

    def _next_due(self, s):
        now = datetime.now()
        for i in range(0, 8):
            day = now + timedelta(days=i)
            if day.weekday() in s["days"]:
                t = self._at(s, day)
                if t > now:
                    return t
        return self._at(s, now + timedelta(days=7))

    def next_run(self, s):
        """nächster Termin (Unix) – ein heute abgebrochener/erledigter Termin zählt nicht"""
        if not s.get("enabled"):
            return None
        now = datetime.now()
        run = self.runs.get(s["id"]) or {}
        for i in range(0, 15):
            day = now + timedelta(days=i)
            if day.weekday() not in s["days"]:
                continue
            t = self._at(s, day)
            if run.get("date") == t.strftime("%Y-%m-%d") and run.get("state") in ("cancelled", "started", "skipped"):
                continue
            if t >= now - timedelta(minutes=1) or (run.get("state") == "waiting" and run.get("date") == t.strftime("%Y-%m-%d")):
                return int(t.timestamp())
        return None

    # ------------------------------------------------------------------ Ablauf
    def _send(self, title, body, sid=None):
        if self.push is not None and self.push.configured:
            data = {"vacuumSchedule": sid} if sid else None
            self.push.send("vacuum", title, body, data=data, category="VACUUM_SCHEDULE" if sid else None)

    def _loop(self):
        time.sleep(20)
        while True:
            try:
                self._tick()
            except Exception as e:
                print(f"[ZEITPLAN] {e}", flush=True)
            time.sleep(20)

    def _tick(self):
        now = datetime.now()
        for s in list(self.items):
            if not s.get("enabled"):
                continue
            t = self._due_today(s)
            if not t:
                continue
            day = t.strftime("%Y-%m-%d")
            run = self.runs.get(s["id"]) or {}
            if run.get("date") != day:
                run = {"date": day, "state": "planned"}
            state = run["state"]
            if state in ("cancelled", "started", "skipped"):
                continue
            name = s["name"]
            dev = (self.vac.status or {}).get(s["duid"]) or {}
            robot = dev.get("name") or "Saugroboter"
            # 5 Minuten vorher Bescheid geben (einmal)
            if state == "planned" and now >= t - timedelta(seconds=WARN) and now < t:
                self._send(f"🧹 {robot} startet um {s['time']}", f"Zeitplan „{name}“ – „Abbrechen“ lässt diesen Termin aus.", s["id"])
                run["state"] = state = "warned"
            if now < t:
                self._store(s["id"], run)
                continue
            # Anwesenheit
            if s["awayOnly"]:
                home = self.presence()
                if home:
                    if s["awayMode"] == "skip":
                        run["state"] = "skipped"; run["reason"] = "jemand zuhause"
                        self._send(f"🧹 {robot}: Termin ausgelassen", f"„{name}“ – es war jemand zuhause.")
                        self._store(s["id"], run)
                        continue
                    if now < t + timedelta(hours=s["maxDelayH"]):
                        if state != "waiting":
                            run["state"] = "waiting"
                            self._send(f"🧹 {robot} wartet", f"„{name}“ startet, sobald alle weg sind (höchstens {s['maxDelayH']} Std).", s["id"])
                        self._store(s["id"], run)
                        continue
                    run["state"] = "skipped"; run["reason"] = "jemand zuhause"
                    self._send(f"🧹 {robot}: Termin ausgelassen", f"„{name}“ – es war die ganze Zeit jemand zuhause.")
                    self._store(s["id"], run)
                    continue
            elif now > t + timedelta(minutes=15):
                continue                                        # Hub war aus – verpassten Termin nicht nachholen
            # Gerät bereit?
            if not dev.get("online") or dev.get("error") or not dev.get("docked") or (dev.get("battery") or 0) < 30:
                why = ("nicht erreichbar" if not dev.get("online") else dev.get("error") or
                       ("nicht in der Station" if not dev.get("docked") else "Akku unter 30 %"))
                run["state"] = "skipped"; run["reason"] = why
                self._send(f"🧹 {robot}: Termin ausgelassen", f"„{name}“ – {why}.")
                self._store(s["id"], run)
                continue
            try:
                if s.get("fan"):
                    self.vac.command(s["duid"], "fan", s["fan"])
                if s.get("water"):
                    self.vac.command(s["duid"], "water", s["water"])
                if s["rooms"]:
                    self.vac.command(s["duid"], "rooms", {"rooms": s["rooms"], "repeat": s["repeat"]})
                else:
                    self.vac.command(s["duid"], "start")
                run["state"] = "started"; run["at"] = int(time.time())
                rooms = ", ".join(r["name"] for r in dev.get("rooms") or [] if r["id"] in s["rooms"]) or "ganze Wohnung"
                self._send(f"🧹 {robot} startet", f"„{name}“: {rooms}.")
                print(f"[ZEITPLAN] „{name}“ gestartet ({rooms})", flush=True)
            except Exception as e:
                run["state"] = "skipped"; run["reason"] = str(e)[:120]
                self._send(f"⚠️ {robot}: Start fehlgeschlagen", f"„{name}“: {str(e)[:100]}")
            self._store(s["id"], run)

    def _store(self, sid, run):
        with self._lock:
            if self.runs.get(sid) != run:
                self.runs[sid] = dict(run)
                self._save()
