"""Проверка чтения каталога: выводит найденные съёмочные дни.

Берёт путь к каталогу и фильтры из config.json:
    python3 scripts/scan_catalog.py
    python3 scripts/scan_catalog.py --no-loc        + папки без распознанной локации
    python3 scripts/scan_catalog.py --raw           без фильтров и без удаления дублей
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.lrcat import group_days, open_catalog, parse_location, read_photos  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=None)
    ap.add_argument("--no-loc", action="store_true")
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    conn = open_catalog(args.catalog or cfg["catalog_path"])
    if args.raw:
        photos, stats = read_photos(conn)
    else:
        photos, stats = read_photos(conn, cfg.get("path_prefix"), cfg.get("exclude"), cfg.get("root_priority"))
    days = group_days(photos)

    print(f"Кадров в каталоге: {stats['total']}  · исключено фильтром: {stats['excluded']}"
          f"  · дублей убрано: {stats['duplicates']}  · осталось: {len(photos)}")
    print(f"Съёмочных дней: {len(days)}  · без локации: {sum(1 for d in days if not d.location)}\n")
    print(f"{'Дата':<11} {'Локация':<28} {'Время':<13} {'Кадров':>7} {'Отобр.':>7}")
    print("-" * 70)
    for d in days:
        t = f"{d.first_shot:%H:%M}–{d.last_shot:%H:%M}" if d.first_shot else ""
        loc = d.location or "— без локации —"
        print(f"{d.date:<11} {loc[:28]:<28} {t:<13} {len(d.photos):>7} {d.selected_count:>7}")

    if args.no_loc:
        missing = Counter(p.folder_path for p in photos if parse_location(p.folder_path)[0] is None)
        print(f"\nПапки без распознанной локации ({len(missing)}):")
        for folder, n in missing.most_common():
            print(f"  {n:>6}  {folder}")


if __name__ == "__main__":
    main()
