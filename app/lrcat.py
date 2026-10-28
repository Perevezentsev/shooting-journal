"""Чтение каталога Lightroom Classic (.lrcat — это SQLite).

Каталог открывается только на чтение. Пока Lightroom запущен, он держит
блокировку, поэтому по умолчанию читаем временную копию файла.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Папка с локацией: дата (в разных записях) + разделитель + название.
#   31.05.2026_Шепелево        6.11.2021 Пруды…          4.11.2021. Большая Ижора
#   15082026_Коккорево         9052022_Шушары_поле       24,05,2024_Невская губа
#   9-10.05.2024_Надпорожье    5-6072023_Пулковские      24_31_072026_Через_горы_к_морю
#   2022-08-21_Название
LOCATION_FOLDER_RE = re.compile(
    r"""^(?:\d{1,2}[-–_])?                      # начало диапазона дней: "9-", "24_"
        (?:\d{1,2}[.,]\d{1,2}[.,]\d{4}          # 12.03.2022, 24,05,2024
          |\d{1,2}_\d{6}                        # 31_072026
          |\d{6,8}                              # 9052022, 15082026
          |\d{4}-\d{2}-\d{2})                   # 2022-08-21
        \.?[ _]+(.+)$""",
    re.X,
)


@dataclass
class Photo:
    image_id: int
    file_name: str
    folder_path: str          # полный путь к папке
    capture_raw: str | None   # время съёмки как в каталоге, с долями секунды
    capture_time: datetime | None
    pick: int                 # 1 — флаг, -1 — отклонён, 0 — нет
    rating: int               # 0..5
    keywords: list[str] = field(default_factory=list)

    @property
    def selected(self) -> bool:
        return self.pick == 1 or self.rating > 0


@dataclass
class ShootDay:
    date: str                 # YYYY-MM-DD, из времени съёмки
    location: str | None      # из названия папки
    location_folder: str | None
    photos: list[Photo] = field(default_factory=list)

    @property
    def first_shot(self) -> datetime | None:
        times = [p.capture_time for p in self.photos if p.capture_time]
        return min(times) if times else None

    @property
    def last_shot(self) -> datetime | None:
        times = [p.capture_time for p in self.photos if p.capture_time]
        return max(times) if times else None

    @property
    def selected_count(self) -> int:
        return sum(1 for p in self.photos if p.selected)


def parse_location(folder_path: str) -> tuple[str | None, str | None]:
    """Ищет в пути папку вида 'дата_Название'. Возвращает (локация, имя папки).
    Берётся самая глубокая подходящая папка."""
    parts = re.split(r"[\\/]+", folder_path.strip("\\/"))
    for part in reversed(parts):
        m = LOCATION_FOLDER_RE.match(part.strip())
        if m:
            name = re.sub(r"[_\s]+", " ", m.group(1)).strip(" .")
            if name:
                return name, part
    return None, None


def parse_capture_time(value: str | None) -> datetime | None:
    """Lightroom хранит время как '2026-05-11T06:42:13', иногда с долями секунды
    или часовым поясом. Берём локальное время камеры без пояса."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:19])
    except ValueError:
        return None


def open_catalog(catalog_path: str | Path, copy: bool = True) -> sqlite3.Connection:
    catalog_path = Path(catalog_path)
    if not catalog_path.exists():
        raise FileNotFoundError(f"Каталог не найден: {catalog_path}")
    if copy:
        tmp_dir = Path(tempfile.mkdtemp(prefix="lrcat_"))
        tmp_path = tmp_dir / catalog_path.name
        shutil.copy2(catalog_path, tmp_path)
        catalog_path = tmp_path
    conn = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


PHOTOS_SQL = """
SELECT
    img.id_local            AS image_id,
    f.baseName || '.' || f.extension AS file_name,
    rf.absolutePath || fo.pathFromRoot AS folder_path,
    img.captureTime         AS capture_time,
    COALESCE(img.pick, 0)   AS pick,
    COALESCE(img.rating, 0) AS rating
FROM Adobe_images img
JOIN AgLibraryFile f         ON f.id_local = img.rootFile
JOIN AgLibraryFolder fo      ON fo.id_local = f.folder
JOIN AgLibraryRootFolder rf  ON rf.id_local = fo.rootFolder
"""

KEYWORDS_SQL = """
SELECT ki.image AS image_id, k.name AS name
FROM AgLibraryKeywordImage ki
JOIN AgLibraryKeyword k ON k.id_local = ki.tag
WHERE k.name IS NOT NULL
"""


def _norm(path: str) -> str:
    return path.replace("\\", "/").lower()


def is_excluded(folder_path: str, exclude: list[str]) -> bool:
    p = _norm(folder_path)
    return any(_norm(x) in p for x in exclude)


def _priority(folder_path: str, root_priority: list[str]) -> int:
    p = _norm(folder_path)
    for i, root in enumerate(root_priority):
        if p.startswith(_norm(root)):
            return i
    return len(root_priority)


def read_photos(
    conn: sqlite3.Connection,
    path_prefix: str | None = None,
    exclude: list[str] | None = None,
    root_priority: list[str] | None = None,
) -> tuple[list[Photo], dict]:
    """Возвращает кадры без исключённых папок и без дублей (копии на разных дисках).
    Дубль = одинаковые имя файла и время съёмки с долями секунды."""
    exclude = exclude or []
    root_priority = root_priority or []

    keywords: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute(KEYWORDS_SQL):
        keywords[row["image_id"]].append(row["name"])

    stats = {"total": 0, "excluded": 0, "duplicates": 0}
    best: dict[tuple, Photo] = {}
    no_time: list[Photo] = []

    for row in conn.execute(PHOTOS_SQL):
        stats["total"] += 1
        folder = row["folder_path"]
        if path_prefix and not _norm(folder).startswith(_norm(path_prefix)):
            stats["excluded"] += 1
            continue
        if is_excluded(folder, exclude):
            stats["excluded"] += 1
            continue
        photo = Photo(
            image_id=row["image_id"],
            file_name=row["file_name"],
            folder_path=folder,
            capture_raw=row["capture_time"],
            capture_time=parse_capture_time(row["capture_time"]),
            pick=int(row["pick"]),
            rating=int(row["rating"]),
            keywords=keywords.get(row["image_id"], []),
        )
        if not photo.capture_raw:
            no_time.append(photo)
            continue
        key = (photo.file_name.lower(), photo.capture_raw)
        prev = best.get(key)
        if prev is None:
            best[key] = photo
            continue
        stats["duplicates"] += 1
        # Оставляем копию с более приоритетного диска, но отбор и ключевые слова
        # берём с любой копии: в Lightroom их могли проставить только на одной.
        keep, other = (photo, prev) if _priority(folder, root_priority) < _priority(prev.folder_path, root_priority) else (prev, photo)
        if other.pick == 1 and keep.pick != -1:
            keep.pick = 1
        keep.rating = max(keep.rating, other.rating)
        keep.keywords = sorted(set(keep.keywords) | set(other.keywords))
        best[key] = keep

    return list(best.values()) + no_time, stats


def group_days(photos: list[Photo]) -> list[ShootDay]:
    """Съёмочный день = дата съёмки + локация из папки."""
    days: dict[tuple[str, str | None], ShootDay] = {}
    for p in photos:
        if p.capture_time is None:
            continue
        location, loc_folder = parse_location(p.folder_path)
        key = (p.capture_time.date().isoformat(), location)
        if key not in days:
            days[key] = ShootDay(date=key[0], location=location, location_folder=loc_folder)
        days[key].photos.append(p)
    return sorted(days.values(), key=lambda d: (d.date, d.location or ""))
