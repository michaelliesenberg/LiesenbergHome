"""Einheitliches Gerätemodell für die App – egal ob LUXORliving oder Home Assistant dahinter steckt.

Struktur (GET /api/home):
  {"name": "Mein Zuhause", "backend": "luxor",
   "floors": [{"id", "name", "rooms": [{"id", "name", "icon", "devices": [
        {"id": "lx:…" | "ha:light.kueche", "name", "kind", "restricted": bool}]}]}]}

kind:
  light   – an/aus                    Zustand {on}
  dimmer  – an/aus + Helligkeit       Zustand {on, level 0–100}
  blind   – Rollladen/Jalousie         Zustand {position 0 = offen … 100 = zu}
  heating – Raumtemperatur             Zustand {current, target}
  gate    – Tor/Tür (Impuls)          kein Zustand, nur „auslösen"
  other   – nur anzeigen

Befehle (POST /api/home/device/{id}):
  {"on": true} · {"level": 40} · {"position": 100} · {"move": "up"|"down"|"stop"} ·
  {"target": 21.5} · {"trigger": true}
"""
import copy
import json
import os
import re

import lxp
from hub_config import DATA_DIR

ROOM_NAMES_FILE = os.environ.get("ROOM_NAMES_FILE", os.path.join(DATA_DIR, "room-names.json"))
RESTRICT_FILE = os.path.join(DATA_DIR, "restricted.json")     # {"add": [ids], "remove": [ids]} vom Besitzer
LXP_FILE = os.environ.get("LXP_FILE", os.path.join(DATA_DIR, "project.lxp"))

GATE_WORDS = re.compile(r"\b(tor|tür|tuer|door|gate|garage|pforte|einfahrt|schloss|lock)\b", re.I)


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def room_names():
    return _read_json(ROOM_NAMES_FILE, {})


def restrictions():
    r = _read_json(RESTRICT_FILE, {})
    return set(r.get("add", [])), set(r.get("remove", []))


def apply_overrides(structure):
    """Eigene Raumnamen und vom Besitzer gesetzte „nur Vollzugriff"-Markierungen anwenden."""
    s = copy.deepcopy(structure)
    names = room_names()
    add, rem = restrictions()
    for fl in s["floors"]:
        for r in fl["rooms"]:
            r["name"] = names.get(r["name"], r["name"])
            for d in r["devices"]:
                if d["id"] in add:
                    d["restricted"] = True
                if d["id"] in rem:
                    d["restricted"] = False
    return s


def device_index(structure):
    return {d["id"]: d for fl in structure["floors"] for r in fl["rooms"] for d in r["devices"]}


