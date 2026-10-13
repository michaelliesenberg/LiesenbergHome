#!/usr/bin/env python3
"""Miele@home – Hausgeräte über die offizielle Miele 3rd Party API (developer.miele.com).

Einrichtung (einmalig, Besitzer): auf developer.miele.com eine App anlegen, als Redirect-URI die Adresse eintragen,
die die Einrichtungsseite anzeigt (…/miele/callback). Client-ID + Secret auf der Einrichtungsseite (Hausgeräte →
Miele) eintragen, „Mit Miele verbinden" – die Anmeldung beim Miele-Konto macht der Besitzer selbst im Browser.
Tokens: miele-tokens.json, Zugangsdaten der App in config.json – beides nur auf dem Hub, chmod 600.

Gelesen werden Status, Programm, Phase, Restzeit, Tür und – wo das Gerät es meldet – Strom/Wasser (ecoFeedback),
bei Kühl-/Gefriergeräten die Temperaturen. Live über den Event-Stream (SSE), zusätzlich alle 5 Minuten abgefragt.
Starten nur, wenn am Gerät „MobileStart" aktiv ist; Stopp/Pause, wenn das Gerät es anbietet.
Mitteilung (Abo „washer"): Programm fertig.
"""
import json
import os
import secrets
import threading
import time
import urllib.parse

import requests

import hub_config
from hub_config import DATA_DIR

API = "https://api.mcs3.miele.com/v1"
AUTH_URL = "https://auth.domestic.miele-iot.com/partner/realms/mcs/protocol/openid-connect/auth"
TOKEN_URL = "https://auth.domestic.miele-iot.com/partner/realms/mcs/protocol/openid-connect/token"
SCOPE = "openid mcs_thirdparty_read mcs_thirdparty_write"
TOKEN_FILE = os.path.join(DATA_DIR, "miele-tokens.json")
PENDING = {}            # state → Zeitpunkt (Anmeldung über die Einrichtungsseite gestartet)
LOCAL_URL_FN = None     # vom Hub gesetzt: Adresse im Heimnetz (z. B. http://192.168.178.28:8080)

# Status-Codes laut Miele-Doku
TYPE_EN = {1: "Washer", 2: "Dryer", 7: "Dishwasher", 24: "WasherDryer"}   # für Smart-Start (wie Home Connect)


def _label(d):
    """„Geschirrspüler · Miele" – mit Marke, damit man es vom Bosch-Geschirrspüler unterscheidet"""
    base = _val(d, "ident", "deviceName") or TYPE_DE.get(_val(d, "ident", "type", "value_raw")) or "Gerät"
    return base if "miele" in base.lower() else f"{base} · Miele"


RUNNING = {5}                          # läuft
ACTIVE = {3, 4, 5, 6, 7, 9, 11}        # programmiert, Startzeit, läuft, Pause, Ende programmiert, unterbrochen, Spülstopp
FINISHED = {7}
STATE_DE = {1: "Aus", 2: "An", 3: "Programmiert", 4: "Startzeit gesetzt", 5: "Läuft", 6: "Pause", 7: "Fertig",
            8: "Störung", 9: "Unterbrochen", 10: "Bereit", 11: "Spülstopp", 12: "Service", 13: "Superfrost",
            14: "Supercool", 15: "Superheat", 144: "Standard", 145: "Gesperrt", 146: "Supercool + Superfrost",
            255: "Nicht verbunden"}
TYPE_DE = {1: "Waschmaschine", 2: "Trockner", 7: "Geschirrspüler", 12: "Backofen", 17: "Kaffeevollautomat",
           18: "Dunstabzug", 19: "Kühlschrank", 20: "Gefrierschrank", 21: "Kühl-/Gefrierkombination",
           24: "Waschtrockner", 32: "Weinkühlschrank", 68: "Weinkühlschrank"}


def _cfg():
    return hub_config.get("miele") or {}


def redirect_uri():
    """Muss exakt so bei developer.miele.com eingetragen sein. Standard: Heimnetz-Adresse – der Browser landet nach
    der Miele-Anmeldung direkt beim Hub, nichts muss aus dem Internet erreichbar sein."""
    c = _cfg()
    if c.get("redirect_uri"):
        return c["redirect_uri"]
    base = (LOCAL_URL_FN() if LOCAL_URL_FN else "") or hub_config.public_url() or "http://<hub-adresse>:8080"
    return base.rstrip("/") + "/miele/callback"


