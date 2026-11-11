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
    ("Ладожская коса", "у тростников", 60.8336, 31.0987, 90, True),
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


# Виды для вкладок «Виды» и «Лайфлист»: (латынь, по-русски, группа iNat, семейство, по-русски,
# месяцы или None — круглый год, места-любимцы или None — где угодно)
SPRING_AUTUMN = {4, 5, 6, 7, 8, 9, 10}
SPECIES = [
    ("Cinclus cinclus", "Оляпка", "Aves", "Cinclidae", "Оляпковые", {1, 2, 3, 11, 12}, {"Еловый ручей", "Речная пойма"}),
    ("Bucephala clangula", "Гоголь", "Aves", "Anatidae", "Утиные", {3, 4, 5, 9, 10, 11}, {"Северный мыс", "Ладожская коса", "Тихое озеро"}),
    ("Anas platyrhynchos", "Кряква", "Aves", "Anatidae", "Утиные", None, None),
    ("Cygnus cygnus", "Лебедь-кликун", "Aves", "Anatidae", "Утиные", {4, 5, 10, 11}, {"Ладожская коса", "Заливные луга"}),
    ("Vanellus vanellus", "Чибис", "Aves", "Charadriidae", "Ржанковые", SPRING_AUTUMN, {"Заливные луга"}),
    ("Tringa totanus", "Травник", "Aves", "Scolopacidae", "Бекасовые", {5, 6, 7, 8}, {"Песчаная бухта", "Северный мыс"}),
    ("Calidris alpina", "Чернозобик", "Aves", "Scolopacidae", "Бекасовые", {5, 8, 9}, {"Песчаная бухта"}),
    ("Numenius arquata", "Большой кроншнеп", "Aves", "Scolopacidae", "Бекасовые", {4, 5, 6, 7}, {"Заливные луга", "Мшистое болото"}),
    ("Larus canus", "Сизая чайка", "Aves", "Laridae", "Чайковые", SPRING_AUTUMN, None),
    ("Sterna hirundo", "Речная крачка", "Aves", "Laridae", "Чайковые", {5, 6, 7, 8}, {"Северный мыс", "Ладожская коса"}),
    ("Ardea cinerea", "Серая цапля", "Aves", "Ardeidae", "Цаплевые", SPRING_AUTUMN, {"Речная пойма", "Тихое озеро"}),
    ("Grus grus", "Серый журавль", "Aves", "Gruidae", "Журавлиные", {4, 5, 9}, {"Мшистое болото", "Заливные луга"}),
    ("Haliaeetus albicilla", "Орлан-белохвост", "Aves", "Accipitridae", "Ястребиные", None, {"Ладожская коса", "Северный мыс"}),
    ("Circus aeruginosus", "Болотный лунь", "Aves", "Accipitridae", "Ястребиные", {5, 6, 7, 8}, {"Заливные луга"}),
    ("Dryocopus martius", "Желна", "Aves", "Picidae", "Дятловые", None, {"Сосновый бор", "Еловый ручей"}),
    ("Dendrocopos major", "Большой пёстрый дятел", "Aves", "Picidae", "Дятловые", None, {"Сосновый бор", "Ольховая роща"}),
    ("Parus major", "Большая синица", "Aves", "Paridae", "Синицевые", None, None),
    ("Lophophanes cristatus", "Хохлатая синица", "Aves", "Paridae", "Синицевые", None, {"Сосновый бор"}),
    ("Sitta europaea", "Поползень", "Aves", "Sittidae", "Поползневые", None, {"Сосновый бор", "Ольховая роща"}),
    ("Erithacus rubecula", "Зарянка", "Aves", "Muscicapidae", "Мухоловковые", SPRING_AUTUMN, {"Еловый ручей", "Ольховая роща"}),
    ("Pyrrhula pyrrhula", "Снегирь", "Aves", "Fringillidae", "Вьюрковые", {1, 2, 3, 10, 11, 12}, None),
    ("Bombycilla garrulus", "Свиристель", "Aves", "Bombycillidae", "Свиристелевые", {1, 2, 3, 11, 12}, None),
    ("Alcedo atthis", "Обыкновенный зимородок", "Aves", "Alcedinidae", "Зимородковые", {6, 7, 8, 9}, {"Речная пойма"}),
    ("Castor fiber", "Речной бобр", "Mammalia", "Castoridae", "Бобровые", None, {"Речная пойма", "Еловый ручей"}),
    ("Sciurus vulgaris", "Обыкновенная белка", "Mammalia", "Sciuridae", "Беличьи", None, {"Сосновый бор"}),
    ("Lepus timidus", "Заяц-беляк", "Mammalia", "Leporidae", "Зайцевые", None, {"Мшистое болото", "Сосновый бор"}),
    ("Halichoerus grypus", "Длинномордый тюлень", "Mammalia", "Phocidae", "Настоящие тюлени", {4, 5, 6, 9}, {"Каменная гряда", "Северный мыс"}),
    ("Rana temporaria", "Травяная лягушка", "Amphibia", "Ranidae", "Настоящие лягушки", {4, 5, 6, 7, 8}, {"Мшистое болото"}),
    ("Aglais io", "Павлиний глаз", "Insecta", "Nymphalidae", "Нимфалиды", {5, 6, 7, 8}, None),
    ("Drosera rotundifolia", "Росянка круглолистная", "Plantae", "Droseraceae", "Росянковые", {6, 7, 8}, {"Мшистое болото"}),
    ("Amanita muscaria", "Красный мухомор", "Fungi", "Amanitaceae", "Мухоморовые", {8, 9, 10}, {"Сосновый бор"}),
]
NEW_DAYS = 8          # последние дни — «новые»: распознаны целиком, с неотобранными кадрами


