"""Локальный сервер журнала. Запуск: ./run.sh, затем http://localhost:8765"""

from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .locations import KINDS, merge_locations, set_day_locations
from .lrcat import Photo
from .sync import sync_catalog
from . import field, inat, plan, review, weather

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
RULES_PATH = ROOT / "locations.txt"
STATIC = ROOT / "static"

app = FastAPI(title="Журнал съёмок")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise HTTPException(500, "Нет config.json — скопируй config.example.json и укажи путь к каталогу")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


@app.get("/api/info")
def api_info():
    with db.connect() as conn:
        return {"demo": db.is_demo(conn)}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# --- Синхронизация -----------------------------------------------------------

@app.post("/api/sync")
def api_sync():
    with db.connect() as conn:
        if db.is_demo(conn):
            raise HTTPException(400, "Это демо-база: синхронизация с каталогом отключена")
        cfg = load_config()
        try:
            res = sync_catalog(conn, cfg, RULES_PATH)
            if cfg.get("utc_offset_h") is not None:
                field.UTC_OFFSET_H = cfg["utc_offset_h"]
            res["field"] = field.import_all(conn, cfg)
            return res
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            # Полный текст ошибки — в терминал сервера, коротко — на страницу
            traceback.print_exc()
            raise HTTPException(500, f"{type(e).__name__}: {e}. Подробности в терминале сервера.")


# --- Дни ---------------------------------------------------------------------

def _spots(conn) -> tuple[dict[int, dict], dict[int, dict]]:
    """Все точки и точки по умолчанию по местам."""
    spots = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM spots")}
    defaults = {s["location_id"]: s for s in spots.values() if s["is_default"]}
    return spots, defaults


@app.get("/api/days")
def api_days():
    with db.connect() as conn:
        days = [dict(r) for r in conn.execute("SELECT * FROM days ORDER BY date DESC, source_name")]
        locs: dict[int, list] = {}
        for r in conn.execute("""
            SELECT dl.day_id, l.id, l.name, l.lat, l.lon, l.kind
            FROM day_locations dl JOIN locations l ON l.id = dl.location_id
            ORDER BY l.name
        """):
            locs.setdefault(r["day_id"], []).append(
                {"id": r["id"], "name": r["name"], "lat": r["lat"], "lon": r["lon"], "kind": r["kind"]})
        spots, defaults = _spots(conn)
        wx = weather.summaries(conn)
        tracks = {r[0]: r[1] for r in conn.execute("SELECT day_id, SUM(length_m) FROM tracks GROUP BY day_id")}
        nfinds = {r[0]: r[1] for r in conn.execute("SELECT day_id, COUNT(*) FROM finds GROUP BY day_id")}
        dsp = review.day_species(conn)
    for d in days:
        d["species"] = dsp.get(d["id"], [])
        d["track_m"] = tracks.get(d["id"])
        d["finds"] = nfinds.get(d["id"], 0)
        d["weather"] = wx.get(d["id"])
        d["locations"] = locs.get(d["id"], [])
        # Точка дня: указанная явно, иначе точка по умолчанию (если место одно)
        spot, by_default = spots.get(d["spot_id"]), False
        if spot is None and len(d["locations"]) == 1:
            spot, by_default = defaults.get(d["locations"][0]["id"]), True
        d["spot"] = ({"id": spot["id"], "name": spot["name"], "lat": spot["lat"], "lon": spot["lon"],
                      "facing": spot["facing"], "by_default": by_default} if spot else None)
    return days


class DayUpdate(BaseModel):
    notes: str | None = None
    location_ids: list[int] | None = None   # задать места вручную ([] — без места)
    spot_id: int | None = None              # точка дня; null — вернуть «по умолчанию»


