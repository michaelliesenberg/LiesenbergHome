#!/usr/bin/env python3
"""Bosch/Siemens Home Connect – Hausgeräte lesen (Status, Programm, Restzeit).

Anmeldung: Einrichtungsseite des Hubs → Hausgeräte → „Mit Home Connect anmelden"
(Device Flow – Code im Browser bestätigen; oder auf dem Pi: sudo python3 homeconnect.py login)

Live-Updates kommen über den Event-Stream von Home Connect (eine dauerhafte
Verbindung), damit wir weit unter dem Limit von 1000 Abfragen/Tag bleiben.
"""
import json
import os
import sys
import threading
import time

import requests

BASE = "https://api.home-connect.com"
ACCEPT = "application/vnd.bsh.sdk.v1+json"
ENV_FILE = "/etc/liesenberg-home.env"
TOKEN_FILE = os.environ.get("HC_TOKEN_FILE", "/opt/liesenberg-home/homeconnect-tokens.json")
SCOPE = "IdentifyAppliance Monitor Settings Control"


def _env(n):
    return os.environ.get(n, "")


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
        with open(tmp, "w") as f:
            json.dump(tok, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, TOKEN_FILE)
        self.tokens = tok

    @property
    def ready(self):
        return bool(self.tokens and self.tokens.get("refresh_token"))

    def access_token(self):
        with self.lock:
            if not self.ready:
                self.tokens = self._load()
                if not self.ready:
                    raise RuntimeError("Home Connect noch nicht angemeldet")
            if time.time() - self.tokens.get("obtained_at", 0) > self.tokens.get("expires_in", 86400) - 300:
                data = {"grant_type": "refresh_token", "refresh_token": self.tokens["refresh_token"],
                        "client_id": _env("HC_CLIENT_ID")}
                if _env("HC_CLIENT_SECRET"):
                    data["client_secret"] = _env("HC_CLIENT_SECRET")
                r = requests.post(f"{BASE}/security/oauth/token", data=data, timeout=15)
                r.raise_for_status()
                new = r.json()
                new.setdefault("refresh_token", self.tokens["refresh_token"])
                self._save(new)
            return self.tokens["access_token"]


# Klartext für die App
STATE_DE = {
    "Inactive": "Aus", "Ready": "Bereit", "DelayedStart": "Startzeit gesetzt", "Run": "Läuft",
    "Pause": "Pause", "ActionRequired": "Aktion nötig", "Finished": "Fertig", "Error": "Fehler",
    "Aborting": "Wird abgebrochen",
}
TYPE_DE = {"Washer": "Waschmaschine", "Dryer": "Trockner", "WasherDryer": "Waschtrockner",
           "Dishwasher": "Geschirrspüler", "Oven": "Backofen", "FridgeFreezer": "Kühlschrank",
           "Refrigerator": "Kühlschrank", "Freezer": "Gefrierschrank", "CoffeeMaker": "Kaffeevollautomat",
           "Hob": "Kochfeld", "Hood": "Dunstabzug", "WineCooler": "Weinkühlschrank"}


# Ereignisse, die eine Mitteilung wert sind (Schlüssel-Ende → Titel, Text)
EVENT_PUSH = {
    "PreheatFinished": ("ist vorgeheizt", "Die Temperatur ist erreicht."),
    "SaltNearlyEmpty": ("Salz fast leer", "Bitte Regeneriersalz nachfüllen."),
    "RinseAidNearlyEmpty": ("Klarspüler fast leer", "Bitte Klarspüler nachfüllen."),
    "IDos1FillLevelPoor": ("i-Dos 1 fast leer", "Bitte Waschmittel nachfüllen."),
    "IDos2FillLevelPoor": ("i-Dos 2 fast leer", "Bitte Weichspüler/Waschmittel nachfüllen."),
    "ProgramAborted": ("Programm abgebrochen", "Das Programm wurde abgebrochen."),
    "DoorAlarmFreezer": ("Gefrierfach-Tür offen", "Bitte Tür schließen."),
    "DoorAlarmRefrigerator": ("Kühlschrank-Tür offen", "Bitte Tür schließen."),
    "TemperatureAlarmFreezer": ("Gefrierfach zu warm", "Bitte nachsehen."),
}


