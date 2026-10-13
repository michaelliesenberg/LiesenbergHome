"""Shelly als Geräte-Quelle – nur im Heimnetz, ohne Cloud und ohne Konto.

Unterstützt (Gen1 = HTTP, Gen2/Gen3/Gen4 = RPC):
  • Relais (Shelly 1/1PM, Plus/Pro 1PM, 2PM als Schalter, Plug S) → kind „light“ (oder „other“, wenn als Gerät markiert)
  • Dimmer (Dimmer 2, Plus Dimmer 0-10V, Pro Dimmer, Wall Dimmer)       → kind „dimmer“
  • Rollladen (2.5/Plus 2PM/Pro 2PM im Cover-Modus)                     → kind „blind“ (0 = offen … 100 = zu)
  • Tor/Tür an einem Relais (als „gate“ markiert)                      → 1-s-Impuls, nur Vollzugriff
  • Leistung (apower / meters.power) je Kanal                          → „watts“ im Zustand
Suche: alle Adressen im eigenen /24-Netz parallel nach GET /shelly abfragen (dauert ~3 s).

Konfiguration (config.json → "shelly"): {"devices": [{"ip", "gen", "model", "name", "channels":
  [{"ch": 0, "kind": "light"|"dimmer"|"blind"|"other", "name", "room", "floor"}]}]}
Gerät-ID in der App: "sh:<ip>:<kanal>" – z. B. "sh:192.168.1.40:0".
"""
import concurrent.futures as cf
import ipaddress
import socket
import threading
import time

import requests

TIMEOUT = 3
# Raum-Symbole der App aus dem Raumnamen (Portugiesisch, Deutsch, Englisch)
_ICONS = {"sala": "LivingRoom", "estar": "LivingRoom", "wohn": "LivingRoom", "living": "LivingRoom",
          "quarto": "Bedroom", "suíte": "Bedroom", "suite": "Bedroom", "schlaf": "Bedroom", "bed": "Bedroom",
          "cozinha": "Kitchen", "küche": "Kitchen", "kitchen": "Kitchen", "banheiro": "Bathroom", "lavabo": "Bathroom",
          "bad": "Bathroom", "bath": "Bathroom", "escritório": "Office", "escritorio": "Office", "büro": "Office",
          "office": "Office", "garagem": "Garage", "garage": "Garage", "jardim": "Garden", "quintal": "Garden",
          "varanda": "Garden", "garten": "Garden", "garden": "Garden", "corredor": "Hallway", "flur": "Hallway",
          "hall": "Hallway", "entrada": "Hallway"}


def _icon(room):
    r = (room or "").lower()
    return next((v for k, v in _ICONS.items() if k in r), "")


