"""Gesichter an der Türklingel erkennen – nur für die Mitteilung („Jo steht vor der Tür“), NIE zum Türöffnen.

Läuft komplett auf dem Hub (keine Cloud): OpenCV mit zwei kleinen Modellen aus dem OpenCV-Modellzoo
  YuNet  – findet Gesichter im Klingel-Bild
  SFace  – macht aus jedem Gesicht einen Zahlenvektor (128 Werte), der mit den angelernten Personen verglichen wird
Gespeichert werden nur diese Vektoren und ein kleines Vorschaubild je angelerntem Foto – keine fremden Gesichter.

Einschalten (Besitzer, /setup → Geräte → Türklingel): der Hub installiert dann OpenCV in seine Python-Umgebung
und lädt die Modelle (ca. 40 MB, Prüfsumme wird kontrolliert). Danach Personen anlernen: Foto hochladen
oder ein Gesicht aus einem früheren Klingel-Bild zuordnen. 3–5 Bilder je Person (auch mit Mütze/Brille, abends) helfen.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

from hub_config import DATA_DIR

DIR = os.path.join(DATA_DIR, "faces")              # Modelle + Vorschaubilder
DB = os.path.join(DATA_DIR, "faces.json")          # angelernte Personen (liegt neben den anderen .json → kommt in die Sicherung)
MODELS = {
    "detect": ("face_detection_yunet_2023mar.onnx",
               "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
               "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "recog": ("face_recognition_sface_2021dec.onnx",
              "models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
              "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
}
MIRRORS = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/",
           "https://github.com/opencv/opencv_zoo/raw/main/")
PIP_PKGS = ["opencv-python-headless>=4.8,<6", "numpy"]
THRESHOLD = 0.40        # Kosinus-Ähnlichkeit SFace: OpenCV empfiehlt 0.363 – etwas strenger, lieber „unbekannt“ als falscher Name
MIN_SCORE = 0.75        # so sicher muss YuNet sein, dass es ein Gesicht ist
MIN_FACE = 36           # kleinere Gesichter (px) sind zu weit weg für eine sichere Erkennung
MAX_SAMPLES = 12        # Bilder je Person


class Faces:
    def __init__(self, get_cfg, set_cfg, name_of=None):
        self.get_cfg, self.set_cfg = get_cfg, set_cfg
        self.name_of = name_of or (lambda pid: None)   # aktueller Name eines Hub-Benutzers (Umbenennen in der App)
        self.status = "aus"            # aus | installiert … | bereit | Fehler
        self.error = None
        self._det = self._rec = None
        self._cv = self._np = None
        self._lock = threading.Lock()   # OpenCV-Netze sind nicht threadsicher
        self._busy = False
        self.on_ready = None            # z. B. alte Klingel-Bilder durchsuchen
        os.makedirs(DIR, exist_ok=True)
        self.db = self._load()
        if self.enabled:
            threading.Thread(target=self._prepare, daemon=True).start()

    # ---- Einstellungen / Speicher
    @property
    def enabled(self):
        return bool((self.get_cfg() or {}).get("enabled"))

    @property
    def ready(self):
        return self._rec is not None

    def threshold(self):
        try:
            return float((self.get_cfg() or {}).get("threshold") or THRESHOLD)
        except (TypeError, ValueError):
            return THRESHOLD

    def _load(self):
        try:
            with open(DB) as f:
                d = json.load(f)
            if not isinstance(d.get("people"), dict):
                return {"people": {}}
            for k, v in list(d["people"].items()):       # frühes Format {Name: [Bilder]} übernehmen
                if isinstance(v, list):
                    d["people"][_key(None, k)] = {"name": k, "pid": None, "samples": v}
                    del d["people"][k]
            return d
        except (OSError, ValueError, AttributeError):
            return {"people": {}}

    def _save(self):
        tmp = DB + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.db, f)
        os.replace(tmp, DB)

    def _name(self, rec):
        return (rec.get("pid") and self.name_of(rec["pid"])) or rec["name"]

    def people(self):
        """[{key, name, pid, samples: [{id, ts, src}]}] – ohne Vektoren."""
        out = [{"key": k, "name": self._name(v), "pid": v.get("pid"),
                "samples": [{x: s.get(x) for x in ("id", "ts", "src")} for s in v["samples"]]}
               for k, v in self.db["people"].items()]
        return sorted(out, key=lambda x: x["name"].lower())

    def samples_of(self, pid):
        rec = self.db["people"].get(_key(pid))
        return [{x: s.get(x) for x in ("id", "ts", "src")} for s in rec["samples"]] if rec else []

    def owner_of_sample(self, sid):
        for k, v in self.db["people"].items():
            if any(s["id"] == sid for s in v["samples"]):
                return k
        return None

    def thumb_path(self, sid):
        sid = "".join(ch for ch in str(sid) if ch.isalnum())
        p = os.path.join(DIR, f"{sid}.jpg")
        return p if sid and os.path.isfile(p) else None

    # ---- Einschalten: OpenCV + Modelle
    def enable(self, on=True):
        self.set_cfg({"enabled": bool(on)})
        if on and not self.ready and not self._busy:
            self.status, self.error = "startet …", None
            threading.Thread(target=self._prepare, daemon=True).start()
        if not on:
            self.status = "aus"

    def _prepare(self):
        if self._busy:
            return
        self._busy = True
        try:
            self.error = None
            try:
                import cv2  # noqa: F401
            except ImportError:
                import importlib
                self.status = "installiert OpenCV …"
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *PIP_PKGS],
                                   capture_output=True, text=True, timeout=1800)
                if r.returncode != 0:
                    raise RuntimeError("OpenCV ließ sich nicht installieren: " + (r.stderr or r.stdout).strip()[-200:])
                importlib.invalidate_caches()
            for key, (fname, rel, sha) in MODELS.items():
                path = os.path.join(DIR, fname)
                if os.path.isfile(path) and _sha(path) == sha:
                    continue
                self.status = f"lädt Modell {key} …"
                self._download(rel, path, sha)
            self.status = "lädt …"
            self._load_models()
            self.status = "bereit"
            print("[FACES] bereit", flush=True)
            if self.on_ready:
                self.on_ready()
        except Exception as e:
            self.status, self.error = "Fehler", str(e)[:240]
            print(f"[FACES] {e}", flush=True)
        finally:
            self._busy = False

    @staticmethod
    def _download(rel, path, sha):
        last = None
        for base in MIRRORS:
            try:
                tmp = path + ".part"
                with urllib.request.urlopen(base + rel, timeout=60) as r, open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                if _sha(tmp) != sha:
                    os.remove(tmp)
                    raise RuntimeError("Prüfsumme stimmt nicht")
                os.replace(tmp, path)
                return
            except Exception as e:
                last = e
        raise RuntimeError(f"Modell nicht ladbar ({os.path.basename(path)}): {last}")

    def _load_models(self):
        import cv2
        import numpy as np
        self._cv, self._np = cv2, np
        with self._lock:
            self._det = cv2.FaceDetectorYN.create(os.path.join(DIR, MODELS["detect"][0]), "", (320, 320), MIN_SCORE, 0.3, 50)
            self._rec = cv2.FaceRecognizerSF.create(os.path.join(DIR, MODELS["recog"][0]), "")

    # ---- Erkennen
    def _decode(self, jpeg):
        arr = self._np.frombuffer(jpeg, dtype=self._np.uint8)
        img = self._cv.imdecode(arr, self._cv.IMREAD_COLOR)
        if img is None:
            raise ValueError("Bild nicht lesbar")
        return img

    def _detect(self, img):
        """Gesichter (YuNet-Zeilen, in Koordinaten von img). Kleine Bilder werden fürs Suchen vergrößert."""
        h, w = img.shape[:2]
        scale = 2.0 if max(w, h) <= 800 else 1.0
        src = self._cv.resize(img, None, fx=scale, fy=scale) if scale != 1.0 else img
        with self._lock:
            self._det.setInputSize((src.shape[1], src.shape[0]))
            _, found = self._det.detect(src)
        rows = []
        for f in (found if found is not None else []):
            f = f.copy()
            f[:14] /= scale
            if min(f[2], f[3]) >= MIN_FACE:
                rows.append(f)
        rows.sort(key=lambda f: -f[2] * f[3])        # größtes (= nächstes) Gesicht zuerst
        return rows

    def _feature(self, img, row):
        with self._lock:
            crop = self._rec.alignCrop(img, row)
            feat = self._rec.feature(crop)
        v = feat.flatten().astype("float32")
        return v / (self._np.linalg.norm(v) or 1.0)

    def _match(self, feat):
        best, best_s = None, -1.0
        for rec in self.db["people"].values():
            name = self._name(rec)
            for s in rec["samples"]:
                sc = float(self._np.dot(feat, self._np.asarray(s["feat"], dtype="float32")))
                if sc > best_s:
                    best, best_s = name, sc
        return (best, best_s) if best_s >= self.threshold() else (None, best_s)

    def identify(self, jpeg):
        """[{name|None, score, box:[x,y,w,h]}] für alle Gesichter im Bild. Leere Liste, wenn aus/nicht bereit."""
        if not (self.enabled and self.ready and jpeg):
            return []
        t = time.time()
        img = self._decode(jpeg)
        out = []
        for row in self._detect(img):
            name, score = self._match(self._feature(img, row)) if self.db["people"] else (None, 0.0)
            out.append({"name": name, "score": round(score, 3), "box": [int(round(x)) for x in row[:4]],
                        "lm": [round(float(x), 1) for x in row[4:14]], "det": round(float(row[14]), 3)})
        print(f"[FACES] {len(out)} Gesicht(er), erkannt: {[f['name'] for f in out if f['name']]} ({time.time() - t:.2f}s)", flush=True)
        return out

    # ---- Anlernen
    def enroll(self, name, jpeg, src="upload", face=None, pid=None):
        """Gesicht aus jpeg unter name speichern (pid = Hub-Benutzer, der sein eigenes Gesicht anlernt).
        face = Gesicht aus identify – sonst das größte Gesicht im Bild."""
        name = " ".join((name or "").split())[:40]
        if not name:
            raise ValueError("Bitte einen Namen angeben")
        if not self.ready:
            raise RuntimeError("Gesichtserkennung ist noch nicht bereit")
        img = self._decode(jpeg)
        if face is not None:
            row = self._np.asarray(list(face["box"]) + list(face["lm"]) + [face.get("det", 1.0)], dtype="float32")
        else:
            rows = self._detect(img)
            if not rows:
                raise ValueError("Kein Gesicht gefunden (Gesicht zu klein, zu dunkel oder zur Seite gedreht?)")
            row = rows[0]
            if pid and len(rows) > 1 and rows[1][2] * rows[1][3] > 0.5 * row[2] * row[3]:
                raise ValueError("Mehr als ein Gesicht im Bild – bitte allein scannen")
        feat = self._feature(img, row)
        sid = hashlib.sha1(f"{name}{time.time()}".encode()).hexdigest()[:12]
        self._save_thumb(img, row, sid)
        if not pid:              # Name von der Einrichtungsseite: gehört er einem Hub-Benutzer, dort speichern
            pid = next((v["pid"] for v in self.db["people"].values()
                        if v.get("pid") and self._name(v).lower() == name.lower()), None)
        rec = self.db["people"].setdefault(_key(pid, name), {"name": name, "pid": pid, "samples": []})
        rec["name"] = name
        samples = rec["samples"]
        samples.append({"id": sid, "ts": int(time.time()), "src": src, "feat": [round(float(x), 5) for x in feat]})
        for old in samples[:-MAX_SAMPLES]:
            self._rm_thumb(old["id"])
        del samples[:-MAX_SAMPLES]
        self._save()
        return sid

    def _save_thumb(self, img, row, sid):
        x, y, w, h = [int(v) for v in row[:4]]
        pad = int(max(w, h) * 0.25)
        H, W = img.shape[:2]
        crop = img[max(0, y - pad):min(H, y + h + pad), max(0, x - pad):min(W, x + w + pad)]
        if crop.size:
            crop = self._cv.resize(crop, (112, int(112 * crop.shape[0] / max(1, crop.shape[1]))))
            self._cv.imwrite(os.path.join(DIR, f"{sid}.jpg"), crop, [self._cv.IMWRITE_JPEG_QUALITY, 80])

    def crop(self, jpeg, box, size=112):
        """Ausschnitt eines Gesichts als JPEG (für die Einrichtungsseite)."""
        if not self.ready:
            return None
        img = self._decode(jpeg)
        x, y, w, h = [int(v) for v in box[:4]]
        pad = int(max(w, h) * 0.25)
        H, W = img.shape[:2]
        c = img[max(0, y - pad):min(H, y + h + pad), max(0, x - pad):min(W, x + w + pad)]
        if not c.size:
            return None
        c = self._cv.resize(c, (size, int(size * c.shape[0] / max(1, c.shape[1]))))
        ok, buf = self._cv.imencode(".jpg", c, [self._cv.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None

    def _rm_thumb(self, sid):
        p = self.thumb_path(sid)
        if p:
            try:
                os.remove(p)
            except OSError:
                pass

    def remove(self, key, sid=None):
        """Ein Bild (sid) oder die ganze Person (key aus people(), oder _key(pid)) löschen."""
        rec = self.db["people"].get(key)
        if rec is None:
            return False
        keep = [s for s in rec["samples"] if sid and s["id"] != sid]
        for s in rec["samples"]:
            if s not in keep:
                self._rm_thumb(s["id"])
        if keep:
            rec["samples"] = keep
        else:
            del self.db["people"][key]
        self._save()
        return True

    def state(self):
        return {"enabled": self.enabled, "ready": self.ready, "status": self.status if self.enabled else "aus",
                "error": self.error, "people": [{"name": p["name"], "samples": len(p["samples"])} for p in self.people()]}


def _key(pid, name=""):
    return f"p:{pid}" if pid else "n:" + " ".join(name.split()).lower()


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def headline(faces):
    """Titel der Klingel-Push aus den erkannten Gesichtern – None = kein Bekannter erkannt."""
    names = []
    for f in faces or []:
        if f.get("name") and f["name"] not in names:
            names.append(f["name"])
    if not names:
        return None
    others = sum(1 for f in faces if not f.get("name"))
    who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " und " + names[-1]
    if others:
        who += " und jemand anderes" if others == 1 else f" und {others} andere"
    verb = "steht" if len(names) == 1 and not others else "stehen"
    return f"{who} {verb} vor der Tür"