# ====================================================================== LUXORliving
class LuxorBackend:
    name = "luxor"

    def __init__(self, luxor):
        self.luxor = luxor          # Verbindung zum IP1 (lesen/schreiben, nacheinander)
        self._cache = {"mtime": None, "project": None}

    # ---- Struktur
    def project(self):
        """Das alte .lxp-Format (für ältere App-Versionen und intern)."""
        mtime = os.path.getmtime(LXP_FILE)          # FileNotFoundError → „noch kein Projekt"
        if self._cache["mtime"] != mtime:
            data = lxp.parse(LXP_FILE)
            data["importedAt"] = mtime - lxp.APPLE_EPOCH
            self._cache.update(mtime=mtime, project=data)
        return self._cache["project"]

    def _functions(self):
        p = self.project()
        return {"lx:" + f["id"]: f for fl in p["floors"] for r in fl["rooms"] for f in r["functions"]}

    def structure(self):
        p = self.project()
        floors = []
        for fl in p["floors"]:
            rooms = []
            for r in fl["rooms"]:
                devs = []
                for f in r["functions"]:
                    kind = "gate" if f.get("pulse") else f["kind"]
                    devs.append({"id": "lx:" + f["id"], "name": f["name"], "kind": kind,
                                 "restricted": kind == "gate"})
                rooms.append({"id": r["id"], "name": r["name"], "icon": r.get("icon", ""), "devices": devs})
            floors.append({"id": fl["id"], "name": fl["name"], "rooms": rooms})
        return {"backend": "luxor", "floors": floors}

    # ---- Zustand
    @staticmethod
    def _reads(f):
        dp = f["datapoints"]
        k = f["kind"]
        if f.get("pulse"):
            return {}
        if k == "dimmer":
            return {"level": dp.get("Status%") or dp.get("Dimmen%"), "on": dp.get("StatusOnOff")}
        if k == "light":
            return {"on": dp.get("StatusOnOff") or dp.get("SchaltenOnOff") or dp.get("OnOff")}
        if k == "blind":
            return {"position": dp.get("StatusHöhe%") or dp.get("Höhe%")}
        if k == "heating":
            return {"current": dp.get("Istwert"), "target": dp.get("StatusSollwert") or dp.get("Sollwert")}
        return {}

    def states(self, ids):
        funcs = self._functions()
        out = {}
        for i in ids:
            f = funcs.get(i)
            if not f:
                continue
            st = {}
            for field, dpid in self._reads(f).items():
                if dpid is None:
                    continue
                v = self.luxor.get(lxp_rest(dpid))
                if v is None:
                    continue
                if field == "on":
                    st["on"] = bool(v)
                elif field in ("level", "position"):
                    st[field] = round(float(v) * 100 / 255)
                else:
                    st[field] = round(float(v), 1)
            if f["kind"] == "dimmer" and "level" in st:
                st["on"] = st["level"] > 0
            if st:
                out[i] = st
        return out

    # ---- Schalten
    @staticmethod
    def writes(f, cmd):
        """Befehl → Liste (Datenpunkt, Wert)."""
        dp, kind = f["datapoints"], f["kind"]
        on_off = dp.get("SchaltenOnOff") or dp.get("OnOff")
        if cmd.get("trigger"):
            return [(on_off, True)] if on_off else []
        if kind == "dimmer":
            lvl = cmd.get("level")
            if cmd.get("on") is False or lvl == 0:
                return [(on_off, False)] if on_off else []
            if lvl is not None and dp.get("Dimmen%"):
                return [(dp["Dimmen%"], int(round(max(1, min(100, float(lvl))) * 255 / 100)))]
            if cmd.get("on") is True:
                return [(on_off, True)] if on_off else []
            return []
        if kind == "light":
            if "on" in cmd:
                return [(on_off, bool(cmd["on"]))] if on_off else []
            if cmd.get("level") is not None:
                return [(on_off, float(cmd["level"]) > 0)] if on_off else []
            return []
        if kind == "blind":
            if cmd.get("move") in ("up", "down") and dp.get("UpDown"):
                return [(dp["UpDown"], cmd["move"] == "down")]          # KNX: 0 = auf, 1 = ab
            if cmd.get("move") == "stop" and dp.get("StepStop"):
                return [(dp["StepStop"], False)]
            if cmd.get("position") is not None and dp.get("Höhe%"):
                return [(dp["Höhe%"], int(round(max(0, min(100, float(cmd["position"]))) * 255 / 100)))]
            return []
        if kind == "heating" and cmd.get("target") is not None and dp.get("Sollwert"):
            return [(dp["Sollwert"], float(cmd["target"]))]
        return []

    def command(self, did, cmd):
        f = self._functions().get(did)
        if not f:
            raise KeyError(did)
        ws = self.writes(f, cmd)
        if not ws:
            raise ValueError("Befehl passt nicht zu diesem Gerät")
        for dpid, val in ws:
            if not self.luxor.set(lxp_rest(dpid), val):
                raise RuntimeError(f"LUXORliving hat den Befehl abgelehnt ({self.luxor.last_write})")
        return True

    def central_off(self):
        # IP1 „Zentral ein/aus" (Datenpunkt 14) – wie der Zentral-aus-Taster in LUXORliving
        if not self.luxor.set(14, False):
            raise RuntimeError(f"Zentral aus abgelehnt ({self.luxor.last_write})")
        return True


