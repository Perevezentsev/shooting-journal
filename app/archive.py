"""Архив: что модель нашла на неотобранных кадрах (без ★2) и что из этого показать.

Решения здесь не принимаются — только кандидаты. Подтверждает Алексей.

- «видел, без хорошего кадра» (seen_frames) — автоматически, без подтверждения, в лайфлист не идёт:
  топ-1 ≥ 0.9 (порог можно поднять для вида, рода, семейства или группы — "seen_min" в config.json),
  животное найдено рамкой ≥ 0.5 (весь кадр не считается), не похоже на «без вида», нет человека,
  вид из регионального списка дня (домашнего или поездки; поездка без координат — не считается).
- кандидаты в первые встречи (first_candidates) — для вида из лайфлиста самый ранний неотобранный
  кадр с топ-1 ≥ 0.8, если он раньше первой подтверждённой встречи;
- «может быть, новый вид» (maybe_new) — вид с топ-1 ≥ 0.8 на неотобранных, которого нет в лайфлисте.

Отклонённое не предлагается снова: species_dismiss (first — кадр, new — вид).
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

from . import review

ROOT = Path(__file__).resolve().parent.parent
SEEN_MIN = 0.9          # «видел»: уверенность топ-1
DET_MIN = 0.5           # «видел»: рамка MegaDetector
CAND_MIN = 0.8          # кандидаты в первые встречи и «может быть, новый вид»
SELECTED = review.SELECTED_RATING

SCHEMA = """
CREATE TABLE IF NOT EXISTS species_dismiss (
    kind     TEXT NOT NULL,                   -- first (key — image_id) | new (key — латынь вида)
    key      TEXT NOT NULL,
    at       TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _config() -> dict:
    try:
        return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def seen_thresholds(cfg: dict | None = None) -> dict[str, float]:
    """Свои пороги «видел»: {"Larus": 0.95, "Scolopacidae": 0.95, "birds": 0.92, "Anas platyrhynchos": 0.97}."""
    raw = (cfg if cfg is not None else _config()).get("seen_min") or {}
    return {str(k): float(v) for k, v in raw.items()}


def _threshold(lab: dict, th: dict[str, float]) -> float:
    sci = lab.get("sci") or ""
    for key in (sci, sci.split(" ")[0], lab.get("family"), lab.get("group")):
        if key and key in th:
            return max(th[key], SEEN_MIN)
    return SEEN_MIN


def _allowed(conn) -> tuple[dict[int, str], dict[str, set[str] | bool | None]]:
    """Профиль дня и допустимые виды профиля: home — домашний список, bbox:… — список поездки,
    all — поездка без координат (None: «видел» там не считаем). Домашнего списка нет вовсе
    (демо, первый запуск) — True: проверять не по чему, считаем все виды."""
    from . import species as sp
    profiles = sp.day_profiles(conn)
    if sp.home_bbox(conn) is None:                   # списков нет совсем — проверять не по чему
        return profiles, {p: True for p in set(profiles.values()) | {"home"}}
    sets: dict[str, set[str] | None] = {}
    confirmed = {r[0] for r in conn.execute("SELECT DISTINCT scientific FROM species_confirm "
                                            "WHERE status = 'confirmed' AND scientific IS NOT NULL")}
    for prof in set(profiles.values()) | {"home"}:
        if prof == "all":
            sets[prof] = None
            continue
        bbox = None if prof == "home" else [float(v) for v in prof[5:].split(",")]
        reg = sp._region_cached(conn, bbox)
        if reg:
            sets[prof] = {s["sci"] for s in reg["species"] if s["count"] >= sp.REGION_MIN_OBS} | confirmed
        else:
            sets[prof] = True if prof == "home" else None
    return profiles, sets


def _empty(conn) -> set[int]:
    try:
        return {r[0] for r in conn.execute("SELECT image_id FROM species_empty WHERE sim_none > sim_species")}
    except sqlite3.OperationalError:
        return set()


def frames(conn, min_score: float, *, need_box: bool = True, year: str | None = None) -> list[dict]:
    """Неотобранные кадры без решения, где модель уверена в виде (общие правила кандидатов)."""
    names = review._names(conn)
    profiles, allowed = _allowed(conn)
    empty = _empty(conn)
    q = ("SELECT p.image_id, p.day_id, p.capture_time, p.rating, d.date, c.status, c.det_conf, c.person, "
         "sp.scientific, sp.common, sp.class_name, sp.score FROM species_preds sp "
         "JOIN photos p ON p.image_id = sp.image_id JOIN days d ON d.id = p.day_id "
         "JOIN species_crops c ON c.image_id = sp.image_id "
         "WHERE sp.rank = 1 AND sp.score >= ? AND COALESCE(p.rating, 0) < ? AND c.person = 0 "
         + ("AND c.status = 'crop' AND c.det_conf >= ? " if need_box else "AND c.status IN ('crop', 'full') ")
         + ("AND d.date LIKE ? " if year else "")
         + "AND NOT EXISTS (SELECT 1 FROM species_confirm sc WHERE sc.image_id = p.image_id)")
    args: list = [min_score, SELECTED] + ([DET_MIN] if need_box else []) + ([f"{year}%"] if year else [])
    out = []
    for r in conn.execute(q, args):
        if r["image_id"] in empty:
            continue
        lab = review._label(r["scientific"], names, r["common"], r["class_name"])
        if not lab.get("sci"):
            continue
        ok = allowed.get(profiles.get(r["day_id"], "home"))
        if ok is None or (ok is not True and lab["sci"] not in ok):
            continue
        out.append({"id": r["image_id"], "day_id": r["day_id"], "date": r["date"], "time": r["capture_time"],
                    "score": round(r["score"], 3), "det": r["det_conf"], "kind": r["status"], **lab})
    return out


def seen_frames(conn, cfg: dict | None = None) -> list[dict]:
    """«Видел, без хорошего кадра»: см. правила в начале модуля."""
    th = seen_thresholds(cfg)
    return [f for f in frames(conn, SEEN_MIN) if f["score"] >= _threshold(f, th)]


def _places(conn) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for d, name in conn.execute("SELECT dl.day_id, l.name FROM day_locations dl JOIN locations l "
                                "ON l.id = dl.location_id ORDER BY l.name"):
        out.setdefault(d, []).append(name)
    return out


def _dismissed(conn, kind: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT key FROM species_dismiss WHERE kind = ?", (kind,))}


def _brief(f: dict, places: dict) -> dict:
    return {"id": f["id"], "date": f["date"], "time": (f["time"] or "")[11:16], "score": f["score"],
            "day_id": f["day_id"], "places": places.get(f["day_id"], [])}


def first_candidates(conn) -> list[dict]:
    """По виду из лайфлиста — самый ранний неотобранный кадр раньше первой подтверждённой встречи."""
    from . import lifelist
    first: dict[str, dict] = {}
    for o in lifelist.observations(conn):               # по времени — первый и есть первая встреча
        first.setdefault(o["sci"], o)
    if not first:
        return []
    places = _places(conn)
    skip = _dismissed(conn, "first")
    best: dict[str, dict] = {}
    for f in frames(conn, CAND_MIN, need_box=False):
        o = first.get(f["sci"])
        if not o or str(f["id"]) in skip or (f["time"] or "") >= (o["time"] or ""):
            continue
        if f["sci"] not in best or (f["time"] or "") < (best[f["sci"]]["time"] or ""):
            best[f["sci"]] = f
    out = []
    for sci, f in best.items():
        o = first[sci]
        out.append({"sci": sci, "ru": o["ru"] or f.get("ru"), "group": f.get("group"),
                    "cand": _brief(f, places),
                    "first": {"id": o["image_id"], "date": o["date"], "time": (o["time"] or "")[11:16],
                              "day_id": o["day_id"], "places": places.get(o["day_id"], [])}})
    out.sort(key=lambda x: x["cand"]["date"])
    return out


def maybe_new(conn, per_species: int = 3) -> list[dict]:
    """Виды, уверенно найденные на неотобранных, которых нет в лайфлисте: лучшие кадры, лучше из разных дней."""
    life = {r[0] for r in conn.execute("SELECT DISTINCT scientific FROM species_confirm "
                                       "WHERE status = 'confirmed' AND scientific IS NOT NULL")}
    names = review._names(conn)
    life |= {review._label(s, names).get("sci") for s in list(life)}
    skip = _dismissed(conn, "new")
    places = _places(conn)
    by: dict[str, list[dict]] = {}
    for f in frames(conn, CAND_MIN, need_box=False):
        if f["sci"] in life or f["sci"] in skip:
            continue
        by.setdefault(f["sci"], []).append(f)
    out = []
    for sci, fs in by.items():
        fs.sort(key=lambda f: -f["score"])
        pick, days = [], set()
        for f in fs:                                     # сначала лучшие из разных дней
            if f["day_id"] not in days:
                pick.append(f)
                days.add(f["day_id"])
            if len(pick) == per_species:
                break
        for f in fs:
            if len(pick) == per_species:
                break
            if f not in pick:
                pick.append(f)
        f0 = fs[0]
        out.append({"sci": sci, "ru": f0.get("ru") or f0.get("en"), "group": f0.get("group"),
                    "family": f0.get("family"), "family_ru": f0.get("family_ru"), "inat_id": f0.get("inat_id"),
                    "n": len(fs), "days": len({f["day_id"] for f in fs}), "best": fs[0]["score"],
                    "frames": [_brief(f, places) for f in pick]})
    out.sort(key=lambda x: (-x["days"], -x["best"]))
    return out


def dismiss(conn, kind: str, key: str) -> None:
    from datetime import datetime
    if kind not in ("first", "new"):
        raise ValueError(kind)
    conn.execute("INSERT OR REPLACE INTO species_dismiss(kind, key, at) VALUES (?, ?, ?)",
                 (kind, str(key), datetime.now().isoformat(timespec="seconds")))
    conn.commit()


def undismiss(conn, kind: str, key: str) -> None:
    conn.execute("DELETE FROM species_dismiss WHERE kind = ? AND key = ?", (kind, str(key)))
    conn.commit()


# --- отчёт после прогона -----------------------------------------------------

def report(conn, top: int = 15) -> dict:
    """Сколько распознано, сколько «видел» и по каким видам, кадры без превью по годам."""
    total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    st = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM species_crops GROUP BY status")}
    nopv = [tuple(r) for r in conn.execute(
        "SELECT substr(d.date, 1, 4), COUNT(*) FROM species_crops c JOIN photos p ON p.image_id = c.image_id "
        "JOIN days d ON d.id = p.day_id WHERE c.status = 'no_preview' GROUP BY 1 ORDER BY 1 DESC")]
    seen = seen_frames(conn)
    by: dict[str, list[dict]] = {}
    for f in seen:
        by.setdefault(f["sci"], []).append(f)
    species = sorted(({"sci": s, "ru": fs[0].get("ru") or fs[0].get("en"), "group": fs[0].get("group"),
                       "frames": len(fs), "days": len({f["day_id"] for f in fs}), "ids": [f["id"] for f in fs]}
                      for s, fs in by.items()), key=lambda x: -x["frames"])
    return {"photos": total, "status": st, "no_preview_by_year": nopv, "seen_frames": len(seen),
            "seen_species": len(species), "seen_days": len({f["day_id"] for f in seen}), "top": species[:top],
            "firsts": len(first_candidates(conn)), "maybe_new": len(maybe_new(conn))}


def contact_sheet(conn, cfg: dict, out_dir: Path, top: int = 15, per: int = 5, seed: int | None = None) -> Path:
    """Лист для глаз: по `per` случайных кропов «видел» для `top` видов с наибольшим числом кадров."""
    rep = report(conn, top)
    rnd = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in rep["top"]:
        ids = rnd.sample(s["ids"], min(per, len(s["ids"])))
        cells = []
        for i in ids:
            p = review.crop_jpeg(conn, cfg, i)
            src = Path("..") / ".." / "crops" / p.name if p else None
            cells.append(f'<figure><img src="{src}" alt="" loading="lazy"><figcaption>{i}</figcaption></figure>'
                         if p else f"<figure><figcaption>{i}: нет кропа</figcaption></figure>")
        th = s["ru"] or s["sci"]
        rows.append(f'<section><h2>{th} <small>{s["sci"]} · {s["frames"]} кадров, {s["days"]} дн.</small></h2>'
                    f'<div class="row">{"".join(cells)}</div></section>')
    html = ("<!doctype html><meta charset=utf-8><title>Проверка «видел»</title><style>"
            "body{font:14px system-ui;margin:20px;background:#f6f5f1;color:#1d2226}"
            "h2{font-size:16px;margin:18px 0 6px} small{color:#6b7378;font-weight:400}"
            ".row{display:flex;gap:8px;flex-wrap:wrap} figure{margin:0} img{width:180px;height:180px;object-fit:cover;"
            "border-radius:4px;display:block} figcaption{font-size:11px;color:#6b7378}"
            "@media(prefers-color-scheme:dark){body{background:#15191b;color:#e6e4de}}</style>"
            f"<h1>Проверка «видел, без хорошего кадра»</h1><p>Всего {rep['seen_frames']} кадров, "
            f"{rep['seen_species']} видов. Если вид часто ошибается — подними порог: "
            "<code>\"seen_min\": {\"Larus\": 0.95}</code> в config.json (вид, род, семейство или группа: birds…).</p>"
            + "".join(rows))
    path = out_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    return path
