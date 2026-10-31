"""Erzeuger: je Wechselrichter (evcc „pv"-Liste) aktuelle Leistung, Spitze heute und Rekord-Spitze.

Quelle ist die Energie-Aufzeichnung (energy_log.py, eine Zeile pro Minute): „pvp" enthält die Leistung jeder
PV-Quelle in der Reihenfolge von evcc, bei nur einer Quelle zählt „pv". Spitzen sind also Minutenwerte.
Beim ersten Start werden alle vorhandenen Tage durchgesehen (Rekord ab Beginn der Aufzeichnung), danach nur noch
neue Zeilen. Gespeichert in DATA_DIR/producers.json: {title: {peakW, peakT}}, plus bis wohin ausgewertet wurde.
"""
import json
import os
import threading
from datetime import date, datetime, timedelta

from energy_log import MAX_GAP
from hub_config import DATA_DIR

FILE = os.path.join(DATA_DIR, "producers.json")


def _parts(row, n):
    """Leistung je Quelle aus einer Log-Zeile (Liste der Länge n) – None, wenn nicht zuzuordnen."""
    pvp = row.get("pvp")
    if isinstance(pvp, list) and 1 < len(pvp) <= n:   # kürzer = später dazugekommene Quelle (z. B. Hütte) fehlte noch
        return [max(float(x or 0), 0.0) for x in pvp] + [None] * (n - len(pvp))
    if n == 1 and row.get("pv") is not None:
        return [max(float(row["pv"]), 0.0)]
    return None


class Producers:
    def __init__(self, energy_log):
        self.log = energy_log
        self._lock = threading.Lock()
        self.data = self._load()           # {"peaks": {title: {peakW, peakT}}, "upto": t, "since": t, "titles": [...]}

    def _load(self):
        try:
            with open(FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"peaks": {}, "upto": 0}

    def _save(self):
        tmp = FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, FILE)

    def _days(self):
        """Alle Tage mit Aufzeichnung (älteste zuerst)."""
        try:
            names = sorted(n[:10] for n in os.listdir(self.log_dir()) if n.endswith(".jsonl"))
        except OSError:
            return []
        out = []
        for n in names:
            try:
                out.append(date.fromisoformat(n))
            except ValueError:
                pass
        return out

    def log_dir(self):
        import energy_log
        return energy_log.DIR

    def _scan(self, titles, rows):
        peaks = self.data.setdefault("peaks", {})
        upto = self.data.get("upto", 0)
        for r in rows:
            if r.get("t", 0) <= upto:
                continue
            parts = _parts(r, len(titles))
            if parts is None:
                continue
            for title, w in zip(titles, parts):
                if w is None:
                    continue
                p = peaks.get(title)
                if p is None or w > p["peakW"]:
                    peaks[title] = {"peakW": round(w), "peakT": r["t"]}
            total = sum(w for w in parts if w)
            p = peaks.get("_total")
            if p is None or total > p["peakW"]:
                peaks["_total"] = {"peakW": round(total), "peakT": r["t"]}
            self.data["since"] = self.data.get("since") or r["t"]
        if rows:
            self.data["upto"] = max(upto, rows[-1].get("t", 0))

    def update(self, titles):
        """Neue Log-Zeilen auswerten. titles = Namen der PV-Quellen in evcc-Reihenfolge."""
        if not titles:
            return
        with self._lock:
            if self.data.get("titles") != titles:       # Anlage geändert → Zuordnung neu (Rekorde behalten)
                self.data["titles"] = titles
            if not self.data.get("upto"):
                days = self._days()                    # erster Start: alles Vorhandene durchsehen
            else:
                last = datetime.fromtimestamp(self.data["upto"]).date()
                days = [last + timedelta(days=i) for i in range((date.today() - last).days + 1)]
            for d in days:
                self._scan(titles, self.log.rows(d))
            self._save()

    @staticmethod
    def today(titles, rows):
        """Spitze heute und kWh heute je Quelle."""
        n = len(titles)
        peak = [(0.0, None)] * n
        wh = [0.0] * n
        prev = None
        for r in rows:
            parts = _parts(r, n)
            if parts is None:
                prev = None
                continue
            parts = [w or 0.0 for w in parts]
            peak = [(w, r["t"]) if w > pk[0] else pk for w, pk in zip(parts, peak)]
            if prev is not None:
                dt = r["t"] - prev[0]
                if 0 < dt <= MAX_GAP:
                    wh = [e + (a + b) / 2 * dt / 3600 for e, a, b in zip(wh, prev[1], parts)]
            prev = (r["t"], parts)
        return peak, wh

    def report(self, pv_sources):
        """pv_sources: evcc „pv"-Liste (mit title, power, site) → Zeilen für die App."""
        titles = [s.get("title") or s.get("name") or f"PV {i + 1}" for i, s in enumerate(pv_sources)]
        self.update(titles)
        peak_today, wh = self.today(titles, self.log.rows(date.today()))
        peaks = self.data.get("peaks", {})
        out = []
        for i, (s, title) in enumerate(zip(pv_sources, titles)):
            rec = peaks.get(title) or {}
            pw, pt = peak_today[i]
            out.append({"id": s.get("name") or title, "title": title, "site": s.get("site", "house"),
                        "powerW": round(float(s.get("power") or 0)),
                        "peakW": rec.get("peakW"), "peakAt": rec.get("peakT"),
                        "todayPeakW": round(pw), "todayPeakAt": pt, "todayKWh": round(wh[i] / 1000, 2)})
        tot = peaks.get("_total") or {}
        return {"producers": out, "totalPowerW": sum(p["powerW"] for p in out),
                "totalPeakW": tot.get("peakW"), "totalPeakAt": tot.get("peakT"),
                "todayKWh": round(sum(wh) / 1000, 2), "since": self.data.get("since")}
