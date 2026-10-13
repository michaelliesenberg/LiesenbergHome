"""Wärmepumpe (alpha innotec / Novelan mit Luxtronik 2.x).

Lesen: Befehle 3004 („Berechnungen") und 3003 („Parameter") an Port 8889.
Schreiben (vom Besitzer freigegeben): NUR drei dokumentierte Parameter – Warmwasser-Soll (105, 40–60 °C),
Betriebsart Heizen (3) und Warmwasser (4: Automatik/Zusätzl. Wärmeerzeuger/Party/Ferien/Aus). Jede Änderung wird
zurückgelesen und protokolliert (DATA_DIR/heatpump-writes.log). Alles andere bleibt unangetastet. Alle 60 s abgefragt, Verlauf (5 Min.) für 7 Tage gespeichert.
Konfiguration: hub_config „heatpump.host" (z. B. 192.168.178.87).
"""
import json
import os
import socket
import struct
import threading
import time
from collections import deque

from hub_config import DATA_DIR

HISTORY = os.path.join(DATA_DIR, "heatpump-history.json")
WRITE_LOG = os.path.join(DATA_DIR, "heatpump-writes.log")
BA = {0: "Automatik", 1: "Zusätzl. Wärmeerzeuger", 2: "Party", 3: "Ferien", 4: "Aus"}
PARAM = {"hotWaterTarget": 105, "heatingMode": 3, "hotWaterMode": 4, "coolingMode": 108}
COOL = {0: "Aus", 1: "Automatik"}

MODE = {0: "Heizen", 1: "Warmwasser", 2: "Schwimmbad/Solar", 3: "EVU-Sperre", 4: "Abtauen", 5: "Keine Anforderung",
        6: "Heizen (ext. Quelle)", 7: "Kühlen"}
STATUS1 = {0: "Wärmepumpe läuft", 1: "Wärmepumpe steht", 2: "Wärmepumpe kommt", 3: "Störung", 4: "Abtauen",
           5: "Warte auf LIN-Verbindung", 6: "Verdichter heizt auf", 7: "Pumpenvorlauf"}
STATUS3 = {0: "Heizen", 1: "Keine Anforderung", 2: "Netz-Einschaltverzögerung", 3: "Schaltspielsperre", 4: "Sperrzeit",
           5: "Warmwasser", 6: "Estrich-Programm", 7: "Abtauen", 8: "Pumpenvorlauf", 9: "Thermische Desinfektion",
           10: "Kühlen", 12: "Schwimmbad/Solar", 13: "Heizen (ext. Energie)", 14: "Warmwasser (ext. Energie)",
           16: "Durchflussüberwachung", 17: "Zweiter Wärmeerzeuger aktiv"}


EFF = os.path.join(DATA_DIR, "heatpump-efficiency.json")


