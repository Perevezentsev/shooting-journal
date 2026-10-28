"""Локальный сервер журнала. Запуск: ./run.sh, затем http://localhost:8765"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db
from .locations import KINDS, merge_locations
from .sync import sync_catalog

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
RULES_PATH = ROOT / "locations.txt"
STATIC = ROOT / "static"

app = FastAPI(title="Журнал съёмок")


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
    for d in days:
        d["locations"] = locs.get(d["id"], [])
    return days


class DayUpdate(BaseModel):
    notes: str | None = None
    location_ids: list[int] | None = None   # задать места вручную ([] — без места)


@app.put("/api/days/{day_id}")
def api_update_day(day_id: int, body: DayUpdate):
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM days WHERE id = ?", (day_id,)).fetchone():
            raise HTTPException(404, "День не найден")
        if body.notes is not None:
            conn.execute("UPDATE days SET notes = ? WHERE id = ?", (body.notes, day_id))
        if body.location_ids is not None:
            conn.execute("DELETE FROM day_locations WHERE day_id = ?", (day_id,))
            conn.executemany("INSERT OR IGNORE INTO day_locations VALUES (?, ?)",
                             [(day_id, i) for i in body.location_ids])
            conn.execute("UPDATE days SET location_manual = 1 WHERE id = ?", (day_id,))
        conn.commit()
    return {"ok": True}


# --- Локации -----------------------------------------------------------------

@app.get("/api/locations")
def api_locations():
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT l.*, COUNT(d.id) AS day_count, MAX(d.date) AS last_date,
                   (SELECT COUNT(*) FROM location_aliases a WHERE a.location_id = l.id) AS alias_count
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
