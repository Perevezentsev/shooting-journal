"""Вкладка «Виды»: разбор предсказаний, серии, подтверждения, кропы для сетки.

- Предсказания (species_preds) пересчитываются rescore'ом, решения Алексея живут
  отдельно — в species_confirm, вместе с днём и местом на момент подтверждения.
- Серия — соседние отобранные кадры одного дня, если между ними шла непрерывная съёмка
  (цепочка любых кадров дня, хоть без звёзд, с промежутками ≤ SERIES_GAP_S) или если
  у них один топ-1 и между ними не больше SAME_SPECIES_GAP_S. Топ-3 серии — средняя
  вероятность вида по её кадрам.
- Решения: confirmed (вид), rejected («не то» — подсказки мимо), none («без вида» — пейзаж).
- Кропы для сетки режутся из превью Lightroom по рамке MegaDetector и кэшируются
  в data/crops/. Превью только читаем.
"""

from __future__ import annotations

import io
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

from . import species as sp

SERIES_GAP_S = 10
SAME_SPECIES_GAP_S = 120
NO_SUGGEST_BELOW = 0.5      # «животное не найдено» и топ-1 ниже — подсказки не показываем
CROPS_DIR = sp.DATA / "crops"
THUMB_PX = 400
BIG_PX = 1600
# Группы для фильтра: иконическая группа iNat, иначе класс TreeOfLife
GROUPS = {"Aves": "birds", "Mammalia": "mammals", "Amphibia": "herps", "Reptilia": "herps",
          "Insecta": "insects", "Plantae": "plants", "Fungi": "fungi"}
PLANT_CLASSES = {"Magnoliopsida", "Liliopsida", "Pinopsida", "Polypodiopsida", "Bryopsida", "Lycopodiopsida",
                 "Equisetopsida", "Sphagnopsida", "Marchantiopsida", "Jungermanniopsida", "Gnetopsida",
                 "Cycadopsida", "Ginkgoopsida", "Polytrichopsida", "Andreaeopsida"}
FUNGI_CLASSES = {"Agaricomycetes", "Lecanoromycetes", "Sordariomycetes", "Leotiomycetes", "Pezizomycetes",
                 "Dothideomycetes", "Tremellomycetes", "Dacrymycetes", "Eurotiomycetes", "Arthoniomycetes",
                 "Pucciniomycetes", "Exobasidiomycetes", "Myxomycetes"}
CONF_BUCKETS = {"high": (0.9, 2), "mid": (0.5, 0.9), "low": (-1, 0.5)}

_store = None
_store_t = 0.0
_store_lock = threading.Lock()


def _preview_store(cfg: dict):
    """Индекс превью Lightroom; перечитываем раз в час (новые кадры, новые превью)."""
    global _store, _store_t
    with _store_lock:
        if _store is None or time.time() - _store_t > 3600:
            from .previews import PreviewStore, lrdata_for
            _store = PreviewStore(lrdata_for(cfg["catalog_path"]))
            _store_t = time.time()
        return _store


# ---------- список серий ----------

