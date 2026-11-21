"""Vorwerk-Kobold-Saugroboter (VR300/VR200, MyKobold-App): Status, Steuerung, Zonen – über pybotvac (Vorwerk-Cloud).

Anmeldung (einmalig, Besitzer, Einrichtungsseite → Vorwerk Kobold): E-Mail der MyKobold-App eingeben → Vorwerk schickt
einen Code per Mail → Code eingeben. Kein Passwort. Mit dem Anmelde-Token holt der Hub einmal die Roboterliste
(Seriennummer + Geräteschlüssel); gesteuert wird danach mit dem Geräteschlüssel über die Vorwerk-Cloud (nucleo).
Gespeichert wird beides nur auf dem Hub (kobold.json, chmod 600).

Gerät-IDs in der App: „kobold-<Seriennummer>" (so kann der Hub Roborock und Vorwerk in einer Liste zeigen).

Räume: Der VR300 kennt keine Raumaufteilung wie Roborock, nur Zonen, die in der MyKobold-App auf der Karte
eingezeichnet wurden. Die erscheinen hier als „Räume". Pro Start kann der Roboter nur eine Zone reinigen; mehrere
gewählte Zonen fährt der Hub nacheinander ab (nächste Zone, sobald der Roboter wieder in der Station steht).
"""
import json
import os
import threading
import time
from datetime import datetime

import requests

from hub_config import DATA_DIR

STATE_FILE = os.path.join(DATA_DIR, "kobold.json")
CLIENT_ID = "KY4YbVAvtgB7lp8vIbWQ7zLk3hssZlhR"          # MyKobold-App (öffentlich bekannt, siehe pybotvac)
TOKEN_URL = "https://mykobold.eu.auth0.com/oauth/token"
PREFIX = "kobold-"

STATE = {0: "invalid", 1: "idle", 2: "busy", 3: "paused", 4: "error"}
ACTION_DE = {1: "saugt", 2: "Spot-Reinigung", 3: "manuell", 4: "fährt zur Station", 5: "Menü am Gerät",
             6: "Reinigung unterbrochen", 7: "Update", 8: "Logs kopieren", 9: "sucht Position", 10: "Test",
             11: "Zonenreinigung", 12: "erkundet Karte", 13: "sucht Position auf Karte", 14: "erstellt Karte",
             15: "Erkundung unterbrochen"}
