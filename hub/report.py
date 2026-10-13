"""Monatsbericht: Sonne, Verbrauch, Eigenversorgung und Geld – mit Vergleich zum Vormonat und Vorjahresmonat.

Nur Auswertung der Minuten-Aufzeichnung (energy_log.py); es wird nichts geschaltet.
"""
from datetime import date, timedelta


def _month_start(d):
    return d.replace(day=1)


def _prev_month(d):
    return (_month_start(d) - timedelta(days=1)).replace(day=1)


def _brief(log, d, prices):
    s = log.summary("month", d, prices)
    t = s["totals"]
    days = [b for b in s["buckets"] if b.get("pv") or b.get("home")]
    if not days:
        return None
    home = t.get("home") or 0
    best = max(days, key=lambda b: b.get("pv") or 0)
    return {
        "month": d.strftime("%Y-%m"), "days": len(days),
        "pv": t["pv"], "home": t["home"], "import": t["import"], "export": t["export"], "selfUse": t["selfUse"],
        "selfShare": round(t["selfUse"] / home, 3) if home else None,              # Autarkie
        "pvPerDay": round(t["pv"] / len(days), 2), "homePerDay": round(home / len(days), 2),
        "bestDay": {"date": best["date"], "pv": round(best.get("pv") or 0, 1)},
        "costs": s.get("costs"),
        "daily": [{"date": b["date"], "pv": round(b.get("pv") or 0, 2), "home": round(b.get("home") or 0, 2)} for b in s["buckets"]],
    }


def month_report(log, prices, month=None):
    """month „JJJJ-MM“ (leer = aktueller Monat)"""
    d = date.fromisoformat(month + "-01") if month else date.today().replace(day=1)
    cur = _brief(log, d, prices)
    prev = _brief(log, _prev_month(d), prices)
    try:
        ly = _brief(log, d.replace(year=d.year - 1), prices)
    except ValueError:
        ly = None
    for x in (prev, ly):
        if x:
            x.pop("daily", None)
    return {"month": d.strftime("%Y-%m"), "current": cur, "previous": prev, "lastYear": ly,
            "complete": d < date.today().replace(day=1), "highlights": _highlights(cur, prev)}


def _highlights(cur, prev):
    """Kurze Sätze für Karte und Mitteilung"""
    if not cur:
        return []
    out = []
    if cur.get("selfShare") is not None:
        out.append(f"{round(cur['selfShare'] * 100)} % selbst versorgt")
    if prev and prev.get("pvPerDay"):
        diff = (cur["pvPerDay"] - prev["pvPerDay"]) / prev["pvPerDay"]
        if abs(diff) >= 0.05:
            out.append(f"{abs(round(diff * 100))} % {'mehr' if diff > 0 else 'weniger'} Sonne pro Tag als im Vormonat")
    if prev and prev.get("homePerDay"):
        diff = (cur["homePerDay"] - prev["homePerDay"]) / prev["homePerDay"]
        if abs(diff) >= 0.05:
            out.append(f"Verbrauch {abs(round(diff * 100))} % {'höher' if diff > 0 else 'niedriger'} pro Tag")
    bd = cur.get("bestDay")
    if bd and bd["pv"] > 0:
        d = date.fromisoformat(bd["date"])
        out.append(f"Bester Sonnentag: {d.day}.{d.month}. mit {bd['pv']:.0f} kWh".replace(".0 kWh", " kWh"))
    c = cur.get("costs")
    if c:
        out.append(f"{c['saved']:.2f} € gespart".replace(".", ","))
    return out


def year_report(log, prices, year=None):
    """Jahresrückblick: je Monat Sonne, Verbrauch, Eigenversorgung, Ersparnis – plus Bestwerte"""
    year = int(year or date.today().year)
    months = []
    for m in range(1, 13):
        d = date(year, m, 1)
        if d > date.today():
            break
        b = _brief(log, d, prices)
        if b:
            b.pop("daily", None)
            months.append(b)
    if not months:
        return {"year": year, "months": [], "totals": None, "highlights": []}
    keys = ("pv", "home", "import", "export", "selfUse")
    tot = {k: round(sum(m[k] for m in months), 1) for k in keys}
    tot["days"] = sum(m["days"] for m in months)
    tot["selfShare"] = round(tot["selfUse"] / tot["home"], 3) if tot["home"] else None
    costs = [m["costs"] for m in months if m.get("costs")]
    if costs:
        tot["saved"] = round(sum(c["saved"] for c in costs), 2)
        tot["exportEuro"] = round(sum(c["export"] for c in costs), 2)
        tot["balance"] = round(sum(c["balance"] for c in costs), 2)
    best_m = max(months, key=lambda m: m["pv"])
    best_d = max((m["bestDay"] for m in months if m.get("bestDay")), key=lambda b: b["pv"], default=None)
    names = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
    hl = [f"{tot['pv']:.0f} kWh Sonnenstrom an {tot['days']} Tagen"]
    if tot.get("selfShare") is not None:
        hl.append(f"{round(tot['selfShare'] * 100)} % selbst versorgt")
    if tot.get("saved") is not None:
        hl.append(f"{tot['saved']:.2f} € gespart, {tot['exportEuro']:.2f} € Einspeisung".replace(".", ","))
    hl.append(f"Sonnigster Monat: {names[int(best_m['month'][5:]) - 1]} ({best_m['pv']:.0f} kWh)")
    if best_d:
        bd = date.fromisoformat(best_d["date"])
        hl.append(f"Rekordtag: {bd.day}. {names[bd.month - 1]} mit {best_d['pv']:.0f} kWh")
    # grob: 0,4 kg CO₂ je kWh Netzstrom, die nicht bezogen werden musste
    hl.append(f"≈ {tot['selfUse'] * 0.4:.0f} kg CO₂ vermieden")
    return {"year": year, "months": months, "totals": tot, "bestDay": best_d, "highlights": hl,
            "complete": year < date.today().year}
