"""Полевые данные дня: GPX-трек и заметки с телефона (OsmAnd и другие трекеры).

Что делает:
- ищет *.gpx в корне папки дня и в «Трек»;
- фото-заметки берёт из «С телефона» (старое имя «Телефон» тоже понимает).

Папка дня (см. CLAUDE.md): ДДММГГГГ_Место/ — NEF (подпапки импорта Lightroom),
Трек/ — GPX, С телефона/ — фото с телефона, Видео/, Аудио/. Подпапки необязательные.
- трек сохраняет в базу, точки (wpt) — как находки, скрытые по умолчанию;
- координаты снимков считает по времени: местное время кадра → UTC → точка трека.

В файлы снимков ничего не пишется: координаты живут только в журнале.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_GAP_S = 120          # дальше двух минут от точек трека — «вне трека», координаты не выдумываем
NEAREST_S = 60           # в разрыве трека: берём ближайшую точку, если она не дальше минуты
NEAR_FIND_M = 200        # кадры ближе этого к скрытой находке тоже считаются чувствительными
SAFE_TRACK_M = 300       # в безопасной выгрузке трек вырезается вокруг скрытых находок

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id          INTEGER PRIMARY KEY,
    day_id      INTEGER NOT NULL REFERENCES days(id) ON DELETE CASCADE,
    file_path   TEXT NOT NULL UNIQUE,
    file_mtime  REAL,
    started_at  TEXT,                 -- UTC
    ended_at    TEXT,
    points      INTEGER,
    length_m    REAL,
    geom        TEXT NOT NULL         -- JSON [[lat, lon, unix_time], ...]
);

CREATE TABLE IF NOT EXISTS finds (
    id          INTEGER PRIMARY KEY,
    day_id      INTEGER NOT NULL REFERENCES days(id) ON DELETE CASCADE,
    source_key  TEXT UNIQUE,          -- откуда пришла: время|координаты точки GPX
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    time        TEXT,                 -- UTC
    name        TEXT,
    note        TEXT,
    photo       TEXT,                 -- имя файла фото-заметки, если есть
    sensitive   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_tracks_day ON tracks(day_id);
CREATE INDEX IF NOT EXISTS idx_finds_day ON finds(day_id);
"""

NS = {"g": "http://www.topografix.com/GPX/1/1"}

# Подпапки дня съёмки. Первое имя — текущее, остальные — старые, их тоже понимаем.
TRACK_DIRS = ("Трек",)
PHONE_DIRS = ("С телефона", "Телефон")
VIDEO_DIRS = ("Видео",)          # пока не читаются: видео не в Lightroom, в плане
AUDIO_DIRS = ("Аудио",)          # пока не читаются: в плане вместе с BirdNET


def subdirs(folder: Path, names: tuple[str, ...]) -> list[Path]:
    """Существующие подпапки дня с такими именами (без учёта регистра), в порядке names."""
    want = [n.casefold() for n in names]
    found: dict[str, Path] = {}
    try:
        for e in os.scandir(folder):
            if e.is_dir() and e.name.casefold() in want:
                found.setdefault(e.name.casefold(), Path(e.path))
    except OSError:
        return []
    return [found[n] for n in want if n in found]


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(photos)")]
    for col, typ in (("lat", "REAL"), ("lon", "REAL"), ("geo_gap_s", "REAL")):
        if col not in cols:
            conn.execute(f"ALTER TABLE photos ADD COLUMN {col} {typ}")
    if "photo_file" not in [r[1] for r in conn.execute("PRAGMA table_info(finds)")]:
        # Фото заметки, выбранное вручную: путь относительно папки дня
        conn.execute("ALTER TABLE finds ADD COLUMN photo_file TEXT")
    if "clock_offset_s" not in [r[1] for r in conn.execute("PRAGMA table_info(days)")]:
        # На сколько секунд часы камеры спешили (+) или отставали (−) в этот день
        conn.execute("ALTER TABLE days ADD COLUMN clock_offset_s INTEGER NOT NULL DEFAULT 0")


# --- Пути --------------------------------------------------------------------

