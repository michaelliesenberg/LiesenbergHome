"""Roborock-Saugroboter (mit Station): Status, Steuerung, Räume, Station – über python-roborock (wie Home Assistant).

Anmeldung (einmalig, Besitzer, Einrichtungsseite → Roborock): E-Mail der Roborock-App eingeben → Roborock schickt
einen Code per Mail → Code eingeben. Kein Passwort. Gespeichert wird nur das Anmelde-Token (roborock.json) und der
Geräte-Cache mit den lokalen Schlüsseln (roborock-cache.pkl) – beides nur auf dem Hub, chmod 600.
Gesteuert wird dann möglichst lokal im WLAN; die Bibliothek fällt automatisch auf die Roborock-Cloud zurück.

Mitteilungen (Abo „vacuum"): Reinigung fertig, Roboter steckt fest/Fehler, Frischwasser leer, Schmutzwasser voll,
Station-Fehler. Optional: „Saugen, wenn alle weg sind" (aus, bis der Besitzer es einschaltet).
"""
import asyncio
import json
import os
import pathlib
import threading
import time
from datetime import datetime

from hub_config import DATA_DIR

STATE_FILE = os.path.join(DATA_DIR, "roborock.json")
CACHE_FILE = os.path.join(DATA_DIR, "roborock-cache.pkl")

STATE_DE = {
    "starting": "startet", "charger_disconnected": "nicht in der Station", "idle": "bereit",
    "remote_control_active": "Fernsteuerung", "cleaning": "saugt", "returning_home": "fährt zur Station",
    "manual_mode": "manuell", "charging": "lädt", "charging_problem": "Ladeproblem", "paused": "pausiert",
    "spot_cleaning": "Spot-Reinigung", "error": "Fehler", "shutting_down": "schaltet aus", "updating": "Update",
    "docking": "dockt an", "going_to_target": "fährt zum Ziel", "zoned_cleaning": "Zonenreinigung",
    "segment_cleaning": "Raumreinigung", "emptying_the_bin": "entleert Staubbehälter",
    "washing_the_mop": "wäscht Mopp", "going_to_wash_the_mop": "fährt zum Mopp-Waschen", "mapping": "kartiert",
    "charging_complete": "voll geladen", "device_offline": "offline", "locked": "gesperrt",
    "air_drying_stopping": "Trocknung endet", "attaching_the_mop": "setzt Mopp ein", "detaching_the_mop": "legt Mopp ab",
}
CLEANING = {"cleaning", "spot_cleaning", "zoned_cleaning", "segment_cleaning", "returning_home", "washing_the_mop",
            "going_to_wash_the_mop", "going_to_target", "paused", "docking", "mapping"}
