"""Превью Lightroom из «<каталог> Previews.lrdata» — только чтение.

Устройство:
- previews.db (SQLite): ImageCacheEntry(imageId, uuid, digest, orientation).
  imageId = Adobe_images.id_local каталога = photos.image_id журнала.
- Файл превью: <lrdata>/<uuid[0]>/<uuid[:4]>/<uuid>-<digest>.lrprev
- .lrprev — цепочка секций. Каждая начинается с «AgHg», заголовок 32 байта:
  длина заголовка (u16 BE, @4), длина данных (u64 BE, @8), выравнивание (u64 BE, @16),
  имя секции (@24). Секции: header (Lua-таблица с размерами), level_1…level_N (JPEG
  от мелкого к крупному). Кроп Lightroom уже учтён, поворот — нет: его применяем
  сами по ImageCacheEntry.orientation.

Читаем только заголовки секций и нужный JPEG через seek: /mnt/c из WSL медленный.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"AgHg"

# Поворот, который нужен для показа, в градусах по часовой.
# Проверено на кадрах: DA — портрет, повёрнутый на 90° против часовой.
# Зеркальные варианты (BA, CB, DC, AD — десятки кадров) сводим к ближайшему повороту.
ROTATE_CW = {"AB": 0, "BC": 270, "CD": 180, "DA": 90,
             "BA": 0, "CB": 270, "DC": 180, "AD": 90}

LEVEL_SIZE_RE = re.compile(r"height\s*=\s*(\d+),\s*width\s*=\s*(\d+)")


def lrdata_for(catalog_path: str | Path) -> Path:
    """«Lightroom Catalog.lrcat» → «Lightroom Catalog Previews.lrdata» рядом."""
    p = Path(catalog_path)
    return p.with_name(f"{p.stem} Previews.lrdata")


@dataclass
class Preview:
    image_id: int
    jpeg: bytes
    width: int | None
    height: int | None       # размеры как в файле, до поворота
    level: int
    rotate_cw: int           # сколько повернуть по часовой для показа

    @property
    def long_side(self) -> int:
        return max(self.width or 0, self.height or 0)

    def image(self, scale: float = 1.0):
        """PIL.Image, уже повёрнутое. Pillow нужен только здесь.
        scale < 1 — JPEG декодируется сразу уменьшенным (1/2, 1/4, 1/8, но не мельче
        запрошенного): так полный уровень 8256 px читается в разы быстрее."""
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(self.jpeg))
        if scale < 1:
            img.draft("RGB", (max(1, int(img.width * scale)), max(1, int(img.height * scale))))
        img.load()
        if self.rotate_cw:
            # PIL.rotate крутит против часовой
            img = img.rotate(-self.rotate_cw, expand=True)
        return img.convert("RGB")


def _sections(f) -> list[tuple[str, int, int]]:
    """[(имя, смещение данных, длина данных), ...] без чтения самих данных."""
    out, pos = [], 0
    while True:
        f.seek(pos)
        h = f.read(32)
        if len(h) < 32 or h[:4] != MAGIC:
            break
        hl, = struct.unpack(">H", h[4:6])
        dl, pl = struct.unpack(">QQ", h[8:24])
        name = h[24:hl].split(b"\0", 1)[0].decode("ascii", "replace")
        out.append((name, pos + hl, dl))
        pos += hl + dl + pl
    return out


class PreviewStore:
    """Индекс imageId → файл превью. Берёт копию previews.db, как sync берёт копию каталога."""

    def __init__(self, lrdata: str | Path):
        self.root = Path(lrdata)
        db = self.root / "previews.db"
        if not db.exists():
            raise FileNotFoundError(f"Нет превью Lightroom: {db}")
        tmp = Path(tempfile.mkdtemp(prefix="lrprev_")) / "previews.db"
        shutil.copy2(db, tmp)
        for ext in ("-wal",):
            side = db.with_name(db.name + ext)
            if side.exists():
                shutil.copy2(side, tmp.with_name(tmp.name + ext))
        conn = sqlite3.connect(tmp)
        self.entries: dict[int, tuple[str, str, str]] = {
            int(r[0]): (r[1], r[2], r[3] or "AB")
            for r in conn.execute("SELECT imageId, uuid, digest, orientation FROM ImageCacheEntry")
        }
        conn.close()
        shutil.rmtree(tmp.parent, ignore_errors=True)

    def __contains__(self, image_id: int) -> bool:
        return image_id in self.entries

    def path(self, image_id: int) -> Path | None:
        e = self.entries.get(image_id)
        if not e:
            return None
        uuid, digest, _ = e
        return self.root / uuid[0] / uuid[:4] / f"{uuid}-{digest}.lrprev"

    def read(self, image_id: int, max_long: int | None = None) -> Preview | None:
        """Самый крупный уровень (или самый крупный не больше max_long — для миниатюр).
        None — превью нет или файл повреждён."""
        p = self.path(image_id)
        if p is None or not p.exists():
            return None
        try:
            with open(p, "rb") as f:
                secs = _sections(f)
                sizes: list[tuple[int, int]] = []
                head = next((s for s in secs if s[0] == "header"), None)
                if head:
                    f.seek(head[1])
                    text = f.read(head[2]).decode("utf-8", "replace")
                    sizes = [(int(w), int(h)) for h, w in LEVEL_SIZE_RE.findall(text)]
                levels = []
                for name, off, ln in secs:
                    m = re.fullmatch(r"level_(\d+)", name)
                    if m:
                        n = int(m.group(1))
                        w, h = sizes[n - 1] if n - 1 < len(sizes) else (None, None)
                        levels.append((n, off, ln, w, h))
                if not levels:
                    return None
                levels.sort()
                pick = levels[-1]
                if max_long:
                    fit = [lv for lv in levels if lv[3] and max(lv[3], lv[4]) <= max_long]
                    if fit:
                        pick = fit[-1]
                n, off, ln, w, h = pick
                f.seek(off)
                data = f.read(ln)
        except OSError:
            return None
        if not data.startswith(b"\xff\xd8"):
            return None
        return Preview(image_id, data, w, h, n, ROTATE_CW.get(self.entries[image_id][2], 0))
