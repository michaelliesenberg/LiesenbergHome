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
  GET  /api/energy · /api/energy/history?date= · /api/energy/days · /api/daikin · /api/appliances · /api/music …
  GET/POST/PUT/DELETE /api/people  – Personen & Einladungen (nur Besitzer)
  GET  /api/backup                 – komplette Sicherung als .tgz (nur Besitzer)
  GET  /api/hub/version · POST /api/hub/update – Version prüfen / aktualisieren (Update nur Besitzer)
Einrichtung im Browser:  http://<hub>:8080/setup  (nur im Heimnetz)
"""
import os
import socket
import ssl
import threading
import time
import urllib.parse

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
        elif kind == "demo":
            obj = devices.DemoBackend()
        else:
            obj = None
        _backend.update(key=key, obj=obj)
    return _backend["obj"]


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
    "owner": {"edit": True, "people": True, "restricted": True, "climate": True},
    "full": {"edit": True, "people": False, "restricted": True, "climate": True},
    "guest": {"edit": False, "people": False, "restricted": False, "climate": False},
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
        return r.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"evcc nicht erreichbar: {e}")


# ---------------------------------------------------------------- Hub-Version & Update (die App zeigt „Update verfügbar")
_latest = {"at": 0.0, "v": None}


def latest_version():
    """Neueste Hub-Version auf GitHub (höchstens einmal pro Stunde nachsehen)."""
    if time.time() - _latest["at"] < 3600:
        return _latest["v"]
    _latest["at"] = time.time()
    try:
        repo = hub_config.get("update.repo", "")
        if "github.com/" in repo:
            raw = repo.replace("github.com", "raw.githubusercontent.com") + f"/{hub_config.get('update.branch', 'main')}/hub/VERSION"
            r = requests.get(raw, timeout=5)
            if r.ok and len(r.text.strip()) < 20:
                _latest["v"] = r.text.strip()
    except requests.RequestException:
        pass
    return _latest["v"]


def _vtuple(v):
    try:
        return tuple(int(x) for x in (v or "0").split("."))
    except ValueError:
        return (0,)


@app.get("/api/hub/version")
def hub_version(p=Depends(person)):
    latest = latest_version()
    return {"installed": VERSION, "latest": latest,
            "updateAvailable": bool(latest) and _vtuple(latest) > _vtuple(VERSION)}


@app.post("/api/hub/update")
def hub_update(p=Depends(owner)):
    """Neueste Version von GitHub holen und neu starten (dauert ca. 30–60 s)."""
    rc, out = setup_web._hubctl("update", timeout=300)
    if rc != 0:
        raise HTTPException(status_code=502, detail=f"Update fehlgeschlagen: {out[-300:]}")
    print(f"[UPDATE] {p['name']} hat den Hub aktualisiert: {out[-80:]}", flush=True)
    _latest["at"] = 0
    return {"ok": True, "message": out[-200:]}


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


@app.get("/api/appliances")
def appliances(p=Depends(person)):
    return {"configured": homeconnect.configured, "loggedIn": homeconnect.auth.ready,
            "updated": homeconnect.updated, "error": homeconnect.error, "appliances": homeconnect.list()}


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
        return scenes.run(sid, phase)
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

setup_web.attach(app, ctx={"luxor": luxor, "home": home, "daikin": daikin, "homeconnect": homeconnect,
                           "invite_link": invite_link, "local_url": local_url, "version": VERSION,
                           "reset_backend": lambda: _backend.update(key=None)})