@app.put("/api/days/{day_id}")
def api_update_day(day_id: int, body: DayUpdate):
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM days WHERE id = ?", (day_id,)).fetchone():
            raise HTTPException(404, "День не найден")
        if body.notes is not None:
            conn.execute("UPDATE days SET notes = ? WHERE id = ?", (body.notes, day_id))
        if body.location_ids is not None:
            set_day_locations(conn, day_id, body.location_ids)
            conn.execute("UPDATE days SET location_manual = 1 WHERE id = ?", (day_id,))
        if "spot_id" in body.model_fields_set:
            if body.spot_id is not None and not conn.execute(
                    """SELECT 1 FROM spots s JOIN day_locations dl ON dl.location_id = s.location_id
                       WHERE s.id = ? AND dl.day_id = ?""", (body.spot_id, day_id)).fetchone():
                raise HTTPException(400, "Эта точка не относится к месту этого дня")
            conn.execute("UPDATE days SET spot_id = ? WHERE id = ?", (body.spot_id, day_id))
        conn.commit()
    return {"ok": True}


# --- Поле: трек, снимки, находки ---------------------------------------------

def _cfg_soft() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}


def _day_or_404(conn, day_id: int):
    day = conn.execute("SELECT * FROM days WHERE id = ?", (day_id,)).fetchone()
    if not day:
        raise HTTPException(404, "День не найден")
    return day


@app.get("/api/days/{day_id}/field")
def api_day_field(day_id: int):
    with db.connect() as conn:
        day = _day_or_404(conn, day_id)
        trk = conn.execute("""SELECT MIN(started_at) s, MAX(ended_at) e, SUM(length_m) m, SUM(points) n
                              FROM tracks WHERE day_id = ?""", (day_id,)).fetchone()
        files = [r[0] for r in conn.execute("SELECT file_path FROM tracks WHERE day_id = ?", (day_id,))]
        track = [[la, lo] for _, la, lo in field._load_track(conn, day_id)]
        finds = [dict(r) for r in conn.execute("SELECT * FROM finds WHERE day_id = ? ORDER BY time", (day_id,))]
        secret = [(f["lat"], f["lon"]) for f in finds if f["sensitive"]]
        photos = []
        for r in conn.execute("""SELECT image_id, file_name, capture_time, pick, rating, lat, lon, geo_gap_s
                                 FROM photos WHERE day_id = ? ORDER BY capture_time""", (day_id,)):
            p = dict(r)
            p["selected"] = (p["rating"] or 0) >= Photo.SELECTED_RATING
            p["processed"] = (p["rating"] or 0) >= Photo.PROCESSED_RATING
            p["sensitive"] = p["lat"] is not None and field.near_sensitive(p["lat"], p["lon"], secret)
            photos.append(p)
        has = bool(track)
        return {
            "day": {"id": day["id"], "date": day["date"], "name": day["source_name"],
                    "folder": day["source_folder"], "clock_offset_s": day["clock_offset_s"]},
            "track": track, "files": files,
            "started_at": trk["s"], "ended_at": trk["e"], "length_m": trk["m"], "points": trk["n"],
            "photos": photos, "finds": finds,
            "matched": sum(1 for p in photos if p["lat"] is not None),
            "out_of_track": sum(1 for p in photos if p["lat"] is None) if has else 0,
            "suggest_offset_s": field.suggest_offset(conn, day_id) if has else None,
            "utc_offset_h": field.UTC_OFFSET_H,
        }


class ClockIn(BaseModel):
    clock_offset_s: int


@app.put("/api/days/{day_id}/clock")
def api_day_clock(day_id: int, body: ClockIn):
    """Сдвиг часов камеры в этот день: + спешили, − отставали. Пересчитывает координаты кадров."""
    with db.connect() as conn:
        _day_or_404(conn, day_id)
        conn.execute("UPDATE days SET clock_offset_s = ? WHERE id = ?", (body.clock_offset_s, day_id))
        res = field.geotag_day(conn, day_id)
        conn.commit()
    return res


@app.post("/api/days/{day_id}/field/rescan")
def api_day_rescan(day_id: int):
    """Перечитать GPX из папки дня (без полной синхронизации с каталогом)."""
    with db.connect() as conn:
        day = _day_or_404(conn, day_id)
        res = field.import_day(conn, day, _cfg_soft().get("path_map"))
        conn.commit()
    return res