def to_local_path(path: str, path_map: dict | None = None) -> Path:
    """Путь из каталога Lightroom ('D:/Фото/…') → путь на этой машине.
    В WSL диск D: виден как /mnt/d. Своё соответствие можно задать в config.json: "path_map"."""
    p = path.replace("\\", "/")
    for src, dst in (path_map or {}).items():
        if p.lower().startswith(src.replace("\\", "/").lower()):
            return Path(dst) / p[len(src):].lstrip("/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m and not Path(p).exists():
        return Path(f"/mnt/{m.group(1).lower()}") / m.group(2)
    return Path(p)


def day_folder(conn: sqlite3.Connection, day: sqlite3.Row, path_map: dict | None) -> Path | None:
    """Папка съёмки дня ('…/08112026_Рощинка') по путям его кадров."""
    if not day["source_folder"]:
        return None
    for (fp,) in conn.execute("SELECT DISTINCT folder_path FROM photos WHERE day_id = ?", (day["id"],)):
        parts = re.split(r"[\\/]+", fp.rstrip("\\/"))
        if day["source_folder"] in parts:
            i = parts.index(day["source_folder"])
            return to_local_path("/".join(parts[: i + 1]), path_map)
    return None


def find_gpx(folder: Path) -> list[Path]:
    """GPX в корне папки дня и в «Трек» (с вложенными папками). Если файл с тем же
    именем лежит в обоих местах, берётся из «Трек»."""
    by_name: dict[str, Path] = {}
    def scan(d: Path, deep: bool):
        try:
            for e in os.scandir(d):
                if e.is_file() and e.name.lower().endswith(".gpx"):
                    by_name[e.name.lower()] = Path(e.path)
                elif deep and e.is_dir():
                    scan(Path(e.path), True)
        except OSError:
            pass
    scan(folder, False)
    for d in subdirs(folder, TRACK_DIRS):
        scan(d, True)
    return sorted(by_name.values())


# --- GPX ---------------------------------------------------------------------

def _time(el) -> datetime | None:
    t = el.find("g:time", NS)
    if t is None or not t.text:
        return None
    try:
        dt = datetime.fromisoformat(t.text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _text(el, tag: str) -> str | None:
    x = el.find(f"g:{tag}", NS)
    return x.text.strip() if x is not None and x.text and x.text.strip() else None


def parse_gpx(path: Path) -> dict:
    """{'track': [(unix, lat, lon), ...], 'waypoints': [{...}]}"""
    root = ET.parse(path).getroot()
    track = []
    for p in root.iterfind(".//g:trkpt", NS):
        t = _time(p)
        if t:
            track.append((t.timestamp(), float(p.get("lat")), float(p.get("lon"))))
    track.sort()
    wpts = []
    for w in root.iterfind("g:wpt", NS):
        link = w.find("g:link", NS)
        href = link.get("href") if link is not None else None
        name, desc = _text(w, "name"), _text(w, "desc")
        t = _time(w)
        photo = href if href and re.search(r"\.(jpe?g|png|heic)$", href, re.I) else None
        # У фото-заметок OsmAnd имя = имя файла, описание = «Фото»: это не текст заметки
        if photo and name == photo:
            name = None
        if desc in ("Фото", "Photo", "Аудио", "Audio", "Видео", "Video"):
            desc = None
        wpts.append({"lat": float(w.get("lat")), "lon": float(w.get("lon")),
                     "time": t.isoformat() if t else None,
                     "name": name, "note": desc, "photo": photo})
    return {"track": track, "waypoints": wpts}


def dist_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    return 2 * r * math.asin(math.sqrt(math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2))


def _length(track) -> float:
    return sum(dist_m(a[1], a[2], b[1], b[2]) for a, b in zip(track, track[1:]))


# --- Импорт ------------------------------------------------------------------

def _local_date(unix: float) -> str:
    return datetime.fromtimestamp(unix, timezone(timedelta(hours=UTC_OFFSET_H))).date().isoformat()


def import_day(conn: sqlite3.Connection, day: sqlite3.Row, path_map: dict | None) -> dict:
    """Находит GPX в папке дня, обновляет треки и находки. Возвращает счётчики.

    В одной папке съёмки бывают кадры разных дат (пробный кадр накануне) — тогда это
    несколько дней журнала. Трек и заметки достаются дню с той же датой, что у них."""
    folder = day_folder(conn, day, path_map)
    res = {"tracks": 0, "finds": 0, "folder": str(folder) if folder else None}
    keys: set[str] = set()
    if not folder:
        return res
    siblings = conn.execute("SELECT id, date, photo_count FROM days WHERE source_folder = ?",
                            (day["source_folder"],)).fetchall() or [day]
    by_date = {r["date"]: r["id"] for r in siblings}
    main_id = max(siblings, key=lambda r: r["photo_count"] or 0)["id"]

    def target(unix: float | None) -> int:
        return by_date.get(_local_date(unix), main_id) if unix is not None else main_id

    touched: set[int] = set()
    for path in find_gpx(folder):
        try:
            mtime = path.stat().st_mtime
            row = conn.execute("SELECT id, day_id, file_mtime FROM tracks WHERE file_path = ?", (str(path),)).fetchone()
            if not row:
                row = _moved_track(conn, list(by_date.values()), path)
            data = parse_gpx(path)
        except (OSError, ET.ParseError):
            continue
        tr = data["track"]
        if tr:
            tday = target(tr[0][0])
            if not (row and row["file_mtime"] == mtime):
                vals = (tday, str(path), mtime,
                        datetime.fromtimestamp(tr[0][0], timezone.utc).isoformat(),
                        datetime.fromtimestamp(tr[-1][0], timezone.utc).isoformat(),
                        len(tr), round(_length(tr)),
                        json.dumps([[round(la, 6), round(lo, 6), int(t)] for t, la, lo in tr]))
                if row:
                    conn.execute("""UPDATE tracks SET day_id=?, file_path=?, file_mtime=?, started_at=?, ended_at=?,
                                    points=?, length_m=?, geom=? WHERE id=?""", (*vals, row["id"]))
                else:
                    conn.execute("""INSERT INTO tracks(day_id, file_path, file_mtime, started_at, ended_at, points,
                                    length_m, geom) VALUES (?,?,?,?,?,?,?,?)""", vals)
            elif row["day_id"] != tday:
                conn.execute("UPDATE tracks SET day_id = ? WHERE id = ?", (tday, row["id"]))
                touched.add(row["day_id"])
            touched.add(tday)
            res["tracks"] += 1
        # Одни и те же точки OsmAnd кладёт и в трек, и в «Места» — ключ по времени и месту
        for w in data["waypoints"]:
            key = f'{w["time"]}|{w["lat"]:.5f}|{w["lon"]:.5f}'
            wday = target(datetime.fromisoformat(w["time"]).timestamp() if w["time"] else None)
            # Пустые поля из GPX не затирают то, что ты дописал в журнале
            conn.execute("""INSERT INTO finds(day_id, source_key, lat, lon, time, name, note, photo)
                            VALUES (?,?,?,?,?,?,?,?)
                            ON CONFLICT(source_key) DO UPDATE SET day_id=excluded.day_id,
                              name=COALESCE(finds.name, excluded.name),
                              note=COALESCE(finds.note, excluded.note),
                              photo=COALESCE(excluded.photo, finds.photo)""",
                         (wday, key, w["lat"], w["lon"], w["time"], w["name"], w["note"], w["photo"]))
            keys.add(key)
    res["finds"] = len(keys)
    relink_photo_files(conn, main_id, folder)
    for d in touched:
        geotag_day(conn, d)
    return res


def _moved_track(conn, day_ids: list[int], path: Path):
    """Трек того же дня с тем же именем файла, которого больше нет на старом месте
    (GPX перенесли из корня в «Трек»): обновим его, а не заведём второй."""
    q = ",".join("?" * len(day_ids))
    for r in conn.execute(f"SELECT id, day_id, file_mtime, file_path FROM tracks WHERE day_id IN ({q})", day_ids):
        old = Path(r["file_path"])
        if old.name.lower() == path.name.lower() and not old.exists():
            return {"id": r["id"], "day_id": r["day_id"], "file_mtime": None}
    return None


def import_all(conn: sqlite3.Connection, cfg: dict) -> dict:
    ensure_schema(conn)
    path_map = cfg.get("path_map")
    total = {"days_with_gpx": 0, "tracks": 0, "finds": 0}
    seen: set[str] = set()
    for day in conn.execute("SELECT * FROM days WHERE photo_count > 0").fetchall():
        if day["source_folder"] in seen:
            continue
        seen.add(day["source_folder"] or f'#{day["id"]}')
        r = import_day(conn, day, path_map)
        if r["tracks"] or r["finds"]:
            total["days_with_gpx"] += 1
        total["tracks"] += r["tracks"]
        total["finds"] += r["finds"]
    conn.commit()
    return total


# --- Координаты снимков ------------------------------------------------------

def _load_track(conn, day_id: int) -> list[tuple[int, float, float]]:
    pts = []
    for (g,) in conn.execute("SELECT geom FROM tracks WHERE day_id = ?", (day_id,)):
        pts += [(t, la, lo) for la, lo, t in json.loads(g)]
    pts.sort()
    return pts


def locate(track, t: float) -> tuple[float | None, float | None, float]:
    """Положение в момент t (unix): (lat, lon, разрыв в секундах). lat=None — вне трека."""
    if not track:
        return None, None, math.inf
    times = [p[0] for p in track]
    i = bisect.bisect_left(times, t)
    if i < len(track) and times[i] == t:
        return track[i][1], track[i][2], 0
    prev = track[i - 1] if i > 0 else None
    nxt = track[i] if i < len(track) else None
    if prev and nxt and nxt[0] - prev[0] <= MAX_GAP_S:
        k = (t - prev[0]) / (nxt[0] - prev[0])
        return prev[1] + (nxt[1] - prev[1]) * k, prev[2] + (nxt[2] - prev[2]) * k, nxt[0] - prev[0]
    near = min((p for p in (prev, nxt) if p), key=lambda p: abs(p[0] - t))
    gap = abs(near[0] - t)
    if gap <= NEAREST_S:
        return near[1], near[2], gap
    return None, None, gap


def photo_utc(capture_local: str, utc_offset_h: float, clock_offset_s: int) -> float:
    """Время кадра в unix: местное время камеры минус пояс минус ошибка часов."""
    dt = datetime.fromisoformat(capture_local[:19]) - timedelta(seconds=clock_offset_s or 0)
    return dt.replace(tzinfo=timezone(timedelta(hours=utc_offset_h))).timestamp()


UTC_OFFSET_H = 3   # Петербург, без перехода на летнее время; можно переопределить в config.json


def geotag_day(conn: sqlite3.Connection, day_id: int, utc_offset_h: float | None = None) -> dict:
    track = _load_track(conn, day_id)
    off = conn.execute("SELECT clock_offset_s FROM days WHERE id = ?", (day_id,)).fetchone()
    clock = off["clock_offset_s"] if off else 0
    tz = UTC_OFFSET_H if utc_offset_h is None else utc_offset_h
    ok = out = 0
    rows = conn.execute("SELECT image_id, capture_time FROM photos WHERE day_id = ?", (day_id,)).fetchall()
    for r in rows:
        if not track or not r["capture_time"]:
            lat = lon = gap = None
        else:
            lat, lon, gap = locate(track, photo_utc(r["capture_time"], tz, clock))
            gap = None if gap == math.inf else round(gap)
        conn.execute("UPDATE photos SET lat = ?, lon = ?, geo_gap_s = ? WHERE image_id = ?",
                     (lat, lon, gap, r["image_id"]))
        if lat is not None:
            ok += 1
        elif track:
            out += 1
    return {"matched": ok, "out_of_track": out}


def suggest_offset(conn: sqlite3.Connection, day_id: int, utc_offset_h: float | None = None) -> int | None:
    """Подсказка сдвига часов: если кадры выходят за трек, на сколько их сдвинуть, чтобы влезли все."""
    track = _load_track(conn, day_id)
    if not track:
        return None
    tz = UTC_OFFSET_H if utc_offset_h is None else utc_offset_h
    times = [photo_utc(r[0], tz, 0) for r in conn.execute(
        "SELECT capture_time FROM photos WHERE day_id = ? AND capture_time IS NOT NULL", (day_id,))]
    if not times:
        return None
    lo, hi = track[0][0] - min(times), track[-1][0] - max(times)   # допустимый диапазон сдвига (−)
    if lo > hi:            # кадры шире трека с обеих сторон — одним сдвигом не исправить
        return None
    if lo <= 0 <= hi:
        return 0
    shift = lo if lo > 0 else hi
    return int(-shift)


# --- Отдача наружу -----------------------------------------------------------

def coarse(lat: float, lon: float) -> tuple[float, float]:
    """Огрубление до квадрата ~1 км (на широте Петербурга 0.01° широты ≈ 1.1 км, 0.02° долготы ≈ 1.1 км)."""
    return round(round(lat / 0.01) * 0.01, 2), round(round(lon / 0.02) * 0.02, 2)


def sensitive_points(conn, day_id: int) -> list[tuple[float, float]]:
    return [(r["lat"], r["lon"]) for r in conn.execute(
        "SELECT lat, lon FROM finds WHERE day_id = ? AND sensitive = 1", (day_id,))]


def near_sensitive(lat, lon, pts, radius=NEAR_FIND_M) -> bool:
    return any(dist_m(lat, lon, a, b) <= radius for a, b in pts)


def export_gpx(conn: sqlite3.Connection, day_id: int, safe: bool = True, hidden: str = "coarse") -> str:
    """GPX дня. safe=True: трек вырезан вокруг скрытых находок, а сами они огрублены (hidden='coarse')
    или убраны (hidden='drop')."""
    track = _load_track(conn, day_id)
    finds = [dict(r) for r in conn.execute("SELECT * FROM finds WHERE day_id = ? ORDER BY time", (day_id,))]
    secret = [(f["lat"], f["lon"]) for f in finds if f["sensitive"]]

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="shooting-journal" xmlns="http://www.topografix.com/GPX/1/1">']
    for f in finds:
        lat, lon = f["lat"], f["lon"]
        if safe and f["sensitive"]:
            if hidden == "drop":
                continue
            lat, lon = coarse(lat, lon)
        out.append(f'  <wpt lat="{lat:.6f}" lon="{lon:.6f}">'
                   + (f'<time>{f["time"]}</time>' if f["time"] and not (safe and f["sensitive"]) else "")
                   + f'<name>{esc(f["name"] or "находка")}</name>'
                   + (f'<desc>{esc(f["note"])}</desc>' if f["note"] else "") + "</wpt>")
    if track:
        out.append("  <trk><name>трек</name><trkseg>")
        for t, la, lo in track:
            if safe and secret and near_sensitive(la, lo, secret, SAFE_TRACK_M):
                if not out[-1].endswith("<trkseg>"):
                    out.append("  </trkseg><trkseg>")
                continue
            ts = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append(f'    <trkpt lat="{la:.6f}" lon="{lo:.6f}"><time>{ts}</time></trkpt>')
        out.append("  </trkseg></trk>")
    out.append("</gpx>")
    return "\n".join(x for x in out if x != "  <trkseg></trkseg>")


# --- Фото-заметки с телефона -------------------------------------------------

_TS_RE = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})[-_ T]?(\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".heic")


