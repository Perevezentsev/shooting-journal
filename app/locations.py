"""Справочник мест: распознавание названий, синонимы, объединение и правила.

Правила лежат в файле locations.txt в корне проекта и применяются
при каждой синхронизации, поэтому твои решения переживают пересинхронизацию.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

KINDS = {"place": "место", "trip": "поездка", "other": "не место"}
KIND_BY_LABEL = {"не место": "other", "поездка": "trip", "место": "place"}


def norm_name(name: str) -> str:
    """Ключ для сравнения названий: регистр, ё/е и лишние пробелы не важны."""
    return re.sub(r"\s+", " ", name.lower().replace("ё", "е")).strip(" .")


def split_combo(name: str) -> list[str]:
    """'Бронка+Знаменка' → ['Бронка', 'Знаменка']."""
    return [p.strip() for p in name.split("+") if p.strip()]


def _find_by_name(conn: sqlite3.Connection, name: str) -> int | None:
    key = norm_name(name)
    for loc in conn.execute("SELECT id, name FROM locations"):
        if norm_name(loc["name"]) == key:
            return loc["id"]
    return None


def ensure_location(conn: sqlite3.Connection, name: str) -> int:
    """Место с таким названием (без учёта синонимов). Создаёт, если нет."""
    loc_id = _find_by_name(conn, name)
    if loc_id is None:
        loc_id = conn.execute("INSERT INTO locations(name) VALUES (?)", (name.strip(),)).lastrowid
    return loc_id


def resolve(conn: sqlite3.Connection, name: str) -> list[int]:
    """Название из папки → id мест. Учитывает '+', синонимы и правила."""
    ids: list[int] = []
    for part in split_combo(name):
        key = norm_name(part)
        rows = conn.execute("SELECT location_id FROM location_aliases WHERE alias_norm = ?", (key,)).fetchall()
        if rows:
            found = [r["location_id"] for r in rows]
        else:
            found = [ensure_location(conn, part)]
        for i in found:
            if i not in ids:
                ids.append(i)
    return ids


def set_day_locations(conn: sqlite3.Connection, day_id: int, loc_ids: list[int]) -> None:
    conn.execute("DELETE FROM day_locations WHERE day_id = ?", (day_id,))
    conn.executemany("INSERT OR IGNORE INTO day_locations VALUES (?, ?)", [(day_id, i) for i in loc_ids])


def _replace_location(conn: sqlite3.Connection, src_id: int, dst_ids: list[int]) -> None:
    """Все дни места src переходят к местам dst_ids, src удаляется."""
    src = conn.execute("SELECT * FROM locations WHERE id = ?", (src_id,)).fetchone()
    if not src:
        return
    for dst in dst_ids:
        conn.execute("""INSERT OR IGNORE INTO day_locations(day_id, location_id)
                        SELECT day_id, ? FROM day_locations WHERE location_id = ?""", (dst, src_id))
        conn.execute("""INSERT OR IGNORE INTO location_aliases(alias_norm, location_id)
                        SELECT alias_norm, ? FROM location_aliases WHERE location_id = ?""", (dst, src_id))
    if len(dst_ids) == 1:
        dst = conn.execute("SELECT * FROM locations WHERE id = ?", (dst_ids[0],)).fetchone()
        if dst["lat"] is None and src["lat"] is not None:
            conn.execute("UPDATE locations SET lat = ?, lon = ? WHERE id = ?", (src["lat"], src["lon"], dst["id"]))
    conn.execute("DELETE FROM locations WHERE id = ?", (src_id,))


def merge_locations(conn: sqlite3.Connection, src_id: int, dst_id: int) -> None:
    """Объединение из интерфейса: src становится синонимом dst."""
    if src_id == dst_id:
        return
    src = conn.execute("SELECT name FROM locations WHERE id = ?", (src_id,)).fetchone()
    if not src or not conn.execute("SELECT 1 FROM locations WHERE id = ?", (dst_id,)).fetchone():
        raise ValueError("Место не найдено")
    _set_alias(conn, src["name"], [dst_id])
    _replace_location(conn, src_id, [dst_id])


def _set_alias(conn: sqlite3.Connection, alias: str, loc_ids: list[int]) -> None:
    key = norm_name(alias)
    conn.execute("DELETE FROM location_aliases WHERE alias_norm = ?", (key,))
    conn.executemany("INSERT OR IGNORE INTO location_aliases VALUES (?, ?)", [(key, i) for i in loc_ids])


# --- Правила из locations.txt ------------------------------------------------

def parse_rules(text: str) -> list[tuple]:
    """Строки файла правил:
        Главное <- Вариант 1, Вариант 2      объединить
        Составное = Место 1 + Место 2         одно название → несколько мест
        не место: Тест 80-200, Луна           тип
        поездка: Almaty, Sorbylak             тип
    Всё после # — комментарий."""
    rules = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "<-" in line:
            main, rest = line.split("<-", 1)
            rules.append(("merge", main.strip(), [x.strip() for x in rest.split(",") if x.strip()]))
        elif "=" in line:
            alias, rest = line.split("=", 1)
            rules.append(("combo", alias.strip(), split_combo(rest)))
        elif ":" in line and norm_name(line.split(":", 1)[0]) in KIND_BY_LABEL:
            label, rest = line.split(":", 1)
            rules.append(("kind", KIND_BY_LABEL[norm_name(label)], [x.strip() for x in rest.split(",") if x.strip()]))
        else:
            raise ValueError(f"locations.txt, строка {n}: не понял «{raw.strip()}»")
    return rules


def apply_rules(conn: sqlite3.Connection, path: Path) -> int:
    """Применяет правила. Повторное применение ничего не ломает."""
    if not path.exists():
        return 0
    rules = parse_rules(path.read_text(encoding="utf-8"))
    for kind, head, items in rules:
        if kind == "merge":
            dst = ensure_location(conn, head)
            _set_alias(conn, head, [dst])
            for alias in items:
                _set_alias(conn, alias, [dst])
                src = _find_by_name(conn, alias)
                if src is not None and src != dst:
                    _replace_location(conn, src, [dst])
        elif kind == "combo":
            targets = [i for name in items for i in resolve(conn, name)]
            _set_alias(conn, head, targets)
            src = _find_by_name(conn, head)
            if src is not None and src not in targets:
                _replace_location(conn, src, targets)
        elif kind == "kind":
            for name in items:
                for loc_id in resolve(conn, name):
                    conn.execute("UPDATE locations SET kind = ? WHERE id = ?", (head, loc_id))
    return len(rules)


def drop_orphans(conn: sqlite3.Connection) -> int:
    """Удаляет места без дней, координат и заметок (например, старые 'А+Б')."""
    # Место, на которое ссылается чужой синоним (правило из locations.txt), не трогаем.
    # Сравниваем в Python: lower() в SQLite не понимает кириллицу.
    candidates = conn.execute("""
        SELECT id, name FROM locations
        WHERE id NOT IN (SELECT location_id FROM day_locations)
          AND lat IS NULL AND COALESCE(note, '') = '' AND kind = 'place'
    """).fetchall()
    removed = 0
    for loc in candidates:
        aliases = {r["alias_norm"] for r in conn.execute(
            "SELECT alias_norm FROM location_aliases WHERE location_id = ?", (loc["id"],))}
        if aliases - {norm_name(loc["name"])}:
            continue
        conn.execute("DELETE FROM locations WHERE id = ?", (loc["id"],))
        removed += 1
    return removed
