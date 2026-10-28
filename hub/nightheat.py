"""Heizplan je Raum: Nachtabsenkung und Sonnen-Extra.

Nachtabsenkung: abends Soll-Temperatur runter, morgens wieder auf den Wert von vorher.
Sonnen-Extra: in den letzten Stunden vor Sonnenuntergang, solange genug Sonnenstrom übrig ist (evcc: Einspeisung +
Akku-Laden, Akku voll genug), Soll-Temperatur um ein paar Grad hoch – der Estrich speichert die Wärme für den Abend.
Endet bei Sonnenuntergang, wenn der Überschuss 15 Minuten weg ist, oder wenn die Nachtabsenkung beginnt.

DATA_DIR/night_heat.json:
{"rooms": {"<fid>": {"enabled": true, "days": [0..6], "start": "22:00", "end": "05:00", "night": 17.0,
                     "active": false, "saved": 21.0,
                     "sun": {"enabled": false, "plus": 1.5, "before": 120, "surplus": 1500, "soc": 80,
                             "active": false, "saved": 21.0}}},
 "log": [...]}

„days“ = Abende, an denen die Absenkung beginnt (0 = Montag; Mo 22:00 → Di 05:00 gehört zu Montag).
Der Hub arbeitet nach Zustand, nicht nach Flanken: liegt jetzt ein Nachtfenster und der Raum ist noch nicht
abgesenkt, wird er abgesenkt (auch nach einem Neustart um 23:00); liegt keins mehr und er ist abgesenkt, kommt der
gemerkte Wert zurück (auch nach einem Neustart um 06:00). Dazwischen wird nichts nachgeregelt – wer nachts von Hand
hochdreht, behält das bis zum Morgen. Nur Geräte vom Typ „heating“ (Soll-Temperatur), sonst nichts.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta

from hub_config import DATA_DIR

FILE = os.environ.get("NIGHT_HEAT_FILE", os.path.join(DATA_DIR, "night_heat.json"))
NIGHT_MIN, NIGHT_MAX = 5.0, 25.0
DEFAULT = {"enabled": False, "days": list(range(7)), "start": "22:00", "end": "05:00", "night": 17.0}
SUN_DEFAULT = {"enabled": False, "plus": 1.5, "before": 120, "surplus": 1500, "soc": 80}
SUN_ON, SUN_OFF = 600, 900          # Überschuss 10 Min. am Stück → an; 15 Min. weg → aus


def _hm(s, fallback):
    for v in (s, fallback):
        try:
            hh, mm = (str(v).split(":") + ["0"])[:2]
            return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
        except (TypeError, ValueError):
            pass
    return 0, 0


def in_window(cfg, now):
    """Liegt `now` (datetime) in einem Nachtfenster dieses Raums?"""
    sh, sm = _hm(cfg.get("start"), "22:00")
    eh, em = _hm(cfg.get("end"), "05:00")
    days = cfg.get("days", range(7))
    for back in (0, 1):                          # Fenster von gestern Abend kann noch laufen
        day = (now - timedelta(days=back)).date()
        if day.weekday() not in days:
            continue
        start = datetime(day.year, day.month, day.day, sh, sm)
        end = datetime(day.year, day.month, day.day, eh, em)
        if end <= start:
            end += timedelta(days=1)             # über Mitternacht
        if start <= now < end:
            return True
    return False


class NightHeat:
    def __init__(self, home_fn, structure_fn, push=None, evcc_fn=None, sunset_fn=None):
        self.home_fn, self.structure_fn, self.push = home_fn, structure_fn, push
        self.evcc_fn = evcc_fn              # → evcc /api/state
        self.sunset_fn = sunset_fn          # date → datetime Sonnenuntergang
        self._since = {}                    # fid → {"good"|"bad": seit wann} (nur im Speicher)
        self._lock = threading.Lock()
        self.cfg = self._load()
        threading.Thread(target=self._loop, daemon=True).start()

    def _load(self):
        try:
            with open(FILE) as f:
                c = json.load(f)
        except (OSError, ValueError):
            c = {}
        return {"rooms": {}, "log": [], **c}

    def _save(self):
        tmp = FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.cfg, f, indent=1, ensure_ascii=False)
        os.replace(tmp, FILE)

    def _heaters(self):
        """fid → {name, room} aller Heizungen (Soll-Temperatur)."""
        try:
            st = self.structure_fn()
        except Exception:
            return {}
        return {d["id"]: {"name": d.get("name"), "room": r.get("name", "")}
                for fl in st["floors"] for r in fl["rooms"] for d in r["devices"] if d.get("kind") == "heating"}

    # ---- API
    def get(self):
        heaters = self._heaters()
        rooms = []
        for fid, h in heaters.items():
            c = {**DEFAULT, **self.cfg["rooms"].get(fid, {})}
            rooms.append({"id": fid, "name": h["name"], "room": h["room"], "enabled": c["enabled"],
                          "days": c["days"], "start": c["start"], "end": c["end"], "night": c["night"],
                          "active": bool(c.get("active")), "saved": c.get("saved"),
                          "sun": {**SUN_DEFAULT, **c.get("sun", {})}})
        return {"rooms": rooms, "surplus": self._surplus(), "log": self.cfg.get("log", [])[:10]}

    def update(self, fid, body, by=""):
        if fid not in self._heaters():
            raise KeyError(fid)
        days = sorted({int(d) for d in (body.get("days") or []) if 0 <= int(d) <= 6})
        enabled = bool(body.get("enabled"))
        if enabled and not days:
            raise ValueError("Mindestens einen Wochentag wählen")
        night = float(body.get("night", DEFAULT["night"]))
        if not NIGHT_MIN <= night <= NIGHT_MAX:
            raise ValueError(f"Nacht-Temperatur nur {NIGHT_MIN:.0f}–{NIGHT_MAX:.0f} °C")
        sh, sm = _hm(body.get("start"), "22:00")
        eh, em = _hm(body.get("end"), "05:00")
        if (sh, sm) == (eh, em):
            raise ValueError("Beginn und Ende dürfen nicht gleich sein")
        sun = None
        if isinstance(body.get("sun"), dict):
            b = body["sun"]
            plus = float(b.get("plus", SUN_DEFAULT["plus"]))
            if not 0.5 <= plus <= 3:
                raise ValueError("Sonnen-Extra nur +0,5 bis +3 °C")
            sun = {"enabled": bool(b.get("enabled")), "plus": round(plus * 2) / 2,
                   "before": max(30, min(360, int(b.get("before", SUN_DEFAULT["before"])))),
                   "surplus": max(300, min(20000, int(b.get("surplus", SUN_DEFAULT["surplus"])))),
                   "soc": max(0, min(100, int(b.get("soc", SUN_DEFAULT["soc"]))))}
        with self._lock:
            cur = self.cfg["rooms"].get(fid, {})
            cur.update(enabled=enabled, days=days or list(range(7)), start=f"{sh:02d}:{sm:02d}",
                       end=f"{eh:02d}:{em:02d}", night=round(night * 2) / 2)
            if sun is not None:
                cur["sun"] = {**cur.get("sun", {}), **sun}
            self.cfg["rooms"][fid] = cur
            self._save()
        print(f"[NACHT] {by}: {fid} {'an' if enabled else 'aus'} {cur['start']}–{cur['end']} {cur['night']} °C", flush=True)
        self.tick()                               # z. B. abgeschaltet während der Nacht → sofort zurück
        return self.get()

    # ---- Ablauf
    def _loop(self):
        time.sleep(40)
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"[NACHT] Fehler: {e}", flush=True)
            time.sleep(30)

    # ---- Sonne
    def _surplus(self):
        """→ {"w": Überschuss in W, "soc": Akku %} aus evcc, oder None."""
        if not self.evcc_fn:
            return None
        try:
            s = self.evcc_fn() or {}
        except Exception:
            return None
        s = s.get("result", s) if isinstance(s, dict) else {}
        if not s:
            return None
        num = lambda v: float(v) if isinstance(v, (int, float)) else None
        bat = s.get("battery") if isinstance(s.get("battery"), dict) else {}
        grid = s.get("grid") if isinstance(s.get("grid"), dict) else {}
        g = num(grid.get("power", s.get("gridPower"))) or 0
        b = num(bat.get("power", s.get("batteryPower"))) or 0
        return {"w": round(max(-g, 0) + max(-b, 0)), "soc": num(bat.get("soc", s.get("batterySoc")))}

    def _sun_window(self, sc, now):
        if not self.sunset_fn:
            return False
        sunset = self.sunset_fn(now.date())
        return bool(sunset) and sunset - timedelta(minutes=sc["before"]) <= now < sunset

    def _sun_tick(self, now):
        rooms = {fid: c for fid, c in self.cfg["rooms"].items() if (c.get("sun") or {}).get("enabled")
                 or (c.get("sun") or {}).get("active")}
        if not rooms:
            self._since.clear()
            return
        sur = self._surplus()
        t = now.timestamp()
        for fid, c in rooms.items():
            sc = {**SUN_DEFAULT, **c["sun"]}
            good = (sur is not None and sur["w"] >= sc["surplus"]
                    and (sur["soc"] is None or sur["soc"] >= sc["soc"]))
            gs = self._since.setdefault(fid, {})
            if good:
                gs["good"] = gs.get("good") or t
                gs.pop("bad", None)
            else:
                gs["bad"] = gs.get("bad") or t
                gs.pop("good", None)
            allowed = (sc["enabled"] and self._sun_window(sc, now)
                       and not (c.get("enabled") and in_window(c, now)) and not c.get("active"))
            if not c["sun"].get("active"):
                if allowed and good and t - gs["good"] >= SUN_ON:
                    self._sun_start(fid, c, sc, sur)
            elif not allowed or (not good and t - gs["bad"] >= SUN_OFF):
                self._sun_end(fid, c, "Sonnenuntergang/Nacht" if not allowed else "kein Überschuss mehr")

    def _sun_start(self, fid, c, sc, sur):
        home = self.home_fn()
        cur = self._target(home, fid) if home else None
        if cur is None:
            return
        new = min(NIGHT_MAX, float(cur) + sc["plus"])
        try:
            home.command(fid, {"target": new})
        except Exception as e:
            self._log(fid, f"Sonnen-Extra fehlgeschlagen: {e}", False)
            return
        with self._lock:
            c["sun"].update(active=True, saved=float(cur))
            self._save()
        self._log(fid, f"Sonnen-Extra: {new} °C (vorher {cur} °C, {sur['w']} W übrig)")

    def _sun_end(self, fid, c, why):
        home = self.home_fn()
        if home is None:
            return
        back = c["sun"].get("saved")
        if back is not None:
            try:
                home.command(fid, {"target": back})
            except Exception as e:
                self._log(fid, f"Sonnen-Extra zurück fehlgeschlagen: {e}", False)
                return
        with self._lock:
            c["sun"]["active"] = False
            self._save()
        self._log(fid, f"Sonnen-Extra Ende ({why}): wieder {back} °C")

    def tick(self, now=None):
        now = now or datetime.now()
        self._sun_tick(now)                      # zuerst: Sonnen-Extra endet, bevor die Nacht den Wert merkt
        for fid, c in list(self.cfg["rooms"].items()):
            want = bool(c.get("enabled")) and in_window(c, now)
            if want and (c.get("sun") or {}).get("active"):
                continue                          # Sonnen-Extra muss erst zurück sein
            if want and not c.get("active"):
                self._start(fid, c)
            elif not want and c.get("active"):
                self._end(fid, c)

    def _target(self, home, fid):
        try:
            return (home.states([fid]).get(fid) or {}).get("target")
        except Exception:
            return None

    def _start(self, fid, c):
        home = self.home_fn()
        if home is None:
            return
        cur = self._target(home, fid)
        try:
            home.command(fid, {"target": c["night"]})
        except Exception as e:
            self._log(fid, f"Absenken fehlgeschlagen: {e}", False)
            return
        with self._lock:
            # schon auf Nachtwert (z. B. von Hand)? Dann den alten Tageswert behalten
            if cur is not None and float(cur) != float(c["night"]):
                c["saved"] = float(cur)
            c["active"] = True
            self._save()
        self._log(fid, f"Nacht: {c['night']} °C (vorher {cur if cur is not None else '?'} °C)")

    def _end(self, fid, c):
        home = self.home_fn()
        if home is None:
            return
        back = c.get("saved")
        if back is not None:
            try:
                home.command(fid, {"target": back})
            except Exception as e:
                self._log(fid, f"Zurückstellen fehlgeschlagen: {e}", False)
                return                            # beim nächsten Durchlauf erneut versuchen
        with self._lock:
            c["active"] = False
            self._save()
        self._log(fid, f"Tag: wieder {back} °C" if back is not None else "Tag: kein alter Wert bekannt – unverändert")

    def _log(self, fid, text, ok=True):
        name = (self._heaters().get(fid) or {}).get("name") or fid
        r = {"at": datetime.now().strftime("%d.%m. %H:%M"), "id": fid, "name": name, "text": text, "ok": ok}
        with self._lock:
            self.cfg["log"] = ([r] + self.cfg.get("log", []))[:20]
            self._save()
        print(f"[NACHT] {name}: {text}", flush=True)
