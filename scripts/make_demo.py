"""Демо-база на выдуманных данных — посмотреть журнал без своего каталога Lightroom.

    python3 scripts/make_demo.py               создаёт data/demo.db
    JOURNAL_DB=data/demo.db ./run.sh           запускает журнал на ней

Места, даты и заметки выдуманы. Синхронизация с каталогом в демо-базе отключена.
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

DEMO_PATH = ROOT / "data" / "demo.db"

# (название, широта, долгота, тип, «вес» — как часто туда ездят)
PLACES = [
    ("Северный мыс",        58.472, 33.561, "place", 14),
    ("Песчаная бухта",      58.438, 33.802, "place", 9),
    ("Тихое озеро",         58.714, 34.218, "place", 11),
    ("Сосновый бор",        58.652, 34.648, "place", 8),
    ("Заливные луга",       58.284, 34.502, "place", 10),
    ("Мшистое болото",      58.361, 34.102, "place", 7),
    ("Еловый ручей",        58.142, 34.827, "place", 6),
    ("Старый карьер",       58.201, 34.354, "place", 5),
    ("Дальняя коса",      59.332, 35.504, "place", 6),
    ("Речная пойма",        58.053, 34.611, "place", 4),
    ("Каменная гряда",      58.797, 33.902, "place", 3),
    ("Ольховая роща",       None,   None,   "place", 2),
    ("Устье протоки",       None,   None,   "place", 2),
    ("Горы, поход 2025",    None,   None,   "trip",  2),
    ("Тест объектива",      None,   None,   "other", 2),
]

# Точки внутри мест: (место, название, широта, долгота, куда смотрит берег, по умолчанию)
SPOTS = [
    ("Северный мыс",   "западная сторона мыса", 58.4735, 33.5490, 270, False),
    ("Северный мыс",   "восточная сторона, у стоянки", 58.4712, 33.5705, 90, True),
    ("Песчаная бухта", "дальний конец косы", 58.4402, 33.8161, 0, True),
    ("Дальняя коса", "у тростников", 59.3336, 35.4987, 90, True),
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
    ("Bucephala clangula", "Гоголь", "Aves", "Anatidae", "Утиные", {3, 4, 5, 9, 10, 11}, {"Северный мыс", "Дальняя коса", "Тихое озеро"}),
    ("Anas platyrhynchos", "Кряква", "Aves", "Anatidae", "Утиные", None, None),
    ("Cygnus cygnus", "Лебедь-кликун", "Aves", "Anatidae", "Утиные", {4, 5, 10, 11}, {"Дальняя коса", "Заливные луга"}),
    ("Vanellus vanellus", "Чибис", "Aves", "Charadriidae", "Ржанковые", SPRING_AUTUMN, {"Заливные луга"}),
    ("Tringa totanus", "Травник", "Aves", "Scolopacidae", "Бекасовые", {5, 6, 7, 8}, {"Песчаная бухта", "Северный мыс"}),
    ("Calidris alpina", "Чернозобик", "Aves", "Scolopacidae", "Бекасовые", {5, 8, 9}, {"Песчаная бухта"}),
    ("Numenius arquata", "Большой кроншнеп", "Aves", "Scolopacidae", "Бекасовые", {4, 5, 6, 7}, {"Заливные луга", "Мшистое болото"}),
    ("Larus canus", "Сизая чайка", "Aves", "Laridae", "Чайковые", SPRING_AUTUMN, None),
    ("Sterna hirundo", "Речная крачка", "Aves", "Laridae", "Чайковые", {5, 6, 7, 8}, {"Северный мыс", "Дальняя коса"}),
    ("Ardea cinerea", "Серая цапля", "Aves", "Ardeidae", "Цаплевые", SPRING_AUTUMN, {"Речная пойма", "Тихое озеро"}),
    ("Grus grus", "Серый журавль", "Aves", "Gruidae", "Журавлиные", {4, 5, 9}, {"Мшистое болото", "Заливные луга"}),
    ("Haliaeetus albicilla", "Орлан-белохвост", "Aves", "Accipitridae", "Ястребиные", None, {"Дальняя коса", "Северный мыс"}),
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


FINDS = [("Нора", "свежие выбросы земли, следы у входа", 1),
         ("Дупло", "слышал птенцов", 1),
         ("Точка у берега", "вечером свет в спину, птица подходит близко", 0)]


def add_tracks(conn, coords: dict[str, tuple[float, float]], n_days: int = 3) -> int:
    """Выдуманные треки (петля вокруг места) и находки — для «Карты дня»: координаты кадров
    берутся по треку, скрытые находки при выгрузке огрубляются."""
    import json
    import math
    from app import field
    rows = conn.execute(
        "SELECT d.id, d.source_name, MIN(p.capture_time) t1, MAX(p.capture_time) t2, COUNT(*) n "
        "FROM days d JOIN photos p ON p.day_id = d.id GROUP BY d.id ORDER BY n DESC").fetchall()
    done = 0
    for r in rows:
        if done >= n_days or r["source_name"] not in coords:
            continue
        lat0, lon0 = coords[r["source_name"]]
        tz = field.UTC_OFFSET_H
        t1 = datetime.fromisoformat(r["t1"]) - timedelta(minutes=25)
        t2 = datetime.fromisoformat(r["t2"]) + timedelta(minutes=25)
        u1 = int(t1.replace(tzinfo=timezone(timedelta(hours=tz))).timestamp())     # GPX — в UTC
        steps = max(60, int((t2 - t1).total_seconds() // 15))
        pts, length = [], 0.0
        r_km, ph = random.uniform(.8, 1.6), random.uniform(0, 6.3)
        for k in range(steps + 1):                       # петля с «гулянием»: эллипс + шум
            a = 2 * math.pi * k / steps
            rr = r_km * (1 + .18 * math.sin(5 * a) + .05 * math.sin(17 * a + ph))
            la = lat0 + rr / 111 * math.sin(a) * .7
            lo = lon0 + rr / (111 * math.cos(math.radians(lat0))) * (1 - math.cos(a))
            if pts:
                length += field.dist_m(pts[-1][0], pts[-1][1], la, lo)
            pts.append([round(la, 6), round(lo, 6), u1 + k * 15])
        iso = lambda u: datetime.fromtimestamp(u, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        conn.execute("INSERT INTO tracks(day_id, file_path, file_mtime, started_at, ended_at, points, length_m, geom) "
                     "VALUES (?, ?, 0, ?, ?, ?, ?, ?)", (r["id"], f"demo/{r['id']}/Трек/track.gpx", iso(pts[0][2]),
                                                         iso(pts[-1][2]), len(pts), round(length), json.dumps(pts)))
        for (name, note, sens), frac in zip(FINDS, (.22, .5, .78)):
            la, lo, u = pts[int(frac * steps)]
            conn.execute("INSERT INTO finds(day_id, source_key, lat, lon, time, name, note, sensitive) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (r["id"], f"demo|{r['id']}|{name}", la + .0004, lo + .0006,
                                                             iso(u), name, note, sens))
        field.geotag_day(conn, r["id"])
        done += 1
    return done


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
    tracks = add_tracks(conn, {p[0]: (p[1], p[2]) for p in PLACES if p[1] is not None})
    conn.commit()
    print(f"Готово: {DEMO_PATH.relative_to(ROOT)} — {len(PLACES)} мест, {n} дней, {len(SPOTS)} точки; "
          f"виды: {sp['photos']} кадров, подтверждено {sp['confirmed']}, «видел» {sp['seen']}; треков {tracks}")
    print("Запуск: JOURNAL_DB=data/demo.db ./run.sh")


if __name__ == "__main__":
    main()
