"""Погода для съёмочных дней: архив Open-Meteo (https://open-meteo.com, CC BY 4.0).

Почасовые данные кэшируются в базе по точке (координаты, округлённые до 0,01°).
Сводка дня считается на лету из кэша, поэтому смена точки дня сразу меняет погоду,
если для новой точки данные уже загружены.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

import httpx

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = ["temperature_2m", "relative_humidity_2m", "precipitation", "snowfall", "snow_depth",
          "cloud_cover", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
          "pressure_msl", "weather_code"]
DAYS_BEFORE = 3          # сколько дней до выезда нужно для «ветра за 3 дня»
ARCHIVE_LAG_DAYS = 6     # архив отстаёт от сегодня примерно на 5 дней
MERGE_GAP_DAYS = 45      # близкие по датам дни одной точки грузим одним запросом

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_hourly (
    lat_key REAL NOT NULL, lon_key REAL NOT NULL, ts TEXT NOT NULL,   -- местное время, 'YYYY-MM-DDTHH:00'
    temp REAL, rh REAL, precip REAL, snowfall REAL, snow_depth REAL, cloud REAL,
    wind REAL, wind_dir REAL, gust REAL, pressure REAL, code INTEGER,
    PRIMARY KEY (lat_key, lon_key, ts)
);
CREATE TABLE IF NOT EXISTS weather_daily (
    lat_key REAL NOT NULL, lon_key REAL NOT NULL, date TEXT NOT NULL,
    sunrise TEXT, sunset TEXT,
    PRIMARY KEY (lat_key, lon_key, date)
);
-- Какие даты по точке уже запрошены (в том числе пустые ответы), чтобы не просить повторно
CREATE TABLE IF NOT EXISTS weather_fetched (
    lat_key REAL NOT NULL, lon_key REAL NOT NULL, date TEXT NOT NULL,
    PRIMARY KEY (lat_key, lon_key, date)
);
"""

# Коды погоды WMO → коротко по-русски
WMO = {
    0: "ясно", 1: "малооблачно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь, туман",
    51: "морось", 53: "морось", 55: "сильная морось", 56: "ледяная морось", 57: "ледяная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь", 66: "ледяной дождь", 67: "ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежные зёрна",
    80: "ливень", 81: "ливень", 82: "сильный ливень", 85: "снегопад", 86: "сильный снегопад",
    95: "гроза", 96: "гроза с градом", 99: "гроза с градом",
}
COMPASS = ["С", "ССВ", "СВ", "ВСВ", "В", "ВЮВ", "ЮВ", "ЮЮВ", "Ю", "ЮЮЗ", "ЮЗ", "ЗЮЗ", "З", "ЗСЗ", "СЗ", "ССЗ"]


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def key(lat: float, lon: float) -> tuple[float, float]:
    return round(lat, 2), round(lon, 2)


def compass(deg: float | None) -> str:
    if deg is None:
        return ""
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


# --- какая точка у дня -------------------------------------------------------

def day_points(conn: sqlite3.Connection) -> dict[int, dict]:
    """Для каждого дня — точка, по которой берём погоду: явная точка дня,
    иначе точка места по умолчанию, иначе само место (первое с координатами)."""
    spots = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM spots")}
    defaults = {s["location_id"]: s for s in spots.values() if s["is_default"]}
    locs: dict[int, list] = {}
    for r in conn.execute("""SELECT dl.day_id, l.id, l.lat, l.lon, l.kind FROM day_locations dl
                             JOIN locations l ON l.id = dl.location_id ORDER BY l.name"""):
        locs.setdefault(r["day_id"], []).append(dict(r))
    out = {}
    for d in conn.execute("SELECT id, spot_id FROM days"):
        dl = locs.get(d["id"], [])
        spot = spots.get(d["spot_id"]) or (defaults.get(dl[0]["id"]) if len(dl) == 1 else None)
        if spot:
            out[d["id"]] = {"lat": spot["lat"], "lon": spot["lon"], "facing": spot["facing"], "source": "точка"}
            continue
        for l in dl:
            if l["lat"] is not None:
                out[d["id"]] = {"lat": l["lat"], "lon": l["lon"], "facing": None, "source": "место"}
                break
    return out


# --- загрузка ----------------------------------------------------------------

def _ranges(dates: list[date]) -> list[tuple[date, date]]:
    """Даты съёмок одной точки → диапазоны для запросов (с запасом на 3 дня до)."""
    out: list[list[date]] = []
    for d in sorted(set(dates)):
        start = d - timedelta(days=DAYS_BEFORE)
        if out and (start - out[-1][1]).days <= MERGE_GAP_DAYS:
            out[-1][1] = max(out[-1][1], d)
        else:
            out.append([start, d])
    return [(a, b) for a, b in out]


