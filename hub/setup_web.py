"""Einrichtungsseite des Hubs:  http://<hub>:8080/setup

Nur aus dem Heimnetz erreichbar (direkte Verbindung – nicht über Tailscale/Caddy) und mit
Hub-Passwort geschützt. Beim allerersten Aufruf legt man Namen und Passwort fest und
verbindet das eigene iPhone per QR-Code als Besitzer.
"""
import html
import io
import ipaddress
import json
import os
import secrets
import subprocess
import time
import urllib.parse

import requests
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import hub_config
import people

SESSIONS = {}            # token → {"exp", "csrf"}
SESSION_TTL = 12 * 3600
LAST_INVITE = {}         # Sitzung → zuletzt erzeugter Einladungslink (für die QR-Anzeige)
FLASH = {}               # Sitzung → Meldung nach dem Speichern
CTX = {}

E = html.escape


# ====================================================================== Hilfen
def _direct_lan(request: Request) -> bool:
    """Direkt aus dem Heimnetz? Über Proxys (Tailscale Funnel, Caddy) kommt alles von 127.0.0.1 → abgelehnt."""
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (ip.is_private or ip.is_link_local) and not ip.is_loopback


def _session(request: Request):
    tok = request.cookies.get("hub_session", "")
    s = SESSIONS.get(tok)
    if s and s["exp"] > time.time():
        return tok, s
    return None, None


