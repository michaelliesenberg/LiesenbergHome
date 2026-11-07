"""JUDO-Enthärtungsanlage (i-soft mit i-safe Leckageschutz) über das Connectivity-Modul im Heimnetz.

Das Modul hat eine kleine REST-API (http://<modul>/api/rest/<Register>, Basic-Auth, ab Werk admin / Connectivity).
Antwort immer {"data": "<hex>"}. Wichtig: Das Modul verträgt nur etwa eine Anfrage alle 10 Sekunden –
schnellere Anfragen kommen leer zurück. Deshalb läuft hier alles nacheinander mit 11 s Abstand.

Register (aus JUDOs eigener HA-Integration JUDO1936/judo-isoft_PRO-hacs und mibragri/ha-judo-isoft):
  FF00 Gerätetyp (1 Byte) · 0600 Gerätenummer (4 Byte LE) · 0100 Softwarestand
  2800 Gesamtwasser (Liter, 4 Byte LE) · 2900 Weichwasser (Liter, 4 Byte LE)
  5100 Wunsch-Resthärte °dH (1 Byte) · 5600 Salz: Byte 0-1 Vorrat g (LE), Byte 2-3 Reichweite Tage (LE)
  3C00 Leckageschutz: Wasser ZU · 3D00 Wasser AUF · 4100/4200 Urlaubsmodus an/aus · 350000 Regeneration starten

Die Anlage meldet den Ventilzustand über diese API nicht zuverlässig zurück. Der Hub merkt sich deshalb,
was zuletzt geschaltet wurde (und sagt das auch so in der App).

Tagesverbrauch: aus dem Gesamtzähler (Liter seit Mitternacht), nicht aus der Tagesstatistik des Geräts –
die springt im laufenden Tag (Erfahrung aus mibragri/ha-judo-isoft).

Mitteilungen (Abo „water“): Salz geht zur Neige · Wasser läuft seit Stunden ununterbrochen (Leck-Verdacht).
"""
import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import requests

from hub_config import DATA_DIR

DIR = os.path.join(DATA_DIR, "judo")
STATE_FILE = os.path.join(DIR, "state.json")
GAP = 11.0                 # Sekunden zwischen zwei Anfragen (Firmware-Grenze ~10 s)
POLL = 300                 # alle 5 Minuten Zähler + Salz lesen
KEEP_DAYS = 800             # Tageswerte (Vorjahresvergleich)
KEEP_HOURS_DAYS = 400       # Tageskurven
SALT_WARN_DAYS = 7         # ab dieser Reichweite (Tage) einmal am Tag erinnern
LEAK_HOURS = 2             # so lange ununterbrochen Wasser → Leck-Verdacht
LEAK_MIN_L_PER_POLL = 1    # „läuft“ = mindestens 1 Liter pro 5 Minuten

DEVICE_TYPES = {0x32: "i-soft", 0x33: "i-soft SAFE+", 0x34: "Softwell P", 0x35: "Softwell S", 0x36: "Softwell K",
                0x37: "i-soft TGA", 0x42: "i-soft K SAFE+", 0x43: "i-soft K", 0x44: "ZEWA / PROM-i-SAFE",
                0x47: "Softwell KP", 0x4B: "i-soft Pro S", 0x4C: "i-soft Pro L", 0x4E: "i-soft C SAFE",
                0x4F: "i-soft C", 0x53: "i-soft", 0x56: "i-soft SAFE", 0x57: "i-soft SAFE+", 0x58: "i-soft Pro",
                0x67: "i-soft K SAFE+", 0x68: "ZEWA / PROM-i-SAFE"}


def le(hexstr):
    return int.from_bytes(bytes.fromhex(hexstr), "little")


def parse_salt(hexstr):
    """5600 → (Vorrat in g, Reichweite in Tagen)."""
    if not hexstr or len(hexstr) < 8:
        return None, None
    return le(hexstr[0:4]), le(hexstr[4:8])


def parse_version(hexstr):
    if hexstr and len(hexstr) >= 6:
        return f"{int(hexstr[4:6], 16)}.{int(hexstr[2:4], 16):02d}"
    return hexstr or None


class JudoError(Exception):
    pass


class _Skip(Exception):
    pass


