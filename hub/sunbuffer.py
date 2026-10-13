"""„Sonnenpuffer“ – nur messen und simulieren, NICHTS an der Wärmepumpe verstellen.

Idee (noch nicht aktiv): Wenn mittags Sonnenstrom übrig ist und die Nacht kalt wird, Warmwasser auf 55–57 °C und
Heizung +2 K, damit Speicher und Estrich Wärme aufnehmen – nachts läuft die Wärmepumpe dann weniger mit Netzstrom.

Dieses Modul zeichnet alle 5 Minuten auf (DATA_DIR/sunbuffer/JJJJ-MM-TT.jsonl): Außentemperatur, Verdichter an/aus,
Betriebsart, geschätzte Leistung der Wärmepumpe, Akkustand, Netz (Bezug/Einspeisung), PV, Akku-Leistung.
Daraus rechnet report() je Tag: Hätte der Sonnenpuffer gegriffen (wann)? Wie viel Wärmepumpen-Strom kam in der
folgenden Nacht aus dem Netz? Wie viel davon hätte man verschieben können und was hätte das gespart (Schätzung)?
"""
import json
import os
import threading
import time
from datetime import date, datetime, timedelta

from hub_config import DATA_DIR

DIR = os.path.join(DATA_DIR, "sunbuffer")
STEP = 300
# Bedingungen, unter denen der Sonnenpuffer greifen würde
SOC_MIN = 90            # %
SURPLUS_MIN = 2000      # W (Einspeisung + Akku-Laden)
NIGHT_LOW_MAX = 5.0     # °C Tiefstwert der kommenden Nacht
MIN_SAMPLES = 3         # 15 Minuten am Stück
# Wie viel Wärme sich puffern lässt (grob): Warmwasser 300 l × 6 K ≈ 2,1 kWh + Estrich +2 K ≈ 6 kWh Wärme
BUFFER_HEAT_KWH = 8.0
COP_DAY, COP_NIGHT = 4.0, 3.2


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class SunBuffer:
    def __init__(self, heatpump, evcc_state_fn, forecast, sun_fn, prices_fn):
        self.hp, self.evcc_state_fn, self.fc, self.sun_fn, self.prices_fn = heatpump, evcc_state_fn, forecast, sun_fn, prices_fn
        os.makedirs(DIR, exist_ok=True)
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- Aufzeichnen (nur lesen)
    def _loop(self):
        time.sleep(60)
        while True:
            start = time.time()
            try:
                self._sample()
            except Exception as e:
                print(f"[SONNENPUFFER] {e}", flush=True)
            time.sleep(max(30, STEP - (time.time() - start)))

    def _sample(self):
        if not self.hp.configured or not self.hp.data:
            return
        d = self.hp.data
        s = self.evcc_state_fn() or {}
        s = s.get("result", s) if isinstance(s, dict) else {}
        bat = s.get("battery") if isinstance(s.get("battery"), dict) else {}
        grid = s.get("grid") if isinstance(s.get("grid"), dict) else {}
        row = {"t": int(time.time()), "out": d.get("outside"), "comp": bool(d.get("compressor")), "mode": d.get("mode"),
               "hpW": self.hp.power_w() if d.get("compressor") else 0,
               "soc": _num(bat.get("soc", s.get("batterySoc"))),
               "grid": _num(grid.get("power", s.get("gridPower"))),
               "bat": _num(bat.get("power", s.get("batteryPower"))),
               "pv": _num(s.get("pvPower"))}
        with open(os.path.join(DIR, f"{date.today().isoformat()}.jsonl"), "a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def _rows(self, d):
        try:
            with open(os.path.join(DIR, f"{d.isoformat()}.jsonl")) as f:
                return [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            return []

    # ---- Auswertung
    def _day(self, d, prices):
        rows = self._rows(d)
        if not rows:
            return None
        sunset = self.sun_fn(d, False) or datetime(d.year, d.month, d.day, 18)
        sunrise_next = self.sun_fn(d + timedelta(days=1), True) or datetime(d.year, d.month, d.day, 7) + timedelta(days=1)
        night = [r for r in rows + self._rows(d + timedelta(days=1)) if sunset.timestamp() <= r["t"] < sunrise_next.timestamp()]
        night_done = time.time() > sunrise_next.timestamp()
        low = min((r["out"] for r in night if r.get("out") is not None), default=None)

        # Tag: wann wäre genug Sonne übrig gewesen?
        day = [r for r in rows if 9 <= datetime.fromtimestamp(r["t"]).hour and r["t"] < sunset.timestamp()]
        ok, run, window, surplus_wh = [], 0, None, 0.0
        for r in day:
            sur = max(-(r.get("grid") or 0), 0) + max(-(r.get("bat") or 0), 0)
            good = (r.get("soc") or 0) >= SOC_MIN and sur >= SURPLUS_MIN
            run = run + 1 if good else 0
            if good:
                surplus_wh += sur * STEP / 3600
            if run >= MIN_SAMPLES and window is None:
                window = r["t"] - (MIN_SAMPLES - 1) * STEP
        cold = low is not None and low < NIGHT_LOW_MAX
        triggered = window is not None and cold

        # Nacht: Wärmepumpen-Strom und wie viel davon aus dem Netz kam
        hp_wh = grid_hp_wh = 0.0
        for r in night:
            w = r.get("hpW") or 0
            if w:
                hp_wh += w * STEP / 3600
                grid_hp_wh += min(w, max(r.get("grid") or 0, 0)) * STEP / 3600
        # Verschiebbar: was der Puffer aufnehmen kann, was an Sonne übrig war, höchstens der Netzanteil
        buffer_el = BUFFER_HEAT_KWH / COP_DAY
        shift = min(grid_hp_wh / 1000, surplus_wh / 1000, buffer_el) if triggered else 0.0
        imp, exp = prices.get("import") or 0.30, prices.get("export") or 0.08
        # gespart: Netzstrom nachts vermieden, dafür tagsüber Sonnenstrom nicht eingespeist; plus besserer COP am Tag
        night_kwh_avoided = shift * COP_DAY / COP_NIGHT
        saving = night_kwh_avoided * imp - shift * exp if shift else 0.0
        return {"date": d.isoformat(), "triggered": triggered, "window": window,
                "surplusKWh": round(surplus_wh / 1000, 1), "nightLow": low, "cold": cold,
                "nightHpKWh": round(hp_wh / 1000, 2), "nightGridHpKWh": round(grid_hp_wh / 1000, 2),
                "shiftKWh": round(shift, 2), "saving": round(max(saving, 0), 2), "complete": night_done,
                "samples": len(rows)}

    def report(self, days=21):
        prices = self.prices_fn() or {}
        out = []
        for i in range(days, -1, -1):
            r = self._day(date.today() - timedelta(days=i), prices)
            if r:
                out.append(r)
        done = [r for r in out if r["complete"]]
        tot = {"days": len(done), "triggeredDays": sum(r["triggered"] for r in done),
               "nightGridHpKWh": round(sum(r["nightGridHpKWh"] for r in done), 1),
               "shiftKWh": round(sum(r["shiftKWh"] for r in done), 1),
               "saving": round(sum(r["saving"] for r in done), 2)}
        if done:
            tot["perYear"] = round(tot["saving"] / len(done) * 180, 0)      # grob: ~180 Heiztage
        return {"since": out[0]["date"] if out else None, "days": out, "totals": tot,
                "rules": {"socMin": SOC_MIN, "surplusMinW": SURPLUS_MIN, "nightLowMax": NIGHT_LOW_MAX,
                          "bufferHeatKWh": BUFFER_HEAT_KWH, "copDay": COP_DAY, "copNight": COP_NIGHT},
                "writes": False}