def _hubctl_bytes(*args, timeout=120):
    """Wie _hubctl, aber liefert die Rohausgabe (für die Sicherungsdatei)."""
    try:
        r = subprocess.run(["sudo", "-n", "/usr/local/sbin/hubctl", *args], capture_output=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr.decode(errors="replace")
    except FileNotFoundError:
        return 127, b"", "hubctl fehlt (Hub mit dem Installationsskript einrichten)"
    except subprocess.TimeoutExpired:
        return 124, b"", "Zeitüberschreitung"


async def _restore_upload(request):
    """Hochgeladene Sicherung nach /tmp/hub-restore.tgz legen und einspielen."""
    f = await request.form()
    up = f.get("backup")
    if not up or not getattr(up, "filename", ""):
        return False, "Bitte eine Sicherungsdatei (.tgz) auswählen."
    data = await up.read()
    if len(data) > 200_000_000 or data[:2] != b"\x1f\x8b":
        return False, "Das ist keine Sicherung des Hubs (.tgz)."
    fd = os.open("/tmp/hub-restore.tgz", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    rc, out = _hubctl("restore", timeout=120)
    return rc == 0, out[-300:] or ("ok" if rc == 0 else "Fehler")


def _hubctl(*args, timeout=60):
    try:
        r = subprocess.run(["sudo", "-n", "/usr/local/sbin/hubctl", *args], capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return 127, "hubctl fehlt (Hub mit dem Installationsskript einrichten)"
    except subprocess.TimeoutExpired:
        return 124, "Zeitüberschreitung"


def _qr_svg(text):
    try:
        import segno
    except ImportError:
        return "<p class=muted>(QR-Code: Paket „segno“ fehlt – Link unten verwenden)</p>"
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, dark="#0a0b0d", light="#ffffff")
    return buf.getvalue().decode()


CSS = """
:root{--bg:#0a0b0d;--card:#15171b;--line:#23262c;--text:#f3f4f6;--muted:#a0a6b0;--sun:#f6b73c;--ok:#46d39a;--bad:#ff8a5b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 80px}h1{font-size:28px;margin:8px 0 4px}h2{font-size:18px;margin:0 0 10px}
.muted{color:var(--muted)}.card{background:var(--card);border-radius:18px;padding:18px;margin:14px 0}
label{display:block;margin:10px 0 4px;color:var(--muted);font-size:13px}
input,select{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:#0f1114;color:var(--text);font-size:15px}
button,.btn{display:inline-block;margin-top:12px;padding:10px 16px;border-radius:12px;border:0;background:var(--sun);color:#111;font-weight:600;font-size:15px;cursor:pointer;text-decoration:none}
.btn2{background:var(--line);color:var(--text)}.danger{background:#3a1d17;color:var(--bad)}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>*{flex:1;min-width:180px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.pill{display:inline-block;padding:2px 10px;border-radius:99px;background:var(--line);font-size:12px}
nav{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0}nav a{color:var(--muted);text-decoration:none;padding:6px 12px;border-radius:99px;background:var(--card);font-size:13px}
nav a.on{background:var(--sun);color:#111}table{width:100%;border-collapse:collapse}td{padding:8px 4px;border-top:1px solid var(--line)}
.qr svg{width:220px;height:220px;border-radius:12px}code{background:#0f1114;padding:2px 6px;border-radius:6px;word-break:break-all}
.flash{background:#13261d;color:var(--ok);padding:10px 14px;border-radius:12px}.flash.err{background:#2a1712;color:var(--bad)}
"""

TABS = [("start", "Übersicht"), ("haus", "Geräte"), ("energie", "Energie"), ("klima", "Klima"),
        ("geraete", "Hausgeräte"), ("fern", "Fernzugriff"), ("personen", "Personen"), ("system", "System")]


def _page(title, body, tab=None, csrf=""):
    nav = ""
    if tab:
        nav = "<nav>" + "".join(f'<a class="{"on" if t == tab else ""}" href="/setup/{t}">{n}</a>' for t, n in TABS) + "</nav>"
    return HTMLResponse(f"""<!doctype html><html lang=de><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{E(title)} · Hub</title><style>{CSS}</style>
<main><div class=muted>{E(hub_config.get('home_name') or '')} · Hub {E(CTX.get('version', ''))}</div><h1>{E(title)}</h1>{nav}{body}</main></html>""")


def _form(action, inner, csrf, button="Speichern", enctype=""):
    enc = f' enctype="{enctype}"' if enctype else ""
    return f'<form method=post action="{action}?csrf={csrf}"{enc}>{inner}<button>{E(button)}</button></form>'


def _field(name, label, value="", kind="text", placeholder="", help_=""):
    h = f'<div class="muted" style="font-size:12px;margin-top:4px">{help_}</div>' if help_ else ""
    return f'<label>{E(label)}</label><input name="{name}" type="{kind}" value="{E(str(value or ""))}" placeholder="{E(placeholder)}">{h}'


def _flash(tok):
    msg = FLASH.pop(tok, None)
    if not msg:
        return ""
    ok, text = msg
    return f'<div class="flash {"" if ok else "err"}">{E(text)}</div>'


def _done(tok, tab, ok=True, text="Gespeichert."):
    FLASH[tok] = (ok, text)
    return RedirectResponse(f"/setup/{tab}", status_code=303)


# ====================================================================== Seiten
def attach(app, ctx):
    CTX.update(ctx)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/setup"):
            if not _direct_lan(request):
                return HTMLResponse("<h3>Die Einrichtung geht nur im Heimnetz.</h3>", status_code=403)
            if path not in ("/setup/login", "/setup/welcome", "/setup/welcome/restore"):
                if not hub_config.get("setup_password_hash"):
                    return RedirectResponse("/setup/welcome", status_code=303)
                tok, s = _session(request)
                if not s:
                    return RedirectResponse("/setup/login", status_code=303)
                if request.method == "POST":
                    if request.query_params.get("csrf") != s["csrf"]:
                        return HTMLResponse("Sitzung abgelaufen – bitte Seite neu laden.", status_code=400)
                request.state.tok, request.state.csrf = tok, s["csrf"]
        return await call_next(request)

    def new_session():
        tok = secrets.token_urlsafe(24)
        SESSIONS[tok] = {"exp": time.time() + SESSION_TTL, "csrf": secrets.token_urlsafe(16)}
        r = RedirectResponse("/setup/start", status_code=303)
        r.set_cookie("hub_session", tok, max_age=SESSION_TTL, httponly=True, samesite="strict")
        return tok, r

    # ---------------------------------------------------------------- Erster Start & Anmeldung
    @app.get("/setup")
    def setup_root():
        return RedirectResponse("/setup/start", status_code=303)

    @app.get("/setup/welcome")
    def welcome():
        if hub_config.get("setup_password_hash"):
            return RedirectResponse("/setup/login", status_code=303)
        body = f"""<div class=card><p>Willkommen! In drei Minuten ist dein Hub eingerichtet.</p>
        <form method=post action="/setup/welcome">
        {_field('home_name', 'Name deines Zuhauses', 'Mein Zuhause', placeholder='z. B. Haus am See')}
        {_field('pw', 'Hub-Passwort (für diese Einrichtungsseite)', '', 'password', help_='Mindestens 8 Zeichen. Die App braucht es nicht.')}
        {_field('pw2', 'Passwort wiederholen', '', 'password')}
        <button>Los geht’s</button></form></div>
        <div class=card><h2>Neuer Pi? Sicherung einspielen</h2>
        <p class=muted>Hast du eine Sicherung deines alten Hubs (.tgz)? Dann ist alles wieder da – Geräte, Personen, Szenen, Anmeldungen.</p>
        <form method=post action="/setup/welcome/restore" enctype="multipart/form-data">
        <input type=file name=backup accept=".tgz,.gz,application/gzip"><button class=btn2>Sicherung einspielen</button></form></div>"""
        return _page("Hub einrichten", body)

    @app.post("/setup/welcome/restore")
    async def welcome_restore(request: Request):
        if hub_config.get("setup_password_hash"):          # nur beim allerersten Start ohne Anmeldung
            return RedirectResponse("/setup/login", status_code=303)
        ok, msg = await _restore_upload(request)
        if not ok:
            return _page("Hub einrichten", f'<div class="flash err">{E(msg)}</div><a class=btn href="/setup/welcome">Zurück</a>')
        return _page("Sicherung eingespielt", '<div class="flash ok">Sicherung eingespielt – der Hub startet neu.</div>'
                     '<p>In etwa 20 Sekunden mit deinem bisherigen Hub-Passwort <a class=btn href="/setup/login">anmelden</a>.</p>')

    @app.post("/setup/welcome")
    async def welcome_save(request: Request):
        if hub_config.get("setup_password_hash"):
            return RedirectResponse("/setup/login", status_code=303)
        f = await request.form()
        pw, pw2 = f.get("pw", ""), f.get("pw2", "")
        if len(pw) < 8 or pw != pw2:
            return _page("Hub einrichten", '<div class="flash err">Passwort zu kurz oder nicht gleich.</div>'
                         '<a class=btn href="/setup/welcome">Zurück</a>')
        hub_config.update({"home_name": (f.get("home_name") or "Mein Zuhause").strip()[:60]})
        people.set_setup_password(pw)
        tok, r = new_session()
        if not people.has_owner():
            code, _ = people.create_owner("Besitzer")
            LAST_INVITE[tok] = ("Besitzer", CTX["invite_link"](code))
        FLASH[tok] = (True, "Fertig! Verbinde jetzt dein iPhone (Personen) und wähle deine Geräte-Quelle.")
        return r

    @app.get("/setup/login")
    def login_page():
        body = """<div class=card><form method=post action="/setup/login">
        <label>Hub-Passwort</label><input name=pw type=password autofocus><button>Anmelden</button></form></div>"""
        return _page("Anmelden", body)

    _login_fails = []

    @app.post("/setup/login")
    async def login(request: Request):
        now = time.time()
        _login_fails[:] = [t for t in _login_fails if now - t < 600]
        if len(_login_fails) >= 10:
            return _page("Anmelden", '<div class="flash err">Zu viele Versuche – bitte 10 Minuten warten.</div>')
        f = await request.form()
        if not people.check_setup_password(f.get("pw", "")):
            _login_fails.append(now)
            return _page("Anmelden", '<div class="flash err">Falsches Passwort.</div><a class=btn href="/setup/login">Nochmal</a>')
        _, r = new_session()
        return r

    @app.get("/setup/logout")
    def logout(request: Request):
        SESSIONS.pop(request.cookies.get("hub_session", ""), None)
        return RedirectResponse("/setup/login", status_code=303)

    # ---------------------------------------------------------------- Übersicht
    @app.get("/setup/start")
    def start(request: Request):
        tok, csrf = request.state.tok, request.state.csrf
        c = hub_config.load()
        h = CTX["home"]()
        dev_state = "nicht eingerichtet"
        if h is not None:
            try:
                n = sum(len(r["devices"]) for f in h.structure()["floors"] for r in f["rooms"])
                dev_state = f'<span class=ok>{n} Geräte</span> ({E(c["backend"])})'
            except Exception as e:
                dev_state = f'<span class=bad>{E(str(e)[:120])}</span>'
        rows = [
            ("Geräte", dev_state, "haus"),
            ("Energie (evcc)", '<span class=ok>verbunden</span>' if c["evcc"]["url"] else "nicht eingerichtet", "energie"),
            ("Klima (Daikin)", '<span class=ok>angemeldet</span>' if CTX["daikin"].auth.ready else "nicht angemeldet", "klima"),
            ("Hausgeräte (Home Connect)", '<span class=ok>angemeldet</span>' if CTX["homeconnect"].auth.ready else "nicht angemeldet", "geraete"),
            ("Fernzugriff", f'<span class=ok>{E(hub_config.public_url())}</span>' if hub_config.public_url() else "nur im Heimnetz", "fern"),
            ("Personen", f'{len(people.list_people())}', "personen"),
        ]
        table = "<table>" + "".join(f'<tr><td>{a}</td><td>{b}</td><td style="text-align:right"><a class=pill href="/setup/{t}">ändern</a></td></tr>'
                                    for a, b, t in rows) + "</table>"
        home_form = _form("/setup/save/home",
                          f'<div class=row><div>{_field("home_name", "Name des Zuhauses", c["home_name"])}</div>'
                          f'<div>{_field("place", "Ort / PLZ (für Sonnenauf- und -untergang)", "", placeholder="z. B. 82335 Berg", help_=f"Aktuell: {c["location"]["lat"]:.2f}, {c["location"]["lon"]:.2f}")}</div></div>',
                          csrf)
        return _page("Übersicht", _flash(tok) + f'<div class=card>{table}</div><div class=card><h2>Zuhause</h2>{home_form}</div>'
                     f'<p class=muted>App-Adresse im Heimnetz: <code>{E(CTX["local_url"]())}</code> · <a href="/setup/logout" class=muted>Abmelden</a></p>',
                     "start", csrf)

    @app.post("/setup/save/home")
    async def save_home(request: Request):
        f = await request.form()
        changes = {"home_name": (f.get("home_name") or "Mein Zuhause").strip()[:60]}
        place = (f.get("place") or "").strip()
        msg = "Gespeichert."
        if place:
            try:
                r = requests.get("https://nominatim.openstreetmap.org/search",
                                 params={"q": place, "format": "json", "limit": 1},
                                 headers={"User-Agent": "liesenberg-home-hub"}, timeout=10)
                hit = r.json()[0]
                changes["location"] = {"lat": round(float(hit["lat"]), 2), "lon": round(float(hit["lon"]), 2)}
                msg = f"Gespeichert – Ort: {hit.get('display_name', '')[:80]}"
            except Exception:
                return _done(request.state.tok, "start", False, "Ort nicht gefunden – bitte PLZ und Ort angeben.")
        hub_config.update(changes)
        return _done(request.state.tok, "start", True, msg)

    # ---------------------------------------------------------------- Geräte-Quelle
    @app.get("/setup/haus")
    def haus(request: Request):
        tok, csrf = request.state.tok, request.state.csrf
        c = hub_config.load()
        b = c["backend"]

        def radio(v, label):
            return f'<label style="display:flex;gap:8px;align-items:center;color:var(--text)"><input style="width:auto" type=radio name=backend value="{v}" {"checked" if b == v else ""}>{label}</label>'
        choose = _form("/setup/save/backend", radio("luxor", "LUXORliving (Theben IP1)") + radio("homeassistant", "Home Assistant")
                       + radio("demo", "Demo-Haus (zum Ausprobieren)"), csrf, "Übernehmen")
        lux = _form("/setup/save/luxor",
                    '<div class=row><div>' + _field("host", "Adresse des IP1", c["luxor"]["host"], placeholder="z. B. 192.168.1.50") + '</div><div>'
                    + _field("user", "Benutzer", c["luxor"]["user"]) + '</div></div>'
                    + _field("password", "Passwort", "", "password", "leer lassen = unverändert")
                    + '<label>Projektdatei aus LUXORplug (.lxp) – Räume & Geräte kommen automatisch daraus</label><input type=file name=lxp accept=".lxp">',
                    csrf, "Speichern & testen", "multipart/form-data")
        has = c["homeassistant"]["token"]
        hass = _form("/setup/save/ha",
                     _field("url", "Home-Assistant-Adresse", c["homeassistant"]["url"], placeholder="http://homeassistant.local:8123")
                     + _field("token", "Langlebiger Zugriffstoken", "", "password", "gespeichert – leer lassen = unverändert" if has else "",
                              "In Home Assistant: Profil → Sicherheit → Langlebige Zugriffstoken → Erstellen. "
                              "Räume und Etagen werden aus HA übernommen (Bereiche → Räume)."),
                     csrf, "Speichern & testen")
        names = ""
        h = CTX["home"]()
        if h is not None:
            try:
                rooms = [r["name"] for f in h.structure()["floors"] for r in f["rooms"]]
                cur = json.load(open(os.path.join(hub_config.DATA_DIR, "room-names.json"))) if os.path.exists(os.path.join(hub_config.DATA_DIR, "room-names.json")) else {}
                inner = "".join(f'<div class=row><div style="padding-top:18px">{E(r)}</div><div><input name="rn::{E(r)}" value="{E(cur.get(r, ""))}" placeholder="eigener Name"></div></div>' for r in rooms)
                names = f'<div class=card><h2>Räume umbenennen</h2><p class=muted>Nur für die App – an der Anlage ändert sich nichts.</p>{_form("/setup/save/roomnames", inner, csrf)}</div>'
            except Exception:
                pass
        body = (_flash(tok) + f'<div class=card><h2>Woher kommen Räume und Geräte?</h2>{choose}</div>'
                + (f'<div class=card><h2>LUXORliving</h2>{lux}</div>' if b == "luxor" else "")
                + (f'<div class=card><h2>Home Assistant</h2>{hass}</div>' if b == "homeassistant" else "")
                + names)
        return _page("Geräte", body, "haus", csrf)

    @app.post("/setup/save/backend")
    async def save_backend(request: Request):
        f = await request.form()
        v = f.get("backend")
        if v not in ("luxor", "homeassistant", "demo"):
            return _done(request.state.tok, "haus", False, "Bitte eine Quelle wählen.")
        hub_config.update({"backend": v})
        CTX["reset_backend"]()
        return _done(request.state.tok, "haus")

    @app.post("/setup/save/luxor")
    async def save_luxor(request: Request):
        f = await request.form()
        ch = {"host": (f.get("host") or "").strip(), "user": (f.get("user") or "admin").strip()}
        if f.get("password"):
            ch["password"] = f.get("password")
        hub_config.update({"luxor": ch, "backend": "luxor"})
        CTX["luxor"].reset()
        CTX["reset_backend"]()
        up = f.get("lxp")
        msg = []
        if up is not None and getattr(up, "filename", ""):
            data = await up.read()
            import lxp
            dest = os.path.join(hub_config.DATA_DIR, "project.lxp")
            tmp = dest + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            try:
                p = lxp.parse(tmp)
                os.replace(tmp, dest)
                msg.append(f"Projekt übernommen: {sum(len(r['functions']) for fl in p['floors'] for r in fl['rooms'])} Geräte")
            except Exception as e:
                os.remove(tmp)
                return _done(request.state.tok, "haus", False, f".lxp nicht lesbar: {e}")
        try:
            v = CTX["luxor"].get(1)
            msg.append("Verbindung zum IP1 ok" + (f" (Wind {v})" if v is not None else ""))
            ok = True
        except Exception as e:
            msg.append(f"IP1 nicht erreichbar: {e}")
            ok = False
        return _done(request.state.tok, "haus", ok, " · ".join(msg))

    @app.post("/setup/save/ha")
    async def save_ha(request: Request):
        f = await request.form()
        ch = {"url": (f.get("url") or "").strip().rstrip("/")}
        if f.get("token"):
            ch["token"] = f.get("token").strip()
        hub_config.update({"homeassistant": ch, "backend": "homeassistant"})
        CTX["reset_backend"]()
        try:
            h = CTX["home"]()
            n = sum(len(r["devices"]) for fl in h.structure()["floors"] for r in fl["rooms"])
            return _done(request.state.tok, "haus", True, f"Home Assistant verbunden – {n} Geräte gefunden.")
        except Exception as e:
            return _done(request.state.tok, "haus", False, f"Home Assistant: {e}")

    @app.post("/setup/save/roomnames")
    async def save_roomnames(request: Request):
        f = await request.form()
        names = {k[4:]: v.strip() for k, v in f.items() if k.startswith("rn::") and v.strip()}
        path = os.path.join(hub_config.DATA_DIR, "room-names.json")
        with open(path, "w") as fh:
            json.dump(names, fh, ensure_ascii=False, indent=2)
        return _done(request.state.tok, "haus")

    # ---------------------------------------------------------------- Energie
    @app.get("/setup/energie")
    def energie(request: Request):
        tok, csrf = request.state.tok, request.state.csrf
        url = hub_config.get("evcc.url")
        form = _form("/setup/save/evcc", _field("url", "evcc-Adresse", url, placeholder="http://192.168.1.20:7070",
                                                 help_="Leer lassen und „Suchen“ drücken – der Hub sucht evcc im Heimnetz."), csrf, "Speichern / Suchen")
        return _page("Energie", _flash(tok) + f'<div class=card><h2>evcc</h2><p class=muted>PV, Akku, Netz und Wallbox kommen aus <a style="color:var(--sun)" href="https://evcc.io">evcc</a>.</p>{form}</div>', "energie", csrf)

    @app.post("/setup/save/evcc")
    async def save_evcc(request: Request):
        f = await request.form()
        url = (f.get("url") or "").strip().rstrip("/")
        if not url:
            url = _find_evcc()
            if not url:
                return _done(request.state.tok, "energie", False, "evcc nicht gefunden – bitte Adresse eintragen.")
        try:
            requests.get(url + "/api/state", timeout=5).raise_for_status()
        except Exception as e:
            return _done(request.state.tok, "energie", False, f"evcc unter {url} nicht erreichbar: {e}")
        hub_config.update({"evcc": {"url": url}})
        return _done(request.state.tok, "energie", True, f"evcc gefunden: {url}")

    # ---------------------------------------------------------------- Klima (Daikin)
    @app.get("/setup/klima")
    def klima(request: Request):
        tok, csrf = request.state.tok, request.state.csrf
        c = hub_config.load()["daikin"]
        pub = hub_config.public_url()
        redirect = (pub + "/daikin/callback") if pub else None
        steps = f"""<ol class=muted style="padding-left:18px">
          <li>Auf <a style="color:var(--sun)" href="https://developer.cloud.daikineurope.com" target=_blank>developer.cloud.daikineurope.com</a> mit deinem Daikin-Konto anmelden → „Create application“.</li>
          <li>Als Redirect-URI eintragen: {f'<code>{E(redirect)}</code>' if redirect else '<b class=bad>erst Fernzugriff einrichten</b> (Daikin verlangt eine https-Adresse)'}</li>
          <li>Client-ID und Client-Secret hier eintragen, dann „Mit Daikin anmelden“.</li></ol>
          <p class=muted style="font-size:12px">Daikin erlaubt 200 Abfragen pro Tag – der Hub fragt deshalb alle 10 Minuten.</p>"""
        form = _form("/setup/save/daikin", '<div class=row><div>' + _field("client_id", "Client-ID", c["client_id"]) + '</div><div>'
                     + _field("client_secret", "Client-Secret", "", "password", "gespeichert – leer = unverändert" if c["client_secret"] else "") + '</div></div>', csrf)
        login = ""
        if c["client_id"] and redirect:
            login = f'<a class=btn href="/setup/daikin/login">Mit Daikin anmelden</a> ' + (
                '<span class=ok>✓ angemeldet</span>' if CTX["daikin"].auth.ready else "")
        return _page("Klima", _flash(tok) + f'<div class=card><h2>Daikin (Onecta)</h2>{steps}{form}{login}</div>', "klima", csrf)

    @app.post("/setup/save/daikin")
    async def save_daikin(request: Request):
        f = await request.form()
        ch = {"client_id": (f.get("client_id") or "").strip()}
        if f.get("client_secret"):
            ch["client_secret"] = f.get("client_secret").strip()
        hub_config.update({"daikin": ch})
        return _done(request.state.tok, "klima")

    @app.get("/setup/daikin/login")
    def daikin_login(request: Request):
        import daikin as dk
        state = secrets.token_urlsafe(16)
        dk.PENDING_STATES[state] = time.time()
        return RedirectResponse(dk.authorize_url(state), status_code=303)

    # ---------------------------------------------------------------- Hausgeräte (Home Connect)
    @app.get("/setup/geraete")
    def geraete(request: Request):
        tok, csrf = request.state.tok, request.state.csrf
        import homeconnect as hc
        c = hub_config.load()["homeconnect"]
        steps = """<ol class=muted style="padding-left:18px">
          <li>Auf <a style="color:var(--sun)" href="https://developer.home-connect.com" target=_blank>developer.home-connect.com</a> registrieren (gleiche E-Mail wie in der Home-Connect-App).</li>
          <li>„Register Application“: OAuth Flow <b>Device Flow</b>, Home Connect User Account for Testing = deine E-Mail.</li>
          <li>Die Client-ID hier eintragen und „Mit Home Connect anmelden“.</li></ol>"""
        form = _form("/setup/save/hc", _field("client_id", "Client-ID", c["client_id"]), csrf)
        st = hc.WEB_LOGIN
        status = ""
        if st.get("status") == "waiting":
            status = (f'<meta http-equiv=refresh content=5><div class=flash>Öffne <a style="color:var(--sun)" target=_blank href="{E(st["url"])}">{E(st["url"])}</a> '
                      f'und bestätige mit Code <b>{E(st["code"])}</b>. Diese Seite aktualisiert sich selbst.</div>')
        elif st.get("status") == "ok":
            status = '<div class=flash>✓ Home Connect angemeldet.</div>'
        elif st.get("status") == "error":
            status = f'<div class="flash err">Anmeldung fehlgeschlagen: {E(st.get("error", ""))}</div>'
        login = _form("/setup/hc/login", "", csrf, "Mit Home Connect anmelden") if c["client_id"] else ""
        ready = '<p class=ok>✓ angemeldet</p>' if CTX["homeconnect"].auth.ready else ""
        return _page("Hausgeräte", _flash(tok) + f'<div class=card><h2>Bosch / Siemens Home Connect</h2>{steps}{form}{status}{login}{ready}</div>', "geraete", csrf)

    @app.post("/setup/save/hc")
    async def save_hc(request: Request):
        f = await request.form()
        hub_config.update({"homeconnect": {"client_id": (f.get("client_id") or "").strip()}})
        return _done(request.state.tok, "geraete")

    @app.post("/setup/hc/login")
    def hc_login(request: Request):
        import homeconnect as hc
        try:
            hc.start_web_login()
        except Exception as e:
            return _done(request.state.tok, "geraete", False, str(e))
        return RedirectResponse("/setup/geraete", status_code=303)

    # ---------------------------------------------------------------- Fernzugriff
    @app.get("/setup/fern")
    def fern(request: Request):
        tok, csrf = request.state.tok, request.state.csrf
        c = hub_config.load()["remote"]
        rc, out = _hubctl("ts-status", timeout=10)
        try:
            ts = json.loads(out)
        except ValueError:
            ts = {"installed": False, "error": out}
        if not ts.get("installed", True) or rc == 127:
            ts_html = _form("/setup/ts/install", "<p>Tailscale ist noch nicht installiert.</p>", csrf, "Tailscale installieren")
        elif ts.get("BackendState") != "Running":
            ts_html = _form("/setup/ts/login", "<p>Tailscale ist installiert, aber noch nicht angemeldet.</p>", csrf, "Bei Tailscale anmelden")
        else:
            dns = (ts.get("Self", {}).get("DNSName") or "").rstrip(".")
            on = c.get("mode") == "tailscale" and c.get("url")
            ts_html = (f'<p>Angemeldet als <code>{E(dns)}</code></p>'
                       + (f'<p class=ok>✓ Öffentlich erreichbar: <code>{E(c["url"])}</code></p>' + _form("/setup/ts/funnel-off", "", csrf, "Fernzugriff ausschalten")
                          if on else _form("/setup/ts/funnel-on", "<p>Jetzt den Fernzugriff einschalten (Tailscale Funnel).</p>", csrf, "Fernzugriff einschalten")))
        custom = _form("/setup/save/remote", _field("url", "Eigene https-Adresse (z. B. mit MyFRITZ + eigenem Zertifikat)", c["url"] if c.get("mode") == "custom" else "",
                                                     placeholder="https://home.example.de"), csrf)
        body = (_flash(tok) + f"""<div class=card><h2>Tailscale (empfohlen)</h2><p class=muted>Damit erreicht die App dein Zuhause auch unterwegs –
        ohne Router-Einstellungen. Du brauchst ein kostenloses Tailscale-Konto. Beim ersten Einschalten fragt Tailscale
        evtl. im Browser, ob HTTPS und „Funnel“ erlaubt werden sollen – einfach bestätigen.</p>{ts_html}</div>
        <div class=card><h2>Eigene Adresse</h2><p class=muted>Nur falls du den Zugang schon selbst eingerichtet hast.</p>{custom}</div>""")
        return _page("Fernzugriff", body, "fern", csrf)

    @app.post("/setup/ts/install")
    def ts_install(request: Request):
        rc, out = _hubctl("ts-install", timeout=300)
        return _done(request.state.tok, "fern", rc == 0, "Tailscale installiert." if rc == 0 else out[-300:])

    @app.post("/setup/ts/login")
    def ts_login(request: Request):
        name = "".join(ch for ch in (hub_config.get("home_name") or "home").lower().replace(" ", "-") if ch.isalnum() or ch == "-")[:30] or "home"
        rc, out = _hubctl("ts-login", name + "-hub", timeout=30)
        if rc == 0 and out.startswith("https://"):
            return _page("Fernzugriff", f'<div class=card><p>Melde den Hub bei Tailscale an:</p><p><a class=btn target=_blank href="{E(out)}">Anmeldeseite öffnen</a></p>'
                         '<p class=muted>Danach hier zurückkommen:</p><a class="btn btn2" href="/setup/fern">Weiter</a></div>', "fern")
        return _done(request.state.tok, "fern", rc == 0, "Bereits angemeldet." if out == "already" else out[-300:])

    @app.post("/setup/ts/funnel-on")
    def funnel_on(request: Request):
        rc, out = _hubctl("funnel-on", timeout=40)
        link = next((w for w in out.split() if w.startswith("https://login.tailscale.com")), None)
        if link:
            return _page("Fernzugriff", f'<div class=card><p>Tailscale muss HTTPS/Funnel für deinen Hub einmal erlauben:</p>'
                         f'<a class=btn target=_blank href="{E(link)}">Bei Tailscale erlauben</a><p class=muted>Danach erneut „Fernzugriff einschalten“.</p>'
                         '<a class="btn btn2" href="/setup/fern">Zurück</a></div>', "fern")
        _, st = _hubctl("ts-status", timeout=10)
        try:
            dns = (json.loads(st).get("Self", {}).get("DNSName") or "").rstrip(".")
        except ValueError:
            dns = ""
        if rc != 0 or not dns:
            return _done(request.state.tok, "fern", False, out[-300:] or "Funnel ließ sich nicht einschalten")
        hub_config.update({"remote": {"mode": "tailscale", "url": f"https://{dns}"}})
        return _done(request.state.tok, "fern", True, f"Fernzugriff an: https://{dns} (kann bis zu 10 Minuten dauern, bis die Adresse überall bekannt ist)")

    @app.post("/setup/ts/funnel-off")
    def funnel_off(request: Request):
        _hubctl("funnel-off", timeout=20)
        hub_config.update({"remote": {"mode": "", "url": ""}})
        return _done(request.state.tok, "fern", True, "Fernzugriff ausgeschaltet.")

    @app.post("/setup/save/remote")
    async def save_remote(request: Request):
        f = await request.form()
        url = (f.get("url") or "").strip().rstrip("/")
        if url and not url.startswith("https://"):
            return _done(request.state.tok, "fern", False, "Die Adresse muss mit https:// beginnen.")
        hub_config.update({"remote": {"mode": "custom" if url else "", "url": url}})
        return _done(request.state.tok, "fern")

    # ---------------------------------------------------------------- Personen
    @app.get("/setup/personen")
    def personen(request: Request):
        tok, csrf = request.state.tok, request.state.csrf
        role_names = {"owner": "Besitzer", "full": "Vollzugriff", "guest": "Gast"}
        rows = ""
        for p in people.list_people():
            seen = time.strftime("%d.%m. %H:%M", time.localtime(p["last_seen"])) if p.get("last_seen") else "noch nie"
            rows += (f'<tr><td>{E(p["name"])}</td><td><span class=pill>{role_names.get(p["role"], p["role"])}</span></td><td class=muted>{seen}</td>'
                     f'<td style="text-align:right">{_form("/setup/people/remove", f"<input type=hidden name=pid value={E(p["id"])}>", csrf, "Entfernen").replace("<button>", "<button class=danger>")}</td></tr>')
        invite = ""
        if tok in LAST_INVITE:
            name, link = LAST_INVITE[tok]
            invite = (f'<div class=card><h2>Einladung für {E(name)}</h2><div class=qr>{_qr_svg(link)}</div>'
                      f'<p class=muted>Mit der iPhone-Kamera scannen – die App öffnet sich und verbindet sich. Der Code gilt 48 Stunden und nur einmal.</p>'
                      f'<p><code>{E(link)}</code></p></div>')
        form = _form("/setup/people/invite", '<div class=row><div>' + _field("name", "Name", "", placeholder="z. B. Laura") + '</div><div><label>Rolle</label>'
                     '<select name=role><option value=full>Vollzugriff</option><option value=guest>Gast (Licht, Rollläden, Musik)</option>'
                     '<option value=owner>Besitzer</option></select></div></div>', csrf, "Einladen")
        body = (_flash(tok) + invite + f'<div class=card><h2>Personen</h2><table>{rows}</table></div>'
                f'<div class=card><h2>Jemanden einladen</h2>{form}<p class=muted style="font-size:12px">Gäste sehen keine Tore/Türen und können Heizung und Klima nicht verstellen.</p></div>')
        return _page("Personen", body, "personen", csrf)

    @app.post("/setup/people/invite")
    async def people_invite(request: Request):
        f = await request.form()
        name = (f.get("name") or "").strip()[:40] or "Gast"
        role = f.get("role") if f.get("role") in people.ROLES else "guest"
        code, _ = people.create_invite(name, role)
        LAST_INVITE[request.state.tok] = (name, CTX["invite_link"](code))
        return RedirectResponse("/setup/personen", status_code=303)

    @app.post("/setup/people/remove")
    async def people_remove(request: Request):
        f = await request.form()
        pid = f.get("pid", "")
        owners = [p for p in people.list_people() if p["role"] == "owner"]
        if len(owners) == 1 and owners[0]["id"] == pid:
            return _done(request.state.tok, "personen", False, "Der letzte Besitzer kann nicht entfernt werden.")
        people.remove(pid)
        return _done(request.state.tok, "personen", True, "Entfernt – der Schlüssel dieser Person gilt ab sofort nicht mehr.")

    # ---------------------------------------------------------------- System
    @app.get("/setup/system")
    def system(request: Request):
        tok, csrf = request.state.tok, request.state.csrf
        latest = ""
        try:
            repo = hub_config.get("update.repo", "")
            if "github.com/" in repo:
                raw = repo.replace("github.com", "raw.githubusercontent.com") + f"/{hub_config.get('update.branch', 'main')}/hub/VERSION"
                v = requests.get(raw, timeout=5)
                if v.ok:
                    latest = v.text.strip()
        except Exception:
            pass
        upd = (f'<p>Installiert: <b>{E(CTX["version"])}</b>' + (f' · Neueste: <b>{E(latest)}</b>' if latest else "") + "</p>"
               + _form("/setup/system/update", "", csrf, "Jetzt aktualisieren"))
        pw = _form("/setup/system/password", _field("pw", "Neues Hub-Passwort", "", "password") + _field("pw2", "Wiederholen", "", "password"), csrf, "Passwort ändern")
        backup = ('<p class=muted>Enthält alles für eine Neuinstallation: Einstellungen, Zugangsdaten, Personen, Szenen, '
                  'LUXOR-Projekt, Daikin/Home-Connect-Anmeldung, evcc und Caddy. <b>Wie ein Schlüssel behandeln</b> – nur privat speichern.</p>'
                  '<a class=btn href="/setup/system/backup">Sicherung herunterladen</a>'
                  + _form("/setup/system/restore", '<label>Sicherung einspielen</label><input type=file name=backup accept=".tgz,.gz,application/gzip">',
                          csrf, "Einspielen", "multipart/form-data").replace("<button>", "<button class=btn2>"))
        dcfg = hub_config.get("drive_backup") or {}
        connected = os.path.exists(os.path.join(hub_config.DATA_DIR, "rclone.conf"))
        last = dcfg.get("last") or "noch nie"
        drive = ('<p class=muted>Der Hub lädt jede Nacht um 3:30 eine Sicherung in deinen Google-Drive-Ordner '
                 '(ältere als 120 Tage werden gelöscht). So ist alles sicher, auch wenn Pi oder SD-Karte kaputtgehen.</p>')
        if connected:
            drive += (f'<p>Status: <span class=ok>verbunden</span> · Ordner <code>{E(dcfg.get("folder", "Hub-Sicherung"))}</code>'
                      f' · letzte Sicherung: <b>{E(last)}</b></p>'
                      + _form("/setup/drive/run", "", csrf, "Jetzt nach Google Drive sichern")
                      + _form("/setup/drive/disconnect", "", csrf, "Trennen").replace("<button>", "<button class=btn2>"))
        else:
            drive += ('<ol class=muted style="padding-left:18px;line-height:1.7">'
                      '<li>Am Mac im Terminal einmalig: <code>brew install rclone</code> '
                      '(ohne Homebrew: <a style="color:var(--sun)" href="https://rclone.org/downloads/" target=_blank>rclone.org/downloads</a>)</li>'
                      '<li>Dann: <code>rclone authorize "drive"</code> – es öffnet sich Google, mit deinem Konto anmelden und erlauben.</li>'
                      '<li>Im Terminal erscheint ein Text, der mit <code>{"access_token"</code> beginnt – komplett kopieren und hier einfügen:</li></ol>'
                      + _form("/setup/drive/connect",
                              '<label>Token von rclone</label><textarea name=token rows=4 style="width:100%" placeholder=\'{"access_token":"…"}\'></textarea>'
                              + _field("folder", "Ordner in Google Drive", "Hub-Sicherung"),
                              csrf, "Google Drive verbinden"))
        body = (_flash(tok) + f'<div class=card><h2>Sicherung in Google Drive</h2>{drive}</div>'
                f'<div class=card><h2>Sicherung</h2>{backup}</div>'
                f'<div class=card><h2>Update</h2>{upd}</div><div class=card><h2>Hub-Passwort</h2>{pw}</div>'
                f'<div class=card><h2>Fehlersuche</h2><a class="btn btn2" href="/setup/system/logs">Protokoll anzeigen</a> '
                f'{_form("/setup/system/restart", "", csrf, "Hub neu starten").replace("<button>", "<button class=btn2>")}</div>')
        return _page("System", body, "system", csrf)

    @app.get("/setup/system/backup")
    def do_backup(request: Request):
        rc, data, err = _hubctl_bytes("backup")
        if rc != 0 or len(data) < 100:
            return _done(request.state.tok, "system", False, f"Sicherung fehlgeschlagen: {err[-200:] or rc}")
        name = "".join(ch for ch in (hub_config.get("home_name") or "hub") if ch.isalnum()) or "hub"
        fn = f"hub-sicherung-{name}-{time.strftime('%Y-%m-%d')}.tgz"
        return Response(data, media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{fn}"', "Cache-Control": "no-store"})

    @app.post("/setup/system/restore")
    async def do_restore(request: Request):
        ok, msg = await _restore_upload(request)
        return _done(request.state.tok, "system", ok, msg + (" – der Hub startet neu." if ok else ""))

    @app.post("/setup/drive/connect")
    async def drive_connect(request: Request):
        f = await request.form()
        token = (f.get("token") or "").strip()
        # rclone gibt manchmal Text drumherum aus – nur das JSON nehmen
        if "{" in token:
            token = token[token.index("{"):token.rindex("}") + 1]
        try:
            t = json.loads(token)
            assert t.get("access_token") and t.get("refresh_token")
        except Exception:
            return _done(request.state.tok, "system", False, "Das ist kein gültiger Token – bitte die ganze Zeile {\"access_token\"…} kopieren.")
        folder = "".join(ch for ch in (f.get("folder") or "Hub-Sicherung") if ch.isalnum() or ch in " ._-")[:60] or "Hub-Sicherung"
        path = os.path.join(hub_config.DATA_DIR, "rclone.conf")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"[gdrive]\ntype = drive\nscope = drive\ntoken = {json.dumps(t, separators=(',', ':'))}\n")
        hub_config.update({"drive_backup": {"folder": folder}})
        rc, out = _hubctl("drive-backup", timeout=300)
        if rc == 0:
            hub_config.update({"drive_backup": {"last": time.strftime("%d.%m.%Y %H:%M")}})
            return _done(request.state.tok, "system", True, f"Verbunden – erste Sicherung liegt in Google Drive ({out[-80:]}).")
        return _done(request.state.tok, "system", False, f"Verbunden, aber das Hochladen klappte nicht: {out[-200:]}")

    @app.post("/setup/drive/run")
    def drive_run(request: Request):
        ok, msg = run_drive_backup()
        return _done(request.state.tok, "system", ok, msg)

    @app.post("/setup/drive/disconnect")
    def drive_disconnect(request: Request):
        try:
            os.remove(os.path.join(hub_config.DATA_DIR, "rclone.conf"))
        except OSError:
            pass
        return _done(request.state.tok, "system", True, "Google Drive getrennt.")

    @app.post("/setup/system/update")
    def do_update(request: Request):
        rc, out = _hubctl("update", timeout=300)
        return _done(request.state.tok, "system", rc == 0, (out[-300:] or "ok") + (" – der Hub startet neu." if rc == 0 else ""))

    @app.post("/setup/system/restart")
    def do_restart(request: Request):
        _hubctl("restart")
        return _done(request.state.tok, "system", True, "Der Hub startet neu – in 10 Sekunden die Seite neu laden.")

    @app.post("/setup/system/password")
    async def do_password(request: Request):
        f = await request.form()
        if len(f.get("pw", "")) < 8 or f.get("pw") != f.get("pw2"):
            return _done(request.state.tok, "system", False, "Passwort zu kurz oder nicht gleich.")
        people.set_setup_password(f.get("pw"))
        return _done(request.state.tok, "system", True, "Passwort geändert.")

    @app.get("/setup/system/logs")
    def logs(request: Request):
        _, out = _hubctl("logs", timeout=15)
        return _page("Protokoll", f'<div class=card><pre style="white-space:pre-wrap;font-size:12px">{E(out)}</pre></div>', "system")


# ====================================================================== evcc im Heimnetz finden
def _find_evcc():
    import concurrent.futures
    import socket
    cands = ["http://127.0.0.1:7070", "http://evcc.local:7070"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        base = ".".join(s.getsockname()[0].split(".")[:3])
        s.close()
        cands += [f"http://{base}.{i}:7070" for i in range(1, 255)]
    except OSError:
        pass

    def probe(u):
        try:
            r = requests.get(u + "/api/state", timeout=0.8)
            return u if r.ok and ("pvPower" in r.text or "loadpoints" in r.text) else None
        except requests.RequestException:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for res in ex.map(probe, cands):
            if res:
                return res
    return None


# ====================================================================== Nächtliche Sicherung nach Google Drive
def run_drive_backup():
    if not os.path.exists(os.path.join(hub_config.DATA_DIR, "rclone.conf")):
        return False, "Google Drive ist nicht verbunden."
    rc, out = _hubctl("drive-backup", timeout=600)
    if rc == 0:
        hub_config.update({"drive_backup": {"last": time.strftime("%d.%m.%Y %H:%M"), "last_day": time.strftime("%Y-%m-%d")}})
        return True, f"Gesichert: {out[-80:]}"
    print(f"[SICHERUNG] Google Drive fehlgeschlagen: {out[-200:]}", flush=True)
    return False, f"Fehlgeschlagen: {out[-200:]}"


def _nightly():
    import threading
    def loop():
        tried = None
        while True:
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            # einmal pro Tag ab 3:30 – auch nachgeholt, wenn der Pi um 3:30 aus war (aber nicht bei jedem Neustart)
            done = (hub_config.get("drive_backup") or {}).get("last_day") == today
            if (now.tm_hour, now.tm_min) >= (3, 30) and not done and tried != today:
                tried = today
                if os.path.exists(os.path.join(hub_config.DATA_DIR, "rclone.conf")):
                    run_drive_backup()
            time.sleep(60)
    threading.Thread(target=loop, daemon=True).start()


_nightly()
