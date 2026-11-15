"""Техника: камера, объектив, телеконвертер и параметры съёмки из EXIF каталога Lightroom.

EXIF читается при синхронизации (AgHarvestedExifMetadata + AgInternedExif*) в photo_exif —
сырыми значениями. Имена объективов, телеконвертер и эквивалент фокусного считаются при
показе: поправил правило — пересинхронизировать не нужно.

Телеконвертер:
- Olympus пишет его в имени объектива: «M.300mm F4.0 + MC-14»;
- Nikon не называет, но пишет фокусное с конвертером: 500 мм f/4 на 700 мм — ×1.4.
"""

from __future__ import annotations

import functools
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime

from . import review, sun

SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_exif (
    image_id   INTEGER PRIMARY KEY,           -- photos.image_id
    camera     TEXT,
    lens       TEXT,                          -- как в каталоге
    focal      REAL,                          -- мм, реальное (с конвертером — уже умноженное)
    fnum       REAL,                          -- диафрагма, f/
    shutter    REAL,                          -- выдержка, с
    iso        INTEGER
);
"""

EXIF_SQL = """
SELECT e.image AS image_id, cm.value AS camera, l.value AS lens, e.focalLength AS focal,
       e.aperture AS av, e.shutterSpeed AS tv, e.isoSpeedRating AS iso
