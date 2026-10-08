#!/usr/bin/env python3
"""Daikin Onecta Cloud API – Klimageräte lesen und steuern.

Anmeldung: Einrichtungsseite des Hubs → Klima → „Mit Daikin anmelden"
(oder auf dem Pi:  sudo python3 /opt/liesenberg-home/daikin.py login)

Danach erneuert der Haus-Server die Anmeldung selbst (Refresh-Token).
Daikin erlaubt ca. 200 Abfragen pro Tag → Status wird alle 10 Minuten geholt
und nach jedem Befehl einmal aktualisiert.
"""
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse

import requests

IDP = "https://idp.onecta.daikineurope.com/v1/oidc"
API = "https://api.onecta.daikineurope.com/v1"
ENV_FILE = "/etc/liesenberg-home.env"
TOKEN_FILE = os.environ.get("DAIKIN_TOKEN_FILE", "/opt/liesenberg-home/daikin-tokens.json")


def redirect_uri():
    """Muss exakt so im Daikin-Entwicklerportal eingetragen sein: <öffentliche Hub-Adresse>/daikin/callback"""
    return os.environ.get("DAIKIN_REDIRECT_URI", "")
POLL_SECONDS = 600
ROOMS_FILE = os.environ.get("ROOMS_FILE", "/opt/liesenberg-home/rooms.json")
PENDING_STATES = {}   # state → Zeitpunkt (Anmeldung über die Einrichtungsseite)


def _env(name):
    return os.environ.get(name, "")


# ------------------------------------------------------------------ Tokens
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

    def exchange_code(self, code):
        r = requests.post(f"{IDP}/token", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": _env("DAIKIN_CLIENT_ID"), "client_secret": _env("DAIKIN_CLIENT_SECRET"),
            "redirect_uri": redirect_uri()}, timeout=15)
        r.raise_for_status()
        self._save(r.json())

    def access_token(self):
        with self.lock:
            if not self.ready:
                raise RuntimeError("Daikin noch nicht angemeldet")
            age = time.time() - self.tokens.get("obtained_at", 0)
            if age > self.tokens.get("expires_in", 3600) - 120:
                r = requests.post(f"{IDP}/token", data={
                    "grant_type": "refresh_token", "refresh_token": self.tokens["refresh_token"],
                    "client_id": _env("DAIKIN_CLIENT_ID"), "client_secret": _env("DAIKIN_CLIENT_SECRET")},
                    timeout=15)
                r.raise_for_status()
                new = r.json()
                new.setdefault("refresh_token", self.tokens["refresh_token"])
                self._save(new)
            return self.tokens["access_token"]


def authorize_url(state):
    q = {"response_type": "code", "client_id": _env("DAIKIN_CLIENT_ID"), "redirect_uri": redirect_uri(),
         "scope": "openid onecta:basic.integration", "state": state,
         "prompt": "login"}  # immer Anmeldeseite zeigen, nie still das Browser-Konto übernehmen
    return f"{IDP}/authorize?{urllib.parse.urlencode(q)}"


# ------------------------------------------------------------------ Geräte
def _v(mp, name, default=None):
    c = mp.get(name)
    return c.get("value", default) if isinstance(c, dict) else default


def _unit(device):
    """Gerät → kompaktes Format für die App."""
    mps = {m.get("embeddedId"): m for m in device.get("managementPoints", [])}
    cc = next((m for m in mps.values() if m.get("managementPointType") == "climateControl"), None)
    if not cc:
        return None
    mode = _v(cc, "operationMode", "")
    sens = _v(cc, "sensoryData", {}) or {}
    tc = (_v(cc, "temperatureControl", {}) or {}).get("operationModes", {})
    sp = (tc.get(mode, {}).get("setpoints", {}) or {}).get("roomTemperature", {})
    return {
        "id": device.get("id"),
        "embeddedId": cc.get("embeddedId"),
        "name": _v(cc, "name") or device.get("deviceModel") or "Klimagerät",
        "on": _v(cc, "onOffMode") == "on",
        "mode": mode,
        "modes": (cc.get("operationMode") or {}).get("values", []),
        "roomTemperature": (sens.get("roomTemperature") or {}).get("value"),
        "outdoorTemperature": (sens.get("outdoorTemperature") or {}).get("value"),
        "target": sp.get("value"),
        "targetMin": sp.get("minValue"), "targetMax": sp.get("maxValue"), "targetStep": sp.get("stepValue", 0.5),
        "online": (device.get("isCloudConnectionUp") or {}).get("value", True),
        "room": None,
    }


