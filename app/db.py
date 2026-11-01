"""Своя база журнала: SQLite в одном файле data/journal.db."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "journal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    lat     REAL,
    lon     REAL,
    note    TEXT,
    kind    TEXT NOT NULL DEFAULT 'place',     -- place | trip (поездка) | other (не место)
    radius_m INTEGER                           -- размер места, м (пусто — по умолчанию)
);

-- Точки внутри места: «Шепелевский — западная сторона мыса».
-- Одна точка может быть по умолчанию: «где я обычно стою на этом месте».
CREATE TABLE IF NOT EXISTS spots (
    id           INTEGER PRIMARY KEY,
    location_id  INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    facing       INTEGER,                      -- куда смотрит берег (к воде), градусы 0–359
    is_default   INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS days (
    id               INTEGER PRIMARY KEY,
    day_key          TEXT NOT NULL UNIQUE,      -- дата|название из папки
    date             TEXT NOT NULL,             -- YYYY-MM-DD
    location_id      INTEGER,                   -- устарело, оставлено для совместимости
    location_manual  INTEGER NOT NULL DEFAULT 0, -- 1 = места назначены вручную
    source_folder    TEXT,
    source_name      TEXT,                      -- название места как в папке
    first_shot       TEXT,
    last_shot        TEXT,
    photo_count      INTEGER NOT NULL DEFAULT 0,
    selected_count   INTEGER NOT NULL DEFAULT 0,
    notes            TEXT,
    synced_at        TEXT,
    spot_id          INTEGER REFERENCES spots(id) ON DELETE SET NULL  -- точка, если указана явно
);

-- День может относиться к нескольким местам ("Бронка+Знаменка")
CREATE TABLE IF NOT EXISTS day_locations (
    day_id       INTEGER NOT NULL REFERENCES days(id) ON DELETE CASCADE,
    location_id  INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    PRIMARY KEY (day_id, location_id)
);

CREATE TABLE IF NOT EXISTS photos (
    image_id      INTEGER PRIMARY KEY,          -- id из каталога Lightroom
    day_id        INTEGER NOT NULL REFERENCES days(id) ON DELETE CASCADE,
    file_name     TEXT,
    folder_path   TEXT,
    capture_time  TEXT,
    pick          INTEGER,
    rating        INTEGER,
    keywords      TEXT                          -- через "; "
);

-- Синонимы названий: "Коккоревский" → Коккорево,
-- "Котлин Шепелевский" → Котлин и Шепелевский маяк (две строки)
CREATE TABLE IF NOT EXISTS location_aliases (
    alias_norm   TEXT NOT NULL,                 -- нормализованное: нижний регистр, ё→е
    location_id  INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    PRIMARY KEY (alias_norm, location_id)
);

CREATE INDEX IF NOT EXISTS idx_days_date ON days(date);
CREATE INDEX IF NOT EXISTS idx_photos_day ON photos(day_id);
CREATE INDEX IF NOT EXISTS idx_daylocs_loc ON day_locations(location_id);
CREATE INDEX IF NOT EXISTS idx_spots_loc ON spots(location_id);
"""


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _migrate(conn: sqlite3.Connection) -> None:
    """Обновляет базу, созданную прошлыми версиями, без потери данных."""
    if "kind" not in _columns(conn, "locations"):
        conn.execute("ALTER TABLE locations ADD COLUMN kind TEXT NOT NULL DEFAULT 'place'")
    if "source_name" not in _columns(conn, "days"):
        conn.execute("ALTER TABLE days ADD COLUMN source_name TEXT")
    if "radius_m" not in _columns(conn, "locations"):
        conn.execute("ALTER TABLE locations ADD COLUMN radius_m INTEGER")
    if "spot_id" not in _columns(conn, "days"):
        conn.execute("ALTER TABLE days ADD COLUMN spot_id INTEGER REFERENCES spots(id) ON DELETE SET NULL")

    # Синонимы: раньше один синоним = одно место, теперь может быть несколько
    pk = [r[1] for r in conn.execute("PRAGMA table_info(location_aliases)") if r[5]]
    if pk == ["alias_norm"]:
        conn.executescript("""
            ALTER TABLE location_aliases RENAME TO location_aliases_old;
            CREATE TABLE location_aliases (
                alias_norm TEXT NOT NULL,
                location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
                PRIMARY KEY (alias_norm, location_id));
            INSERT OR IGNORE INTO location_aliases SELECT alias_norm, location_id FROM location_aliases_old;
            DROP TABLE location_aliases_old;
        """)

    # Место дня переезжает из старой колонки days.location_id в day_locations.
    # После переноса колонку обнуляем: в базах прошлых версий она ссылается
    # на места и не даёт их удалять при объединении.
    if conn.execute("SELECT 1 FROM days WHERE location_id IS NOT NULL LIMIT 1").fetchone():
        if not conn.execute("SELECT 1 FROM day_locations LIMIT 1").fetchone():
            conn.execute("""INSERT OR IGNORE INTO day_locations(day_id, location_id)
                            SELECT id, location_id FROM days WHERE location_id IS NOT NULL""")
        conn.execute("UPDATE days SET location_id = NULL WHERE location_id IS NOT NULL")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn
