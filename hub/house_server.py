#!/usr/bin/env python3
"""Liesenberg Home Hub – der Haus-Server für den Raspberry Pi.

Die App spricht nur mit dem Hub. Der Hub spricht mit den Geräten:
  LUXORliving (Theben IP1) oder Home Assistant · evcc · Daikin Onecta · Home Connect · HomePods

App-API (Authorization: Bearer <persönlicher Schlüssel>):
  GET  /api/health                 – Lebenszeichen (ohne Schlüssel)
  POST /api/join                   – Einladungscode → persönlicher Schlüssel (ohne Schlüssel)
  GET  /api/me                     – wer bin ich, was darf ich, wie heißt das Zuhause
  GET  /api/home                   – Etagen → Räume → Geräte
  GET  /api/home/state?ids=a,b     – Zustand von Geräten
  POST /api/home/device/{id}       – Gerät schalten  {"on"|"level"|"position"|"move"|"target"|"trigger"}
  POST /api/home/central-off       – alle Lichter aus
  GET/PUT/DELETE /api/scenes, POST /api/scenes/{id}/run
  GET  /api/energy · /api/energy/history?date= · /api/energy/days · /api/energy/summary?period=&date= · /api/energy/plan · GET/POST/DELETE /api/smartstart (Geräte automatisch starten) ·
  GET/PUT /api/prices (Strompreise) und /api/energy/summary: Besitzer + Vollzugriff, nicht Gäste · /api/daikin · /api/appliances · /api/music …
  GET/POST/PUT/DELETE /api/people  – Personen & Einladungen (nur Besitzer)
  GET  /api/backup                 – komplette Sicherung als .tgz (nur Besitzer)
  GET  /api/hub/version · POST /api/hub/update – Version prüfen / aktualisieren (Update: Besitzer + Vollzugriff)
Einrichtung im Browser:  http://<hub>:8080/setup  (nur im Heimnetz)
"""
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
from datetime import datetime, timezone

import requests
import urllib3
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

import hub_config
import people

hub_config.load()
hub_config.apply_env()                      # vor dem Import von daikin/homeconnect
people.migrate_legacy_token(hub_config.legacy_app_token())

import devices  # noqa: E402
import lxp      # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = open(os.path.join(HERE, "VERSION")).read().strip() if os.path.exists(os.path.join(HERE, "VERSION")) else "dev"
PORT = int(os.environ.get("HUB_PORT", "8080"))