class Judo:
    def __init__(self, get_cfg, push=None, clock=time.time, start=True):
        self.get_cfg, self.push, self.clock = get_cfg, push, clock
        self._io = threading.Lock()            # genau eine Anfrage gleichzeitig, mit Abstand
        self._lock = threading.Lock()          # Zustand
        self._last_call = 0.0
        self._urgent = 0                       # wartende Schaltbefehle → laufende Abfrage bricht ab
        self.connected = False
        self.error = None
        self.info = {}                         # type, number, version
        self.live = {}                         # total_l, soft_l, salt_g, salt_days, hardness, updated
        os.makedirs(DIR, exist_ok=True)
        self.st = self._load()                 # valve, vacation, days{iso: liter}, day_start{date,total}, flow[], notes
        if start:
            threading.Thread(target=self._loop, daemon=True).start()

    # ---- Einstellungen
    def _cfg(self):
        return self.get_cfg() or {}

    @property
    def configured(self):
        return bool((self._cfg().get("host") or "").strip())

    # ---- Speicher
    def _load(self):
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        d.setdefault("valve", None)            # "open" | "closed" | None (unbekannt)
        d.setdefault("valve_ts", None)
        d.setdefault("vacation", False)
        d.setdefault("days", {})               # Tag → Liter
        d.setdefault("hours", {})              # Tag → 24 Werte (Liter je Stunde) – Tageskurve
        d.setdefault("months", {})             # „2026-11“ → Liter (bleibt für immer)
        d.setdefault("last", None)             # [ts, Zählerstand] der letzten Abfrage
        d.pop("day_start", None)
        d.setdefault("samples", [])            # [ts, total_l] der letzten Stunden (Leck-Erkennung, Durchfluss)
        d.setdefault("notes", {})              # zuletzt geschickte Mitteilungen (Tag je Art)
        return d

    def _save(self):
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.st, f)
        os.replace(tmp, STATE_FILE)

    # ---- REST
    def _request(self, method, register, expect=True):
        c = self._cfg()
        host = (c.get("host") or "").strip().replace("http://", "").replace("https://", "").rstrip("/")
        if not host:
            raise JudoError("JUDO nicht eingerichtet")
        auth = (c.get("user") or "admin", c.get("password") or "Connectivity")
        with self._io:
            wait = GAP - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                r = requests.request(method, f"http://{host}/api/rest/{register}", auth=auth, timeout=12,
                                     allow_redirects=False)
            except requests.RequestException as e:
                raise JudoError(f"nicht erreichbar: {e}") from e
            finally:
                self._last_call = time.monotonic()
        if r.status_code == 401:
            raise JudoError("Benutzer oder Passwort falsch")
        if r.status_code != 200:
            raise JudoError(f"HTTP {r.status_code} bei {register}")
        try:
            data = (r.json() or {}).get("data")
        except ValueError:
            data = None
        if expect and not data:
            raise JudoError(f"leere Antwort bei {register}")
        return data

    def test(self):
        """Verbindung prüfen (Einrichtungsseite): Typ, Nummer, Softwarestand."""
        t = self._request("GET", "FF00")
        code = int(t[:2], 16)
        num = self._request("GET", "0600")
        ver = self._request("GET", "0100", expect=False)
        self.info = {"type": DEVICE_TYPES.get(code, f"JUDO (Typ 0x{code:02X})"), "number": str(le(num)),
                     "version": parse_version(ver)}
        self.connected, self.error = True, None
        return self.info

    # ---- Lesen
    def _read(self, register, expect=True):
        if self._urgent:                       # Schaltbefehl hat Vorrang – Abfrage beim nächsten Mal
            raise _Skip()
        return self._request("GET", register, expect)

    def poll(self):
        try:
            total = le(self._read("2800"))
            soft = le(self._read("2900"))
            salt_g, salt_days = parse_salt(self._read("5600", expect=False))
            hard = self._read("5100", expect=False)
        except _Skip:
            return False
        self._record(total, soft, salt_g, salt_days, int(hard[:2], 16) if hard else None)
        return True

    def _command(self, register):
        self._urgent += 1
        try:
            self._request("POST", register, expect=False)
        finally:
            self._urgent -= 1

    def _add(self, t0, t1, liters):
        """Liter zwischen zwei Abfragen anteilig auf die Stunden verteilen (über Mitternacht hinweg richtig)."""
        if liters <= 0:
            return
        if t1 - t0 > 48 * 3600 or t1 <= t0:        # lange Lücke (Hub aus) → alles auf die aktuelle Stunde
            t0 = t1 - 1
        span, t = t1 - t0, t0
        while t < t1:
            dt = datetime.fromtimestamp(t)
            nxt = min(t1, (dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp())
            part = liters * (nxt - t) / span
            d = dt.date().isoformat()
            h = self.st["hours"].setdefault(d, [0.0] * 24)
            h[dt.hour] = round(h[dt.hour] + part, 2)
            self.st["days"][d] = round(self.st["days"].get(d, 0) + part, 2)
            m = d[:7]
            self.st["months"][m] = round(self.st["months"].get(m, 0) + part, 2)
            t = nxt

    def _record(self, total, soft, salt_g, salt_days, hardness):
        now = self.clock()
        today = datetime.fromtimestamp(now).date().isoformat()
        with self._lock:
            last = self.st.get("last")
            if last and total >= last[1]:
                self._add(last[0], now, total - last[1])
            # total < letzter Stand: Zähler neu gestartet → nur neu ansetzen
            self.st["last"] = [now, total]
            self.st["days"].setdefault(today, 0)
            self.st["hours"].setdefault(today, [0.0] * 24)
            for key, keep in (("days", KEEP_DAYS), ("hours", KEEP_HOURS_DAYS)):
                for k in sorted(self.st[key])[:-keep]:
                    del self.st[key][k]
            s = self.st["samples"]
            s.append([int(now), total])
            cut = now - (LEAK_HOURS + 1) * 3600
            self.st["samples"] = [x for x in s if x[0] >= cut]
            self.live = {"total_l": total, "soft_l": soft, "salt_g": salt_g, "salt_days": salt_days,
                         "hardness": hardness, "updated": int(now)}
            self.connected, self.error = True, None
            self._save()
        self._check_alerts(salt_days)

    def flow_lph(self):
        """Durchfluss der letzten Minuten in l/h, aus zwei Zählerständen."""
        s = self.st.get("samples") or []
        if len(s) < 2 or s[-1][0] <= s[-2][0]:
            return None
        return round((s[-1][1] - s[-2][1]) * 3600 / (s[-1][0] - s[-2][0]))

    def running_since(self):
        """Seit wann (ts) läuft ununterbrochen Wasser? None = gerade nicht."""
        s = self.st.get("samples") or []
        since = None
        for i in range(len(s) - 1, 0, -1):
            if s[i][1] - s[i - 1][1] >= LEAK_MIN_L_PER_POLL and s[i][0] - s[i - 1][0] <= POLL * 2.5:
                since = s[i - 1][0]
            else:
                break
        return since

    # ---- Mitteilungen
    def _note_once(self, kind, title, body):
        today = datetime.fromtimestamp(self.clock()).date().isoformat()
        if self.st["notes"].get(kind) == today:
            return False
        self.st["notes"][kind] = today
        self._save()
        if self.push:
            self.push.send("water", title, body, data={"open": "water"})
        print(f"[JUDO] {title} – {body}", flush=True)
        return True

    def _check_alerts(self, salt_days):
        if salt_days is not None and 0 <= salt_days <= SALT_WARN_DAYS:
            self._note_once("salt", "🧂 Salz nachfüllen",
                            "Das Regeneriersalz der Enthärtung reicht nur noch "
                            + ("heute." if salt_days == 0 else f"etwa {salt_days} Tag{'e' if salt_days != 1 else ''}."))
        since = self.running_since()
        if since and self.clock() - since >= LEAK_HOURS * 3600 and self.st.get("valve") != "closed":
            h = int((self.clock() - since) // 3600)
            self._note_once("leak", "💧 Wasser läuft seit über %d Stunden" % h,
                            "Ununterbrochener Verbrauch – vielleicht ein Leck oder ein offener Hahn. "
                            "In der App kannst du das Wasser absperren.")

    # ---- Schalten
    def set_valve(self, open_):
        self._command("3D00" if open_ else "3C00")
        with self._lock:
            self.st["valve"] = "open" if open_ else "closed"
            self.st["valve_ts"] = int(self.clock())
            self._save()
        return self.state()

    def set_vacation(self, on):
        self._command("4100" if on else "4200")
        with self._lock:
            self.st["vacation"] = bool(on)
            self._save()
        return self.state()

    def regenerate(self):
        self._command("350000")
        return self.state()

    # ---- Auswertung (Tag / Monat / Jahr) und Kosten
    @staticmethod
    def cost(liters, days, prices):
        """Kosten in € für eine Menge und einen Zeitraum: (Wasser + Abwasser) je m³ + anteilige Grundgebühr."""
        p = prices or {}
        per_m3 = (p.get("water") or 0) + (p.get("sewage") or 0)
        if not per_m3 and not p.get("base_year"):
            return None
        return round(liters / 1000 * per_m3 + (p.get("base_year") or 0) * days / 365, 2)

    def summary(self, period="day", day=None, prices=None):
        """period day: 24 Stundenwerte · month: Tageswerte · year: Monatswerte. day = ISO-Datum im Zeitraum."""
        ref = date.fromisoformat(day) if day else datetime.fromtimestamp(self.clock()).date()
        today = datetime.fromtimestamp(self.clock()).date()
        if period == "year":
            pts = [{"label": f"{m:02d}", "key": f"{ref.year}-{m:02d}", "l": self.st["months"].get(f"{ref.year}-{m:02d}")}
                   for m in range(1, 13)]
            prev = sum(v for k, v in self.st["months"].items() if k.startswith(f"{ref.year - 1}-")) or None
            start, end = date(ref.year, 1, 1), date(ref.year, 12, 31)
        elif period == "month":
            first = ref.replace(day=1)
            nxt = (first + timedelta(days=32)).replace(day=1)
            pts = []
            d = first
            while d < nxt:
                pts.append({"label": str(d.day), "key": d.isoformat(), "l": self.st["days"].get(d.isoformat())})
                d += timedelta(days=1)
            pm = (first - timedelta(days=1)).strftime("%Y-%m")
            prev = self.st["months"].get(pm)
            start, end = first, nxt - timedelta(days=1)
        else:
            period = "day"
            h = self.st["hours"].get(ref.isoformat())
            pts = [{"label": f"{i:02d}", "l": (h[i] if h else None)} for i in range(24)]
            if ref == today and h:
                now_h = datetime.fromtimestamp(self.clock()).hour
                for i in range(now_h + 1, 24):
                    pts[i]["l"] = None                       # Zukunft nicht als 0 zeichnen
            prev = self.st["days"].get((ref - timedelta(days=1)).isoformat())
            start = end = ref
        vals = [x["l"] for x in pts if x["l"] is not None]
        total = round(sum(vals), 1) if vals else None
        # Durchschnitt pro Tag im Zeitraum (nur gemessene Tage, ohne heute)
        days = [v for k, v in self.st["days"].items() if start.isoformat() <= k <= end.isoformat() and k != today.isoformat()]
        avg = round(sum(days) / len(days)) if days else None
        span = (min(end, today) - start).days + 1
        out = {"period": period, "date": ref.isoformat(), "points": pts, "total_l": total,
               "prev_l": round(prev, 1) if prev else None, "avg_day_l": avg}
        if prices is not None:
            out["cost"] = self.cost(total or 0, max(span, 1), prices) if total is not None else None
            out["cost_per_m3"] = round((prices.get("water") or 0) + (prices.get("sewage") or 0), 2) or None
        return out

    # ---- Für die App
    def history(self, days=30):
        out = []
        today = datetime.fromtimestamp(self.clock()).date()
        for i in range(days - 1, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            out.append({"date": d, "liters": self.st["days"].get(d)})
        return out

    def state(self):
        c = self._cfg()
        lv = self.live
        since = self.running_since()
        today = datetime.fromtimestamp(self.clock()).date().isoformat()
        return {
            "configured": self.configured, "connected": self.connected, "error": self.error,
            "host": c.get("host") or "", "device": self.info,
            "today_l": self.st["days"].get(today), "total_m3": round(lv["total_l"] / 1000, 2) if lv.get("total_l") is not None else None,
            "soft_m3": round(lv["soft_l"] / 1000, 2) if lv.get("soft_l") is not None else None,
            "flow_lph": self.flow_lph(), "running_since": since,
            "salt_kg": round(lv["salt_g"] / 1000, 1) if lv.get("salt_g") is not None else None,
            "salt_days": lv.get("salt_days"), "hardness": lv.get("hardness"),
            "valve": self.st.get("valve"), "valve_ts": self.st.get("valve_ts"), "vacation": self.st.get("vacation"),
            "updated": lv.get("updated"), "history": self.history(30),
        }

    # ---- Hintergrund
    def _loop(self):
        time.sleep(20)
        while True:
            if not self.configured:
                self.connected = False
                time.sleep(30)
                continue
            try:
                if not self.info:
                    self.test()
                self.poll()
            except Exception as e:
                self.connected = False
                self.error = str(e)[:200]
                print(f"[JUDO] {self.error}", flush=True)
            time.sleep(POLL)
