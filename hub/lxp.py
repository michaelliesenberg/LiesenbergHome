"""Liest die LUXORplug-Projektdatei (.lxp) und baut Etagen → Räume → Funktionen.

Die KNX-Gruppenadressen der .lxp werden in die Datenpunkt-IDs der IP1-REST-API übersetzt.
Das Ergebnis hat genau das Format, das die App erwartet.
"""
import time
import xml.etree.ElementTree as ET

NS = {"lx": "http://www.theben.de/LUXORplug/2016/12"}
APPLE_EPOCH = 978307200  # Swift-Date zählt ab 2001-01-01

# Die `address` in der .lxp ist eine KNX-Gruppenadresse (Haupt/Mitte/Unter als 16 Bit).
# Das IP1 nummeriert seine REST-Datenpunkte pro Kanal lückenlos durch – die Blöcke schließen
# direkt aneinander an, deshalb verschiebt ein zusätzlicher Schaltkanal alle Dimmer usw.:
#   S1..  Schalten/Rückmeldung            1/0/x, 1/1/x   → 151 + 2x (+1)
#   D1..  Dimmer (8er-Blöcke)             2/0..5/x      → D + 8x + Versatz,   D = 151 + 2·(Anzahl S)
#   J1..  Jalousie (7er-Blöcke)           3/0..6/x      → J + 7x + Versatz,   J = D + 8·(Anzahl D)
#   H2..  Heizung (7er-Blöcke)            4/0..4/x      → H + 7(x-1) + Mitte, H = J + 7·(Anzahl J) + 7
# Anzahl = höchste Unteradresse + 1 in der .lxp (geprüft: 23 S → D=197, J=653, H=842 bei 824 Datenpunkten;
# 25 S → D=201, J=657, H=846 bei 828 Datenpunkten)
_D_OFF = {0: 0, 1: 1, 2: 2, 3: 4, 4: 3, 5: 5}          # Schalten, rel, abs, Status%, StatusOnOff, Begrenzung
_J_OFF = {0: 0, 1: 1, 2: 2, 3: 3, 4: 5, 5: 6, 6: 4}    # Auf/Ab, Step, Höhe, Lamelle, StatusHöhe, StatusLamelle, Fenster


_base = {"D": 197, "J": 653, "H": 842}               # aus der zuletzt gelesenen .lxp (siehe layout)


def layout(root):
    """Blockanfänge D/J/H aus der höchsten Unteradresse je Kanaltyp der .lxp."""
    top = {1: -1, 2: -1, 3: -1}
    for dp in root.iter(f"{{{NS['lx']}}}datapoint"):
        addr = dp.get("address")
        if addr and addr.isdigit():
            a = int(addr)
            if a >> 11 in top:
                top[a >> 11] = max(top[a >> 11], a & 255)
    d = 151 + 2 * (top[1] + 1)
    j = d + 8 * (top[2] + 1)
    return {"D": d, "J": j, "H": j + 7 * (top[3] + 1) + 7}


def rest_id(addr, base=None):
    b = base or _base
    main, mid, sub = addr >> 11, (addr >> 8) & 7, addr & 255
    if main == 1 and mid in (0, 1):
        return 151 + 2 * sub + mid
    if main == 2 and mid in _D_OFF:
        return b["D"] + 8 * sub + _D_OFF[mid]
    if main == 3 and mid in _J_OFF:
        return b["J"] + 7 * sub + _J_OFF[mid]
    if main == 4 and mid <= 4 and sub >= 1:
        return b["H"] + 7 * (sub - 1) + mid
    return None


def _datapoints(actuator, base):
    own, group = {}, {}
    for dp in actuator.findall("lx:datapoint", NS):
        role, addr = dp.get("role"), dp.get("address")
        if not role or not addr or not addr.isdigit():
            continue
        rid = rest_id(int(addr), base)
        if rid is None:          # Zentral-/Sicherheitsobjekte gibt es nicht als REST-Datenpunkt
            continue
        if role.startswith("group@"):
            group[role.split("/")[-1]] = rid
        else:
            own[role] = rid
    for k, v in group.items():  # Gruppen-Datenpunkt nur, wenn kein eigener existiert
        own.setdefault(k, v)
    return own


def _kind(ftype, dps):
    if ftype == "Dimming":
        return "dimmer" if "Dimmen%" in dps else "light"
    return {"Switch": "light", "Blind": "blind", "Heating": "heating"}.get(ftype, "other")


def parse(path):
    root = ET.parse(path).getroot()
    base = layout(root)
    actuators = {a.get("id"): a for a in root.iter(f"{{{NS['lx']}}}actuator")}
    floors = []
    # LUXORliving zeigt das oberste Geschoss zuerst – in der Datei steht der Keller vorne
    for floor in reversed(root.findall("lx:floors/lx:floor", NS)):
        rooms = []
        for room in floor.findall("lx:room", NS):
            funcs = []
            for f in room.findall("lx:function", NS):
                ref = f.find("lx:actuator-ref", NS)
                act = actuators.get(ref.get("ref")) if ref is not None else None
                if act is None:
                    continue
                dps = _datapoints(act, base)
                item = {"id": f.get("id"), "name": act.get("name", "Gerät").strip(),
                        "kind": _kind(f.get("type", ""), dps), "datapoints": dps}
                # Impuls-Relais (Tor, Türöffner): LUXOR-„useCase" 4 = Treppenlicht/Impuls
                params = {p.get("name"): p.get("value") for p in act.findall("lx:parameter", NS)}
                if item["kind"] == "light" and params.get("useCase") == "4":
                    item["pulse"] = True
                # Heizungen mit identischen Datenpunkten nur einmal zeigen (wie LUXORliving)
                dup = next((i for i, x in enumerate(funcs) if x["kind"] == "heating" == item["kind"]
                            and x["datapoints"].get("Istwert") == dps.get("Istwert")), None)
                if dup is not None:
                    funcs[dup] = item
                else:
                    funcs.append(item)
            rooms.append({"id": room.get("id"), "name": room.get("name", "Raum"),
                          "icon": room.get("icon", ""), "functions": funcs})
        floors.append({"id": floor.get("id"), "name": floor.get("name", "Etage"), "rooms": rooms})
    _base.update(base)
    return {"name": root.get("name") or "LUXORliving",
            "importedAt": time.time() - APPLE_EPOCH,
            "floors": floors}
