"""Automatische Hub-Updates – nur signierte Pakete (Prüfung und Installation macht hubctl als root).

Ablauf: alle 6 Stunden `hubctl release-fetch` (holt das neueste Paket, falls eine Quelle eingetragen ist) und
`hubctl release-check` (Signatur + Prüfsumme + „neuer als installiert?“).
  • Einstellung „ask“ (Standard): Mitteilung „Hub-Update x.y.z verfügbar“ – installiert wird erst, wenn jemand mit
    Besitzer-/Vollzugriff in der App bzw. auf der Einrichtungsseite auf „Aktualisieren“ tippt.
  • Einstellung „auto“: nachts zwischen 3 und 5 Uhr selbst installieren.
Installation (hubctl release-install): Sicherung → Dateien tauschen → Neustart → Gesundheitscheck → sonst zurück.
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime

from hub_config import DATA_DIR

STATUS = os.path.join(DATA_DIR, "update-status.json")


def hubctl(*args, timeout=120):
    try:
        r = subprocess.run(["sudo", "-n", "/usr/local/sbin/hubctl", *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or r.stderr).strip()
    except Exception as e:
        return 1, str(e)


class Updater:
    def __init__(self, mode_fn, push=None, version=""):
        self.mode_fn, self.push, self.version = mode_fn, push, version
        self.check, self.checked_at = {}, 0
        self._notified = None
        threading.Thread(target=self._loop, daemon=True).start()

    def refresh(self):
        hubctl("release-fetch", timeout=180)
        rc, out = hubctl("release-check")
        try:
            self.check = json.loads(out.splitlines()[-1]) if out else {}
        except (ValueError, IndexError):
            self.check = {"available": False, "reason": out[-200:]}
        self.checked_at = time.time()
        return self.check

    def available(self):
        return self.check.get("version") if self.check.get("available") else None

    def install(self):
        if not self.available():
            self.refresh()
        if not self.available():
            raise RuntimeError(self.check.get("reason") or "Kein neueres, signiertes Paket vorhanden")
        rc, out = hubctl("release-install")
        if rc != 0:
            raise RuntimeError(out[-300:])
        return out

    def last_result(self):
        try:
            with open(STATUS) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _loop(self):
        time.sleep(120)
        while True:
            try:
                self.refresh()
                v = self.available()
                if v:
                    mode = self.mode_fn() or "ask"
                    h = datetime.now().hour
                    if mode == "auto" and 3 <= h < 5:
                        print(f"[UPDATE] automatisch: {self.version} → {v}", flush=True)
                        self.install()
                    elif self._notified != v and self.push is not None:
                        self._notified = v
                        self.push.send("weekly", f"Hub-Update {v} verfügbar",
                                       "In der App auf „Aktualisieren“ tippen – geprüft, gesichert, bei Problemen automatisch zurück.")
            except Exception as e:
                print(f"[UPDATE] {e}", flush=True)
            # nachts öfter schauen (für „auto“), sonst alle 6 Stunden
            time.sleep(1800 if 2 <= datetime.now().hour < 5 else 6 * 3600)
