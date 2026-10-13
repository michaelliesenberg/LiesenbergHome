"""Hintergrund-Wächter: erkennt Ereignisse und schickt Mitteilungen (über push.py).

  • Wasch-/Spül-/Trockenmaschine fertig (Home Connect: Betrieb → Fertig)
  • Auto-Start ausgeführt oder fehlgeschlagen (smartstart.py)
  • Auto voll geladen bzw. Ladeziel erreicht (evcc-Ladepunkt: lädt → lädt nicht mehr, Ziel erreicht)
  • Wochenbericht sonntags um 19 Uhr (Sonne, Eigenversorgung, gespart)
  • Monatsbericht am 1. um 9 Uhr (report.py)
  • Solar-Assistent: viel Überschuss → Vorschlag, welche Geräte jetzt passen (startet nichts – nur Hinweis)
  • Billigstrom-Alarm: nur bei dynamischem Tarif – günstigstes Fenster der Nacht bzw. negativer Preis
"""
import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import requests

from hub_config import DATA_DIR

STATE = os.path.join(DATA_DIR, "notifier.json")
RUNNING = {"Run", "Pause", "DelayedStart"}


class Notifier:
    def __init__(self, push, homeconnect, smartstart, evcc_url_fn, summary_fn):
        self.push, self.hc, self.smart = push, homeconnect, smartstart
        self.evcc_url_fn, self.summary_fn = evcc_url_fn, summary_fn
        self.prev_hc, self.prev_lp, self.prev_jobs = {}, {}, {}
        self.state = self._load()
        self.plan_fn = None          # → planner.plan(...) (wird in house_server gesetzt)
        self.apps_fn = None          # → Hausgeräte [{name, type, state}] aus Home Connect + Miele
        self.report_fn = None        # → report.month_report(...)
        self._surplus = []           # letzte Überschuss-Werte (W), eine Minute Abstand
        threading.Thread(target=self._loop, daemon=True).start()

    def _load(self):
        try:
            with open(STATE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        with open(STATE, "w") as f:
            json.dump(self.state, f)

    def _loop(self):
        time.sleep(20)
        while True:
            for check in (self._appliances, self._smartstart, self._cars, self._weekly, self._monthly, self._solar, self._cheap):
                try:
                    check()
                except Exception as e:          # ein Fehler darf die anderen Wächter nicht stoppen
                    print(f"[NOTIFY] {check.__name__}: {e}", flush=True)
            time.sleep(60)

    # ---- Hausgeräte fertig
    def _appliances(self):
        for a in self.hc.list():
            st, prev = a.get("state"), self.prev_hc.get(a["id"])
            if prev in RUNNING and st in ("Finished", "Ready", "Inactive") and a.get("connected"):
                self.push.send("washer", f"{a.get('name') or 'Gerät'} ist fertig", "Zeit zum Ausräumen.")
            self.prev_hc[a["id"]] = st

    # ---- Auto-Start (smartstart)
    def _smartstart(self):
        for ha_id, job in list(self.smart.jobs.items()):
            st, prev = job.get("status"), self.prev_jobs.get(ha_id)
            if prev == "geplant" and st == "gestartet":
                self.push.send("smart", f"{job.get('name')} gestartet",
                               f"Automatisch zur besten Zeit ({job.get('reason', '')}).")
            elif prev == "geplant" and st == "fehler":
                self.push.send("smart", f"{job.get('name')}: Start fehlgeschlagen", job.get("error") or "")
            self.prev_jobs[ha_id] = st

    # ---- Auto voll
    def _cars(self):
        url = (self.evcc_url_fn() or "").rstrip("/")
        if not url:
            return
        s = requests.get(url + "/api/state", timeout=5).json()
        s = s.get("result", s)
        for i, lp in enumerate(s.get("loadpoints") or []):
            prev = self.prev_lp.get(i)
            now = {"charging": bool(lp.get("charging")), "connected": bool(lp.get("connected"))}
            if prev and prev["charging"] and not now["charging"] and now["connected"]:
                soc, limit = lp.get("vehicleSoc"), lp.get("effectiveLimitSoc") or lp.get("limitSoc")
                car = lp.get("vehicleTitle") or "Auto"
                if soc is not None and limit and soc >= limit - 1:
                    self.push.send("car", f"{car} ist geladen", f"{round(soc)} % – Ladeziel erreicht.")
            self.prev_lp[i] = now

    # ---- Wochenbericht sonntags 19 Uhr
    def _weekly(self):
        now = datetime.now()
        if now.weekday() != 6 or now.hour < 19:
            return
        week = now.strftime("%G-%V")
        if self.state.get("weekly") == week:
            return
        s = self.summary_fn("week", date.today())
        t, c = s["totals"], s.get("costs")
        share = round(100 * t["selfUse"] / t["home"]) if t.get("home") else 0
        body = f"{t['pv']:.0f} kWh Sonne · {share} % selbst versorgt"
        if c:
            body += f" · {c['saved']:.2f} € gespart · Saldo {c['balance']:+.2f} €"
        self.push.send("weekly", "Deine Energie-Woche", body)
        self.state["weekly"] = week
        self._save()

    # ---- Monatsbericht am 1. um 9 Uhr (für den Vormonat)
    def _monthly(self):
        now = datetime.now()
        if now.day != 1 or now.hour < 9 or not self.report_fn:
            return
        prev = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        if self.state.get("monthly") == prev:
            return
        r = self.report_fn(prev)
        cur = r.get("current")
        if cur:
            names = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
                     "Oktober", "November", "Dezember"]
            title = f"Dein Energie-{names[int(prev[5:]) - 1]}"
            body = f"{cur['pv']:.0f} kWh Sonne · " + " · ".join(r["highlights"][:3])
            self.push.send("monthly", title, body, data={"open": "report", "month": prev})
        self.state["monthly"] = prev
        self._save()

    # ---- Solar-Assistent: Überschuss → Vorschlag (höchstens einmal am Tag, nur 9–17 Uhr)
    SOLAR_MIN_W = 1500
    SOLAR_MINUTES = 10

    def _solar(self):
        now = datetime.now()
        if not self.plan_fn or not (9 <= now.hour < 17):
            self._surplus = []
            return
        today = date.today().isoformat()
        if self.state.get("solar") == today:
            return
        surplus, soc = self._surplus_now()
        self._surplus = (self._surplus + [surplus])[-self.SOLAR_MINUTES:]
        # genug Überschuss über 10 Minuten und der Akku ist (fast) voll – sonst lädt die Sonne erst den Akku
        if len(self._surplus) < self.SOLAR_MINUTES or min(self._surplus) < self.SOLAR_MIN_W or (soc is not None and soc < 85):
            return
        p = self.plan_fn()                           # erst jetzt den (teureren) Plan rechnen
        # nur Geräte vorschlagen, die es gibt und die gerade nicht laufen
        apps = self.apps_fn() if self.apps_fn else []
        types = {"washer": ("Washer", "WasherDryer"), "dishwasher": ("Dishwasher",), "dryer": ("Dryer", "WasherDryer")}
        idle = []
        for rec in p.get("recommendations") or []:
            if rec["id"] in types:
                mine = [a for a in apps if a.get("type") in types[rec["id"]]]
                if mine and all(a.get("state") not in RUNNING for a in mine):
                    idle.append(rec["title"])
            elif rec["id"] == "vacuum" and rec.get("now"):
                idle.append("Saugroboter")
        if not idle:
            return
        kw = f"{min(self._surplus) / 1000:.1f}".replace(".", ",")
        self.push.send("solar", f"☀️ {kw} kW Sonnenstrom übrig",
                       f"Guter Moment für: {', '.join(idle)}. Starten nur in der App – mit Rückfrage.",
                       data={"open": "energy"})
        self.state["solar"] = today
        self._save()

    def _surplus_now(self):
        """Überschuss jetzt (Einspeisung + Akku-Laden) und Akkustand – direkt aus evcc, ohne Plan"""
        url = (self.evcc_url_fn() or "").rstrip("/")
        s = requests.get(url + "/api/state", timeout=5).json()
        s = s.get("result", s)
        grid = s.get("grid") if isinstance(s.get("grid"), dict) else {}
        bat = s.get("battery") if isinstance(s.get("battery"), dict) else {}
        g = grid.get("power", s.get("gridPower")) or 0
        b = bat.get("power", s.get("batteryPower")) or 0
        soc = bat.get("soc", s.get("batterySoc"))
        return max(-g, 0) + max(-b, 0), soc

    # ---- Billigstrom-Alarm (nur dynamischer Tarif)
    def _cheap(self):
        if not self.plan_fn:
            return
        now = datetime.now()
        if now.minute % 15 != 0 and not (now.hour == 18 and self.state.get("cheap") != date.today().isoformat()):
            return                                   # Plan nicht jede Minute rechnen
        p = self.plan_fn()
        if not p.get("dynamicTariff"):
            return
        sl = [s for s in p.get("timeline") or [] if s.get("price") is not None]
        if len(sl) < 16:
            return
        today = date.today().isoformat()
        # negativer Preis gerade jetzt → sofort (einmal am Tag)
        if sl[0]["price"] <= 0 and self.state.get("cheap_neg") != today:
            self.push.send("cheap", "Strompreis gerade negativ",
                           f"{sl[0]['price'] * 100:.0f} ct/kWh – guter Moment für Auto, Wäsche oder Spülmaschine.",
                           data={"open": "energy"})
            self.state["cheap_neg"] = today
            self._save()
        # abends um 18 Uhr: günstigstes 3-Stunden-Fenster der nächsten 18 h, wenn es deutlich billiger ist
        if now.hour != 18 or self.state.get("cheap") == today:
            return
        horizon = [s for s in sl if s["t"] < time.time() + 18 * 3600]
        n = 12                                       # 12 × 15 min = 3 h
        if len(horizon) < n + 1:
            return
        best = min(range(len(horizon) - n + 1), key=lambda i: sum(s["price"] for s in horizon[i:i + n]))
        avg = sum(s["price"] for s in horizon[best:best + n]) / n
        median = sorted(s["price"] for s in horizon)[len(horizon) // 2]
        self.state["cheap"] = today
        self._save()
        if avg > median * 0.8:
            return                                   # kein lohnender Unterschied
        a = datetime.fromtimestamp(horizon[best]["t"]).strftime("%H:%M")
        b = datetime.fromtimestamp(horizon[best]["t"] + n * 900).strftime("%H:%M")
        self.push.send("cheap", f"Billigstrom {a}–{b} Uhr",
                       f"Im Schnitt {avg * 100:.0f} ct/kWh statt {median * 100:.0f} ct – Auto oder Geräte dann laden/starten (in der App planen).",
                       data={"open": "energy"})
