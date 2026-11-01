"""Локальный сервер журнала. Запуск: ./run.sh, затем http://localhost:8765"""

from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .locations import KINDS, merge_locations, set_day_locations
from .sync import sync_catalog

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


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# --- Синхронизация -----------------------------------------------------------

@app.post("/api/sync")
def api_sync():
    cfg = load_config()
    with db.connect() as conn:
        try:
            return sync_catalog(conn, cfg, RULES_PATH)
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
    for d in days:
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