def _ts(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except ValueError:
        return None


def _names(conn) -> dict[str, dict]:
    """Названия по научному имени — и по имени iNat, и по имени TreeOfLife (синонимы)."""
    names = {r["scientific"]: dict(r) for r in conn.execute("SELECT * FROM species_names")}
    for tol, sci in conn.execute("SELECT tol_sci, scientific FROM species_alias"):
        if sci in names and tol not in names:
            names[tol] = {**names[sci], "inat_sci": sci}
    return names


def _group(names_row: dict | None, class_name: str | None) -> str:
    g = GROUPS.get((names_row or {}).get("grp"))
    if g:
        return g
    if class_name in GROUPS:
        return GROUPS[class_name]
    if class_name in PLANT_CLASSES:
        return "plants"
    if class_name in FUNGI_CLASSES:
        return "fungi"
    return "other"


def _label(sci: str | None, names: dict, common: str | None = None, class_name: str | None = None) -> dict:
    """Название для показа: русское, если есть; научное без автора."""
    if not sci:
        return {"sci": None, "ru": None, "en": None}
    clean = sci.split(" (")[0]
    clean = " ".join(clean.split()[:2]) if clean[:1].isupper() else clean
    n = names.get(clean) or {}
    # подтверждаем под именем iNat: так вид совпадёт с поиском и с наблюдениями iNat
    return {"sci": n.get("inat_sci") or clean, "ru": n.get("ru"), "en": n.get("en") or common or None,
            "inat_id": n.get("inat_id"), "group": _group(n, class_name), "rare": bool(n) and (n.get("obs") or 0) < sp.REGION_MIN_OBS}


def items(conn, *, day: str | None = None, loc: int | None = None, state: str = "todo",
          conf: str | None = None, kind: str | None = None) -> dict:
    names = _names(conn)
    day_locs: dict[int, list[int]] = {}
    for d, l in conn.execute("SELECT day_id, location_id FROM day_locations"):
        day_locs.setdefault(d, []).append(l)
    loc_names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM locations")}

    preds: dict[int, list] = {}
    for r in conn.execute("SELECT image_id, rank, scientific, common, score, class_name FROM species_preds "
                          "ORDER BY image_id, rank"):
        preds.setdefault(r[0], []).append((r[2], r[3], r[4], r[5]))
    confirms = {r["image_id"]: dict(r) for r in conn.execute("SELECT * FROM species_confirm")}

    rows = conn.execute(
        "SELECT p.image_id, p.day_id, p.capture_time, c.status AS kind, c.det_conf, d.date "
        "FROM photos p JOIN species_crops c ON c.image_id = p.image_id "
        "JOIN days d ON d.id = p.day_id "
        "WHERE c.status IN ('crop', 'full') "
        + ("AND d.date = ? " if day else "")
        + "ORDER BY p.day_id, p.capture_time, p.image_id", ([day] if day else [])).fetchall()

    series = _series(conn, rows, preds)

    out = []
    counts = {"todo": 0, "confirmed": 0, "rejected": 0, "none": 0}
    for frames in series:
        d = frames[0]["day_id"]
        if loc and loc not in day_locs.get(d, []):
            continue
        agg: dict[str, list] = {}
        fr_out = []
        for f in frames:
            ps = preds.get(f["image_id"], [])
            for sci, common, score, cls in ps:
                a = agg.setdefault(sci, [0.0, common, cls])
                a[0] += score
            c = confirms.get(f["image_id"])
            fr_out.append({
                "id": f["image_id"], "time": (f["capture_time"] or "")[11:19], "kind": f["kind"],
                "top": [{**_label(s, names, cm, cl), "score": round(sc, 3)} for s, cm, sc, cl in ps[:3]],
                "decision": _decision(c, names),
            })
        n = len(frames)
        top = sorted(((v[0] / n, s, v[1], v[2]) for s, v in agg.items()), reverse=True)[:3]
        top = [{**_label(s, names, cm, cl), "score": round(sc, 3)} for sc, s, cm, cl in top]
        kinds = {f["kind"] for f in frames}
        k = kinds.pop() if len(kinds) == 1 else "mixed"
        decided = [fr["decision"] for fr in fr_out if fr["decision"]]
        if len(decided) < n:
            st = "todo"
        elif any(x["status"] == "confirmed" for x in decided):
            st = "confirmed"
        elif all(x["status"] == "none" for x in decided):
            st = "none"
        else:
            st = "rejected"
        counts[st] += 1
        top1 = top[0]["score"] if top else 0
        if state != "all" and st != state:
            continue
        if conf in CONF_BUCKETS and not (CONF_BUCKETS[conf][0] <= top1 < CONF_BUCKETS[conf][1]):
            continue
        if kind == "crop" and k == "full" or kind == "full" and k != "full":
            continue
        rep = max(frames, key=lambda f: (f["kind"] == "crop", f["det_conf"] or 0))
        same = {(x["sci"], x["ru"]) for x in decided if x["status"] == "confirmed"}
        out.append({
            "id": frames[0]["image_id"], "rep": rep["image_id"], "n": n,
            "day_id": d, "date": frames[0]["date"], "time": (frames[0]["capture_time"] or "")[11:16],
            "places": [loc_names.get(l) for l in day_locs.get(d, []) if loc_names.get(l)],
            "kind": k, "top": top,
            "suggest": not (k == "full" and top1 < NO_SUGGEST_BELOW),
            "state": st,
            "decision": decided[0] if decided and len(same) <= 1 and len(decided) == n else None,
            "mixed_decisions": len(same) > 1,
            "key": _series_key(st, decided, top, names),
            # у всех кадров один топ-1 — серию можно подтверждать массово
            "uniform": len({fr["top"][0]["sci"] if fr["top"] else None for fr in fr_out}) == 1,
            "frames": fr_out,
        })
    out.sort(key=lambda s: (s["date"], s["time"]), reverse=True)
    return {"series": out, "counts": counts, "frames": len(rows)}


def _series_key(st: str, decided: list, top: list, names: dict) -> dict | None:
    """Вид серии для фильтров: подтверждённый (самый частый по кадрам), иначе топ-1."""
    conf = [d for d in decided if d["status"] == "confirmed"]
    if st == "confirmed" and conf:
        from collections import Counter
        sci, ru = Counter((d["sci"], d["ru"]) for d in conf).most_common(1)[0][0]
        n = names.get(sci) if sci else None
        return {"sci": sci or ru, "ru": ru or (n or {}).get("ru"), "group": _group(n, None)}
    if top:
        return {"sci": top[0]["sci"], "ru": top[0]["ru"], "group": top[0]["group"]}
    return None


def _series(conn, rows, preds) -> list[list]:
    """Склейка отобранных кадров в серии (см. описание модуля)."""
    days = sorted({r["day_id"] for r in rows})
    chain: dict[int, int] = {}          # image_id → номер непрерывной съёмки (по всем кадрам дня)
    for k in range(0, len(days), 500):
        part = days[k:k + 500]
        q = ",".join("?" * len(part))
        n, last_d, last_t = 0, None, None
        for img, d, ct in conn.execute(f"SELECT image_id, day_id, capture_time FROM photos "
                                       f"WHERE day_id IN ({q}) ORDER BY day_id, capture_time, image_id", part):
            t = _ts(ct)
            if d != last_d or t is None or last_t is None or t - last_t > SERIES_GAP_S:
                n += 1
            chain[img] = (k, n)
            last_d, last_t = d, t
    top1 = {i: ps[0][0] for i, ps in preds.items() if ps}
    series: list[list] = []
    prev = None
    for r in rows:
        t = _ts(r["capture_time"])
        join = False
        if prev is not None and r["day_id"] == prev["day_id"]:
            pt = _ts(prev["capture_time"])
            if chain.get(r["image_id"]) == chain.get(prev["image_id"]):
                join = True
            elif (t is not None and pt is not None and t - pt <= SAME_SPECIES_GAP_S
                  and top1.get(r["image_id"]) and top1.get(r["image_id"]) == top1.get(prev["image_id"])):
                join = True
        if join:
            series[-1].append(r)
        else:
            series.append([r])
        prev = r
    return series


def _decision(c: dict | None, names: dict) -> dict | None:
    if not c:
        return None
    lab = _label(c["scientific"], names) if c["scientific"] else {"sci": None, "en": None}
    return {"status": c["status"], "sci": lab["sci"], "ru": c["name_ru"] or lab.get("ru"),
            "source": c["source"]}


def meta(conn) -> dict:
    """Дни и места, где есть разобранные моделью кадры, — для фильтров."""
    days = [dict(r) for r in conn.execute(
        "SELECT d.id, d.date, COUNT(*) n, "
        "SUM(CASE WHEN sc.image_id IS NULL THEN 1 ELSE 0 END) todo, "
        "(SELECT GROUP_CONCAT(l.name, ', ') FROM day_locations dl JOIN locations l ON l.id = dl.location_id "
        " WHERE dl.day_id = d.id) places "
        "FROM photos p JOIN species_crops c ON c.image_id = p.image_id AND c.status IN ('crop','full') "
        "JOIN days d ON d.id = p.day_id LEFT JOIN species_confirm sc ON sc.image_id = p.image_id "
        "GROUP BY d.id ORDER BY d.date DESC")]
    locs = [dict(r) for r in conn.execute(
        "SELECT l.id, l.name, COUNT(DISTINCT p.image_id) n FROM locations l "
        "JOIN day_locations dl ON dl.location_id = l.id JOIN photos p ON p.day_id = dl.day_id "
        "JOIN species_crops c ON c.image_id = p.image_id AND c.status IN ('crop','full') "
        "GROUP BY l.id ORDER BY l.name")]
    return {"days": days, "locations": locs, "series_gap_s": SERIES_GAP_S}


# ---------- поиск вида ----------

def _norm(s: str | None) -> str:
    return (s or "").lower().replace("ё", "е")


def search(conn, q: str, limit: int = 15) -> list[dict]:
    q = _norm(q.strip())
    if len(q) < 2:
        return []
    found = []
    for r in conn.execute("SELECT scientific, ru, en, inat_id, obs, grp FROM species_names"):
        best = None
        for field in (r["ru"], r["scientific"], r["en"]):
            f = _norm(field)
            if not f:
                continue
            if f.startswith(q):
                best = 0 if best is None else min(best, 0)
            elif any(w.startswith(q) for w in f.split()):
                best = 1 if best is None else min(best, 1)
            elif q in f:
                best = 2 if best is None else min(best, 2)
        if best is not None:
            found.append((best, -(r["obs"] or 0), dict(r)))
    found.sort(key=lambda x: (x[0], x[1]))
    out = [{"sci": r["scientific"], "ru": r["ru"], "en": r["en"], "inat_id": r["inat_id"],
            "obs": r["obs"], "group": r["grp"], "rare": (r["obs"] or 0) < sp.REGION_MIN_OBS}
           for _, _, r in found[:limit]]
    # свои подтверждения видов не из регионального списка (вписанные вручную)
    if len(out) < limit:
        have = {o["sci"] for o in out}
        for r in conn.execute("SELECT DISTINCT scientific, name_ru FROM species_confirm "
                              "WHERE status = 'confirmed' AND scientific IS NOT NULL"):
            if r[0] not in have and (q in _norm(r[0]) or q in _norm(r[1])):
                out.append({"sci": r[0], "ru": r[1], "en": None, "inat_id": None, "obs": None,
                            "group": None, "rare": True, "own": True})
    return out[:limit]


# ---------- подтверждение ----------

def _photo_location(conn, image_id: int) -> tuple[int | None, int | None]:
    """День и место кадра: место дня; если мест несколько — ближайшее к координатам кадра
    (по треку), иначе место точки дня, иначе первое."""
    p = conn.execute("SELECT day_id, lat, lon FROM photos WHERE image_id = ?", (image_id,)).fetchone()
    if not p:
        return None, None
    locs = conn.execute("SELECT l.id, l.lat, l.lon FROM day_locations dl JOIN locations l ON l.id = dl.location_id "
                        "WHERE dl.day_id = ? ORDER BY l.name", (p["day_id"],)).fetchall()
    if not locs:
        return p["day_id"], None
    if len(locs) == 1:
        return p["day_id"], locs[0]["id"]
    if p["lat"] is not None:
        with_c = [l for l in locs if l["lat"] is not None]
        if with_c:
            k = math.cos(math.radians(p["lat"]))
            return p["day_id"], min(with_c, key=lambda l: (l["lat"] - p["lat"]) ** 2
                                    + ((l["lon"] - p["lon"]) * k) ** 2)["id"]
    spot = conn.execute("SELECT s.location_id FROM days d JOIN spots s ON s.id = d.spot_id WHERE d.id = ?",
                        (p["day_id"],)).fetchone()
    if spot and spot[0] in [l["id"] for l in locs]:
        return p["day_id"], spot[0]
    return p["day_id"], locs[0]["id"]


def decide(conn, ids: list[int], action: str, sci: str | None = None, ru: str | None = None,
           inat_id: int | None = None, source: str | None = None) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    if action == "clear":
        conn.executemany("DELETE FROM species_confirm WHERE image_id = ?", [(i,) for i in ids])
        conn.commit()
        return len(ids)
    if action == "confirm":
        if not (sci or ru):
            raise ValueError("Не указан вид")
        if sci and not (ru and inat_id):
            n = conn.execute("SELECT ru, inat_id FROM species_names WHERE scientific = ?", (sci,)).fetchone()
            if n:
                ru, inat_id = ru or n[0], inat_id or n[1]
        status = "confirmed"
    elif action in ("reject", "none"):
        sci = ru = inat_id = None
        status = "rejected" if action == "reject" else "none"
    else:
        raise ValueError(f"Неизвестное действие: {action}")
    for i in ids:
        day_id, loc_id = _photo_location(conn, i)
        conn.execute(
            "INSERT OR REPLACE INTO species_confirm(image_id, status, scientific, name_ru, inat_id, day_id, "
            "location_id, source, confirmed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (i, status, sci, ru, inat_id, day_id, loc_id, source, now))
    conn.commit()
    return len(ids)


def previous(conn, ids: list[int]) -> list[dict]:
    """Текущие решения по кадрам — чтобы фронтенд мог отменить последнее действие."""
    q = ",".join("?" * len(ids))
    return [dict(r) for r in conn.execute(f"SELECT * FROM species_confirm WHERE image_id IN ({q})", ids)]


def restore(conn, ids: list[int], rows: list[dict]) -> None:
    """Отмена: вернуть решения как были (rows из previous)."""
    conn.executemany("DELETE FROM species_confirm WHERE image_id = ?", [(i,) for i in ids])
    cols = ("image_id", "status", "scientific", "name_ru", "inat_id", "day_id", "location_id",
            "source", "confirmed_at")
    conn.executemany(f"INSERT INTO species_confirm({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [tuple(r.get(c) for c in cols) for r in rows if int(r["image_id"]) in ids])
    conn.commit()


# ---------- картинки ----------

def crop_jpeg(conn, cfg: dict, image_id: int) -> Path | None:
    """Кроп для сетки (или целый кадр, если животное не найдено), кэш в data/crops/."""
    dst = CROPS_DIR / f"{image_id}.jpg"
    if dst.exists():
        return dst
    c = conn.execute("SELECT status, bbox FROM species_crops WHERE image_id = ?", (image_id,)).fetchone()
    if not c:
        return None
    store = _preview_store(cfg)
    bbox = json.loads(c["bbox"]) if c["bbox"] else None
    if c["status"] == "crop" and bbox:
        src = store.read(image_id, max_long=sp.MID_LONG) if bbox[2] >= sp.SMALL_BOX else None
        src = src or store.read(image_id)
        if src is None:
            return None
        shown = sp._shown_size(src)
        scale = 1.0
        if shown:
            side = max(bbox[2] * shown[0], bbox[3] * shown[1]) * (1 + 2 * sp.CROP_PAD)
            scale = min(1.0, THUMB_PX / max(side, 1))
        img = src.image(scale)
        img = img.crop(sp.crop_box(bbox, *img.size))
    else:
        src = store.read(image_id, max_long=sp.MID_LONG)
        if src is None:
            return None
        img = src.image(min(1.0, THUMB_PX * 1.5 / max(src.long_side, 1)))
    img.thumbnail((THUMB_PX, THUMB_PX))
    CROPS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    img.save(tmp, "JPEG", quality=85)
    tmp.replace(dst)
    return dst


def big_jpeg(cfg: dict, image_id: int) -> bytes | None:
    """Кадр целиком, крупно — посмотреть перед решением."""
    src = _preview_store(cfg).read(image_id, max_long=sp.MID_LONG)
    if src is None:
        return None
    img = src.image(min(1.0, BIG_PX / max(src.long_side, 1)))
    img.thumbnail((BIG_PX, BIG_PX))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


_warm_lock = threading.Lock()


def warm(connect, cfg: dict, ids: list[int]) -> None:
    """Нарезать кропы заранее в фоне, пока Алексей смотрит первые карточки."""
    if not _warm_lock.acquire(blocking=False):
        return

    def job():
        try:
            conn = connect()
            for i in ids:
                if not (CROPS_DIR / f"{i}.jpg").exists():
                    try:
                        crop_jpeg(conn, cfg, i)
                    except Exception:
                        pass
            conn.close()
        finally:
            _warm_lock.release()
    threading.Thread(target=job, daemon=True).start()