# ---------------------------------------------------------------- LUXORliving IP1
class _LegacyTLS(HTTPAdapter):
    """Das IP1 spricht nur alte TLS-Ciphers mit selbstsigniertem Zertifikat."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        self.poolmanager = PoolManager(*args, ssl_context=ctx, **kwargs)


class Luxor:
    """Verbindung zum IP1. Zugangsdaten kommen bei jedem Login frisch aus der Einrichtung."""

    def __init__(self):
        self.s = requests.Session()
        self.s.mount("https://", _LegacyTLS(pool_maxsize=8))
        self.token = None
        self.lock = threading.Lock()
        self.io = threading.RLock()          # das IP1 verträgt keine parallelen Anfragen
        self.id_style = None
        self.last_write = ""

    @property
    def host(self):
        return hub_config.get("luxor.host")

    def _login(self):
        if not self.host:
            raise RuntimeError("LUXORliving ist noch nicht eingerichtet")
        r = self.s.post(f"https://{self.host}/rest/login",
                        json={"username": hub_config.get("luxor.user") or "admin",
                              "password": hub_config.get("luxor.password") or ""},
                        verify=False, timeout=10)
        if r.status_code != 200 or "<html" in r.text:
            raise RuntimeError(f"LUXOR-Anmeldung fehlgeschlagen (HTTP {r.status_code})")
        self.token = r.text.strip().strip('"')

    def reset(self):
        with self.lock:
            self.token = None

    def _ensure(self):
        with self.lock:
            if not self.token:
                self._login()

    def _relogin(self):
        with self.lock:
            self._login()

    def get(self, dp: int):
        with self.io:
            self._ensure()
            for attempt in (1, 2):
                try:
                    r = self.s.get(f"https://{self.host}/rest/datapoints/{dp}",
                                   cookies={"user": self.token}, verify=False, timeout=5)
                except requests.RequestException:
                    r = None
                if r is not None and r.status_code == 200:
                    try:
                        data = r.json()
                    except ValueError:
                        data = None
                    if isinstance(data, dict):
                        return data.get("value")
                if attempt == 1:
                    self._relogin()
            return None

    def raw_get(self, path: str):
        with self.io:
            self._ensure()
            r = self.s.get(f"https://{self.host}{path}", cookies={"user": self.token}, verify=False, timeout=10)
            return r.status_code, r.text

    def set(self, dp: int, value):
        variants = [dp, str(dp)] if self.id_style is None else [dp if self.id_style == "int" else str(dp)]
        with self.io:
            self._ensure()
            for attempt in (1, 2):
                for ident in variants:
                    body = {"command": 3, "datapoints_values": [{"id": ident, "value": value}]}
                    r = self.s.put(f"https://{self.host}/rest/datapoints/values", json=body,
                                   cookies={"user": self.token}, verify=False, timeout=5)
                    self.last_write = f"HTTP {r.status_code}: {r.text[:200]}"
                    print(f"[LUXOR] write dp={ident!r} value={value!r} -> {self.last_write}", flush=True)
                    if 200 <= r.status_code < 300 and "error" not in r.text.lower():
                        self.id_style = "int" if isinstance(ident, int) else "str"
                        return True
                if attempt == 1:
                    self._relogin()
            return False


luxor = Luxor()
app = FastAPI(title="Liesenberg Home Hub", version=VERSION)


# ---------------------------------------------------------------- Geräte-Backend (LUXOR / Home Assistant / Demo)
_backend = {"key": None, "obj": None}


def home():
    """Aktives Backend laut Einrichtung (oder None)."""
    c = hub_config.load()
    kind = c.get("backend")
    key = (kind, c["homeassistant"]["url"], c["homeassistant"]["token"])
    if _backend["key"] != key:
        if kind == "luxor":
            obj = devices.LuxorBackend(luxor)
        elif kind == "homeassistant" and c["homeassistant"]["url"]:
            import ha
            obj = ha.HABackend(c["homeassistant"]["url"], c["homeassistant"]["token"])
        elif kind == "shelly":
            import shelly
            obj = shelly.ShellyBackend(lambda: hub_config.get("shelly"), lambda: hub_config.get("home_name"))
        elif kind == "demo":
            obj = devices.DemoBackend()
        else:
            obj = None
        if obj is not None:
            _wrap_for_consumers(obj)
        _backend.update(key=key, obj=obj)
    return _backend["obj"]


def _wrap_for_consumers(obj):
    """Jeder Schaltbefehl (App, Szene, Siri, Zentral aus) → Verbrauchs-Messung im Hintergrund."""
    orig = obj.command

    def command(did, cmd):
        res = orig(did, cmd)
        try:
            c = globals().get("consumers")
            if c is not None:
                d = devices.device_index(devices.apply_overrides(obj.structure())).get(did)
                if d:
                    c.observe(did, d, cmd)
        except Exception:
            pass
        return res
    obj.command = command

    orig_off = obj.central_off

    def central_off():
        c = globals().get("consumers")
        if c is not None:
            c.note_command()                # Zentral aus: viele Lichter gleichzeitig, nicht zuordnen
        return orig_off()
    obj.central_off = central_off


def need_home():
    h = home()
    if h is None:
        raise HTTPException(status_code=409, detail="Noch kein Haus eingerichtet – Einrichtungsseite des Hubs öffnen")
    return h


def structure():
    try:
        return devices.apply_overrides(need_home().structure())
    except FileNotFoundError:
        raise HTTPException(status_code=409, detail="Noch keine LUXOR-Projektdatei (.lxp) hochgeladen")
    except PermissionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Geräte nicht erreichbar: {e}")


# ---------------------------------------------------------------- Zugang & Rollen
_fails = {}          # IP → [Zeitpunkte fehlgeschlagener Versuche]


def client_ip(request: Request):
    """Echte Absender-IP. Über Caddy/Tailscale kommen Anfragen von 127.0.0.1 – dann zählt der
    letzte X-Forwarded-For-Eintrag (den hat der Proxy selbst gesetzt, nicht der Absender)."""
    host = request.client.host if request.client else "?"
    fwd = request.headers.get("x-forwarded-for")
    if fwd and host in ("127.0.0.1", "::1"):
        return fwd.split(",")[-1].strip()
    return host


def _throttle(ip):
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 600]
    _fails[ip] = recent
    if len(recent) >= 20:
        raise HTTPException(status_code=429, detail="Zu viele Fehlversuche – bitte 10 Minuten warten")


def person(request: Request, authorization: str = Header(default="")):
    ip = client_ip(request)
    _throttle(ip)
    key = authorization[7:] if authorization.startswith("Bearer ") else ""
    p = people.authenticate(key)
    if not p:
        _fails.setdefault(ip, []).append(time.time())
        raise HTTPException(status_code=401, detail="unauthorized")
    return p


def can_edit(p=Depends(person)):
    if p["role"] not in ("owner", "full"):
        raise HTTPException(status_code=403, detail="Als Gast nicht erlaubt")
    return p


def owner(p=Depends(person)):
    if p["role"] != "owner":
        raise HTTPException(status_code=403, detail="Nur der Besitzer darf das")
    return p


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def local_url():
    return hub_config.get("local_url") or f"http://{lan_ip()}:{PORT}"


def home_info():
    return {"name": hub_config.get("home_name"), "local": local_url(), "remote": hub_config.public_url(),
            "backend": hub_config.get("backend"), "version": VERSION}


PERMS = {
    "owner": {"edit": True, "people": True, "restricted": True, "climate": True, "update": True},
    "full": {"edit": True, "people": False, "restricted": True, "climate": True, "update": True},
    "guest": {"edit": False, "people": False, "restricted": False, "climate": False, "update": False},
}


# ---------------------------------------------------------------- Allgemein
@app.get("/api/health")
def health():
    return {"ok": True, "hub": "liesenberg-home", "version": VERSION}


@app.get("/api/me")
def me(p=Depends(person)):
    return {"person": p, "permissions": PERMS[p["role"]], "home": home_info()}


class JoinRequest(BaseModel):
    code: str
    device: str | None = None


@app.post("/api/join")
def join(body: JoinRequest, request: Request):
    ip = client_ip(request)
    _throttle(ip)
    key, p = people.claim(body.code)
    if not key:
        _fails.setdefault(ip, []).append(time.time())
        raise HTTPException(status_code=404, detail="Einladung ungültig oder abgelaufen")
    return {"key": key, "person": p, "permissions": PERMS[p["role"]], "home": home_info()}


# ---------------------------------------------------------------- Haus & Geräte
def _visible(s, p):
    """Gäste sehen Tore/Türen gar nicht erst (Heizung: nur anschauen, nicht ändern)."""
    if PERMS[p["role"]]["restricted"]:
        return s
    for fl in s["floors"]:
        for r in fl["rooms"]:
            r["devices"] = [d for d in r["devices"] if not d.get("restricted")]
    return s


@app.get("/api/home")
def home_structure(p=Depends(person)):
    s = _visible(structure(), p)
    s["name"] = hub_config.get("home_name")
    return s


@app.get("/api/home/state")
def home_state(ids: str = Query(...), p=Depends(person)):
    wanted = [i for i in ids.split(",") if i][:300]
    try:
        return need_home().states(wanted)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Geräte nicht erreichbar: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/home/device/{did:path}")
async def home_command(did: str, request: Request, p=Depends(person)):
    cmd = await request.json()
    d = devices.device_index(structure()).get(did)
    if not d:
        raise HTTPException(status_code=404, detail="Gerät unbekannt")
    if d.get("restricted") and not PERMS[p["role"]]["restricted"]:
        raise HTTPException(status_code=403, detail="Als Gast nicht erlaubt")
    if d["kind"] == "heating" and not PERMS[p["role"]]["climate"]:
        raise HTTPException(status_code=403, detail="Als Gast nicht erlaubt")
    try:
        need_home().command(did, cmd)
    except KeyError:
        raise HTTPException(status_code=404, detail="Gerät unbekannt")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    print(f"[HAUS] {p['name']}: {d['name']} {cmd}", flush=True)
    return {"ok": True}


@app.post("/api/home/central-off")
def central_off(p=Depends(person)):
    try:
        need_home().central_off()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True}


# ---------------------------------------------------------------- Personen & Einladungen
def invite_link(code):
    q = {"c": code, "n": hub_config.get("home_name"), "l": local_url()}
    if hub_config.public_url():
        q["r"] = hub_config.public_url()
    return "liesenberghome://join?" + urllib.parse.urlencode(q)


class InviteRequest(BaseModel):
    name: str
    role: str = "guest"


@app.get("/api/people")
def people_list(p=Depends(owner)):
    return {"people": people.list_people()}


@app.post("/api/people")
def people_invite(body: InviteRequest, p=Depends(owner)):
    try:
        code, pid = people.create_invite(body.name, body.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": pid, "link": invite_link(code), "expiresInHours": people.INVITE_TTL // 3600}


class RoleChange(BaseModel):
    role: str


@app.put("/api/people/{pid}")
def people_role(pid: str, body: RoleChange, p=Depends(owner)):
    try:
        people.set_role(pid, body.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/api/people/{pid}")
def people_remove(pid: str, p=Depends(owner)):
    if pid == p["id"]:
        raise HTTPException(status_code=400, detail="Du kannst dich nicht selbst entfernen")
    people.remove(pid)
    push.remove_person(pid)
    return {"ok": True}


# ---------------------------------------------------------------- Energie (evcc)
@app.get("/api/energy")
def energy(p=Depends(person)):
    url = hub_config.get("evcc.url")
    if not url:
        raise HTTPException(status_code=409, detail="evcc ist nicht eingerichtet")
    try:
        r = requests.get(f"{url.rstrip('/')}/api/state", timeout=5)
        r.raise_for_status()
        state = r.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"evcc nicht erreichbar: {e}")
    # Jede PV-Quelle bekommt ihr Gebäude ("house" | "other"), eingestellt auf der Einrichtungsseite – Standard Haus
    sites = hub_config.get("evcc.sites", {}) or {}
    body = state.get("result", state) if isinstance(state, dict) else state
    for src in (body.get("pv") or []) if isinstance(body, dict) else []:
        if isinstance(src, dict):
            src["site"] = "other" if sites.get(src.get("title") or "") == "other" else "house"
    return state


# ---------------------------------------------------------------- Hub-Version & Update (die App zeigt „Update verfügbar")
import updater as _updater  # noqa: E402

updater = None          # wird nach dem Anlegen von push gestartet (unten)


def _vtuple(v):
    try:
        return tuple(int(x) for x in (v or "0").split("."))
    except ValueError:
        return (0,)


@app.get("/api/hub/version")
def hub_version(p=Depends(person)):
    """Installierte Version und – falls ein neueres, SIGNIERTES Paket bereitliegt – dessen Version."""
    u = updater
    latest = u.available() if u else None
    return {"installed": VERSION, "latest": latest or VERSION, "updateAvailable": bool(latest),
            "mode": hub_config.get("update.mode") or "ask", "last": u.last_result() if u else None}


@app.post("/api/hub/update")
def hub_update(p=Depends(can_edit)):   # Besitzer + Vollzugriff, Gäste nicht
    """Signiertes Paket installieren: Sicherung → Installation → Gesundheitscheck → sonst automatisch zurück."""
    if updater is None:
        raise HTTPException(status_code=503, detail="Updater startet noch")
    try:
        out = updater.install()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    print(f"[UPDATE] {p['name']} hat das Update gestartet: {out[-80:]}", flush=True)
    return {"ok": True, "message": "Update läuft – der Hub startet gleich neu."}


# ---------------------------------------------------------------- Sicherung (nur Besitzer, z. B. automatisch durch die App)
@app.get("/api/backup")
def backup(p=Depends(owner)):
    """Komplette Sicherung als .tgz – enthält Zugangsdaten, darum nur für den Besitzer."""
    from fastapi.responses import Response
    rc, data, err = setup_web._hubctl_bytes("backup")
    if rc != 0 or len(data) < 100:
        raise HTTPException(status_code=502, detail=f"Sicherung fehlgeschlagen: {err[-200:] or rc}")
    name = "".join(ch for ch in (hub_config.get("home_name") or "hub") if ch.isalnum()) or "hub"
    fn = f"hub-sicherung-{name}-{time.strftime('%Y-%m-%d')}.tgz"
    print(f"[SICHERUNG] {p['name']} hat eine Sicherung geladen ({len(data) // 1024} KB)", flush=True)
    return Response(data, media_type="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"', "Cache-Control": "no-store"})


# ---------------------------------------------------------------- Energie-Verlauf (der Hub zeichnet jede Minute auf)
import energy_log as _elog  # noqa: E402

energy_history = _elog.EnergyLog(lambda: hub_config.get("evcc.url"))


@app.get("/api/energy/history")
def energy_day(date: str = "", p=Depends(person)):
    """Ein Tag (JJJJ-MM-TT, leer = heute): Minutenwerte + Summen in kWh."""
    try:
        d = _elog.parse_day(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Datum bitte als JJJJ-MM-TT")
    return energy_history.day(d)


def _prices():
    pr = hub_config.get("prices", {}) or {}
    return {k: (float(pr[k]) if isinstance(pr.get(k), (int, float)) else None) for k in ("import", "export", "base_month")}


@app.get("/api/prices")
def prices_get(p=Depends(can_edit)):   # Kosten/Preise: Besitzer + Vollzugriff, nicht für Gäste
    return _prices()


@app.put("/api/prices")
async def prices_put(request: Request, p=Depends(can_edit)):
    """Strompreise fürs ganze Haus (Besitzer + Vollzugriff). Leere Werte = nicht gesetzt."""
    body = await request.json()
    new = {}
    for k in ("import", "export", "base_month"):
        v = body.get(k)
        if v in (None, ""):
            new[k] = None
            continue
        try:
            v = float(str(v).replace(",", "."))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Ungültiger Preis: {k}")
        if not 0 <= v <= (500 if k == "base_month" else 5):
            raise HTTPException(status_code=400, detail=f"Preis außerhalb des Bereichs: {k}")
        new[k] = v
    hub_config.update({"prices": new})
    return _prices()


@app.get("/api/energy/summary")
def energy_summary(period: str = "day", date: str = "", p=Depends(can_edit)):
    """Kostenübersicht Tag/Woche/Monat (kWh + Euro nach den eingetragenen Preisen)."""
    try:
        d = _elog.parse_day(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Datum bitte als JJJJ-MM-TT")
    return energy_history.summary(period, d, _prices())


import planner as _planner  # noqa: E402
import report as _report  # noqa: E402


@app.get("/api/energy/report")
def energy_report(month: str = "", p=Depends(can_edit)):
    """Monatsbericht (JJJJ-MM, leer = aktueller Monat) mit Vormonat und Vorjahresmonat."""
    if month and not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(status_code=400, detail="Monat bitte als JJJJ-MM")
    return _report.month_report(energy_history, _prices(), month or None)


@app.get("/api/energy/year")
def energy_year(year: int = 0, p=Depends(can_edit)):
    """Jahresrückblick (Monate, Summen, Rekorde)."""
    return _report.year_report(energy_history, _prices(), year or None)


@app.get("/api/energy/plan")
def energy_plan(p=Depends(person)):
    """Energie-Planer: beste Startzeiten für Geräte und Auto (Sonne, Preis, Grundverbrauch)."""
    extra = [_planner.VACUUM] if globals().get("vacuum") is not None and vacuum.configured and vacuum.status else []
    r = _planner.plan(_evcc_state(), energy_history, _prices(), extra=extra)
    for rec in r.get("recommendations", []):
        if rec["id"] == "vacuum":
            # Vorschlag nur – nie automatisch starten. App bietet „Erinnern", „Jetzt starten" (mit Rückfrage)
            # und „Als Zeitplan übernehmen" (Option „nur wenn niemand zuhause").
            rec["note"] = "Verbrauch geschätzt ≈ 0,5 kWh (Laden, Mopp waschen, Heißluft-Trocknen)"
            rec["someoneHome"] = someone_home()
            rec["devices"] = [{"id": d, "name": st.get("name")} for d, st in vacuum.status.items()]
    return r


def _evcc_state():
    url = hub_config.get("evcc.url")
    if not url:
        raise HTTPException(status_code=409, detail="evcc ist nicht eingerichtet")
    try:
        r = requests.get(f"{url.rstrip('/')}/api/state", timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"evcc nicht erreichbar: {e}")


def _best_window(profile, deadline=None):
    recs = _planner.plan(_evcc_state(), energy_history, _prices(), only=profile, deadline=deadline)["recommendations"]
    if not recs:
        raise HTTPException(status_code=400, detail="Kein passendes Zeitfenster – „fertig bis“ liegt zu früh")
    return recs[0]


import smartstart as _smartstart  # noqa: E402


@app.get("/api/smartstart")
def smartstart_list(p=Depends(person)):
    """Wasch-/Spül-/Trockenmaschinen aus Home Connect mit geplantem Auto-Start."""
    return {"devices": smartstart.devices()}


class SmartStartRequest(BaseModel):
    deadline: float | None = None    # Unix-Zeit „fertig bis" (optional)


@app.post("/api/smartstart/{ha_id}")
def smartstart_schedule(ha_id: str, body: SmartStartRequest, p=Depends(person)):   # alle Personen (z. B. Haushaltshilfe)
    try:
        return smartstart.schedule(ha_id, body.deadline, by=p["name"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/smartstart/{ha_id}")
def smartstart_cancel(ha_id: str, p=Depends(person)):
    smartstart.cancel(ha_id)
    return {"ok": True}


@app.get("/api/energy/days")
def energy_days(limit: int = 31, p=Depends(person)):
    """Tageswerte (kWh) der letzten Tage – neuester zuerst."""
    return {"days": energy_history.days(max(1, min(limit, 400)))}


# ---------------------------------------------------------------- Daikin (Onecta)
import daikin as _daikin  # noqa: E402


def _room_names():
    try:
        return [r["name"] for f in structure()["floors"] for r in f["rooms"]]
    except HTTPException:
        return []


daikin = _daikin.Daikin(room_names=_room_names)


@app.get("/api/daikin")
def daikin_units(p=Depends(person)):
    return {"configured": daikin.configured, "loggedIn": daikin.auth.ready,
            "updated": daikin.updated, "error": daikin.error, "units": daikin.units}


class ClimateCommand(BaseModel):
    on: bool | None = None
    mode: str | None = None
    target: float | None = None


@app.put("/api/daikin/{unit_id}")
def daikin_set(unit_id: str, cmd: ClimateCommand, p=Depends(person)):
    if not PERMS[p["role"]]["climate"]:
        raise HTTPException(status_code=403, detail="Als Gast nicht erlaubt")
    try:
        return daikin.set(unit_id, on=cmd.on, mode=cmd.mode, target=cmd.target)
    except KeyError:
        raise HTTPException(status_code=404, detail="Gerät unbekannt")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/daikin/callback")
def daikin_callback(code: str = "", state: str = ""):
    """Ziel der Daikin-Anmeldung (über die öffentliche Hub-Adresse)."""
    from fastapi.responses import HTMLResponse
    if not code or state not in _daikin.PENDING_STATES:
        raise HTTPException(status_code=400, detail="Anmeldung bitte über die Einrichtungsseite starten")
    _daikin.PENDING_STATES.pop(state, None)
    try:
        daikin.auth.exchange_code(code)
        daikin._wake.set()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return HTMLResponse("<meta name=viewport content='width=device-width'><body style='font-family:-apple-system;"
                        "background:#0a0b0d;color:#eee;padding:40px'><h2>Daikin verbunden ✓</h2>"
                        "<p>Du kannst dieses Fenster schließen.</p>")


# ---------------------------------------------------------------- Home Connect (Bosch/Siemens)
import homeconnect as _hc  # noqa: E402

homeconnect = _hc.HomeConnect()
smartstart = _smartstart.SmartStart(homeconnect, _best_window)


def _surplus_w():
    """Überschuss jetzt in W: Einspeisung + Akku-Laden (evcc)"""
    s = _evcc_state()
    s = s.get("result", s)
    grid = s.get("grid") if isinstance(s.get("grid"), dict) else {}
    bat = s.get("battery") if isinstance(s.get("battery"), dict) else {}
    g = grid.get("power", s.get("gridPower")) or 0
    b = bat.get("power", s.get("batteryPower")) or 0
    return max(-g, 0) + max(-b, 0)


smartstart.surplus_fn = _surplus_w   # Auto-Start über Home Connect


homeconnect.on_event = lambda ha, title, body: push.send("washer", f"{_smartstart.label(ha)}: {title}", body) \
    if globals().get("push") is not None and push.configured else None


@app.get("/api/hc/{ha_id}")
def hc_detail(ha_id: str, fresh: bool = False, p=Depends(person)):
    try:
        return homeconnect.detail(ha_id, fresh=fresh)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/hc/{ha_id}/program/{key}")
def hc_program(ha_id: str, key: str, p=Depends(person)):
    try:
        return homeconnect.program_options(ha_id, key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class HCAction(BaseModel):
    action: str                     # start | stop | option | power
    key: str | None = None          # Programm- bzw. Options-Schlüssel
    value: float | int | str | bool | None = None
    options: dict | None = None     # beim Start: {Optionsschlüssel: Wert}


@app.post("/api/hc/{ha_id}/action")
def hc_action(ha_id: str, body: HCAction, p=Depends(can_edit)):
    try:
        if body.action == "start":
            homeconnect.start(ha_id, body.key, body.options)
        elif body.action == "stop":
            homeconnect.stop(ha_id)
        elif body.action == "option":
            homeconnect.set_option(ha_id, body.key, body.value)
        elif body.action == "power":
            homeconnect.set_power(ha_id, str(body.value))
        else:
            raise HTTPException(status_code=400, detail="Unbekannte Aktion")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    print(f"[HAUSGERÄTE] {p['name']}: {body.action} {body.key or ''} {body.value or ''}", flush=True)
    return {"ok": True}


# ---------------------------------------------------------------- Miele@home (offizielle 3rd-Party-API)
import miele as _miele  # noqa: E402

miele = _miele.Miele()          # Mitteilungen werden weiter unten angebunden (push entsteht später)
smartstart.miele = miele        # Miele-Spüler/-Waschmaschinen auch in „Energie sparen"
_miele.LOCAL_URL_FN = lambda: local_url()


@app.get("/miele/callback")
def miele_callback(code: str = "", state: str = ""):
    """Ziel der Miele-Anmeldung (Redirect-URI aus dem Miele-Entwicklerportal)."""
    from fastapi.responses import HTMLResponse
    if not code or state not in _miele.PENDING:
        raise HTTPException(status_code=400, detail="Anmeldung bitte über die Einrichtungsseite starten")
    _miele.PENDING.pop(state, None)
    try:
        miele.auth.exchange_code(code)
        miele._wake.set()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Miele: {e}")
    return HTMLResponse("<meta name=viewport content='width=device-width'><body style='font-family:-apple-system;"
                        "background:#0a0b0d;color:#eee;padding:40px'><h2>Miele verbunden ✓</h2>"
                        "<p>Du kannst dieses Fenster schließen.</p>")


class MieleAction(BaseModel):
    action: str        # start | stop | pause | start_time (Min.) | light | power_on | power_off | target_temp | program | super…
    value: dict | int | float | bool | str | None = None


@app.get("/api/miele/{serial}")
def miele_detail(serial: str, p=Depends(person)):
    try:
        return miele.detail(serial)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Miele: {e}")


@app.post("/api/miele/{serial}/action")
def miele_action(serial: str, body: MieleAction, p=Depends(can_edit)):
    try:
        miele.action(serial, body.action, body.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Miele: {e}")
    print(f"[MIELE] {p['name']}: {body.action} {serial}", flush=True)
    return {"ok": True}


@app.get("/api/appliances")
def appliances(p=Depends(person)):
    # Home Connect (Bosch/Siemens) + Miele in einer Liste – die App zeigt beide gleich an
    lg_ = globals().get("lg")
    errors = [e for e in (homeconnect.error, miele.error, lg_.error if lg_ else None) if e]
    return {"configured": homeconnect.configured or miele.configured or bool(lg_ and lg_.configured),
            "loggedIn": homeconnect.auth.ready or miele.auth.ready or bool(lg_ and lg_.configured),
            "updated": max(homeconnect.updated or 0, miele.updated or 0, (lg_.updated if lg_ else 0) or 0),
            "error": " · ".join(errors) or None,
            "appliances": (homeconnect.list() if homeconnect.auth.ready else []) + miele.list() + (lg_.list() if lg_ else [])}


# ---------------------------------------------------------------- Musik (HomePods / AirPlay)
try:
    import music as _music
    music = _music.Music()
    _music_error = None
except Exception as _e:   # pyatv fehlt o. Ä. – Rest des Hubs läuft trotzdem
    music = None
    _music_error = str(_e)


def _need_music():
    if music is None:
        raise HTTPException(status_code=503, detail=f"Musik nicht verfügbar: {_music_error}")
    return music


@app.get("/api/music")
def music_state(p=Depends(person)):
    m = _need_music()
    return {"speakers": m.list(), "schedules": m.schedules, "radio": list(_music.RADIO), "error": m.error}


@app.post("/api/music/rescan")
def music_rescan(p=Depends(person)):
    _need_music().rescan()
    return {"ok": True}


class MusicCommand(BaseModel):
    action: str                     # play | pause | toggle | next | previous | volume | radio
    value: str | float | None = None


@app.post("/api/music/speaker/{speaker_id}")
def music_command(speaker_id: str, cmd: MusicCommand, p=Depends(person)):
    try:
        _need_music().command(speaker_id, cmd.action, cmd.value)
    except KeyError:
        raise HTTPException(status_code=404, detail="Lautsprecher unbekannt")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True}


class MusicSchedule(BaseModel):
    id: str | None = None
    name: str = ""
    time: str                       # "06:45"
    days: list[int] = [0, 1, 2, 3, 4, 5, 6]   # 0 = Montag
    devices: list[str]
    action: str = "radio"           # radio | play | pause
    station: str | None = None
    volume: float | None = None     # 0–100
    enabled: bool = True


@app.put("/api/music/schedules")
def music_schedule_save(s: MusicSchedule, p=Depends(can_edit)):
    return _need_music().upsert_schedule(s.model_dump())


@app.delete("/api/music/schedules/{sid}")
def music_schedule_delete(sid: str, p=Depends(can_edit)):
    _need_music().delete_schedule(sid)
    return {"ok": True}


# ---------------------------------------------------------------- Szenen
import scenes as _scenes  # noqa: E402

scenes = _scenes.Scenes(home)


@app.get("/api/scenes")
def scenes_list(p=Depends(person)):
    return {"scenes": scenes.list()}


@app.put("/api/scenes")
async def scenes_save(request: Request, p=Depends(can_edit)):
    body = await request.json()
    if not isinstance(body, dict) or not body.get("name"):
        raise HTTPException(status_code=400, detail="Szene braucht einen Namen")
    return scenes.upsert(body)


@app.delete("/api/scenes/{sid}")
def scenes_delete(sid: str, p=Depends(can_edit)):
    scenes.delete(sid)
    return {"ok": True}


@app.post("/api/scenes/{sid}/run")
def scenes_run(sid: str, phase: str = "start", p=Depends(person)):
    if phase not in ("start", "stop"):
        raise HTTPException(status_code=400, detail="phase = start oder stop")
    try:
        return scenes.run(sid, phase, allow_gates=PERMS[p["role"]]["restricted"])   # Gäste lösen keine Tore aus
    except KeyError:
        raise HTTPException(status_code=404, detail="Szene nicht gefunden")


# ---------------------------------------------------------------- Ältere App-Versionen (LUXOR direkt)
@app.get("/api/luxor/project")
def luxor_project(p=Depends(can_edit)):
    h = need_home()
    if not isinstance(h, devices.LuxorBackend):
        raise HTTPException(status_code=409, detail="Nur mit LUXORliving")
    try:
        data = h.project()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Keine .lxp-Datei")
    import copy
    data = copy.deepcopy(data)
    names = devices.room_names()
    for fl in data["floors"]:
        for r in fl["rooms"]:
            r["name"] = names.get(r["name"], r["name"])
    return data


@app.get("/api/luxor/values")
def luxor_values(ids: str = Query(...), p=Depends(can_edit)):
    try:
        dps = sorted({int(x) for x in ids.split(",") if x.strip()})[:200]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids müssen Zahlen sein")
    results = [luxor.get(devices.lxp_rest(dp)) if devices.lxp_rest(dp) else None for dp in dps]
    return {str(dp): v for dp, v in zip(dps, results) if v is not None}


class WriteValue(BaseModel):
    id: int
    value: bool | int | float


@app.put("/api/luxor/value")
def luxor_write(body: WriteValue, p=Depends(can_edit)):
    dp = devices.lxp_rest(body.id)
    if dp is None:
        raise HTTPException(status_code=400, detail=f"Datenpunkt {body.id} gibt es im IP1 nicht")
    if not luxor.set(dp, body.value):
        raise HTTPException(status_code=502, detail=f"LUXORliving hat den Befehl abgelehnt ({luxor.last_write})")
    return {"ok": True}


@app.post("/api/luxor/central-off")
def luxor_central_off(p=Depends(person)):
    return central_off(p)


# ---------------------------------------------------------------- Einrichtungsseite
import setup_web  # noqa: E402

# ---------------------------------------------------------------- Mitteilungen (Apple Push) + Wächter
import push as _push  # noqa: E402
import notifier as _notifier  # noqa: E402

push = _push.Push(lambda: hub_config.get("push") or {})
miele.push = push
updater = _updater.Updater(lambda: hub_config.get("update.mode"), push, VERSION)

import lgthinq as _lgthinq  # noqa: E402

lg = _lgthinq.LGThinQ(lambda: hub_config.get("lgthinq"), lambda ch: hub_config.update({"lgthinq": ch}), push)
smartstart.lg = lg              # Auto-Start auch für LG (nur nach Bestätigung in der App)

notifier = _notifier.Notifier(push, homeconnect, smartstart, lambda: hub_config.get("evcc.url"),
                              lambda period, d: energy_history.summary(period, d, _prices()))
notifier.report_fn = lambda month: _report.month_report(energy_history, _prices(), month)
notifier.plan_fn = lambda: _planner.plan(_evcc_state(), energy_history, _prices(),
                                         extra=[_planner.VACUUM] if globals().get("vacuum") is not None and vacuum.configured and vacuum.status else [])


def _all_apps():
    out = list(homeconnect.list()) if homeconnect.auth.ready else []
    try:
        out += miele.list()
    except Exception:
        pass
    out += lg.list()
    return out


notifier.apps_fn = _all_apps


class PushRegister(BaseModel):
    token: str
    env: str = "production"
    prefs: dict | None = None


@app.post("/api/push/register")
def push_register(body: PushRegister, p=Depends(person)):
    """App meldet ihr Geräte-Token an (bei jedem Start) – Mitteilungen gehen an dieses iPhone."""
    try:
        d = push.register(p, body.token, body.env, body.prefs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"configured": push.configured, "prefs": d["prefs"]}


@app.put("/api/push/prefs")
def push_prefs(body: PushRegister, p=Depends(person)):
    try:
        d = push.set_prefs(p, body.token, body.prefs or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"configured": push.configured, "prefs": d["prefs"]}


@app.get("/api/push")
def push_status(p=Depends(person)):
    return {"configured": push.configured, "error": push.last_error}


# ---------------------------------------------------------------- Wetterstation (KNX über das LUXOR IP1)
# Das IP1 legt die Wetterstation auf feste REST-Datenpunkte 1–12 (geprüft am IP1 mit „Wetterstation 1“).
_WEATHER_DP = {"wind": 1, "windAlarm1": 2, "windAlarm2": 3, "temperature": 4, "frost": 5, "rain": 6,
               "lux1": 7, "lux2": 8, "lux3": 9, "lux4": 10, "dusk1": 11, "dusk2": 12}
_weather = {"at": 0.0, "data": None}


@app.get("/api/weather")
def weather(p=Depends(person)):
    """Außentemperatur, Wind, Regen, Helligkeit der KNX-Wetterstation (30 s zwischengespeichert)."""
    if hub_config.get("backend") != "luxor":
        return {"available": False}
    if time.time() - _weather["at"] > 30 or _weather["data"] is None:
        vals = {}
        for k, dp in _WEATHER_DP.items():
            try:
                vals[k] = luxor.get(dp)
            except Exception:
                vals[k] = None
        if vals.get("temperature") is None and vals.get("wind") is None:
            return {"available": False}
        wind_kn = float(vals["wind"]) if isinstance(vals.get("wind"), (int, float)) else None
        lux = [v for v in (vals.get("lux1"), vals.get("lux2"), vals.get("lux3"), vals.get("lux4")) if isinstance(v, (int, float))]
        _weather["data"] = {
            "available": True,
            "temperature": vals.get("temperature"),
            # Die Wetterstation liefert Knoten (KNX-App zeigt „kn“, Abgleich mit Open-Meteo bestätigt das).
            # "wind" bleibt für ältere Apps in m/s, dazu Knoten und km/h direkt.
            "wind": round(wind_kn * 0.514444, 2) if wind_kn is not None else None,
            "windKnots": round(wind_kn, 1) if wind_kn is not None else None,
            "windKmh": round(wind_kn * 1.852, 1) if wind_kn is not None else None,
            "windAlarm": bool(vals.get("windAlarm1") or vals.get("windAlarm2")),
            "rain": bool(vals.get("rain")),
            "frost": bool(vals.get("frost")),
            "lux": max(lux) if lux else None,               # hellste Seite
            "luxSides": lux,
            "dusk": bool(vals.get("dusk1") or vals.get("dusk2")),
        }
        _weather["at"] = time.time()
    return _weather["data"]


# ---------------------------------------------------------------- Wetter-Vorhersage, Regen-Nowcast, DWD-Warnungen
import forecast as _forecast  # noqa: E402

forecast = _forecast.Forecast(lambda: hub_config.get("location"), push)


@app.get("/api/weather/forecast")
def weather_forecast(p=Depends(person)):
    """Vorhersage (48 h stündlich, 7 Tage, Regen je 15 min), Gewitter-Risiko und aktive DWD-Warnungen."""
    return forecast.forecast()


@app.get("/api/weather/precip")
def weather_precip(p=Depends(person)):
    """Niederschlags-Raster (ICON, stündlich 48 h) – Verlängerung des Regenradars."""
    try:
        return forecast.precip_grid()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Regenvorhersage: {e}")


import geo_ref as _geo_ref  # noqa: E402


def _weather_grids_warm():
    """Wind-, Regen-Raster und Kartenlinien im Hintergrund aktuell halten – die App muss nie 20 s warten."""
    time.sleep(45)
    while True:
        for fn in (forecast.wind_grid, forecast.precip_grid):
            try:
                fn()
            except Exception as e:
                print(f"[WETTER] Raster: {e}", flush=True)
        try:
            loc = hub_config.get("location") or {}
            if loc.get("lat") is not None:
                _geo_ref.lines(float(loc["lat"]), float(loc["lon"]))
        except Exception as e:
            print(f"[WETTER] Karte: {e}", flush=True)
        time.sleep(3 * 3600 + 60)


threading.Thread(target=_weather_grids_warm, daemon=True).start()


@app.get("/api/weather/geo")
def weather_geo(p=Depends(person)):
    """Flüsse, Seen und Grenzen um das Haus (Natural Earth) für Radar- und Windkarte."""
    loc = hub_config.get("location") or {}
    lat, lon = loc.get("lat"), loc.get("lon")
    if lat is None:
        raise HTTPException(status_code=400, detail="Standort fehlt")
    return _geo_ref.lines(float(lat), float(lon))


@app.get("/api/weather/wind")
def weather_wind(p=Depends(person)):
    """Windkarte: Raster um das Haus (Knoten), 48 h stündlich."""
    try:
        return forecast.wind_grid()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Windkarte: {e}")


@app.get("/api/weather/alerts")
def weather_alerts(p=Depends(person)):
    return {"alerts": forecast.alerts()}


# ---------------------------------------------------------------- Verbraucher (gelernt aus Schaltvorgängen)
import consumers as _consumers  # noqa: E402

consumers = _consumers.Consumers(lambda: hub_config.get("evcc.url"), energy_history)


def _all_lights():
    h = home()
    if h is None:
        return {}
    return {did: d for did, d in devices.device_index(structure()).items() if d.get("kind") in ("light", "dimmer")}


def _light_states(ids):
    h = home()
    return h.states(ids) if h is not None else {}


consumers.watch(_all_lights, _light_states)     # Schalten am Wandtaster (LUXOR/IP1/KNX) mitlernen


@app.get("/api/consumers")
def consumers_report(p=Depends(person)):
    """Grundlast, gelernter Verbrauch je Licht und was davon gerade an ist."""
    ids = list(consumers.data.keys())
    states, home_now = {}, None
    try:
        if ids:
            states = need_home().states(ids)
    except Exception:
        pass
    if consumers.buf:
        home_now = consumers.buf[-1][1]
    rep = consumers.report(states, home_now)
    _add_appliance_consumers(rep)
    return rep


def _add_appliance_consumers(rep):
    """Miele-Geräte mit Verbrauchsmessung (ecoFeedback): laufendes Programm = kWh bisher, mittlere Leistung."""
    try:
        apps = miele.list()
    except Exception:
        return
    for a in apps:
        kwh = a.get("energyKWh")
        if not isinstance(kwh, (int, float)) or a.get("type") is None:
            continue
        running = a.get("state") == "Run"
        el = a.get("elapsed") or 0
        avg = kwh * 1000 / (el / 3600) if running and el >= 300 and kwh > 0 else 0
        now = round(avg) if running else 0
        rep["devices"].append({"id": a["id"], "name": _smartstart.label(a), "kind": "appliance", "brand": "Miele",
                               "watts": round(avg), "samples": 0, "on": running, "nowWatts": now,
                               "programKWh": round(kwh, 2), "waterL": a.get("waterL")})
        rep["knownNow"] = round((rep.get("knownNow") or 0) + now)
        if rep.get("rest") is not None:
            rep["rest"] = round(rep["rest"] - now)
    rep["devices"].sort(key=lambda x: -(x.get("watts") or 0))


# ---------------------------------------------------------------- Auto laden (evcc-Ladepunkte steuern)
class CarMode(BaseModel):
    mode: str                         # off | now | minpv | pv


class CarPlan(BaseModel):
    soc: int                          # Ziel in %
    time: float                       # Unix-Zeit „fertig bis"


class CarLimit(BaseModel):
    soc: int                          # Ladegrenze in % (evcc „limitSoc“, nur zur Laufzeit – keine Konfiguration)


def _evcc_post(path, method="POST"):
    url = (hub_config.get("evcc.url") or "").rstrip("/")
    if not url:
        raise HTTPException(status_code=409, detail="evcc ist nicht eingerichtet")
    try:
        r = requests.request(method, url + path, timeout=8)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"evcc nicht erreichbar: {e}")
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"evcc: {r.text[:160]}")
    return {"ok": True}


@app.post("/api/car/{lp}/mode")
def car_mode(lp: int, body: CarMode, p=Depends(can_edit)):
    if body.mode not in ("off", "now", "minpv", "pv"):
        raise HTTPException(status_code=400, detail="Modus: off, now, minpv oder pv")
    return _evcc_post(f"/api/loadpoints/{lp + 1}/mode/{body.mode}")


@app.post("/api/car/{lp}/plan")
def car_plan(lp: int, body: CarPlan, p=Depends(can_edit)):
    """Ladeplan „bis Uhrzeit X %“ – evcc lädt dafür in den günstigsten/sonnigsten Stunden."""
    if not 10 <= body.soc <= 100:
        raise HTTPException(status_code=400, detail="Ziel zwischen 10 und 100 %")
    st = _evcc_state()
    st = st.get("result", st)
    lps = st.get("loadpoints") or []
    if lp >= len(lps):
        raise HTTPException(status_code=404, detail="Ladepunkt nicht gefunden")
    ts = datetime.fromtimestamp(body.time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    vehicle = lps[lp].get("vehicleName")
    if vehicle:
        return _evcc_post(f"/api/vehicles/{vehicle}/plan/soc/{body.soc}/{ts}")
    raise HTTPException(status_code=400, detail="Kein Fahrzeug am Ladepunkt erkannt – Fahrzeug in evcc anlegen")


def _loadpoint(lp):
    st = _evcc_state()
    lps = st.get("result", st).get("loadpoints") or []
    if lp >= len(lps):
        raise HTTPException(status_code=404, detail="Ladepunkt nicht gefunden")
    return lps[lp]


@app.delete("/api/car/{lp}/plan")
def car_plan_delete(lp: int, p=Depends(can_edit)):
    """Ladeplan löschen (evcc-Laufzeit-API)"""
    vehicle = _loadpoint(lp).get("vehicleName")
    if not vehicle:
        raise HTTPException(status_code=400, detail="Kein Fahrzeug am Ladepunkt erkannt")
    return _evcc_post(f"/api/vehicles/{vehicle}/plan/soc", method="DELETE")


@app.post("/api/car/{lp}/limit")
def car_limit(lp: int, body: CarLimit, p=Depends(can_edit)):
    """Ladegrenze („bis X % laden“) – evcc-Laufzeit-API, ändert keine evcc-Konfiguration."""
    if not 20 <= body.soc <= 100:
        raise HTTPException(status_code=400, detail="Grenze zwischen 20 und 100 %")
    _loadpoint(lp)
    return _evcc_post(f"/api/loadpoints/{lp + 1}/limitsoc/{body.soc}")


# ---------------------------------------------------------------- Roborock-Saugroboter + Anwesenheit
import roborock_vac as _roborock  # noqa: E402

vacuum = _roborock.Vacuum(push)


@app.get("/api/vacuum")
def vacuum_list(p=Depends(person)):
    return vacuum.list()


class VacuumCommand(BaseModel):
    action: str                     # start | pause | stop | dock | find | rooms | fan | water | wash | empty | dry
    value: dict | str | bool | None = None


@app.post("/api/vacuum/{duid}")
def vacuum_command(duid: str, body: VacuumCommand, p=Depends(person)):
    try:
        vacuum.command(duid, body.action, body.value)
    except KeyError:
        raise HTTPException(status_code=404, detail="Saugroboter unbekannt")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Saugroboter: {e}")
    print(f"[ROBOROCK] {p['name']}: {body.action} {body.value or ''}", flush=True)
    return {"ok": True}


class AutoAway(BaseModel):
    on: bool


# ---------------------------------------------------------------- Wärmepumpe (Luxtronik, nur lesen)
import heatpump as _heatpump  # noqa: E402

heatpump = _heatpump.HeatPump(lambda: hub_config.get("heatpump.host") or "",
                               house_power_fn=lambda: list(consumers.buf))


import sunbuffer as _sunbuffer  # noqa: E402


def _evcc_state_safe():
    try:
        return _evcc_state()
    except HTTPException:
        return {}


sunbuffer = _sunbuffer.SunBuffer(heatpump, _evcc_state_safe, forecast,
                                 lambda d, rising: _scenes._sun_time(d, rising=rising), _prices)


@app.get("/api/heatpump/sunbuffer")
def heatpump_sunbuffer(p=Depends(person)):
    """Sonnenpuffer-Simulation: nur Auswertung, es wird nichts an der Wärmepumpe geschaltet."""
    return sunbuffer.report()


@app.get("/api/heatpump")
def heatpump_state(p=Depends(person)):
    return heatpump.state()


@app.get("/api/heatpump/settings")
def heatpump_settings(p=Depends(person)):
    try:
        return heatpump.settings()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Wärmepumpe: {e}")


class HeatPumpWrite(BaseModel):
    what: str                      # hotWaterTarget | heatingMode | hotWaterMode
    value: float


@app.put("/api/heatpump/settings")
def heatpump_write(body: HeatPumpWrite, p=Depends(can_edit)):
    """Nur Besitzer/Vollzugriff; die App fragt vorher nach."""
    try:
        return heatpump.write(body.what, body.value, by=p["name"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Wärmepumpe: {e}")


@app.get("/api/heatpump/history")
def heatpump_history(hours: int = 24, p=Depends(person)):
    return {"samples": heatpump.hist(max(1, min(hours, 168)))}


# ---------------------------------------------------------------- Live Activities (Sperrbildschirm / Dynamic Island)
import liveactivity as _liveactivity  # noqa: E402

@app.get("/api/lg/{did}")
def lg_detail(did: str, p=Depends(person)):
    try:
        return lg.detail(did)
    except KeyError:
        raise HTTPException(status_code=404, detail="LG-Gerät unbekannt")


class _MieleAndLG:
    """Live-Aktivitäten: Miele- und LG-Geräte zusammen (gleiches Format)"""
    def list(self):
        return miele.list() + lg.list()


liveactivities = _liveactivity.LiveActivities(
    push, lambda: _liveactivity.collect(homeconnect, _MieleAndLG(), vacuum, _evcc_state))


@app.post("/api/liveactivity/register")
def liveactivity_register(body: dict, p=Depends(person)):
    """App meldet Push-to-Start- bzw. Aktualisierungs-Token ihrer Live Activities."""
    return liveactivities.register(body)


@app.get("/api/vacuum/{duid}/map")
def vacuum_map(duid: str, fresh: bool = False, p=Depends(person)):
    """Karte des Saugroboters: PNG (base64), Räume mit Farbe/Rechteck, Roboter- und Stationsposition."""
    try:
        return vacuum.map(duid, fresh=fresh)
    except KeyError:
        raise HTTPException(status_code=404, detail="Saugroboter unbekannt")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Karte: {e}")


class VacuumZones(BaseModel):
    zones: list[list[float]]           # je Zone x0, y0, x1, y1 im Kartenbild (0…1)
    repeat: int = 1
    dryRun: bool = False               # nur umrechnen und anzeigen, nichts senden


@app.post("/api/vacuum/{duid}/zones")
def vacuum_zones(duid: str, body: VacuumZones, p=Depends(person)):
    """Zonen-Reinigung: Rechtecke aus der App in Roboter-Koordinaten umrechnen und starten (App fragt vorher)."""
    try:
        payload = vacuum.zone_payload(duid, body.zones, body.repeat)
        if body.dryRun:
            return {"ok": True, "dryRun": True, "zones": payload}
        vacuum.command(duid, "zone", {"zones": payload})
    except KeyError:
        raise HTTPException(status_code=404, detail="Saugroboter unbekannt")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Saugroboter: {e}")
    print(f"[ROBOROCK] {p['name']}: Zonen {payload}", flush=True)
    return {"ok": True, "zones": payload}


@app.put("/api/vacuum/auto-away")
def vacuum_auto_away(body: AutoAway, p=Depends(can_edit)):
    vacuum.set_auto_away(body.on)
    return {"ok": True, "autoAway": body.on}


# ---------------------------------------------------------------- Mammotion-Mähroboter
import mower as _mower  # noqa: E402

mower = _mower.Mower(push, rain_soon_fn=forecast.rain_soon, storm_fn=forecast.storm_soon)


@app.get("/api/mower")
def mower_list(p=Depends(person)):
    return mower.list()


class MowerCommand(BaseModel):
    action: str                     # start (value = Aufgaben-ID) | pause | resume | dock | cancel | blade (value = mm)
    value: str | int | None = None


@app.get("/api/mower/{name}/map")
def mower_map(name: str, p=Depends(person)):
    try:
        return mower.map(name)
    except KeyError:
        raise HTTPException(status_code=404, detail="Mähroboter unbekannt")


class MapOffset(BaseModel):
    north: float = 0.0              # Meter, + = nach Norden
    east: float = 0.0               # Meter, + = nach Osten
    rotate: float = 0.0             # Grad, + = im Uhrzeigersinn um die Kartenmitte


@app.put("/api/mower/{name}/offset")
def mower_offset(name: str, body: MapOffset, p=Depends(can_edit)):
    try:
        return mower.set_offset(name, body.north, body.east, body.rotate)
    except KeyError:
        raise HTTPException(status_code=404, detail="Mähroboter unbekannt")


@app.post("/api/mower/{name}")
def mower_command(name: str, body: MowerCommand, p=Depends(can_edit)):
    try:
        mower.command(name, body.action, body.value)
    except KeyError:
        raise HTTPException(status_code=404, detail="Mähroboter unbekannt")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mähroboter: {e}")
    print(f"[MAMMOTION] {p['name']}: {body.action} {body.value or ''}", flush=True)
    return {"ok": True}


class RainGuard(BaseModel):
    on: bool


@app.put("/api/mower/rain-guard")
def mower_rain_guard(body: RainGuard, p=Depends(can_edit)):
    mower.set_rain_guard(body.on)
    return {"ok": True, "rainGuard": body.on}


_PRESENCE_FILE = os.path.join(hub_config.DATA_DIR, "presence.json")
try:                                 # person_id → (zuhause?, Zeitpunkt) – meldet die App bei Ankommen/Wegfahren
    with open(_PRESENCE_FILE) as _f:
        _presence = {k: tuple(v) for k, v in json.load(_f).items()}
except (OSError, ValueError):
    _presence = {}


def someone_home():
    """True = jemand zuhause, False = alle weg, None = unbekannt (noch keine Meldungen der App)"""
    recent = [h for h, t in _presence.values() if time.time() - t < 7 * 86400]
    return any(recent) if recent else None


class Presence(BaseModel):
    home: bool
    note: str | None = None      # z. B. „Tor automatisch geöffnet“ / „Nicht automatisch: …“ (fürs Nachvollziehen)
    build: str | None = None


@app.post("/api/presence")
def presence(body: Presence, p=Depends(person)):
    print(f"[ANWESENHEIT] {p['name']}: {'da' if body.home else 'weg'}"
          + (f" – {body.note[:300]}" if body.note else "") + (f" (App {body.build[:20]})" if body.build else ""), flush=True)
    _presence[p["id"]] = (body.home, time.time())
    try:
        with open(_PRESENCE_FILE + ".tmp", "w") as f:
            json.dump(_presence, f)
        os.replace(_PRESENCE_FILE + ".tmp", _PRESENCE_FILE)
    except OSError:
        pass
    all_away = someone_home() is False
    vacuum.on_presence(all_away)
    return {"ok": True, "allAway": all_away}


# ---------------------------------------------------------------- Ankommen: gemeinsame Regeln fürs ganze Haus
# Zufahrt (Richtung/Breite) und „vorher mindestens weg“ gelten für alle iPhones im Haushalt –
# einmal eingestellt (Besitzer/Vollzugriff), alle Apps holen sie sich. Nichts davon löst selbst etwas aus.
_ARRIVAL_FILE = os.path.join(hub_config.DATA_DIR, "arrival-rules.json")


class ArrivalRules(BaseModel):
    approachBearing: float | None = None
    approachWidth: float = 70
    minAwayMeters: float = 500


def _arrival_rules():
    try:
        with open(_ARRIVAL_FILE) as f:
            return ArrivalRules(**json.load(f)).model_dump()
    except (OSError, ValueError, TypeError):
        return ArrivalRules().model_dump()


@app.get("/api/arrival/rules")
def arrival_rules_get(p=Depends(person)):
    return _arrival_rules()


@app.put("/api/arrival/rules")
def arrival_rules_put(body: ArrivalRules, p=Depends(can_edit)):
    r = body.model_dump()
    if r["approachBearing"] is not None:
        r["approachBearing"] = float(r["approachBearing"]) % 360
    r["approachWidth"] = min(360.0, max(20.0, float(r["approachWidth"])))
    r["minAwayMeters"] = min(5000.0, max(100.0, float(r["minAwayMeters"])))
    with open(_ARRIVAL_FILE + ".tmp", "w") as f:
        json.dump(r, f)
    os.replace(_ARRIVAL_FILE + ".tmp", _ARRIVAL_FILE)
    print(f"[ANWESENHEIT] {p['name']}: Zufahrt fürs Haus {r}", flush=True)
    return r


# ---------------------------------------------------------------- Saugroboter-Zeitpläne
import vacuum_schedule as _vacuum_schedule  # noqa: E402

vacuum_schedules = _vacuum_schedule.VacuumSchedules(vacuum, push, someone_home)


@app.get("/api/vacuum/schedules")
def vacuum_schedules_list(p=Depends(person)):
    try:
        app = vacuum.app_timers()
    except Exception:
        app = []
    return {"schedules": vacuum_schedules.list(), "presenceKnown": someone_home() is not None, "appTimers": app}


@app.post("/api/vacuum/schedules")
def vacuum_schedules_save(body: dict, p=Depends(can_edit)):
    try:
        return vacuum_schedules.upsert(body, by=p["name"])
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/vacuum/schedules/{sid}")
def vacuum_schedules_delete(sid: str, p=Depends(can_edit)):
    vacuum_schedules.delete(sid)
    return {"ok": True}


@app.post("/api/vacuum/schedules/{sid}/skip")
def vacuum_schedules_skip(sid: str, p=Depends(person)):
    try:
        return vacuum_schedules.skip_next(sid, by=p["name"])
    except KeyError:
        raise HTTPException(status_code=404, detail="Zeitplan unbekannt")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------- Urlaubsmodus (Lichtsimulation)
import vacation as _vacation  # noqa: E402

vacation = _vacation.Vacation(home, lambda d: _scenes._sun_time(d, rising=False), push)


@app.get("/api/vacation")
def vacation_get(p=Depends(person)):
    return vacation.status()


@app.put("/api/vacation")
async def vacation_put(request: Request, p=Depends(can_edit)):
    try:
        return vacation.set(await request.json(), by=p.get("name", ""))
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------- Wartung (Erinnerungen)
import maintenance as _maintenance  # noqa: E402

maintenance = _maintenance.Maintenance(push, vacuum_fn=lambda: vacuum.status if vacuum.configured else {})


@app.get("/api/maintenance")
def maintenance_list(p=Depends(person)):
    return maintenance.list()


@app.post("/api/maintenance")
async def maintenance_add(request: Request, p=Depends(can_edit)):
    b = await request.json()
    try:
        return maintenance.add(b.get("title"), b.get("days") or 0, b.get("icon"), b.get("last"))
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/maintenance/{iid}")
async def maintenance_update(iid: str, request: Request, p=Depends(can_edit)):
    try:
        return maintenance.update(iid, await request.json())
    except KeyError:
        raise HTTPException(status_code=404, detail="Aufgabe unbekannt")
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/maintenance/{iid}/done")
def maintenance_done(iid: str, p=Depends(person)):
    try:
        return maintenance.done(iid)
    except KeyError:
        raise HTTPException(status_code=404, detail="Aufgabe unbekannt")


@app.delete("/api/maintenance/{iid}")
def maintenance_delete(iid: str, p=Depends(can_edit)):
    try:
        maintenance.delete(iid)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Aufgabe unbekannt")


# ---------------------------------------------------------------- Pico-Firmware (OTA)
PICO_DIR = os.path.join(hub_config.DATA_DIR, "pico")    # z. B. pico/24v/manifest.json + Dateien


@app.get("/pico/{path:path}")
def pico_file(path: str, request: Request):
    """Firmware-Dateien für die Picos – nur im Heimnetz (über den Funnel kommt 127.0.0.1 → 404)."""
    from fastapi.responses import FileResponse
    host = request.client.host if request.client else ""
    if not (host.startswith("192.168.") or host.startswith("10.")):
        raise HTTPException(status_code=404)
    root = os.path.realpath(PICO_DIR)
    full = os.path.realpath(os.path.join(root, path))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        raise HTTPException(status_code=404)
    return FileResponse(full, media_type="application/json" if full.endswith(".json") else "text/plain")


setup_web.attach(app, ctx={"luxor": luxor, "home": home, "daikin": daikin, "homeconnect": homeconnect, "push": push,
                           "vacuum": vacuum, "mower": mower, "miele": miele, "lg": lg, "updater": lambda: updater,
                           "invite_link": invite_link, "local_url": local_url, "version": VERSION,
                           "reset_backend": lambda: _backend.update(key=None)})

