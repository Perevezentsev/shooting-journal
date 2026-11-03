"""Планирование выходных: прогноз по местам + своя история в этот сезон.

Логика простая и объяснимая:
- места — те, где ты бывал в эти недели прошлых лет (±SEASON_DAYS дней от той же даты);
- прогноз считается на твоё обычное время съёмки на этом месте;
- «похоже на» — твой удачный день на этом месте с самой похожей погодой.
Прогноз: Open-Meteo Forecast API (https://open-meteo.com, CC BY 4.0).
"""

from __future__ import annotations

import sqlite3
import statistics
import time
from datetime import date, datetime, timedelta

import httpx

from . import sun as sunmod
from . import weather as wx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
SEASON_DAYS = 14          # окно сезона: ±2 недели от той же даты
GOOD_SEASON_DAYS = 45     # удачные дни для «похоже на» ищем в пределах ±45 дней
BATCH = 25                # точек в одном запросе прогноза
CACHE_TTL = 3 * 3600      # прогноз живёт 3 часа
MAX_AHEAD = 10            # насколько вперёд разрешаем планировать

_cache: dict[tuple, dict] = {}     # (lat_key, lon_key) → {"at": ts, "hourly": {ts: row}, "sun": {date: (sr, ss)}}


# --- прогноз -------------------------------------------------------------------

def _parse(item: dict) -> dict:
    h = item.get("hourly") or {}
    times = h.get("time") or []
    rows = {}
    names = {"temperature_2m": "temp", "relative_humidity_2m": "rh", "precipitation": "precip",
             "snowfall": "snowfall", "snow_depth": "snow_depth", "cloud_cover": "cloud",
             "wind_speed_10m": "wind", "wind_direction_10m": "wind_dir", "wind_gusts_10m": "gust",
             "pressure_msl": "pressure", "weather_code": "code"}
    for i, t in enumerate(times):
        rows[t] = {short: (h.get(long) or [None] * len(times))[i] for long, short in names.items()}
        rows[t]["ts"] = t
    d = item.get("daily") or {}
    sun = {t: (sr, ss) for t, sr, ss in zip(d.get("time") or [], d.get("sunrise") or [], d.get("sunset") or [])}
    return {"at": time.time(), "hourly": rows, "sun": sun}


def fetch_forecast(points: list[tuple[float, float]], client: httpx.Client | None = None) -> list[dict]:
    """Прогноз для нескольких точек одним запросом (Open-Meteo принимает списки координат)."""
    params = {
        "latitude": ",".join(str(p[0]) for p in points),
        "longitude": ",".join(str(p[1]) for p in points),
        "hourly": ",".join(wx.HOURLY), "daily": "sunrise,sunset",
        "timezone": "auto", "wind_speed_unit": "ms",
        "past_days": wx.DAYS_BEFORE, "forecast_days": MAX_AHEAD + 2,
    }
    get = client.get if client else httpx.get
    r = get(FORECAST_URL, params=params, timeout=60)
    if r.status_code == 429:
        raise RuntimeError("Open-Meteo: превышен лимит запросов, попробуй позже")
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else [data]


def ensure_forecast(points: list[tuple[float, float]], force: bool = False) -> None:
    need = [p for p in dict.fromkeys(points)
            if force or p not in _cache or time.time() - _cache[p]["at"] > CACHE_TTL]
    if not need:
        return
    with httpx.Client(headers={"User-Agent": "shooting-journal/0.4"}) as client:
        for i in range(0, len(need), BATCH):
            chunk = need[i:i + BATCH]
            for p, item in zip(chunk, fetch_forecast(chunk, client)):
                _cache[p] = _parse(item)
            time.sleep(0.2)


# --- сводка прогноза на окно съёмки ------------------------------------------

def _summary(k: tuple, day: date, h1: int, h2: int, facing: int | None) -> dict | None:
    c = _cache.get(k)
    if not c:
        return None
    rows = c["hourly"]
    window = [rows[t] for t in (f"{day.isoformat()}T{h:02d}:00" for h in range(h1, h2 + 1)) if t in rows]
    if not window:
        return None
    start = datetime(day.year, day.month, day.day, h1)
    before = [r for t, r in rows.items()
              if start - timedelta(days=wx.DAYS_BEFORE) <= datetime.fromisoformat(t) < start]
    wind, wind_from = wx._vec_mean(window)
    temps = [r["temp"] for r in window if r["temp"] is not None]
    codes = [r["code"] for r in window if r["code"] is not None]
    sun = c["sun"].get(day.isoformat())
    s = {
        "temp": wx._avg(window, "temp"),
        "temp_min": min(temps) if temps else None, "temp_max": max(temps) if temps else None,
        "cloud": wx._avg(window, "cloud"),
        "precip": sum(r["precip"] or 0 for r in window),
        "snowfall": sum(r["snowfall"] or 0 for r in window),
        "wind": wind, "wind_from": wind_from, "wind_from_txt": wx.compass(wind_from),
        "gust": max((r["gust"] for r in window if r["gust"] is not None), default=None),
        "sky": wx.WMO.get(max(set(codes), key=codes.count)) if codes else None,
        "sunrise": sun[0][11:16] if sun and sun[0] else None,
        "sunset": sun[1][11:16] if sun and sun[1] else None,
        "window": f"{h1:02d}:00–{h2 + 1:02d}:00",
    }
    w3, w3_from = wx._vec_mean(before)
    s["wind3d"], s["wind3d_from_txt"] = w3, wx.compass(w3_from)
    s.update(sunmod.describe(k[0], k[1], day, h1, h2, facing))
    if facing is not None:
        s["facing"] = facing
        if wind_from is not None:
            s["shore_wind"] = wx._relation(wind_from, facing)
        onshore = wx._onshore(before, facing)
        if onshore is not None:
            s["onshore3d"] = onshore
            s["water"] = ("вода у этого берега скорее выше" if onshore > 2 else
                          "вода у этого берега скорее ниже" if onshore < -2 else "без явного нагона или сгона")
    flags = []
    if s["precip"] >= 2:
        flags.append(f"осадки {s['precip']:.1f} мм")
    if (s["gust"] or 0) >= 15:
        flags.append(f"порывы до {s['gust']:.0f} м/с")
    s["flags"] = flags
    return s


