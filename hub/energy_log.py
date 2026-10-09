"""Energie-Verlauf: Der Hub fragt evcc jede Minute ab und speichert die Werte – rund um die Uhr,
auch wenn keine App offen ist. Daraus entstehen der komplette Tagesgraph und die Tageswerte (kWh).

Ablage: DATA_DIR/energy/JJJJ-MM-TT.jsonl  (eine Zeile pro Minute, ca. 150 KB pro Tag)
Vorzeichen wie evcc: grid + Bezug / − Einspeisung, bat + Entladen / − Laden.
"""
import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import requests

from hub_config import DATA_DIR

DIR = os.path.join(DATA_DIR, "energy")
SUMMARY = os.path.join(DIR, "summary.json")
INTERVAL = 60
MAX_GAP = 10 * 60          # längere Lücken (Hub aus) nicht „überbrücken"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def sample_from_state(s):
    """evcc /api/state → kompakte Zeile."""
    s = s.get("result", s)
    bat = s.get("battery") if isinstance(s.get("battery"), dict) else {}
    grid = s.get("grid") if isinstance(s.get("grid"), dict) else {}
    row = {
        "t": int(time.time()),
        "pv": _num(s.get("pvPower")) or 0.0,
        "home": _num(s.get("homePower")) or 0.0,
        "grid": _num(grid.get("power")) if grid.get("power") is not None else (_num(s.get("gridPower")) or 0.0),
        "bat": _num(bat.get("power")) if bat.get("power") is not None else (_num(s.get("batteryPower")) or 0.0),
        "soc": _num(bat.get("soc")) if bat.get("soc") is not None else _num(s.get("batterySoc")),
    }
    parts = [_num(p.get("power")) or 0.0 for p in (s.get("pv") or []) if isinstance(p, dict)]
    if len(parts) > 1:
        row["pvp"] = parts
    charge = sum(_num(lp.get("chargePower")) or 0.0 for lp in (s.get("loadpoints") or []) if isinstance(lp, dict))
    if charge:
        row["car"] = charge
    return row


def totals(rows):
    """Energie (kWh) aus Leistungswerten – Trapezregel, Lücken > 10 Min. zählen nicht."""
    t = {"pv": 0.0, "home": 0.0, "import": 0.0, "export": 0.0, "car": 0.0, "batIn": 0.0, "batOut": 0.0}
    _integrate(rows, t)
    return {k: round(v / 1000, 2) for k, v in t.items()}


def _integrate(rows, t):
    """Wh je Größe in t aufsummieren (Trapezregel)."""
    for a, b in zip(rows, rows[1:]):
        dt = b["t"] - a["t"]
        if dt <= 0 or dt > MAX_GAP:
            continue
        h = dt / 3600

        def avg(f, key):
            return (f(a.get(key) or 0) + f(b.get(key) or 0)) / 2 * h

        t["pv"] += avg(lambda x: max(x, 0), "pv")
        t["home"] += avg(lambda x: max(x, 0), "home")
        t["import"] += avg(lambda x: max(x, 0), "grid")
        t["export"] += avg(lambda x: max(-x, 0), "grid")
        t["car"] += avg(lambda x: max(x, 0), "car")
        t["batOut"] += avg(lambda x: max(x, 0), "bat")
        t["batIn"] += avg(lambda x: max(-x, 0), "bat")


def hourly(rows):
    """Netzbezug/Einspeisung/PV/Verbrauch je Stunde (kWh) – für die Balken der Tagesansicht."""
    out = []
    for h in range(24):
        sel = [r for r in rows if datetime.fromtimestamp(r["t"]).hour == h]
        t = {"pv": 0.0, "home": 0.0, "import": 0.0, "export": 0.0, "car": 0.0, "batIn": 0.0, "batOut": 0.0}
        _integrate(sel, t)
        out.append({"hour": h, **{k: round(t[k] / 1000, 3) for k in ("pv", "home", "import", "export")}})
    return out


def period_range(period, d):
    """Tag, Woche (Mo–So) oder Monat, in dem d liegt → (erster Tag, letzter Tag)."""
    if period == "week":
        start = d - timedelta(days=d.weekday())
        return start, start + timedelta(days=6)
    if period == "month":
        start = d.replace(day=1)
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start, nxt - timedelta(days=1)
    return d, d