ALERT_DE = {
    "ui_alert_dust_bin_full": "Staubbehälter voll", "ui_alert_recovering_location": "sucht seine Position",
    "ui_alert_battery_chargebasecommerr": "Verbindung zur Station gestört", "ui_alert_busy_charging": "lädt noch",
    "ui_alert_charging_base": "steht in der Station", "ui_alert_charging_power": "lädt",
    "ui_alert_connect_chrg_cable": "Ladekabel prüfen", "ui_alert_info_thank_you": "",
    "ui_alert_invalid": "", "ui_alert_old_error": "", "ui_alert_swupdate_fail": "Update fehlgeschlagen",
    "dustbin_full": "Staubbehälter voll", "maint_brush_change": "Bürste wechseln", "maint_filter_change": "Filter wechseln",
    "clean_completed_to_start": "", "nav_floorplan_not_created": "keine Karte",
    "nav_floorplan_load_fail": "Karte nicht geladen", "nav_floorplan_localization_fail": "findet sich auf der Karte nicht",
    "clean_incomplete_to_start": "Reinigung unvollständig", "log_upload_failed": "",
}
ERROR_DE = {
    "ui_error_battery_overtemp": "Akku zu heiß", "ui_error_bin_full": "Staubbehälter voll",
    "ui_error_brush_stuck": "Bürste blockiert", "ui_error_brush_overloaded": "Bürste überlastet",
    "ui_error_bumper_stuck": "Stoßstange klemmt", "ui_error_check_battery_switch": "Akkuschalter prüfen",
    "ui_error_corrupt_scb": "interner Fehler", "ui_error_deck_debris": "Schmutz unter dem Gerät",
    "ui_error_dflt_app": "Fehler – bitte MyKobold-App prüfen", "ui_error_disconnect_chrg_cable": "Ladekabel trennen",
    "ui_error_disconnect_usb_cable": "USB-Kabel trennen", "ui_error_dust_bin_missing": "Staubbehälter fehlt",
    "ui_error_dust_bin_emptied": "Staubbehälter geleert", "ui_error_dust_bin_full": "Staubbehälter voll",
    "ui_error_hardware_failure": "Hardwarefehler", "ui_error_ldrop_stuck": "Rad links hängt",
    "ui_error_lds_jammed": "Laser blockiert", "ui_error_lds_bad_packets": "Laser gestört",
    "ui_error_lds_disconnected": "Laser getrennt", "ui_error_lds_missed_packets": "Laser gestört",
    "ui_error_lwheel_stuck": "Rad links blockiert", "ui_error_navigation_backdrop_frontbump": "kommt nicht weiter",
    "ui_error_navigation_backdrop_leftbump": "kommt nicht weiter", "ui_error_navigation_backdrop_wheelextended": "kommt nicht weiter",
    "ui_error_navigation_noprogress": "kommt nicht weiter", "ui_error_navigation_origin_unclean": "Startpunkt nicht sauber",
    "ui_error_navigation_pathproblems": "findet keinen Weg", "ui_error_navigation_pinkycommsfail": "interner Fehler",
    "ui_error_navigation_falling": "Absturzsensor – Hindernis entfernen", "ui_error_navigation_noexitstogo": "findet keinen Ausgang",
    "ui_error_navigation_nomotioncommands": "interner Fehler", "ui_error_navigation_rightdrop_leftbump": "kommt nicht weiter",
    "ui_error_navigation_undockingfailed": "kommt nicht aus der Station", "ui_error_picked_up": "wurde hochgehoben",
    "ui_error_qa_fail": "interner Fehler", "ui_error_rdrop_stuck": "Rad rechts hängt",
    "ui_error_reconnect_failed": "Verbindung fehlgeschlagen", "ui_error_rwheel_stuck": "Rad rechts blockiert",
    "ui_error_stuck": "steckt fest", "ui_error_unable_to_return_to_base": "findet nicht zurück zur Station",
    "ui_error_unable_to_see": "Sensoren verschmutzt", "ui_error_vacuum_slip": "rutscht",
    "ui_error_vacuum_stuck": "Sauger blockiert", "ui_error_warning": "Warnung",
    "error_check_battery_switch": "Akkuschalter prüfen", "error_dust_bin_missing": "Staubbehälter fehlt",
    "error_dust_bin_full": "Staubbehälter voll",
}
FAN_DE = {"eco": "Eco", "turbo": "Turbo"}
FAN_CODE = {"eco": 1, "turbo": 2}
NAV_DE = {"normal": "Normal", "extra_care": "Extra Care", "deep": "Gründlich"}
NAV_CODE = {"normal": 1, "extra_care": 2, "deep": 3}


def _text(code, table):
    if not code:
        return None
    if code in table:
        return table[code] or None
    return code.replace("ui_error_", "").replace("ui_alert_", "").replace("_", " ")


