"""Mammotion-Mähroboter (Luba / Yuka) über pymammotion – wie die Home-Assistant-Integration.

Mammotion hat keine lokale WLAN-Schnittstelle: gesteuert wird über die Mammotion-Cloud. Die Anmeldung braucht
E-Mail + Passwort des Mammotion-Kontos. Der Besitzer gibt sie selbst auf der Einrichtungsseite ein (Empfehlung:
ein zweites Konto, mit dem der Mäher in der Mammotion-App geteilt ist). Gespeichert nur auf dem Hub
(mammotion.json, chmod 600), nie im Protokoll. Dazu der Token-Cache, damit nicht ständig neu angemeldet wird.

Mitteilungen (Abo „mower"): fertig, Fehler/steckt fest, wegen Regen zurück zur Station.
Optional (aus, bis der Besitzer es einschaltet): bei Regen in den nächsten 30 Minuten zurück zur Station.
"""
import asyncio
import json
import os
import threading
import time

from hub_config import DATA_DIR

STATE_FILE = os.path.join(DATA_DIR, "mammotion.json")

MODE_DE = {
    0: "nicht aktiv", 1: "online", 2: "offline", 3: "ausgeschaltet", 8: "deaktiviert", 10: "startet",
    11: "bereit", 12: "nicht verbunden", 13: "mäht", 14: "fährt zur Station", 15: "lädt", 16: "Update",
    17: "gesperrt", 18: "Systemfehler", 19: "pausiert", 20: "manuell", 22: "Update fertig", 23: "Update fehlgeschlagen",
    31: "Karte zeichnen", 32: "Karte zeichnen", 34: "Karte zeichnen", 35: "Karte zeichnen", 36: "Grenze bearbeiten",
    37: "Ortungsfehler", 38: "Grenze überschritten", 39: "Laden pausiert", 45: "kartiert",
}
WORKING, RETURNING, CHARGING, READY, PAUSE = 13, 14, 15, 11, 19
ERROR_MODES = {18, 23, 37, 38}