class Daikin:
    def __init__(self, room_names=lambda: []):
        self.auth = Auth()
        self.units = []
        self.raw_summary = []
        self.updated = None
        self.error = None
        self.room_names = room_names
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def configured(self):
        return bool(_env("DAIKIN_CLIENT_ID") and _env("DAIKIN_CLIENT_SECRET"))

    def _headers(self):
        return {"Authorization": f"Bearer {self.auth.access_token()}"}

    def refresh(self):
        r = requests.get(f"{API}/gateway-devices", headers=self._headers(), timeout=20)
        r.raise_for_status()
        raw = r.json()
        self.raw_summary = [{"id": d.get("id"), "model": d.get("deviceModel"),
                             "types": [m.get("managementPointType") for m in d.get("managementPoints", [])]}
                            for d in raw]
        units = [u for u in (_unit(d) for d in raw) if u]
        rooms = self.room_names()
        # Feste Zuordnung (hat Vorrang): /opt/liesenberg-home/rooms.json  {"Gerätename": "LUXOR-Raum"}
        try:
            with open(ROOMS_FILE) as f:
                fixed = json.load(f)
        except (OSError, ValueError):
            fixed = {}
        for u in units:  # sonst: Gerätename → LUXOR-Raum ("Schlafzimmer DG" → "Schlafzimmer")
            low = (u["name"] or "").lower()
            u["room"] = fixed.get(u["name"]) or next(
                (n for n in sorted(rooms, key=len, reverse=True) if n.lower() in low), None)
        self.units, self.updated, self.error = units, time.time(), None

    def _loop(self):
        while True:
            if not self.auth.ready:
                self.auth.tokens = self.auth._load()  # Anmeldung per CLI ohne Neustart erkennen
            if self.configured and self.auth.ready:
                try:
                    self.refresh()
                except Exception as e:  # nicht abstürzen, beim nächsten Mal wieder
                    self.error = str(e)
            # angemeldet: alle 10 min; sonst alle 30 s nachsehen
            self._wake.wait(POLL_SECONDS if self.auth.ready else 30)
            self._wake.clear()

    def _patch(self, u, characteristic, value, path=None):
        body = {"value": value}
        if path:
            body["path"] = path
        r = requests.patch(f"{API}/gateway-devices/{u['id']}/management-points/{u['embeddedId']}"
                           f"/characteristics/{characteristic}", json=body, headers=self._headers(), timeout=20)
        if r.status_code not in (200, 204):
            raise RuntimeError(f"Daikin lehnt ab (HTTP {r.status_code}): {r.text[:200]}")

    def set(self, unit_id, on=None, mode=None, target=None):
        u = next((x for x in self.units if x["id"] == unit_id), None)
        if not u:
            raise KeyError(unit_id)
        if mode is not None:
            self._patch(u, "operationMode", mode)
            u["mode"] = mode
        if target is not None:
            self._patch(u, "temperatureControl", target,
                        f"/operationModes/{u['mode']}/setpoints/roomTemperature")
            u["target"] = target
        if on is not None:
            self._patch(u, "onOffMode", "on" if on else "off")
            u["on"] = on
        # Daikin braucht ein paar Sekunden – dann einmal frisch lesen
        threading.Timer(8, self._wake.set).start()
        return u


# ------------------------------------------------------------------ Anmeldung (CLI)
def _load_env_file():
    try:
        with open(ENV_FILE) as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.rstrip("\n").split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except PermissionError:
        sys.exit("Bitte mit sudo starten:  sudo python3 /opt/liesenberg-home/daikin.py login")


def _login():
    _load_env_file()
    if not (_env("DAIKIN_CLIENT_ID") and _env("DAIKIN_CLIENT_SECRET")):
        sys.exit("DAIKIN_CLIENT_ID / DAIKIN_CLIENT_SECRET fehlen in /etc/liesenberg-home.env")
    state = secrets.token_urlsafe(16)
    print("\n1) Diesen Link im Browser öffnen und mit deinem Daikin-Konto anmelden:\n")
    print("   " + authorize_url(state) + "\n")
    print("2) Danach landet der Browser auf deiner Hub-Adresse (Fehlerseite ist ok).")
    print("   Die KOMPLETTE Adresse aus der Adresszeile kopieren und hier einfügen.\n")
    url = input("Adresse: ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if q.get("state", [""])[0] != state or "code" not in q:
        sys.exit("Das ist nicht die richtige Adresse (code/state fehlt). Bitte nochmal.")
    Auth().exchange_code(q["code"][0])
    owner = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if all(owner):
        os.chown(TOKEN_FILE, int(owner[0]), int(owner[1]))
    print("\nDaikin angemeldet. Jetzt:  sudo systemctl restart liesenberg-home")


if __name__ == "__main__":
    if sys.argv[1:] == ["login"]:
        _login()
    else:
        print(__doc__)
