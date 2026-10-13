"""Kartenlinien für Regenradar und Windkarte: Flüsse, Seen, Landes- und Bundeslandgrenzen (Natural Earth, gemeinfrei).

Die GeoJSON-Dateien werden einmal geladen (DATA_DIR/naturalearth), auf den Ausschnitt um das Haus zugeschnitten,
vereinfacht und im Speicher gehalten. Die App zeichnet sie als scharfe weiße Linien über das graue Relief.
"""
import json
import os
import threading

import requests

from hub_config import DATA_DIR

BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
LAYERS = {"rivers": "ne_10m_rivers_lake_centerlines", "rivers_eu": "ne_10m_rivers_europe", "lakes": "ne_10m_lakes",
          "lakes_eu": "ne_10m_lakes_europe", "borders": "ne_10m_admin_0_boundary_lines_land",
          "states": "ne_10m_admin_1_states_provinces_lines"}
DIR = os.path.join(DATA_DIR, "naturalearth")
_lock = threading.Lock()
_cache = {}


def _file(name):
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, name + ".geojson")
    if not os.path.exists(path):
        r = requests.get(BASE + name + ".geojson", timeout=120)
        if r.status_code != 200:
            return None
        with open(path + ".tmp", "wb") as f:
            f.write(r.content)
        os.replace(path + ".tmp", path)
    with open(path) as f:
        return json.load(f)


def _lines(geom):
    t, c = geom.get("type"), geom.get("coordinates") or []
    if t == "LineString":
        return [c]
    if t == "MultiLineString":
        return c
    if t == "Polygon":
        return c[:1]
    if t == "MultiPolygon":
        return [p[0] for p in c if p]
    return []


def _clip(line, bb):
    """Teilstücke innerhalb des Ausschnitts (grob, punktweise) – reicht für die Darstellung."""
    out, cur = [], []
    w, s, e, n = bb
    for lon, lat in ((p[0], p[1]) for p in line):
        if w <= lon <= e and s <= lat <= n:
            cur.append([round(lat, 4), round(lon, 4)])
        elif cur:
            out.append(cur); cur = []
    if cur:
        out.append(cur)
    return [c for c in out if len(c) > 1]


def lines(lat, lon, dlat=1.6, dlon=2.4):
    key = (round(lat, 2), round(lon, 2))
    with _lock:
        if key in _cache:
            return _cache[key]
        bb = (lon - dlon, lat - dlat, lon + dlon, lat + dlat)
        res = {"rivers": [], "lakes": [], "borders": [], "states": []}
        for kind, name in LAYERS.items():
            try:
                gj = _file(name)
            except Exception as e:
                print(f"[KARTE] {name}: {e}", flush=True)
                gj = None
            target = kind.replace("_eu", "")
            for ft in (gj or {}).get("features", []):
                for ln in _lines(ft.get("geometry") or {}):
                    if target == "lakes":          # Seen als ganze Fläche (zum Füllen), wenn sie im Ausschnitt liegen
                        if any(bb[0] <= p[0] <= bb[2] and bb[1] <= p[1] <= bb[3] for p in ln):
                            res["lakes"].append([[round(p[1], 4), round(p[0], 4)] for p in ln])
                    else:
                        res[target].extend(_clip(ln, bb))
        res["source"] = "Natural Earth (gemeinfrei)"
        _cache[key] = res
        return res
