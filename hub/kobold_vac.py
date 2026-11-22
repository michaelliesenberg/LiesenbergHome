"""Vorwerk-Kobold-Saugroboter VR7 (MyKobold-App): Status live, Steuerung, Räume – über die Vorwerk-Cloud.

Weg wie die MyKobold-App (nachgebaut nach github.com/TWIX99b/ha-vorwerk-kobold, MIT-Lizenz, © Niklas Boose):
  * Anmeldung: E-Mail der MyKobold-App → Vorwerk schickt einen Code → Code eingeben (Auth0, kein Passwort).
    Gespeichert werden nur id_token + refresh_token (kobold.json, chmod 600, nur auf dem Hub).
  * Roboter, Karten, Räume, Starten:  REST https://orbital.ksecosys.com  (Authorization: Auth0Bearer <id_token>)
  * Status live:  Websocket der „Companion"-Cloud (Phoenix-Kanäle), Anmeldung per POST /api/v1/profile/login.
  * Befehle (Pause, Station, Finden …):  POST /vendors/3/robots/<Seriennummer>/messages {"ability": …}

Der ältere VR300/VR200 läuft über eine andere Vorwerk-Cloud (pybotvac/beehive) und wird hier nicht unterstützt.

Gerät-IDs in der App: „kobold-<Roboter-ID>". Räume = Zonen, die in der MyKobold-App in eine Karte eingezeichnet
wurden (App-IDs 1…n, der Hub merkt sich Karte + Zone dazu).
"""
import asyncio
import base64
import json
import os
import ssl
import threading
import time
import uuid
from datetime import datetime

from hub_config import DATA_DIR

STATE_FILE = os.path.join(DATA_DIR, "kobold.json")
PREFIX = "kobold-"

AUTH = "https://mykobold.eu.auth0.com"
CLIENT_ID = "FPSBig7ePFvAE6q99cEDROM8gYUTygkD"          # MyKobold-App (aus ha-vorwerk-kobold)
ORBITAL = "https://orbital.ksecosys.com"
COMPANION = "https://api-2-prod.companion.kobold.vorwerk.com"
COMPANION_WS = "wss://api-2-prod.companion.kobold.vorwerk.com/api/ws"
VENDOR_ID = 3
UA = "okhttp/5.1.0"
APP_HEADERS = {"Accept-Language": "de-DE", "mobile-app-version": "3.14.0", "mobile-app-build": "40408",
               "mobile-app-os": "android", "mobile-app-os-version": "11", "User-Agent": UA}
ORB_ACCEPT = "application/vnd.neato.orbital-http.v1+json"

CLEANING_ACTIONS = {"cleaning", "fast_mapping", "mapping", "map_creation", "spot_cleaning"}
RETURNING_ACTIONS = {"docking", "returning", "going_home"}
FAN_DE = {"auto": "Auto", "eco": "Eco", "turbo": "Turbo", "pet": "Tierhaare"}
NAV_DE = {"normal": "Normal", "extra_care": "Extra Care"}
ERROR_DE = {
    "brush_stuck": "Bürste blockiert", "bin_full": "Staubbehälter voll", "dustbin_missing": "Staubbehälter fehlt",
    "cleaning_path_blocked": "Weg blockiert", "navigation_path_problems_returning_home": "findet nicht zurück zur Station",
    "bumper_stuck": "Stoßstange klemmt", "wheel_stuck": "Rad blockiert", "robot_stuck": "steckt fest",
    "battery_low": "Akku leer", "picked_up": "wurde hochgehoben", "cliff_sensor": "Absturzsensor verschmutzt",
}
BASE_ERROR_DE = {"bag_full": "Staubbeutel der Station voll", "bag_missing": "Staubbeutel der Station fehlt"}