@app.get("/api/days/{day_id}/field.gpx")
def api_day_gpx(day_id: int, safe: bool = True, hidden: str = "coarse"):
    """Выгрузка дня в GPX. По умолчанию безопасная: скрытые находки огрублены, трек вокруг них вырезан."""
    with db.connect() as conn:
        day = _day_or_404(conn, day_id)
        text = field.export_gpx(conn, day_id, safe=safe, hidden="drop" if hidden == "drop" else "coarse")
    name = f'{day["date"]}_{day["source_name"] or "день"}{"" if safe else "_точно"}.gpx'
    return Response(text, media_type="application/gpx+xml",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


class FindIn(BaseModel):
    name: str | None = None
    note: str | None = None
    sensitive: bool | None = None
    photo_file: str | None = None           # путь к фото в папке дня; "" — отвязать


@app.put("/api/finds/{find_id}")
def api_update_find(find_id: int, body: FindIn):
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM finds WHERE id = ?", (find_id,)).fetchone():
            raise HTTPException(404, "Находка не найдена")
        for k in ("name", "note"):
            if k in body.model_fields_set:
                conn.execute(f"UPDATE finds SET {k} = ? WHERE id = ?",
                             ((getattr(body, k) or "").strip() or None, find_id))
        if "photo_file" in body.model_fields_set:
            conn.execute("UPDATE finds SET photo_file = ? WHERE id = ?", (body.photo_file or None, find_id))
        if body.sensitive is not None:
            conn.execute("UPDATE finds SET sensitive = ? WHERE id = ?", (int(body.sensitive), find_id))
        conn.commit()
    return {"ok": True}


def _find_ctx(conn, find_id: int):
    f = conn.execute("SELECT * FROM finds WHERE id = ?", (find_id,)).fetchone()
    if not f:
        raise HTTPException(404, "Заметка не найдена")
    day = conn.execute("SELECT * FROM days WHERE id = ?", (f["day_id"],)).fetchone()
    folder = field.day_folder(conn, day, _cfg_soft().get("path_map"))
    if f["photo_file"] and folder and field.relink_photo_files(conn, day["id"], folder):
        f = conn.execute("SELECT * FROM finds WHERE id = ?", (find_id,)).fetchone()
    return dict(f), folder


@app.get("/api/finds/{find_id}/photo")
def api_find_photo(find_id: int):
    """Фото-заметка с телефона из папки дня: выбранная вручную или найденная по имени/времени."""
    with db.connect() as conn:
        f, folder = _find_ctx(conn, find_id)
    found = field.resolve_photo(folder, f) if folder and folder.is_dir() else None
    if found:
        return FileResponse(found)
    raise HTTPException(404, "Файл фото-заметки не найден в папке дня")


@app.get("/api/days/{day_id}/phone-photos")
def api_phone_photos(day_id: int):
    """Снимки с телефона в папке дня — чтобы привязать к заметке вручную."""
    with db.connect() as conn:
        day = _day_or_404(conn, day_id)
        folder = field.day_folder(conn, day, _cfg_soft().get("path_map"))
        if folder:
            field.relink_photo_files(conn, day_id, folder)
        finds = [dict(r) for r in conn.execute("SELECT * FROM finds WHERE day_id = ?", (day_id,))]
    if not folder or not folder.is_dir():
        return {"folder": str(folder) if folder else None, "photos": []}
    files = field.phone_photos(folder)
    used = {}
    for f in finds:
        p = field.resolve_photo(folder, f, files)
        if p:
            used[str(p.resolve())] = f["id"]
    out = []
    for p in files:
        t = field.photo_time(p)
        out.append({"path": p.relative_to(folder).as_posix(), "time": t.isoformat() if t else None,
                    "find_id": used.get(str(p.resolve()))})
    return {"folder": str(folder), "photos": out}


@app.get("/api/days/{day_id}/phone-photo")
def api_phone_photo(day_id: int, path: str):
    with db.connect() as conn:
        day = _day_or_404(conn, day_id)
        folder = field.day_folder(conn, day, _cfg_soft().get("path_map"))
    if folder:
        p = (folder / path).resolve()
        if p.is_file() and folder.resolve() in p.parents and p.suffix.lower() in field.PHOTO_EXT:
            return FileResponse(p)
    raise HTTPException(404, "Нет такого файла")


# --- Виды: разбор предсказаний ----------------------------------------------

@app.get("/api/species/meta")
def api_species_meta():
    with db.connect() as conn:
        return review.meta(conn)


@app.get("/api/species/items")
def api_species_items(day: str | None = None, loc: int | None = None, state: str = "todo",
                      conf: str | None = None, kind: str | None = None):
    with db.connect() as conn:
        res = review.items(conn, day=day, loc=loc, state=state, conf=conf, kind=kind)
    cfg = _cfg_soft()
    if cfg.get("catalog_path") and res["series"]:
        # нарезать кропы первых карточек заранее, пока смотришь начало списка
        review.warm(db.connect, cfg, [s["rep"] for s in res["series"][:240]])
    return res


@app.get("/api/species/search")
def api_species_search(q: str):
    """Регион, затем всё древо TreeOfLife (латынь, английские названия) — без сети."""
    with db.connect() as conn:
        return review.search(conn, q)


@app.get("/api/species/search-group")
def api_species_search_group(q: str):
    """Род или семейство для «неопределимо» — из списка видов региона, без сети."""
    with db.connect() as conn:
        return review.search_groups(conn, q)


@app.get("/api/species/search-world")
def api_species_search_world(q: str):
    """iNaturalist по всему миру, с русскими названиями (джейран, сайгак…)."""
    try:
        with db.connect() as conn:
            return review.search_inat(conn, q)
    except Exception as e:
        raise HTTPException(502, f"iNaturalist недоступен: {e}")


class SpeciesDecision(BaseModel):
    ids: list[int]
    action: str                         # confirm | unid | reject | none | clear
    sci: str | None = None
    ru: str | None = None
    inat_id: int | None = None
    source: str | None = None           # top1..top3 | search | series


@app.post("/api/species/decide")
def api_species_decide(body: SpeciesDecision):
    if not body.ids:
        raise HTTPException(400, "Нет кадров")
    with db.connect() as conn:
        before = review.previous(conn, body.ids)
        try:
            n = review.decide(conn, body.ids, body.action, body.sci, body.ru, body.inat_id, body.source)
        except ValueError as e:
            raise HTTPException(400, str(e))
    return {"ok": True, "n": n, "undo": {"ids": body.ids, "rows": before}}


class SpeciesBatchItem(BaseModel):
    ids: list[int]
    sci: str | None = None
    ru: str | None = None
    inat_id: int | None = None


class SpeciesBatch(BaseModel):
    action: str                         # confirm | unid | reject | none
    items: list[SpeciesBatchItem]
    source: str | None = "bulk"


@app.post("/api/species/decide-batch")
def api_species_decide_batch(body: SpeciesBatch):
    """Массовое решение (серии с уверенным топ-1, «без вида» на день) — одна отмена на всё."""
    ids = [i for it in body.items for i in it.ids]
    if not ids:
        raise HTTPException(400, "Нет кадров")
    with db.connect() as conn:
        before = review.previous(conn, ids)
        try:
            for it in body.items:
                review.decide(conn, it.ids, body.action, it.sci, it.ru, it.inat_id, body.source)
        except ValueError as e:
            raise HTTPException(400, str(e))
    return {"ok": True, "n": len(ids), "series": len(body.items), "undo": {"ids": ids, "rows": before}}


class SpeciesUndo(BaseModel):
    ids: list[int]
    rows: list[dict]


@app.post("/api/species/undo")
def api_species_undo(body: SpeciesUndo):
    with db.connect() as conn:
        review.restore(conn, body.ids, body.rows)
    return {"ok": True}


def _species_cfg() -> dict:
    cfg = _cfg_soft()
    if not cfg.get("catalog_path"):
        raise HTTPException(404, "Нет config.json — превью Lightroom недоступны")
    return cfg


@app.get("/api/species/crop/{image_id}")
def api_species_crop(image_id: int):
    cfg = _species_cfg()
    with db.connect() as conn:
        try:
            p = review.crop_jpeg(conn, cfg, image_id)
        except (ImportError, FileNotFoundError) as e:
            raise HTTPException(404, f"Кроп недоступен: {e}")
    if not p:
        raise HTTPException(404, "Нет превью")
    return FileResponse(p, headers={"Cache-Control": "max-age=86400"})


@app.get("/api/species/frame/{image_id}")
def api_species_frame(image_id: int):
    try:
        data = review.big_jpeg(_species_cfg(), image_id)
    except (ImportError, FileNotFoundError) as e:
        raise HTTPException(404, f"Превью недоступно: {e}")
    if not data:
        raise HTTPException(404, "Нет превью")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


# --- Локации -----------------------------------------------------------------

@app.get("/api/locations")
def api_locations():
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT l.*, COUNT(d.id) AS day_count, MAX(d.date) AS last_date,
                   (SELECT COUNT(*) FROM location_aliases a WHERE a.location_id = l.id) AS alias_count,
                   (SELECT COUNT(*) FROM spots s WHERE s.location_id = l.id) AS spot_count
            FROM locations l
            LEFT JOIN day_locations dl ON dl.location_id = l.id
            LEFT JOIN days d ON d.id = dl.day_id
            GROUP BY l.id ORDER BY l.name
        """).fetchall()
    return [dict(r) for r in rows]


class LocationIn(BaseModel):
    name: str | None = None
    coords: str | None = None   # "59.93, 30.31" — как копируется из Яндекс.Карт
    note: str | None = None
    kind: str | None = None     # place | trip | other


def parse_coords(text: str) -> tuple[float | None, float | None]:
    text = text.strip()
    if not text:
        return None, None
    parts = [p for p in text.replace(";", ",").replace(" ", ",").split(",") if p]
    if len(parts) != 2:
        raise HTTPException(400, "Координаты в формате «59.93, 30.31»")
    lat, lon = float(parts[0]), float(parts[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "Координаты вне допустимого диапазона")
    return lat, lon


@app.post("/api/locations")
def api_create_location(body: LocationIn):
    if not body.name:
        raise HTTPException(400, "Нужно название")
    lat, lon = parse_coords(body.coords or "")
    with db.connect() as conn:
        try:
            new_id = conn.execute("INSERT INTO locations(name, lat, lon, note) VALUES (?, ?, ?, ?)",
                                  (body.name.strip(), lat, lon, body.note)).lastrowid
        except Exception:
            raise HTTPException(400, "Такая локация уже есть")
        conn.commit()
    return {"id": new_id}


@app.put("/api/locations/{loc_id}")
def api_update_location(loc_id: int, body: LocationIn):
    with db.connect() as conn:
        if body.coords is not None:
            lat, lon = parse_coords(body.coords)
            conn.execute("UPDATE locations SET lat = ?, lon = ? WHERE id = ?", (lat, lon, loc_id))
        if body.note is not None:
            conn.execute("UPDATE locations SET note = ? WHERE id = ?", (body.note, loc_id))
        if body.kind is not None:
            if body.kind not in KINDS:
                raise HTTPException(400, "Тип места: place, trip или other")
            conn.execute("UPDATE locations SET kind = ? WHERE id = ?", (body.kind, loc_id))
        conn.commit()
    return {"ok": True}


class MergeIn(BaseModel):
    into: int


@app.post("/api/locations/{loc_id}/merge")
def api_merge_location(loc_id: int, body: MergeIn):
    """Объединить локацию loc_id с локацией body.into (loc_id исчезает)."""
    with db.connect() as conn:
        try:
            merge_locations(conn, loc_id, body.into)
        except ValueError as e:
            raise HTTPException(404, str(e))
        conn.commit()
    return {"ok": True}


# --- Точки внутри мест -------------------------------------------------------

FACING = {"С": 0, "СВ": 45, "В": 90, "ЮВ": 135, "Ю": 180, "ЮЗ": 225, "З": 270, "СЗ": 315}


class SpotIn(BaseModel):
    name: str | None = None
    coords: str | None = None
    facing: int | None = None       # градусы, куда смотрит берег; null — не задано
    is_default: bool | None = None
    note: str | None = None


def _set_default(conn, loc_id: int, spot_id: int, on: bool) -> None:
    if on:
        conn.execute("UPDATE spots SET is_default = 0 WHERE location_id = ?", (loc_id,))
    conn.execute("UPDATE spots SET is_default = ? WHERE id = ?", (1 if on else 0, spot_id))


@app.get("/api/spots")
def api_spots():
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT s.*, l.name AS location_name,
                   (SELECT COUNT(*) FROM days d WHERE d.spot_id = s.id) AS day_count
            FROM spots s JOIN locations l ON l.id = s.location_id
            ORDER BY l.name, s.is_default DESC, s.name
        """).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/locations/{loc_id}/spots")
