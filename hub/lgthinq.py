"""LG ThinQ (Waschmaschine, Trockner, …) über LGs offizielle Schnittstelle „ThinQ Connect“.

Anmeldung: Der Besitzer erstellt selbst auf https://connect-pat.lgthinq.com einen „Personal Access Token“ (PAT)
und trägt ihn auf der Einrichtungsseite ein. Kein LG-Passwort auf dem Hub. Token liegt nur in config.json (chmod 600).

Lesen: Geräteliste, Status, Restzeit → gleiche Form wie Home Connect/Miele (/api/appliances), Mitteilung „fertig“.
Starten: nur über den Auto-Start (smartstart.py) nach Bestätigung in der App, nur mit Fernstart am Gerät.
"""
import base64
import re
import threading
import time
import uuid

import requests

API_KEY = "v6GFvkweNo7DK7yD3ylIZ9w52aKBU0eJ7wLXkSR3"      # öffentlicher Schlüssel des offiziellen ThinQ-Connect-SDK
REGION = {"DE": "eic", "AT": "eic", "CH": "eic", "BR": "aic", "US": "aic"}
TYPE = {"DEVICE_WASHER": ("Washer", "Waschmaschine"), "DEVICE_DRYER": ("Dryer", "Trockner"),
        "DEVICE_WASHTOWER_WASHER": ("Washer", "Waschmaschine"), "DEVICE_WASHTOWER_DRYER": ("Dryer", "Trockner"),
        "DEVICE_WASHCOMBO_MAIN": ("WasherDryer", "Waschtrockner"), "DEVICE_STYLER": ("Styler", "Styler"),
        "DEVICE_DISH_WASHER": ("Dishwasher", "Geschirrspüler"), "DEVICE_REFRIGERATOR": ("Refrigerator", "Kühlschrank")}
RUN = {"RUNNING", "RINSING", "SPINNING", "DRYING", "COOLING", "STEAM_SOFTENING", "DETECTING", "PREWASH", "WASHING",
       "REFRESHING", "SOAKING", "RINSE_HOLD", "WRINKLE_CARE", "COOL_DOWN"}
STATE_DE = {"POWER_OFF": "Aus", "INITIAL": "Bereit", "PAUSE": "Pausiert", "END": "Fertig", "ERROR": "Störung",
            "RESERVED": "Startzeit gesetzt", "RUNNING": "Läuft", "RINSING": "Spülen", "SPINNING": "Schleudern",
            "DRYING": "Trocknen", "COOLING": "Abkühlen", "DETECTING": "Beladung erkennen", "PREWASH": "Vorwäsche",
            "WASHING": "Waschen", "SOAKING": "Einweichen", "WRINKLE_CARE": "Knitterschutz", "COOL_DOWN": "Abkühlen",
            "SLEEP": "Bereit"}


