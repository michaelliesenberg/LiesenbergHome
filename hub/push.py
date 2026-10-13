"""Mitteilungen aufs iPhone – direkt vom Hub über Apple Push (APNs), ohne fremden Server.

Einrichtung (einmalig, Besitzer): im Apple-Developer-Konto einen „APNs Auth Key" (.p8) anlegen und auf der
Einrichtungsseite unter System → Mitteilungen hochladen (Key-ID + Team-ID). Der Schlüssel liegt nur auf dem Hub
(DATA_DIR/apns.p8, chmod 600). Jede App meldet ihr Geräte-Token beim Hub an (POST /api/push/register).

Abos je Person: washer (Wäsche/Spülen fertig), car (Auto voll/eingesteckt), weekly (Wochenbericht), smart (Auto-Start),
wx_alert / wx_rain / wx_gust (Wetter: DWD-Warnung, Regen in Kürze, Sturmböen – forecast.py),
solar (Sonnenstrom übrig), monthly (Monatsbericht), cheap (Billigstrom, nur dynamischer Tarif),
wx_frost (Frost), garden (Gieß-Hinweis), maint (Wartung fällig – maintenance.py).
"""
import base64
import json
import os
import threading
import time

from hub_config import DATA_DIR

KEY_FILE = os.path.join(DATA_DIR, "apns.p8")
DEV_FILE = os.path.join(DATA_DIR, "push.json")
TOPICS = ("washer", "car", "weekly", "smart", "wx_alert", "wx_rain", "wx_gust", "vacuum", "mower", "solar", "monthly", "cheap", "wx_frost", "garden", "maint")
BUNDLE = "biz.liesenberg.home"


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Push:
    def __init__(self, get_cfg):
        self.get_cfg = get_cfg            # → {"key_id", "team_id"}
        self._lock = threading.Lock()
        self._jwt = (0, "")
        self.devices = self._load()       # person_id → {token: {"env", "prefs", "name", "updated"}}
        self.last_error = None

    # ---- Geräte
    def _load(self):
        try:
            with open(DEV_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = DEV_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.devices, f)
        os.replace(tmp, DEV_FILE)

    def register(self, person, token, env="production", prefs=None):
        token = "".join(ch for ch in str(token) if ch in "0123456789abcdefABCDEF")[:200]
        if len(token) < 32:
            raise ValueError("Ungültiges Geräte-Token")
        with self._lock:
            mine = self.devices.setdefault(person["id"], {})
            old = mine.get(token, {})
            mine[token] = {"env": "sandbox" if env == "sandbox" else "production",
                           "prefs": prefs or old.get("prefs") or {t: True for t in TOPICS},
                           "name": person.get("name"), "updated": int(time.time())}
            self._save()
        return mine[token]

    def set_prefs(self, person, token, prefs):
        with self._lock:
            d = self.devices.get(person["id"], {}).get(token)
            if not d:
                raise ValueError("Gerät nicht angemeldet")
            d["prefs"] = {t: bool(prefs.get(t, d["prefs"].get(t, True))) for t in TOPICS}
            self._save()
            return d

    def remove_person(self, person_id):
        with self._lock:
            if self.devices.pop(person_id, None) is not None:
                self._save()

    @property
    def configured(self):
        c = self.get_cfg() or {}
        return bool(c.get("key_id") and c.get("team_id") and os.path.exists(KEY_FILE))

    # ---- Senden
    def _token(self):
        """JWT für APNs (ES256), ca. 50 Minuten gültig."""
        if time.time() - self._jwt[0] < 50 * 60:
            return self._jwt[1]
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        c = self.get_cfg()
        with open(KEY_FILE, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        head = _b64(json.dumps({"alg": "ES256", "kid": c["key_id"]}).encode())
        body = _b64(json.dumps({"iss": c["team_id"], "iat": int(time.time())}).encode())
        r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
        sig = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        self._jwt = (time.time(), f"{head}.{body}.{sig}")
        return self._jwt[1]

    def send(self, topic, title, body, person_ids=None, data=None, category=None):
        """An alle Geräte mit diesem Abo (optional nur bestimmte Personen). Läuft im Hintergrund."""
        if not self.configured:
            return 0
        targets = []
        for pid, toks in self.devices.items():
            if person_ids is not None and pid not in person_ids:
                continue
            for tok, d in toks.items():
                if d.get("prefs", {}).get(topic, True):
                    targets.append((pid, tok, d.get("env", "production")))
        if targets:
            data = dict(data or {})
            if category:
                data["_category"] = category
            threading.Thread(target=self._deliver, args=(targets, title, body, topic, data), daemon=True).start()
        return len(targets)

    def _deliver(self, targets, title, body, topic, data):
        try:
            import httpx
        except ImportError:
            self.last_error = "httpx fehlt (pip install 'httpx[http2]')"
            return
        data = dict(data)
        category = data.pop("_category", None)
        aps = {"alert": {"title": title, "body": body}, "sound": "default", "thread-id": topic}
        if category:
            aps["category"] = category            # Aktions-Knöpfe in der Mitteilung (z. B. „Abbrechen“)
        payload = {"aps": aps, "topic": topic, **data}
        try:
            auth = self._token()
        except Exception as e:
            self.last_error = f"APNs-Schlüssel: {e}"
            return
        with httpx.Client(http2=True, timeout=10) as cli:
            for pid, tok, env in targets:
                host = "api.sandbox.push.apple.com" if env == "sandbox" else "api.push.apple.com"
                try:
                    r = cli.post(f"https://{host}/3/device/{tok}", json=payload,
                                 headers={"authorization": f"bearer {auth}", "apns-topic": BUNDLE,
                                          "apns-push-type": "alert", "apns-priority": "10"})
                    if r.status_code == 200:
                        self.last_error = None
                    elif r.status_code == 410 or "BadDeviceToken" in r.text or "Unregistered" in r.text:
                        with self._lock:                       # App gelöscht → Token vergessen
                            self.devices.get(pid, {}).pop(tok, None)
                            self._save()
                    else:
                        self.last_error = f"APNs {r.status_code}: {r.text[:160]}"
                except Exception as e:
                    self.last_error = f"APNs nicht erreichbar: {e}"
        print(f"[PUSH] {topic}: {title} → {len(targets)} Gerät(e){' – ' + self.last_error if self.last_error else ''}", flush=True)
