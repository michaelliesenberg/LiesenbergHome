"""Liest die LUXORplug-Projektdatei (.lxp) und baut Etagen → Räume → Funktionen.

Die KNX-Gruppenadressen der .lxp werden in die Datenpunkt-IDs der IP1-REST-API übersetzt.
Das Ergebnis hat genau das Format, das die App erwartet.
"""
import time
import xml.etree.ElementTree as ET

NS = {"lx": "http://www.theben.de/LUXORplug/2016/12"}
APPLE_EPOCH = 978307200  # Swift-Date zählt ab 2001-01-01

# Die `address` in der .lxp ist eine KNX-Gruppenadresse (Haupt/Mitte/Unter als 16 Bit).
# Das IP1 nummeriert seine REST-Datenpunkte pro Kanal fest durch:
#   S1..  Schalten/Rückmeldung            1/0/x, 1/1/x   → 151 + 2x (+1)
#   D1..  Dimmer (8er-Blöcke)             2/0..5/x      → 197 + 8x + Versatz
#   J1..  Jalousie (7er-Blöcke)           3/0..6/x      → 653 + 7x + Versatz
#   H2..  Heizung (7er-Blöcke)            4/0..4/x      → 842 + 7(x-1) + Mitte
# (geprüft gegen alle 824 Datenpunkte des IP1)
_D_OFF = {0: 0, 1: 1, 2: 2, 3: 4, 4: 3, 5: 5}          # Schalten, rel, abs, Status%, StatusOnOff, Begrenzung
_J_OFF = {0: 0, 1: 1, 2: 2, 3: 3, 4: 5, 5: 6, 6: 4}    # Auf/Ab, Step, Höhe, Lamelle, StatusHöhe, StatusLamelle, Fenster


def rest_id(addr):
    main, mid, sub = addr >> 11, (addr >> 8) & 7, addr & 255
    if main == 1 and mid in (0, 1):
        return 151 + 2 * sub + mid
    if main == 2 and mid in _D_OFF:
        return 197 + 8 * sub + _D_OFF[mid]
    if main == 3 and mid in _J_OFF:
        return 653 + 7 * sub + _J_OFF[mid]
    if main == 4 and mid <= 4 and sub >= 1:
        return 842 + 7 * (sub - 1) + mid
    return None


def _datapoints(actuator):
    own, group = {}, {}
    for dp in actuator.findall("lx:datapoint", NS):
        role, addr = dp.get("role"), dp.get("address")
        if not role or not addr or not addr.isdigit():
            continue
        rid = rest_id(int(addr))
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
                dps = _datapoints(act)
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
    return {"name": root.get("name") or "LUXORliving",
            "importedAt": time.time() - APPLE_EPOCH,
            "floors": floors}
