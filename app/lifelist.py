"""Лайфлист и аналитика по подтверждённым видам.

Источник — только решения Алексея (species_confirm, status = confirmed): подсказки модели
сюда не попадают, «неопределимо» тоже (вид не известен). День — текущий день кадра,
место — место дня; если мест у дня несколько — то, что запомнилось при подтверждении
(ближайшее к кадру), если оно всё ещё среди мест дня, иначе первое по имени.
"""

from __future__ import annotations

from datetime import date

from . import review


def observations(conn) -> list[dict]:
    """Подтверждённые кадры: вид, группа, день, место, время."""
    names = review._names(conn)
    day_locs: dict[int, list[int]] = {}
    for d, l in conn.execute("SELECT dl.day_id, dl.location_id FROM day_locations dl "
                             "JOIN locations l ON l.id = dl.location_id ORDER BY l.name"):
        day_locs.setdefault(d, []).append(l)
    out = []
    for r in conn.execute(
            "SELECT sc.image_id, sc.scientific, sc.name_ru, sc.location_id, p.day_id, p.capture_time, d.date "
            "FROM species_confirm sc JOIN photos p ON p.image_id = sc.image_id JOIN days d ON d.id = p.day_id "
            "WHERE sc.status = 'confirmed' ORDER BY p.capture_time"):
        lab = review._label(r["scientific"], names) if r["scientific"] else {}
        sci = lab.get("sci") or r["scientific"] or r["name_ru"]
        if not sci:
            continue
        locs = day_locs.get(r["day_id"], [])
        loc = locs[0] if len(locs) == 1 else (r["location_id"] if r["location_id"] in locs else
                                               (locs[0] if locs else None))
        out.append({"image_id": r["image_id"], "sci": sci, "ru": r["name_ru"] or lab.get("ru"),
                    "group": lab.get("group", "other"), "family": lab.get("family"),
                    "family_ru": lab.get("family_ru"), "day_id": r["day_id"], "date": r["date"],
                    "time": r["capture_time"], "loc": loc})
    return out


def _loc_names(conn) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT id, name FROM locations")}


def build(conn) -> dict:
    """Всё для вкладки «Лайфлист»: виды с первой встречей, годы, группы, места × месяцы."""
    obs = observations(conn)
    locn = _loc_names(conn)
    species: dict[str, dict] = {}
    for o in obs:                                    # obs по времени: первый кадр — первая встреча
        s = species.get(o["sci"])
        if not s:
            s = species[o["sci"]] = {
                "sci": o["sci"], "ru": o["ru"], "group": o["group"], "family": o["family"],
                "family_ru": o["family_ru"],
                "first": {"date": o["date"], "day_id": o["day_id"], "image_id": o["image_id"],
                          "loc": o["loc"], "place": locn.get(o["loc"])},
                "_days": set(), "_years": set(), "frames": 0, "last": o["date"], "_locs": set()}
        s["ru"] = s["ru"] or o["ru"]
        s["_days"].add(o["day_id"])
        s["_years"].add(o["date"][:4])
        if o["loc"]:
            s["_locs"].add(o["loc"])
        s["frames"] += 1
        s["last"] = o["date"]
    for s in species.values():
        s["days"] = len(s.pop("_days"))
        s["years"] = sorted(s.pop("_years"))
        s["places"] = len(s.pop("_locs"))

    years: dict[str, dict] = {}
    for s in species.values():
        for y in s["years"]:
            yr = years.setdefault(y, {"year": y, "species": 0, "new": 0, "groups": {}})
            yr["species"] += 1
            yr["groups"][s["group"]] = yr["groups"].get(s["group"], 0) + 1
        years[s["first"]["date"][:4]]["new"] += 1
    groups: dict[str, int] = {}
    for s in species.values():
        groups[s["group"]] = groups.get(s["group"], 0) + 1

    # места × месяцы: в скольких днях вид подтверждён на месте в этот месяц (все годы)
    cells: dict[int, dict[str, dict[int, set]]] = {}
    for o in obs:
        if o["loc"] is None:
            continue
        m = int(o["date"][5:7])
        cells.setdefault(o["loc"], {}).setdefault(o["sci"], {}).setdefault(m, set()).add(o["day_id"])
    visits: dict[int, dict[int, int]] = {}           # сколько дней ты был на месте в этот месяц
    for loc, d in conn.execute("SELECT dl.location_id, d.date FROM day_locations dl JOIN days d ON d.id = dl.day_id"):
        m = int(d[5:7])
        visits.setdefault(loc, {})[m] = visits.get(loc, {}).get(m, 0) + 1
    places = []
    for loc, sp in cells.items():
        places.append({
            "id": loc, "name": locn.get(loc, "?"), "species": len(sp),
            "visits": {str(m): n for m, n in sorted(visits.get(loc, {}).items())},
            "rows": sorted(({"sci": sci, "months": {str(m): len(ds) for m, ds in sorted(ms.items())},
                             "days": len(set().union(*ms.values()))} for sci, ms in sp.items()),
                           key=lambda r: -r["days"])})
    places.sort(key=lambda p: -p["species"])

    return {"total": len(species), "frames": len(obs), "days": len({o["day_id"] for o in obs}),
            "species": sorted(species.values(), key=lambda s: s["first"]["date"], reverse=True),
            "years": sorted(years.values(), key=lambda y: y["year"], reverse=True),
            "groups": groups, "places": places}


def _doy_diff(a: date, b: date) -> int:
    d = abs(a.timetuple().tm_yday - b.timetuple().tm_yday)
    return min(d, 365 - d)


def season_at(conn, loc_ids: list[int], dates: list[date], window: int) -> dict[int, list[dict]]:
    """Для «Выходных»: какие виды ты подтверждал на месте в это время года (±window дней, все годы).
    {loc_id: [{sci, ru, group, days, last, years}]} — по числу дней."""
    want = set(loc_ids)
    acc: dict[int, dict[str, dict]] = {}
    for o in observations(conn):
        if o["loc"] not in want:
            continue
        if not any(_doy_diff(date.fromisoformat(o["date"]), t) <= window for t in dates):
            continue
        s = acc.setdefault(o["loc"], {}).setdefault(o["sci"], {"sci": o["sci"], "ru": o["ru"], "group": o["group"],
                                                               "_days": set(), "last": o["date"]})
        s["_days"].add(o["day_id"])
        s["last"] = max(s["last"], o["date"])
    out = {}
    for loc, sp in acc.items():
        for s in sp.values():
            s["days"] = len(s.pop("_days"))
        out[loc] = sorted(sp.values(), key=lambda s: (-s["days"], s["ru"] or s["sci"]))
    return out