class HeatPump:
    """Effizienz: Luxtronik misst nur die Wärmemenge. Die elektrische Leistung lernt der Hub selbst – aus dem Sprung
    im Hausverbrauch (evcc), wenn der Verdichter an- bzw. ausgeht. Daraus: Strom ≈ Leistung × Verdichterlaufzeit,
    COP = Wärme ÷ Strom (je Tag und gesamt). Ist nur eine Schätzung – mit eigenem Zähler für die WP wäre es genau."""

    def __init__(self, host_fn, house_power_fn=None):
        self.host_fn = host_fn
        self.house_power_fn = house_power_fn          # → Liste (zeit, Watt) der letzten Minuten
        self.eff = self._load_eff()
        self._prev_comp = None
        self.data = None
        self.error = None
        self.history = deque(self._load(), maxlen=7 * 24 * 12)
        self._last_hist = 0
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def configured(self):
        return bool(self.host_fn())

    def _load_eff(self):
        try:
            with open(EFF) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"samples": [], "days": {}}

    def _save_eff(self):
        tmp = EFF + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.eff, f)
        os.replace(tmp, EFF)

    def _learn_power(self, turned_on, t_switch):
        """Hausverbrauch vor/nach dem Schalten vergleichen (wie bei den Lichtern)"""
        if not self.house_power_fn:
            return

        def go():
            time.sleep(150)                     # Verdichter läuft hoch, evcc rechnet alle 30 s neu
            buf = self.house_power_fn() or []
            before = [w for t, w in buf if t_switch - 90 <= t <= t_switch - 5]
            after = [w for t, w in buf if t_switch + 60 <= t <= t_switch + 150]
            if len(before) < 3 or len(after) < 3:
                return
            import statistics
            if statistics.pstdev(before) > 150 or statistics.pstdev(after) > 250:
                return                          # zu unruhig (Kochen, Waschmaschine …)
            d = statistics.median(after) - statistics.median(before)
            w = d if turned_on else -d
            if 300 <= w <= 8000:
                self.eff["samples"] = (self.eff.get("samples", []) + [round(w)])[-30:]
                self._save_eff()
                print(f"[WÄRMEPUMPE] Leistung gemessen: {w:.0f} W ({len(self.eff['samples'])} Messungen)", flush=True)
        threading.Thread(target=go, daemon=True).start()

    def power_w(self):
        import statistics
        s = self.eff.get("samples") or []
        return round(statistics.median(s)) if len(s) >= 3 else None

    def _track_day(self, c):
        """Tageswerte: Zähler Wärme (kWh) und Verdichter-Laufzeit (s) um Mitternacht merken"""
        day = time.strftime("%Y-%m-%d")
        heat = (c[151] + c[152]) / 10 if len(c) > 152 else None
        run = c[56] if len(c) > 56 else None
        if heat is None or run is None:
            return
        days = self.eff.setdefault("days", {})
        if day not in days:
            days[day] = {"heat0": heat, "run0": run}
            for k in sorted(days)[:-40]:
                days.pop(k, None)
        days[day].update(heat=heat, run=run)
        if int(time.time()) % 600 < 60:
            self._save_eff()

    def efficiency(self):
        p = self.power_w()
        days = []
        for day, v in sorted((self.eff.get("days") or {}).items())[-14:]:
            dh, dr = v["heat"] - v["heat0"], (v["run"] - v["run0"]) / 3600
            el = (p / 1000) * dr if p else None
            days.append({"day": day, "heatKWh": round(dh, 1), "hours": round(dr, 1),
                         "elecKWh": round(el, 1) if el else None,
                         "cop": round(dh / el, 2) if el and el > 0.3 and dh > 0 else None})
        total = None
        if p and self.data and self.data.get("compressorHours"):
            el = p / 1000 * self.data["compressorHours"]
            total = round((self.data.get("heatEnergyTotal") or 0) / el, 2) if el else None
        return {"powerW": p, "measurements": len(self.eff.get("samples") or []), "days": days, "copTotal": total,
                "note": "geschätzt: Strom aus gelernter Verdichterleistung × Laufzeit"}

    def _load(self):
        try:
            with open(HISTORY) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _save(self):
        tmp = HISTORY + ".tmp"
        with open(tmp, "w") as f:
            json.dump(list(self.history), f)
        os.replace(tmp, HISTORY)

    # ------------------------------------------------------------------ Lesen (Befehl 3004, sonst nichts)
    def _read(self, host):
        with socket.create_connection((host, 8889), timeout=8) as s:
            s.settimeout(8)
            s.sendall(struct.pack(">ii", 3004, 0))

            def ri():
                b = b""
                while len(b) < 4:
                    x = s.recv(4 - len(b))
                    if not x:
                        raise EOFError("Verbindung beendet")
                    b += x
                return struct.unpack(">i", b)[0]

            if ri() != 3004:
                raise RuntimeError("unerwartete Antwort")
            ri()                                   # Status
            n = ri()
            if not 100 < n < 2000:
                raise RuntimeError(f"unerwartete Länge {n}")
            return [ri() for _ in range(n)]

    def _params(self, host):
        with socket.create_connection((host, 8889), timeout=8) as s:
            s.settimeout(8)
            s.sendall(struct.pack(">ii", 3003, 0))

            def ri():
                b = b""
                while len(b) < 4:
                    x = s.recv(4 - len(b))
                    if not x:
                        raise EOFError("Verbindung beendet")
                    b += x
                return struct.unpack(">i", b)[0]

            if ri() != 3003:
                raise RuntimeError("unerwartete Antwort")
            n = ri()
            if not 100 < n < 3000:
                raise RuntimeError(f"unerwartete Länge {n}")
            return [ri() for _ in range(n)]

    def settings(self):
        host = self.host_fn()
        p = self._params(host)
        return {"hotWaterTarget": p[105] / 10, "heatingMode": p[3], "hotWaterMode": p[4],
                "heatingModeText": BA.get(p[3]), "hotWaterModeText": BA.get(p[4]), "modes": BA,
                "coolingMode": p[108] if len(p) > 108 else None, "coolingModeText": COOL.get(p[108]) if len(p) > 108 else None,
                "coolingReleaseTemp": p[110] / 10 if len(p) > 110 else None}

    def write(self, what, value, by=""):
        """Nur die drei freigegebenen Werte; Grenzen prüfen, schreiben, zurücklesen, protokollieren."""
        if what not in PARAM:
            raise ValueError("Diese Einstellung lässt sich nicht ändern")
        if what == "hotWaterTarget":
            v = float(value)
            if not 40 <= v <= 60:
                raise ValueError("Warmwasser-Soll nur 40–60 °C")
            raw = int(round(v * 10))
        elif what == "coolingMode":
            raw = int(value)
            if raw not in COOL:
                raise ValueError("Kühlen nur Aus oder Automatik")
        else:
            raw = int(value)
            if raw not in BA:
                raise ValueError("Unbekannte Betriebsart")
        host = self.host_fn()
        before = self._params(host)[PARAM[what]]
        if before != raw:
            with socket.create_connection((host, 8889), timeout=8) as s:
                s.settimeout(8)
                s.sendall(struct.pack(">iii", 3002, PARAM[what], raw))
                s.recv(8)
        time.sleep(1.5)
        after = self._params(host)[PARAM[what]]
        line = f"{time.strftime('%F %T')} {by}: {what} (Parameter {PARAM[what]}) {before} → {raw}, gelesen {after}\n"
        with open(WRITE_LOG, "a") as f:
            f.write(line)
        print(f"[WÄRMEPUMPE] {line.strip()}", flush=True)
        if after != raw:
            raise RuntimeError("Die Wärmepumpe hat den Wert nicht übernommen")
        return self.settings()

    def _parse(self, c):
        t = lambda i: round(c[i] / 10, 1) if i < len(c) else None
        e = lambda i: round(c[i] / 10, 1) if i < len(c) and c[i] >= 0 else None
        heat_out = c[257] if len(c) > 257 and 0 <= c[257] < 50000 else None    # Heizleistung in W (nicht jedes Modell)
        return {
            "outside": t(15), "outsideAvg": t(16),
            "flow": t(10), "return": t(11), "returnTarget": t(12),
            "hotWater": t(17), "hotWaterTarget": t(18),
            "hotGas": t(14), "sourceIn": t(19), "sourceOut": t(20),
            "compressor": bool(c[44]) if len(c) > 44 else None,
            "mode": MODE.get(c[80], f"Code {c[80]}") if len(c) > 80 else None,
            "status": STATUS1.get(c[117]) if len(c) > 117 else None,
            "statusDetail": STATUS3.get(c[119]) if len(c) > 119 else None,
            "compressorHours": round(c[56] / 3600) if len(c) > 56 else None,
            "compressorStarts": c[57] if len(c) > 57 else None,
            "heatEnergyHeating": e(151), "heatEnergyHotWater": e(152),
            "heatEnergyTotal": round((e(151) or 0) + (e(152) or 0), 1),
            "heatOutput": heat_out,
            "flowRate": c[155] if len(c) > 155 and c[155] >= 0 else None,       # l/h
            "coolingReleased": bool(c[146]) if len(c) > 146 else None,
            "firmware": "".join(chr(x) for x in c[81:91] if 31 < x < 127) if len(c) > 90 else None,
            "updated": int(time.time()),
        }

    def _loop(self):
        time.sleep(25)
        while True:
            host = self.host_fn()
            if host:
                try:
                    raw = self._read(host)
                    d = self._parse(raw)
                    # direkt nach einem Neustart liefert die Steuerung kurz nur Nullen → überspringen
                    if any(d[k] not in (None, 0) for k in ("outside", "flow", "return", "hotWater")):
                        self.data, self.error = d, None
                        self._track_day(raw)
                        comp = d.get("compressor")
                        if self._prev_comp is not None and comp is not None and comp != self._prev_comp:
                            self._learn_power(bool(comp), time.time())
                        self._prev_comp = comp
                        if time.time() - self._last_hist >= 300:
                            self._last_hist = time.time()
                            self.history.append({k: d[k] for k in ("updated", "outside", "flow", "return", "hotWater",
                                                                    "compressor", "heatOutput")})
                            if len(self.history) % 12 == 0:
                                self._save()
                except Exception as ex:
                    self.error = f"Wärmepumpe nicht erreichbar ({type(ex).__name__})"
            time.sleep(60)

    def state(self):
        eff = self.efficiency()
        d = self.data
        if d and d.get("compressor") and d.get("heatOutput") and eff.get("powerW"):
            eff["copNow"] = round(d["heatOutput"] / eff["powerW"], 2)
        return {"configured": self.configured, "error": self.error, "data": d, "efficiency": eff}

    def hist(self, hours=24):
        since = time.time() - hours * 3600
        return [h for h in self.history if h["updated"] >= since]