class HomeConnect:
    def __init__(self):
        self.auth = Auth()
        self.appliances = {}   # haId → dict
        self.error = None
        self.updated = None
        self.on_event = None   # (gerät, titel, text) → Mitteilung; vom Hub gesetzt
        self._detail_cache = {}
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def configured(self):
        return bool(_env("HC_CLIENT_ID"))

    def _get(self, path):
        r = requests.get(f"{BASE}{path}", headers={"Authorization": f"Bearer {self.auth.access_token()}",
                                                   "Accept": ACCEPT, "Accept-Language": "de-DE"}, timeout=20)
        if r.status_code in (404, 409):   # Gerät offline / kein Programm aktiv / nicht unterstützt
            return None
        r.raise_for_status()
        return r.json().get("data")

    def _put(self, path, body):
        r = requests.put(f"{BASE}{path}", json=body, timeout=20,
                         headers={"Authorization": f"Bearer {self.auth.access_token()}",
                                  "Accept": ACCEPT, "Content-Type": ACCEPT})
        if r.status_code >= 400:
            try:
                msg = (r.json().get("error") or {}).get("description") or r.text
            except ValueError:
                msg = r.text
            raise RuntimeError(f"Home Connect: {msg[:200]}")

    def start_selected(self, ha_id):
        """Das am Gerät gewählte Programm starten (Fernstart muss am Gerät erlaubt sein)."""
        prog = self._get(f"/api/homeappliances/{ha_id}/programs/selected")
        if not prog or not prog.get("key"):
            raise RuntimeError("Am Gerät ist kein Programm gewählt")
        opts = [{"key": o["key"], "value": o["value"]} for o in prog.get("options", [])
                if o.get("key") and "value" in o and not o["key"].endswith(("StartInRelative", "FinishInRelative"))]
        try:
            self._put(f"/api/homeappliances/{ha_id}/programs/active", {"data": {"key": prog["key"], "options": opts}})
        except RuntimeError:
            # manche Optionen sind nur lesbar → nur mit dem Programm (Standard-Optionen) starten
            self._put(f"/api/homeappliances/{ha_id}/programs/active", {"data": {"key": prog["key"]}})
        return prog["key"].split(".")[-1]

    def _delete(self, path):
        r = requests.delete(f"{BASE}{path}", timeout=20,
                            headers={"Authorization": f"Bearer {self.auth.access_token()}", "Accept": ACCEPT})
        if r.status_code >= 400:
            raise RuntimeError(f"Home Connect: {r.text[:200]}")

    # ---------------------------------------------------------- Details (gecacht – Home Connect erlaubt 1000 Abfragen/Tag)
    @staticmethod
    def _opt(o):
        return {"key": o.get("key"), "name": o.get("name") or o.get("key", "").split(".")[-1], "value": o.get("value"),
                "displayvalue": o.get("displayvalue"), "unit": o.get("unit")}

    def detail(self, ha_id, fresh=False):
        c = self._detail_cache.get(ha_id)
        if c and not fresh and time.time() - c[0] < 45:
            return c[1]
        ha = dict(self.appliances.get(ha_id) or {})
        base = f"/api/homeappliances/{ha_id}"
        status = (self._get(f"{base}/status") or {}).get("status", [])
        settings = (self._get(f"{base}/settings") or {}).get("settings", [])
        active = self._get(f"{base}/programs/active")
        selected = self._get(f"{base}/programs/selected")
        avail = (self._get(f"{base}/programs/available") or {}).get("programs", [])
        val = {x["key"]: x.get("value") for x in status + settings if "key" in x}
        shortv = lambda k: str(val.get(k) or "").split(".")[-1] or None
        out = {
            **ha,
            "door": shortv("BSH.Common.Status.DoorState"),
            "remoteControl": bool(val.get("BSH.Common.Status.RemoteControlActive")),
            "remoteStart": bool(val.get("BSH.Common.Status.RemoteControlStartAllowed")),
            "power": shortv("BSH.Common.Setting.PowerState"),
            "cavityTemp": val.get("Cooking.Oven.Status.CurrentCavityTemperature"),
            "active": ({"key": active.get("key"), "name": active.get("name") or active.get("key", "").split(".")[-1],
                        "options": [self._opt(o) for o in active.get("options", [])]} if active else None),
            "selected": ({"key": selected.get("key"), "name": selected.get("name") or selected.get("key", "").split(".")[-1],
                          "options": [self._opt(o) for o in selected.get("options", [])]} if selected else None),
            "programs": [{"key": p.get("key"), "name": p.get("name") or p.get("key", "").split(".")[-1]}
                         for p in avail if p.get("key")],
            "settings": [{"key": x["key"], "name": x.get("name") or x["key"].split(".")[-1], "value": x.get("value"),
                          "unit": x.get("unit")} for x in settings if "key" in x],
        }
        self._detail_cache[ha_id] = (time.time(), out)
        return out

    def program_options(self, ha_id, key):
        """Optionen eines Programms mit erlaubten Werten (Temperatur, Dauer, Schleudern …)"""
        p = self._get(f"/api/homeappliances/{ha_id}/programs/available/{key}") or {}
        opts = []
        for o in p.get("options", []):
            con = o.get("constraints") or {}
            opts.append({"key": o.get("key"), "name": o.get("name") or o.get("key", "").split(".")[-1], "type": o.get("type"),
                         "unit": o.get("unit"), "default": con.get("default"), "min": con.get("min"), "max": con.get("max"),
                         "step": con.get("stepsize"), "allowed": con.get("allowedvalues"), "display": con.get("displayvalues"),
                         "readOnly": con.get("access") == "read"})
        return {"key": key, "name": p.get("name") or key.split(".")[-1], "options": opts}

    def start(self, ha_id, key, options=None):
        """Programm mit Optionen starten – nur wenn Fernstart am Gerät erlaubt ist (prüft Home Connect selbst)."""
        body = {"data": {"key": key}}
        if options:
            body["data"]["options"] = [{"key": k, "value": v} for k, v in options.items() if v is not None]
        self._put(f"/api/homeappliances/{ha_id}/programs/active", body)
        self._detail_cache.pop(ha_id, None)

    def stop(self, ha_id):
        self._delete(f"/api/homeappliances/{ha_id}/programs/active")
        self._detail_cache.pop(ha_id, None)

    def set_option(self, ha_id, key, value):
        """Option des laufenden Programms ändern (z. B. Backofen-Temperatur oder Dauer)"""
        self._put(f"/api/homeappliances/{ha_id}/programs/active/options/{key}", {"data": {"key": key, "value": value}})
        self._detail_cache.pop(ha_id, None)

    def set_power(self, ha_id, state):
        self._put(f"/api/homeappliances/{ha_id}/settings/BSH.Common.Setting.PowerState",
                  {"data": {"key": "BSH.Common.Setting.PowerState", "value": f"BSH.Common.EnumType.PowerState.{state}"}})
        self._detail_cache.pop(ha_id, None)

    def _apply(self, ha, key, value):
        short = key.split(".")[-1]
        if key == "BSH.Common.Status.OperationState":
            ha["state"] = str(value).split(".")[-1]
        elif key == "BSH.Common.Status.DoorState":
            ha["door"] = str(value).split(".")[-1]
        elif key == "BSH.Common.Status.RemoteControlStartAllowed":
            ha["remoteStart"] = bool(value)
        elif key == "BSH.Common.Option.RemainingProgramTime":
            ha["remaining"] = value
        elif key == "BSH.Common.Option.ProgramProgress":
            ha["progress"] = value
        elif key == "BSH.Common.Root.ActiveProgram":
            ha["program"] = str(value).split(".")[-1] if value else None
        elif short in ("FridgeSetpointTemperature", "SetpointTemperatureRefrigerator"):
            ha["fridgeTemp"] = value
        elif short in ("FreezerSetpointTemperature", "SetpointTemperatureFreezer"):
            ha["freezerTemp"] = value
        ha["stateText"] = STATE_DE.get(ha.get("state"), ha.get("state") or "–")

    def refresh_all(self):
        data = self._get("/api/homeappliances") or {}
        for a in data.get("homeappliances", []):
            ha = self.appliances.setdefault(a["haId"], {})
            type_de = TYPE_DE.get(a.get("type"), a.get("type"))
            name = a.get("name") or ""
            # Home Connect liefert oft nur den englischen Typ als Namen ("Dishwasher") → deutsch anzeigen
            if not name or name == a.get("type"):
                name = type_de
            ha.update(id=a["haId"], name=name, brand=a.get("brand"), type=a.get("type"),
                      typeText=type_de, connected=a.get("connected", False))
            if not ha["connected"]:
                ha["stateText"] = "Offline"
                continue
            for item in (self._get(f"/api/homeappliances/{a['haId']}/status") or {}).get("status", []):
                self._apply(ha, item["key"], item.get("value"))
            prog = self._get(f"/api/homeappliances/{a['haId']}/programs/active")
            ha["program"] = prog.get("key", "").split(".")[-1] if prog else None
            for opt in (prog or {}).get("options", []):
                self._apply(ha, opt["key"], opt.get("value"))
            if a.get("type") in ("FridgeFreezer", "Refrigerator", "Freezer"):
                for s in (self._get(f"/api/homeappliances/{a['haId']}/settings") or {}).get("settings", []):
                    self._apply(ha, s["key"], s.get("value"))
        self.updated, self.error = time.time(), None

    def _events(self):
        """Dauerhafte Verbindung – Home Connect schickt Änderungen sofort."""
        with requests.get(f"{BASE}/api/homeappliances/events",
                          headers={"Authorization": f"Bearer {self.auth.access_token()}",
                                   "Accept": "text/event-stream"}, stream=True, timeout=(15, 120)) as r:
            r.raise_for_status()
            ev, ha_id = None, None
            for raw in r.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                if raw.startswith("event:"):
                    ev = raw[6:].strip()
                elif raw.startswith("id:"):
                    ha_id = raw[3:].strip()
                elif raw.startswith("data:") and ev in ("STATUS", "EVENT", "NOTIFY"):
                    try:
                        items = json.loads(raw[5:]).get("items", [])
                    except ValueError:
                        continue
                    ha = self.appliances.get(ha_id or "")
                    for it in items:
                        ha = self.appliances.get(it.get("haId", ha_id), ha)
                        if ha is not None:
                            self._apply(ha, it.get("key", ""), it.get("value"))
                            self._detail_cache.pop(ha.get("id"), None)
                            short = it.get("key", "").split(".")[-1]
                            present = str(it.get("value", "")).endswith(("Present", "Confirmed")) or it.get("value") is True
                            if ev == "EVENT" and short in EVENT_PUSH and present and self.on_event:
                                title, body = EVENT_PUSH[short]
                                try:
                                    self.on_event(ha, title, body)
                                except Exception:
                                    pass
                    self.updated = time.time()
                elif raw.startswith("data:") and ev in ("CONNECTED", "DISCONNECTED", "PAIRED", "DEPAIRED"):
                    self.refresh_all()

    def _run(self):
        last_full = 0
        while True:
            if not (self.configured and self.auth.ready):
                self.auth.tokens = self.auth._load()
                time.sleep(30)
                continue
            try:
                if time.time() - last_full > 1800:   # alle 30 min komplett, sonst nur Events
                    self.refresh_all()
                    last_full = time.time()
                self._events()
            except Exception as e:
                self.error = str(e)
                time.sleep(60)

    def list(self):
        return sorted(self.appliances.values(), key=lambda a: a.get("typeText") or "")