def api_create_spot(loc_id: int, body: SpotIn):
    if not (body.name or "").strip():
        raise HTTPException(400, "Нужно название точки")
    lat, lon = parse_coords(body.coords or "")
    if lat is None:
        raise HTTPException(400, "Нужны координаты точки")
    if body.facing is not None and not 0 <= body.facing < 360:
        raise HTTPException(400, "Направление — градусы от 0 до 359")
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM locations WHERE id = ?", (loc_id,)).fetchone():
            raise HTTPException(404, "Место не найдено")
        first = not conn.execute("SELECT 1 FROM spots WHERE location_id = ?", (loc_id,)).fetchone()
        spot_id = conn.execute(
            "INSERT INTO spots(location_id, name, lat, lon, facing, note) VALUES (?, ?, ?, ?, ?, ?)",
            (loc_id, body.name.strip(), lat, lon, body.facing, body.note)).lastrowid
        # Первая точка места сразу становится точкой по умолчанию, если не сказано иное
        _set_default(conn, loc_id, spot_id, body.is_default if body.is_default is not None else first)
        conn.commit()
    return {"id": spot_id}


@app.put("/api/spots/{spot_id}")
def api_update_spot(spot_id: int, body: SpotIn):
    with db.connect() as conn:
        spot = conn.execute("SELECT * FROM spots WHERE id = ?", (spot_id,)).fetchone()
        if not spot:
            raise HTTPException(404, "Точка не найдена")
        f = body.model_fields_set
        if "name" in f and (body.name or "").strip():
            conn.execute("UPDATE spots SET name = ? WHERE id = ?", (body.name.strip(), spot_id))
        if "coords" in f:
            lat, lon = parse_coords(body.coords or "")
            if lat is None:
                raise HTTPException(400, "Нужны координаты точки")
            conn.execute("UPDATE spots SET lat = ?, lon = ? WHERE id = ?", (lat, lon, spot_id))
        if "facing" in f:
            if body.facing is not None and not 0 <= body.facing < 360:
                raise HTTPException(400, "Направление — градусы от 0 до 359")
            conn.execute("UPDATE spots SET facing = ? WHERE id = ?", (body.facing, spot_id))
        if "note" in f:
            conn.execute("UPDATE spots SET note = ? WHERE id = ?", (body.note, spot_id))
        if "is_default" in f and body.is_default is not None:
            _set_default(conn, spot["location_id"], spot_id, body.is_default)
        conn.commit()
    return {"ok": True}


