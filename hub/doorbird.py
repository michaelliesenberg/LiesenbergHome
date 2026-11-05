"""DoorBird-Türstation: Klingeln → Mitteilung mit Bild aufs iPhone.

Der Hub hält eine Verbindung zur LAN-API der DoorBird offen (monitor.cgi, „doorbell:H“ = es klingelt),
holt beim Klingeln sofort ein Standbild (image.cgi) und schickt eine Push (Abo „door“).
Die App lädt das Bild in der Mitteilung nach (Notification Service Extension) – das Bild bleibt auf dem Hub.

Einrichtung (Besitzer, /setup → Geräte): Adresse (leer = automatisch suchen), DoorBird-Benutzer + Passwort.
Der Benutzer braucht in der DoorBird-App die Rechte „API-Operator“ (für die Klingel-Meldung)
und am besten „Live-Bild jederzeit“ (sonst liefert die DoorBird das Bild nur kurz nach dem Klingeln).

Ändert sich die IP (DHCP), sucht der Hub die DoorBird im eigenen /24-Netz über die Herstellerkennung
der MAC-Adresse (1C:CA:E3) und merkt sich die neue Adresse.
"""
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from hub_config import DATA_DIR

DIR = os.path.join(DATA_DIR, "doorbird")
HISTORY = os.path.join(DIR, "rings.json")
KEEP = 40                        # so viele Klingel-Bilder bleiben auf dem Hub
MAC_PREFIX = "1c:ca:e3"          # Bird Home Automation GmbH
DEBOUNCE = 15                    # Sekunden: Dauerdrücken = ein Klingeln