# ------------------------------------------------------------------ Anmeldung über die Einrichtungsseite
WEB_LOGIN = {"status": "idle"}     # idle | waiting | ok | error


def start_web_login():
    """Device Flow starten; ein Hintergrund-Thread wartet auf die Bestätigung im Browser."""
    if not _env("HC_CLIENT_ID"):
        raise RuntimeError("Erst die Client-ID eintragen")
    r = requests.post(f"{BASE}/security/oauth/device_authorization",
                      data={"client_id": _env("HC_CLIENT_ID"), "scope": SCOPE}, timeout=15)
    r.raise_for_status()
    d = r.json()
    WEB_LOGIN.clear()
    WEB_LOGIN.update(status="waiting", url=d.get("verification_uri_complete", d["verification_uri"]),
                     code=d["user_code"], expires=time.time() + d.get("expires_in", 300))

    def poll():
        interval = d.get("interval", 5)
        while time.time() < WEB_LOGIN["expires"]:
            time.sleep(interval)
            data = {"grant_type": "device_code", "device_code": d["device_code"], "client_id": _env("HC_CLIENT_ID")}
            if _env("HC_CLIENT_SECRET"):
                data["client_secret"] = _env("HC_CLIENT_SECRET")
            try:
                t = requests.post(f"{BASE}/security/oauth/token", data=data, timeout=15)
            except requests.RequestException:
                continue
            if t.status_code == 200:
                Auth()._save(t.json())
                WEB_LOGIN.update(status="ok")
                return
            try:
                err = t.json().get("error")
            except ValueError:
                err = t.text[:80]
            if err == "slow_down":
                interval += 5
            elif err != "authorization_pending":
                WEB_LOGIN.update(status="error", error=str(err))
                return
        WEB_LOGIN.update(status="error", error="Zeit abgelaufen")

    threading.Thread(target=poll, daemon=True).start()
    return dict(WEB_LOGIN)


