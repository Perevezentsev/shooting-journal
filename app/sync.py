"""Синхронизация: каталог Lightroom → база журнала.

Повторный запуск безопасен: заметки и вручную назначенные места
не перезаписываются, обновляются только данные из каталога.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .locations import apply_rules, drop_orphans, norm_name, resolve, set_day_locations
from .lrcat import group_days, open_catalog, read_photos


def sync_catalog(conn: sqlite3.Connection, cfg: dict, rules_path: Path | None = None) -> dict:
    lr = open_catalog(cfg["catalog_path"])
    photos, stats = read_photos(
        lr,
        path_prefix=cfg.get("path_prefix"),
        exclude=cfg.get("exclude", []),
        root_priority=cfg.get("root_priority", []),
    )
    lr.close()
    days = group_days(photos)

    # Правила из locations.txt — до разбора дней, чтобы синонимы уже работали
    rules = apply_rules(conn, rules_path) if rules_path else 0

    # Метка прогона: время + уникальный хвост, чтобы два быстрых прогона не совпали
    now = f'{datetime.now().isoformat(timespec="seconds")}#{uuid.uuid4().hex[:6]}'
    created = updated = 0

    conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen_photos (image_id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM seen_photos")

    for d in days:
        key = f"{d.date}|{norm_name(d.location) if d.location else '—'}"
        fields = dict(
            date=d.date,
            source_folder=d.location_folder,
            source_name=d.location,
            first_shot=d.first_shot.isoformat() if d.first_shot else None,
            last_shot=d.last_shot.isoformat() if d.last_shot else None,
            photo_count=len(d.photos),
            selected_count=d.selected_count,
            synced_at=now,
        )
        row = conn.execute("SELECT id, location_manual FROM days WHERE day_key = ?", (key,)).fetchone()
        if row:
            day_id = row["id"]
            sets = ", ".join(f"{k} = :{k}" for k in fields)
            conn.execute(f"UPDATE days SET {sets} WHERE id = :id", {**fields, "id": day_id})
            manual = row["location_manual"]
            updated += 1
        else:
            day_id = conn.execute(
                f"INSERT INTO days(day_key, {', '.join(fields)}) VALUES (:day_key, {', '.join(':' + k for k in fields)})",
                {**fields, "day_key": key},
            ).lastrowid
            manual = 0
            created += 1
        if not manual:
            set_day_locations(conn, day_id, resolve(conn, d.location) if d.location else [])

        conn.executemany(
            "INSERT INTO photos(image_id, day_id, file_name, folder_path, capture_time, pick, rating, keywords) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(image_id) DO UPDATE SET day_id=excluded.day_id, file_name=excluded.file_name, "
            "folder_path=excluded.folder_path, capture_time=excluded.capture_time, pick=excluded.pick, "
            "rating=excluded.rating, keywords=excluded.keywords",
            [
                (p.image_id, day_id, p.file_name, p.folder_path,
                 p.capture_time.isoformat() if p.capture_time else None,
                 p.pick, p.rating, "; ".join(p.keywords))
                for p in d.photos
            ],
        )
        conn.executemany("INSERT OR IGNORE INTO seen_photos VALUES (?)", [(p.image_id,) for p in d.photos])

    # Кадры, которых больше нет (исключены фильтром, дубли, удалены из каталога)
    conn.execute("DELETE FROM photos WHERE image_id NOT IN (SELECT image_id FROM seen_photos)")

    # Дни, которых больше нет. С твоими заметками или ручными местами — не удаляем,
    # только обнуляем счётчики, чтобы ничего твоего не пропало.
    removed = kept = 0
    for row in conn.execute("SELECT id, notes, location_manual FROM days WHERE synced_at IS NOT ?", (now,)).fetchall():
        if (row["notes"] or "").strip() or row["location_manual"]:
            conn.execute("UPDATE days SET photo_count = 0, selected_count = 0 WHERE id = ?", (row["id"],))
            kept += 1
        else:
            conn.execute("DELETE FROM days WHERE id = ?", (row["id"],))
            removed += 1

    orphans = drop_orphans(conn)
    conn.commit()
    return {
        "photos": len(photos), "days": len(days), "created": created, "updated": updated,
        "removed": removed, "kept_with_notes": kept, "rules": rules, "orphans": orphans,
        "excluded": stats["excluded"], "duplicates": stats["duplicates"],
    }
