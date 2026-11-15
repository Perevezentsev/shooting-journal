"""Мост к Lua-плагину Lightroom: что записать в каталог и что уже записано.

Журнал сам в каталог не пишет. Плагин (lightroom/journal.lrplugin) забирает отсюда список,
показывает предпросмотр и пишет через API Lightroom одной отменяемой операцией, а потом
сообщает, что записал (lr_written). Следующий запуск берёт только новое и изменившееся.

Обмен — строки с табуляцией (UTF-8): в Lua нет JSON, а тащить библиотеку ради этого незачем.

Ответ /api/lr/pending:
    journal\t1                                    — заголовок и версия формата
    S\t<id>\t<группа>\t<вид>\t<латынь>\t<старая группа>\t<старый вид>   — ключевое слово (заменить старое)
    R\t<id>\t<старая группа>\t<старый вид>         — снять слово: в журнале вида больше нет
    G\t<id>\t<lat>\t<lon>\t<старые lat>\t<старые lon> — координаты по треку
    X\t<id>\t<старые lat>\t<старые lon>             — убрать записанные журналом координаты (кадр вне трека)
    H\t<id>                                       — кадр ближе 200 м к скрытой находке: снять GPS в Lightroom,
                                                    кто бы его ни поставил (каждый запуск; без GPS — ничего)

Ответ плагина /api/lr/done — те же буквы о сделанном:
    S\t<id>\t<группа>\t<вид>\t<латынь>   R\t<id>   G\t<id>\t<lat>\t<lon>   O\t<id> (свои координаты)   X\t<id>
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from . import field, review

SCHEMA = """
CREATE TABLE IF NOT EXISTS lr_written (
    image_id   INTEGER PRIMARY KEY,           -- photos.image_id = localIdentifier в Lightroom
    kw_group   TEXT,                          -- записанное слово: Виды › kw_group › kw_name
    kw_name    TEXT,
    kw_sci     TEXT,                          -- синоним (латынь)
    kw_at      TEXT,
    gps_lat    REAL,                          -- записанные координаты
    gps_lon    REAL,
    gps_state  TEXT,                          -- written | own (у кадра свои координаты — не трогаем)
    gps_at     TEXT
);
"""

ROOT_KEYWORD = "Виды"
GROUP_TITLE = {"birds": "Птицы", "mammals": "Млекопитающие", "herps": "Земноводные и рептилии",
               "insects": "Насекомые", "plants": "Растения", "fungi": "Грибы", "other": "Прочее"}
GPS_EPS = 1e-6


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _kw_text(s: str | None) -> str:
    """Имя ключевого слова: в Lightroom запятая разделяет слова, табуляция — наш разделитель."""
    s = re.sub(r"\s*,\s*", " / ", (s or "").strip())
    return re.sub(r"[\t\r\n|]", " ", s).strip()


def wanted_keywords(conn) -> dict[int, tuple[str, str, str]]:
    """{image_id: (группа, вид, латынь)} — что должно стоять в Lightroom по решениям журнала.
    Подтверждённый вид — «Обыкновенная оляпка», латынь синонимом. Неопределимо с группой —
    «Кулики sp.» (латынь «Scolopacidae sp.»). Без группы, «не то» и «без вида» — без слова."""
    names = review._names(conn)
    taxa = None
    out = {}
    for r in conn.execute("SELECT sc.image_id, sc.status, sc.scientific, sc.name_ru FROM species_confirm sc "
                          "JOIN photos p ON p.image_id = sc.image_id "
                          "WHERE sc.status IN ('confirmed', 'unid')"):
        sci, ru = r["scientific"], r["name_ru"]
        if not (sci or ru):
            continue
        if r["status"] == "confirmed":
            lab = review._label(sci, names) if sci else {}
            group = lab.get("group") or "other"
            name = ru or lab.get("ru") or sci
            latin = sci if name != sci else ""
        else:
            if taxa is None:
                taxa = review._taxa(names)
            t = taxa.get(sci or "") or next((t for t in taxa.values() if ru and t["ru"] == ru), None)
            # «Птицы» целиком, без семейства — сама группа
            whole = next((g for g, title in GROUP_TITLE.items()
                          if (ru or "").casefold() == title.casefold() or sci in (k for k, v in review.GROUPS.items() if v == g)), None)
            group = whole or (t or {}).get("group") or "other"
            name = f"{ru or sci} sp."
            latin = f"{sci} sp." if sci and ru else ""
        out[r["image_id"]] = (GROUP_TITLE.get(group, "Прочее"), _kw_text(name), _kw_text(latin))
    return out


def wanted_gps(conn) -> tuple[dict[int, tuple[float, float]], set[int]]:
    """Координаты по треку, которые можно отдать в Lightroom, и кадры рядом со скрытыми находками."""
    secret: dict[int, list] = {}
    for r in conn.execute("SELECT day_id, lat, lon FROM finds WHERE sensitive = 1"):
        secret.setdefault(r["day_id"], []).append((r["lat"], r["lon"]))
    ok, near = {}, set()
    for r in conn.execute("SELECT image_id, day_id, lat, lon FROM photos WHERE lat IS NOT NULL"):
        pts = secret.get(r["day_id"])
        if pts and field.near_sensitive(r["lat"], r["lon"], pts):
            near.add(r["image_id"])
        else:
            ok[r["image_id"]] = (round(r["lat"], 6), round(r["lon"], 6))
    return ok, near


def _same(a, b) -> bool:
    return a[0] is not None and b[0] is not None and abs(a[0] - b[0]) < GPS_EPS and abs(a[1] - b[1]) < GPS_EPS


def pending(conn) -> list[tuple]:
    """Что отдать плагину: строки S/R/G/X (см. описание модуля)."""
    done = {r["image_id"]: r for r in conn.execute("SELECT * FROM lr_written")}
    out = []
    kws = wanted_keywords(conn)
    for img, (grp, name, sci) in sorted(kws.items()):
        w = done.get(img)
        if w and (w["kw_group"], w["kw_name"], w["kw_sci"] or "") == (grp, name, sci):
            continue
        out.append(("S", img, grp, name, sci, (w and w["kw_group"]) or "", (w and w["kw_name"]) or ""))
    for img, w in sorted(done.items()):
        if w["kw_name"] and img not in kws:
            out.append(("R", img, w["kw_group"], w["kw_name"]))
    gps, near = wanted_gps(conn)
    for img, ll in sorted(gps.items()):
        w = done.get(img)
        if w and w["gps_state"] == "own":
            continue
        old = (w["gps_lat"], w["gps_lon"]) if w and w["gps_state"] == "written" else (None, None)
        if _same(old, ll):
            continue
        out.append(("G", img, f"{ll[0]:.6f}", f"{ll[1]:.6f}",
                    "" if old[0] is None else f"{old[0]:.6f}", "" if old[1] is None else f"{old[1]:.6f}"))
    for img, w in sorted(done.items()):
        # записанные журналом координаты, которые больше отдавать нельзя: кадр вне трека или трек убран
        if w["gps_state"] == "written" and img not in gps and img not in near:
            out.append(("X", img, f"{w['gps_lat']:.6f}", f"{w['gps_lon']:.6f}"))
    # рядом со скрытой находкой координат в каталоге быть не должно — ни наших, ни поставленных
    # Lightroom по треклогу или вручную; плагин проверяет и снимает при каждом запуске
    out += [("H", img) for img in sorted(near)]
    return out


def pending_text(conn) -> str:
    lines = ["journal\t1"] + ["\t".join(str(x) for x in row) for row in pending(conn)]
    return "\n".join(lines) + "\n"


def summary(conn) -> dict:
    rows = pending(conn)
    count = {k: sum(1 for r in rows if r[0] == k) for k in "SRGXH"}
    w = conn.execute("SELECT SUM(kw_name IS NOT NULL) kw, SUM(gps_state = 'written') gps, "
                     "SUM(gps_state = 'own') own FROM lr_written").fetchone()
    return {"pending": count, "written": {"keywords": w["kw"] or 0, "gps": w["gps"] or 0, "own_gps": w["own"] or 0}}


def mark_done(conn, text: str) -> dict:
    """Плагин сообщил, что записал. Неизвестные строки пропускаем."""
    now = datetime.now().isoformat(timespec="seconds")
    n = {k: 0 for k in "SRGOX"}
    for line in text.splitlines():
        p = line.rstrip("\r").split("\t")
        if not p or p[0] not in n or len(p) < 2 or not p[1].isdigit():
            continue
        img = int(p[1])
        conn.execute("INSERT OR IGNORE INTO lr_written(image_id) VALUES (?)", (img,))
        if p[0] == "S" and len(p) >= 4:
            conn.execute("UPDATE lr_written SET kw_group = ?, kw_name = ?, kw_sci = ?, kw_at = ? WHERE image_id = ?",
                         (p[2], p[3], (p[4] if len(p) > 4 else "") or None, now, img))
        elif p[0] == "R":
            conn.execute("UPDATE lr_written SET kw_group = NULL, kw_name = NULL, kw_sci = NULL, kw_at = ? "
                         "WHERE image_id = ?", (now, img))
        elif p[0] == "G" and len(p) >= 4:
            conn.execute("UPDATE lr_written SET gps_lat = ?, gps_lon = ?, gps_state = 'written', gps_at = ? "
                         "WHERE image_id = ?", (float(p[2]), float(p[3]), now, img))
        elif p[0] == "O":
            conn.execute("UPDATE lr_written SET gps_lat = NULL, gps_lon = NULL, gps_state = 'own', gps_at = ? "
                         "WHERE image_id = ?", (now, img))
        elif p[0] == "X":
            conn.execute("UPDATE lr_written SET gps_lat = NULL, gps_lon = NULL, gps_state = NULL, gps_at = ? "
                         "WHERE image_id = ?", (now, img))
        else:
            continue
        n[p[0]] += 1
    conn.execute("DELETE FROM lr_written WHERE kw_name IS NULL AND gps_state IS NULL")
    conn.commit()
    return n