class Mower:
    def __init__(self, push=None, rain_soon_fn=None, storm_fn=None):
        self.storm_fn = storm_fn          # → Grund (Sturm/Gewitter in der nächsten Stunde) oder None
        self.push, self.rain_soon_fn = push, rain_soon_fn
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.api = None
        self.names = []
        self.status = {}
        self.error = None
        self._prev = {}
        self.state = self._load()
        if self.state.get("email") and self.state.get("password"):
            asyncio.run_coroutine_threadsafe(self._connect(restore=True), self.loop)
        threading.Thread(target=self._poller, daemon=True).start()

    # ---------------------------------------------------------- Infrastruktur
    def _run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

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
        return bool(self.state.get("email") and self.state.get("password"))

    # ---------------------------------------------------------- Anmeldung
    def login(self, email, password):
        email = (email or "").strip()
        # E-Mail oder Mammotion-ID (Kontonummer) – laut Mammotion-HA klappt manchmal nur die ID
        if not email or not password or ("@" not in email and not email.isdigit()):
            raise ValueError("Bitte E-Mail oder Mammotion-ID und Passwort des Mammotion-Kontos eingeben")
        old = dict(self.state)
        self.state = {"email": email, "password": password, "rain_guard": old.get("rain_guard", False)}
        try:
            self._run(self._connect(restore=False), timeout=120)
        except Exception:
            self.state = old
            raise
        if self.error and not self.names:
            msg, self.state = self.error, old
            raise RuntimeError(msg)
        self._save()

    def logout(self):
        async def go():
            if self.api is not None:
                await self.api.mammotion.stop()
        try:
            self._run(go())
        except Exception:
            pass
        self.api, self.names, self.status = None, [], {}
        self.state = {}
        try:
            os.remove(STATE_FILE)
        except OSError:
            pass

    async def _persist_cache(self):
        try:
            cache = self.api.mammotion.to_cache() if self.api else {}
            if cache:
                self.state["cache"] = json.loads(json.dumps(cache, default=str))
                self._save()
        except Exception as e:
            print(f"[MAMMOTION] Cache speichern: {type(e).__name__}", flush=True)

    async def _connect(self, restore):
        from pymammotion.data.model.device import MowerDevice
        from pymammotion.homeassistant.mower_api import HomeAssistantMowerApi
        try:
            if self.api is not None:
                await self.api.mammotion.stop()
            if self.state.get("legacy_login"):
                self._use_legacy_login()           # dieses Konto klappt nur mit dem alten Login (auch bei Neuanmeldung)
            self.api = HomeAssistantMowerApi("liesenberg-home")
            m = self.api.mammotion
            if restore and self.state.get("cache"):
                await m.restore_credentials(self.state["email"], self.state["password"], self.state["cache"])
            else:
                try:
                    await m.login_and_initiate_cloud(self.state["email"], self.state["password"])
                except Exception as e:
                    if "mismatch" not in str(e).lower() and type(e).__name__ != "LoginFailedError":
                        raise
                    print(f"[MAMMOTION] Login abgelehnt ({str(e)[-60:]}) – {self._diag()}", flush=True)
                    # neuer Login (oauth2) abgelehnt → älteren, verschlüsselten Login der Bibliothek probieren
                    if not await self._legacy_login_ok():
                        raise
                    self._use_legacy_login()
                    self.state["legacy_login"] = True
                    await m.login_and_initiate_cloud(self.state["email"], self.state["password"])
            m.on_credentials_updated = self._persist_cache
            names = []
            for h in m._device_registry.all_devices:
                dev = m.get_device_by_name(h.device_name)
                if isinstance(dev, MowerDevice):
                    names.append(h.device_name)
            self.names = names
            self.error = None if names else "Kein Mähroboter im Mammotion-Konto gefunden"
            await self._persist_cache()
            for n in names:
                await self._refresh(n)
        except Exception as e:
            # nie Zugangsdaten ins Protokoll – nur Fehlertyp und Text der Bibliothek
            self.error = f"Mammotion: {e}"
            print(f"[MAMMOTION] Anmelden: {type(e).__name__}: {str(e)[:120]}", flush=True)

    def _diag(self):
        """Hinweise ohne Zugangsdaten preiszugeben: Sonderzeichen-Art, Großbuchstaben in der E-Mail."""
        pw, em = self.state.get("password") or "", self.state.get("email") or ""
        masked = (em[:2] + "…@" + em.split("@")[-1]) if "@" in em else "?"
        return (f"konto={masked} email_gross={em != em.lower()} pw_nicht_ascii={any(ord(c) > 127 for c in pw)} "
                f"pw_leerzeichen={' ' in pw}")

    async def _legacy_login_ok(self):
        """Nur Diagnose: klappt der alte Login (/oauth/token, AES) mit denselben Daten? Protokolliert nur Code/Text."""
        from pymammotion.http.http import MammotionHTTP
        try:
            r = await MammotionHTTP().login(self.state["email"], self.state["password"])
            print(f"[MAMMOTION] Login neu abgelehnt – alter Login: code={r.code} msg={(r.msg or '')[:80]}", flush=True)
            return r.code == 0 and r.data is not None
        except Exception as e:
            print(f"[MAMMOTION] alter Login: {type(e).__name__}: {str(e)[:80]}", flush=True)
            return False

    @staticmethod
    def _use_legacy_login():
        from pymammotion.http.http import MammotionHTTP
        if getattr(MammotionHTTP, "_lh_legacy", False):
            return
        MammotionHTTP.login_v2 = MammotionHTTP.login
        MammotionHTTP._lh_legacy = True
        print("[MAMMOTION] verwende alten Login-Weg", flush=True)

    # ---------------------------------------------------------- Status
    async def _refresh(self, name, ask=False):
        from pymammotion.data.error_codes import describe
        st = self.status.setdefault(name, {"id": name, "name": name})
        try:
            try:
                if ask:
                    await self.api.mammotion.refresh_status(name)
                else:                                # Bericht anfordern, wenn der letzte älter als 1 Min. ist
                    await self.api.mammotion.ensure_fresh_state(name, max_age_s=60)
            except Exception as e:
                print(f"[MAMMOTION] Statusabfrage: {type(e).__name__}: {str(e)[:80]}", flush=True)
            dev = await self.api.update(name)
            if dev is None:
                return
            rd = dev.report_data
            mode = rd.dev.sys_status
            limits = self.api.device_limits(name)
            lo, hi = getattr(limits.blade_height, "min", None), getattr(limits.blade_height, "max", None)
            kh = rd.work.knife_height or None
            if kh and lo and hi and not lo <= kh <= hi:
                lo = hi = None                       # Grenzwerte passen nicht zum Modell → Regler lieber ausblenden
            codes = list(getattr(dev.errors, "active_codes", []) or [])
            plans = [{"id": p.plan_id, "name": p.task_name or p.job_name or "Aufgabe"}
                     for p in (dev.map.plan or {}).values() if getattr(p, "plan_id", "")]
            areas = [a.name for a in (dev.map.area_name or []) if getattr(a, "name", "")]
            st.update(
                online=self.api.is_online(name), mode=mode, stateText=MODE_DE.get(mode, f"Status {mode}"),
                mowing=mode == WORKING, paused=mode == PAUSE, returning=mode == RETURNING,
                docked=mode == CHARGING or (mode == READY and rd.dev.charge_state != 0),
                battery=rd.dev.battery_val, charging=rd.dev.charge_state != 0,
                percent=rd.work.mow_percent if mode in (WORKING, PAUSE, RETURNING) else None,
                area=rd.work.area_mowed if mode in (WORKING, PAUSE, RETURNING) else None,
                bladeHeight=rd.work.knife_height or None,
                bladeMin=lo, bladeMax=hi,
                error=describe(codes[0], "de") if codes and mode in ERROR_MODES | {PAUSE, READY} else None,
                plans=plans, areas=areas, updated=int(time.time()), **self._model(name, dev))
        except Exception as e:
            st.update(online=False, stateText="nicht erreichbar", error=str(e)[:120])
        self._notify(name, st)

    @staticmethod
    def _model(name, dev):
        try:
            from pymammotion.utility.device_type import DeviceType
            t = DeviceType.value_of_str(name, getattr(dev.mower_state, "product_key", "") or "")
            return {"model": t.get_model(), "camera": bool(t.is_support_video())}
        except Exception:
            return {}

    def map(self, name):
        """Karte für die App: Flächen/Sperrzonen/Wege (GeoJSON der Bibliothek), Mähweg, Fortschritt, Position."""
        if self.api is None or name not in self.names:
            raise KeyError(name)
        dev = self.api.mammotion.get_device_by_name(name)
        if dev is None:
            raise KeyError(name)
        try:
            self.api.mammotion.regenerate_stale_geojson(name)
        except Exception:
            pass

        def pt(p):
            lat, lon = getattr(p, "latitude", None), getattr(p, "longitude", None)
            return {"lat": lat, "lon": lon} if lat and lon and abs(lat) <= 90 and abs(lon) <= 180 else None
        m = dev.map
        return {"areas": m.generated_geojson or None, "path": m.generated_mow_path_geojson or None,
                "progress": m.generated_mow_progress_geojson or None,
                "mower": pt(dev.location.device), "heading": dev.location.orientation,
                "mapped": bool(m.area), "updated": int(time.time()),
                "offset": (self.state.get("offsets") or {}).get(name, {"north": 0.0, "east": 0.0, "rotate": 0.0})}

    def set_offset(self, name, north, east, rotate=0.0):
        """Karten-Verschiebung in Metern (Satellitenbild und RTK-Koordinaten passen oft um einige Meter nicht)."""
        if name not in self.names:
            raise KeyError(name)
        clamp = lambda v: max(-50.0, min(50.0, round(float(v), 2)))
        self.state.setdefault("offsets", {})[name] = {"north": clamp(north), "east": clamp(east),
                                                      "rotate": max(-45.0, min(45.0, round(float(rotate), 1)))}
        self._save()
        return self.state["offsets"][name]

    def list(self):
        return {"configured": self.configured, "error": self.error, "rainGuard": bool(self.state.get("rain_guard")),
                "devices": [self.status[n] for n in self.names if n in self.status]}

    # ---------------------------------------------------------- Steuern
    async def _command(self, name, action, value=None):
        api = self.api
        if action == "start":
            if value:                                       # Aufgabe aus der Mammotion-App
                await api.start_task(name, str(value))
            elif self.status.get(name, {}).get("paused"):
                await api.async_send_command(name, "resume_execute_task")
            else:
                raise ValueError("Bitte eine Aufgabe wählen")
        elif action == "pause":
            await api.async_send_command(name, "pause_execute_task")
        elif action == "resume":
            await api.async_send_command(name, "resume_execute_task")
        elif action == "dock":
            await api.async_send_command(name, "return_to_dock")
        elif action == "cancel":
            await api.async_send_command(name, "cancel_job")
        elif action == "blade":
            await api.async_blade_height(name, int(value))
        else:
            raise ValueError(action)

    def command(self, name, action, value=None):
        if name not in self.names or self.api is None:
            raise KeyError(name)
        self._run(self._command(name, action, value))
        self.loop.call_later(5, lambda: asyncio.ensure_future(self._refresh(name, ask=True)))

    def set_rain_guard(self, on):
        self.state["rain_guard"] = bool(on)
        self._save()

    # ---------------------------------------------------------- Hintergrund + Mitteilungen
    def _poller(self):
        time.sleep(30)
        last_retry = time.time()
        while True:
            # Anmeldung beim Start fehlgeschlagen (z. B. Mammotion-Cloud kurz nicht erreichbar) → alle 10 Min neu
            if self.configured and not self.names and time.time() - last_retry > 600:
                last_retry = time.time()
                print("[MAMMOTION] neuer Anmeldeversuch", flush=True)
                try:
                    self._run(self._connect(restore=True), timeout=120)
                except Exception as e:
                    print(f"[MAMMOTION] Anmelden: {type(e).__name__}", flush=True)
            busy = any(s.get("mowing") or s.get("returning") for s in self.status.values())
            for n in list(self.names):
                try:
                    self._run(self._refresh(n), timeout=90)
                except Exception as e:
                    print(f"[MAMMOTION] Abfrage: {type(e).__name__}", flush=True)
                self._rain_guard(n)
            time.sleep(30 if busy else 120)

    def _rain_guard(self, name):
        st = self.status.get(name) or {}
        if not (self.state.get("rain_guard") and st.get("mowing") and self.rain_soon_fn):
            return
        try:
            soon = self.rain_soon_fn()
        except Exception:
            soon = False
        storm = None
        try:
            storm = self.storm_fn() if self.storm_fn else None
        except Exception:
            pass
        # Nur heimschicken (parken), nie starten – und nur, wenn der Regenschutz in der App eingeschaltet ist
        if (soon or storm) and time.time() - self.state.get("rain_guard_at", 0) > 3600:
            self.state["rain_guard_at"] = time.time()
            self._save()
            try:
                self.command(name, "dock")
                if storm:
                    self._send("⛈ Mähroboter fährt heim", f"{name}: {storm} in der nächsten Stunde – zurück zur Station.")
                else:
                    self._send("🌧 Mähroboter fährt heim", f"{name}: Regen in den nächsten 30 Minuten – zurück zur Station.")
            except Exception as e:
                print(f"[MAMMOTION] Regen-Heimfahrt: {type(e).__name__}", flush=True)

    def _send(self, title, body):
        if self.push is not None and self.push.configured:
            n = self.push.send("mower", title, body)
            print(f"[MAMMOTION] {title} → {n} Gerät(e)", flush=True)

    def _notify(self, name, st):
        prev = self._prev.get(name)
        self._prev[name] = dict(st)
        if prev is None or st.get("online") is False:
            return
        if prev.get("mowing") and st.get("returning"):
            raining = False
            try:
                raining = bool(self.rain_soon_fn and self.rain_soon_fn(now=True))
            except Exception:
                pass
            if raining:
                self._send("🌧 Mähroboter fährt heim", f"{name}: es regnet – zurück zur Station.")
        if (prev.get("mowing") or prev.get("returning")) and st.get("docked") and (prev.get("percent") or 0) >= 95:
            self._send(f"🌱 {name} ist fertig", f"Rasen gemäht ({prev.get('percent')} %).")
        if st.get("error") and st.get("error") != prev.get("error"):
            self._send(f"⚠️ {name} braucht Hilfe", st["error"])