# --- похожесть погоды ----------------------------------------------------------

def _distance(a: dict, b: dict) -> float:
    """0 — одинаковая погода, ~3.5 — совсем разная. Только то, что важно для съёмки."""
    d = 0.0
    if a.get("wind_from") is not None and b.get("wind_from") is not None and min(a["wind"] or 0, b["wind"] or 0) >= 2:
        d += abs((a["wind_from"] - b["wind_from"] + 180) % 360 - 180) / 180          # направление ветра
    if a.get("wind") is not None and b.get("wind") is not None:
        d += min(abs(a["wind"] - b["wind"]) / 6, 1) * 0.7                             # сила ветра
    if a.get("cloud") is not None and b.get("cloud") is not None:
        d += abs(a["cloud"] - b["cloud"]) / 100 * 0.6                                 # облачность
    if (a.get("precip", 0) >= 1) != (b.get("precip", 0) >= 1):
        d += 0.5                                                                     # дождь или нет
    if a.get("onshore3d") is not None and b.get("onshore3d") is not None:
        if (a["onshore3d"] > 2) != (b["onshore3d"] > 2) or (a["onshore3d"] < -2) != (b["onshore3d"] < -2):
            d += 0.7                                                                 # нагон/сгон у берега
    return d


def _doy_diff(a: date, b: date) -> int:
    x = abs(a.timetuple().tm_yday - b.timetuple().tm_yday)
    return min(x, 365 - x)


# --- план ------------------------------------------------------------------------

def default_dates(today: date | None = None) -> list[date]:
    today = today or date.today()
    sat = today + timedelta(days=(5 - today.weekday()) % 7)
    if today.weekday() == 6:              # воскресенье — сегодня и следующая суббота
        return [today, today + timedelta(days=6)]
    return [sat, sat + timedelta(days=1)]