FROM AgHarvestedExifMetadata e
LEFT JOIN AgInternedExifCameraModel cm ON cm.id_local = e.cameraModelRef
LEFT JOIN AgInternedExifLens l ON l.id_local = e.lensRef
"""

# Кроп-фактор по модели камеры (подстрока, без учёта регистра). Нет в списке — эквивалент не считаем.
CROP = [("e-m1", 2.0), ("e-m5", 2.0), ("e-m10", 2.0), ("om-1", 2.0), ("om-5", 2.0), ("dmc-g", 2.0), ("dc-g", 2.0),
        ("gr ii", 1.53), ("gr iii", 1.53), ("d850", 1.0), ("d750", 1.0), ("d810", 1.0), ("z 9", 1.0), ("z 8", 1.0),
        ("z 6", 1.0), ("z 7", 1.0), ("z f", 1.0), ("eos r", 1.0), ("d500", 1.5), ("d7", 1.5), ("z 50", 1.5)]
CAMERA_NAME = {"NIKON D850": "Nikon D850", "NIKON D750": "Nikon D750", "NIKON Z 9": "Nikon Z 9",
               "E-M1MarkII": "Olympus E-M1 II", "E-M1MarkIII": "Olympus E-M1 III", "GR II": "Ricoh GR II",
               "Canon EOS R": "Canon EOS R", "DMC-G3": "Panasonic G3", "CET-LX9": "Huawei (телефон)",
               "HUAWEI Mate 50": "Huawei Mate 50", "SM-N770F": "Samsung Note 10 Lite"}
PHONES = ("huawei", "sm-", "iphone", "pixel", "cet-", "redmi", "xiaomi")
TC_STEPS = (1.4, 1.7, 2.0)
DEFAULT_LATLON = (59.94, 30.31)       # если у кадра и места нет координат — для высоты солнца


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _apex_f(av):
    return round(2 ** (av / 2), 1) if av is not None else None


def _apex_t(tv):
    return 2 ** (-tv) if tv is not None else None


def read_exif(lr: sqlite3.Connection, image_ids: set[int]) -> list[tuple]:
    """EXIF кадров журнала из каталога (lr — копия каталога)."""
    out = []
    for r in lr.execute(EXIF_SQL):
        if r["image_id"] in image_ids:
            iso = r["iso"]
            out.append((r["image_id"], r["camera"], r["lens"], r["focal"], _apex_f(r["av"]), _apex_t(r["tv"]),
                        int(iso) if iso else None))
    return out


def store_exif(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.execute("DELETE FROM photo_exif")
    conn.executemany("INSERT OR REPLACE INTO photo_exif VALUES (?,?,?,?,?,?,?)", rows)


# --- Имена ---------------------------------------------------------------------

@functools.lru_cache(maxsize=256)
def camera_name(raw: str | None) -> str:
    if not raw or "\ufffd" in raw or not any(ch.isalnum() for ch in raw):
        return "Камера без данных"
    if raw in CAMERA_NAME:
        return CAMERA_NAME[raw]
    brand, _, rest = raw.strip().partition(" ")
    return f"{brand.capitalize()} {rest}" if brand in ("NIKON", "RICOH", "OLYMPUS", "CANON", "SONY", "FUJIFILM") and rest else raw.strip()


@functools.lru_cache(maxsize=256)
def is_phone(raw: str | None) -> bool:
    return bool(raw) and any(p in raw.lower() for p in PHONES)


@functools.lru_cache(maxsize=256)
def crop(raw: str | None) -> float | None:
    low = (raw or "").lower()
    return next((k for sub, k in CROP if sub in low), None)


def _num(s: str) -> str:
    return f"{float(s):g}"


@functools.lru_cache(maxsize=4096)
def lens_info(camera: str | None, lens: str | None, focal: float | None) -> tuple[str, float | None, str]:
    """(имя объектива без конвертера, телеконвертер ×k или None, имя с конвертером)."""
    if is_phone(camera) or (not lens and camera and "gr" in camera.lower()):
        base = "встроенный"
    elif not lens or re.match(r"^0(\.0)? ?mm f/0(\.0)?$", lens.strip()):
        base = "Объектив без данных"
    else:
        base = lens.strip()
    tc, tc_name = None, None
    m = re.search(r"\s*\+\s*(MC|EC|TC)-?(\d)(\d)\b.*$", base, re.I)       # Olympus: «+ MC-14»
    if m:
        tc = int(m.group(2)) + int(m.group(3)) / 10
        tc_name = f"{m.group(1).upper()}-{m.group(2)}{m.group(3)}"
        base = base[:m.start()].strip()
    base = re.sub(r"^OLYMPUS\s+", "", base, flags=re.I)
    if (camera or "").upper().startswith("NIKON") and " Z " not in f" {base} ":
        # «AF-S Nikkor 200-500mm f/5.6E ED VR» — тот же объектив, что «200.0-500.0 mm f/5.6»
        m3 = re.search(r"(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))?\s?mm f/(\d+(?:\.\d+)?)", base)
        if m3:
            base = f"{m3.group(1)}{'-' + m3.group(2) if m3.group(2) else ''} mm f/{m3.group(3)}"
    # «200.0-500.0 mm f/5.6» → «200–500 мм f/5.6»
    m2 = re.match(r"^(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))? ?mm f/(\d+(?:\.\d+)?)$", base)
    if m2:
        lo, hi, f = m2.groups()
        base = f"{_num(lo)}{'–' + _num(hi) if hi else ''} мм f/{_num(f)}"
    if tc is None and focal and base not in ("встроенный", "Объектив без данных"):
        # Nikon и др.: фокусное больше паспортного (наибольшее «… мм» в имени) — стоял конвертер
        hi = max([float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s?(?:мм|mm)", base, re.I)] or [0])
        if hi >= 50 and focal > hi * 1.15:
            tc = min(TC_STEPS, key=lambda s: abs(s - focal / hi))
            tc_name = f"{'встроенный ' if ' TC ' in f' {base} ' else ''}TC ×{tc:g}"
    return base, tc, (f"{base} + {tc_name}" if tc else base)


def lens_label(cam: str, base: str, full: str) -> str:
    """Имя объектива в таблицах: встроенный и «без данных» — вместе с камерой."""
    if base == "встроенный":
        return f"{cam} (встроенный)"
    if base == "Объектив без данных":
        return f"{base} · {cam}"
    return full


# --- Сводка --------------------------------------------------------------------

def _light(elev: float, morning: bool) -> str:
    if elev < -6:
        return "night"
    if elev < 0:
        return "dawn" if morning else "dusk"
    if elev < 10:
        return "low_am" if morning else "low_pm"
    if elev < 30:
        return "am" if morning else "pm"
    return "high"


LIGHT = [("night", "ночь"), ("dawn", "утренние сумерки"), ("low_am", "низкое солнце утром"), ("am", "утро"),
         ("high", "высокое солнце"), ("pm", "вечер"), ("low_pm", "низкое солнце вечером"), ("dusk", "вечерние сумерки")]
ISO_BUCKETS = [100, 200, 400, 800, 1600, 3200, 6400, 12800]
SHUTTER_BUCKETS = [(1/4000, "≤1/4000"), (1/2000, "1/2000"), (1/1000, "1/1000"), (1/500, "1/500"),
                   (1/250, "1/250"), (1/125, "1/125"), (1/60, "1/60"), (1/15, "1/15"), (math.inf, "длиннее")]


def _iso_bucket(iso):
    for b in ISO_BUCKETS:
        if iso <= b * 1.2:
            return b
    return ISO_BUCKETS[-1] * 2


def _shutter_bucket(t):
    for lim, name in SHUTTER_BUCKETS:
        if t <= lim * 1.2:
            return name
    return SHUTTER_BUCKETS[-1][1]


def _rows(conn, year: str | None = None):
    """Кадры журнала с EXIF, днём, рейтингом и координатами для солнца."""
    loc_ll = {}
    for d, la, lo in conn.execute("SELECT dl.day_id, l.lat, l.lon FROM day_locations dl JOIN locations l "
                                  "ON l.id = dl.location_id WHERE l.lat IS NOT NULL"):
        loc_ll.setdefault(d, (la, lo))
    q = ("SELECT p.image_id, p.day_id, p.capture_time, p.rating, p.lat, p.lon, d.date, "
         "e.camera, e.lens, e.focal, e.fnum, e.shutter, e.iso FROM photos p JOIN days d ON d.id = p.day_id "
         "JOIN photo_exif e ON e.image_id = p.image_id")
    args = []
    if year:
        q += " WHERE substr(d.date, 1, 4) = ?"
        args.append(year)
    for r in conn.execute(q, args):
        yield r, loc_ll.get(r["day_id"])


def _stat():
    return {"n": 0, "sel": 0, "proc": 0}


def _add(s, rating):
    s["n"] += 1
    s["sel"] += rating >= 2
    s["proc"] += rating >= 5


_cache: dict = {}


def summary(conn, year: str | None = None, camera: str | None = None) -> dict:
    """Сводка для вкладки «Техника». Пересчёт ~2 с, поэтому запоминаем до синхронизации
    или нового решения по виду."""
    ver = tuple(conn.execute("SELECT (SELECT MAX(synced_at) FROM days), (SELECT COUNT(*) FROM photo_exif), "
                             "(SELECT COUNT(*) FROM species_confirm), (SELECT MAX(confirmed_at) FROM species_confirm)"
                             ).fetchone())
    key = (year, camera)
    if _cache.get("ver") != ver:
        _cache.clear()
        _cache["ver"] = ver
    if key not in _cache:
        _cache[key] = _summary(conn, year, camera)
    return _cache[key]


def _summary(conn, year: str | None, camera: str | None) -> dict:
    cams, lenses = defaultdict(_stat), defaultdict(_stat)
    lens_cam = defaultdict(Counter)
    focal, focal_eq = Counter(), Counter()
    iso = defaultdict(lambda: defaultdict(_stat))
    shutter, aperture = defaultdict(_stat), defaultdict(_stat)
    light = defaultdict(lambda: defaultdict(int))
    tc_seen = Counter()
    years = {r[0] for r in conn.execute("SELECT DISTINCT substr(d.date, 1, 4) FROM days d "
                                        "JOIN photos p ON p.day_id = d.id JOIN photo_exif e ON e.image_id = p.image_id")}
    lens_of = {}
    for r, ll in _rows(conn, year):
        cam = camera_name(r["camera"])
        if camera and cam != camera:
            continue
        rating = r["rating"] or 0
        _add(cams[cam], rating)
        base, tc, full = lens_info(r["camera"], r["lens"], r["focal"])
        lens_key = lens_label(cam, base, full)
        _add(lenses[lens_key], rating)
        lens_cam[lens_key][cam] += 1
        lens_of[r["image_id"]] = lens_key
        if tc:
            tc_seen[full] += 1
        if r["focal"] and not is_phone(r["camera"]):
            focal[int(round(r["focal"]))] += 1
            k = crop(r["camera"])
            if k:
                focal_eq[int(round(r["focal"] * k))] += 1
        if r["iso"]:
            _add(iso[cam][_iso_bucket(r["iso"])], rating)
        if r["shutter"]:
            _add(shutter[_shutter_bucket(r["shutter"])], rating)
        if r["fnum"]:
            _add(aperture[f"f/{r['fnum']:g}"], rating)
        if r["capture_time"]:
            la, lo = (r["lat"], r["lon"]) if r["lat"] is not None else (ll or DEFAULT_LATLON)
            t = datetime.fromisoformat(r["capture_time"][:19])
            _, elev = sun.position(la, lo, t)
            noon = 12 + (3 * 15 - lo) / 15          # солнечный полдень, местное время ≈
            light[lens_key][_light(elev, t.hour + t.minute / 60 < noon)] += 1

    # Объективы по видам: подтверждённые кадры
    names = review._names(conn)
    by_species = defaultdict(Counter)
    sp_name = {}
    for r in conn.execute("SELECT image_id, scientific, name_ru FROM species_confirm WHERE status = 'confirmed'"):
        lk = lens_of.get(r["image_id"])
        if not lk:
            continue
        lab = review._label(r["scientific"], names) if r["scientific"] else {}
        key = lab.get("sci") or r["scientific"] or r["name_ru"]
        sp_name[key] = lab.get("ru") or r["name_ru"] or key
        by_species[key][lk] += 1

    def table(d):
        return sorted(({"name": k, **v} for k, v in d.items()), key=lambda x: -x["n"])

    lens_rows = table(lenses)
    for x in lens_rows:
        x["cameras"] = [c for c, _ in lens_cam[x["name"]].most_common()]
        x["light"] = dict(light[x["name"]])
    shutter_order = [n for _, n in SHUTTER_BUCKETS]
    return {
        "years": sorted(years, reverse=True),
        "cameras_all": sorted({camera_name(r[0]) for r in conn.execute("SELECT DISTINCT camera FROM photo_exif")}),
        "total": sum(c["n"] for c in cams.values()),
        "cameras": table(cams),
        "lenses": lens_rows,
        "focal": sorted(focal.items()),
        "focal_eq": sorted(focal_eq.items()),
        "iso": {cam: [{"iso": b, **v} for b, v in sorted(d.items())] for cam, d in iso.items()},
        "shutter": [{"name": n, **shutter[n]} for n in shutter_order if n in shutter],
        "aperture": sorted(({"name": k, **v} for k, v in aperture.items()), key=lambda x: float(x["name"][2:])),
        "light": LIGHT,
        "species": sorted(({"sci": k, "ru": sp_name[k], "n": sum(c.values()),
                            "lenses": [{"name": l, "n": n} for l, n in c.most_common(4)]}
                           for k, c in by_species.items()), key=lambda x: -x["n"]),
        "teleconverter": [{"name": k, "n": n} for k, n in tc_seen.most_common()],
    }


def day_gear(conn) -> dict[int, list[dict]]:
    """Чем снимал в каждый день: [{name: «Nikon D850 · 200–500 мм f/5.6», n}], по убыванию."""
    out = defaultdict(Counter)
    for r in conn.execute("SELECT p.day_id, e.camera, e.lens, e.focal FROM photos p "
                          "JOIN photo_exif e ON e.image_id = p.image_id"):
        cam = camera_name(r["camera"])
        base, tc, full = lens_info(r["camera"], r["lens"], r["focal"])
        out[r["day_id"]][cam if base == "встроенный" else f"{cam} · {full}"] += 1
    return {d: [{"name": k, "n": n} for k, n in c.most_common()] for d, c in out.items()}
