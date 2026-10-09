"""Einstellungen des Hubs – eine JSON-Datei statt SSH und env-Dateien.

Alles, was der Besitzer auf der Einrichtungsseite (/setup) einträgt, landet in
DATA_DIR/config.json (nur für den Dienst lesbar, chmod 600).
Ältere Installationen mit /etc/liesenberg-home.env werden beim ersten Start übernommen.
"""
import copy
import json
import os
import threading

DATA_DIR = os.environ.get("HUB_DATA_DIR", "/opt/liesenberg-home")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
LEGACY_ENV = "/etc/liesenberg-home.env"
LEGACY_LUXOR_HOST = "192.168.178.50"     # Standard der Versionen vor 2.0 (ohne LUXOR_HOST in der env-Datei)

DEFAULTS = {
    "home_name": "Mein Zuhause",
    "backend": "",                        # "luxor" | "homeassistant" | ""
    "luxor": {"host": "", "user": "admin", "password": ""},
    "homeassistant": {"url": "", "token": ""},
    "evcc": {"url": ""},
    "daikin": {"client_id": "", "client_secret": ""},
    "homeconnect": {"client_id": ""},
    "remote": {"mode": "", "url": ""},    # mode: "tailscale" | "custom" | ""
    "location": {"lat": 51.16, "lon": 10.45},   # Mitte Deutschlands, bis der Besitzer seinen Ort einträgt
    "local_url": "",                      # z. B. http://192.168.1.20:8080 (leer = automatisch)
    "setup_password_hash": "",
    "update": {"repo": "https://github.com/michaelliesenberg/LiesenbergHome", "branch": "main"},
}

_lock = threading.RLock()
_cfg = None


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def _from_legacy():
    """Übernahme aus der alten env-Datei + Umgebungsvariablen (Liesenberg-Installation)."""
    env = {**_read_env(LEGACY_ENV), **{k: v for k, v in os.environ.items()
                                        if k.startswith(("LUXOR_", "EVCC_", "DAIKIN_", "HC_", "HA_", "HUB_")) or k == "APP_TOKEN"}}
    c = {}
    if env.get("LUXOR_PASSWORD") or env.get("LUXOR_HOST"):
        c["backend"] = "luxor"
        # Alte Server-Versionen hatten die IP1-Adresse fest eingebaut, wenn LUXOR_HOST fehlte
        c["luxor"] = {"host": env.get("LUXOR_HOST") or LEGACY_LUXOR_HOST, "user": env.get("LUXOR_USER", "admin"),
                      "password": env.get("LUXOR_PASSWORD", "")}
    if env.get("EVCC_URL"):
        c["evcc"] = {"url": env["EVCC_URL"]}
    elif env.get("APP_TOKEN") or env.get("LUXOR_PASSWORD"):
        c["evcc"] = {"url": "http://127.0.0.1:7070"}   # Standard der Versionen vor 2.0 (evcc auf demselben Pi)
    if env.get("DAIKIN_CLIENT_ID"):
        c["daikin"] = {"client_id": env["DAIKIN_CLIENT_ID"], "client_secret": env.get("DAIKIN_CLIENT_SECRET", "")}
    if env.get("HC_CLIENT_ID"):
        c["homeconnect"] = {"client_id": env["HC_CLIENT_ID"]}
    if env.get("DAIKIN_REDIRECT_URI", "").startswith("https://"):
        c["remote"] = {"mode": "custom", "url": env["DAIKIN_REDIRECT_URI"].rsplit("/daikin/", 1)[0]}
    return c, env.get("APP_TOKEN", "")


def load():
    """Lädt (einmal) und liefert eine Kopie der Einstellungen."""
    global _cfg
    with _lock:
        if _cfg is None:
            try:
                with open(CONFIG_FILE) as f:
                    _cfg = _merge(DEFAULTS, json.load(f))
                # Reparatur für Hubs, die 2.0.0 mit leerer LUXOR-Adresse übernommen haben
                legacy, _ = _from_legacy()
                fixed = False
                if _cfg.get("backend") == "luxor" and not _cfg["luxor"].get("host") and legacy.get("luxor", {}).get("host"):
                    _cfg["luxor"]["host"] = legacy["luxor"]["host"]; fixed = True
                if not _cfg["evcc"].get("url") and legacy.get("evcc", {}).get("url"):
                    _cfg["evcc"]["url"] = legacy["evcc"]["url"]; fixed = True
                if fixed:
                    _write(_cfg)
            except (OSError, ValueError):
                legacy, _ = _from_legacy()
                _cfg = _merge(DEFAULTS, legacy)
                if legacy:
                    _write(_cfg)
        return copy.deepcopy(_cfg)


def legacy_app_token():
    return _from_legacy()[1]


def get(path, default=None):
    cur = load()
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def update(changes: dict):
    """Teilweise Änderung speichern, z. B. update({"luxor": {"host": "…"}})."""
    global _cfg
    with _lock:
        _cfg = _merge(load(), changes)
        _write(_cfg)
        apply_env()
        return copy.deepcopy(_cfg)


def _write(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)


def public_url():
    return (get("remote.url") or "").rstrip("/")


def apply_env():
    """Die Module daikin/homeconnect lesen ihre Zugangsdaten aus os.environ."""
    c = load()
    os.environ["DAIKIN_CLIENT_ID"] = c["daikin"]["client_id"]
    os.environ["DAIKIN_CLIENT_SECRET"] = c["daikin"]["client_secret"]
    os.environ["HC_CLIENT_ID"] = c["homeconnect"]["client_id"]
    if public_url():
        os.environ["DAIKIN_REDIRECT_URI"] = public_url() + "/daikin/callback"
    os.environ.setdefault("DAIKIN_TOKEN_FILE", os.path.join(DATA_DIR, "daikin-tokens.json"))
    os.environ.setdefault("HC_TOKEN_FILE", os.path.join(DATA_DIR, "homeconnect-tokens.json"))