# ------------------------------------------------------------------ Anmeldung (CLI)
def _load_env_file():
    try:
        with open(ENV_FILE) as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.rstrip("\n").split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except PermissionError:
        sys.exit("Bitte mit sudo starten:  sudo python3 /opt/liesenberg-home/homeconnect.py login")


def _login():
    _load_env_file()
    if not _env("HC_CLIENT_ID"):
        sys.exit("HC_CLIENT_ID fehlt in /etc/liesenberg-home.env")
    r = requests.post(f"{BASE}/security/oauth/device_authorization",
                      data={"client_id": _env("HC_CLIENT_ID"), "scope": SCOPE}, timeout=15)
    r.raise_for_status()
    d = r.json()
    print("\n1) Diese Seite im Browser öffnen:\n")
    print("   " + d.get("verification_uri_complete", d["verification_uri"]))
    print(f"\n2) Falls gefragt, diesen Code eingeben:  {d['user_code']}")
    print("   Mit deinem Home-Connect-Konto (wie in der App) anmelden und zustimmen.\n")
    print("Warte auf Bestätigung …")
    interval = d.get("interval", 5)
    deadline = time.time() + d.get("expires_in", 600)
    while time.time() < deadline:
        time.sleep(interval)
        data = {"grant_type": "device_code", "device_code": d["device_code"], "client_id": _env("HC_CLIENT_ID")}
        if _env("HC_CLIENT_SECRET"):
            data["client_secret"] = _env("HC_CLIENT_SECRET")
        t = requests.post(f"{BASE}/security/oauth/token", data=data, timeout=15)
        if t.status_code == 200:
            Auth()._save(t.json())
            owner = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
            if all(owner):
                os.chown(TOKEN_FILE, int(owner[0]), int(owner[1]))
            print("\nHome Connect angemeldet. Der Haus-Server übernimmt das in ca. 30 Sekunden.")
            return
        err = t.json().get("error") if t.headers.get("content-type", "").startswith("application/json") else t.text
        if err == "slow_down":
            interval += 5
        elif err not in ("authorization_pending",):
            sys.exit(f"Anmeldung fehlgeschlagen: {err}")
    sys.exit("Zeit abgelaufen – bitte nochmal starten.")


if __name__ == "__main__":
    if sys.argv[1:] == ["login"]:
        _login()
    else:
        print(__doc__)
