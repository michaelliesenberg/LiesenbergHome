"""Türgong auf allen HomePods: klingelt jemand an der DoorBird, spielt der Hub einen Gong über AirPlay.

Der Gong ist ein selbst erzeugter Zweiklang („Ding-Dong“, keine fremde Tondatei). Wer einen eigenen Ton
möchte, legt ihn als DATA_DIR/doorbird/gong-eigen.mp3 (oder .wav/.m4a) ab – der wird dann genommen.

Ablauf pro Lautsprecher (alle gleichzeitig, music.Music.chime):
Lautstärke merken → Gong-Lautstärke setzen → Gong abspielen → alte Lautstärke zurück →
lief vorher Musik, wird „Play“ geschickt (ob die Musik weiterläuft, entscheidet der HomePod).

Einstellungen (config.json → doorbird.chime, Seite /setup → Geräte → Türklingel):
  enabled        Gong an/aus
  volume         Lautstärke 0–100 tagsüber
  night_volume   Lautstärke nachts (None = wie tagsüber, 0 = nachts kein Gong)
  night_from/to  Nachtzeit, z. B. "22:00" – "07:00"
  repeat         wie oft „Ding-Dong“ (1–3)
  exclude        Lautsprecher-IDs, die NICHT klingeln (z. B. Kinderzimmer)
"""
import math
import os
import struct
import threading
import time
import wave
from datetime import datetime

from hub_config import DATA_DIR

DIR = os.path.join(DATA_DIR, "doorbird")
RATE = 44100
DEFAULTS = {"enabled": True, "volume": 45, "night_volume": 25, "night_from": "22:00", "night_to": "07:00",
            "repeat": 2, "exclude": []}


def settings(raw):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (raw or {}).items() if k in DEFAULTS})
    return s


def _minutes(hm, fallback):
    try:
        h, m = str(hm).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return fallback


def volume_now(s, now=None):
    """Gong-Lautstärke für jetzt (0 = still)."""
    now = now or datetime.now()
    t = now.hour * 60 + now.minute
    a, b = _minutes(s.get("night_from"), 22 * 60), _minutes(s.get("night_to"), 7 * 60)
    night = (a <= t or t < b) if a > b else (a <= t < b)
    v = s.get("night_volume") if night and s.get("night_volume") is not None else s.get("volume")
    try:
        return max(0, min(100, int(float(v))))
    except (TypeError, ValueError):
        return DEFAULTS["volume"]


# ------------------------------------------------------------------ Ton erzeugen
def _bell(freq, length, rate=RATE):
    """Ein Glockenschlag: Grundton + Obertöne, weicher Anschlag, langes Ausklingen."""
    partials = ((1.0, 1.0, 1.1), (2.0, 0.45, 0.8), (3.0, 0.22, 0.55), (4.2, 0.12, 0.35), (5.4, 0.06, 0.25))
    n = int(length * rate)
    out = [0.0] * n
    for ratio, amp, decay in partials:
        w = 2 * math.pi * freq * ratio / rate
        k = 1.0 / (decay * rate)
        for i in range(n):
            out[i] += amp * math.sin(w * i) * math.exp(-i * k)
    attack = int(0.004 * rate)
    for i in range(min(attack, n)):
        out[i] *= i / attack
    return out


def make_gong(path, repeat=2):
    """Schreibt „Ding-Dong“ (repeat-mal) als 16-bit-WAV."""
    ding, dong = _bell(659.25, 1.6), _bell(523.25, 2.4)        # E5 – C5
    lead = int(0.35 * RATE)                                     # Stille vorn: AirPlay schluckt sonst den Anfang
    step = int(0.62 * RATE)                                     # Abstand Ding → Dong
    pair = int(1.9 * RATE)                                      # Abstand zwischen zwei Ding-Dongs
    repeat = max(1, min(3, int(repeat or 1)))
    total = lead + (repeat - 1) * pair + step + len(dong) + int(0.2 * RATE)
    mix = [0.0] * total
    for r in range(repeat):
        for off, tone in ((lead + r * pair, ding), (lead + r * pair + step, dong)):
            for i, v in enumerate(tone):
                if off + i < total:
                    mix[off + i] += v
    peak = max(abs(v) for v in mix) or 1.0
    scale = 0.85 * 32767 / peak
    frames = b"".join(struct.pack("<hh", int(v * scale), int(v * scale)) for v in mix)
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames)
    os.replace(tmp, path)
    return path


def gong_file(repeat=2):
    for ext in ("mp3", "wav", "m4a"):
        p = os.path.join(DIR, f"gong-eigen.{ext}")
        if os.path.isfile(p):
            return p
    repeat = max(1, min(3, int(repeat or 1)))
    p = os.path.join(DIR, f"gong-{repeat}.wav")
    if not os.path.isfile(p):
        os.makedirs(DIR, exist_ok=True)
        make_gong(p, repeat)
    return p


# ------------------------------------------------------------------ Abspielen
class Chime:
    def __init__(self, get_cfg, music):
        self.get_cfg, self.music = get_cfg, music
        self.last = None             # {"ts", "volume", "speakers": {name: "ok" | Fehler}}
        self._busy = threading.Lock()
        if music is not None:        # Ton schon beim Start erzeugen, damit das erste Klingeln nicht wartet
            threading.Thread(target=lambda: gong_file(self.settings()["repeat"]), daemon=True).start()

    def settings(self):
        return settings(self.get_cfg())

    @property
    def available(self):
        return self.music is not None

    def ring(self, test=False, only=None):
        """Gong spielen. test = auch wenn ausgeschaltet; only = nur diese Lautsprecher-IDs."""
        s = self.settings()
        if self.music is None or not (s["enabled"] or test):
            return None
        vol = volume_now(s) if not test else max(volume_now(s), 20)
        if vol <= 0:
            print("[GONG] nachts stumm geschaltet", flush=True)
            return None
        if not self._busy.acquire(blocking=False):      # läuft schon (Dauerklingeln)
            return None
        try:
            t0 = time.time()
            res = self.music.chime(gong_file(s["repeat"]), vol, exclude=set(s["exclude"] or []), only=only)
            self.last = {"ts": int(t0), "volume": vol, "speakers": res}
            print(f"[GONG] {len(res)} Lautsprecher, Lautstärke {vol}: "
                  + ", ".join(f"{k} {v}" for k, v in res.items()), flush=True)
            return self.last
        finally:
            self._busy.release()

    def ring_async(self, test=False):
        threading.Thread(target=self.ring, kwargs={"test": test}, daemon=True).start()
