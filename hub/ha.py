"""Home Assistant als Geräte-Quelle: Etagen → Bereiche → Geräte, Zustände und Befehle über die REST-API.

Benötigt die HA-Adresse (z. B. http://homeassistant.local:8123) und einen
„Langlebigen Zugriffstoken" (HA → Profil → Sicherheit).
"""
import json
import threading
import time

import requests

# Etagen + Bereiche + Entitäten in EINER Abfrage (Template-API)
_STRUCTURE_TEMPLATE = """
{%- set ns = namespace(floors=[], used=[]) -%}
{%- for f in floors() -%}
  {%- set rs = namespace(rooms=[]) -%}
  {%- for a in floor_areas(f) -%}
    {%- set rs.rooms = rs.rooms + [{"id": a, "name": area_name(a), "entities": area_entities(a)}] -%}
    {%- set ns.used = ns.used + [a] -%}
  {%- endfor -%}
  {%- set ns.floors = ns.floors + [{"id": f, "name": floor_name(f), "rooms": rs.rooms}] -%}
{%- endfor -%}
{%- set rest = namespace(rooms=[]) -%}
{%- for a in areas() if a not in ns.used -%}
  {%- set rest.rooms = rest.rooms + [{"id": a, "name": area_name(a), "entities": area_entities(a)}] -%}
{%- endfor -%}
{{ {"floors": ns.floors, "other": rest.rooms} | to_json }}
"""

DOMAINS = ("light", "switch", "cover", "climate", "lock")
_ICON_HINTS = {"schlaf": "Bedroom", "bed": "Bedroom", "bad": "Bathroom", "bath": "Bathroom", "küche": "Kitchen",
               "kitchen": "Kitchen", "wohn": "LivingRoom", "living": "LivingRoom", "garage": "Garage",
               "büro": "Office", "office": "Office", "flur": "Hallway", "garten": "Garden", "garden": "Garden"}