@app.delete("/api/spots/{spot_id}")
def api_delete_spot(spot_id: int):
    with db.connect() as conn:
        spot = conn.execute("SELECT * FROM spots WHERE id = ?", (spot_id,)).fetchone()
        if not spot:
            raise HTTPException(404, "Точка не найдена")
        # Дни с этой точкой вернутся к точке по умолчанию (ON DELETE SET NULL)
        conn.execute("DELETE FROM spots WHERE id = ?", (spot_id,))
        # Удалили точку по умолчанию — ею становится первая из оставшихся
        if spot["is_default"]:
            nxt = conn.execute("SELECT id FROM spots WHERE location_id = ? ORDER BY id LIMIT 1",
                               (spot["location_id"],)).fetchone()
            if nxt:
                conn.execute("UPDATE spots SET is_default = 1 WHERE id = ?", (nxt["id"],))
        conn.commit()
    return {"ok": True}


# --- Погода ------------------------------------------------------------------

weather_loader = weather.Loader()


@app.post("/api/weather/fetch")
def api_weather_fetch():
    """Запускает фоновую загрузку погоды для дней, у которых её ещё нет."""
    return weather_loader.start(db.connect)


@app.get("/api/weather/status")
def api_weather_status():
    st = weather_loader.status()
    if not st["running"]:
        with db.connect() as conn:
            st["pending"] = len(weather.plan(conn))
    return st