def build(conn: sqlite3.Connection, dates: list[date], all_places: bool = False, force: bool = False) -> dict:
    wx.ensure_schema(conn)
    locs = {r["id"]: dict(r) for r in conn.execute(
        "SELECT * FROM locations WHERE kind = 'place' AND lat IS NOT NULL")}
    defaults = {r["location_id"]: dict(r) for r in conn.execute("SELECT * FROM spots WHERE is_default = 1")}
    past = wx.summaries(conn)

    # история по местам (только дни с одним местом — так погода и место точно совпадают)
    hist: dict[int, list[dict]] = {}
    for r in conn.execute("""
        SELECT d.*, dl.location_id AS place_id FROM days d JOIN day_locations dl ON dl.day_id = d.id
        WHERE d.id IN (SELECT day_id FROM day_locations GROUP BY day_id HAVING COUNT(*) = 1)
    """):
        if r["place_id"] in locs:
            hist.setdefault(r["place_id"], []).append(dict(r))

    # какие места показываем
    chosen = []
    for loc_id, loc in locs.items():
        days = hist.get(loc_id, [])
        season = [d for d in days if any(_doy_diff(date.fromisoformat(d["date"]), t) <= SEASON_DAYS for t in dates)]
        if season or (all_places and days):
            chosen.append((loc, days, season))

    def point(loc):
        sp = defaults.get(loc["id"])
        return (sp["lat"], sp["lon"], sp["facing"], sp["name"]) if sp else (loc["lat"], loc["lon"], None, None)

    ensure_forecast([wx.key(*point(loc)[:2]) for loc, _, _ in chosen], force=force)

    places = []
    for loc, days, season in chosen:
        lat, lon, facing, spot_name = point(loc)
        k = wx.key(lat, lon)
        def shoot_window(target: date) -> tuple[int, int, str | None]:
            """Обычное время съёмки на этом месте — по выездам в близкий сезон,
            с проверкой, что окно приходится на светлое время."""
            near = [d for d in days if d["first_shot"]
                    and _doy_diff(date.fromisoformat(d["date"]), target) <= GOOD_SEASON_DAYS]
            pool = near or [d for d in days if d["first_shot"]]
            h1 = int(statistics.median(int(d["first_shot"][11:13]) for d in pool)) if pool else 7
            lasts = [int(d["last_shot"][11:13]) for d in pool if d["last_shot"]]
            h2 = max(int(statistics.median(lasts)) if lasts else h1 + 3, h1)
            sun = (_cache.get(k) or {}).get("sun", {}).get(target.isoformat())
            if not sun or not sun[0] or not sun[1]:
                return h1, h2, None
            sr, ss = int(sun[0][11:13]), int(sun[1][11:13])
            if h1 >= ss or h2 < sr:                    # всё окно в темноте — берём утро
                note = f"обычно здесь снимаешь в {h1:02d}–{h2 + 1:02d}, но в это время темно — взято утро"
                return sr, min(sr + 3, max(ss - 1, sr)), note
            return max(h1, sr), min(h2, ss), None

        # удачные дни на этом месте — с известной погодой, лучше в близкий сезон
        with_wx = [d for d in days if d["id"] in past and d["photo_count"] >= 20]

        def good_days(target: date) -> list[dict]:
            near = [d for d in with_wx if _doy_diff(date.fromisoformat(d["date"]), target) <= GOOD_SEASON_DAYS]
            pool = near if len(near) >= 2 else with_wx
            pool = sorted(pool, key=lambda d: d["selected_count"], reverse=True)
            return pool[:max(3, round(len(pool) * .3))] if len(pool) >= 2 else []

        ph = sum(d["photo_count"] for d in season)
        sel = sum(d["selected_count"] for d in season)
        notes = [{"date": d["date"], "day_id": d["id"], "text": d["notes"]}
                 for d in sorted(season, key=lambda d: d["date"], reverse=True) if (d["notes"] or "").strip()][:2]
        per_date = {}
        for t in dates:
            h1, h2, wnote = shoot_window(t)
            f = _summary(k, t, h1, h2, facing)
            if f and wnote:
                f["window_note"] = wnote
            good = good_days(t)
            like = None
            if f and good:
                best = min(good, key=lambda d: _distance(f, past[d["id"]]))
                dist = _distance(f, past[best["id"]])
                w = past[best["id"]]
                like = {"date": best["date"], "day_id": best["id"], "same_season": _doy_diff(date.fromisoformat(best["date"]), t) <= GOOD_SEASON_DAYS, "selected": best["selected_count"], "photos": best["photo_count"],
                        "similarity": max(0, round(100 * (1 - dist / 3.5))),
                        "wind_from_txt": w.get("wind_from_txt"), "wind": w.get("wind"),
                        "cloud": w.get("cloud"), "sky": w.get("sky"), "water": w.get("water")}
            per_date[t.isoformat()] = {"forecast": f, "like": like}
        places.append({
            "id": loc["id"], "name": loc["name"], "lat": lat, "lon": lon, "spot": spot_name,
            "season_visits": len(season),
            "season_list": [{"date": d["date"], "day_id": d["id"], "selected": d["selected_count"]}
                            for d in sorted(season, key=lambda d: d["date"], reverse=True)],
            "season_years": sorted({d["date"][:4] for d in season}),
            "season_rate": round(sel / ph * 100, 1) if ph else None,
            "total_visits": len(days),
            "notes": notes,
            "per_date": per_date,
        })

    # Сортировка: опыт в этот сезон весит наравне с похожестью погоды.
    # Похожесть на день из другого сезона — вполовину: птицы и свет тогда другие.
    for p in places:
        likes = [v["like"] for v in p["per_date"].values() if v["like"]]
        sim = max((l["similarity"] * (1 if l["same_season"] else .5) for l in likes), default=0)
        n = p["season_visits"]
        rate = p["season_rate"] or 0
        rate_pts = min(rate, 15) * (1 if n >= 2 else .5)        # отбор по одному выезду — слабый сигнал
        p["score"] = round(.6 * sim + 25 * min(n, 4) / 4 + rate_pts)
        why = []
        if n:
            why.append(f"{n} {'выезд' if n % 10 == 1 and n % 100 != 11 else 'выезда' if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else 'выездов'} в эти недели")
        if any(l["same_season"] and l["similarity"] >= 80 for l in likes):
            why.append("погода похожа на твой удачный день в этот же сезон")
        elif likes and not any(l["same_season"] for l in likes):
            why.append("удачных дней в этот сезон здесь мало — сравнение с другим сезоном")
        if rate >= 8 and n >= 2:
            why.append(f"высокий отбор в сезон ({rate}%)")
        p["why"] = why
    places.sort(key=lambda p: (p["score"], p["total_visits"]), reverse=True)
    return {"dates": [t.isoformat() for t in dates], "places": places,
            "season_days": SEASON_DAYS, "generated_at": datetime.now().isoformat(timespec="minutes")}