def _local_net():
    """Eigenes /24-Netz aus der Adresse, über die das Heimnetz erreicht wird"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))           # nur Routing-Abfrage, es wird nichts gesendet
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ipaddress.ip_network(ip + "/24", strict=False)


def probe(ip, timeout=1.2):
    """GET /shelly → Kurzinfo oder None"""
    try:
        r = requests.get(f"http://{ip}/shelly", timeout=timeout)
        if r.status_code != 200:
            return None
        j = r.json()
    except (requests.RequestException, ValueError):
        return None
    if "gen" in j:                                # Gen2+
        return {"ip": str(ip), "gen": int(j["gen"]), "model": j.get("app") or j.get("model"), "id": j.get("id"),
                "name": j.get("name"), "auth": bool(j.get("auth_en"))}
    if "type" in j:                               # Gen1
        return {"ip": str(ip), "gen": 1, "model": j.get("type"), "id": j.get("mac"), "name": None, "auth": bool(j.get("auth"))}
    return None


def scan(net=None, workers=64):
    net = ipaddress.ip_network(net) if net else _local_net()
    found = []
    with cf.ThreadPoolExecutor(workers) as ex:
        for res in ex.map(probe, [str(h) for h in net.hosts()]):
            if res:
                found.append(res)
    return found


class Device:
    """Ein Shelly – liest Zustand und schaltet je Kanal"""

    def __init__(self, cfg):
        self.ip, self.gen = cfg["ip"], int(cfg.get("gen") or 2)
        self.s = requests.Session()

    def _get(self, path, **params):
        r = self.s.get(f"http://{self.ip}{path}", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json() if r.content else {}

    # ---- Fähigkeiten (für die Einrichtung)
    def channels(self):
        """[(ch, kind)] aus dem aktuellen Zustand ableiten"""
        out = []
        if self.gen == 1:
            st = self._get("/status")
            for i, _ in enumerate(st.get("rollers") or []):
                out.append((i, "blind"))
            if not out:
                for i, _ in enumerate(st.get("lights") or []):
                    out.append((i, "dimmer"))
                for i, _ in enumerate(st.get("relays") or []):
                    out.append((i, "light"))
            return out
        st = self._get("/rpc/Shelly.GetStatus")
        for k in sorted(st):
            comp, _, idx = k.partition(":")
            if not idx.isdigit():
                continue
            kind = {"switch": "light", "light": "dimmer", "cover": "blind"}.get(comp)
            if kind:
                out.append((int(idx), kind))
        return out

    # ---- Zustand
    def status(self):
        """{ch: {on, level, position, watts}}"""
        out = {}
        if self.gen == 1:
            st = self._get("/status")
            meters = st.get("meters") or []
            for i, r in enumerate(st.get("relays") or []):
                out[i] = {"on": bool(r.get("ison")), "watts": (meters[i].get("power") if i < len(meters) else None)}
            for i, l in enumerate(st.get("lights") or []):
                out[i] = {"on": bool(l.get("ison")), "level": l.get("brightness") if l.get("ison") else 0,
                          "watts": (meters[i].get("power") if i < len(meters) else None)}
            for i, r in enumerate(st.get("rollers") or []):
                pos = r.get("current_pos")
                out[i] = {"position": 100 - pos if isinstance(pos, (int, float)) and pos >= 0 else None,
                          "watts": r.get("power")}
            return out
        st = self._get("/rpc/Shelly.GetStatus")
        for k, v in st.items():
            comp, _, idx = k.partition(":")
            if not idx.isdigit() or not isinstance(v, dict):
                continue
            i = int(idx)
            if comp == "switch":
                out[i] = {"on": bool(v.get("output")), "watts": v.get("apower")}
            elif comp == "light":
                out[i] = {"on": bool(v.get("output")), "level": v.get("brightness") if v.get("output") else 0,
                          "watts": v.get("apower")}
            elif comp == "cover":
                pos = v.get("current_pos")
                out[i] = {"position": 100 - pos if isinstance(pos, (int, float)) else None, "watts": v.get("apower"),
                          "moving": v.get("state") in ("opening", "closing")}
        return out

    # ---- Befehle
    def command(self, ch, kind, cmd):
        g1 = self.gen == 1
        if kind == "gate":                               # Tor/Tür: 1-Sekunden-Impuls, Shelly schaltet selbst wieder ab
            if not cmd.get("trigger"):
                raise ValueError("Tor nur auslösen")
            return self._get(f"/relay/{ch}", turn="on", timer=1) if g1 else \
                self._get("/rpc/Switch.Set", id=ch, on="true", toggle_after=1)
        if kind == "blind":
            if cmd.get("position") is not None:
                pos = max(0, min(100, round(100 - float(cmd["position"]))))     # App: 100 = zu, Shelly: 100 = offen
                return self._get(f"/roller/{ch}", go="to_pos", roller_pos=pos) if g1 else \
                    self._get("/rpc/Cover.GoToPosition", id=ch, pos=pos)
            mv = cmd.get("move")
            if mv in ("up", "down", "stop"):
                if g1:
                    return self._get(f"/roller/{ch}", go={"up": "open", "down": "close", "stop": "stop"}[mv])
                return self._get("/rpc/Cover." + {"up": "Open", "down": "Close", "stop": "Stop"}[mv], id=ch)
            raise ValueError("Befehl passt nicht zum Rollladen")
        if kind == "dimmer":
            if cmd.get("level") is not None:
                lvl = max(0, min(100, round(float(cmd["level"]))))
                if g1:
                    return self._get(f"/light/{ch}", turn="on" if lvl else "off", brightness=max(lvl, 1))
                return self._get("/rpc/Light.Set", id=ch, on="true" if lvl else "false", **({"brightness": lvl} if lvl else {}))
            if "on" in cmd:
                return self._get(f"/light/{ch}", turn="on" if cmd["on"] else "off") if g1 else \
                    self._get("/rpc/Light.Set", id=ch, on="true" if cmd["on"] else "false")
            raise ValueError("Befehl passt nicht zum Dimmer")
        if "on" in cmd:                                  # Relais
            return self._get(f"/relay/{ch}", turn="on" if cmd["on"] else "off") if g1 else \
                self._get("/rpc/Switch.Set", id=ch, on="true" if cmd["on"] else "false")
        raise ValueError("Befehl passt nicht zu diesem Gerät")


class ShellyBackend:
    """Geräte-Backend für den Hub (gleiche Schnittstelle wie LuxorBackend/HABackend)"""
    name = "shelly"

    def __init__(self, cfg_fn, home_name_fn=lambda: "Casa"):
        self.cfg_fn, self.home_name_fn = cfg_fn, home_name_fn
        self._devs = {}
        self._cache = {}                # ip → (zeit, status)
        self._lock = threading.Lock()

    def _conf(self):
        return (self.cfg_fn() or {}).get("devices") or []

    def _dev(self, ip, gen):
        d = self._devs.get(ip)
        if d is None or d.gen != gen:
            d = self._devs[ip] = Device({"ip": ip, "gen": gen})
        return d

    def structure(self):
        floors = {}
        for dv in self._conf():
            for c in dv.get("channels") or []:
                if c.get("hidden"):
                    continue
                fl_name = c.get("floor") or "Casa"
                room = c.get("room") or "Sem cômodo"
                fl = floors.setdefault(fl_name, {"id": "f-" + fl_name.lower().replace(" ", "-"), "name": fl_name, "rooms": {}})
                r = fl["rooms"].setdefault(room, {"id": "r-" + room.lower().replace(" ", "-"), "name": room, "icon": _icon(room), "devices": []})
                r["devices"].append({"id": f"sh:{dv['ip']}:{c['ch']}", "name": c.get("name") or dv.get("name") or dv.get("model"),
                                     "kind": c.get("kind") or "light", "restricted": c.get("kind") == "gate"})
        return {"backend": "shelly", "name": self.home_name_fn(),
                "floors": [{"id": f["id"], "name": f["name"], "rooms": list(f["rooms"].values())} for f in floors.values()]}

    def _find(self, did):
        try:
            ip, ch = did[3:].rsplit(":", 1)             # „sh:<ip>:<kanal>“ (ip darf zum Testen „host:port“ sein)
            ch = int(ch)
        except ValueError:
            raise KeyError(did)
        for dv in self._conf():
            if dv["ip"] == ip:
                c = next((c for c in dv.get("channels") or [] if int(c["ch"]) == ch), None)
                if c:
                    return dv, c
        raise KeyError(did)

    def _status(self, dv):
        now = time.time()
        hit = self._cache.get(dv["ip"])
        if hit and now - hit[0] < 2:
            return hit[1]
        st = self._dev(dv["ip"], int(dv.get("gen") or 2)).status()
        self._cache[dv["ip"]] = (now, st)
        return st

    def states(self, ids):
        out, by_ip = {}, {}
        for i in ids:
            try:
                dv, c = self._find(i)
            except KeyError:
                continue
            by_ip.setdefault(dv["ip"], (dv, []))[1].append((i, c))

        def one(item):
            dv, chans = item
            try:
                st = self._status(dv)
            except Exception:
                return {i: {"offline": True} for i, _ in chans}
            res = {}
            for i, c in chans:
                s = dict(st.get(int(c["ch"])) or {})
                k = c.get("kind")
                if k == "light" or k == "other":
                    res[i] = {"on": bool(s.get("on")), **({"watts": s["watts"]} if s.get("watts") is not None else {})}
                elif k == "dimmer":
                    res[i] = {"on": bool(s.get("on")), "level": s.get("level") or 0}
                elif k == "blind":
                    res[i] = {"position": s.get("position") if s.get("position") is not None else 0}
            return res
        with cf.ThreadPoolExecutor(8) as ex:
            for r in ex.map(one, by_ip.values()):
                out.update(r)
        return out

    def command(self, did, cmd):
        dv, c = self._find(did)
        self._dev(dv["ip"], int(dv.get("gen") or 2)).command(int(c["ch"]), c.get("kind") or "light", cmd)
        self._cache.pop(dv["ip"], None)
        return True

    def central_off(self):
        errors = []
        for dv in self._conf():
            for c in dv.get("channels") or []:
                if c.get("kind") in ("light", "dimmer") and not c.get("hidden"):
                    try:
                        self._dev(dv["ip"], int(dv.get("gen") or 2)).command(int(c["ch"]), c["kind"], {"on": False})
                    except Exception as e:
                        errors.append(f"{c.get('name')}: {e}")
            self._cache.pop(dv["ip"], None)
        if errors:
            raise RuntimeError("; ".join(errors[:3]))
        return True

    def power(self):
        """Summe der gemessenen Leistung aller Shelly-Kanäle (W) – Ersatz für den Hausverbrauch ohne evcc"""
        total = 0.0
        for dv in self._conf():
            try:
                for ch, s in self._status(dv).items():
                    total += float(s.get("watts") or 0)
            except Exception:
                pass
        return round(total, 1)
