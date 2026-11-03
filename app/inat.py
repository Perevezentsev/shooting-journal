"""Наблюдения iNaturalist рядом с местами (https://www.inaturalist.org, открытый API v1).

Что берём: виды птиц и млекопитающих в радиусе места за нужные месяцы (все годы)
и число разных наблюдателей — как сигнал «людности». Только исследовательский уровень
(research grade), названия по-русски, где они есть.
Ответы кэшируются в базе на CACHE_DAYS; запросы — не чаще раза в секунду.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

import httpx

API = "https://api.inaturalist.org/v1"
TAXA = "Aves,Mammalia"
RADIUS_KM = 10
CACHE_DAYS = 30
PAUSE = 1.1                      # iNat просит ~1 запрос в секунду
HEADERS = {"User-Agent": "shooting-journal/0.5 (local wildlife photo journal)"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS inat_cache (
    key        TEXT PRIMARY KEY,           -- species|lat|lon|radius|months  или  observers|…
    data       TEXT NOT NULL,              -- JSON
    fetched_at TEXT NOT NULL
);
"""

_last = 0.0
_lock = threading.Lock()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def months_around(dates: list[date], days: int = 14) -> list[int]:
    """Месяцы, которые захватывает окно ±days вокруг дат."""
    ms = set()
    for t in dates:
        for k in (-days, 0, days):
            ms.add((t + timedelta(days=k)).month)
    return sorted(ms)


def _key(kind: str, lat: float, lon: float, radius: int, months: list[int] | None) -> str:
    m = ",".join(map(str, months)) if months else "all"
    return f"{kind}|{lat:.3f}|{lon:.3f}|{radius}|{m}"


def cached(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT data, fetched_at FROM inat_cache WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["fetched_at"]) < datetime.now() - timedelta(days=CACHE_DAYS):
        return None
    return json.loads(row["data"])


def _get(client: httpx.Client, path: str, params: dict) -> dict:
    global _last
    with _lock:
        wait = PAUSE - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = client.get(f"{API}{path}", params=params, timeout=30)
        finally:
            _last = time.time()
    if r.status_code == 429:
        raise RuntimeError("iNaturalist: слишком много запросов, попробуй через несколько минут")
    r.raise_for_status()
    return r.json()


def _base(lat: float, lon: float, radius: int, months: list[int] | None) -> dict:
    p = {"lat": lat, "lng": lon, "radius": radius, "iconic_taxa": TAXA,
         "quality_grade": "research", "locale": "ru"}
    if months:
        p["month"] = ",".join(map(str, months))
    return p


def fetch_place(conn: sqlite3.Connection, client: httpx.Client, lat: float, lon: float,
                radius: int = RADIUS_KM, months: list[int] | None = None) -> None:
    """Виды и наблюдатели для точки; кладёт в кэш."""
    now = datetime.now().isoformat(timespec="seconds")
    k_sp = _key("species", lat, lon, radius, months)
    if cached(conn, k_sp) is None:
        data = _get(client, "/observations/species_counts", {**_base(lat, lon, radius, months), "per_page": 100})
        species = [{
            "id": r["taxon"]["id"],
            "name": r["taxon"].get("preferred_common_name") or r["taxon"]["name"],
            "sci": r["taxon"]["name"],
            "group": r["taxon"].get("iconic_taxon_name"),
            "count": r["count"],
        } for r in data.get("results", []) if r.get("taxon") and r["taxon"].get("rank") in ("species", "subspecies")]
        species.sort(key=lambda x: x["count"], reverse=True)
        payload = {"total_species": data.get("total_results", len(species)), "species": species}
        conn.execute("INSERT OR REPLACE INTO inat_cache VALUES (?, ?, ?)", (k_sp, json.dumps(payload, ensure_ascii=False), now))
    k_ob = _key("observers", lat, lon, radius, months)
    if cached(conn, k_ob) is None:
        obs = _get(client, "/observations/observers", {**_base(lat, lon, radius, months), "per_page": 1})
        cnt = _get(client, "/observations", {**_base(lat, lon, radius, months), "per_page": 0})
        payload = {"observers": obs.get("total_results", 0), "observations": cnt.get("total_results", 0)}
        conn.execute("INSERT OR REPLACE INTO inat_cache VALUES (?, ?, ?)", (k_ob, json.dumps(payload), now))
    conn.commit()


def place_info(conn: sqlite3.Connection, lat: float, lon: float,
               radius: int = RADIUS_KM, months: list[int] | None = None) -> dict | None:
    sp = cached(conn, _key("species", lat, lon, radius, months))
    ob = cached(conn, _key("observers", lat, lon, radius, months))
    if sp is None or ob is None:
        return None
    return {**sp, **ob, "radius_km": radius, "months": months}


class Loader:
    """Фоновая загрузка для списка точек — чтобы страница не ждала."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = {"running": False, "done": 0, "total": 0, "error": None, "finished_at": None}

    def status(self) -> dict:
        with self.lock:
            return dict(self.state)

    def start(self, connect, jobs: list[tuple[float, float, list[int] | None]]) -> dict:
        with self.lock:
            if self.state["running"]:
                return dict(self.state)
            self.state = {"running": True, "done": 0, "total": len(jobs), "error": None, "finished_at": None}
        threading.Thread(target=self._run, args=(connect, jobs), daemon=True).start()
        return self.status()

    def _run(self, connect, jobs) -> None:
        try:
            with connect() as conn, httpx.Client(headers=HEADERS) as client:
                ensure_schema(conn)
                for lat, lon, months in jobs:
                    fetch_place(conn, client, lat, lon, RADIUS_KM, months)
                    with self.lock:
                        self.state["done"] += 1
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.state["error"] = f"{type(e).__name__}: {e}"
        finally:
            with self.lock:
                self.state["running"] = False
                self.state["finished_at"] = datetime.now().isoformat(timespec="seconds")