def costs(tot, prices, days_elapsed, period, month_days):
    """Euro-Werte aus kWh und den Preisen des Hauses. Ohne Bezugspreis → None (App zeigt „Preise eintragen")."""
    imp_p = prices.get("import")
    if imp_p is None:
        return None
    exp_p = prices.get("export") or 0.0
    base_m = prices.get("base_month") or 0.0
    # Grundgebühr anteilig für die schon vergangenen Tage (Monat: je Monatstag, sonst Jahresbetrag / 365)
    base = base_m * (days_elapsed / month_days if period == "month" else days_elapsed * 12 / 365)
    c = {"import": tot["import"] * imp_p, "export": tot["export"] * exp_p,
         "saved": tot["selfUse"] * imp_p, "base": base}
    c["balance"] = c["export"] - c["import"] - c["base"]
    return {k: round(v, 2) for k, v in c.items()}


class EnergyLog:
    def __init__(self, evcc_url_fn):
        self.evcc_url_fn = evcc_url_fn
        self.error = None
        os.makedirs(DIR, exist_ok=True)
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- Aufzeichnen
    def _path(self, d):
        return os.path.join(DIR, f"{d.isoformat()}.jsonl")

    def _loop(self):
        while True:
            start = time.time()
            url = (self.evcc_url_fn() or "").rstrip("/")
            if url:
                try:
                    r = requests.get(url + "/api/state", timeout=10)
                    r.raise_for_status()
                    row = sample_from_state(r.json())
                    with self._lock, open(self._path(date.today()), "a") as f:
                        f.write(json.dumps(row, separators=(",", ":")) + "\n")
                    self.error = None
                except Exception as e:          # evcc kurz weg → nächste Minute wieder
                    self.error = str(e)[:200]
            time.sleep(max(5, INTERVAL - (time.time() - start)))

    # ---- Lesen
    def rows(self, d):
        try:
            with open(self._path(d)) as f:
                return [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            return []

    def day(self, d):
        rows = self.rows(d)
        return {"date": d.isoformat(), "samples": rows, "totals": {"date": d.isoformat(), **totals(rows)},
                "error": self.error}

    def _summary(self):
        try:
            with open(SUMMARY) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _day_totals(self, d, cache):
        """Tageswerte, abgeschlossene Tage aus dem Zwischenspeicher. → (werte, cache_geändert)"""
        key = d.isoformat()
        if d >= date.today():
            return totals(self.rows(d)), False
        if key in cache:
            return cache[key], False
        rows = self.rows(d)
        if not rows:
            return None, False
        cache[key] = totals(rows)
        return cache[key], True

    def _save_summary(self, cache):
        tmp = SUMMARY + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, SUMMARY)

    def summary(self, period, d, prices):
        """Kostenübersicht für Tag/Woche/Monat: kWh, Euro und Balken (Stunden bzw. Tage)."""
        period = period if period in ("day", "week", "month") else "day"
        start, end = period_range(period, d)
        today = date.today()
        cache, changed = self._summary(), False
        keys = ("pv", "home", "import", "export")
        tot = {k: 0.0 for k in keys}
        buckets, elapsed = [], 0
        cur = start
        while cur <= end:
            t, ch = self._day_totals(cur, cache) if cur <= today else (None, False)
            changed |= ch
            if cur <= today:
                elapsed += 1
            row = {"date": cur.isoformat(), **{k: (t or {}).get(k, 0.0) for k in keys}}
            for k in keys:
                tot[k] += row[k]
            buckets.append(row)
            cur += timedelta(days=1)
        if changed:
            self._save_summary(cache)
        if period == "day":
            buckets = hourly(self.rows(d))
        tot = {k: round(v, 2) for k, v in tot.items()}
        # Eigenverbrauch = Verbrauch, der nicht aus dem Netz kam (Sonne direkt oder über den Akku)
        tot["selfUse"] = round(max(tot["home"] - tot["import"], 0.0), 2)
        month_days = (period_range("month", start)[1] - period_range("month", start)[0]).days + 1
        return {"period": period, "start": start.isoformat(), "end": end.isoformat(), "totals": tot,
                "buckets": buckets, "prices": prices, "costs": costs(tot, prices, elapsed, period, month_days)}

    def days(self, limit=31):
        """Tageswerte der letzten Tage (abgeschlossene Tage werden zwischengespeichert)."""
        cache = self._summary()
        changed = False
        out = []
        today = date.today()
        for i in range(limit):
            d = today - timedelta(days=i)
            key = d.isoformat()
            if i == 0:
                tot = totals(self.rows(d))
            elif key in cache:
                tot = cache[key]
            else:
                rows = self.rows(d)
                if not rows:
                    continue
                tot = totals(rows)
                cache[key] = tot
                changed = True
            if tot.get("pv") or tot.get("home") or i == 0:
                out.append({"date": key, **tot})
        if changed:
            tmp = SUMMARY + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cache, f)
            os.replace(tmp, SUMMARY)
        return out


def parse_day(s):
    if not s:
        return date.today()
    return datetime.strptime(s, "%Y-%m-%d").date()
