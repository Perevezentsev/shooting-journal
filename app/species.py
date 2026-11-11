"""Распознавание видов по превью Lightroom: MegaDetector → кроп → эмбеддинг BioCLIP-2.

Запуск (нужен requirements-species.txt; журнал работает и без него):
    python -m app.species run              # отобранные (★2+), свежие первыми
    python -m app.species run --minutes 30 # частями: остановится сам через 30 минут
    python -m app.species status
    python -m app.species show 7842234
    python -m app.species region           # региональный список видов iNat (кэш в базе)
    python -m app.species rescore          # пересчитать топ-5 из эмбеддингов, без превью
    python -m app.species sample           # 30 случайных «целых кадров» с уверенностью < 0.5

Что хранится:
- species_crops — по кадру: рамка MegaDetector, откуда взят кроп, статус. Ключ продолжения:
  кадр с записью здесь повторно не обрабатывается.
- data/embeddings/bioclip-2/<image_id>.npy — нормированный эмбеддинг кропа (float16, 768).
  Это главный результат: смена списка видов или априорных вероятностей — пересчёт
  (rescore), без повторного чтения превью.
- species_preds — топ-5 видов. Пересчитываемый, его можно стереть в любой момент.

Прогон бережёт карту: перед каждой пачкой читает температуру GPU, при hot °C (78) ждёт
остывания до cool °C (70) или пока температура не перестанет падать. Ctrl+C — аккуратная остановка после текущей пачки.

Тяжёлые библиотеки (torch, bioclip, megadetector) импортируются только внутри функций.
"""

from __future__ import annotations

import os

# Pascal (GTX 1060): addmm со смещением через cuBLASLt выбирает медленное ядро,
# без него кодировщик BioCLIP быстрее в 1,5 раза (254 → 170 мс/кадр). Ставим до импорта torch.
os.environ.setdefault("DISABLE_ADDMM_CUDA_LT", "1")

import argparse
import fcntl
import json
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODELS_DIR = DATA / "models"
MD_FILE = "md_v5a.0.1.pt"
EMB_MODEL = "bioclip-2"
EMB_DIR = DATA / "embeddings" / EMB_MODEL
LOCK_FILE = DATA / "species.lock"

DETECT_LONG = 1600      # уровень превью для MegaDetector (обычно попадается 1032)
MID_LONG = 2100         # промежуточный уровень для кропа (обычно 2064)
SMALL_BOX = 0.10        # рамка уже 10 % ширины кадра → кроп из полного уровня
CROP_PAD = 0.15         # поля вокруг рамки, доля её стороны
CROP_PX = 448           # кроп не мельче этого (модель смотрит 224×224)
DET_THRESHOLD = 0.2
TOP_K = 5
PLATEAU_S = 120        # пауза: столько секунд без остывания — дальше ждать бесполезно