class Auth:
    def __init__(self):
        self.lock = threading.Lock()
        self.tokens = self._load()

    def _load(self):
        try:
            with open(TOKEN_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save(self, tok):
        tok["obtained_at"] = time.time()
        tmp = TOKEN_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tok, f)
        os.replace(tmp, TOKEN_FILE)
        self.tokens = tok

    @property
    def ready(self):
        return bool(self.tokens and self.tokens.get("refresh_token"))

    def login_url(self):
        c = _cfg()
        if not c.get("client_id"):
            raise RuntimeError("Bitte zuerst Client-ID und Secret eintragen")
        state = secrets.token_urlsafe(16)
        PENDING[state] = time.time()
        q = {"response_type": "code", "client_id": c["client_id"], "redirect_uri": redirect_uri(),
             "scope": SCOPE, "state": state}
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    def exchange_code(self, code):
        c = _cfg()
        r = requests.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
                                           "client_id": c.get("client_id", ""), "client_secret": c.get("client_secret", ""),
                                           "redirect_uri": redirect_uri()}, timeout=20)
        r.raise_for_status()
        self._save(r.json())

    def access_token(self):
        with self.lock:
            if not self.ready:
                raise RuntimeError("Miele noch nicht verbunden")
            if time.time() - self.tokens.get("obtained_at", 0) > self.tokens.get("expires_in", 3600) - 120:
                c = _cfg()
                r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": self.tokens["refresh_token"],
                                                   "client_id": c.get("client_id", ""), "client_secret": c.get("client_secret", "")},
                                  timeout=20)
                r.raise_for_status()
                new = r.json()
                new.setdefault("refresh_token", self.tokens["refresh_token"])
                self._save(new)
            return self.tokens["access_token"]

    def logout(self):
        self.tokens = None
        try:
            os.remove(TOKEN_FILE)
        except OSError:
            pass


def _val(d, *keys):
    """Verschachtelte Werte sicher lesen"""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