# --- Планирование выходных ----------------------------------------------------

@app.get("/api/plan")
def api_plan(dates: str | None = None, all: bool = False, refresh: bool = False):  # noqa: A002
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    if dates:
        try:
            ds = sorted({_date.fromisoformat(x) for x in dates.split(",") if x})
        except ValueError:
            raise HTTPException(400, "Даты в формате ГГГГ-ММ-ДД через запятую")
    else:
        ds = plan.default_dates(today)
    if not ds or any(d < today or d > today + _td(days=plan.MAX_AHEAD) for d in ds):
        raise HTTPException(400, f"Можно планировать на сегодня и до {plan.MAX_AHEAD} дней вперёд")
    with db.connect() as conn:
        try:
            return plan.build(conn, ds[:3], all_places=all, force=refresh)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Прогноз недоступен: {e}")
        except RuntimeError as e:
            raise HTTPException(502, str(e))


# --- iNaturalist ---------------------------------------------------------------

inat_loader = inat.Loader()


class InatJobs(BaseModel):
    jobs: list[list]       # [[lat, lon, [месяцы] | null], …]


@app.post("/api/inat/fetch")
def api_inat_fetch(body: InatJobs):
    jobs = [(float(j[0]), float(j[1]), [int(m) for m in j[2]] if j[2] else None) for j in body.jobs[:60]]
    return inat_loader.start(db.connect, jobs)