def add_species(conn, days: list[tuple[int, date, str]]) -> dict:
    """Кадры, подсказки и решения: архив — только ★2+ (почти всё разобрано),
    новые дни — все кадры, неотобранные без решения (часть — «видел, без хорошего кадра»)."""
    for sci, ru, grp, fam, fam_ru, _, _ in SPECIES:
        conn.execute("INSERT INTO species_names(scientific, ru, inat_id, obs, grp, family, family_ru) "
                     "VALUES (?, ?, NULL, ?, ?, ?, ?)", (sci, ru, random.randint(50, 5000), grp, fam, fam_ru))
    by_sci = {x[0]: x for x in SPECIES}
    stats = {"photos": 0, "confirmed": 0, "seen": 0}
    img = 900_000_000
    days = sorted(days, key=lambda x: x[1])
    edge = days[-NEW_DAYS - 1][0] if len(days) > NEW_DAYS else 0
    for k, (day_id, d, place) in enumerate(days):
        new = day_id > edge
        pool = [x for x in SPECIES if (x[5] is None or d.month in x[5]) and (x[6] is None or place in x[6])]
        if not pool:
            continue
        seen_today = random.sample(pool, min(len(pool), random.randint(1, 5)))
        t = datetime(d.year, d.month, d.day, random.choice([6, 7, 8]), random.randint(0, 59))
        for sp in seen_today:
            for _ in range(random.randint(1, 4)):                     # серии по 1–4 кадра
                burst = random.randint(1, 4)
                for b in range(burst):
                    rating = random.choices([0, 1, 2, 3, 5], [45, 15, 25, 10, 5])[0]
                    if rating < 2 and not new:
                        t += timedelta(seconds=2)
                        continue                                       # архив: неотобранные не распознавались
                    img += 1
                    conn.execute("INSERT INTO photos(image_id, day_id, file_name, folder_path, capture_time, pick, rating, keywords) "
                                 "VALUES (?, ?, ?, '', ?, 0, ?, '')", (img, day_id, f"DSC_{img % 100000:05d}.NEF",
                                                                      t.isoformat(timespec="seconds"), rating))
                    conn.execute("INSERT INTO species_crops(image_id, status, det_conf, model, processed_at) "
                                 "VALUES (?, 'crop', ?, 'bioclip-2', ?)", (img, round(random.uniform(.6, .98), 2), t.isoformat()))
                    top = random.uniform(.55, .99)
                    others = random.sample([x for x in SPECIES if x is not sp], 4)
                    rest = sorted((random.uniform(0, 1 - top) for _ in others), reverse=True)
                    for rank, (x, sc) in enumerate([(sp, top)] + list(zip(others, rest)), 1):
                        conn.execute("INSERT INTO species_preds(image_id, rank, scientific, class_name, score) "
                                     "VALUES (?, ?, ?, ?, ?)", (img, rank, x[0], x[2], round(sc, 3)))
                    stats["photos"] += 1
                    status = None
                    if rating >= 2 and (not new or random.random() < .5):
                        status = random.choices(["confirmed", "unid", "none"], [88, 7, 5])[0]
                    if status:
                        sci, name = (sp[0], sp[1]) if status == "confirmed" else \
                            ((sp[3], sp[4]) if status == "unid" and random.random() < .7 else (None, None))
                        conn.execute("INSERT INTO species_confirm(image_id, status, scientific, name_ru, day_id, source, confirmed_at) "
                                     "VALUES (?, ?, ?, ?, ?, 'top1', ?)", (img, status, sci, name, day_id, t.isoformat()))
                        stats["confirmed"] += status == "confirmed"
                    elif rating < 2 and top >= .9:
                        stats["seen"] += 1
                    t += timedelta(seconds=2)
            t += timedelta(minutes=random.randint(3, 40))
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('species_all_after_day', ?)", (str(edge),))
    return stats


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
    made: list[tuple[int, date, str]] = []
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
        made.append((day_id, d, place))
        n += 1

    sp = add_species(conn, made)
    conn.commit()
    print(f"Готово: {DEMO_PATH.relative_to(ROOT)} — {len(PLACES)} мест, {n} дней, {len(SPOTS)} точки; "
          f"виды: {sp['photos']} кадров, подтверждено {sp['confirmed']}, «видел» {sp['seen']}")
    print("Запуск: JOURNAL_DB=data/demo.db ./run.sh")


if __name__ == "__main__":
    main()
