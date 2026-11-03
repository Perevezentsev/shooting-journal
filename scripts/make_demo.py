"""Демо-база на выдуманных данных — посмотреть журнал без своего каталога Lightroom.

    python3 scripts/make_demo.py               создаёт data/demo.db
    JOURNAL_DB=data/demo.db ./run.sh           запускает журнал на ней

Места, даты и заметки выдуманы. Синхронизация с каталогом в демо-базе отключена.
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

DEMO_PATH = ROOT / "data" / "demo.db"

# (название, широта, долгота, тип, «вес» — как часто туда ездят)
PLACES = [
    ("Северный мыс",        59.972, 29.161, "place", 14),
    ("Песчаная бухта",      59.938, 29.402, "place", 9),
    ("Тихое озеро",         60.214, 29.818, "place", 11),
    ("Сосновый бор",        60.152, 30.248, "place", 8),
    ("Заливные луга",       59.784, 30.102, "place", 10),
    ("Мшистое болото",      59.861, 29.702, "place", 7),
    ("Еловый ручей",        59.642, 30.427, "place", 6),
    ("Старый карьер",       59.701, 29.954, "place", 5),
    ("Ладожская коса",      60.832, 31.104, "place", 6),
    ("Речная пойма",        59.553, 30.211, "place", 4),
    ("Каменная гряда",      60.297, 29.502, "place", 3),
    ("Ольховая роща",       None,   None,   "place", 2),
    ("Устье протоки",       None,   None,   "place", 2),
    ("Горы, поход 2025",    None,   None,   "trip",  2),
    ("Тест объектива",      None,   None,   "other", 2),
]

# Точки внутри мест: (место, название, широта, долгота, куда смотрит берег, по умолчанию)
SPOTS = [
    ("Северный мыс",   "западная сторона мыса", 59.9735, 29.1490, 270, False),
    ("Северный мыс",   "восточная сторона, у стоянки", 59.9712, 29.1705, 90, True),
    ("Песчаная бухта", "дальний конец косы", 59.9402, 29.4161, 0, True),
    ("Ладожская коса", "у тростников", 60.8336, 31.0987, 270, True),
]

NOTES = [
    "гоголи на разводьях, вода высокая",
    "литораль открыта, кулики у кромки",
    "туман до 9, потом ясно",
    "видел лося у дороги, не снял",
    "ветер в лицо, птица далеко",
    "первые чибисы в этом году",
    "оляпка на перекате",
    "много людей, рано уехал",
]


def season_weight(d: date) -> float:
    # Весна и осень — пролёт, выезжают чаще
    return {3: 1.2, 4: 2.2, 5: 2.4, 6: 1.0, 7: 0.7, 8: 1.1, 9: 1.6, 10: 1.8, 11: 0.8}.get(d.month, 0.6)


def main() -> None:
    random.seed(42)
    DEMO_PATH.parent.mkdir(exist_ok=True)
    if DEMO_PATH.exists():
        DEMO_PATH.unlink()
    conn = db.connect(DEMO_PATH)
    conn.execute("INSERT INTO meta VALUES ('demo', '1')")

    ids = {}
    for name, lat, lon, kind, _ in PLACES:
        ids[name] = conn.execute("INSERT INTO locations(name, lat, lon, kind) VALUES (?, ?, ?, ?)",
                                 (name, lat, lon, kind)).lastrowid
    for loc, name, lat, lon, facing, default in SPOTS:
        conn.execute("INSERT INTO spots(location_id, name, lat, lon, facing, is_default) VALUES (?, ?, ?, ?, ?, ?)",
                     (ids[loc], name, lat, lon, facing, int(default)))

    names = [p[0] for p in PLACES]
    weights = [p[4] for p in PLACES]
    start, end = date(2023, 3, 1), date(2026, 10, 25)
    used: set[tuple] = set()
    n = 0
    while n < 140:
        d = start + timedelta(days=random.randint(0, (end - start).days))
        if random.random() > season_weight(d) / 2.4:
            continue
        place = random.choices(names, weights)[0]
        if (d, place) in used:
            continue
        used.add((d, place))
        first = datetime(d.year, d.month, d.day, random.choice([5, 6, 7, 8, 9]), random.randint(0, 59))
        last = first + timedelta(minutes=random.randint(40, 300))
        photos = random.randint(15, 900)
        day_id = conn.execute(
            """INSERT INTO days(day_key, date, source_name, source_folder, first_shot, last_shot,
                                photo_count, selected_count, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"{d}|{place}", d.isoformat(), place, f"{d:%d.%m.%Y}_{place.replace(' ', '_')}",
             first.isoformat(), last.isoformat(), photos, int(photos * random.uniform(0, .12)),
             random.choice(NOTES) if random.random() < .3 else None)).lastrowid
        conn.execute("INSERT INTO day_locations VALUES (?, ?)", (day_id, ids[place]))
        n += 1

    conn.commit()
    print(f"Готово: {DEMO_PATH.relative_to(ROOT)} — {len(PLACES)} мест, {n} дней, {len(SPOTS)} точки")
    print("Запуск: JOURNAL_DB=data/demo.db ./run.sh")


if __name__ == "__main__":
    main()
