"""„Aktion nötig“ (ab Hub 2.55): alles, worum sich gerade jemand kümmern muss, als eine Liste für die Startseite der App.

GET /api/attention → {"items": [...]}, sortiert: critical vor warning vor info, dann das Neueste zuerst.

Ein Eintrag:
  id        stabil, z. B. "water.lock" (die App merkt sich damit nichts, aber Listen-Animationen)
  kind      Quelle: water | vacuum | maintenance | gate
  severity  "critical" | "warning" | "info"
  icon      SF-Symbol-Name (Android bildet ihn auf eigene Symbole ab)
  title, text
  since     Zeitstempel (s) oder None
  open      Bildschirm der App: water | vacuum | maintenance | gates
  action    optional {label, method, path, body, confirm?} – ein Knopf direkt im Banner
  dismiss   optional {label, method, path, body} – „erledigt / stimmt nicht“

Die Quellen (providers) sind kleine Funktionen (person_perms) → [Eintrag]; Fehler einer Quelle verschlucken wir,
damit ein hängendes Gerät nicht die ganze Liste kaputt macht. Rollen: Gäste bekommen keine Ventil-Knöpfe
(wie /api/water/valve: nur Besitzer + Vollzugriff), Tor-Hinweise nur mit Tor-Recht.
"""
import time

SEVERITY = {"critical": 0, "warning": 1, "info": 2}
SALT_WARN_DAYS = 7          # wie judo.SALT_WARN_DAYS
SALT_CRIT_DAYS = 2


def item(id, kind, severity, icon, title, text, open=None, since=None, action=None, dismiss=None):
    return {"id": id, "kind": kind, "severity": severity, "icon": icon, "title": title, "text": text,
            "since": since, "open": open, "action": action, "dismiss": dismiss}


def _hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


# ---------------------------------------------------------------- Wasser (JUDO)
OPEN_WATER = {"label": "Wasser öffnen", "method": "POST", "path": "/api/water/valve", "body": {"open": True},
              "confirm": "Erst prüfen, dass kein Rohr/Gerät leckt."}
CLOSE_WATER = {"label": "Wasser absperren", "method": "POST", "path": "/api/water/valve", "body": {"open": False},
               "confirm": "Das Wasser im ganzen Haus wird abgesperrt."}


def water_items(s, can_edit, now=None, leak_hours=2):
    """s = judo.state()"""
    if not s or not s.get("configured"):
        return []
    now = now or time.time()
    out = []
    lock = s.get("suspect_lock")
    if lock:
        out.append(item(
            "water.lock", "water", "critical", "drop.triangle.fill", "Wasser vermutlich abgesperrt",
            f"Nach {lock.get('draw_l')} l / {lock.get('draw_min')} min Dauerentnahme fließt seit {_hm(lock['since'])} Uhr "
            "kein Wasser mehr – wahrscheinlich hat der Leckageschutz der JUDO-Enthärtung abgesperrt."
            + ("" if can_edit else " Bitte jemanden mit Vollzugriff Bescheid geben."),
            open="water", since=lock.get("since"),
            action=OPEN_WATER if can_edit else None,
            dismiss={"label": "Wasser läuft", "method": "POST", "path": "/api/water/lock_ok", "body": {}} if can_edit else None))
    elif s.get("valve") == "closed":
        out.append(item(
            "water.closed", "water", "warning", "drop.triangle", "Wasser ist abgesperrt",
            "Das Wasser wurde über die App abgesperrt." + ("" if can_edit else " Öffnen kann nur jemand mit Vollzugriff."),
            open="water", since=s.get("valve_ts"), action=OPEN_WATER if can_edit else None))
    since = s.get("running_since")
    if since and now - since >= leak_hours * 3600 and s.get("valve") != "closed" and not lock:
        h = int((now - since) // 3600)
        out.append(item(
            "water.leak", "water", "critical", "exclamationmark.triangle.fill", f"Wasser läuft seit über {h} Stunden",
            f"Ununterbrochener Verbrauch seit {_hm(since)} Uhr – vielleicht ein Leck oder ein offener Hahn.",
            open="water", since=since, action=CLOSE_WATER if can_edit else None))
    days = s.get("salt_days")
    if days is not None and 0 <= days <= SALT_WARN_DAYS:
        out.append(item(
            "water.salt", "water", "warning" if days <= SALT_CRIT_DAYS else "info", "cube.fill", "Salz nachfüllen",
            "Das Regeneriersalz der Enthärtung reicht nur noch "
            + ("heute." if days == 0 else f"etwa {days} Tag{'e' if days != 1 else ''}."),
            open="water"))
    return out


# ---------------------------------------------------------------- Saugroboter (Saros-Wassertanks, roborock_vac)
def vacuum_items(lst):
    """lst = vacuum.list(); Hinweise kommen fertig aus roborock_vac._tank_view (dieselben wie das alte iOS-Banner)."""
    out = []
    for d in (lst or {}).get("devices") or []:
        duid = d.get("duid") or d.get("id")
        for n in d.get("notices") or []:
            act = None
            if n.get("action") and duid:
                act = {"label": "Geleert und ausgespült", "method": "POST", "path": f"/api/vacuum/{duid}",
                       "body": {"action": n["action"]}}
            out.append(item(
                f"vacuum.{duid}.{n.get('id')}", "vacuum", "warning" if n.get("id") == "dirty_full" else "info",
                n.get("icon") or "drop.fill", n.get("title") or "", n.get("text") or "", open="vacuum",
                since=d.get("dirtySince") if str(n.get("id", "")).startswith("dirty") else None, action=act))
    return out


# ---------------------------------------------------------------- Wartung (maintenance.py)
def maintenance_items(lst):
    due = [x for x in (lst or {}).get("items") or [] if x.get("due")]
    if not due:
        return []
    if len(due) == 1:
        x = due[0]
        act = None
        if not x.get("auto"):
            act = {"label": "Erledigt", "method": "POST", "path": f"/api/maintenance/{x['id']}/done", "body": {}}
        text = f"{x.get('device')}: fällig." if x.get("auto") and x.get("device") else "Ist jetzt fällig."
        return [item(f"maintenance.{x['id']}", "maintenance", "info", "wrench.and.screwdriver", x.get("title") or "Wartung",
                     text, open="maintenance", action=act)]
    names = ", ".join(x.get("title") or "?" for x in due[:3]) + (" …" if len(due) > 3 else "")
    return [item("maintenance.due", "maintenance", "info", "wrench.and.screwdriver", f"{len(due)} Wartungen fällig",
                 names, open="maintenance")]


# ---------------------------------------------------------------- Sammeln
def collect(providers):
    """providers: Liste von Funktionen () → [Eintrag]."""
    items = []
    for fn in providers:
        try:
            items.extend(fn() or [])
        except Exception as e:
            print(f"[ACHTUNG] {getattr(fn, '__name__', fn)}: {e}", flush=True)
    items.sort(key=lambda x: (SEVERITY.get(x["severity"], 9), -(x.get("since") or 0)))
    return {"items": items}