class Miele:
    def __init__(self, push=None):
        self.push = push
        self.auth = Auth()
        self.devices = {}          # Seriennummer → Rohdaten
        self._reconnect = False
        self.error = None
        self.updated = 0
        self._prev = {}
        self._wake = threading.Event()
        threading.Thread(target=self._poller, daemon=True).start()
        threading.Thread(target=self._events, daemon=True).start()

    @property
    def configured(self):
        return bool(_cfg().get("client_id"))

    def _headers(self):
        return {"Authorization": "Bearer " + self.auth.access_token(), "Accept": "application/json"}

    # ---------------------------------------------------------- Abruf
    def refresh(self):
        r = requests.get(f"{API}/devices", params={"language": "de"}, headers=self._headers(), timeout=20)
        r.raise_for_status()
        data = r.json() or {}
        new = set(data) - set(self.devices) if isinstance(data, dict) else set()
        if new and self.devices:
            # neues Gerät im Miele-Konto → Event-Stream neu verbinden, damit es Live-Updates bekommt
            print(f"[MIELE] neue Geräte: {', '.join(sorted(new))}", flush=True)
            self._reconnect = True
        self._apply(data)

    def _apply(self, data):
        if not isinstance(data, dict):
            return
        for serial, dev in data.items():
            self.devices[serial] = dev
        self.updated = int(time.time())
        self.error = None
        for serial in list(data):
            self._notify(serial)

    def _poller(self):
        time.sleep(10)
        while True:
            if self.auth.ready:
                try:
                    self.refresh()
                except Exception as e:
                    self.error = f"Miele: {e}"
                    print(f"[MIELE] Abruf: {e}", flush=True)
            self._wake.wait(120)
            self._wake.clear()

    def _events(self):
        """Live-Updates (Server-Sent Events). Bei Abbruch nach kurzer Pause neu verbinden."""
        time.sleep(20)
        while True:
            if not self.auth.ready:
                time.sleep(60)
                continue
            try:
                h = self._headers(); h["Accept"] = "text/event-stream"
                with requests.get(f"{API}/devices/all/events", params={"language": "de"}, headers=h, stream=True, timeout=(15, 120)) as r:
                    r.raise_for_status()
                    event = None
                    self._reconnect = False
                    for raw in r.iter_lines(decode_unicode=True):
                        if self._reconnect:
                            break
                        if raw is None:
                            continue
                        line = raw.strip()
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:") and event == "devices":
                            try:
                                self._apply(json.loads(line[5:].strip()))
                            except ValueError:
                                pass
            except Exception as e:
                print(f"[MIELE] Event-Stream: {type(e).__name__}", flush=True)
            time.sleep(30)

    # ---------------------------------------------------------- für die App (gleiches Format wie Home Connect)
    def _one(self, serial, d):
        ident, st = d.get("ident") or {}, d.get("state") or {}
        type_raw = _val(ident, "type", "value_raw")
        status = _val(st, "status", "value_raw")
        rem = st.get("remainingTime") or [0, 0]
        el = st.get("elapsedTime") or [0, 0]
        remaining = (rem[0] * 3600 + rem[1] * 60) if isinstance(rem, list) and len(rem) == 2 else None
        elapsed = (el[0] * 3600 + el[1] * 60) if isinstance(el, list) and len(el) == 2 else 0
        progress = None
        if remaining and status in RUNNING and elapsed:
            progress = round(elapsed / (elapsed + remaining) * 100)
        temps = [t.get("value_raw") for t in (st.get("temperature") or []) if isinstance(t, dict)]
        temps = [t / 100 for t in temps if isinstance(t, (int, float)) and t > -32000]
        eco = st.get("ecoFeedback") or {}
        door = st.get("signalDoor")
        a = {
            "id": "miele:" + serial, "brand": "Miele",
            "name": ident.get("deviceName") or TYPE_DE.get(type_raw) or _val(ident, "type", "value_localized") or "Miele-Gerät",
            "typeText": TYPE_DE.get(type_raw) or _val(ident, "type", "value_localized"),
            "type": TYPE_EN.get(type_raw),
            "elapsed": elapsed,
            "connected": status != 255,
            "state": {5: "Run", 6: "Pause", 7: "Finished", 8: "Error", 4: "DelayedStart"}.get(status, "Ready" if status in (2, 3, 10) else "Inactive"),
            "stateText": STATE_DE.get(status) or _val(st, "status", "value_localized") or "–",   # Miele liefert teils Englisch
            "program": _val(st, "ProgramID", "value_localized") or None,
            "phase": _val(st, "programPhase", "value_localized") or None,
            "remaining": remaining if (status in ACTIVE and remaining) else None,
            "progress": progress,
            "door": ("offen" if door else "zu") if door is not None else None,
            "mobileStart": bool(_val(st, "remoteEnable", "mobileStart")),
            "energyKWh": _val(eco, "currentEnergyConsumption", "value"),
            "waterL": _val(eco, "currentWaterConsumption", "value"),
        }
        if type_raw in (19, 21, 32, 68) and temps:
            a["fridgeTemp"] = temps[0]
        if type_raw in (20, 21) and temps:
            a["freezerTemp"] = temps[-1]
        return a

    def list(self):
        return [self._one(s, d) for s, d in sorted(self.devices.items(), key=lambda kv: kv[0])]

    # ---------------------------------------------------------- Details für die App
    FRIDGE = {19, 20, 21, 32, 68}

    def _get(self, path, **params):
        r = requests.get(f"{API}{path}", params=params or None, headers=self._headers(), timeout=20)
        r.raise_for_status()
        return r.json()

    def detail(self, serial):
        serial = serial.removeprefix("miele:")
        d = self._get(f"/devices/{serial}", language="de")
        self.devices[serial] = d
        base = self._one(serial, d)
        st, ident = d.get("state") or {}, d.get("ident") or {}
        type_raw = _val(ident, "type", "value_raw")
        try:
            allowed = self._get(f"/devices/{serial}/actions")
        except Exception:
            allowed = {}
        try:
            progs = self._get(f"/devices/{serial}/programs", language="de")
            programs = [{"id": p.get("programId"), "name": p.get("program")} for p in progs if isinstance(p, dict)]
        except Exception:
            programs = []                     # gibt es nur, wenn das Gerät an und bereit ist
        try:
            fill = self._get(f"/devices/{serial}/fillingLevels")
        except Exception:
            fill = {}
        eco = st.get("ecoFeedback") or {}

        def hm(v):
            return v[0] * 60 + v[1] if isinstance(v, list) and len(v) == 2 else None
        zones = []
        cur = [t.get("value_raw") for t in (st.get("temperature") or []) if isinstance(t, dict)]
        tgt = [t.get("value_raw") for t in (st.get("targetTemperature") or []) if isinstance(t, dict)]
        ranges = {z.get("zone"): z for z in (allowed.get("targetTemperature") or []) if isinstance(z, dict)}
        for i in range(max(len(cur), len(tgt))):
            c = cur[i] if i < len(cur) else None
            t = tgt[i] if i < len(tgt) else None
            if (c is None or c <= -32000) and (t is None or t <= -32000):
                continue
            r = ranges.get(i + 1) or {}
            zones.append({"zone": i + 1, "current": c / 100 if c is not None and c > -32000 else None,
                          "target": t / 100 if t is not None and t > -32000 else None,
                          "min": r.get("min"), "max": r.get("max")})
        pa = set(allowed.get("processAction") or [])
        names = {"twinDosContainer1FillingLevel": "TwinDos 1", "twinDosContainer2FillingLevel": "TwinDos 2",
                 "powerDiscFillingLevel": "AutoDos PowerDisc", "saltFillingLevel": "Salz", "rinseAidFillingLevel": "Klarspüler"}
        levels = [{"name": n, "percent": round(fill[k] * (100 if isinstance(fill[k], float) and fill[k] <= 1 else 1))}
                  for k, n in names.items() if isinstance(fill.get(k), (int, float))]
        st_range = allowed.get("startTime") or []
        base.update({
            "model": _val(ident, "deviceIdentLabel", "techType"),
            "elapsedMin": hm(st.get("elapsedTime")),
            "startInMin": hm(st.get("startTime")) or None,
            "energyNow": _val(eco, "currentEnergyConsumption", "value"),
            "energyForecast": eco.get("energyForecast"),
            "waterNow": _val(eco, "currentWaterConsumption", "value"),
            "waterForecast": eco.get("waterForecast"),
            "info": bool(st.get("signalInfo")), "failure": bool(st.get("signalFailure")),
            "doorOpen": bool(st.get("signalDoor")),
            "light": {1: True, 2: False}.get(st.get("light")),
            "zones": zones, "levels": levels, "programs": programs,
            "isFridge": type_raw in self.FRIDGE,
            "can": {"start": 1 in pa, "stop": 2 in pa, "pause": 3 in pa,
                    "superfreezeOn": 4 in pa, "superfreezeOff": 5 in pa, "supercoolOn": 6 in pa, "supercoolOff": 7 in pa,
                    "startTime": bool(st_range), "light": bool(allowed.get("light")),
                    "powerOn": bool(allowed.get("powerOn")), "powerOff": bool(allowed.get("powerOff")),
                    "targetTemp": bool(ranges), "program": bool(allowed.get("programId")) or bool(programs)},
        })
        return base

    def action(self, serial, action, value=None):
        """Nur, was das Gerät gerade erlaubt (actions-Endpunkt). Die App fragt vorher nach."""
        serial = serial.removeprefix("miele:")
        allowed = self._get(f"/devices/{serial}/actions")
        pa = set(allowed.get("processAction") or [])
        codes = {"start": 1, "stop": 2, "pause": 3, "superfreeze_on": 4, "superfreeze_off": 5, "supercool_on": 6, "supercool_off": 7}
        if action in codes:
            if codes[action] not in pa:
                raise ValueError("Das Gerät erlaubt das gerade nicht" + (" – MobileStart am Gerät aktivieren" if action == "start" else ""))
            body = {"processAction": codes[action]}
        elif action == "start_time":
            if not allowed.get("startTime"):
                raise ValueError("Startzeit kann gerade nicht gesetzt werden")
            mins = max(0, int(value or 0))
            body = {"startTime": [mins // 60, mins % 60]}
        elif action == "light":
            if not allowed.get("light"):
                raise ValueError("Licht lässt sich nicht schalten")
            body = {"light": 1 if value else 2}
        elif action in ("power_on", "power_off"):
            key = "powerOn" if action == "power_on" else "powerOff"
            if not allowed.get(key):
                raise ValueError("Das Gerät erlaubt das gerade nicht")
            body = {key: True}
        elif action == "target_temp":
            zone, temp = int((value or {}).get("zone", 1)), float((value or {}).get("value"))
            r = next((z for z in allowed.get("targetTemperature") or [] if z.get("zone") == zone), None)
            if not r or not (r.get("min", -99) <= temp <= r.get("max", 99)):
                raise ValueError("Temperatur außerhalb des erlaubten Bereichs")
            body = {"targetTemperature": [{"zone": zone, "value": int(round(temp))}]}
        elif action == "program":
            h = self._headers(); h["Content-Type"] = "application/json"
            r = requests.put(f"{API}/devices/{serial}/programs", headers=h, json={"programId": int(value)}, timeout=15)
            r.raise_for_status()
            self._wake.set()
            return
        else:
            raise ValueError("Unbekannte Aktion")
        h = self._headers(); h["Content-Type"] = "application/json"
        r = requests.put(f"{API}/devices/{serial}/actions", headers=h, json=body, timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(f"Miele lehnt ab: {r.text[:120]}")
        self._wake.set()

    # ---------------------------------------------------------- Steuern
    def command(self, serial, action):
        """start nur mit MobileStart am Gerät; stop/pause wenn angeboten"""
        serial = serial.removeprefix("miele:")
        h = self._headers(); h["Content-Type"] = "application/json"
        r = requests.get(f"{API}/devices/{serial}/actions", headers=h, timeout=15)
        r.raise_for_status()
        allowed = set(r.json().get("processAction") or [])
        code = {"start": 1, "stop": 2, "pause": 3}.get(action)
        if code is None:
            raise ValueError("Unbekannte Aktion")
        if code not in allowed:
            raise ValueError("Das Gerät erlaubt das gerade nicht" + (" – MobileStart am Gerät aktivieren" if action == "start" else ""))
        r = requests.put(f"{API}/devices/{serial}/actions", headers=h, json={"processAction": code}, timeout=15)
        r.raise_for_status()
        self._wake.set()

    # ---------------------------------------------------------- Mitteilung: Programm fertig
    def _notify(self, serial):
        d = self.devices.get(serial) or {}
        status = _val(d, "state", "status", "value_raw")
        prev = self._prev.get(serial)
        self._prev[serial] = status
        self._signals(serial, d)
        if prev in RUNNING and status in FINISHED | {1, 2, 10} and self.push is not None and self.push.configured:
            name = _label(d)
            eco = _val(d, "state", "ecoFeedback", "currentEnergyConsumption", "value")
            body = "Zeit zum Ausräumen." + (f" Verbrauch: {eco:.2f} kWh." if isinstance(eco, (int, float)) and eco else "")
            self.push.send("washer", f"{name} ist fertig", body)
            print(f"[MIELE] {name} fertig → Mitteilung", flush=True)

    # Hinweise: Tür offen (Kühl/Gefrier, > 2 Min.), Info-Signal (Salz/Klarspüler/TwinDos), Störung – jeweils einmal pro Ereignis
    def _signals(self, serial, d):
        if self.push is None or not self.push.configured:
            return
        st = d.get("state") or {}
        type_raw = _val(d, "ident", "type", "value_raw")
        name = _label(d)
        flags = getattr(self, "_flags", None)
        if flags is None:
            flags = self._flags = {}
        first = serial not in flags                 # nach Hub-Neustart: Zustand nur merken, nicht erneut melden
        f = flags.setdefault(serial, {})
        now = time.time()
        door = bool(st.get("signalDoor"))
        if type_raw in self.FRIDGE:
            if door:
                f.setdefault("door_since", now)
                if now - f["door_since"] > 120 and not f.get("door_sent"):
                    f["door_sent"] = True
                    self.push.send("washer", f"🚪 {name}: Tür offen", "Die Tür steht seit über 2 Minuten offen.")
            else:
                f.pop("door_since", None); f.pop("door_sent", None)
        for key, title, body in (("signalInfo", f"ℹ️ {name}", "Hinweis am Gerät – z. B. Salz, Klarspüler oder TwinDos nachfüllen."),
                                 ("signalFailure", f"⚠️ {name}: Störung", "Bitte am Gerät oder in der Miele-App nachsehen.")):
            on = bool(st.get(key))
            if on and not f.get(key) and not first:
                self.push.send("washer", title, body)
            f[key] = on