DOCKED = {"charging", "charging_complete", "idle", "emptying_the_bin", "air_drying_stopping"}
ERROR_DE = {
    "lidar_blocked": "Laser-Sensor blockiert", "bumper_stuck": "Stoßstange klemmt", "wheels_suspended": "Räder in der Luft",
    "cliff_sensor_error": "Absturzsensor verschmutzt", "main_brush_jammed": "Hauptbürste blockiert",
    "side_brush_jammed": "Seitenbürste blockiert", "wheels_jammed": "Räder blockiert", "robot_trapped": "steckt fest",
    "no_dustbin": "Staubbehälter fehlt", "low_battery": "Akku leer", "charging_error": "Ladefehler",
    "battery_error": "Akkufehler", "wall_sensor_dirty": "Wandsensor verschmutzt", "robot_tilted": "Roboter gekippt",
    "side_brush_error": "Seitenbürste", "fan_error": "Lüfter", "dock": "Station", "optical_flow_sensor_dirt": "Bodensensor verschmutzt",
    "vertical_bumper_pressed": "obere Stoßstange gedrückt", "dock_locator_error": "Station nicht gefunden",
    "return_to_dock_fail": "findet nicht zurück zur Station", "nogo_zone_detected": "Sperrzone",
    "vibrarise_jammed": "Mopp-Hebung blockiert", "robot_on_carpet": "steht auf Teppich",
    "filter_blocked": "Filter verstopft", "invisible_wall_detected": "virtuelle Wand", "cannot_cross_carpet": "kommt nicht über den Teppich",
    "internal_error": "interner Fehler", "clean_tank_empty": "Frischwasser leer", "dirty_tank_full": "Schmutzwasser voll",
    "water_carriage_drop": "Wischmodul fehlt", "mop_attached": "Mopp aufgesetzt",
}
DOCK_ERROR_DE = {
    "water_empty": "Frischwasser leer", "waste_water_tank_full": "Schmutzwasser voll",
    "dirty_tank_latch_open": "Schmutzwassertank nicht eingerastet", "no_dustbin": "Staubbeutel fehlt",
    "duct_blockage": "Absaugkanal verstopft", "no_dustbin_or_filter": "Staubbeutel/Filter fehlt",
    "maintenance_brush_jammed": "Reinigungsbürste der Station blockiert", "cleaning_tank_full_or_blocked": "Reinigungswanne voll/verstopft",
    "auto_empty_dock_fan_error": "Absaugung: Lüfterfehler", "auto_empty_dock_voltage_error": "Absaugung: Spannungsfehler",
}
FAN_DE = {"quiet": "Leise", "balanced": "Normal", "turbo": "Turbo", "max": "Max", "max_plus": "Max+", "off": "Aus (nur wischen)",
          "custom": "Individuell", "gentle": "Sanft", "smart_mode": "Smart"}
WATER_DE = {"off": "Aus", "slight": "Sehr wenig", "low": "Niedrig", "mild": "Niedrig", "gentle": "Sanft", "medium": "Mittel",
            "moderate": "Mittel+", "high": "Hoch",
            "intense": "Hoch", "custom": "Individuell", "smart_mode": "Smart", "extreme": "Extrem"}


def _name(v):
    return getattr(v, "name", None) or (str(v) if v is not None else None)