def plan(conn: sqlite3.Connection) -> list[dict]:
    """Что нужно загрузить: по точкам — диапазоны дат, которых ещё нет в кэше."""
    ensure_schema(conn)
    points = day_points(conn)
    latest = date.today() - timedelta(days=ARCHIVE_LAG_DAYS)
    by_key: dict[tuple, list[date]] = {}
    for d in conn.execute("SELECT id, date FROM days"):
        p = points.get(d["id"])
        day = date.fromisoformat(d["date"])
        if not p or day > latest:
            continue
        k = key(p["lat"], p["lon"])
        have = conn.execute("SELECT COUNT(*) FROM weather_fetched WHERE lat_key=? AND lon_key=? AND date BETWEEN ? AND ?",
                            (*k, (day - timedelta(days=DAYS_BEFORE)).isoformat(), day.isoformat())).fetchone()[0]
        if have < DAYS_BEFORE + 1:
            by_key.setdefault(k, []).append(day)
    jobs = []
    for k, dates in by_key.items():
        for a, b in _ranges(dates):
            jobs.append({"lat": k[0], "lon": k[1], "start": a, "end": min(b, latest)})
    return jobs


def fetch_range(lat: float, lon: float, start: date, end: date, client: httpx.Client | None = None) -> dict:
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": ",".join(HOURLY), "daily": "sunrise,sunset",
        "timezone": "auto", "wind_speed_unit": "ms",
    }
    get = client.get if client else httpx.get
    r = get(ARCHIVE_URL, params=params, timeout=60)
    if r.status_code == 429:
        raise RuntimeError("Open-Meteo: превышен лимит запросов, попробуй позже")
    r.raise_for_status()
    return r.json()


def store(conn: sqlite3.Connection, lat: float, lon: float, start: date, end: date, data: dict) -> int:
    k = key(lat, lon)
    h = data.get("hourly") or {}
    times = h.get("time") or []
    col = lambda name: h.get(name) or [None] * len(times)  # noqa: E731
    rows = list(zip(times, *(col(n) for n in HOURLY)))
    conn.executemany(f"""INSERT OR REPLACE INTO weather_hourly
        (lat_key, lon_key, ts, temp, rh, precip, snowfall, snow_depth, cloud, wind, wind_dir, gust, pressure, code)
        VALUES (?, ?, {", ".join("?" * (len(HOURLY) + 1))})""", [(*k, *r) for r in rows])
    dd = data.get("daily") or {}
    conn.executemany("INSERT OR REPLACE INTO weather_daily VALUES (?, ?, ?, ?, ?)",
                     [(*k, t, sr, ss) for t, sr, ss in zip(dd.get("time") or [], dd.get("sunrise") or [], dd.get("sunset") or [])])
    n = (end - start).days + 1
    conn.executemany("INSERT OR IGNORE INTO weather_fetched VALUES (?, ?, ?)",
                     [(*k, (start + timedelta(days=i)).isoformat()) for i in range(n)])
    return len(rows)