class LGThinQ:
    def __init__(self, cfg_fn, save_fn, push=None):
        self.cfg_fn, self.save_fn, self.push = cfg_fn, save_fn, push
        self.devices, self.states, self.error, self.updated = {}, {}, None, 0
        self._prev = {}
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def configured(self):
        return bool((self.cfg_fn() or {}).get("pat"))

    def _headers(self):
        c = self.cfg_fn() or {}
        cid = c.get("client_id")
        if not cid:
            cid = "liesenberg-home-" + uuid.uuid4().hex[:12]
            self.save_fn({"client_id": cid})
        return {"Authorization": f"Bearer {c['pat']}", "x-country": c.get("country") or "DE",
                "x-message-id": base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("="),
                "x-client-id": cid, "x-api-key": API_KEY, "x-service-phase": "OP", "Content-Type": "application/json"}

    def _url(self, path):
        c = self.cfg_fn() or {}
        return f"https://api-{REGION.get(c.get('country') or 'DE', 'eic')}.lgthinq.com{path}"

    def _get(self, path):
        r = requests.get(self._url(path), headers=self._headers(), timeout=15)
        if r.status_code in (401, 403):
            raise PermissionError("LG ThinQ: Token ungültig oder abgelaufen – neuen Token erstellen")
        r.raise_for_status()
        return r.json().get("response")

    def test(self):
        """Für die Einrichtungsseite: Token prüfen → Liste der Geräte"""
        return self._refresh(full=True)

    def _refresh(self, full=False):
        if full or not self.devices or time.time() - getattr(self, "_devs_at", 0) > 600:   # neue Geräte nach ≤ 10 min
            self._devs_at = time.time()
            devs = self._get("/devices") or []
            self.devices = {d["deviceId"]: d.get("deviceInfo") or {} for d in devs if isinstance(d, dict) and d.get("deviceId")}
        for did in list(self.devices):
            try:
                st = self._get(f"/devices/{did}/state")
                self.states[did] = st[0] if isinstance(st, list) and st else (st or {})
            except PermissionError:
                raise
            except Exception:
                self.states[did] = {"_offline": True}
        self.updated = time.time()
        self.error = None
        return [self._one(d) for d in self.devices]

    def _loop(self):
        time.sleep(30)
        while True:
            if self.configured:
                try:
                    self._refresh()
                    self._notify()
                except Exception as e:
                    self.error = str(e)[:160]
                    print(f"[LG] {self.error}", flush=True)
            busy = any(self._one(d)["state"] == "Run" for d in self.devices)
            time.sleep(60 if busy else 180)

    def _one(self, did):
        info, st = self.devices.get(did) or {}, self.states.get(did) or {}
        typ, type_de = TYPE.get(info.get("deviceType"), (None, "LG-Gerät"))
        cur = (st.get("runState") or {}).get("currentState") or ("POWER_OFF" if st.get("_offline") else None)
        t = st.get("timer") or {}
        rem = (t.get("remainHour") or 0) * 3600 + (t.get("remainMinute") or 0) * 60
        tot = (t.get("totalHour") or 0) * 3600 + (t.get("totalMinute") or 0) * 60
        running = cur in RUN
        return {
            "id": "lg:" + did, "brand": "LG", "name": (type_de if re.fullmatch(r"(Washer|Dryer|Styler|Dishwasher|Refrigerator)\s*\d*", info.get("alias") or "Washer")
                 else info.get("alias")), "typeText": type_de, "type": typ,
            "connected": not st.get("_offline"),
            "state": "Run" if running else {"PAUSE": "Pause", "END": "Finished", "ERROR": "Error",
                                             "RESERVED": "DelayedStart"}.get(cur, "Ready" if cur in ("INITIAL", "SLEEP") else "Inactive"),
            "stateText": STATE_DE.get(cur, cur or "–"),
            "phase": STATE_DE.get(cur) if running else None,
            "program": None,
            "remaining": rem if running and rem else None,
            "progress": round((tot - rem) / tot * 100) if running and tot and rem <= tot else None,
            "remoteStart": bool((st.get("remoteControlEnable") or {}).get("remoteControlEnabled")),
        }

    def detail(self, did):
        did = did.removeprefix("lg:")
        if did not in self.devices:
            raise KeyError(did)
        try:
            st = self._get(f"/devices/{did}/state")
            self.states[did] = st[0] if isinstance(st, list) and st else (st or {})
        except Exception:
            pass
        info, st = self.devices[did], self.states.get(did) or {}
        a = self._one(did)
        t = st.get("timer") or {}
        a.update(model=info.get("modelName"), cycles=(st.get("cycle") or {}).get("cycleCount"),
                 totalMinutes=(t.get("totalHour") or 0) * 60 + (t.get("totalMinute") or 0) or None,
                 endsIn=((t.get("relativeHourToStop") or 0) * 60 + (t.get("relativeMinuteToStop") or 0)) or None,
                 rawState=(st.get("runState") or {}).get("currentState"))
        return a

    def start(self, did):
        """Startet das am Gerät gewählte Programm (nur mit Fernstart am Gerät). Aufruf NUR aus dem Auto-Start
        (smartstart.py), der vorher in der App bestätigt wurde."""
        did = did.removeprefix("lg:")
        info, st = self.devices.get(did) or {}, self.states.get(did) or {}
        if not (st.get("remoteControlEnable") or {}).get("remoteControlEnabled"):
            raise RuntimeError("Fernstart ist am Gerät nicht aktiv")
        typ = info.get("deviceType") or ""
        if "DRYER" in typ:
            body = {"operation": {"dryerOperationMode": "START"}}
        else:
            body = {"location": {"locationName": (st.get("location") or {}).get("locationName") or "MAIN"},
                    "operation": {"washerOperationMode": "START"}}
        h = self._headers()
        h["x-conditional-control"] = "true"
        r = requests.post(self._url(f"/devices/{did}/control"), json=body, headers=h, timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(f"LG lehnt den Start ab: {r.text[:160]}")
        print(f"[LG] Start: {self._one(did)['name']}", flush=True)
        return True

    def list(self):
        if not self.configured:
            return []
        out, seen = [], {}
        for d in self.devices:                          # zwei gleiche Namen → „Waschmaschine 2“
            a = self._one(d)
            seen[a["name"]] = seen.get(a["name"], 0) + 1
            if seen[a["name"]] > 1:
                a["name"] = f"{a['name']} {seen[a['name']]}"
            out.append(a)
        return out

    def _notify(self):
        for did in self.devices:
            a = self._one(did)
            prev = self._prev.get(did)
            self._prev[did] = a["state"]
            if prev == "Run" and a["state"] in ("Finished", "Ready", "Inactive") and self.push is not None:
                self.push.send("washer", f"{a['name']} ist fertig", "Zeit zum Ausräumen." if a["type"] != "Dryer" else "Die Wäsche ist trocken.")
