"""Live Activities (Sperrbildschirm + Dynamic Island) für laufende Geräte – vom Hub per APNs gestartet.

Ablauf: Die App meldet ihr „Push-to-Start"-Token (/api/liveactivity/register). Startet ein Gerät (Spül-/Waschmaschine,
Trockner, Backofen über Home Connect/Miele, Saugroboter, Auto an der Wallbox), schickt der Hub „start" mit Restzeit –
die Anzeige zählt dann selbst herunter. Für jede laufende Activity meldet die App ein Aktualisierungs-Token; darüber
kommen Fortschritt (höchstens alle 5 Min.) und am Ende „fertig". Nur Anzeige – der Hub schaltet hier nichts.
"""
import json
import os
import threading
import time

from hub_config import DATA_DIR

FILE = os.path.join(DATA_DIR, "liveactivity.json")
TOPIC = "biz.liesenberg.home.push-type.liveactivity"
KIND = {"Dishwasher": "dishwasher", "Washer": "washer", "WasherDryer": "washer", "Dryer": "dryer", "Oven": "oven"}


class LiveActivities:
    def __init__(self, push, sources):
        """sources: Funktion → Liste aktiver Geräte [{id, kind, name, status, detail, progress, remaining}]"""
        self.push, self.sources = push, sources
        self._lock = threading.Lock()
        d = self._load()
        self.start_tokens = d.get("start", {})        # Token → env
        self.update_tokens = d.get("update", {})      # Geräte-ID → {token, env}
        self.active = {}                              # Geräte-ID → {start, end, sent}
        threading.Thread(target=self._loop, daemon=True).start()

    def _load(self):
        try:
            with open(FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        with self._lock:
            tmp = FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"start": self.start_tokens, "update": self.update_tokens}, f)
            os.replace(tmp, FILE)

    def register(self, body):
        if body.get("startToken"):
            self.start_tokens[body["startToken"]] = body.get("env", "production")
        if body.get("updateToken") and body.get("deviceID"):
            self.update_tokens[body["deviceID"]] = {"token": body["updateToken"], "env": body.get("env", "production"),
                                                    "at": int(time.time())}
        self._save()
        return {"ok": True}

    # ------------------------------------------------------------------ APNs
    def _send(self, token, env, payload, priority=10):
        if self.push is None or not self.push.configured:
            return False
        try:
            import httpx
            auth = self.push._token()
            host = "api.sandbox.push.apple.com" if env == "sandbox" else "api.push.apple.com"
            with httpx.Client(http2=True, timeout=10) as cli:
                r = cli.post(f"https://{host}/3/device/{token}", json=payload,
                             headers={"authorization": f"bearer {auth}", "apns-topic": TOPIC,
                                      "apns-push-type": "liveactivity", "apns-priority": str(priority)})
            if r.status_code == 410 or "BadDeviceToken" in r.text or "Unregistered" in r.text:
                return None                             # Token ungültig → vergessen
            if r.status_code != 200:
                print(f"[LIVE] APNs {r.status_code}: {r.text[:120]}", flush=True)
            return r.status_code == 200
        except Exception as e:
            print(f"[LIVE] {e}", flush=True)
            return False

    @staticmethod
    def _state(dev, start, end, done=False):
        s = {"status": dev.get("status") or ("fertig" if done else "läuft"), "start": start, "end": end, "done": done}
        if dev.get("detail"):
            s["detail"] = dev["detail"]
        if dev.get("progress") is not None:
            s["progress"] = max(0.0, min(1.0, dev["progress"]))
        return s

    def _start(self, dev, start, end):
        now = int(time.time())
        payload = {"aps": {"timestamp": now, "event": "start", "content-state": self._state(dev, start, end),
                           "attributes-type": "HomeActivityAttributes",
                           "attributes": {"kind": dev["kind"], "name": dev["name"], "deviceID": dev["id"]},
                           "stale-date": int((end or now + 3 * 3600) + 900),
                           "alert": {"title": dev["name"], "body": f"{dev.get('status') or 'läuft'}"}}}
        for tok, env in list(self.start_tokens.items()):
            if self._send(tok, env, payload) is None:
                self.start_tokens.pop(tok, None); self._save()
        print(f"[LIVE] Start: {dev['name']}", flush=True)

    def _update(self, dev, start, end, done=False):
        t = self.update_tokens.get(dev["id"])
        if not t:
            return
        now = int(time.time())
        aps = {"timestamp": now, "event": "end" if done else "update", "content-state": self._state(dev, start, end, done)}
        if done:
            aps["dismissal-date"] = now + 15 * 60
        else:
            aps["stale-date"] = int((end or now + 3 * 3600) + 900)
        if self._send(t["token"], t["env"], {"aps": aps}, priority=10 if done else 5) is None or done:
            self.update_tokens.pop(dev["id"], None); self._save()

    # ------------------------------------------------------------------ Überwachung
    def _loop(self):
        time.sleep(40)
        while True:
            try:
                self._tick()
            except Exception as e:
                print(f"[LIVE] {e}", flush=True)
            time.sleep(30)

    def _tick(self):
        if not self.start_tokens and not self.update_tokens:
            return
        now = time.time()
        running = {d["id"]: d for d in self.sources()}
        for did, dev in running.items():
            end = now + dev["remaining"] if dev.get("remaining") else None
            a = self.active.get(did)
            if a is None:
                self.active[did] = {"start": now, "end": end, "sent": now, "dev": dev}
                self._start(dev, now, end)
            elif now - a["sent"] > 300:
                a.update(end=end or a["end"], sent=now, dev=dev)
                self._update(dev, a["start"], a["end"])
        for did in [d for d in self.active if d not in running]:
            a = self.active.pop(did)
            dev = dict(a["dev"], status="fertig", progress=1.0)
            self._update(dev, a["start"], a["end"], done=True)
            print(f"[LIVE] Ende: {dev['name']}", flush=True)