@app.get("/api/inat/status")
def api_inat_status():
    return inat_loader.status()


@app.get("/api/inat/place/{loc_id}")
def api_inat_place(loc_id: int, months: str | None = None):
    """Виды рядом с местом — за указанные месяцы (например, 10,11) или за весь год.
    Для карточки на карте; загружает, если нет в кэше."""
    try:
        ms = sorted({int(m) for m in months.split(",") if m}) if months else None
    except ValueError:
        raise HTTPException(400, "Месяцы — числа через запятую")
    if ms and not all(1 <= m <= 12 for m in ms):
        raise HTTPException(400, "Месяцы — от 1 до 12")
    with db.connect() as conn:
        inat.ensure_schema(conn)
        loc = conn.execute("SELECT * FROM locations WHERE id = ?", (loc_id,)).fetchone()
        if not loc or loc["lat"] is None:
            raise HTTPException(404, "У места нет координат")
        info = inat.place_info(conn, loc["lat"], loc["lon"], inat.RADIUS_KM, ms)
        if info is None:
            try:
                with httpx.Client(headers=inat.HEADERS) as client:
                    inat.fetch_place(conn, client, loc["lat"], loc["lon"], inat.RADIUS_KM, ms)
            except (httpx.HTTPError, RuntimeError) as e:
                raise HTTPException(502, f"iNaturalist недоступен: {e}")
            info = inat.place_info(conn, loc["lat"], loc["lon"], inat.RADIUS_KM, ms)
    return {**info, "name": loc["name"], "lat": loc["lat"], "lon": loc["lon"]}


# --- Поиск мест на карте (геокодер OpenStreetMap Nominatim) ------------------
# Правила Nominatim: не чаще 1 запроса в секунду и понятный User-Agent.
# Ответы кэшируем, чтобы не спрашивать одно и то же дважды.

_geo_cache: dict[str, list] = {}
_geo_lock = threading.Lock()
_geo_last = 0.0
# Ленинградская область с запасом — результаты отсюда идут первыми
LENOBL_VIEWBOX = "27.3,61.4,35.8,58.3"


@app.get("/api/geocode")
def api_geocode(q: str):
    global _geo_last
    q = q.strip()
    if not q:
        return []
    if q in _geo_cache:
        return _geo_cache[q]
    with _geo_lock:
        wait = 1.1 - (time.time() - _geo_last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = httpx.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "jsonv2", "limit": 6, "accept-language": "ru",
                        "countrycodes": "ru,kz", "viewbox": LENOBL_VIEWBOX, "bounded": 0},
                headers={"User-Agent": "shooting-journal/0.1 (personal photo log)"},
                timeout=15,
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Геокодер недоступен: {e}")
        finally:
            _geo_last = time.time()
    results = [
        {"name": x.get("display_name", ""), "lat": float(x["lat"]), "lon": float(x["lon"]),
         "type": x.get("addresstype") or x.get("type", "")}
        for x in r.json()
    ]
    _geo_cache[q] = results
    return results
