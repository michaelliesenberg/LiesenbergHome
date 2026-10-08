"""Personen & Einladungen – jede Person hat ihren eigenen Schlüssel.

Rollen:
  owner – alles, inkl. Personen und Einrichtung
  full  – alle Geräte und Szenen bearbeiten, keine Personen/Einrichtung
  guest – Licht, Rollläden, Musik, Szenen starten; KEINE Tore/Türen, keine Heizung/Klima, nichts bearbeiten

Auf dem Hub werden nur Hashes der Schlüssel gespeichert. Eine Einladung ist ein
Einmal-Code (48 h gültig), den die App gegen den eigentlichen Schlüssel tauscht –
ein abfotografierter QR-Code ist danach wertlos.
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from hub_config import DATA_DIR

PEOPLE_FILE = os.path.join(DATA_DIR, "people.json")
ROLES = ("owner", "full", "guest")
INVITE_TTL = 48 * 3600

_lock = threading.RLock()


def _h(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _load():
    try:
        with open(PEOPLE_FILE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("people", [])
    d.setdefault("invites", [])
    return d


def _save(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PEOPLE_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, PEOPLE_FILE)


def _public(p):
    return {k: p.get(k) for k in ("id", "name", "role", "created", "last_seen")}


def list_people():
    with _lock:
        return [_public(p) for p in _load()["people"]]


def has_owner():
    with _lock:
        return any(p["role"] == "owner" for p in _load()["people"])


def migrate_legacy_token(token: str):
    """Alter gemeinsamer App-Schlüssel → Person „Besitzer" (damit vorhandene Apps weiterlaufen)."""
    if not token:
        return
    with _lock:
        d = _load()
        if any(p.get("key_hash") == _h(token) for p in d["people"]):
            return
        d["people"].append({"id": secrets.token_hex(4), "name": "Besitzer", "role": "owner",
                            "key_hash": _h(token), "created": int(time.time()), "last_seen": None})
        _save(d)


_seen_cache = {}


def authenticate(key: str):
    """Schlüssel → Person (oder None)."""
    if not key:
        return None
    kh = _h(key)
    with _lock:
        for p in _load()["people"]:
            if hmac.compare_digest(p.get("key_hash", ""), kh):
                now = int(time.time())
                if now - _seen_cache.get(p["id"], 0) > 300:   # „zuletzt gesehen" höchstens alle 5 Min schreiben
                    _seen_cache[p["id"]] = now
                    d = _load()
                    for q in d["people"]:
                        if q["id"] == p["id"]:
                            q["last_seen"] = now
                    _save(d)
                return _public(p)
    return None


def create_owner(name="Besitzer"):
    """Erster Start ohne Besitzer: Besitzer anlegen und Einladung für sein iPhone erzeugen."""
    return create_invite(name, "owner")


def create_invite(name: str, role: str):
    if role not in ROLES:
        raise ValueError("Rolle unbekannt")
    code = secrets.token_urlsafe(18)
    with _lock:
        d = _load()
        pid = secrets.token_hex(4)
        d["people"].append({"id": pid, "name": name.strip() or "Gast", "role": role, "key_hash": "",
                            "created": int(time.time()), "last_seen": None, "pending": True})
        d["invites"] = [i for i in d["invites"] if i["expires"] > time.time()]
        d["invites"].append({"code_hash": _h(code), "person": pid, "expires": int(time.time()) + INVITE_TTL})
        _save(d)
    return code, pid


def claim(code: str):
    """Einladungscode → neuer Schlüssel (einmalig). Liefert (key, person) oder (None, None)."""
    ch = _h(code or "")
    with _lock:
        d = _load()
        inv = next((i for i in d["invites"] if hmac.compare_digest(i["code_hash"], ch)), None)
        if not inv or inv["expires"] < time.time():
            return None, None
        key = secrets.token_urlsafe(32)
        person = None
        for p in d["people"]:
            if p["id"] == inv["person"]:
                p["key_hash"] = _h(key)
                p.pop("pending", None)
                person = p
        d["invites"] = [i for i in d["invites"] if i is not inv]
        if not person:
            return None, None
        _save(d)
        return key, _public(person)


def remove(pid: str):
    with _lock:
        d = _load()
        d["people"] = [p for p in d["people"] if p["id"] != pid]
        d["invites"] = [i for i in d["invites"] if i["person"] != pid]
        _save(d)


def set_role(pid: str, role: str):
    if role not in ROLES:
        raise ValueError("Rolle unbekannt")
    with _lock:
        d = _load()
        for p in d["people"]:
            if p["id"] == pid:
                p["role"] = role
        if not any(p["role"] == "owner" and not p.get("pending") for p in d["people"]):
            raise ValueError("Mindestens ein Besitzer muss bleiben")
        _save(d)


# ---------------------------------------------------------------- Einrichtungs-Passwort
def set_setup_password(pw: str):
    import hub_config
    salt = secrets.token_hex(8)
    hub_config.update({"setup_password_hash": salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()})


def check_setup_password(pw: str) -> bool:
    import hub_config
    stored = hub_config.get("setup_password_hash") or ""
    if "$" not in stored:
        return False
    salt, h = stored.split("$", 1)
    return hmac.compare_digest(h, hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), salt.encode(), 200_000).hex())
