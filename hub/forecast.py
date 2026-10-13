"""Wetter fürs Haus: Vorhersage, Regen-Nowcast, Gewitter-Risiko und DWD-Warnungen – plus Mitteilungen.

Quellen (kostenlos, ohne Konto):
  • Open-Meteo (best_match = in Deutschland das DWD-Modell ICON-D2/ICON-EU): 15-Minuten-Niederschlag für die
    nächsten 3 h, stündlich 48 h, täglich 7 Tage
  • DWD-Warnungen über Bright Sky (api.brightsky.dev) für die Gemeinde des Hauses
Die eigene KNX-Wetterstation (Wind, Temperatur, Helligkeit, Regen) liefert weiter /api/weather.

Mitteilungen (Abos je Gerät): wx_alert (neue DWD-Warnung: Gewitter, Sturm, Starkregen, Hagel …),
wx_rain („Regen in ~15 min", nur wenn es gerade trocken ist, 7–22 Uhr, höchstens alle 3 h),
wx_gust (Böen über 60 km/h in den nächsten 12 h, höchstens alle 12 h),
wx_frost (Frost in der kommenden Nacht, Oktober–Mai, 18 Uhr),
garden (Gieß-Hinweis April–September um 18 Uhr: Regen minus Verdunstung der letzten 4 + nächsten 2 Tage).
storm_soon() nutzt der Mähroboter: Sturmböen oder Gewitter in der nächsten Stunde → heimfahren (nur mit Regenschutz).
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from hub_config import DATA_DIR

STATE = os.path.join(DATA_DIR, "forecast-state.json")
REFRESH = 10 * 60
OM_URL = "https://api.open-meteo.com/v1/forecast"
ALERTS_URL = "https://api.brightsky.dev/alerts"
# DWD-Niederschlagsradar (1 km, alle 5 min, inkl. 2 h Vorhersage) als WMS – die App holt die Bilder direkt
RADAR_WMS = "https://maps.dwd.de/geoserver/dwd/wms"
RADAR_LAYER = "dwd:Niederschlagsradar"
RADAR_CAPS = "https://maps.dwd.de/geoserver/dwd/Niederschlagsradar/wms?service=WMS&version=1.3.0&request=GetCapabilities"

HOURLY = ("temperature_2m,apparent_temperature,precipitation,precipitation_probability,weather_code,cloud_cover,"
          "cloud_cover_low,cloud_cover_mid,cloud_cover_high,wind_speed_10m,wind_gusts_10m,wind_direction_10m,"
          "cape,lightning_potential,uv_index,sunshine_duration,is_day,relative_humidity_2m")
DAILY = ("weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,"
         "precipitation_hours,wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,sunrise,sunset,"
         "uv_index_max,sunshine_duration")
CURRENT = ("temperature_2m,apparent_temperature,precipitation,weather_code,cloud_cover,wind_speed_10m,wind_gusts_10m,"
           "wind_direction_10m,is_day,relative_humidity_2m,pressure_msl")

ALERT_WORDS = ("gewitter", "sturm", "orkan", "starkregen", "dauerregen", "hagel", "unwetter", "schnee", "glätte", "glatteis")
RAIN_MM = 0.2           # mm je 15 min, ab dem es „regnet"
GUST_KMH = 60
FROST_C = 0.5           # Tiefstwert, ab dem es „Frost" gibt (Bodenfrost schon etwas darüber)
WATER_DEFICIT = 12      # mm Wasserdefizit (Verdunstung − Regen), ab dem Gießen sinnvoll ist


def _cols(block, rename):
    """Open-Meteo {"time": [...], "x": [...]} → [{"t": …, "x": …}, …]"""
    if not block:
        return []
    out = []
    for i, t in enumerate(block.get("time", [])):
        row = {"t": t}
        for src, dst in rename.items():
            v = block.get(src)
            row[dst] = v[i] if v is not None and i < len(v) else None
        out.append(row)
    return out


def thunder_level(h):
    """0 = nichts, 1 = möglich, 2 = wahrscheinlich, 3 = Gewitter vorhergesagt"""
    code = h.get("code") or 0
    cape, lpi, prob = h.get("cape") or 0, h.get("lightning") or 0, h.get("prob") or 0
    if code >= 95:
        return 3
    if lpi >= 1 or (cape >= 1000 and prob >= 40):
        return 2
    if cape >= 400 and prob >= 30:
        return 1
    return 0


class Forecast:
    def __init__(self, location_fn, push=None):
        self.location_fn, self.push = location_fn, push
        self.data, self.alerts_list, self.error = None, [], None
        self.updated = 0.0
        self._lock = threading.Lock()
        self.state = self._load()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- Zustand für Mitteilungen
    def _load(self):
        try:
            with open(STATE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, STATE)

    # ---- Abrufen
    def _loc(self):
        loc = self.location_fn() or {}
        lat, lon = loc.get("lat"), loc.get("lon")
        return (float(lat), float(lon)) if lat is not None and lon is not None else None

    @staticmethod
    def _get(url, params):
        """Open-Meteo antwortet gelegentlich kurz mit 503 – bis zu 3 Versuche."""
        for attempt in range(3):
            try:
                r = requests.get(url, params=params, timeout=45)
                r.raise_for_status()
                return r
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(3 * (attempt + 1))

    def _fetch(self):
        loc = self._loc()
        if not loc:
            self.error = "Standort des Hauses fehlt (Einrichtung → Standort)"
            return
        lat, lon = loc
        r = self._get(OM_URL, params={
            "latitude": lat, "longitude": lon, "models": "best_match", "timezone": "Europe/Berlin",
            "timeformat": "unixtime", "wind_speed_unit": "kmh", "current": CURRENT, "hourly": HOURLY, "daily": DAILY,
            "minutely_15": "precipitation,lightning_potential", "past_minutely_15": 4, "forecast_minutely_15": 12,
            "forecast_hours": 48, "forecast_days": 7})
        om = r.json()
        c = om.get("current") or {}
        hourly = _cols(om.get("hourly"), {
            "temperature_2m": "temp", "apparent_temperature": "feels", "precipitation": "precip",
            "precipitation_probability": "prob", "weather_code": "code", "cloud_cover": "cloud",
            "cloud_cover_low": "cloudLow", "cloud_cover_mid": "cloudMid", "cloud_cover_high": "cloudHigh",
            "wind_speed_10m": "wind", "wind_gusts_10m": "gust", "wind_direction_10m": "dir", "cape": "cape",
            "lightning_potential": "lightning", "uv_index": "uv", "sunshine_duration": "sun", "is_day": "isDay",
            "relative_humidity_2m": "humidity"})
        for h in hourly:
            h["thunder"] = thunder_level(h)
        daily = _cols(om.get("daily"), {
            "weather_code": "code", "temperature_2m_max": "max", "temperature_2m_min": "min",
            "precipitation_sum": "precip", "precipitation_probability_max": "prob", "precipitation_hours": "rainHours",
            "wind_speed_10m_max": "wind", "wind_gusts_10m_max": "gust", "wind_direction_10m_dominant": "dir",
            "sunrise": "sunrise", "sunset": "sunset", "uv_index_max": "uv", "sunshine_duration": "sun"})
        for d in daily:                             # Gewitter-Risiko je Tag = höchstes der Stunden
            day = [h["thunder"] for h in hourly if d["t"] <= h["t"] < d["t"] + 86400]
            d["thunder"] = max(day) if day else (3 if (d.get("code") or 0) >= 95 else 0)
        nowcast = _cols(om.get("minutely_15"), {"precipitation": "mm", "lightning_potential": "lightning"})
        now = time.time()
        data = {
            "updated": int(now), "source": "DWD ICON über Open-Meteo",
            "current": {"temp": c.get("temperature_2m"), "feels": c.get("apparent_temperature"),
                        "precip": c.get("precipitation"), "code": c.get("weather_code"), "cloud": c.get("cloud_cover"),
                        "wind": c.get("wind_speed_10m"), "gust": c.get("wind_gusts_10m"),
                        "dir": c.get("wind_direction_10m"), "isDay": c.get("is_day"),
                        "humidity": c.get("relative_humidity_2m"), "pressure": c.get("pressure_msl")},
            "nowcast": nowcast, "hourly": hourly, "daily": daily,
        }
        data["rainText"] = self._rain_text(nowcast, now)
        data["thunder"] = self._thunder_summary(hourly)
        with self._lock:
            self.data = data
            self.updated = now
            self.error = None

    def _fetch_radar(self):
        """Verfügbare Radarzeiten: letzte Stunde + Vorhersage bis Ende (alle 10 min)."""
        txt = requests.get(RADAR_CAPS, timeout=20).text
        m = re.search(r'<Dimension name="time"[^>]*>[^/<]+/([^/<]+)/PT5M', txt)
        ref = re.search(r'<Dimension name="REFERENCE_TIME" default="([^"]+)"', txt)
        if not m:
            return
        parse = lambda v: datetime.strptime(v[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        end = parse(m.group(1))
        now = datetime.now(timezone.utc)
        reference = parse(ref.group(1)) if ref else min(end, now)
        start = reference - timedelta(minutes=60)
        frames, t = [], start
        while t <= end:
            frames.append({"t": int(t.timestamp()), "time": t.strftime("%Y-%m-%dT%H:%M:00.000Z"),
                           "forecast": t > reference})
            t += timedelta(minutes=10)
        with self._lock:
            self.radar = {"wms": RADAR_WMS, "layer": RADAR_LAYER, "reference": int(reference.timestamp()), "frames": frames}

    def _fetch_alerts(self):
        loc = self._loc()
        if not loc:
            return
        r = requests.get(ALERTS_URL, params={"lat": loc[0], "lon": loc[1]}, timeout=20)
        r.raise_for_status()
        j = r.json()
        out = []
        for a in j.get("alerts") or []:
            out.append({"id": str(a.get("alert_id") or a.get("id")), "event": a.get("event_de") or a.get("event_en"),
                        "headline": a.get("headline_de") or a.get("headline_en"),
                        "description": a.get("description_de") or a.get("description_en"),
                        "instruction": a.get("instruction_de") or a.get("instruction_en"),
                        "severity": a.get("severity"), "onset": a.get("onset"), "expires": a.get("expires")})
        with self._lock:
            self.alerts_list = out
            self.place = (j.get("location") or {}).get("name")

    # ---- Texte
    @staticmethod
    def _rain_text(nowcast, now):
        """„Regen in 20 min" / „Regen hört gegen 14:30 auf" / „Kein Regen in den nächsten 3 h"."""
        fut = [n for n in nowcast if n["t"] + 900 > now and n.get("mm") is not None]
        if not fut:
            return None
        raining = (fut[0].get("mm") or 0) >= RAIN_MM
        if raining:
            stop = next((n for n in fut if (n.get("mm") or 0) < RAIN_MM), None)
            if stop is None:
                return "Regen hält die nächsten 3 h an"
            return "Regen hört gegen " + datetime.fromtimestamp(stop["t"]).strftime("%H:%M") + " auf"
        start = next((n for n in fut if (n.get("mm") or 0) >= RAIN_MM), None)
        if start is None:
            return "Kein Regen in den nächsten 3 h"
        mins = max(0, int(round((start["t"] - now) / 60 / 5)) * 5)
        return f"Regen in ~{mins} min" if mins > 0 else "Regen beginnt gleich"

    @staticmethod
    def _thunder_summary(hourly):
        nxt = [h for h in hourly[:24] if h["thunder"] > 0]
        if not nxt:
            return {"level": 0, "text": "Keine Gewitter in den nächsten 24 h"}
        top = max(nxt, key=lambda h: h["thunder"])
        word = {1: "Gewitter möglich", 2: "Gewitter wahrscheinlich", 3: "Gewitter vorhergesagt"}[top["thunder"]]
        return {"level": top["thunder"], "at": top["t"],
                "text": word + " ab " + datetime.fromtimestamp(nxt[0]["t"]).strftime("%H") + " Uhr"}

    # ---- für die App
    def forecast(self):
        if self.data is None or time.time() - self.updated > REFRESH * 2:
            try:
                self._fetch()
            except Exception as e:
                self.error = f"Vorhersage nicht erreichbar: {e}"
        if getattr(self, "radar", None) is None or time.time() - self.radar.get("reference", 0) > 30 * 60:
            try:
                self._fetch_radar()
            except Exception:
                pass
        with self._lock:
            d = dict(self.data or {})
            d["alerts"] = list(self.alerts_list)
            d["place"] = getattr(self, "place", None)
            d["radar"] = getattr(self, "radar", None)
            loc = self._loc()
            d["location"] = {"lat": loc[0], "lon": loc[1]} if loc else None     # Mittelpunkt für Radar & Karte
            d["error"] = self.error
            d["available"] = self.data is not None
        try:
            d["garden"] = self.garden()
        except Exception:
            d["garden"] = None
        return d

    # ---- Windkarte (wie Windy): Raster um das Haus, stündlich 48 h, in Knoten
    def wind_grid(self, step=0.25, lat_span=1.5, lon_span=2.25):
        """≈ 165 km × 165 km um das Haus, alle 0,25° (≈ 190 Punkte). Alle 3 h neu (Open-Meteo-Kontingent schonen)."""
        cache = getattr(self, "_wind", None)
        if cache and time.time() - cache["at"] < 3 * 3600:
            return cache["data"]
        loc = self._loc()
        if not loc:
            raise RuntimeError("Standort des Hauses fehlt")
        lat0, lon0 = loc
        n_lat, n_lon = int(round(2 * lat_span / step)) + 1, int(round(2 * lon_span / step)) + 1
        lats = [round(lat0 + lat_span - i * step, 3) for i in range(n_lat)]          # Nord → Süd
        lons = [round(lon0 - lon_span + j * step, 3) for j in range(n_lon)]          # West → Ost
        pts = [(la, lo) for la in lats for lo in lons]
        speed, direc, gust, temp, times = [], [], [], [], None
        for k in range(0, len(pts), 100):                                               # in Paketen abfragen
            chunk = pts[k:k + 100]
            r = self._get(OM_URL, params={
                "latitude": ",".join(str(p[0]) for p in chunk), "longitude": ",".join(str(p[1]) for p in chunk),
                "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,temperature_2m", "wind_speed_unit": "kn",
                "models": "icon_seamless", "forecast_hours": 48, "timeformat": "unixtime", "timezone": "UTC"})
            js = r.json()
            js = js if isinstance(js, list) else [js]
            for item in js:
                h = item.get("hourly") or {}
                times = times or h.get("time")
                speed.append(h.get("wind_speed_10m") or [])
                direc.append(h.get("wind_direction_10m") or [])
                gust.append(h.get("wind_gusts_10m") or [])
                temp.append(h.get("temperature_2m") or [])
        nt = len(times or [])
        # pro Stunde ein flaches Feld (Zeile für Zeile, Nord → Süd), auf eine Nachkommastelle gerundet
        pack = lambda arr: [[round(arr[i][t] or 0, 1) if t < len(arr[i]) else 0 for i in range(len(arr))] for t in range(nt)]
        data = {"lats": lats, "lons": lons, "times": times or [], "unit": "kn", "home": {"lat": lat0, "lon": lon0},
                "speed": pack(speed), "dir": pack(direc), "gust": pack(gust), "temp": pack(temp), "source": "DWD ICON über Open-Meteo",
                "updated": int(time.time())}
        self._wind = {"at": time.time(), "data": data}
        return data

    def precip_grid(self, step=0.2, lat_span=1.2, lon_span=1.8):
        """Niederschlag (mm/h) als Raster um das Haus, stündlich 48 h (ICON) – verlängert das Regenradar über +2 h hinaus.
        Gleicher Ausschnitt wie das Radar (±200 km). Alle 3 h neu."""
        cache = getattr(self, "_precip", None)
        if cache and time.time() - cache["at"] < 3 * 3600:
            return cache["data"]
        loc = self._loc()
        if not loc:
            raise RuntimeError("Standort des Hauses fehlt")
        lat0, lon0 = loc
        n_lat, n_lon = int(round(2 * lat_span / step)) + 1, int(round(2 * lon_span / step)) + 1
        lats = [round(lat0 + lat_span - i * step, 3) for i in range(n_lat)]
        lons = [round(lon0 - lon_span + j * step, 3) for j in range(n_lon)]
        pts = [(la, lo) for la in lats for lo in lons]
        rain, times = [], None
        for k in range(0, len(pts), 50):
            chunk = pts[k:k + 50]
            r = self._get(OM_URL, params={
                "latitude": ",".join(str(p[0]) for p in chunk), "longitude": ",".join(str(p[1]) for p in chunk),
                "hourly": "precipitation", "models": "icon_seamless", "forecast_hours": 48,
                "timeformat": "unixtime", "timezone": "UTC"})
            js = r.json()
            js = js if isinstance(js, list) else [js]
            for item in js:
                h = item.get("hourly") or {}
                times = times or h.get("time")
                rain.append(h.get("precipitation") or [])
        nt = len(times or [])
        data = {"lats": lats, "lons": lons, "times": times or [], "unit": "mm/h",
                "rain": [[round(rain[i][t] or 0, 2) if t < len(rain[i]) else 0 for i in range(len(rain))] for t in range(nt)],
                "source": "DWD ICON über Open-Meteo", "updated": int(time.time())}
        self._precip = {"at": time.time(), "data": data}
        return data

    def rain_soon(self, now=False, minutes=30):
        """Regnet es gerade (now=True) bzw. in den nächsten `minutes` Minuten? (15-Minuten-Nowcast)"""
        t = time.time()
        nc = (self.data or {}).get("nowcast") or []
        if now:
            cur = [n for n in nc if n["t"] <= t < n["t"] + 900]
            return bool(cur) and (cur[0].get("mm") or 0) >= RAIN_MM
        return any((n.get("mm") or 0) >= RAIN_MM for n in nc if t - 900 < n["t"] <= t + minutes * 60)

    def storm_soon(self, minutes=60):
        """Grund, falls in der nächsten Stunde Sturmböen oder Gewitter drohen (für die Mäher-Heimfahrt), sonst None"""
        t = time.time()
        hrs = [h for h in (self.data or {}).get("hourly") or [] if t - 3600 < h["t"] <= t + minutes * 60]
        g = max((h.get("gust") or 0 for h in hrs), default=0)
        if g > GUST_KMH:
            return f"Böen bis {g:.0f} km/h"
        if any((h.get("thunder") or 0) >= 2 for h in hrs) or any((n.get("lightning") or 0) > 0 for n in
                                                                 (self.data or {}).get("nowcast") or [] if t <= n["t"] <= t + 3600):
            return "Gewitter"
        return None

    def garden(self):
        """Wasserbilanz für den Gieß-Hinweis (einmal am Tag frisch von Open-Meteo, mit den letzten 4 Tagen)"""
        g = getattr(self, "_garden", None)
        if g and time.time() - g["at"] < 6 * 3600:
            return g["data"]
        loc = self._loc()
        if not loc:
            return None
        r = self._get(OM_URL, params={"latitude": loc[0], "longitude": loc[1], "timezone": "Europe/Berlin",
                                      "daily": "precipitation_sum,et0_fao_evapotranspiration,temperature_2m_max",
                                      "past_days": 4, "forecast_days": 3, "timeformat": "unixtime"}).json()
        d = r.get("daily") or {}
        rain, et0, tmax = d.get("precipitation_sum") or [], d.get("et0_fao_evapotranspiration") or [], d.get("temperature_2m_max") or []
        if len(rain) < 7 or len(et0) < 7:
            return None
        past_rain, past_et = sum(x or 0 for x in rain[:4]), sum(x or 0 for x in et0[:4])
        next_rain, next_et = sum(x or 0 for x in rain[4:6]), sum(x or 0 for x in et0[4:6])
        deficit = past_et + next_et - past_rain - next_rain
        dry_days = 0
        for x in reversed(rain[:4]):                 # Tage ohne nennenswerten Regen bis heute
            if (x or 0) >= 1:
                break
            dry_days += 1
        month = datetime.now().month
        data = {"pastRain": round(past_rain, 1), "pastEvap": round(past_et, 1), "nextRain": round(next_rain, 1),
                "nextEvap": round(next_et, 1), "deficit": round(deficit, 1), "dryDays": dry_days,
                "maxTemp": max((x for x in tmax[4:6] if x is not None), default=None),
                "season": 4 <= month <= 9,
                "water": 4 <= month <= 9 and deficit >= WATER_DEFICIT and (rain[4] or 0) < 3}
        if data["water"]:
            data["text"] = (f"Seit {dry_days} Tagen kaum Regen ({past_rain:.0f} mm), verdunstet sind ~{past_et:.0f} mm"
                            + (" – auch morgen kein Regen in Sicht." if next_rain < 2 else f" – morgen nur {next_rain:.0f} mm Regen."))
        elif data["season"]:
            data["text"] = "Kein Gießen nötig" + (f" – {next_rain:.0f} mm Regen in Sicht." if next_rain >= 2 else ".")
        self._garden = {"at": time.time(), "data": data}
        return data

    def alerts(self):
        with self._lock:
            return list(self.alerts_list)

    # ---- Hintergrund: abrufen + Mitteilungen
    def _loop(self):
        time.sleep(20)
        while True:
            try:
                self._fetch()
            except Exception as e:
                self.error = f"Vorhersage nicht erreichbar: {e}"
            try:
                self._fetch_radar()
            except Exception as e:
                print(f"[WETTER] Radar: {e}", flush=True)
            try:
                self._fetch_alerts()
            except Exception as e:
                print(f"[WETTER] Warnungen: {e}", flush=True)
            try:
                self._notify()
            except Exception as e:
                print(f"[WETTER] Mitteilung: {e}", flush=True)
            time.sleep(REFRESH)

    def _send(self, topic, title, body):
        if self.push is not None and self.push.configured:
            n = self.push.send(topic, title, body)
            print(f"[WETTER] {topic}: {title} – {body} → {n} Gerät(e)", flush=True)

    def _notify(self):
        now = time.time()
        changed = False
        # 1) neue DWD-Warnungen
        seen = self.state.setdefault("alerts", {})
        for a in self.alerts():
            ev = (a.get("event") or "").lower()
            if a["id"] in seen or not any(w in ev for w in ALERT_WORDS):
                continue
            seen[a["id"]] = int(now)
            changed = True
            self._send("wx_alert", "⚠️ " + (a.get("event") or "Wetterwarnung").title(),
                       a.get("headline") or "Neue Warnung des Deutschen Wetterdienstes")
        for k in [k for k, t in seen.items() if now - t > 7 * 86400]:
            del seen[k]
        d = self.data or {}
        hour = datetime.now().hour
        # 2) Regen in ~15 min, wenn es gerade trocken ist
        nc = d.get("nowcast") or []
        past = [n for n in nc if n["t"] + 900 <= now and n["t"] >= now - 1800]
        fut = [n for n in nc if now - 900 < n["t"] <= now + 45 * 60]
        dry = all((n.get("mm") or 0) < 0.05 for n in past) and fut and (fut[0].get("mm") or 0) < RAIN_MM
        soon = next((n for n in fut[1:] if (n.get("mm") or 0) >= RAIN_MM), None)
        if dry and soon and 7 <= hour < 22 and now - self.state.get("rain_at", 0) > 3 * 3600:
            mins = max(5, int(round((soon["t"] - now) / 60 / 5)) * 5)
            mm_h = max((n.get("mm") or 0) for n in nc if n["t"] >= soon["t"]) * 4
            self.state["rain_at"] = int(now)
            changed = True
            self._send("wx_rain", f"🌧 Regen in ~{mins} min",
                       ("Starker Regen" if mm_h >= 10 else "Regen") + f" zieht auf (bis {mm_h:.0f} mm/h). Fenster zu?")
        # 3) Sturmböen in den nächsten 12 h
        gusts = [h for h in (d.get("hourly") or []) if now - 3600 < h["t"] <= now + 12 * 3600 and (h.get("gust") or 0) > GUST_KMH]
        if gusts and now - self.state.get("gust_at", 0) > 12 * 3600:
            top = max(gusts, key=lambda h: h["gust"])
            self.state["gust_at"] = int(now)
            changed = True
            self._send("wx_gust", f"💨 Böen bis {top['gust']:.0f} km/h",
                       "Um " + datetime.fromtimestamp(top["t"]).strftime("%H") + " Uhr – Markise und Gartenmöbel sichern.")
        # 4) Frost in der kommenden Nacht (Oktober–Mai), abends um 18 Uhr
        today = datetime.now().strftime("%Y-%m-%d")
        if hour >= 18 and datetime.now().month in (10, 11, 12, 1, 2, 3, 4, 5) and self.state.get("frost_day") != today:
            night = [h for h in (d.get("hourly") or []) if now < h["t"] <= now + 16 * 3600 and h.get("temp") is not None]
            if night:
                low = min(night, key=lambda h: h["temp"])
                self.state["frost_day"] = today
                changed = True
                if low["temp"] <= FROST_C:
                    first = self.state.get("frost_season") != str(datetime.now().year if datetime.now().month >= 7 else datetime.now().year - 1)
                    self.state["frost_season"] = str(datetime.now().year if datetime.now().month >= 7 else datetime.now().year - 1)
                    self._send("wx_frost", ("❄️ Erster Frost: " if first else "❄️ Frost heute Nacht: ") + f"{low['temp']:.0f} °C".replace("-", "−"),
                               "Gegen " + datetime.fromtimestamp(low["t"]).strftime("%H") + " Uhr. Kübelpflanzen schützen, "
                               "Gartenschlauch und Außenwasserhahn leeren.")
        # 5) Gieß-Hinweis (April–September), abends um 18 Uhr, höchstens alle 2 Tage
        if hour >= 18 and self.state.get("water_day") != today and now - self.state.get("water_at", 0) > 40 * 3600:
            try:
                g = self.garden()
            except Exception:
                g = None
            if g and g.get("season"):
                self.state["water_day"] = today
                changed = True
                if g.get("water"):
                    self.state["water_at"] = int(now)
                    self._send("garden", "🌱 Garten gießen?", g["text"])
        if changed:
            self._save()