def collect(homeconnect, miele, vacuum, evcc_state_fn):
    """Laufende Geräte aus allen Quellen einsammeln (für LiveActivities)"""
    out = []
    apps = list(homeconnect.list()) if homeconnect.auth.ready else []
    try:
        apps += miele.list()
    except Exception:
        pass
    for a in apps:
        kind = KIND.get(a.get("type"))
        if not kind or a.get("state") != "Run":
            continue
        brand = a.get("brand")
        name = a.get("name") or a.get("typeText") or "Gerät"
        if brand and brand.lower() not in name.lower():
            name = f"{name} · {brand}"
        prog = a.get("progress")
        out.append({"id": a["id"], "kind": kind, "name": name, "status": a.get("phase") or "läuft",
                    "detail": a.get("program"), "progress": (prog / 100) if isinstance(prog, (int, float)) else None,
                    "remaining": a.get("remaining")})
    try:
        for duid, st in (vacuum.status or {}).items():
            if st.get("cleaning"):
                pct = st.get("cleanPercent")
                area = st.get("cleanArea")
                out.append({"id": "vac:" + duid, "kind": "vacuum", "name": st.get("name") or "Saugroboter",
                            "status": st.get("stateText") or "saugt",
                            "detail": f"{area:.0f} m² · {st.get('cleanMinutes') or 0} min" if area else None,
                            "progress": (pct / 100) if isinstance(pct, (int, float)) else None})
    except Exception:
        pass
    try:
        s = evcc_state_fn() or {}
        s = s.get("result", s) if isinstance(s, dict) else {}
        for i, lp in enumerate(s.get("loadpoints") or []):
            if lp.get("charging"):
                soc, lim = lp.get("vehicleSoc"), lp.get("effectiveLimitSoc") or lp.get("limitSoc")
                rem = lp.get("chargeRemainingDuration")
                if isinstance(rem, (int, float)) and rem > 10 ** 7:
                    rem = rem / 1e9                      # ältere evcc: Nanosekunden
                kw = (lp.get("chargePower") or 0) / 1000
                out.append({"id": f"lp:{i}", "kind": "car", "name": lp.get("vehicleTitle") or "Auto",
                            "status": "lädt", "detail": f"{kw:.1f} kW".replace(".", ",") + (f" · {soc:.0f} %" if soc is not None else ""),
                            "progress": (soc / lim) if soc is not None and lim else None,
                            "remaining": rem if isinstance(rem, (int, float)) and rem > 0 else None})
    except Exception:
        pass
    return out