def lxp_rest(dp):
    """IP1-REST-IDs gehen bis ~1000; größere Zahlen sind KNX-Gruppenadressen."""
    return dp if dp < 2048 else lxp.rest_id(dp)


# ====================================================================== Demo (für Apple-Prüfung & Ausprobieren)
class DemoBackend:
    """Ein erfundenes Haus mit Speicher im RAM – zeigt alle Funktionen ohne echte Geräte."""
    name = "demo"

    def __init__(self):
        def dev(i, n, k, r=False):
            return {"id": "demo:" + i, "name": n, "kind": k, "restricted": r}
        self._s = {"backend": "demo", "floors": [
            {"id": "og", "name": "Obergeschoss", "rooms": [
                {"id": "schlafen", "name": "Schlafzimmer", "icon": "Bedroom", "devices": [
                    dev("sz-decke", "Deckenlicht", "dimmer"), dev("sz-lese", "Leselampe", "light"),
                    dev("sz-fenster", "Fenster", "blind"), dev("sz-heizung", "Heizung", "heating")]},
                {"id": "bad", "name": "Bad", "icon": "Bathroom", "devices": [
                    dev("bad-spiegel", "Spiegel", "dimmer"), dev("bad-heizung", "Fußboden", "heating")]}]},
            {"id": "eg", "name": "Erdgeschoss", "rooms": [
                {"id": "wohnen", "name": "Wohnzimmer", "icon": "LivingRoom", "devices": [
                    dev("wz-spots", "Spots", "dimmer"), dev("wz-stehlampe", "Stehlampe", "light"),
                    dev("wz-fenster", "Terrasse", "blind"), dev("wz-heizung", "Heizung", "heating")]},
                {"id": "kueche", "name": "Küche", "icon": "Kitchen", "devices": [
                    dev("k-insel", "Kochinsel", "dimmer"), dev("k-arbeit", "Arbeitsplatte", "light")]},
                {"id": "garage", "name": "Garage", "icon": "Garage", "devices": [
                    dev("g-tor", "Garagentor", "gate", True), dev("g-licht", "Licht", "light")]}]}]}
        self._st = {"demo:sz-heizung": {"current": 21.4, "target": 21.0},
                    "demo:bad-heizung": {"current": 23.1, "target": 23.0},
                    "demo:wz-heizung": {"current": 21.9, "target": 21.5},
                    "demo:wz-spots": {"on": True, "level": 60}, "demo:k-insel": {"on": True, "level": 100},
                    "demo:wz-fenster": {"position": 0}, "demo:sz-fenster": {"position": 100}}

    def structure(self):
        return copy.deepcopy(self._s)

    def states(self, ids):
        idx = device_index(self._s)
        out = {}
        for i in ids:
            d = idx.get(i)
            if not d or d["kind"] in ("gate", "other"):
                continue
            base = {"light": {"on": False}, "dimmer": {"on": False, "level": 0}, "blind": {"position": 0},
                    "heating": {"current": 21.0, "target": 21.0}}[d["kind"]]
            out[i] = {**base, **self._st.get(i, {})}
        return out

    def command(self, did, cmd):
        d = device_index(self._s).get(did)
        if not d:
            raise KeyError(did)
        st = self._st.setdefault(did, {})
        if "on" in cmd:
            st["on"] = bool(cmd["on"])
            if d["kind"] == "dimmer":
                st["level"] = 100 if cmd["on"] and not st.get("level") else (st.get("level", 0) if cmd["on"] else 0)
        if cmd.get("level") is not None:
            st["level"] = float(cmd["level"]); st["on"] = st["level"] > 0
        if cmd.get("position") is not None:
            st["position"] = float(cmd["position"])
        if cmd.get("move") in ("up", "down"):
            st["position"] = 0 if cmd["move"] == "up" else 100
        if cmd.get("target") is not None:
            st["target"] = float(cmd["target"])
        return True

    def central_off(self):
        for d in device_index(self._s).values():
            if d["kind"] in ("light", "dimmer"):
                self._st.setdefault(d["id"], {}).update(on=False, level=0)
        return True
