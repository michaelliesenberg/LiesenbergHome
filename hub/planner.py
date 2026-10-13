"""Energie-Planer: Wann lohnt sich was? (Stufe 1 – Empfehlungen, nichts wird automatisch geschaltet)

Eingaben (alles schon vorhanden):
  • Solarprognose und Strompreise aus evcc (/api/state → forecast.solar / forecast.grid / forecast.feedin, je Viertelstunde)
  • typischer Hausverbrauch je Viertelstunde des Tages – gelernt aus der Minuten-Aufzeichnung des Hubs (letzte 14 Tage)
  • Akku-Stand aus evcc

Ausgabe: Zeitleiste der nächsten 36 h (Sonne, Grundverbrauch, Überschuss, Preis) und für jedes Gerät
(Waschmaschine, Spülmaschine, Trockner, Auto) das beste Startfenster samt Ersparnis gegenüber „jetzt starten".
smartstart.py nutzt dasselbe Fenster, um Geräte über Home Connect automatisch zu starten.
"""
import time
from datetime import date, datetime, timedelta

SLOT = 900                     # 15 Minuten
HORIZON = 36 * 3600

# Typische Geräte: Laufzeit und mittlere Leistung (W) – bewusst grob, reicht für die Fensterwahl
APPLIANCES = [
    {"id": "washer", "title": "Waschmaschine", "icon": "washer", "minutes": 120, "watts": 800},
    {"id": "dishwasher", "title": "Spülmaschine", "icon": "dishwasher", "minutes": 150, "watts": 700},
    {"id": "dryer", "title": "Trockner", "icon": "dryer", "minutes": 120, "watts": 1500},
]
CAR = {"id": "car", "title": "Auto laden (20 kWh)", "icon": "car", "minutes": 180, "watts": 7000}
# Saugroboter (Roborock Saros mit Station): Akku nachladen ~70 Wh, Mopp mit warmem Wasser waschen ~100 Wh,
# Heißluft-Trocknen ~3 h × ~100 W, Absaugen kurz. Geschätzt ≈ 0,5 kWh über ~4 h – bewusst eher zu hoch.
VACUUM = {"id": "vacuum", "title": "Saugroboter", "icon": "vacuum", "minutes": 240, "watts": 125, "estimated": True}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _series(raw):
    """evcc-Zeitreihen → {slotbeginn: wert}. Formen: [[start, ende, wert]], [[zeit, wert]] oder [{ts, val}]."""
    out = {}
    for p in raw or []:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            t, v = _num(p[0]), _num(p[-1])
        elif isinstance(p, dict):
            ts = p.get("ts") or p.get("start")
            try:
                t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() if isinstance(ts, str) else _num(ts)
            except ValueError:
                t = None
            v = _num(p.get("val", p.get("value", p.get("price"))))
        else:
            continue
        if t is not None and v is not None:
            out[int(t) // SLOT * SLOT] = v
    return out


def baseline_profile(energy_log, days=14):
    """Mittlerer Hausverbrauch (ohne Auto) je Viertelstunde des Tages, aus der Aufzeichnung des Hubs."""
    sums, counts = [0.0] * 96, [0] * 96
    today = date.today()
    for i in range(1, days + 1):
        for r in energy_log.rows(today - timedelta(days=i)):
            dt = datetime.fromtimestamp(r["t"])
            k = (dt.hour * 60 + dt.minute) // 15
            sums[k] += max((r.get("home") or 0) - (r.get("car") or 0), 0)
            counts[k] += 1
    known = [s / c for s, c in zip(sums, counts) if c]
    fallback = sum(known) / len(known) if known else 500.0     # ohne Verlauf: 500 W Grundlast
    return [s / c if c else fallback for s, c in zip(sums, counts)]


def plan(state, energy_log, prices, only=None, deadline=None, extra=None):
    """Zeitleiste + Empfehlungen. state = evcc /api/state (ggf. mit „result")."""
    s = state.get("result", state) if isinstance(state, dict) else {}
    fc = s.get("forecast") or {}
    solar = _series((fc.get("solar") or {}).get("timeseries") if isinstance(fc.get("solar"), dict) else fc.get("solar"))
    grid = _series(fc.get("grid"))
    feedin = _series(fc.get("feedin"))
    # Feste Preise (eigene Eingabe auf dem Hub) als Rückfall, sonst evcc-Tarif
    p_grid = prices.get("import") if prices.get("import") is not None else _num(s.get("tariffGrid"))
    p_feed = prices.get("export") if prices.get("export") is not None else _num(s.get("tariffFeedIn"))
    dynamic = len({round(v, 3) for v in grid.values()}) > 1

    base = baseline_profile(energy_log)
    now = int(time.time())
    start = now // SLOT * SLOT
    slots = []
    for t in range(start, now + HORIZON, SLOT):
        dt = datetime.fromtimestamp(t)
        b = base[(dt.hour * 60 + dt.minute) // 15]
        sol = solar.get(t, 0.0)
        slots.append({"t": t, "solar": round(sol), "base": round(b), "surplus": round(max(sol - b, 0)),
                      "price": grid.get(t, p_grid), "feedin": feedin.get(t, p_feed)})

    bat = s.get("battery") if isinstance(s.get("battery"), dict) else {}
    grid_now = _num((s.get("grid") or {}).get("power")) if isinstance(s.get("grid"), dict) else _num(s.get("gridPower"))
    bat_now = _num(bat.get("power")) or 0.0
    # Überschuss jetzt = was eingespeist wird + was gerade in den Akku geht
    surplus_now = max(-(grid_now or 0), 0) + max(-bat_now, 0)

    def cost(i, dev):
        """Kosten eines Laufs ab Slot i: Netzstrom × Preis + entgangene Einspeisung für genutzten Überschuss."""
        n = max(1, dev["minutes"] * 60 // SLOT)
        if i + n > len(slots):
            return None
        c, solar_share, need = 0.0, 0.0, 0.0
        for sl in slots[i:i + n]:
            load = dev["watts"] * SLOT / 3600 / 1000          # kWh je Slot
            free = sl["surplus"] * SLOT / 3600 / 1000
            own = min(load, free)
            pg, pf = sl["price"] or 0, sl["feedin"] or 0
            c += (load - own) * pg + own * pf
            solar_share += own
            need += load
        return c, (solar_share / need if need else 0)

    recs = []
    loadpoints = [lp for lp in (s.get("loadpoints") or []) if isinstance(lp, dict)]
    devices = APPLIANCES + ([CAR] if loadpoints else []) + list(extra or [])
    if only:
        devices = [d for d in APPLIANCES + [CAR, VACUUM] if d["id"] == only]
    for dev in devices:
        now_cost = cost(0, dev)
        # Startzeiten bis 24 h voraus (oder bis „fertig bis"), frühestens jetzt; bei Gleichstand der frühere Termin
        best = None
        for i in range(0, min(len(slots), 96)):
            if deadline and slots[i]["t"] + dev["minutes"] * 60 > deadline:
                break
            r = cost(i, dev)
            if r and (best is None or r[0] < best[1][0] - 0.005):
                best = (i, r)
        if not best or not now_cost:
            continue
        i, (c, share) = best
        recs.append({
            "id": dev["id"], "title": dev["title"], "icon": dev["icon"],
            "start": slots[i]["t"], "end": slots[i]["t"] + dev["minutes"] * 60,
            "now": i == 0, "solarShare": round(share, 2),
            "cost": round(c, 2), "costNow": round(now_cost[0], 2), "saving": round(max(now_cost[0] - c, 0), 2),
            "reason": ("Jetzt – genug Sonne" if i == 0 and share > 0.6 else
                       "Jetzt ist am günstigsten" if i == 0 else
                       f"{round(share * 100)} % Sonnenstrom" if share > 0.3 else
                       "Günstigster Strompreis" if dynamic else "Meiste Sonne in den nächsten 24 h"),
        })

    return {
        "generated": now, "slotSeconds": SLOT, "dynamicTariff": dynamic,
        "priceNow": slots[0]["price"] if slots else p_grid, "feedinNow": p_feed,
        "surplusNow": round(surplus_now), "batterySoc": _num(bat.get("soc")),
        "carConfigured": bool(loadpoints),
        "timeline": slots, "recommendations": recs,
    }
