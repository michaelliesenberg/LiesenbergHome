"""Wartungserinnerungen: eigene Aufgaben mit Intervall (z. B. Rauchmelder testen, Wärmepumpe warten) plus
automatische Einträge aus den Geräten (Verschleißteile des Saugroboters unter 10 %).

Mitteilung (Abo „maint“) um 10 Uhr, wenn etwas fällig wird – je Aufgabe einmal, danach erst wieder nach „Erledigt“
oder nach 14 Tagen. Hier wird nichts geschaltet.
"""
import json
import os
import threading
import time
import uuid
from datetime import date, datetime, timedelta

from hub_config import DATA_DIR

FILE = os.path.join(DATA_DIR, "maintenance.json")
DEFAULTS = [   # „zuletzt erledigt“ = heute, damit am Anfang nichts fällig ist – in der App anpassen
    {"title": "Rauchmelder testen", "icon": "smoke", "days": 365},
    {"title": "Wärmepumpe warten lassen", "icon": "heat.waves", "days": 365},
    {"title": "Dunstabzug-Filter reinigen", "icon": "fan", "days": 90},
    {"title": "Mähroboter-Messer prüfen", "icon": "leaf", "days": 60},
    {"title": "Spülmaschine: Filter reinigen", "icon": "dishwasher", "days": 30},
]


class Maintenance:
    def __init__(self, push=None, vacuum_fn=None):
        self.push, self.vacuum_fn = push, vacuum_fn
        self._lock = threading.Lock()
        self.data = self._load()
        if "items" not in self.data:
            today = date.today().isoformat()
            self.data = {"items": [dict(d, id=uuid.uuid4().hex[:8], last=today) for d in DEFAULTS], "sent": {}}
            self._save()
        threading.Thread(target=self._loop, daemon=True).start()

    def _load(self):
        try:
            with open(FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, FILE)

    # ---- Liste
    @staticmethod
    def _due(it):
        last = date.fromisoformat(it["last"])
        nxt = last + timedelta(days=int(it["days"]))
        return nxt, (nxt - date.today()).days

    def _auto(self):
        """Automatische Einträge aus den Geräten (nur Anzeige, nicht editierbar)"""
        out = []
        try:
            for duid, st in ((self.vacuum_fn() if self.vacuum_fn else None) or {}).items():
                for c in st.get("consumables") or []:
                    p = c.get("percent")
                    if p is not None and p <= 10:
                        out.append({"id": f"auto:{duid}:{c['name']}", "title": f"{c['name']} wechseln",
                                    "device": st.get("name") or "Saugroboter", "icon": "vacuum", "auto": True,
                                    "percent": p, "daysLeft": 0, "due": p <= 5})
        except Exception:
            pass
        return out

    def list(self):
        with self._lock:
            items = []
            for it in self.data["items"]:
                nxt, left = self._due(it)
                items.append(dict(it, next=nxt.isoformat(), daysLeft=left, due=left <= 0))
        items.sort(key=lambda x: x["daysLeft"])
        return {"items": self._auto() + items}

    def add(self, title, days, icon="wrench.and.screwdriver", last=None):
        title = (title or "").strip()[:60]
        if not title or not 1 <= int(days) <= 3650:
            raise ValueError("Titel und Intervall (1–3650 Tage) angeben")
        with self._lock:
            it = {"id": uuid.uuid4().hex[:8], "title": title, "icon": icon or "wrench.and.screwdriver",
                  "days": int(days), "last": (last or date.today().isoformat())}
            date.fromisoformat(it["last"])
            self.data["items"].append(it)
            self._save()
        return it

    def update(self, iid, body):
        with self._lock:
            it = next((x for x in self.data["items"] if x["id"] == iid), None)
            if not it:
                raise KeyError(iid)
            if "title" in body and str(body["title"]).strip():
                it["title"] = str(body["title"]).strip()[:60]
            if "days" in body:
                d = int(body["days"])
                if not 1 <= d <= 3650:
                    raise ValueError("Intervall 1–3650 Tage")
                it["days"] = d
            if "last" in body:
                date.fromisoformat(body["last"])
                it["last"] = body["last"]
            self.data["sent"].pop(iid, None)
            self._save()
            return it

    def done(self, iid):
        return self.update(iid, {"last": date.today().isoformat()})

    def delete(self, iid):
        with self._lock:
            n = len(self.data["items"])
            self.data["items"] = [x for x in self.data["items"] if x["id"] != iid]
            if len(self.data["items"]) == n:
                raise KeyError(iid)
            self.data["sent"].pop(iid, None)
            self._save()

    # ---- Erinnern
    def _loop(self):
        time.sleep(90)
        while True:
            try:
                if datetime.now().hour >= 10:
                    self._remind()
            except Exception as e:
                print(f"[WARTUNG] {e}", flush=True)
            time.sleep(15 * 60)

    def _remind(self):
        if self.push is None or not self.push.configured:
            return
        now = time.time()
        due = [x for x in self.list()["items"] if x["due"]]
        fresh = [x for x in due if now - self.data["sent"].get(x["id"], 0) > 14 * 86400]
        if not fresh:
            return
        for x in fresh:
            self.data["sent"][x["id"]] = int(now)
        self._save()
        titles = [x["title"] + (f" ({x['device']})" if x.get("device") else "") for x in fresh]
        self.push.send("maint", "🔧 Wartung fällig" if len(titles) == 1 else f"🔧 {len(titles)} Wartungen fällig",
                       ", ".join(titles[:4]) + (" …" if len(titles) > 4 else ""), data={"open": "maintenance"})