class Loader:
    """Загрузка в фоне, чтобы страница не ждала. Прогресс — через status()."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = {"running": False, "done": 0, "total": 0, "error": None, "finished_at": None}

    def status(self) -> dict:
        with self.lock:
            return dict(self.state)

    def start(self, connect) -> dict:
        with self.lock:
            if self.state["running"]:
                return dict(self.state)
            self.state = {"running": True, "done": 0, "total": 0, "error": None, "finished_at": None}
        threading.Thread(target=self._run, args=(connect,), daemon=True).start()
        return self.status()

    def _run(self, connect) -> None:
        try:
            with connect() as conn:
                jobs = plan(conn)
                with self.lock:
                    self.state["total"] = len(jobs)
                with httpx.Client(headers={"User-Agent": "shooting-journal/0.3"}) as client:
                    for job in jobs:
                        data = fetch_range(job["lat"], job["lon"], job["start"], job["end"], client)
                        store(conn, job["lat"], job["lon"], job["start"], job["end"], data)
                        conn.commit()
                        with self.lock:
                            self.state["done"] += 1
                        time.sleep(0.3)   # бережно к бесплатному API
        except Exception as e:  # noqa: BLE001 — ошибку показываем на странице
            with self.lock:
                self.state["error"] = f"{type(e).__name__}: {e}"
        finally:
            with self.lock:
                self.state["running"] = False
                self.state["finished_at"] = datetime.now().isoformat(timespec="seconds")


# --- сводка дня ----------------------------------------------------------------

def _vec_mean(rows: list[dict]) -> tuple[float | None, float | None]:
    """Средний ветер по вектору: (скорость, откуда дует, градусы)."""
    u = v = 0.0
    n = 0
    for r in rows:
        if r["wind"] is None or r["wind_dir"] is None:
            continue
        a = math.radians(r["wind_dir"])
        u += r["wind"] * math.sin(a)
        v += r["wind"] * math.cos(a)
        n += 1
    if not n:
        return None, None
    u, v = u / n, v / n
    return math.hypot(u, v), (math.degrees(math.atan2(u, v)) + 360) % 360


def _onshore(rows: list[dict], facing: int) -> float | None:
    """Средняя составляющая ветра «с воды на берег», м/с. Плюс — дует с воды."""
    vals = [r["wind"] * math.cos(math.radians(r["wind_dir"] - facing))
            for r in rows if r["wind"] is not None and r["wind_dir"] is not None]
    return sum(vals) / len(vals) if vals else None


def _relation(wind_from: float, facing: int) -> str:
    diff = abs((wind_from - facing + 180) % 360 - 180)   # 0 — дует с воды прямо на берег
    if diff <= 45:
        return "в лицо (с воды)"
    if diff >= 135:
        return "в спину (с берега)"
    return "вдоль берега"


def _avg(rows, f):
    vals = [r[f] for r in rows if r[f] is not None]
    return sum(vals) / len(vals) if vals else None


def summaries(conn: sqlite3.Connection) -> dict[int, dict]:
    """Сводка погоды по каждому дню, для которого есть данные в кэше."""
    ensure_schema(conn)
    points = day_points(conn)
    out = {}
    for d in conn.execute("SELECT id, date, first_shot, last_shot FROM days"):
        p = points.get(d["id"])
        if not p:
            continue
        k = key(p["lat"], p["lon"])
        day = date.fromisoformat(d["date"])
        h1 = int(d["first_shot"][11:13]) if d["first_shot"] else 6
        h2 = int(d["last_shot"][11:13]) if d["last_shot"] else 12
        window = [dict(r) for r in conn.execute(
            "SELECT * FROM weather_hourly WHERE lat_key=? AND lon_key=? AND ts BETWEEN ? AND ?",
            (*k, f"{d['date']}T{h1:02d}:00", f"{d['date']}T{h2:02d}:00"))]
        if not window:
            continue
        before = [dict(r) for r in conn.execute(
            "SELECT * FROM weather_hourly WHERE lat_key=? AND lon_key=? AND ts >= ? AND ts < ?",
            (*k, f"{(day - timedelta(days=DAYS_BEFORE)).isoformat()}T00:00", f"{d['date']}T{h1:02d}:00"))]
        sun = conn.execute("SELECT sunrise, sunset FROM weather_daily WHERE lat_key=? AND lon_key=? AND date=?",
                           (*k, d["date"])).fetchone()

        wind, wind_from = _vec_mean(window)
        temps = [r["temp"] for r in window if r["temp"] is not None]
        codes = [r["code"] for r in window if r["code"] is not None]
        s = {
            "source": p["source"],
            "temp": _avg(window, "temp"),
            "temp_min": min(temps) if temps else None,
            "temp_max": max(temps) if temps else None,
            "cloud": _avg(window, "cloud"),
            "precip": sum(r["precip"] or 0 for r in window),
            "snowfall": sum(r["snowfall"] or 0 for r in window),
            "snow_depth": window[0]["snow_depth"],
            "wind": wind,
            "wind_from": wind_from,
            "wind_from_txt": compass(wind_from),
            "gust": max((r["gust"] for r in window if r["gust"] is not None), default=None),
            "pressure": _avg(window, "pressure"),
            "sky": WMO.get(max(set(codes), key=codes.count)) if codes else None,
            "sunrise": sun["sunrise"][11:16] if sun and sun["sunrise"] else None,
            "sunset": sun["sunset"][11:16] if sun and sun["sunset"] else None,
        }
        if sun and sun["sunrise"] and d["first_shot"]:
            s["after_sunrise_min"] = round((datetime.fromisoformat(d["first_shot"][:16])
                                            - datetime.fromisoformat(sun["sunrise"][:16])).total_seconds() / 60)
        w3, w3_from = _vec_mean(before)
        s["wind3d"], s["wind3d_from_txt"] = w3, compass(w3_from)
        if p["facing"] is not None:
            s["facing"] = p["facing"]
            if wind_from is not None:
                s["shore_wind"] = _relation(wind_from, p["facing"])
            onshore = _onshore(before, p["facing"])
            if onshore is not None:
                s["onshore3d"] = onshore
                s["water"] = ("вода у этого берега скорее выше" if onshore > 2 else
                              "вода у этого берега скорее ниже" if onshore < -2 else "без явного нагона или сгона")
        out[d["id"]] = s
    return out