class Vacuum:
    def __init__(self, push=None):
        self.push = push
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.manager = None
        self.devices = {}          # duid → RoborockDevice
        self.status = {}           # duid → dict für die App
        self.error = None
        self._pending = None       # (email, api client) zwischen „Code anfordern" und „Code eingeben"
        self._prev = {}            # duid → letzter Zustand (für Mitteilungen)
        self._last_full = {}       # duid → Zeitpunkt der letzten vollständigen Abfrage (Räume, Verbrauchsteile)
        self.state = self._load()
        if self.state.get("user_data"):
            self._run(self._connect(), timeout=None, wait=False)
        threading.Thread(target=self._poller, daemon=True).start()

    # ---------------------------------------------------------- Infrastruktur
    def _run(self, coro, timeout=40, wait=True):
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
        return bool(self.state.get("user_data"))

    # ---------------------------------------------------------- Anmeldung (E-Mail-Code)
    def request_code(self, email):
        from roborock.web_api import RoborockApiClient
        email = (email or "").strip()
        if "@" not in email:
            raise ValueError("Bitte die E-Mail-Adresse der Roborock-App eingeben")

        async def go():
            client = RoborockApiClient(username=email)
            await client.request_code_v4()
            return client
        self._pending = (email, self._run(go()), time.time())

    def login(self, code):
        if not self._pending or time.time() - self._pending[2] > 15 * 60:
            raise ValueError("Bitte zuerst den Code anfordern (gilt 15 Minuten)")
        email, client, _ = self._pending
        code = "".join(ch for ch in str(code) if ch.isdigit())

        async def go():
            ud = await client.code_login_v4(code)
            try:
                base = await client.base_url
            except Exception:
                base = None
            return ud, base
        ud, base = self._run(go())
        self.state = {"email": email, "user_data": ud.as_dict(), "base_url": base,
                      "auto_away": self.state.get("auto_away", False)}
        self._save()
        self._pending = None
        self._run(self._connect(), timeout=90)

    def logout(self):
        async def go():
            if self.manager:
                await self.manager.close()
        try:
            self._run(go())
        except Exception:
            pass
        self.manager, self.devices, self.status = None, {}, {}
        self.state = {}
        for p in (STATE_FILE, CACHE_FILE):
            try:
                os.remove(p)
            except OSError:
                pass

    async def _connect(self):
        from roborock.data import UserData
        from roborock.devices.device_manager import UserParams, create_device_manager
        try:
            if self.manager:
                await self.manager.close()
            params = UserParams(username=self.state["email"], user_data=UserData.from_dict(self.state["user_data"]),
                                base_url=self.state.get("base_url"))
            # ohne Datei-Cache: die Gerätedaten (inkl. lokaler Schlüssel) holt der Hub beim Start einmal von Roborock.
            # Der Datei-Cache der Bibliothek ließ sich hier nicht speichern (leere Datei → „Ran out of input").
            if os.path.exists(CACHE_FILE):
                os.remove(CACHE_FILE)
            self.manager = await create_device_manager(params)
            devs = await self.manager.get_devices()
            self.devices = {d.duid: d for d in devs if d.v1_properties is not None}
            self.error = None if self.devices else "Kein unterstützter Saugroboter im Roborock-Konto gefunden"
            for duid in self.devices:
                await self._refresh(duid, full=True)
        except Exception as e:
            self.error = f"Roborock: {e}"
            print(f"[ROBOROCK] Verbinden: {e}", flush=True)

    # ---------------------------------------------------------- Status
    async def _refresh(self, duid, full=False):
        dev = self.devices[duid]
        p = dev.v1_properties
        st = self.status.setdefault(duid, {"id": duid})
        try:
            await p.status.refresh()
            s = p.status
            state = _name(s.state)
            err = _name(s.error_code)
            dock_err = _name(getattr(s, "dock_error_status", None))
            st.update(
                name=dev.name, model=getattr(dev.product, "model", None), online=True,
                local=bool(getattr(dev, "is_local_connected", False)),
                state=state, stateText=STATE_DE.get(state or "", (state or "–").replace("_", " ")),
                cleaning=state in CLEANING, docked=state in DOCKED,
                battery=s.battery, cleanArea=s.square_meter_clean_area, cleanMinutes=round((s.clean_time or 0) / 60),
                cleanPercent=getattr(s, "clean_percent", None),
                error=None if err in (None, "none") else ERROR_DE.get(err, err.replace("_", " ")),
                dockError=None if dock_err in (None, "ok", "unknown") else DOCK_ERROR_DE.get(dock_err, dock_err.replace("_", " ")),
                waterShortage=bool(getattr(s, "water_shortage_status", 0)),
                drying=bool(getattr(s, "dry_status", 0)),
                fan=s.fan_speed_name, fanText=FAN_DE.get(s.fan_speed_name or "", s.fan_speed_name),
                water=s.water_mode_name, waterText=WATER_DE.get(s.water_mode_name or "", s.water_mode_name),
                fanOptions=[{"id": n, "text": FAN_DE.get(n, n)} for n in s.fan_speed_mapping.values()],
                waterOptions=[{"id": n, "text": WATER_DE.get(n, n)} for n in s.water_mode_mapping.values()],
                dock=_name(getattr(s, "dock_type", None)),
                updated=int(time.time()))
            if full or time.time() - self._last_full.get(duid, 0) > 600:
                self._last_full[duid] = time.time()
                try:
                    await p.rooms.refresh()
                    st["rooms"] = [{"id": r.segment_id, "name": r.name} for r in (p.rooms.rooms or [])]
                except Exception as e:
                    print(f"[ROBOROCK] Räume: {e}", flush=True)
                try:
                    await p.consumables.refresh()
                    c = p.consumables
                    def pct(left, total):
                        return None if left is None else max(0, min(100, round(left / total * 100)))
                    st["consumables"] = [x for x in [
                        {"name": "Hauptbürste", "percent": pct(c.main_brush_time_left, 300 * 3600)},
                        {"name": "Seitenbürste", "percent": pct(c.side_brush_time_left, 200 * 3600)},
                        {"name": "Filter", "percent": pct(c.filter_time_left, 150 * 3600)},
                        {"name": "Sensoren", "percent": pct(c.sensor_time_left, 30 * 3600)},
                    ] if x["percent"] is not None]
                except Exception as e:
                    print(f"[ROBOROCK] Verbrauchsteile: {e}", flush=True)
        except Exception as e:
            st.update(online=False, stateText="nicht erreichbar", error=str(e)[:120])
        self._notify(duid, st)

    def list(self):
        return {"configured": self.configured, "error": self.error, "autoAway": bool(self.state.get("auto_away")),
                "devices": sorted(self.status.values(), key=lambda s: s.get("name") or "")}

    def refresh_now(self):
        for duid in list(self.devices):
            try:
                self._run(self._refresh(duid))
            except Exception:
                pass

    # ---------------------------------------------------------- Steuern
    async def _command(self, duid, action, value=None):
        from roborock.roborock_typing import RoborockCommand as C
        p = self.devices[duid].v1_properties
        send = p.command.send
        if action == "start":
            await send(C.APP_START)
        elif action == "pause":
            await send(C.APP_PAUSE)
        elif action == "stop":
            await send(C.APP_STOP)
        elif action == "dock":
            await send(C.APP_CHARGE)
        elif action == "find":
            await send(C.FIND_ME)
        elif action == "rooms":
            ids = [int(x) for x in (value or {}).get("rooms", [])]
            if not ids:
                raise ValueError("Keine Räume gewählt")
            await send(C.APP_SEGMENT_CLEAN, [{"segments": ids, "repeat": int((value or {}).get("repeat", 1))}])
        elif action == "zone":
            zones = (value or {}).get("zones") or []
            if not zones:
                raise ValueError("Keine Zone gezeichnet")
            await send(C.APP_ZONED_CLEAN, zones)
        elif action == "fan":
            code = next((k for k, v in p.status.fan_speed_mapping.items() if v == value), None)
            if code is None:
                raise ValueError("Unbekannte Saugstufe")
            await send(C.SET_CUSTOM_MODE, [code])
        elif action == "water":
            code = next((k for k, v in p.status.water_mode_mapping.items() if v == value), None)
            if code is None:
                raise ValueError("Unbekannte Wassermenge")
            await send(C.SET_WATER_BOX_CUSTOM_MODE, [code])
        elif action == "wash":
            await send(C.APP_START_WASH)
        elif action == "empty":
            await send(C.APP_START_COLLECT_DUST)
        elif action == "dry":
            await send(C.APP_SET_DRYER_STATUS, {"status": 1 if value else 0})
        else:
            raise ValueError(action)

    def app_timers(self):
        """Zeitpläne aus der Roborock-App lesen (nur anzeigen; beide Arten laufen unabhängig nebeneinander).
        Format je Eintrag (Roborock): [id, "on"/"off", ["m h * * Wochentage", [befehl, ...]], ...]"""
        if time.time() - getattr(self, "_timers_at", 0) < 600:
            return getattr(self, "_timers", [])
        from roborock.roborock_typing import RoborockCommand as C
        out = []
        for duid, dev in list(self.devices.items()):
            try:
                raw = self._run(dev.v1_properties.command.send(C.GET_SERVER_TIMER), timeout=20) or []
            except Exception as e:
                print(f"[ROBOROCK] App-Zeitpläne: {e}", flush=True)
                continue
            for t in raw if isinstance(raw, list) else []:
                try:
                    cron = t[2][0] if isinstance(t[2], list) else str(t[2])
                    m, h, _, _, dow = cron.split()[:5]
                    days = [] if dow == "*" else [(int(d) + 6) % 7 for d in dow.split(",") if d.isdigit()]  # So=0 → Mo=0
                    out.append({"duid": duid, "robot": dev.name, "enabled": str(t[1]).lower() in ("on", "1", "true"),
                                "time": f"{int(h):02d}:{int(m):02d}", "days": sorted(days) or list(range(7))})
                except Exception:
                    out.append({"duid": duid, "robot": dev.name, "raw": str(t)[:80]})
        self._timers, self._timers_at = out, time.time()
        return out

    # ---------------------------------------------------------- Karte (Bild + Räume zum Antippen)
    async def _map(self, duid):
        import base64
        from vacuum_map_parser_base.map_data import Point
        p = self.devices[duid].v1_properties
        await p.map_content.refresh()
        mc = p.map_content
        md, png = mc.map_data, mc.image_content
        if md is None or not png or md.image is None:
            raise RuntimeError("Keine Karte vom Roboter")
        dims = md.image.dimensions
        from PIL import Image, ImageChops
        import io
        img = Image.open(io.BytesIO(png)).convert("RGB")
        W0, H0 = img.size
        # leeren Rand abschneiden (Hintergrundfarbe = Ecke), damit die Karte groß angezeigt wird
        bg = img.getpixel((0, 0))
        box = ImageChops.difference(img, Image.new("RGB", img.size, bg)).getbbox() or (0, 0, W0, H0)
        m = 20
        box = (max(0, box[0] - m), max(0, box[1] - m), min(W0, box[2] + m), min(H0, box[3] + m))
        img = img.crop(box)
        W, H = img.size

        def norm(x, y):
            q = Point(x, y).to_img(dims).rotated(dims)
            return [round((q.x - box[0]) / W, 4), round((q.y - box[1]) / H, 4)]

        names = {r["id"]: r["name"] for r in (self.status.get(duid, {}).get("rooms") or [])}
        small = img.resize((max(1, W // 4), max(1, H // 4)), Image.NEAREST)
        sw, sh = small.size
        px = small.load()
        rooms = []
        for rid, r in (md.rooms or {}).items():
            a, b = norm(r.x0, r.y0), norm(r.x1, r.y1)
            rect = [min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])]
            # häufigste Farbe im Raum-Rechteck = Farbe des Raums im Bild (zum Antippen und Hervorheben)
            counts = {}
            for yy in range(int(rect[1] * sh), int(rect[3] * sh)):
                for xx in range(int(rect[0] * sw), int(rect[2] * sw)):
                    c = px[min(xx, sw - 1), min(yy, sh - 1)]
                    if c != bg and sum(c) > 120:
                        counts[c] = counts.get(c, 0) + 1
            col = max(counts, key=counts.get) if counts else None
            rooms.append({"id": int(rid), "name": names.get(int(rid)) or getattr(r, "name", None) or f"Raum {rid}",
                          "rect": rect, "color": list(col) if col else None})
        buf = io.BytesIO(); img.save(buf, "PNG", optimize=True)
        # Bild (0…1) ↔ Roboter-Koordinaten (mm): affine Abbildung aus drei Stützpunkten – zum Zonen-Reinigen
        p0, px, py = norm(25000, 25000), norm(30000, 25000), norm(25000, 30000)
        self._affine = getattr(self, "_affine", {})
        self._affine[duid] = (p0, px, py)
        out = {"image": base64.b64encode(buf.getvalue()).decode(), "width": W, "height": H, "rooms": rooms,
               "robot": norm(md.vacuum_position.x, md.vacuum_position.y) if md.vacuum_position else None,
               "dock": norm(md.charger.x, md.charger.y) if md.charger else None,
               "updated": int(time.time())}
        return out

    def map(self, duid, fresh=False):
        """Karte zwischenspeichern: neu nach 10 Min., bei Reinigung nach 1 Min."""
        if duid not in self.devices:
            raise KeyError(duid)
        cache = getattr(self, "_maps", None)
        if cache is None:
            cache = self._maps = {}
        c = cache.get(duid)
        busy = (self.status.get(duid) or {}).get("cleaning")
        if c and not fresh and time.time() - c["updated"] < (60 if busy else 600):
            return c
        cache[duid] = self._run(self._map(duid), timeout=60)
        return cache[duid]

    def zone_payload(self, duid, zones, repeat=1):
        """Rechtecke im Kartenbild (0…1: x0,y0,x1,y1) → Roborock-Zonen [x0,y0,x1,y1,Durchgänge] in mm"""
        aff = getattr(self, "_affine", {}).get(duid)
        if not aff:
            self.map(duid)
            aff = self._affine.get(duid)
        (ax, ay), (bx, by), (cx, cy) = aff
        # Bildpunkt = p0 + (X-25000)/5000·(px-p0) + (Y-25000)/5000·(py-p0)  →  nach X, Y auflösen
        ux, uy, vx, vy = bx - ax, by - ay, cx - ax, cy - ay
        det = ux * vy - uy * vx
        if abs(det) < 1e-12:
            raise RuntimeError("Kartenumrechnung nicht möglich")

        def to_robot(nx, ny):
            dx, dy = nx - ax, ny - ay
            a = (dx * vy - dy * vx) / det
            b = (ux * dy - uy * dx) / det
            return 25000 + a * 5000, 25000 + b * 5000

        out = []
        for z in zones[:5]:
            x0, y0 = to_robot(z[0], z[1])
            x1, y1 = to_robot(z[2], z[3])
            out.append([int(min(x0, x1)), int(min(y0, y1)), int(max(x0, x1)), int(max(y0, y1)), max(1, min(3, int(repeat)))])
        return out

    def command(self, duid, action, value=None):
        if duid not in self.devices:
            raise KeyError(duid)
        self._run(self._command(duid, action, value))
        self.loop.call_later(3, lambda: asyncio.ensure_future(self._refresh(duid)))

    def set_auto_away(self, on):
        self.state["auto_away"] = bool(on)
        self._save()

    # ---------------------------------------------------------- Hintergrund + Mitteilungen
    def _poller(self):
        time.sleep(15)
        while True:
            busy = any(s.get("cleaning") for s in self.status.values())
            for duid in list(self.devices):
                try:
                    self._run(self._refresh(duid), timeout=40)
                except Exception as e:
                    print(f"[ROBOROCK] Abfrage: {e}", flush=True)
            time.sleep(15 if busy else 60)

    def _send(self, title, body):
        if self.push is not None and self.push.configured:
            n = self.push.send("vacuum", title, body)
            print(f"[ROBOROCK] {title} – {body} → {n} Gerät(e)", flush=True)

    def _notify(self, duid, st):
        prev = self._prev.get(duid)
        self._prev[duid] = dict(st)
        if prev is None or not st.get("online"):
            return
        name = st.get("name") or "Saugroboter"
        if prev.get("cleaning") and not st.get("cleaning") and st.get("docked") and not st.get("error"):
            area = st.get("cleanArea")
            self._send(f"🧹 {name} ist fertig", (f"{area:.0f} m² in {st.get('cleanMinutes')} min" if area else "Zurück in der Station"))
        if st.get("error") and st.get("error") != prev.get("error"):
            self._send(f"⚠️ {name} braucht Hilfe", st["error"])
        if st.get("dockError") and st.get("dockError") != prev.get("dockError"):
            self._send(f"🚰 Station: {st['dockError']}", "Bitte an der Station nachsehen.")
        if st.get("waterShortage") and not prev.get("waterShortage"):
            self._send("🚰 Frischwasser leer", f"{name}: bitte den Frischwassertank auffüllen.")

    def on_presence(self, all_away):
        """Vom Hub aufgerufen, wenn sich die Anwesenheit ändert. Startet höchstens einmal am Tag."""
        if not all_away or not self.state.get("auto_away"):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.get("auto_away_last") == today or not 8 <= datetime.now().hour < 20:
            return
        for duid, st in self.status.items():
            if st.get("docked") and not st.get("error") and (st.get("battery") or 0) >= 50:
                try:
                    self.command(duid, "start")
                    self.state["auto_away_last"] = today
                    self._save()
                    self._send(f"🧹 {st.get('name') or 'Saugroboter'} startet", "Alle sind weg – es wird gesaugt.")
                except Exception as e:
                    print(f"[ROBOROCK] Auto-Start: {e}", flush=True)