def exif_time(path: Path) -> datetime | None:
    """Время съёмки из EXIF JPEG (DateTimeOriginal, иначе DateTime) — без сторонних библиотек."""
    import struct
    try:
        with open(path, "rb") as f:
            data = f.read(256 * 1024)
    except OSError:
        return None
    if data[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker, size = data[pos + 1], struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if marker == 0xE1 and data[pos + 4:pos + 10] == b"Exif\x00\x00":
            t = data[pos + 10:pos + 2 + size]
            end = "<" if t[:2] == b"II" else ">"

            def ifd(off):
                out = {}
                if off + 2 > len(t):
                    return out
                n = struct.unpack(end + "H", t[off:off + 2])[0]
                for k in range(n):
                    e = off + 2 + 12 * k
                    if e + 12 > len(t):
                        break
                    tag, typ, cnt = struct.unpack(end + "HHI", t[e:e + 8])
                    val = t[e + 8:e + 12]
                    out[tag] = (typ, cnt, val)
                return out

            def ascii_(entry):
                typ, cnt, val = entry
                off = struct.unpack(end + "I", val)[0]
                return t[off:off + cnt].rstrip(b"\x00").decode("ascii", "ignore")

            try:
                i0 = ifd(struct.unpack(end + "I", t[4:8])[0])
                txt = None
                if 0x8769 in i0:
                    ex = ifd(struct.unpack(end + "I", i0[0x8769][2])[0])
                    if 0x9003 in ex:
                        txt = ascii_(ex[0x9003])
                if not txt and 0x0132 in i0:
                    txt = ascii_(i0[0x0132])
                return datetime.strptime(txt.strip(), "%Y:%m:%d %H:%M:%S") if txt else None
            except (struct.error, ValueError, AttributeError):
                return None
        if marker == 0xDA:
            break
        pos += 2 + size
    return None


def phone_photos(folder: Path) -> list[Path]:
    """Снимки с телефона: «С телефона» и старое «Телефон», с вложенными папками."""
    files: list[Path] = []
    for d in subdirs(folder, PHONE_DIRS):
        files += [p for p in d.rglob("*") if p.suffix.lower() in PHOTO_EXT and p.is_file()]
    return sorted(files)


def relink_photo_files(conn: sqlite3.Connection, day_id: int, folder: Path) -> int:
    """Ручные привязки фото к заметкам после переименования «Телефон» → «С телефона»:
    путь, которого больше нет, переписывается на ту же картинку в другой папке телефона."""
    if not folder or not folder.is_dir():
        return 0
    dirs = subdirs(folder, PHONE_DIRS)
    fixed = 0
    for fid, rel in conn.execute("SELECT f.id, f.photo_file FROM finds f WHERE f.photo_file IS NOT NULL "
                                 "AND f.day_id IN (SELECT id FROM days WHERE source_folder = "
                                 "(SELECT source_folder FROM days WHERE id = ?))", (day_id,)).fetchall():
        if (folder / rel).is_file():
            continue
        head, _, tail = rel.replace("\\", "/").partition("/")
        if head.casefold() not in [n.casefold() for n in PHONE_DIRS] or not tail:
            continue
        for d in dirs:
            if (d / tail).is_file():
                conn.execute("UPDATE finds SET photo_file = ? WHERE id = ?",
                             ((d / tail).relative_to(folder).as_posix(), fid))
                fixed += 1
                break
    if fixed:
        conn.commit()
    return fixed


def photo_time(p: Path) -> datetime | None:
    """Местное время снимка: из EXIF, иначе из имени файла (IMG_20261108_093853.jpg)."""
    t = exif_time(p)
    if t:
        return t
    m = _TS_RE.search(p.stem)
    if m:
        try:
            return datetime(*map(int, m.groups()))
        except ValueError:
            return None
    return None


def find_note_photo(folder: Path, name: str | None, time_utc: str | None, tol_s: int = 90,
                    files: list[Path] | None = None) -> Path | None:
    """Файл фото-заметки: 1) по имени из GPX (0zNEUCKC--.1.jpg); 2) по времени съёмки (EXIF или имя файла)."""
    files = phone_photos(folder) if files is None else files
    if name:
        for p in files:
            if p.name.lower() == name.lower():
                return p
    if time_utc:
        t = datetime.fromisoformat(time_utc).timestamp()
        tz = timezone(timedelta(hours=UTC_OFFSET_H))
        best, best_d = None, tol_s + 1
        for p in files:
            lt = photo_time(p)
            if lt is None:
                continue
            d = abs(lt.replace(tzinfo=tz).timestamp() - t)
            if d < best_d:
                best, best_d = p, d
        return best
    return None


def resolve_photo(folder: Path, find: dict, files: list[Path] | None = None) -> Path | None:
    """Фото заметки: выбранное вручную (photo_file), иначе найденное автоматически."""
    if find.get("photo_file"):
        p = (folder / find["photo_file"]).resolve()
        if p.is_file() and folder.resolve() in p.parents:
            return p
    return find_note_photo(folder, find.get("photo"), find.get("time"), files=files)