SCHEMA = """
CREATE TABLE IF NOT EXISTS species_crops (
    image_id     INTEGER PRIMARY KEY,          -- photos.image_id (без FK: sync пересоздаёт photos)
    status       TEXT NOT NULL,                -- crop | full (животное не найдено, весь кадр) | no_preview | error
    det_conf     REAL,
    bbox         TEXT,                         -- JSON [x, y, w, h], доли кадра после поворота
    dets         TEXT,                         -- JSON всех рамок животных ≥ порога
    person       INTEGER NOT NULL DEFAULT 0,   -- в кадре человек
    src_long     INTEGER,                      -- длинная сторона уровня превью, из которого кроп
    model        TEXT,
    processed_at TEXT,
    error        TEXT
);
CREATE TABLE IF NOT EXISTS species_preds (
    image_id    INTEGER NOT NULL,
    rank        INTEGER NOT NULL,              -- 1..5
    tol_idx     INTEGER,                       -- строка в TreeOfLife txt_emb_species
    scientific  TEXT,
    common      TEXT,
    class_name  TEXT,                          -- Aves, Mammalia, Insecta…
    score       REAL,
    PRIMARY KEY (image_id, rank)
);
CREATE INDEX IF NOT EXISTS idx_species_preds_sci ON species_preds(scientific);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def available() -> bool:
    """Установлены ли зависимости распознавания (без их импорта)."""
    from importlib.util import find_spec
    return all(find_spec(m) for m in ("torch", "bioclip", "megadetector"))


# ---------- GPU: температура ----------

def gpu_stats() -> dict | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,clocks.sm",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout.strip().splitlines()[0]
        t, p, c = (s.strip() for s in out.split(","))
        return {"temp": int(t), "power": float(p) if p not in ("[N/A]", "") else None, "clock": int(c)}
    except Exception:
        return None


def _gpu_text(g: dict | None) -> str:
    if not g:
        return "GPU ?"
    s = f"GPU {g['temp']} °C"
    if g.get("power"):
        s += f" {g['power']:.0f} Вт"
    return s


# ---------- геометрия кропа ----------

def crop_box(bbox, W: int, H: int, pad: float = CROP_PAD) -> tuple[int, int, int, int]:
    """Квадрат вокруг рамки с полями, сдвинутый внутрь кадра."""
    x, y, w, h = bbox
    cx, cy = (x + w / 2) * W, (y + h / 2) * H
    side = max(w * W, h * H) * (1 + 2 * pad)
    side = min(max(side, 32), max(W, H))
    left = min(max(cx - side / 2, 0), max(W - side, 0))
    top = min(max(cy - side / 2, 0), max(H - side, 0))
    return int(left), int(top), int(min(left + side, W)), int(min(top + side, H))


def pad_square(img):
    """Дополнить до квадрата нейтральным цветом: препроцессинг BioCLIP сжимает в 224×224."""
    from PIL import Image
    w, h = img.size
    if w == h:
        return img
    s = max(w, h)
    out = Image.new("RGB", (s, s), (124, 116, 104))
    out.paste(img, ((s - w) // 2, (s - h) // 2))
    return out


def _shown_size(p) -> tuple[int, int] | None:
    """Размер превью после поворота, без декодирования."""
    if not p.width or not p.height:
        return None
    return (p.height, p.width) if p.rotate_cw in (90, 270) else (p.width, p.height)


# ---------- модели ----------

def md_model_path() -> Path:
    """Веса MegaDetector в data/models/; при первом запуске скачиваются туда."""
    dst = MODELS_DIR / MD_FILE
    if dst.exists():
        return dst
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    from megadetector.detection.run_detector import try_download_known_detector
    src = Path(try_download_known_detector("MDV5A"))
    shutil.move(str(src), dst)
    return dst


class TolLabels:
    """Названия и текстовые эмбеддинги TreeOfLife (из кэша Hugging Face pybioclip)."""

    def __init__(self):
        from huggingface_hub import hf_hub_download
        from bioclip._constants import BIOCLIP_MODEL_STR
        from bioclip.predict import HF_DATAFILE_REPO_TYPE, get_tol_repo_id
        repo = get_tol_repo_id(BIOCLIP_MODEL_STR)
        self._get = lambda fn: hf_hub_download(repo_id=repo, filename=fn, repo_type=HF_DATAFILE_REPO_TYPE)
        self._names = None

    def txt_emb(self):
        import numpy as np
        return np.load(self._get("embeddings/txt_emb_species.npy"), mmap_mode="r")

    @property
    def names(self) -> list:
        if self._names is None:
            with open(self._get("embeddings/txt_emb_species.json"), encoding="utf-8") as f:
                self._names = json.load(f)
        return self._names

    def describe(self, idx: int) -> tuple[str, str, str]:
        tax, common = self.names[idx]
        return f"{tax[-2]} {tax[-1]}".strip(), common or "", tax[2] if len(tax) > 2 else ""


class Scorer:
    """Топ-k по эмбеддингам: softmax(scale · emb · txt + log_prior) по выбранным видам.
    keep — индексы видов TreeOfLife (региональный список), None — все."""

    def __init__(self, labels: TolLabels, device: str, scale: float = 100.0,
                 keep=None, log_prior=None):
        import numpy as np
        import torch
        self.labels, self.device, self.scale = labels, device, scale
        txt = labels.txt_emb()
        self.keep = None if keep is None else np.asarray(keep, dtype=np.int64)
        if self.keep is not None:
            txt = txt[:, self.keep]
        # fp16: 867 тыс. видов × 768 — 1,3 ГБ вместо 2,7 ГБ на карте с 6 ГБ
        self.txt = torch.from_numpy(np.array(txt, dtype=np.float16 if device == "cuda" else np.float32)).to(device)
        self.log_prior = None if log_prior is None else \
            torch.as_tensor(log_prior, dtype=torch.float32, device=device)

    def topk(self, emb, k: int = TOP_K) -> list[list[tuple[int, float]]]:
        """emb: (N, 768) нормированные, numpy. → [[(tol_idx, prob), ...], ...]"""
        import torch
        with torch.no_grad():
            e = torch.as_tensor(emb, device=self.device).to(self.txt.dtype)
            logits = (e @ self.txt).float() * self.scale
            if self.log_prior is not None:
                logits += self.log_prior
            v, i = torch.softmax(logits, dim=1).topk(k)
        v, i = v.cpu().numpy(), i.cpu().numpy()
        if self.keep is not None:
            i = self.keep[i]
        return [[(int(a), float(b)) for a, b in zip(ri, rv)] for ri, rv in zip(i, v)]


class Embedder:
    def __init__(self, device: str):
        import torch
        import open_clip as oc
        from bioclip._constants import BIOCLIP_MODEL_STR
        from bioclip.predict import preprocess_img
        # Без torch.compile из pybioclip: triton не поддерживает Pascal, а выигрыша нет.
        model, _ = oc.create_model_from_pretrained(BIOCLIP_MODEL_STR)
        self.model = model.to(device).eval()
        self.preprocess, self.device, self.torch = preprocess_img, device, torch
        self.scale = float(model.logit_scale.exp().item())

    def encode(self, images):
        torch = self.torch
        x = torch.stack([self.preprocess(im) for im in images]).to(self.device)
        with torch.no_grad():
            f = self.model.encode_image(x)
            f = torch.nn.functional.normalize(f, dim=-1)
        return f.float().cpu().numpy()


# ---------- один кадр ----------

@dataclass
class Item:
    image_id: int
    status: str
    image: object = None        # PIL-кроп для BioCLIP
    det_conf: float | None = None
    bbox: list | None = None
    dets: list | None = None
    person: int = 0
    src_long: int | None = None
    error: str | None = None


def prepare(store, md, image_id: int, small) -> Item:
    """Превью → MegaDetector → кроп. small — уровень для детектора (уже прочитан)."""
    if small is None:
        return Item(image_id, "no_preview")
    img = small.image()
    r = md.generate_detections_one_image(img, str(image_id), detection_threshold=DET_THRESHOLD)
    dets = r.get("detections") or []
    animals = sorted((d for d in dets if d["category"] == "1"), key=lambda d: -d["conf"])
    person = int(any(d["category"] == "2" and d["conf"] >= 0.5 for d in dets))
    if not animals:
        # Растения, грибы, насекомые MegaDetector обычно не видит — берём кадр целиком.
        im = pad_square(img)
        im.thumbnail((CROP_PX, CROP_PX))
        return Item(image_id, "full", im, person=person, src_long=small.long_side)

    best = animals[0]
    bbox = [round(v, 4) for v in best["bbox"]]
    src = None
    if bbox[2] >= SMALL_BOX:
        src = store.read(image_id, max_long=MID_LONG)
        if src is not None and src.long_side <= small.long_side:
            src = None                      # промежуточного нет — берём полный
    src = src or store.read(image_id) or small
    shown = _shown_size(src)
    scale = 1.0
    if shown:
        W, H = shown
        side = max(bbox[2] * W, bbox[3] * H) * (1 + 2 * CROP_PAD)
        scale = min(1.0, CROP_PX / max(side, 1))
    full = src.image(scale)
    crop = pad_square(full.crop(crop_box(bbox, *full.size)))
    crop.thumbnail((CROP_PX, CROP_PX))
    return Item(image_id, "crop", crop, det_conf=round(best["conf"], 3), bbox=bbox,
                dets=[[round(d["conf"], 3)] + [round(v, 4) for v in d["bbox"]] for d in animals],
                person=person, src_long=src.long_side)


# ---------- очередь и прогон ----------

def queue(conn: sqlite3.Connection, all_photos=False, day=None, retry=False) -> list[int]:
    where = ["1=1"] if all_photos else ["p.rating >= 2"]
    args: list = []
    if day:
        where.append("p.capture_time LIKE ?")
        args.append(f"{day}%")
    skip = "c.status NOT IN ('error', 'no_preview')" if retry else "1=1"
    sql = (f"SELECT p.image_id FROM photos p "
           f"WHERE {' AND '.join(where)} AND NOT EXISTS ("
           f"  SELECT 1 FROM species_crops c WHERE c.image_id = p.image_id AND {skip}) "
           f"ORDER BY p.capture_time DESC")
    return [r[0] for r in conn.execute(sql, args)]


def _prefetch(store, ids, ahead: int):
    """Читает уровни для детектора заранее в потоках: /mnt/c из WSL медленный."""
    with ThreadPoolExecutor(4) as ex:
        it = iter(ids)
        futs = deque((i, ex.submit(store.read, i, DETECT_LONG)) for i in islice(it, ahead))
        while futs:
            i, f = futs.popleft()
            nxt = next(it, None)
            if nxt is not None:
                futs.append((nxt, ex.submit(store.read, nxt, DETECT_LONG)))
            try:
                yield i, f.result(), None
            except Exception as e:
                yield i, None, e


def _save(conn, items: list[Item], embs, scorer: Scorer | None):
    now = datetime.now().isoformat(timespec="seconds")
    EMB_DIR.mkdir(parents=True, exist_ok=True)
    import numpy as np
    tops = scorer.topk(embs) if scorer is not None and len(embs) else []
    with_img = [it for it in items if it.image is not None]
    for it, e in zip(with_img, embs):
        np.save(EMB_DIR / f"{it.image_id}.npy", e.astype(np.float16))
    for it in items:
        conn.execute(
            "INSERT OR REPLACE INTO species_crops(image_id, status, det_conf, bbox, dets, person, "
            "src_long, model, processed_at, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (it.image_id, it.status, it.det_conf, json.dumps(it.bbox) if it.bbox else None,
             json.dumps(it.dets) if it.dets else None, it.person, it.src_long,
             EMB_MODEL if it.image is not None else None, now, it.error))
    _write_preds(conn, [it.image_id for it in with_img], tops, scorer.labels if scorer else None)
    conn.commit()


def _write_preds(conn, ids, tops, labels: TolLabels | None):
    for image_id, top in zip(ids, tops):
        conn.execute("DELETE FROM species_preds WHERE image_id = ?", (image_id,))
        conn.executemany(
            "INSERT INTO species_preds(image_id, rank, tol_idx, scientific, common, class_name, score) "
            "VALUES (?,?,?,?,?,?,?)",
            [(image_id, r, idx, *labels.describe(idx), round(p, 4)) for r, (idx, p) in enumerate(top, 1)])


def _fmt_dur(s: float) -> str:
    s = int(s)
    return f"{s // 3600} ч {s % 3600 // 60:02d} мин" if s >= 3600 else f"{s // 60} мин {s % 60:02d} с"


class Progress:
    def __init__(self, total: int):
        self.total, self.done, self.t0 = total, 0, time.time()
        self.busy = 0.0                    # время работы без пауз — для скорости и прогноза
        self.tty = sys.stderr.isatty()
        self._w = 0

    def line(self, gpu: dict | None, note: str = "", final=False):
        rate = self.done / self.busy if self.busy > 0 else 0
        parts = [f"{self.done}/{self.total}"]
        if rate:
            parts.append(f"{rate:.2f} кадр/с")
            if self.done < self.total:
                parts.append(f"осталось ~{_fmt_dur((self.total - self.done) / rate)}")
        parts.append(_gpu_text(gpu))
        if note:
            parts.append(note)
        s = "  " + " · ".join(parts)
        if self.tty:
            sys.stderr.write("\r" + s.ljust(self._w) + ("\n" if final else ""))
            sys.stderr.flush()
            self._w = len(s)
        else:
            print(s, file=sys.stderr, flush=True)


def run(conn: sqlite3.Connection, cfg: dict, *, all_photos=False, day=None, retry=False,
        limit=None, minutes=None, batch=16, hot=78, cool=70, rest=0.0) -> dict:
    from .previews import PreviewStore, lrdata_for

    # Второй прогон параллельно не нужен: он делил бы и карту, и запись в базу.
    DATA.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Прогон уже идёт в другом окне.")
        return {"done": 0, "reason": "уже идёт"}
    # Журнал (sync, заметки) может писать в базу одновременно — подождём, а не упадём.
    conn.execute("PRAGMA busy_timeout = 30000")

    ids = queue(conn, all_photos, day, retry)
    if limit:
        ids = ids[:limit]
    if not ids:
        print("Нечего обрабатывать: все кадры уже в species_crops.")
        return {"done": 0}

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        print("Внимание: CUDA недоступна, считаю на процессоре — это очень медленно.")
    print(f"Кадров в очереди: {len(ids)}. Загружаю превью и модели…", flush=True)
    store = PreviewStore(lrdata_for(cfg["catalog_path"]))

    from megadetector.detection.run_detector import load_detector
    md = load_detector(str(md_model_path()))
    emb = Embedder(device)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('species_logit_scale', ?)", (str(emb.scale),))
    conn.commit()                           # не держать транзакцию, пока идёт прогон
    labels = TolLabels()
    keep, log_prior = region_filter(conn, labels, verbose=True)   # если список уже скачан
    scorer = Scorer(labels, device, emb.scale, keep, log_prior)

    stop = {"flag": False}
    def on_sigint(sig, frm):
        if stop["flag"]:
            raise KeyboardInterrupt
        stop["flag"] = True
        sys.stderr.write("\n  Остановлюсь после текущей пачки (ещё раз Ctrl+C — сразу)…\n")
    old = signal.signal(signal.SIGINT, on_sigint)

    prog = Progress(len(ids))
    deadline = time.time() + minutes * 60 if minutes else None
    counts: dict[str, int] = {}
    gpu = gpu_stats()
    if gpu is None:
        print("nvidia-smi не отвечает — без контроля температуры.")
    pending: list[Item] = []

    def flush():
        imgs = [it.image for it in pending if it.image is not None]
        embs = emb.encode(imgs) if imgs else []
        _save(conn, pending, embs, scorer)
        for it in pending:
            counts[it.status] = counts.get(it.status, 0) + 1
        prog.done += len(pending)
        pending.clear()

    reason = "ошибка"
    try:
        t_batch = time.time()
        for image_id, small, err in _prefetch(store, ids, ahead=batch * 2):
            try:
                it = Item(image_id, "error", error=repr(err)) if err else prepare(store, md, image_id, small)
            except Exception as e:
                it = Item(image_id, "error", error=f"{type(e).__name__}: {e}"[:500])
            pending.append(it)
            if len(pending) < batch:
                continue
            flush()
            prog.busy += time.time() - t_batch
            gpu = gpu_stats()
            prog.line(gpu)
            if stop["flag"]:
                reason = "остановлено"
                break
            if deadline and time.time() >= deadline:
                reason = f"прошло {minutes} мин"
                break
            # Пауза: карта перегрелась — ждём остывания, показываем температуру.
            # В простое карта сама держит 67–70 °C, поэтому cool может быть недостижим:
            # если 2 минуты не холоднее достигнутого минимума — остыла до предела, продолжаем.
            if gpu and gpu["temp"] >= hot and prog.done < prog.total:
                torch.cuda.empty_cache()
                t_pause = t_min = time.time()
                low = gpu["temp"]
                while gpu and gpu["temp"] > cool and not stop["flag"] and time.time() - t_min < PLATEAU_S:
                    prog.line(gpu, f"пауза: остываю до {cool} °C ({_fmt_dur(time.time() - t_pause)})")
                    time.sleep(5)
                    gpu = gpu_stats()
                    if gpu and gpu["temp"] < low:
                        low, t_min = gpu["temp"], time.time()
                if stop["flag"]:
                    reason = "остановлено"
                    break
            if rest and prog.done < prog.total:
                prog.line(gpu, f"отдых {rest:.0f} с")
                time.sleep(rest)
            t_batch = time.time()
        else:
            if pending:
                flush()
                prog.busy += time.time() - t_batch
            reason = "готово"
    finally:
        if pending:                         # второй Ctrl+C посреди пачки: сохранить сделанное
            try:
                flush()
            except Exception:
                conn.rollback()
        signal.signal(signal.SIGINT, old)
        prog.line(gpu_stats(), reason, final=True)

    left = len(queue(conn, all_photos, day, retry))
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    print(f"Обработано {prog.done} ({summary}). В очереди осталось {left}."
          + (" Продолжить: та же команда." if left else ""))
    return {"done": prog.done, "counts": counts, "left": left, "reason": reason}


# ---------- региональный список видов (iNaturalist) ----------

# Иконические группы iNat запрашиваем по отдельности: у API потолок 10 000 записей
# на один запрос (page × per_page), а по всем группам сразу видов в регионе больше.
REGION_GROUPS = ("Aves", "Mammalia", "Amphibia", "Reptilia", "Actinopterygii", "Mollusca",
                 "Arachnida", "Insecta", "Animalia", "Plantae", "Fungi", "Chromista")
REGION_PER_PAGE = 500
REGION_CAP = 10_000
REGION_CACHE_KEY = "species_region"


def region_bbox(conn, cfg: dict | None) -> list[float]:
    """[swlat, swlng, nelat, nelng]: из config.json «species_region» или по местам журнала,
    огрублённо до целых градусов с полем в 1°. Наружу уходит только этот прямоугольник."""
    if cfg and cfg.get("species_region"):
        return [float(v) for v in cfg["species_region"]]
    import math
    r = conn.execute("SELECT MIN(lat), MIN(lon), MAX(lat), MAX(lon) FROM locations "
                     "WHERE lat IS NOT NULL AND lon IS NOT NULL").fetchone()
    if not r or r[0] is None:
        raise SystemExit("У мест нет координат — задай \"species_region\": [swlat, swlng, nelat, nelng] в config.json.")
    return [math.floor(r[0]) - 1, math.floor(r[1]) - 1, math.ceil(r[2]) + 1, math.ceil(r[3]) + 1]


def fetch_region(conn, bbox: list[float], refresh=False) -> dict:
    """Все виды iNat (research grade) в прямоугольнике, с числом наблюдений. Кэш — в inat_cache."""
    import httpx
    from . import inat
    inat.ensure_schema(conn)
    key = f"{REGION_CACHE_KEY}|{','.join(f'{v:g}' for v in bbox)}"
    if not refresh:
        row = conn.execute("SELECT data FROM inat_cache WHERE key = ?", (key,)).fetchone()
        if row:
            return json.loads(row[0])
    base = {"swlat": bbox[0], "swlng": bbox[1], "nelat": bbox[2], "nelng": bbox[3],
            "quality_grade": "research", "per_page": REGION_PER_PAGE}
    species: dict[str, dict] = {}
    truncated = []
    with httpx.Client(headers=inat.HEADERS) as client:
        for group in REGION_GROUPS:
            page, got, total = 1, 0, None
            while True:
                data = inat._get(client, "/observations/species_counts",
                                 {**base, "iconic_taxa": group, "page": page})
                res = data.get("results") or []
                total = data.get("total_results", 0)
                for r in res:
                    t = r.get("taxon") or {}
                    if t.get("rank") not in ("species", "subspecies", "variety", "form"):
                        continue
                    sci = " ".join(t.get("name", "").split()[:2])
                    if sci.count(" ") != 1 or "×" in sci:
                        continue
                    s = species.setdefault(sci, {"sci": sci, "count": 0, "group": group,
                                                 "id": t.get("id"),
                                                 "common": t.get("preferred_common_name")})
                    s["count"] += r.get("count", 0)
                got += len(res)
                print(f"  {group}: {got}/{total}", end="\r", file=sys.stderr, flush=True)
                if not res or got >= total:
                    break
                if page * REGION_PER_PAGE >= REGION_CAP:
                    truncated.append(f"{group} ({got} из {total})")
                    break
                page += 1
            print(f"  {group}: {got} видов".ljust(40), file=sys.stderr)
    payload = {"bbox": bbox, "fetched_at": datetime.now().isoformat(timespec="seconds"),
               "truncated": truncated,
               "species": sorted(species.values(), key=lambda s: -s["count"])}
    conn.execute("INSERT OR REPLACE INTO inat_cache VALUES (?, ?, ?)",
                 (key, json.dumps(payload, ensure_ascii=False), payload["fetched_at"]))
    conn.commit()
    return payload


def _region_cached(conn) -> dict | None:
    from . import inat
    inat.ensure_schema(conn)
    row = conn.execute("SELECT data FROM inat_cache WHERE key LIKE ? ORDER BY fetched_at DESC LIMIT 1",
                       (f"{REGION_CACHE_KEY}|%",)).fetchone()
    return json.loads(row[0]) if row else None


def region_filter(conn, labels: TolLabels, prior: float = 0.0, verbose=False):
    """(keep, log_prior) для Scorer по сохранённому региональному списку; (None, None) — списка нет.
    Сопоставление по «род вид»: у растений в TreeOfLife к эпитету приписан автор — его отрезаем.
    prior > 0 — слабая поправка на частоту вида в iNat: + prior·ln(наблюдений)."""
    reg = _region_cached(conn)
    if not reg:
        return None, None
    import math
    index: dict[str, list[int]] = {}
    by_common: dict[str, list[int]] = {}
    by_epithet: dict[tuple[str, str], list[int]] = {}    # (класс, эпитет) → виды
    for i, (tax, common) in enumerate(labels.names):
        if len(tax) >= 2 and tax[-1]:
            ep = tax[-1].split()[0].lower()
            index.setdefault(f"{tax[-2]} {ep}".lower(), []).append(i)
            if len(tax) > 2:
                by_epithet.setdefault((tax[2], ep), []).append(i)
            if common:
                by_common.setdefault(common.lower(), []).append(i)
    keep, lp, missed = [], [], []
    synonyms = 0
    for s in reg["species"]:
        idx = index.get(s["sci"].lower())
        if not idx:
            # Синонимы: iNat уже перешёл на новые роды (Schoeniclus, Curruca, Astur),
            # TreeOfLife — ещё нет. Сначала по английскому названию, потом по эпитету,
            # если в том же классе он единственный.
            com = (s.get("common") or s.get("ru") or "").lower()
            idx = by_common.get(com) if com else None
            if not idx or len(idx) > 1:
                cand = by_epithet.get((s["group"], s["sci"].split()[1].lower()), [])
                idx = cand if len(cand) == 1 else None
            if not idx:
                missed.append(s)
                continue
            synonyms += 1
        for i in idx:
            keep.append(i)
            lp.append(prior * math.log(max(s["count"], 1)))
    if verbose:
        print(f"Региональный список: {len(reg['species'])} видов iNat, в TreeOfLife нашлось "
              f"{len(reg['species']) - len(missed)} (из них {synonyms} — через синонимы), "
              f"не нашлось {len(missed)}.")
        if missed:
            print("  Частые из ненайденных: " + ", ".join(
                f"{s['sci']} ({s['count']})" for s in missed[:12]))
    if not keep:
        return None, None
    order = sorted(range(len(keep)), key=keep.__getitem__)
    keep = [keep[i] for i in order]
    lp = [lp[i] for i in order]
    return keep, (lp if prior else None)


def rescore(conn: sqlite3.Connection, keep=None, log_prior=None, chunk=4096,
            region=True, prior=0.0) -> int:
    """Пересчитать species_preds из сохранённых эмбеддингов — превью и модели не нужны.
    region — ограничить видами регионального списка, если он скачан (`region`)."""
    import numpy as np
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    conn.execute("PRAGMA busy_timeout = 30000")     # прогон может писать в базу параллельно
    row = conn.execute("SELECT value FROM meta WHERE key = 'species_logit_scale'").fetchone()
    labels = TolLabels()
    if region and keep is None:
        keep, log_prior = region_filter(conn, labels, prior, verbose=True)
        if keep is None:
            print("Регионального списка нет (python -m app.species region) — считаю по всем видам.")
    scorer = Scorer(labels, device, float(row[0]) if row else 100.0, keep, log_prior)
    ids = [r[0] for r in conn.execute(
        "SELECT image_id FROM species_crops WHERE model = ? ORDER BY image_id", (EMB_MODEL,))]
    done = 0
    for i in range(0, len(ids), chunk):
        part = [j for j in ids[i:i + chunk] if (EMB_DIR / f"{j}.npy").exists()]
        if not part:
            continue
        embs = np.stack([np.load(EMB_DIR / f"{j}.npy").astype(np.float32) for j in part])
        _write_preds(conn, part, scorer.topk(embs), scorer.labels)
        conn.commit()
        done += len(part)
        print(f"  {done}/{len(ids)}", end="\r", file=sys.stderr, flush=True)
    print(f"Пересчитано: {done} кадров.")
    return done


def sample(conn, cfg: dict, out: Path, n=30, status="full", max_score=0.5, seed=None) -> int:
    """Случайные кадры для просмотра глазами: превью среднего уровня + index.csv.
    Имя файла: <image_id>_<score>_<топ-1>.jpg."""
    import csv
    import random
    from .previews import PreviewStore, lrdata_for
    rows = conn.execute(
        "SELECT c.image_id, p.score, p.scientific, p.common, ph.capture_time "
        "FROM species_crops c JOIN species_preds p ON p.image_id = c.image_id AND p.rank = 1 "
        "LEFT JOIN photos ph ON ph.image_id = c.image_id "
        "WHERE c.status = ? AND p.score < ?", (status, max_score)).fetchall()
    rows = random.Random(seed).sample(list(rows), min(n, len(rows)))
    out.mkdir(parents=True, exist_ok=True)
    store = PreviewStore(lrdata_for(cfg["catalog_path"]))
    written = 0
    with open(out / "index.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["файл", "image_id", "снято", "уверенность", "топ-1", "название"])
        for image_id, score, sci, common, taken in rows:
            pv = store.read(image_id, max_long=MID_LONG)
            if pv is None:
                continue
            name = f"{image_id}_{score:.2f}_{sci.split(' (')[0].replace(' ', '-')}.jpg"
            im = pv.image()
            im.thumbnail((2048, 2048))
            im.save(out / name, quality=90)
            w.writerow([name, image_id, taken, f"{score:.3f}", sci, common])
            written += 1
    print(f"Отобрано {written} из {len(rows)} (всего подходящих в базе — по условию status={status}, "
          f"score<{max_score}). Папка: {out}")
    return written


# ---------- CLI ----------

def _status(conn):
    rows = dict(conn.execute("SELECT status, COUNT(*) FROM species_crops GROUP BY status").fetchall())
    total = conn.execute("SELECT COUNT(*) FROM photos WHERE rating >= 2").fetchone()[0]
    print(f"Отобранных (★2+): {total}; обработано: {sum(rows.values())} "
          f"({', '.join(f'{k}: {v}' for k, v in sorted(rows.items())) or '—'}); "
          f"в очереди: {len(queue(conn))}")
    print(_gpu_text(gpu_stats()))
    top = conn.execute(
        "SELECT scientific, common, COUNT(*) n, ROUND(AVG(score), 2) FROM species_preds "
        "WHERE rank = 1 GROUP BY scientific ORDER BY n DESC LIMIT 15").fetchall()
    if top:
        reg = _region_cached(conn)
        print("Чаще всего (топ-1" + (f", региональный список {len(reg['species'])} видов" if reg
                                     else ", по всем видам") + "):")
        for sci, com, n, avg in top:
            print(f"  {n:5d}  {sci}  {com}  (ср. {avg})")


def _show(conn, image_id: int):
    c = conn.execute("SELECT * FROM species_crops WHERE image_id = ?", (image_id,)).fetchone()
    if not c:
        print("Кадр ещё не обработан.")
        return
    print(dict(c))
    for r in conn.execute("SELECT rank, score, scientific, common, class_name FROM species_preds "
                          "WHERE image_id = ? ORDER BY rank", (image_id,)):
        print(f"  {r[0]}. {r[1]:.3f}  {r[2]}  {r[3]}  [{r[4]}]")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m app.species", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="прогон: детектор, кропы, эмбеддинги, топ-5")
    r.add_argument("--all", action="store_true", help="все кадры, а не только ★2+")
    r.add_argument("--day", help="только день YYYY-MM-DD")
    r.add_argument("--retry", action="store_true", help="повторить ошибки и кадры без превью")
    r.add_argument("--limit", type=int, help="не больше N кадров за запуск")
    r.add_argument("--minutes", type=float, help="остановиться через N минут")
    r.add_argument("--batch", type=int, default=16)
    r.add_argument("--hot", type=int, default=78, help="пауза при такой температуре GPU, °C")
    r.add_argument("--cool", type=int, default=70, help="продолжить после остывания до, °C "
                   "(или когда 2 мин не остывает дальше)")
    r.add_argument("--rest", type=float, default=0, help="отдых между пачками, с")
    sub.add_parser("status", help="сколько сделано и что в очереди")
    s = sub.add_parser("show", help="результат по кадру")
    s.add_argument("image_id", type=int)
    rs = sub.add_parser("rescore", help="пересчитать топ-5 из эмбеддингов")
    rs.add_argument("--no-region", action="store_true", help="по всем видам, без регионального списка")
    rs.add_argument("--prior", type=float, default=0.0,
                    help="поправка на частоту вида в iNat: + W·ln(наблюдений), 0 — без неё")
    rg = sub.add_parser("region", help="скачать региональный список видов iNaturalist")
    rg.add_argument("--refresh", action="store_true", help="скачать заново, а не из кэша")
    sm = sub.add_parser("sample", help="случайные кадры в папку для просмотра")
    sm.add_argument("-n", type=int, default=30)
    sm.add_argument("--status", default="full", help="full | crop")
    sm.add_argument("--max-score", type=float, default=0.5)
    sm.add_argument("--seed", type=int)
    sm.add_argument("--out", type=Path, default=DATA / "review" / "full-lowconf")
    a = ap.parse_args(argv)
    # Тише: библиотеки моделей сами настраивают логи (HTTP-запросы, сообщения open_clip
    # и yolov5 — каждое дважды). Свой вывод у модуля через print.
    import logging
    import warnings
    logging.disable(logging.INFO)
    warnings.filterwarnings("ignore", message="pkg_resources is deprecated")
    # Модель и список видов уже скачаны — не ходить в Hugging Face при каждом запуске.
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    if all((hub / d).exists() for d in ("models--imageomics--bioclip-2",
                                        "datasets--imageomics--TreeOfLife-200M")):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")

    from .db import DB_PATH, connect
    conn = connect()
    if a.cmd == "status":
        _status(conn)
    elif a.cmd == "show":
        _show(conn, a.image_id)
    elif a.cmd == "rescore":
        rescore(conn, region=not a.no_region, prior=a.prior)
        return
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    if a.cmd == "region":
        bbox = region_bbox(conn, cfg)
        print(f"Прямоугольник: {bbox[0]:g}–{bbox[2]:g}° с.ш., {bbox[1]:g}–{bbox[3]:g}° в.д.")
        reg = fetch_region(conn, bbox, refresh=a.refresh)
        if reg.get("truncated"):
            print("Упёрлись в потолок iNat: " + ", ".join(reg["truncated"]))
        region_filter(conn, TolLabels(), verbose=True)
        print("Дальше: python -m app.species rescore")
        return
    if not cfg_path.exists():
        sys.exit("Нет config.json — не знаю, где каталог Lightroom.")
    if a.cmd == "sample":
        sample(conn, cfg, a.out, a.n, a.status, a.max_score, a.seed)
    elif a.cmd == "run":
        from .db import is_demo
        if is_demo(conn):
            sys.exit("Демо-база: превью Lightroom к ней не относятся.")
        print(f"База: {DB_PATH}")
        run(conn, cfg, all_photos=a.all, day=a.day,
            retry=a.retry, limit=a.limit, minutes=a.minutes, batch=a.batch,
            hot=a.hot, cool=a.cool, rest=a.rest)


if __name__ == "__main__":
    main()