def _jwt_exp(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return float(json.loads(base64.urlsafe_b64decode(p)).get("exp"))
    except Exception:
        return None


class KoboldError(Exception):
    pass


class AuthError(KoboldError):
    pass


class Kobold:
    def __init__(self, push=None):
        self.push = push
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.session = None
        self.robots = {}           # duid → Roboter-Daten aus der Cloud (id, serial, name, model …)
        self.raw = {}              # duid → letzter Roh-Status (für Befehle: available_commands)
        self.status = {}           # duid → dict für die App (gleiches Format wie roborock_vac)
        self.cleaning = {}         # duid → letzter cleaning_state
        self.error = None
        self._pending = None       # (email, Zeit) zwischen „Code anfordern" und „Code eingeben"
        self._prev = {}
        self._ws = None
        self._ws_task = None
        self._joins = {}
        self._ref = 0
        self.state = self._load()
        if self.configured:
            self._run(self._start(), wait=False)

    # ---------------------------------------------------------- Infrastruktur
    def _run(self, coro, timeout=45, wait=True):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout) if wait else fut

    def _load(self):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = STATE_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, STATE_FILE)

    @property
    def configured(self):
        return bool(self.state.get("refresh_token"))

    def owns(self, duid):
        return str(duid).startswith(PREFIX)

    async def _http(self):
        if self.session is None or self.session.closed:
            import aiohttp
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                ctx = ssl.create_default_context()
            self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx),
                                                 timeout=aiohttp.ClientTimeout(total=30))
        return self.session

    async def _req(self, method, url, headers, body=None, want_resp=False):
        s = await self._http()
        async with s.request(method, url, headers=headers, json=body) as r:
            text = await r.text()
            if r.status == 401:
                raise AuthError(f"HTTP 401: {text[:150]}")
            if r.status >= 400:
                raise KoboldError(f"HTTP {r.status}: {text[:200]}")
            if want_resp:
                return r
            try:
                return json.loads(text) if text else None
            except ValueError:
                return text

    # ---------------------------------------------------------- Anmeldung (E-Mail-Code)
    def request_code(self, email):
        email = (email or "").strip().lower()
        if "@" not in email:
            raise ValueError("Bitte die E-Mail-Adresse der MyKobold-App eingeben")
        self._run(self._req("POST", f"{AUTH}/passwordless/start", {"User-Agent": UA, "Content-Type": "application/json"},
                            {"client_id": CLIENT_ID, "email": email, "send": "code", "connection": "email"}))
        self._pending = (email, time.time())

    def login(self, code):
        if not self._pending or time.time() - self._pending[1] > 15 * 60:
            raise ValueError("Bitte zuerst den Code anfordern (gilt 15 Minuten)")
        email = self._pending[0]
        code = "".join(ch for ch in str(code) if ch.isdigit())
        try:
            tok = self._run(self._req("POST", f"{AUTH}/oauth/token", {"User-Agent": UA, "Content-Type": "application/json"}, {
                "client_id": CLIENT_ID, "grant_type": "http://auth0.com/oauth/grant-type/passwordless/otp",
                "realm": "email", "username": email, "otp": code, "scope": "openid profile email offline_access",
                "platform": "android", "locale": "de", "source": "vorwerk_auth0"}))
        except KoboldError as e:
            raise ValueError(f"Code falsch oder abgelaufen ({e})")
        if not isinstance(tok, dict) or not tok.get("id_token") or not tok.get("refresh_token"):
            raise ValueError("Vorwerk hat kein Anmelde-Token geliefert")
        self.state.update(email=email, id_token=tok["id_token"], refresh_token=tok["refresh_token"])
        self._save()
        self._pending = None
        self._run(self._start(), timeout=60)
        if not self.robots:
            raise ValueError(self.error or "Kein Roboter im MyKobold-Konto gefunden")

    def logout(self):
        self._run(self._stop_ws(), wait=True)
        self.robots, self.status, self.raw, self.cleaning = {}, {}, {}, {}
        keep = {k: self.state[k] for k in ("auto_away",) if k in self.state}
        self.state = keep
        try:
            os.remove(STATE_FILE)
        except OSError:
            pass

    async def _token(self):
        tok = self.state.get("id_token")
        exp = _jwt_exp(tok) if tok else None
        if tok and (exp is None or exp - 300 > time.time()):
            return tok
        return await self._refresh_token()

    async def _refresh_token(self):
        rt = self.state.get("refresh_token")
        if not rt:
            raise AuthError("nicht angemeldet")
        try:
            r = await self._req("POST", f"{AUTH}/oauth/token", {"User-Agent": UA, "Content-Type": "application/json"},
                                {"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": rt})
        except KoboldError as e:
            if "403" in str(e) or "401" in str(e) or "invalid_grant" in str(e):
                self.error = "Vorwerk-Anmeldung abgelaufen – bitte auf der Einrichtungsseite neu anmelden"
                raise AuthError(self.error)
            raise
        self.state["id_token"] = r["id_token"]
        if r.get("refresh_token"):
            self.state["refresh_token"] = r["refresh_token"]
        self._save()
        return r["id_token"]

    async def _orb(self, method, path, body=None, app=False):
        h = {"Authorization": f"Auth0Bearer {await self._token()}", "Accept": ORB_ACCEPT,
             "Content-Type": "application/json", "User-Agent": UA}
        if app:
            h.update(APP_HEADERS)
        try:
            return await self._req(method, ORBITAL + path, h, body)
        except AuthError:
            self.state["id_token"] = None
            h["Authorization"] = f"Auth0Bearer {await self._token()}"
            return await self._req(method, ORBITAL + path, h, body)

    # ---------------------------------------------------------- Roboter, Räume, Live-Verbindung
    async def _start(self):
        try:
            await self._load_robots()
            await self._load_rooms()
            self.error = None if self.robots else "Kein Roboter im MyKobold-Konto gefunden"
        except Exception as e:
            self.error = f"Vorwerk: {e}"
            print(f"[KOBOLD] Start: {e}", flush=True)
        if self.robots and (self._ws_task is None or self._ws_task.done()):
            self._ws_task = asyncio.ensure_future(self._ws_loop())
            asyncio.ensure_future(self._housekeeping())

    async def _load_robots(self):
        found = {}
        for r in await self._orb("GET", "/users/me/robots") or []:
            if isinstance(r, dict) and r.get("id"):
                duid = PREFIX + str(r["id"])
                found[duid] = {"id": str(r["id"]), "serial": str(r.get("serial") or r["id"]),
                               "name": r.get("name") or "Kobold",
                               "model": "Kobold VR7" if r.get("model_name") in ("VRXX", "VR7") else (r.get("model_name") or "Kobold"),
                               "firmware": r.get("firmware")}
        self.robots = found
        for duid, r in found.items():
            st = self.status.setdefault(duid, {"id": duid, "kind": "kobold", "online": False, "stateText": "verbinde …"})
            st.update(name=r["name"], model=r["model"])
            try:
                feats = await self._orb("GET", f"/robots/{r['id']}/features", app=True) or {}
                r["fans"] = [m for m in FAN_DE if m in (feats.get("vacuuming_modes") or [])] or list(FAN_DE)
                r["extra_care"] = bool(feats.get("extra_care_navigation"))
            except Exception as e:
                print(f"[KOBOLD] Funktionen: {e}", flush=True)

    async def _load_rooms(self):
        """Benannte Zonen aller benannten Karten → „Räume" mit laufender Nummer"""
        rooms = self.state.setdefault("rooms", {})
        for duid, r in self.robots.items():
            out, maps = [], []
            try:
                plans = await self._orb("GET", f"/robots/{r['id']}/floorplans?sort_by=promoted_at&sort_order=asc") or []
                for p in plans:
                    if not isinstance(p, dict) or not p.get("floorplan_uuid") or not (p.get("name") or "").strip():
                        continue
                    tracks = await self._orb("GET", f"/maps/floorplans/{p['floorplan_uuid']}/tracks") or []
                    for t in tracks:
                        if isinstance(t, dict) and t.get("track_uuid") and t.get("type") in (None, "cleaning"):
                            out.append({"map": p["floorplan_uuid"], "mapName": p["name"].strip(), "track": t["track_uuid"],
                                        "name": (t.get("name") or "").strip() or "Zone"})
                    maps.append({"uuid": p["floorplan_uuid"], "name": p["name"].strip()})
            except Exception as e:
                print(f"[KOBOLD] Karten: {e}", flush=True)
                continue
            r["maps"] = maps
            multi = len({x["map"] for x in out}) > 1
            for x in out:
                if multi:
                    x["name"] = f'{x["name"]} ({x["mapName"]})'
            rooms[duid] = out
            if duid in self.status:
                self.status[duid]["rooms"] = [{"id": i + 1, "name": x["name"]} for i, x in enumerate(out)]
        self._save()

    async def _companion_auth(self):
        r = await self._req("POST", f"{COMPANION}/api/v1/profile/login",
                            {"Authorization": f"Bearer {await self._token()}", **APP_HEADERS}, want_resp=True)
        a = r.headers.get("Authorization")
        if not a:
            raise KoboldError("Companion-Anmeldung ohne Schlüssel")
        return a

    def _next_ref(self):
        self._ref += 1
        return str(self._ref)

    async def _request_state(self, duid=None):
        if self._ws is None or self._ws.closed:
            return False
        for d in [duid] if duid else list(self.robots):
            ref = self._joins.get(d)
            if ref:
                await self._ws.send_str(json.dumps([ref, self._next_ref(), f"robots:{self.robots[d]['id']}",
                                                    "last_state", {"request_id": str(uuid.uuid4())}]))
        return True

    async def _ws_loop(self):
        import aiohttp
        delay = 2
        while self.configured and self.robots:
            try:
                auth = await self._companion_auth()
                s = await self._http()
                async with s.ws_connect(COMPANION_WS, headers={"Authorization": auth, **APP_HEADERS}, heartbeat=None) as ws:
                    self._ws, self._ref, self._joins = ws, 0, {}
                    for duid, r in self.robots.items():
                        ref = self._next_ref()
                        self._joins[duid] = ref
                        await ws.send_str(json.dumps([ref, ref, f"robots:{r['id']}", "phx_join", {}]))
                    await self._request_state()
                    print("[KOBOLD] Live-Verbindung steht", flush=True)
                    delay = 2
                    hb = asyncio.ensure_future(self._heartbeat(ws))
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._on_message(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        hb.cancel()
            except asyncio.CancelledError:
                raise
            except AuthError as e:
                self.error = str(e)
                print(f"[KOBOLD] Live-Verbindung: Anmeldung abgelehnt: {e}", flush=True)
                delay = 600
            except Exception as e:
                print(f"[KOBOLD] Live-Verbindung: {type(e).__name__}: {e}", flush=True)
            self._ws = None
            for st in self.status.values():
                if time.time() - (st.get("updated") or 0) > 1800:
                    st.update(online=False, stateText="nicht erreichbar")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    async def _heartbeat(self, ws):
        while not ws.closed:
            await asyncio.sleep(30)
            try:
                await ws.send_str(json.dumps([None, self._next_ref(), "phoenix", "heartbeat", {}]))
            except Exception:
                return

    async def _housekeeping(self):
        """alle 10 Min. Status nachfragen, alle 6 h Räume neu laden"""
        n = 0
        while self.configured:
            await asyncio.sleep(600)
            n += 1
            try:
                await self._request_state()
                if n % 36 == 0:
                    await self._load_rooms()
            except Exception as e:
                print(f"[KOBOLD] Nachfragen: {e}", flush=True)

    async def _stop_ws(self):
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    def _duid_for(self, robot_id):
        if robot_id and PREFIX + str(robot_id) in self.robots:
            return PREFIX + str(robot_id)
        return next(iter(self.robots), None) if len(self.robots) == 1 else None

    def _on_message(self, raw):
        try:
            data = json.loads(raw)
        except ValueError:
            return
        kind, rid, body = None, None, None
        if isinstance(data, list) and len(data) >= 5:          # Phoenix: [join_ref, ref, topic, event, payload]
            topic, event, payload = data[2], data[3], data[4] if isinstance(data[4], dict) else {}
            rid = topic.split(":", 1)[1] if isinstance(topic, str) and topic.startswith("robots:") else None
            if event == "phx_reply":
                resp = payload.get("response") or {}
                b = resp.get("body") if isinstance(resp, dict) else None
                if payload.get("status") == "ok" and isinstance(b, dict) and ("state" in b or "details" in b):
                    kind, body = "state", b
            elif event in ("last_state", "state_changed"):
                kind, body = "state", payload.get("body", payload.get("state"))
            elif event == "cleaning_state":
                kind, body = "cleaning", payload.get("body") or payload.get("state") or payload
        elif isinstance(data, dict):                             # Companion: {"event_type": …, "payload": …}
            et, payload = data.get("event_type"), data.get("payload")
            rid = (payload or {}).get("robot_id") if isinstance(payload, dict) else None
            if et == "state_changed":
                kind, body = "state", (payload or {}).get("state")
            elif et == "cleaning_state":
                kind, body = "cleaning", (payload or {}).get("state")
            elif et == "service_status" and isinstance(payload, dict):
                print(f"[KOBOLD] Hinweis der Cloud: {payload.get('state')} – {payload.get('title')}", flush=True)
        if not kind or not isinstance(body, dict):
            return
        duid = self._duid_for(rid)
        if not duid:
            return
        if kind == "cleaning":
            self.cleaning[duid] = body
        else:
            self.raw[duid] = body
        self._update(duid)

    # ---------------------------------------------------------- Status für die App
    def _update(self, duid):
        r = self.robots.get(duid) or {}
        b = self.raw.get(duid)
        st = self.status.setdefault(duid, {"id": duid})
        if not b:
            return
        det = b.get("details") or {}
        center = b.get("cleaning_center") or {}
        cmds = {k for k, v in (b.get("available_commands") or {}).items() if v}
        errs = [e for e in (b.get("errors") or []) if isinstance(e, dict) and e.get("code")
                and e.get("severity") in (None, "", "error")]
        state, action = b.get("state"), b.get("action")
        docked = bool(det.get("is_docked"))
        charging = bool(det.get("is_charging"))
        if state == "error" or errs:
            s, text = "error", "Fehler"
        elif action in CLEANING_ACTIONS and state != "paused":
            s, text = "cleaning", {"spot_cleaning": "Spot-Reinigung", "mapping": "kartiert", "fast_mapping": "kartiert",
                                   "map_creation": "kartiert"}.get(action, "saugt")
        elif action in RETURNING_ACTIONS:
            s, text = "returning_home", "fährt zur Station"
        elif state == "paused":
            s, text = "paused", "pausiert"
        elif docked or charging:
            s, text = ("charging", "lädt") if charging else ("charging_complete", "in der Station")
            if (det.get("charge") or 0) >= 100:
                text = "voll geladen"
        elif state == "busy":
            s, text = "cleaning", "aktiv"
        else:
            s, text = "idle", "bereit (nicht in der Station)"
        code = errs[0]["code"] if errs else None
        base_err = center.get("base_error") or (center.get("bag_status") if center.get("bag_status") not in (None, "bag_ok") else None)
        clean = self.cleaning.get(duid) or {}
        runs = clean.get("runs") or []
        area = sum(float((x.get("stats") or {}).get("area") or 0) for x in runs if isinstance(x, dict)) or None
        t = clean.get("timing") or {}
        minutes = None
        try:
            if t.get("start"):
                end = t.get("end") if s not in ("cleaning", "paused", "returning_home") else None
                t0 = datetime.fromisoformat(t["start"].replace("Z", "+00:00")).timestamp()
                t1 = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() if end else time.time()
                minutes = max(0, round((t1 - t0 - float(t.get("paused") or 0) - float(t.get("charging") or 0)) / 60))
        except Exception:
            pass
        fan = (self.state.get("fan") or {}).get(duid, "auto")
        nav = (self.state.get("nav") or {}).get(duid, "normal")
        fans = r.get("fans") or list(FAN_DE)
        st.update(
            id=duid, kind="kobold", name=r.get("name") or "Kobold", model=r.get("model") or "Kobold VR7",
            online=True, local=False, via="Vorwerk-Cloud", state=s, stateText=text,
            cleaning=s in ("cleaning", "paused", "returning_home"), docked=docked or charging,
            battery=det.get("charge"), cleanArea=area, cleanMinutes=minutes,
            error=(ERROR_DE.get(code) or code.replace("_", " ")) if code else (text if s == "error" else None),
            dockError=BASE_ERROR_DE.get(base_err, base_err.replace("_", " ")) if base_err else None,
            fan=fan, fanText=FAN_DE.get(fan, fan), fanOptions=[{"id": k, "text": FAN_DE[k]} for k in fans],
            mode=nav, modeText=NAV_DE.get(nav, nav),
            modeOptions=[{"id": k, "text": v} for k, v in NAV_DE.items()] if r.get("extra_care") else [],
            rooms=[{"id": i + 1, "name": x["name"]} for i, x in enumerate((self.state.get("rooms") or {}).get(duid) or [])],
            commands=sorted(cmds), features=["find", "rooms"], updated=int(time.time()))
        self._notify(duid, st)

    def list(self):
        return {"configured": self.configured, "error": self.error, "autoAway": bool(self.state.get("auto_away")),
                "devices": sorted(self.status.values(), key=lambda s: s.get("name") or "")}

    def refresh_now(self):
        try:
            self._run(self._request_state(), timeout=10)
        except Exception:
            pass

    # ---------------------------------------------------------- Steuern
    async def _ability(self, duid, ability):
        r = self.robots[duid]
        return await self._orb("POST", f"/vendors/{VENDOR_ID}/robots/{r['serial']}/messages", {"ability": ability})

    async def _start_clean(self, duid, plan=None, tracks=None):
        fan = (self.state.get("fan") or {}).get(duid, "auto")
        nav = (self.state.get("nav") or {}).get(duid, "normal")
        settings = {"mode": fan, "navigation_mode": nav}
        if plan and tracks:
            runs = [{"settings": dict(settings), "map": {"floorplan_uuid": plan, "zone_uuid": t, "nogo_enabled": True}}
                    for t in tracks]
        else:
            maps = (self.robots[duid].get("maps") or [])
            runs = [{"settings": settings,
                     "map": {"floorplan_uuid": maps[0]["uuid"], "zone_uuid": None, "nogo_enabled": True} if len(maps) == 1 else None}]
        await self._orb("POST", f"/robots/{self.robots[duid]['id']}/cleaning/v2", {"runs": runs, "ability": "cleaning.start"}, app=True)

    async def _wait_cmd(self, duid, cmd, secs):
        for _ in range(secs):
            if cmd in set(self.status.get(duid, {}).get("commands") or []):
                return True
            await self._request_state(duid)
            await asyncio.sleep(1)
        return False

    async def _command(self, duid, action, value):
        st = self.status.get(duid) or {}
        cmds = set(st.get("commands") or [])
        if action == "start":
            if "resume" in cmds and "start" not in cmds:
                await self._ability(duid, "cleaning.resume")
            else:
                await self._start_clean(duid)
        elif action == "pause":
            await self._ability(duid, "cleaning.pause")
        elif action in ("stop", "dock"):
            if st.get("docked"):
                if "cancel" in cmds:
                    await self._ability(duid, "cleaning.cancel")
                return
            if "return_to_base" not in cmds:
                if "pause" not in cmds:
                    raise ValueError("Der Roboter ist nicht unterwegs und findet so nicht allein zurück – bitte in die Station setzen")
                await self._ability(duid, "cleaning.pause")
                if not await self._wait_cmd(duid, "return_to_base", 30):
                    raise ValueError("Der Roboter nimmt „Zur Station“ gerade nicht an")
            await self._ability(duid, "navigation.return_to_base")
        elif action == "find":
            await self._ability(duid, "utilities.find_me")
        elif action == "rooms":
            rooms = (self.state.get("rooms") or {}).get(duid) or []
            ids = [int(x) for x in (value or {}).get("rooms", [])]
            picked = [rooms[i - 1] for i in ids if 1 <= i <= len(rooms)]
            if not picked:
                raise ValueError("Keine Zone gewählt")
            if len({x["map"] for x in picked}) > 1:
                raise ValueError("Die Räume liegen auf verschiedenen Karten – bitte nur Räume einer Karte wählen")
            tracks = [x["track"] for x in picked] * max(1, min(3, int((value or {}).get("repeat", 1))))
            await self._start_clean(duid, picked[0]["map"], tracks)
        elif action == "fan":
            if value not in FAN_DE:
                raise ValueError("Unbekannte Saugstufe")
            self.state.setdefault("fan", {})[duid] = value
            self._save()
        elif action == "mode":
            if value not in NAV_DE:
                raise ValueError("Unbekannter Reinigungsmodus")
            self.state.setdefault("nav", {})[duid] = value
            self._save()
        elif action == "water":              # Zeitpläne setzen ggf. Wassermenge – der VR7 wischt nicht
            return
        elif action == "rooms-reload":
            await self._load_rooms()
        else:
            raise ValueError("Bei diesem Roboter nicht möglich")
        await asyncio.sleep(1)
        await self._request_state(duid)

    def command(self, duid, action, value=None):
        if duid not in self.robots:
            raise KeyError(duid)
        try:
            self._run(self._command(duid, action, value), timeout=60)
        except KoboldError as e:
            msg = str(e)
            if "503" in msg:
                raise ValueError("Der Roboter nimmt den Befehl gerade nicht an")
            raise
        st = self.status.get(duid)
        if st and action in ("fan", "mode"):
            self._update(duid)

    def map(self, duid, fresh=False):
        raise ValueError("Für den Vorwerk Kobold gibt es hier keine Kartenansicht")

    def zone_payload(self, duid, zones, repeat=1):
        raise ValueError("Zonen zeichnen geht beim Vorwerk Kobold nur in der MyKobold-App")

    def app_timers(self):
        return []

    def set_auto_away(self, on):
        self.state["auto_away"] = bool(on)
        self._save()

    def on_presence(self, all_away):
        """Saugen, wenn alle weg sind – wie bei Roborock höchstens 1× am Tag, 8–20 Uhr, Akku ≥ 50 %"""
        if not all_away or not self.state.get("auto_away"):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.get("auto_away_last") == today or not 8 <= datetime.now().hour < 20:
            return
        for duid, st in list(self.status.items()):
            if st.get("docked") and not st.get("error") and (st.get("battery") or 0) >= 50:
                try:
                    self.command(duid, "start")
                    self.state["auto_away_last"] = today
                    self._save()
                    self._send(f"🧹 {st.get('name') or 'Kobold'} startet", "Alle sind weg – es wird gesaugt.")
                except Exception as e:
                    print(f"[KOBOLD] Auto-Start: {e}", flush=True)

    # ---------------------------------------------------------- Mitteilungen
    def _send(self, title, body):
        if self.push is not None and self.push.configured:
            n = self.push.send("vacuum", title, body)
            print(f"[KOBOLD] {title} – {body} → {n} Gerät(e)", flush=True)

    def _notify(self, duid, st):
        prev = self._prev.get(duid)
        self._prev[duid] = dict(st)
        if prev is None or not st.get("online"):
            return
        name = st.get("name") or "Kobold"
        if prev.get("cleaning") and not st.get("cleaning") and st.get("docked") and not st.get("error"):
            area = st.get("cleanArea")
            self._send(f"🧹 {name} ist fertig",
                       f"{area:.0f} m² in {st.get('cleanMinutes') or 0} min" if area else "Zurück in der Station")
        if st.get("error") and st.get("error") != prev.get("error"):
            self._send(f"⚠️ {name} braucht Hilfe", st["error"])
        if st.get("dockError") and st.get("dockError") != prev.get("dockError"):
            self._send(f"🗑️ {name}: {st['dockError']}", "Bitte an der Station nachsehen.")
