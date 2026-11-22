"""Alle Saugroboter als eine Liste: Roborock (roborock_vac) + Vorwerk Kobold (kobold_vac).

Der Hub und die App sprechen weiter nur mit „vacuum" (/api/vacuum …); hier wird je Gerät an die richtige
Anbindung weitergereicht. Vorwerk-Geräte erkennt man an der ID „kobold-…".
"""


class Vacuums:
    def __init__(self, roborock, kobold):
        self.roborock, self.kobold = roborock, kobold
        self.away = None          # away_clean.AwayClean – „Wenn alle weg sind, saugen“ für alle Roboter (ab Hub 2.58)

    def _of(self, duid):
        return self.kobold if self.kobold.owns(duid) else self.roborock

    @property
    def configured(self):
        return self.roborock.configured or self.kobold.configured

    @property
    def status(self):
        return {**(self.roborock.status or {}), **(self.kobold.status or {})}

    @property
    def error(self):
        return self.roborock.error or self.kobold.error

    def list(self):
        a = self.roborock.list() if self.roborock.configured else None
        b = self.kobold.list() if self.kobold.configured else None
        parts = [x for x in (a, b) if x]
        if not parts:
            return {"configured": False, "error": None, "autoAway": False, "devices": []}
        devs = sorted([d for x in parts for d in x["devices"]], key=lambda s: s.get("name") or "")
        auto = self.away.cfg["enabled"] if self.away else any(x.get("autoAway") for x in parts)
        return {"configured": True, "error": next((x["error"] for x in parts if x.get("error")), None),
                "autoAway": auto, "devices": devs}

    def command(self, duid, action, value=None):
        return self._of(duid).command(duid, action, value)

    def map(self, duid, fresh=False):
        return self._of(duid).map(duid, fresh=fresh)

    def zone_payload(self, duid, zones, repeat=1):
        return self._of(duid).zone_payload(duid, zones, repeat)

    def tank_emptied(self, duid):
        return self._of(duid).tank_emptied(duid)

    def app_timers(self):
        return (self.roborock.app_timers() if self.roborock.configured else []) + self.kobold.app_timers()

    def set_auto_away(self, on):
        if self.away:
            return self.away.set_enabled(on)
        for v in (self.roborock, self.kobold):
            if v.configured:
                v.set_auto_away(on)

    def on_presence(self, all_away):
        if self.away:
            return self.away.on_presence(all_away)
        for v in (self.roborock, self.kobold):
            if v.configured:
                v.on_presence(all_away)

    def refresh_now(self):
        for v in (self.roborock, self.kobold):
            if v.configured:
                v.refresh_now()
