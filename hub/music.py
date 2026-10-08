#!/usr/bin/env python3
"""HomePods & AirPlay-Lautsprecher: finden, steuern und Zeitpläne (pyatv).

Was geht: Lautsprecher-Liste, Play/Pause/Weiter, Lautstärke, Radio-Stream abspielen,
"weiterspielen" (zuletzt gehörte Musik), Zeitpläne (z. B. Mo–Fr 6:45 Küche Bayern 3, 22:00 alles aus).
Was NICHT geht: eine bestimmte Apple-Music-Playlist zeitgesteuert starten – das kann nur die Home-App.
"""
import asyncio
import json
import os
import threading
import time
import uuid
from datetime import datetime

import pyatv
from pyatv.const import DeviceState, Protocol

SCHEDULE_FILE = os.environ.get("MUSIC_SCHEDULE_FILE", "/opt/liesenberg-home/music-schedules.json")

RADIO = {
    "Bayern 3": "https://dispatcher.rndfnk.com/br/br3/live/mp3/mid",
    "Bayern 1": "https://dispatcher.rndfnk.com/br/br1/obb/mp3/mid",
    "Antenne Bayern": "https://stream.antenne.de/antenne",
    "Radio Arabella": "https://live.arabella.de/arabella-muenchen/stream/mp3",
}

STATE_DE = {DeviceState.Playing: "spielt", DeviceState.Paused: "Pause", DeviceState.Idle: "bereit",
            DeviceState.Loading: "lädt", DeviceState.Stopped: "gestoppt", DeviceState.Seeking: "spult"}


class Music:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.configs = {}      # id → pyatv config
        self.conns = {}        # id → verbundenes Gerät
        self.status = {}       # id → dict für die App
        self.error = None
        self.schedules = self._load()
        self._last_fired = {}
        self._run(self._scan())
        threading.Thread(target=self._scheduler, daemon=True).start()

    # ---------------------------------------------------------- Infrastruktur
    def _run(self, coro, timeout=20):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _scan(self):
        try:
            found = await pyatv.scan(self.loop, timeout=5)
        except Exception as e:
            self.error = f"Suche fehlgeschlagen: {e}"
            return
        for c in found:
            # nur Geräte, die AirPlay sprechen (HomePod, AirPlay-Boxen, Apple TV)
            if not c.get_service(Protocol.AirPlay):
                continue
            did = c.identifier
            self.configs[did] = c
            st = self.status.setdefault(did, {"id": did})
            st.update(name=c.name, model=str(c.device_info.model).split(".")[-1].replace("HomePod", "HomePod "),
                      address=str(c.address))

    async def _conn(self, did):
        if did in self.conns:
            return self.conns[did]
        conf = self.configs.get(did)
        if not conf:
            raise KeyError(did)
        atv = await pyatv.connect(conf, self.loop)
        self.conns[did] = atv
        return atv

    async def _drop(self, did):
        atv = self.conns.pop(did, None)
        if atv:
            atv.close()

    async def _refresh_one(self, did):
        st = self.status[did]
        try:
            atv = await self._conn(did)
            p = await atv.metadata.playing()
            st.update(state=STATE_DE.get(p.device_state, str(p.device_state).split(".")[-1]),
                      playing=p.device_state == DeviceState.Playing,
                      title=p.title, artist=p.artist, album=p.album,
                      volume=round(atv.audio.volume) if atv.audio else None, online=True)
        except Exception as e:
            st.update(online=False, state="nicht erreichbar", error=str(e)[:120])
            await self._drop(did)

    async def _refresh_all(self):
        if not self.configs:
            await self._scan()
        await asyncio.gather(*(self._refresh_one(d) for d in list(self.configs)), return_exceptions=True)

    # ---------------------------------------------------------- für die App
    def list(self):
        try:
            self._run(self._refresh_all(), timeout=25)
        except Exception as e:
            self.error = str(e)
        return sorted(self.status.values(), key=lambda s: s.get("name") or "")

    def rescan(self):
        self._run(self._scan())

    async def _command(self, did, action, value=None):
        atv = await self._conn(did)
        rc = atv.remote_control
        if action == "play":
            await rc.play()
        elif action == "pause":
            await rc.pause()
        elif action == "toggle":
            await rc.play_pause()
        elif action == "next":
            await rc.next()
        elif action == "previous":
            await rc.previous()
        elif action == "volume":
            await atv.audio.set_volume(float(value))
        elif action == "radio":
            url = RADIO.get(value, value)
            # Stream in eigenem Task – play_url blockiert, solange der Stream läuft
            asyncio.ensure_future(atv.stream.play_url(url))
        else:
            raise ValueError(action)

    def command(self, did, action, value=None):
        try:
            self._run(self._command(did, action, value))
        except KeyError:
            raise
        except Exception:
            self._run(self._drop(did))          # Verbindung neu aufbauen und einmal wiederholen
            self._run(self._command(did, action, value))
        self.loop.call_later(1.5, lambda: asyncio.ensure_future(self._refresh_one(did)))

    # ---------------------------------------------------------- Zeitpläne
    def _load(self):
        try:
            with open(SCHEDULE_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _save(self):
        tmp = SCHEDULE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.schedules, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SCHEDULE_FILE)

    def upsert_schedule(self, s):
        s = dict(s)
        if not s.get("id"):                     # neue Zeitpläne kommen mit "id": null
            s["id"] = uuid.uuid4().hex[:8]
        s.setdefault("enabled", True)
        self.schedules = [x for x in self.schedules if x["id"] != s["id"]] + [s]
        self.schedules.sort(key=lambda x: x.get("time", ""))
        self._save()
        return s

    def delete_schedule(self, sid):
        self.schedules = [x for x in self.schedules if x["id"] != sid]
        self._save()

    def _fire(self, s):
        for did in s.get("devices", []):
            try:
                if s.get("volume") is not None:
                    self.command(did, "volume", s["volume"])
                act = s.get("action", "play")
                if act == "radio":
                    self.command(did, "radio", s.get("station", "Bayern 3"))
                elif act in ("play", "pause"):
                    self.command(did, act)
            except Exception as e:
                self.error = f"Zeitplan {s.get('name') or s['id']}: {e}"

    def _scheduler(self):
        while True:
            now = datetime.now()
            hm, wd = now.strftime("%H:%M"), now.weekday()   # 0 = Montag
            for s in list(self.schedules):
                key = (s["id"], now.strftime("%Y-%m-%d %H:%M"))
                if s.get("enabled", True) and s.get("time") == hm and wd in s.get("days", range(7)) \
                        and key not in self._last_fired:
                    self._last_fired[key] = True
                    threading.Thread(target=self._fire, args=(s,), daemon=True).start()
            time.sleep(15)