class DoorBird:
    def __init__(self, get_cfg, set_host, push, lan_ip_fn):
        self.get_cfg, self.set_host, self.push, self.lan_ip = get_cfg, set_host, push, lan_ip_fn
        self.connected = False
        self.error = None
        self.info = {}
        self.on_ring = None            # optional: weitere Aktion beim Klingeln (z. B. Licht)
        self._last_ring = 0
        self._last_scan = 0
        self._fails = 0
        self._lock = threading.Lock()
        os.makedirs(DIR, exist_ok=True)
        self.rings = self._load()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- Einstellungen
    def _cfg(self):
        return self.get_cfg() or {}

    @property
    def configured(self):
        c = self._cfg()
        return bool(c.get("user") and c.get("password"))

    def _auth(self):
        c = self._cfg()
        return (c.get("user") or "", c.get("password") or "")

    def _base(self):
        h = (self._cfg().get("host") or "").strip()
        return f"http://{h}" if h else ""

    # ---- Verlauf
    def _load(self):
        try:
            with open(HISTORY) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _save(self):
        tmp = HISTORY + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.rings, f)
        os.replace(tmp, HISTORY)

    def image_path(self, rid):
        rid = "".join(ch for ch in str(rid) if ch.isdigit())
        p = os.path.join(DIR, f"{rid}.jpg")
        return p if rid and os.path.isfile(p) else None

    # ---- DoorBird-API
    def test(self):
        """Verbindung prüfen (Einrichtungsseite). Sucht die DoorBird, wenn keine Adresse eingetragen ist."""
        if not self._base():
            self.discover(force=True)
        if not self._base():
            raise RuntimeError("DoorBird im Netz nicht gefunden – bitte IP-Adresse eintragen")
        r = requests.get(self._base() + "/bha-api/info.cgi", auth=self._auth(), timeout=6)
        if r.status_code == 401:
            raise RuntimeError("Benutzer oder Passwort falsch")
        r.raise_for_status()
        v = (r.json().get("BHA", {}).get("VERSION") or [{}])[0]
        self.info = {"firmware": v.get("FIRMWARE"), "build": v.get("BUILD_NUMBER"),
                     "mac": v.get("PRIMARY_MAC_ADDR") or v.get("WIFI_MAC_ADDR"), "type": v.get("DEVICE-TYPE")}
        self.error = None
        return self.info

    def snapshot(self):
        """Aktuelles Standbild (JPEG-Bytes) oder None."""
        if not (self.configured and self._base()):
            return None
        try:
            r = requests.get(self._base() + "/bha-api/image.cgi", auth=self._auth(), timeout=6)
            if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                return r.content
            if r.status_code == 204:
                self.error = "Kein Bild: dem DoorBird-Benutzer fehlt das Recht „Live-Bild“"
        except requests.RequestException as e:
            self.error = f"Bild: {e}"
        return None

    # ---- Klingeln
    def ring(self, test=False, only=None):
        """Klingeln verarbeiten: Bild holen, speichern, Push schicken. test = Probe-Klingeln, only = nur an diese Person-IDs."""
        now = time.time()
        if not test:
            if now - self._last_ring < DEBOUNCE:
                return None
            self._last_ring = now
        rid = str(int(now * 1000))
        img = self.snapshot()
        if img is None:                    # das erste Bild kommt manchmal zu früh – kurz nachfassen
            time.sleep(0.7)
            img = self.snapshot()
        entry = {"id": rid, "ts": int(now), "image": bool(img), "test": bool(test)}
        with self._lock:
            if img:
                with open(os.path.join(DIR, f"{rid}.jpg"), "wb") as f:
                    f.write(img)
            self.rings.insert(0, entry)
            for old in self.rings[KEEP:]:
                try:
                    os.remove(os.path.join(DIR, f"{old['id']}.jpg"))
                except OSError:
                    pass
            del self.rings[KEEP:]
            self._save()
        data = {"door": rid}
        if img:
            data["image"] = f"api/doorbird/snapshot/{rid}"
        title = "Es klingelt" + (" (Test)" if test else "")
        self.push.send("door", title, f"An der Haustür · {datetime.now().strftime('%H:%M')}",
                       person_ids=only, data=data,
                       aps_extra={"mutable-content": 1, "interruption-level": "time-sensitive"})
        print(f"[DOORBIRD] Klingeln{' (Test)' if test else ''} – Bild: {'ja' if img else 'nein'}", flush=True)
        if self.on_ring and not test:
            try:
                self.on_ring()
            except Exception as e:
                print(f"[DOORBIRD] on_ring: {e}", flush=True)
        return entry

    def state(self):
        c = self._cfg()
        return {"configured": self.configured, "connected": self.connected, "host": c.get("host") or "",
                "error": self.error, "rings": [r for r in self.rings if not r.get("test")][:20]}

    # ---- Dauerverbindung (monitor.cgi)
    def _loop(self):
        time.sleep(10)
        while True:
            if not self.configured:
                self.connected = False
                time.sleep(30)
                continue
            if not self._base():
                self.discover()
                if not self._base():
                    time.sleep(60)
                    continue
            try:
                self._monitor()
                self._fails = 0
            except (requests.exceptions.ReadTimeout, requests.exceptions.ChunkedEncodingError):
                if self.connected:               # war verbunden, nur still → einfach neu verbinden
                    self.connected, self._fails = False, 0
                    continue
                self._fails += 1
            except Exception as e:
                self.connected = False
                self._fails += 1
                self.error = f"DoorBird nicht erreichbar: {e}"[:200]
                if self._fails >= 3:                 # vielleicht neue IP → im Netz suchen
                    self.discover()
            time.sleep(min(60, 5 * max(1, self._fails)))

    def _monitor(self):
        url = self._base() + "/bha-api/monitor.cgi?ring=doorbell"
        with requests.get(url, auth=self._auth(), stream=True, timeout=(6, 120)) as r:
            if r.status_code == 401:
                raise RuntimeError("Benutzer oder Passwort falsch (oder Recht „API-Operator“ fehlt)")
            if r.status_code == 509:
                raise RuntimeError("zu viele Verbindungen zur DoorBird")
            r.raise_for_status()
            self.connected, self.error = True, None
            print(f"[DOORBIRD] verbunden mit {self._base()}", flush=True)
            for line in r.iter_lines(chunk_size=1, decode_unicode=False):
                if line and b"doorbell:H" in line:
                    threading.Thread(target=self.ring, daemon=True).start()
        self.connected = False

    # ---- Suche im Netz (neue IP nach DHCP)
    def discover(self, force=False):
        if not force and time.time() - self._last_scan < 600:
            return None
        self._last_scan = time.time()
        ip = self.lan_ip()
        if not ip or ip.startswith("127."):
            return None
        net = ip.rsplit(".", 1)[0]

        def knock(i):                        # TCP-Verbindung → der Pi lernt die MAC (ARP-Tabelle)
            try:
                with socket.create_connection((f"{net}.{i}", 80), timeout=0.4):
                    pass
            except OSError:
                pass

        with ThreadPoolExecutor(max_workers=48) as ex:
            list(ex.map(knock, range(1, 255)))
        found = None
        try:
            with open("/proc/net/arp") as f:
                for row in f.read().splitlines()[1:]:
                    cols = row.split()
                    if len(cols) >= 4 and cols[3].lower().startswith(MAC_PREFIX) and cols[2] != "0x0":
                        found = cols[0]
                        break
        except OSError:
            pass
        if found and found != (self._cfg().get("host") or ""):
            print(f"[DOORBIRD] gefunden unter {found}", flush=True)
            self.set_host(found)
        return found