class Kobold:
    def __init__(self, push=None):
        self.push = push
        self.robots = {}           # duid → pybotvac.Robot
        self.status = {}           # duid → dict für die App (gleiches Format wie roborock_vac)
        self.error = None
        self._pending = None       # (email, session, Zeit)
        self._prev = {}
        self._queue = {}           # duid → noch zu reinigende Zonen (boundary ids)
        self._lock = threading.Lock()
        self.state = self._load()
        if self.state.get("robots"):
            threading.Thread(target=self._connect, daemon=True).start()
        threading.Thread(target=self._poller, daemon=True).start()

    # ---------------------------------------------------------- Infrastruktur
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
        return bool(self.state.get("token"))

    def owns(self, duid):
        return str(duid).startswith(PREFIX)

    # ---------------------------------------------------------- Anmeldung (E-Mail-Code)
    def request_code(self, email):
        from pybotvac import PasswordlessSession, Vorwerk
        email = (email or "").strip()
        if "@" not in email:
            raise ValueError("Bitte die E-Mail-Adresse der MyKobold-App eingeben")
        s = PasswordlessSession(client_id=CLIENT_ID, vendor=Vorwerk())
        s.send_email_otp(email)
        self._pending = (email, s, time.time())

    def login(self, code):
        if not self._pending or time.time() - self._pending[2] > 15 * 60:
            raise ValueError("Bitte zuerst den Code anfordern (gilt 15 Minuten)")
        email, s, _ = self._pending
        code = "".join(ch for ch in str(code) if ch.isdigit())
        s.fetch_token_passwordless(email, code)
        self.state = {"email": email, "token": s._token, "auto_away": self.state.get("auto_away", False),
                      "fan": self.state.get("fan", {}), "nav": self.state.get("nav", {})}
        self._pending = None
        self._discover()
        self._save()
        if not self.state.get("robots"):
            raise ValueError(self.error or "Kein Roboter im MyKobold-Konto gefunden")

    def logout(self):
        self.robots, self.status, self.state, self._queue = {}, {}, {}, {}
        try:
            os.remove(STATE_FILE)
        except OSError:
            pass

    # ---------------------------------------------------------- Token + Roboterliste (nur hierfür braucht es das Token)
    def _session(self):
        from pybotvac import PasswordlessSession, Vorwerk
        return PasswordlessSession(token=self.state.get("token"), client_id=CLIENT_ID, vendor=Vorwerk())

    def _refresh_token(self):
        rt = (self.state.get("token") or {}).get("refresh_token")
        if not rt:
            raise RuntimeError("Anmeldung abgelaufen – bitte auf der Einrichtungsseite neu anmelden")
        r = requests.post(TOKEN_URL, json={"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": rt},
                          timeout=15)
        r.raise_for_status()
        tok = r.json()
        tok.setdefault("refresh_token", rt)
        self.state["token"] = tok
        self._save()

    def _account_call(self, fn):
        """fn(Account) mit gültigem Token; bei abgelaufenem Token einmal erneuern"""
        from pybotvac import Account
        try:
            return fn(Account(self._session()))
        except Exception:
            self._refresh_token()
            return fn(Account(self._session()))

    def _discover(self):
        """Roboter (Seriennummer, Schlüssel, Server) und Zonen aus dem Konto holen und merken"""
        def go(acc):
            resp = acc._session.get("users/me/robots").json()
            maps = {}
            for r in resp:
                try:
                    pm = acc._session.get(f"users/me/robots/{r['serial']}/persistent_maps").json()
                    maps[r["serial"]] = [m.get("id") for m in pm if m.get("id")]
                except Exception:
                    maps[r["serial"]] = []
            return resp, maps
        try:
            robots, maps = self._account_call(go)
        except Exception as e:
            self.error = f"Vorwerk: {e}"
            print(f"[KOBOLD] Roboterliste: {e}", flush=True)
            return
        self.state["robots"] = [{"serial": r["serial"], "secret": r["secret_key"], "name": r.get("name"),
                                 "model": r.get("model"), "traits": r.get("traits") or [],
                                 "nucleo": r.get("nucleo_url") or "https://nucleo.ksecosys.com:4443",
                                 "maps": maps.get(r["serial"], [])} for r in robots]
        self.error = None if robots else "Kein Roboter im MyKobold-Konto gefunden"
        self._save()
        self._connect()

    def _connect(self):
        from pybotvac import Robot, Vorwerk
        robots = {}
        for r in self.state.get("robots") or []:
            duid = PREFIX + r["serial"]
            try:
                robots[duid] = Robot(serial=r["serial"], secret=r["secret"], traits=r["traits"], vendor=Vorwerk(),
                                     name=r.get("name") or "Kobold", endpoint=r["nucleo"],
                                     has_persistent_maps=bool(r.get("maps")))
            except Exception as e:
                print(f"[KOBOLD] {r.get('name')}: {e}", flush=True)
                self.status[duid] = {"id": duid, "name": r.get("name") or "Kobold", "online": False,
                                     "stateText": "nicht erreichbar", "error": str(e)[:120], "kind": "kobold"}
        self.robots = robots
        for duid in robots:
            self._load_zones(duid)
            self._refresh(duid)

    def _load_zones(self, duid):
        """Zonen aus der MyKobold-Karte als „Räume" (id = laufende Nummer, die App erwartet Zahlen)"""
        rec = self._rec(duid)
        zones = []
        for map_id in rec.get("maps") or []:
            try:
                data = self.robots[duid].get_map_boundaries(map_id).json().get("data") or {}
                for b in data.get("boundaries") or []:
                    if b.get("type") == "polygon" and b.get("name") and b.get("enabled", True):
                        zones.append({"bid": b["id"], "map": map_id, "name": b["name"]})
            except Exception as e:
                print(f"[KOBOLD] Zonen: {e}", flush=True)
        if zones or "zones" not in rec:
            rec["zones"] = zones
            self._save()

    def _rec(self, duid):
        serial = duid[len(PREFIX):]
        return next((r for r in self.state.get("robots") or [] if r["serial"] == serial), {})

    # ---------------------------------------------------------- Status
    def _refresh(self, duid):
        rob = self.robots[duid]
        rec = self._rec(duid)
        st = self.status.setdefault(duid, {"id": duid})
        try:
            s = rob.get_robot_state().json()
        except Exception as e:
            st.update(id=duid, name=rec.get("name") or "Kobold", online=False, stateText="nicht erreichbar",
                      error=None, kind="kobold")
            print(f"[KOBOLD] Status: {e}", flush=True)
            return
        state = STATE.get(s.get("state"), "invalid")
        action = s.get("action") or 0
        det = s.get("details") or {}
        cleaning = state in ("busy", "paused") and action not in (5, 7, 8, 10)
        docked = bool(det.get("isDocked"))
        err = s.get("error") if state == "error" else None      # außerhalb von „Fehler" meldet die Cloud oft alte Fehler
        err_text = _text(err, ERROR_DE) if err else None
        if state == "error" and not err_text:
            err_text = "Fehler – bitte am Roboter nachsehen"
        alert = _text(s.get("alert"), ALERT_DE)
        if state == "busy":
            text = ACTION_DE.get(action, "aktiv")
        elif state == "paused":
            text = "pausiert"
        elif state == "error":
            text = "Fehler"
        elif det.get("isCharging"):
            text = "lädt"
        elif docked:
            text = "voll geladen" if (det.get("charge") or 0) >= 100 else "in der Station"
        else:
            text = "bereit"
        fan = (self.state.get("fan") or {}).get(duid, "eco")
        nav = (self.state.get("nav") or {}).get(duid, "normal")
        meta = s.get("meta") or {}
        ver = ((s.get("availableServices") or {}).get("houseCleaning") or "")
        st.update(
            id=duid, kind="kobold", name=rec.get("name") or "Kobold",
            model=rec.get("model") or meta.get("modelName") or "Vorwerk Kobold",
            online=True, local=False, via="Vorwerk-Cloud",
            state="paused" if state == "paused" else (("cleaning" if action not in (4,) else "returning_home") if state == "busy"
                                                     else ("error" if state == "error" else ("charging" if docked else "idle"))),
            stateText=text, cleaning=cleaning, docked=docked, battery=det.get("charge"),
            error=err_text, dockError=None, notice=alert,
            fan=fan, fanText=FAN_DE[fan],
            fanOptions=[{"id": k, "text": v} for k, v in FAN_DE.items()] if ver != "minimal-2" else [],
            mode=nav, modeText=NAV_DE[nav],
            modeOptions=[{"id": k, "text": v} for k, v in NAV_DE.items()] if ver in ("basic-3", "basic-4", "basic-2") else [],
            rooms=[{"id": i + 1, "name": z["name"]} for i, z in enumerate(rec.get("zones") or [])],
            scheduleOn=bool(det.get("isScheduleEnabled")),
            features=["find", "rooms", "schedule"],
            updated=int(time.time()))
        self._next_zone(duid, st)
        self._notify(duid, st)

    def list(self):
        return {"configured": self.configured, "error": self.error, "autoAway": bool(self.state.get("auto_away")),
                "devices": sorted(self.status.values(), key=lambda s: s.get("name") or "")}

    def refresh_now(self):
        for duid in list(self.robots):
            try:
                self._refresh(duid)
            except Exception:
                pass

    # ---------------------------------------------------------- Steuern
    def _start(self, duid, zone=None):
        rob = self.robots[duid]
        fan = FAN_CODE[(self.state.get("fan") or {}).get(duid, "eco")]
        nav = NAV_CODE[(self.state.get("nav") or {}).get(duid, "normal")]
        if zone:
            r = rob.start_cleaning(mode=fan, navigation_mode=nav, category=4, boundary_id=zone["bid"], map_id=zone["map"])
        else:
            r = rob.start_cleaning(mode=fan, navigation_mode=nav)
        res = r.json()
        if res.get("result") not in ("ok", None):
            raise RuntimeError(_text(res.get("alert"), ALERT_DE) or res.get("result"))

    def command(self, duid, action, value=None):
        if duid not in self.robots:
            raise KeyError(duid)
        rob = self.robots[duid]
        st = self.status.get(duid) or {}
        with self._lock:
            if action == "start":
                self._queue.pop(duid, None)
                if st.get("state") == "paused":
                    rob.resume_cleaning()
                else:
                    self._start(duid)
            elif action == "pause":
                rob.pause_cleaning()
            elif action == "stop":
                self._queue.pop(duid, None)
                rob.stop_cleaning()
            elif action == "dock":
                self._queue.pop(duid, None)
                if st.get("cleaning"):
                    try:
                        rob.pause_cleaning()
                        time.sleep(2)
                    except Exception:
                        pass
                rob.send_to_base()
            elif action == "find":
                rob.locate()
            elif action == "rooms":
                zones = self._rec(duid).get("zones") or []
                ids = [int(x) for x in (value or {}).get("rooms", [])]
                picked = [zones[i - 1] for i in ids if 1 <= i <= len(zones)]
                if not picked:
                    raise ValueError("Keine Zone gewählt")
                picked = picked * max(1, min(3, int((value or {}).get("repeat", 1))))
                self._start(duid, picked[0])
                self._queue[duid] = {"zones": picked[1:], "started": time.time()}
            elif action == "fan":
                if value not in FAN_CODE:
                    raise ValueError("Unbekannte Saugstufe")
                self.state.setdefault("fan", {})[duid] = value
                self._save()
            elif action == "mode":
                if value not in NAV_CODE:
                    raise ValueError("Unbekannter Reinigungsmodus")
                self.state.setdefault("nav", {})[duid] = value
                self._save()
            elif action == "schedule":
                rob.enable_schedule() if value else rob.disable_schedule()
            elif action == "water":                  # Zeitpläne setzen ggf. Wassermenge – der Kobold wischt nicht
                return
            elif action == "zones-reload":
                self._load_zones(duid)
            else:
                raise ValueError("Bei diesem Roboter nicht möglich")
        try:
            self._refresh(duid)
        except Exception:
            pass

    def _next_zone(self, duid, st):
        """Mehrere Zonen nacheinander: nächste starten, sobald der Roboter wieder in der Station steht"""
        q = self._queue.get(duid)
        if not q:
            return
        if not q["zones"] or st.get("error"):
            self._queue.pop(duid, None)
            return
        if time.time() - q["started"] > 90 and st.get("docked") and not st.get("cleaning"):
            z = q["zones"].pop(0)
            q["started"] = time.time()
            try:
                self._start(duid, z)
                print(f"[KOBOLD] nächste Zone: {z['name']}", flush=True)
            except Exception as e:
                print(f"[KOBOLD] Zone {z['name']}: {e}", flush=True)
                self._queue.pop(duid, None)

    def map(self, duid, fresh=False):
        raise ValueError("Für den Vorwerk Kobold gibt es keine Kartenansicht")

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
        for duid, st in self.status.items():
            if st.get("docked") and not st.get("error") and (st.get("battery") or 0) >= 50:
                try:
                    self.command(duid, "start")
                    self.state["auto_away_last"] = today
                    self._save()
                    self._send(f"🧹 {st.get('name') or 'Kobold'} startet", "Alle sind weg – es wird gesaugt.")
                except Exception as e:
                    print(f"[KOBOLD] Auto-Start: {e}", flush=True)

    # ---------------------------------------------------------- Hintergrund + Mitteilungen
    def _poller(self):
        time.sleep(20)
        last_zones = time.time()
        while True:
            busy = any(s.get("cleaning") for s in self.status.values())
            for duid in list(self.robots):
                try:
                    self._refresh(duid)
                except Exception as e:
                    print(f"[KOBOLD] Abfrage: {e}", flush=True)
            if self.robots and time.time() - last_zones > 6 * 3600:      # neue Zonen aus der MyKobold-App übernehmen
                last_zones = time.time()
                for duid in list(self.robots):
                    self._load_zones(duid)
            time.sleep(20 if busy else 60)

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
        if prev.get("cleaning") and not st.get("cleaning") and st.get("docked") and not st.get("error") \
                and not (self._queue.get(duid) or {}).get("zones"):
            self._send(f"🧹 {name} ist fertig", "Zurück in der Station")
        if st.get("error") and st.get("error") != prev.get("error"):
            self._send(f"⚠️ {name} braucht Hilfe", st["error"])
        if st.get("notice") == "Staubbehälter voll" and prev.get("notice") != st.get("notice"):
            self._send(f"🗑️ {name}: Staubbehälter voll", "Bitte den Staubbehälter leeren.")
