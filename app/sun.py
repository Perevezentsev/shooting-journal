"""Положение солнца без внешних сервисов (упрощённый алгоритм NOAA, точность ~1°).

Азимут — от севера по часовой стрелке, как у направления берега.
Время — местное «настенное» для часового пояса с данным смещением от UTC.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta


def _solar(dt_utc: datetime) -> tuple[float, float]:
    """Склонение солнца и уравнение времени (минуты) на момент UTC."""
    n = dt_utc.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1 + (dt_utc.hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    return decl, eqtime


def position(lat: float, lon: float, local: datetime, utc_offset_h: float = 3) -> tuple[float, float]:
    """Азимут и высота солнца (градусы) в момент local (местное время)."""
    dt_utc = local - timedelta(hours=utc_offset_h)
    decl, eqtime = _solar(dt_utc)
    minutes = dt_utc.hour * 60 + dt_utc.minute + dt_utc.second / 60
    tst = minutes + eqtime + 4 * lon                       # истинное солнечное время, мин
    ha = math.radians(tst / 4 - 180)                       # часовой угол
    phi = math.radians(lat)
    cos_zen = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    zen = math.acos(max(-1, min(1, cos_zen)))
    elev = 90 - math.degrees(zen)
    az = math.degrees(math.atan2(math.sin(ha), math.cos(ha) * math.sin(phi) - math.tan(decl) * math.cos(phi))) + 180
    return az % 360, elev


def rise_set_azimuth(lat: float, lon: float, day: datetime) -> tuple[float | None, float | None]:
    """Азимуты восхода и заката (с поправкой на рефракцию, -0.833°). None — полярный день/ночь."""
    decl, _ = _solar(datetime(day.year, day.month, day.day, 12))
    phi = math.radians(lat)
    h0 = math.radians(-0.833)
    cos_ha = (math.sin(h0) - math.sin(phi) * math.sin(decl)) / (math.cos(phi) * math.cos(decl))
    if not -1 <= cos_ha <= 1:
        return None, None
    ha = math.acos(cos_ha)
    # азимут восхода: из сферического треугольника при часовом угле -ha
    def az_at(h):
        return (math.degrees(math.atan2(math.sin(h), math.cos(h) * math.sin(phi) - math.tan(decl) * math.cos(phi))) + 180) % 360
    return az_at(-ha), az_at(ha)


def light_relation(sun_az: float, facing: int) -> str:
    """Как падает свет, если стоишь на берегу лицом к воде (facing)."""
    diff = abs((sun_az - facing + 180) % 360 - 180)
    if diff <= 50:
        return "солнце над водой — контровой свет"
    if diff >= 130:
        return "солнце за спиной — фронтальный свет"
    return "солнце сбоку — боковой свет"


COMPASS16 = ["С", "ССВ", "СВ", "ВСВ", "В", "ВЮВ", "ЮВ", "ЮЮВ", "Ю", "ЮЮЗ", "ЮЗ", "ЗЮЗ", "З", "ЗСЗ", "СЗ", "ССЗ"]


def _compass(d: float) -> str:
    return COMPASS16[int((d % 360) / 22.5 + 0.5) % 16]


def describe(lat: float, lon: float, day, h1: int, h2: int, facing: int | None) -> dict:
    """Солнце для дня съёмки: азимуты восхода/заката и свет в середине окна."""
    d0 = datetime(day.year, day.month, day.day)
    sr, ss = rise_set_azimuth(lat, lon, d0)
    mid = d0 + timedelta(hours=(h1 + h2 + 1) / 2)
    az, el = position(lat, lon, mid)
    out = {
        "sunrise_az": round(sr) if sr is not None else None,
        "sunset_az": round(ss) if ss is not None else None,
        "sunrise_az_txt": _compass(sr) if sr is not None else None,
        "sunset_az_txt": _compass(ss) if ss is not None else None,
        "sun_mid_time": mid.strftime("%H:%M"),
        "sun_mid_az": round(az), "sun_mid_az_txt": _compass(az), "sun_mid_elev": round(el),
    }
    if facing is not None:
        out["light"] = light_relation(az, facing) if el > 0 else "солнце под горизонтом"
    return out