class HABackend:
    name = "homeassistant"

    def __init__(self, url, token):
        self.url = (url or "").rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self._states = {"at": 0, "data": {}}
        self._struct = {"at": 0, "data": None}
        self._lock = threading.Lock()

    # ---- Grundlagen
    def _get(self, path):
        r = self.s.get(self.url + path, timeout=10)
        if r.status_code == 401:
            raise PermissionError("Home Assistant: Zugriffstoken ungültig")
        r.raise_for_status()
        return r.json()

    def _call(self, domain, service, data):
        r = self.s.post(f"{self.url}/api/services/{domain}/{service}", json=data, timeout=10)
        if r.status_code >= 400:
            raise RuntimeError(f"Home Assistant: {domain}.{service} → HTTP {r.status_code} {r.text[:120]}")
        self._states["at"] = 0          # nächste Abfrage frisch
        return True

    def test(self):
        return self._get("/api/").get("message", "ok")

    def _all_states(self, max_age=2.0):
        with self._lock:
            if time.time() - self._states["at"] > max_age:
                self._states = {"at": time.time(), "data": {s["entity_id"]: s for s in self._get("/api/states")}}
            return self._states["data"]

    # ---- Struktur
    @staticmethod
    def _kind(eid, st):
        domain = eid.split(".")[0]
        attrs = st.get("attributes", {}) if st else {}
        if domain == "light":
            modes = attrs.get("supported_color_modes") or []
            return "dimmer" if any(m != "onoff" for m in modes) or "brightness" in attrs else "light"
        if domain == "switch":
            return "light"
        if domain == "cover":
            return "gate" if attrs.get("device_class") in ("garage", "gate", "door") else "blind"
        if domain == "climate":
            return "heating"
        if domain == "lock":
            return "gate"
        return "other"

    def structure(self, max_age=60):
        if self._struct["data"] and time.time() - self._struct["at"] < max_age:
            return self._struct["data"]
        r = self.s.post(self.url + "/api/template", json={"template": _STRUCTURE_TEMPLATE}, timeout=15)
        if r.status_code == 401:
            raise PermissionError("Home Assistant: Zugriffstoken ungültig")
        r.raise_for_status()
        raw = json.loads(r.text)
        states = self._all_states(max_age=0)

        def room(a):
            devs = []
            for eid in a["entities"]:
                if eid.split(".")[0] not in DOMAINS or eid not in states:
                    continue
                st = states[eid]
                if st.get("attributes", {}).get("hidden"):
                    continue
                kind = self._kind(eid, st)
                devs.append({"id": "ha:" + eid, "name": st.get("attributes", {}).get("friendly_name", eid),
                             "kind": kind, "restricted": kind == "gate"})
            lname = (a["name"] or "").lower()
            icon = next((v for k, v in _ICON_HINTS.items() if k in lname), "")
            return {"id": a["id"], "name": a["name"] or a["id"], "icon": icon, "devices": devs}

        floors = [{"id": f["id"], "name": f["name"], "rooms": [room(a) for a in f["rooms"]]} for f in raw["floors"]]
        if raw["other"]:
            floors.append({"id": "_other", "name": "Weitere Räume" if floors else "Räume",
                           "rooms": [room(a) for a in raw["other"]]})
        # Räume ohne steuerbare Geräte weglassen
        for f in floors:
            f["rooms"] = [r for r in f["rooms"] if r["devices"]]
        data = {"backend": "homeassistant", "floors": [f for f in floors if f["rooms"]]}
        self._struct = {"at": time.time(), "data": data}
        return data

    # ---- Zustand
    def states(self, ids):
        all_st = self._all_states()
        out = {}
        for i in ids:
            eid = i[3:] if i.startswith("ha:") else i
            st = all_st.get(eid)
            if not st or st["state"] in ("unavailable", "unknown"):
                continue
            a = st.get("attributes", {})
            d = eid.split(".")[0]
            if d in ("light", "switch"):
                v = {"on": st["state"] == "on"}
                if d == "light" and a.get("brightness") is not None:
                    v["level"] = round(a["brightness"] * 100 / 255)
                elif d == "light" and st["state"] == "off":
                    v["level"] = 0
                out[i] = v
            elif d == "cover":
                if a.get("current_position") is not None:
                    out[i] = {"position": 100 - int(a["current_position"])}   # HA: 100 = offen
                else:
                    out[i] = {"position": 0 if st["state"] == "open" else 100}
            elif d == "climate":
                out[i] = {"current": a.get("current_temperature"), "target": a.get("temperature")}
        return out

    # ---- Schalten
    def command(self, did, cmd):
        eid = did[3:] if did.startswith("ha:") else did
        d = eid.split(".")[0]
        base = {"entity_id": eid}
        if d == "light":
            if cmd.get("on") is False or cmd.get("level") == 0:
                return self._call("light", "turn_off", base)
            if cmd.get("level") is not None:
                return self._call("light", "turn_on", {**base, "brightness_pct": max(1, min(100, round(float(cmd["level"]))))})
            return self._call("light", "turn_on", base)
        if d == "switch":
            return self._call("switch", "turn_on" if cmd.get("on", True) else "turn_off", base)
        if d == "cover":
            if cmd.get("trigger"):
                return self._call("cover", "toggle", base)
            mv = cmd.get("move")
            if mv in ("up", "down", "stop"):
                return self._call("cover", {"up": "open_cover", "down": "close_cover", "stop": "stop_cover"}[mv], base)
            if cmd.get("position") is not None:
                return self._call("cover", "set_cover_position", {**base, "position": 100 - round(float(cmd["position"]))})
        if d == "climate" and cmd.get("target") is not None:
            return self._call("climate", "set_temperature", {**base, "temperature": float(cmd["target"])})
        if d == "lock" and (cmd.get("trigger") or "on" in cmd):
            return self._call("lock", "unlock" if cmd.get("trigger") or cmd.get("on") else "lock", base)
        raise ValueError("Befehl passt nicht zu diesem Gerät")

    def central_off(self):
        lights = [d["id"][3:] for f in self.structure()["floors"] for r in f["rooms"] for d in r["devices"]
                  if d["id"].startswith("ha:light.")]
        if lights:
            self._call("light", "turn_off", {"entity_id": lights})
        return True
