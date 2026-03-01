#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except Exception as exc:
    print(f"Missing dependency: pillow. Error: {exc}")
    raise SystemExit(1)

try:
    from pillow_heif import register_heif_opener
except Exception:
    register_heif_opener = None

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    import openai as openai_module
except Exception:
    openai_module = None


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
CACHE_DIR = BASE_DIR / "cache"
CACHE_FILE = CACHE_DIR / "hash.json"
PROCESSED_INDEX_FILE = CACHE_DIR / "processed_index.json"
DETECTION_CACHE_FILE = CACHE_DIR / "detection.json"
ORIGINAL_HEIC_DIR = INPUT_DIR / "original_heic"
CROPS_DIR = CACHE_DIR / "crops"

PROCESSABLE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ALLOWED_TYPES = {"single", "series", "block", "sheet", "fdc", "unknown"}
ALLOWED_CONDITIONS = {"mint", "used", "cto", "unknown"}

DEFAULT_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini")
DEFAULT_DETECTION_MODEL = os.getenv("OPENAI_DETECTION_MODEL", DEFAULT_MODEL)
MAX_FILENAME_LENGTH = 120
DETECTION_PADDING_RATIO = float(os.getenv("DETECTION_PADDING_RATIO", "0.35"))
MIN_DETECTED_DIM_RATIO = float(os.getenv("MIN_DETECTED_DIM_RATIO", "0.16"))
MIN_DETECTED_AREA_RATIO = float(os.getenv("MIN_DETECTED_AREA_RATIO", "0.015"))
DETECTION_CACHE_VERSION = 2
CSV_HEADERS_PL = [
    "nazwa_pliku",
    "zrodlo_obrazu",
    "indeks_znaczka",
    "bbox",
    "tytul",
    "opis",
    "tagi",
    "typ",
    "stan",
    "pewnosc",
    "wymaga_weryfikacji",
]

TYPE_TO_PL = {
    "single": "pojedynczy",
    "series": "seria",
    "block": "blok",
    "sheet": "arkusz",
    "fdc": "koperta_fdc",
    "unknown": "nieznany",
}

CONDITION_TO_PL = {
    "mint": "czysty",
    "used": "kasowany",
    "cto": "cto",
    "unknown": "nieznany",
}

MODEL_PROMPT = """You analyze a photo of a postage stamp and produce data for an Allegro listing.

Rules:

Return JSON only.
No markdown.
No explanations.

Do not invent facts.
If unsure use null.

Keep title short.

Fields:

country
era
year
series_name
topic
nominal
type
condition
defects
tags
confidence
needs_manual_review
title_pl
description_pl

Description rules:

Polish language.

Multiple lines separated by newline.

Include:

Country and era
Year if known
Topic or series
Type
Condition
Defects if visible
Stored in album
Combined shipping possible
"""

DETECTION_PROMPT = """You detect all visible postage stamps on an image.

Return JSON only, no markdown, no explanations.

Expected JSON schema:
{
  "stamps": [
    {
      "x": number,
      "y": number,
      "w": number,
      "h": number,
      "confidence": number
    }
  ]
}

Rules:
- Coordinates are normalized between 0 and 1.
- x,y are top-left corner.
- w,h are width and height.
- Include every visible stamp.
- Do not include album borders or empty spaces.
- If there is only one stamp, return one bounding box.
- If unsure, return empty array.
"""


def ensure_directories() -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CROPS_DIR.mkdir(parents=True, exist_ok=True)
    if not CACHE_FILE.exists():
        CACHE_FILE.write_text("{}", encoding="utf-8")
    if not PROCESSED_INDEX_FILE.exists():
        PROCESSED_INDEX_FILE.write_text("{}", encoding="utf-8")
    if not DETECTION_CACHE_FILE.exists():
        DETECTION_CACHE_FILE.write_text("{}", encoding="utf-8")


def load_cache() -> dict[str, dict[str, Any]]:
    if not CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        parsed: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, dict):
                parsed[key] = value
        return parsed
    except Exception as exc:
        print(f"Warning: failed to load cache file {CACHE_FILE.name}: {exc}")
        return {}


def save_cache(cache: dict[str, dict[str, Any]]) -> None:
    tmp_file = CACHE_FILE.with_suffix(".tmp")
    tmp_file.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_file.replace(CACHE_FILE)


def load_processed_index() -> dict[str, dict[str, Any]]:
    if not PROCESSED_INDEX_FILE.exists():
        return {}
    try:
        raw = json.loads(PROCESSED_INDEX_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        parsed: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, dict):
                parsed[key] = value
        return parsed
    except Exception as exc:
        print(
            f"Warning: failed to load processed index file {PROCESSED_INDEX_FILE.name}: {exc}"
        )
        return {}


def save_processed_index(processed_index: dict[str, dict[str, Any]]) -> None:
    tmp_file = PROCESSED_INDEX_FILE.with_suffix(".tmp")
    tmp_file.write_text(
        json.dumps(processed_index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_file.replace(PROCESSED_INDEX_FILE)


def load_detection_cache() -> dict[str, dict[str, Any]]:
    if not DETECTION_CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(DETECTION_CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        parsed: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, dict):
                parsed[key] = value
        return parsed
    except Exception as exc:
        print(f"Warning: failed to load detection cache file {DETECTION_CACHE_FILE.name}: {exc}")
        return {}


def save_detection_cache(detection_cache: dict[str, dict[str, Any]]) -> None:
    tmp_file = DETECTION_CACHE_FILE.with_suffix(".tmp")
    tmp_file.write_text(
        json.dumps(detection_cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_file.replace(DETECTION_CACHE_FILE)


def sha1_of_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_ascii(value: Any, fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "_", ascii_text)
    ascii_text = re.sub(r"_+", "_", ascii_text).strip("_")
    return ascii_text or fallback


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return False


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        return default
    return max(0.0, min(1.0, parsed))


def to_year(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        if digits:
            try:
                return int(digits)
            except ValueError:
                return None
    return None


def to_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def normalize_response(data: dict[str, Any]) -> dict[str, Any]:
    country = str(data.get("country")).strip() if data.get("country") is not None else None
    era = str(data.get("era")).strip() if data.get("era") is not None else None
    series_name = str(data.get("series_name")).strip() if data.get("series_name") is not None else None
    topic = str(data.get("topic")).strip() if data.get("topic") is not None else None
    nominal = str(data.get("nominal")).strip() if data.get("nominal") is not None else None
    stype = str(data.get("type", "unknown")).strip().lower()
    condition = str(data.get("condition", "unknown")).strip().lower()
    confidence = to_float(data.get("confidence"), default=0.0)
    review_flag = bool_value(data.get("needs_manual_review"))
    review_flag = review_flag or confidence < 0.7
    title_pl = str(data.get("title_pl") or "").strip()
    description_pl = str(data.get("description_pl") or "").strip()

    if stype not in ALLOWED_TYPES:
        stype = "unknown"
    if condition not in ALLOWED_CONDITIONS:
        condition = "unknown"

    year = to_year(data.get("year"))

    if len(title_pl) > 80:
        title_pl = title_pl[:80].strip()

    if not title_pl:
        title_country = country or "Nieznany kraj"
        title_topic = topic or series_name or "znaczek"
        title_pl = f"{title_country} {title_topic}".strip()
        title_pl = re.sub(r"\s+", " ", title_pl)
        title_pl = title_pl[:60].strip()

    if not description_pl:
        year_text = str(year) if year is not None else "nieznany"
        topic_text = topic or series_name or "nieznany"
        defects = to_string_list(data.get("defects"))
        defects_text = ", ".join(defects) if defects else "brak widocznych"
        description_pl = (
            f"- Kraj/okres: {country or 'nieznany'} {era or ''}".strip()
            + f"\n- Rok: {year_text}"
            + f"\n- Motyw: {topic_text}"
            + f"\n- Typ: {stype}"
            + f"\n- Stan: {condition}"
            + f"\n- Wady: {defects_text}"
            + "\n- Pochodzenie: klaser"
            + "\n- Wysylka: mozliwosc laczenia wysylek"
        )

    return {
        "country": country or None,
        "era": era or None,
        "year": year,
        "series_name": series_name or None,
        "topic": topic or None,
        "nominal": nominal or None,
        "type": stype,
        "condition": condition,
        "defects": to_string_list(data.get("defects")),
        "tags": to_string_list(data.get("tags")),
        "confidence": confidence,
        "needs_manual_review": review_flag,
        "title_pl": title_pl,
        "description_pl": description_pl,
    }


def parse_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Empty model response")
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response")
    parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Model response JSON is not an object")
    return parsed


def get_openai_client() -> Any | None:
    if OpenAI is None:
        if openai_module is not None:
            version = getattr(openai_module, "__version__", "unknown")
            print(
                "Warning: installed openai package is too old "
                f"(version {version}). Upgrade to openai>=1.0.0."
            )
        return None
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    base_url = os.getenv("OPENAI_BASE_URL")
    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url)
    return OpenAI(api_key=api_key)


def image_to_data_url(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        mime = "image/jpeg"
    elif ext == ".png":
        mime = "image/png"
    else:
        mime = "application/octet-stream"

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def analyze_image(client: Any, image_path: Path, model: str) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": MODEL_PROMPT,
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Analyze this stamp image."},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_to_data_url(image_path)},
                    },
                ],
            },
        ],
    )
    raw_content = response.choices[0].message.content or "{}"
    return normalize_response(parse_json(raw_content))


def analyze_stamp_regions(client: Any, image_path: Path, model: str) -> list[dict[str, Any]]:
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": DETECTION_PROMPT,
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Detect stamps and return bounding boxes."},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_to_data_url(image_path)},
                    },
                ],
            },
        ],
    )
    raw_content = response.choices[0].message.content or "{}"
    payload = parse_json(raw_content)
    raw_stamps = payload.get("stamps")
    if not isinstance(raw_stamps, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw_stamps:
        if not isinstance(item, dict):
            continue
        x = to_float(item.get("x"), 0.0)
        y = to_float(item.get("y"), 0.0)
        w = to_float(item.get("w"), 0.0)
        h = to_float(item.get("h"), 0.0)
        conf = to_float(item.get("confidence"), 0.0)
        if w <= 0.0 or h <= 0.0:
            continue
        out.append({"x": x, "y": y, "w": w, "h": h, "confidence": conf})
    return out


def convert_heic_files() -> None:
    heic_files = sorted(
        p for p in INPUT_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".heic"
    )
    if not heic_files:
        return

    if register_heif_opener is None:
        print("Warning: pillow-heif is not installed. HEIC files will be skipped.")
        return

    register_heif_opener()
    ORIGINAL_HEIC_DIR.mkdir(parents=True, exist_ok=True)

    for heic_path in heic_files:
        jpg_path = INPUT_DIR / f"{heic_path.stem}.jpg"
        try:
            with Image.open(heic_path) as image:
                rgb_image = image.convert("RGB")
                rgb_image.save(jpg_path, format="JPEG", quality=95)

            target_original = ORIGINAL_HEIC_DIR / heic_path.name
            if target_original.exists():
                target_original = ORIGINAL_HEIC_DIR / f"{heic_path.stem}_{sha1_of_file(heic_path)[:8]}{heic_path.suffix.lower()}"
            shutil.move(str(heic_path), str(target_original))
            print(f"Converted {heic_path.name} -> {jpg_path.name}")
        except Exception as exc:
            print(f"Error converting HEIC {heic_path.name}: {exc}")
            continue


def should_process_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in PROCESSABLE_EXTENSIONS


def is_recognized(data: dict[str, Any]) -> bool:
    core_known = any(
        [
            data.get("country"),
            data.get("era"),
            data.get("year") is not None,
            data.get("series_name"),
            data.get("topic"),
            data.get("nominal"),
            data.get("type") != "unknown",
        ]
    )
    has_text = bool(str(data.get("title_pl") or "").strip()) and bool(
        str(data.get("description_pl") or "").strip()
    )
    return core_known and has_text and to_float(data.get("confidence"), 0.0) > 0.0


def build_target_filename(
    image_path: Path, data: dict[str, Any], file_hash: str
) -> str:
    country = safe_ascii(data.get("country"))
    era = safe_ascii(data.get("era"))
    year = safe_ascii(data.get("year"))
    topic_series = safe_ascii(data.get("topic") or data.get("series_name"))
    stype = safe_ascii(data.get("type"))
    condition = safe_ascii(data.get("condition"))
    confidence = to_float(data.get("confidence"), 0.0)
    confidence_part = f"c{confidence:.2f}"
    hash8 = file_hash[:8]

    extension = ".jpg" if image_path.suffix.lower() in {".jpg", ".jpeg"} else ".png"
    review_prefix = (
        "REVIEW__"
        if bool_value(data.get("needs_manual_review")) or confidence < 0.7
        else ""
    )

    def build(topic_value: str) -> str:
        return (
            f"{review_prefix}"
            f"{country}_{era}_{year}_{topic_value}_{stype}_{condition}_"
            f"{confidence_part}__{hash8}{extension}"
        )

    filename = build(topic_series)
    while len(filename) > MAX_FILENAME_LENGTH and len(topic_series) > 1:
        topic_series = topic_series[:-1]
        filename = build(topic_series)

    if len(filename) > MAX_FILENAME_LENGTH:
        topic_series = "x"
        filename = build(topic_series)

    if len(filename) > MAX_FILENAME_LENGTH:
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        stem = stem[: max(1, MAX_FILENAME_LENGTH - len(suffix))]
        filename = f"{stem}{suffix}"

    return filename


def make_unique_path(candidate: Path, current_path: Path) -> Path:
    if candidate == current_path or not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2

    while True:
        extra = f"_{counter}"
        allowed_stem = MAX_FILENAME_LENGTH - len(suffix) - len(extra)
        new_stem = stem[: max(1, allowed_stem)]
        new_candidate = candidate.with_name(f"{new_stem}{extra}{suffix}")
        if new_candidate == current_path or not new_candidate.exists():
            return new_candidate
        counter += 1


def bbox_to_text(bbox: tuple[int, int, int, int] | None) -> str:
    if bbox is None:
        return ""
    x, y, w, h = bbox
    return f"{x},{y},{w},{h}"


def format_listing_entry(
    filename: str,
    data: dict[str, Any],
    source_image: str | None = None,
    crop_index: int | None = None,
    crop_bbox: tuple[int, int, int, int] | None = None,
) -> str:
    tags = ", ".join(data.get("tags") or [])
    confidence = to_float(data.get("confidence"), 0.0)
    review = bool_value(data.get("needs_manual_review")) or confidence < 0.7
    stype = TYPE_TO_PL.get(str(data.get("type") or "unknown"), "nieznany")
    condition = CONDITION_TO_PL.get(
        str(data.get("condition") or "unknown"), "nieznany"
    )

    lines = [
        f"=== {filename} ===",
        "",
        "TYTUL:",
        str(data.get("title_pl") or ""),
        "",
        "OPIS:",
        str(data.get("description_pl") or ""),
        "",
        "TAGI:",
        tags,
        "",
        "TYP:",
        stype,
        "",
        "STAN:",
        condition,
        "",
        "PEWNOSC:",
        f"{confidence:.2f}",
        "",
        "WYMAGA_WERYFIKACJI:",
        "tak" if review else "nie",
        "",
        "ZRODLO_OBRAZU:",
        str(source_image or ""),
        "",
        "INDEKS_ZNACZKA:",
        str(crop_index or ""),
        "",
        "BBOX:",
        bbox_to_text(crop_bbox),
        "",
    ]
    return "\n".join(lines)


def flatten_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def failed_listing_entry(
    filename: str,
    source_image: str | None = None,
    crop_index: int | None = None,
    crop_bbox: tuple[int, int, int, int] | None = None,
) -> str:
    return (
        f"=== {filename} ===\n\n"
        "NIEPOWODZENIE\n\n"
        f"ZRODLO_OBRAZU:\n{source_image or ''}\n\n"
        f"INDEKS_ZNACZKA:\n{crop_index or ''}\n\n"
        f"BBOX:\n{bbox_to_text(crop_bbox)}\n"
    )


def failed_csv_row(
    filename: str,
    source_image: str | None = None,
    crop_index: int | None = None,
    crop_bbox: tuple[int, int, int, int] | None = None,
) -> dict[str, str]:
    return {
        "nazwa_pliku": filename,
        "zrodlo_obrazu": str(source_image or ""),
        "indeks_znaczka": str(crop_index or ""),
        "bbox": bbox_to_text(crop_bbox),
        "tytul": "NIEPOWODZENIE",
        "opis": "Nie udalo sie sklasyfikowac znaczka.",
        "tagi": "",
        "typ": "nieznany",
        "stan": "nieznany",
        "pewnosc": "0.00",
        "wymaga_weryfikacji": "tak",
    }


def success_csv_row(
    filename: str,
    data: dict[str, Any],
    source_image: str | None = None,
    crop_index: int | None = None,
    crop_bbox: tuple[int, int, int, int] | None = None,
) -> dict[str, str]:
    confidence = to_float(data.get("confidence"), 0.0)
    needs_review = bool_value(data.get("needs_manual_review")) or confidence < 0.7
    return {
        "nazwa_pliku": filename,
        "zrodlo_obrazu": str(source_image or ""),
        "indeks_znaczka": str(crop_index or ""),
        "bbox": bbox_to_text(crop_bbox),
        "tytul": str(data.get("title_pl") or ""),
        "opis": flatten_text(str(data.get("description_pl") or "")),
        "tagi": ", ".join(data.get("tags") or []),
        "typ": TYPE_TO_PL.get(str(data.get("type") or "unknown"), "nieznany"),
        "stan": CONDITION_TO_PL.get(str(data.get("condition") or "unknown"), "nieznany"),
        "pewnosc": f"{confidence:.2f}",
        "wymaga_weryfikacji": "tak" if needs_review else "nie",
    }


def build_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Przetwarzanie zdjec znaczkow: analiza, rename, opisy i CSV."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Wymus pelne ponowne przetworzenie wszystkich zdjec.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Ponow analize tylko dla rekordow oznaczonych jako failed.",
    )
    parser.add_argument(
        "--recheck-review",
        action="store_true",
        help="Ponow analize tylko dla rekordow oznaczonych jako review.",
    )
    parser.add_argument(
        "--no-skip-processed",
        action="store_true",
        help="Nie pomijaj rekordow done i zawsze analizuj ponownie.",
    )
    parser.add_argument(
        "--single-stamp-only",
        action="store_true",
        help="Wylacz detekcje wielu znaczkow i analizuj cale zdjecie jako jeden znaczek.",
    )
    return parser.parse_args()


def processed_status(data: dict[str, Any]) -> str:
    confidence = to_float(data.get("confidence"), 0.0)
    needs_review = bool_value(data.get("needs_manual_review")) or confidence < 0.7
    return "review" if needs_review else "done"


def upsert_processed_record(
    processed_index: dict[str, dict[str, Any]],
    file_hash: str,
    source_filename: str,
    output_filename: str,
    status: str,
    source_image: str | None = None,
    crop_index: int | None = None,
    crop_bbox: tuple[int, int, int, int] | None = None,
) -> None:
    processed_index[file_hash] = {
        "file_hash": file_hash,
        "filename": source_filename,
        "source_image": source_image or source_filename,
        "crop_index": crop_index or 1,
        "crop_bbox": bbox_to_text(crop_bbox),
        "output_filename": output_filename,
        "status": status,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }


def should_retry_by_status(status: str, args: argparse.Namespace) -> bool:
    if args.force:
        return True
    if status == "failed":
        return args.retry_failed
    if status == "review":
        return args.recheck_review
    if status == "done":
        return args.no_skip_processed
    return args.no_skip_processed


def prepare_output_stamp_dirs() -> None:
    for item in OUTPUT_DIR.iterdir():
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)


def prepare_crops_dir() -> None:
    if CROPS_DIR.exists():
        shutil.rmtree(CROPS_DIR, ignore_errors=True)
    CROPS_DIR.mkdir(parents=True, exist_ok=True)


def clamp_pixel_box(
    x: int,
    y: int,
    w: int,
    h: int,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    x = max(0, min(x, max(0, width - 1)))
    y = max(0, min(y, max(0, height - 1)))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    if w <= 1 or h <= 1:
        return None
    return (x, y, w, h)


def expand_box_with_padding(
    box: tuple[int, int, int, int],
    image_width: int,
    image_height: int,
    padding_ratio: float,
) -> tuple[int, int, int, int] | None:
    x, y, w, h = box
    pad_x = int(round(w * max(0.0, padding_ratio)))
    pad_y = int(round(h * max(0.0, padding_ratio)))
    expanded = clamp_pixel_box(
        x - pad_x,
        y - pad_y,
        w + 2 * pad_x,
        h + 2 * pad_y,
        image_width,
        image_height,
    )
    if expanded is None:
        return None

    ex, ey, ew, eh = expanded
    min_w = max(80, int(round(image_width * max(0.05, MIN_DETECTED_DIM_RATIO))))
    min_h = max(80, int(round(image_height * max(0.05, MIN_DETECTED_DIM_RATIO))))
    if ew < min_w or eh < min_h:
        cx = ex + ew // 2
        cy = ey + eh // 2
        target_w = max(ew, min_w)
        target_h = max(eh, min_h)
        expanded = clamp_pixel_box(
            cx - target_w // 2,
            cy - target_h // 2,
            target_w,
            target_h,
            image_width,
            image_height,
        )
    return expanded


def bbox_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ax2 = ax + aw
    ay2 = ay + ah
    bx2 = bx + bw
    by2 = by + bh
    inter_x1 = max(ax, bx)
    inter_y1 = max(ay, by)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0, inter_x2 - inter_x1)
    inter_h = max(0, inter_y2 - inter_y1)
    inter = inter_w * inter_h
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    if union <= 0:
        return 0.0
    return inter / union


def normalize_boxes(
    normalized_regions: list[dict[str, Any]],
    image_width: int,
    image_height: int,
) -> list[tuple[int, int, int, int]]:
    out: list[tuple[int, int, int, int]] = []
    total_area = max(1, image_width * image_height)
    for region in normalized_regions:
        x = int(round(to_float(region.get("x"), 0.0) * image_width))
        y = int(round(to_float(region.get("y"), 0.0) * image_height))
        w = int(round(to_float(region.get("w"), 0.0) * image_width))
        h = int(round(to_float(region.get("h"), 0.0) * image_height))
        clamped = clamp_pixel_box(x, y, w, h, image_width, image_height)
        if clamped is None:
            continue
        expanded = expand_box_with_padding(
            clamped,
            image_width,
            image_height,
            DETECTION_PADDING_RATIO,
        )
        if expanded is None:
            continue
        _, _, cw, ch = expanded
        area_ratio = (cw * ch) / total_area
        if area_ratio < max(0.001, MIN_DETECTED_AREA_RATIO):
            continue
        out.append(expanded)
    out.sort(key=lambda b: (b[1], b[0]))

    deduped: list[tuple[int, int, int, int]] = []
    for candidate in out:
        if any(bbox_iou(candidate, existing) > 0.85 for existing in deduped):
            continue
        deduped.append(candidate)
    return deduped


def fallback_single_box(source_path: Path) -> list[tuple[int, int, int, int]]:
    with Image.open(source_path) as image:
        width, height = image.size
    return [(0, 0, max(1, width), max(1, height))]


def is_full_image_box(
    bbox: tuple[int, int, int, int] | None,
    image_width: int,
    image_height: int,
) -> bool:
    if bbox is None:
        return True
    x, y, w, h = bbox
    return x <= 0 and y <= 0 and w >= image_width and h >= image_height


def make_crop_file(
    source_path: Path,
    source_hash: str,
    bbox: tuple[int, int, int, int],
    crop_label: str,
) -> Path:
    x, y, w, h = bbox
    ext = source_path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        out_ext = ".jpg"
        fmt = "JPEG"
    elif ext == ".png":
        out_ext = ".png"
        fmt = "PNG"
    else:
        out_ext = ".jpg"
        fmt = "JPEG"

    crop_name = (
        f"{safe_ascii(source_path.stem, fallback='source')}"
        f"__{crop_label}_{source_hash[:8]}{out_ext}"
    )
    crop_path = CROPS_DIR / crop_name

    with Image.open(source_path) as image:
        cropped = image.crop((x, y, x + w, y + h))
        if fmt == "JPEG":
            cropped = cropped.convert("RGB")
            cropped.save(crop_path, format=fmt, quality=95)
        else:
            cropped.save(crop_path, format=fmt)
    return crop_path


def detect_stamp_boxes(
    source_path: Path,
    source_hash: str,
    client: Any | None,
    detection_cache: dict[str, dict[str, Any]],
    detection_model: str,
    single_stamp_only: bool,
    force_refresh: bool = False,
) -> list[tuple[int, int, int, int]]:
    if single_stamp_only:
        return fallback_single_box(source_path)

    if not force_refresh:
        cached = detection_cache.get(source_hash)
        if isinstance(cached, dict):
            cached_version = int(cached.get("version") or 0)
            cached_source = str(cached.get("source") or "").strip()
            cached_boxes = cached.get("boxes")
            if (
                cached_version >= DETECTION_CACHE_VERSION
                and cached_source != "fallback_detection_error"
                and isinstance(cached_boxes, list)
            ):
                parsed: list[tuple[int, int, int, int]] = []
                for item in cached_boxes:
                    if (
                        isinstance(item, list)
                        and len(item) == 4
                        and all(isinstance(x, int) for x in item)
                    ):
                        parsed.append((item[0], item[1], item[2], item[3]))
                if parsed:
                    return parsed

    if client is None:
        boxes = fallback_single_box(source_path)
        detection_cache[source_hash] = {
            "boxes": [list(b) for b in boxes],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "fallback_no_client",
            "version": DETECTION_CACHE_VERSION,
        }
        return boxes

    try:
        with Image.open(source_path) as image:
            width, height = image.size
        detected = analyze_stamp_regions(client, source_path, detection_model)
        boxes = normalize_boxes(detected, width, height)
        if not boxes:
            boxes = fallback_single_box(source_path)
            source = "fallback_empty_detection"
        else:
            source = "openai_detection"
        detection_cache[source_hash] = {
            "boxes": [list(b) for b in boxes],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "version": DETECTION_CACHE_VERSION,
        }
        return boxes
    except Exception as exc:
        print(f"Warning: detection failed for {source_path.name}: {exc}")
        boxes = fallback_single_box(source_path)
        detection_cache[source_hash] = {
            "boxes": [list(b) for b in boxes],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "fallback_detection_error",
            "version": DETECTION_CACHE_VERSION,
        }
        return boxes


def build_stamp_candidates(
    source_path: Path,
    source_hash: str,
    boxes: list[tuple[int, int, int, int]],
) -> list[dict[str, Any]]:
    if len(boxes) <= 1:
        bbox = boxes[0] if boxes else None
        with Image.open(source_path) as image:
            width, height = image.size
        use_crop = bbox is not None and not is_full_image_box(bbox, width, height)
        if use_crop and bbox is not None:
            crop_path = make_crop_file(
                source_path=source_path,
                source_hash=source_hash,
                bbox=bbox,
                crop_label="single",
            )
            analysis_path = crop_path
            output_image_path = crop_path
        else:
            analysis_path = source_path
            output_image_path = source_path
        return [
            {
                "analysis_path": analysis_path,
                "output_image_path": output_image_path,
                "source_image": source_path.name,
                "crop_index": 1,
                "crop_bbox": bbox,
                "is_multi": False,
                "should_rename_source": True,
                "is_single_cropped": use_crop,
            }
        ]

    candidates: list[dict[str, Any]] = []
    for index, bbox in enumerate(boxes, start=1):
        crop_path = make_crop_file(
            source_path=source_path,
            source_hash=source_hash,
            bbox=bbox,
            crop_label=f"s{index:02d}",
        )
        candidates.append(
            {
                "analysis_path": crop_path,
                "output_image_path": crop_path,
                "source_image": source_path.name,
                "crop_index": index,
                "crop_bbox": bbox,
                "is_multi": True,
                "should_rename_source": False,
                "is_single_cropped": False,
            }
        )
    return candidates


def write_stamp_output(
    image_path: Path,
    listing_text: str,
    output_image_name: str | None = None,
) -> None:
    try:
        image_name = output_image_name or image_path.name
        folder_base = safe_ascii(Path(image_name).stem, fallback="znaczek")
        counter = 2
        target_dir = OUTPUT_DIR / folder_base
        while True:
            try:
                target_dir.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                target_dir = OUTPUT_DIR / f"{folder_base}_{counter}"
                counter += 1
        (target_dir / "opis.txt").write_text(listing_text, encoding="utf-8")
        shutil.copy2(image_path, target_dir / image_name)
    except Exception as exc:
        print(f"Error writing output package for {image_path.name}: {exc}")


def process_images(args: argparse.Namespace) -> int:
    ensure_directories()
    convert_heic_files()
    prepare_output_stamp_dirs()
    prepare_crops_dir()

    cache = load_cache()
    processed_index = load_processed_index()
    detection_cache = load_detection_cache()
    client = get_openai_client()
    model = DEFAULT_MODEL
    detection_model = DEFAULT_DETECTION_MODEL

    if client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print("Warning: OPENAI_API_KEY not found. Only cached results will be available.")
        elif OpenAI is None:
            print(
                "Warning: OpenAI client is unavailable (install openai>=1.0.0). "
                "Only cached results will be available."
            )
        else:
            print("Warning: OpenAI client unavailable. Only cached results will be available.")

    input_files = sorted(p for p in INPUT_DIR.iterdir() if should_process_file(p))
    if not input_files:
        print("No files to process in input/")
        with (OUTPUT_DIR / "listings.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS_PL)
            writer.writeheader()
        return 0

    csv_rows: list[dict[str, str]] = []

    for source_path in input_files:
        print(f"Processing {source_path.name}")
        try:
            source_hash = sha1_of_file(source_path)
        except Exception as exc:
            print(f"Error hashing file {source_path.name}: {exc}")
            failed_entry = failed_listing_entry(source_path.name, source_path.name, 1, None)
            write_stamp_output(source_path, failed_entry, output_image_name=source_path.name)
            csv_rows.append(failed_csv_row(source_path.name, source_path.name, 1, None))
            continue

        boxes = detect_stamp_boxes(
            source_path=source_path,
            source_hash=source_hash,
            client=client,
            detection_cache=detection_cache,
            detection_model=detection_model,
            single_stamp_only=args.single_stamp_only,
            force_refresh=args.force,
        )
        candidates = build_stamp_candidates(source_path, source_hash, boxes)
        if len(candidates) > 1:
            print(f"-> detected {len(candidates)} stamp regions")

        for candidate in candidates:
            analysis_path = candidate["analysis_path"]
            output_image_path = candidate["output_image_path"]
            source_image = str(candidate["source_image"])
            crop_index = int(candidate["crop_index"])
            crop_bbox = candidate.get("crop_bbox")
            is_multi = bool(candidate.get("is_multi"))
            should_rename_source = bool(candidate.get("should_rename_source"))
            is_single_cropped = bool(candidate.get("is_single_cropped"))
            candidate_name = analysis_path.name

            try:
                file_hash = sha1_of_file(analysis_path)
            except Exception as exc:
                print(f"Error hashing file {candidate_name}: {exc}")
                failed_entry = failed_listing_entry(
                    candidate_name,
                    source_image,
                    crop_index,
                    crop_bbox,
                )
                write_stamp_output(
                    analysis_path,
                    failed_entry,
                    output_image_name=candidate_name,
                )
                csv_rows.append(
                    failed_csv_row(
                        candidate_name,
                        source_image,
                        crop_index,
                        crop_bbox,
                    )
                )
                continue

            indexed_record = processed_index.get(file_hash, {})
            existing_status = str(indexed_record.get("status") or "").strip().lower()
            if existing_status and not should_retry_by_status(existing_status, args):
                if existing_status == "done":
                    print(f"Skipping {candidate_name} (already processed)")
                    cached_data = cache.get(file_hash)
                    if cached_data is None:
                        print(
                            f"Warning: missing cache for skipped file {candidate_name}, marking as failed."
                        )
                        failed_entry = failed_listing_entry(
                            candidate_name,
                            source_image,
                            crop_index,
                            crop_bbox,
                        )
                        write_stamp_output(
                            analysis_path,
                            failed_entry,
                            output_image_name=candidate_name,
                        )
                        csv_rows.append(
                            failed_csv_row(
                                candidate_name,
                                source_image,
                                crop_index,
                                crop_bbox,
                            )
                        )
                    else:
                        data = normalize_response(cached_data)
                        if not should_rename_source:
                            output_filename = build_target_filename(
                                analysis_path, data, file_hash
                            )
                            output_source_path = output_image_path
                        else:
                            target_filename = build_target_filename(
                                source_path, data, file_hash
                            )
                            target_path = INPUT_DIR / target_filename
                            target_path = make_unique_path(target_path, source_path)
                            output_source_path = output_image_path
                            output_filename = source_path.name
                            if target_path != source_path:
                                try:
                                    source_path.rename(target_path)
                                    output_filename = target_path.name
                                    source_path = target_path
                                    print(f"-> renamed to {output_filename}")
                                except Exception as exc:
                                    print(f"Error renaming {source_path.name}: {exc}")
                            if not is_single_cropped:
                                output_source_path = source_path
                        if is_single_cropped:
                            print(f"-> single stamp crop used for output: {output_image_path.name}")

                        listing_entry = format_listing_entry(
                            output_filename,
                            data,
                            source_image=source_image,
                            crop_index=crop_index,
                            crop_bbox=crop_bbox,
                        )
                        write_stamp_output(
                            output_source_path,
                            listing_entry,
                            output_image_name=output_filename,
                        )
                        csv_rows.append(
                            success_csv_row(
                                output_filename,
                                data,
                                source_image=source_image,
                                crop_index=crop_index,
                                crop_bbox=crop_bbox,
                            )
                        )
                        upsert_processed_record(
                            processed_index=processed_index,
                            file_hash=file_hash,
                            source_filename=analysis_path.name,
                            source_image=source_image,
                            crop_index=crop_index,
                            crop_bbox=crop_bbox,
                            output_filename=output_filename,
                            status=processed_status(data),
                        )
                    continue

                if existing_status == "failed":
                    print(f"Skipping {candidate_name} (failed before, use --retry-failed)")
                    failed_entry = failed_listing_entry(
                        candidate_name,
                        source_image,
                        crop_index,
                        crop_bbox,
                    )
                    write_stamp_output(
                        analysis_path,
                        failed_entry,
                        output_image_name=candidate_name,
                    )
                    csv_rows.append(
                        failed_csv_row(
                            candidate_name,
                            source_image,
                            crop_index,
                            crop_bbox,
                        )
                    )
                    continue

                if existing_status == "review":
                    print(f"Skipping {candidate_name} (review before, use --recheck-review)")
                    cached_data = cache.get(file_hash)
                    if cached_data is None:
                        failed_entry = failed_listing_entry(
                            candidate_name,
                            source_image,
                            crop_index,
                            crop_bbox,
                        )
                        write_stamp_output(
                            analysis_path,
                            failed_entry,
                            output_image_name=candidate_name,
                        )
                        csv_rows.append(
                            failed_csv_row(
                                candidate_name,
                                source_image,
                                crop_index,
                                crop_bbox,
                            )
                        )
                    else:
                        data = normalize_response(cached_data)
                        if not should_rename_source:
                            output_filename = build_target_filename(
                                analysis_path, data, file_hash
                            )
                            output_source_path = output_image_path
                        else:
                            target_filename = build_target_filename(
                                source_path, data, file_hash
                            )
                            target_path = INPUT_DIR / target_filename
                            target_path = make_unique_path(target_path, source_path)
                            output_source_path = output_image_path
                            output_filename = source_path.name
                            if target_path != source_path:
                                try:
                                    source_path.rename(target_path)
                                    output_filename = target_path.name
                                    source_path = target_path
                                    print(f"-> renamed to {output_filename}")
                                except Exception as exc:
                                    print(f"Error renaming {source_path.name}: {exc}")
                            if not is_single_cropped:
                                output_source_path = source_path
                        if is_single_cropped:
                            print(f"-> single stamp crop used for output: {output_image_path.name}")

                        listing_entry = format_listing_entry(
                            output_filename,
                            data,
                            source_image=source_image,
                            crop_index=crop_index,
                            crop_bbox=crop_bbox,
                        )
                        write_stamp_output(
                            output_source_path,
                            listing_entry,
                            output_image_name=output_filename,
                        )
                        csv_rows.append(
                            success_csv_row(
                                output_filename,
                                data,
                                source_image=source_image,
                                crop_index=crop_index,
                                crop_bbox=crop_bbox,
                            )
                        )
                        upsert_processed_record(
                            processed_index=processed_index,
                            file_hash=file_hash,
                            source_filename=analysis_path.name,
                            source_image=source_image,
                            crop_index=crop_index,
                            crop_bbox=crop_bbox,
                            output_filename=output_filename,
                            status=processed_status(data),
                        )
                    continue

            if args.force:
                print(f"Reprocessing {candidate_name} (force)")
            elif existing_status == "failed" and args.retry_failed:
                print(f"Reprocessing {candidate_name} (retry failed)")
            elif existing_status == "review" and args.recheck_review:
                print(f"Reprocessing {candidate_name} (recheck review)")
            elif existing_status == "done" and args.no_skip_processed:
                print(f"Reprocessing {candidate_name} (no skip processed)")

            data = cache.get(file_hash)
            if data is None:
                if client is None:
                    print(f"Error: cannot analyze {candidate_name} without OpenAI client and cache.")
                    failed_entry = failed_listing_entry(
                        candidate_name,
                        source_image,
                        crop_index,
                        crop_bbox,
                    )
                    write_stamp_output(
                        analysis_path,
                        failed_entry,
                        output_image_name=candidate_name,
                    )
                    csv_rows.append(
                        failed_csv_row(
                            candidate_name,
                            source_image,
                            crop_index,
                            crop_bbox,
                        )
                    )
                    upsert_processed_record(
                        processed_index=processed_index,
                        file_hash=file_hash,
                        source_filename=analysis_path.name,
                        source_image=source_image,
                        crop_index=crop_index,
                        crop_bbox=crop_bbox,
                        output_filename=candidate_name,
                        status="failed",
                    )
                    continue

                try:
                    data = analyze_image(client, analysis_path, model)
                    cache[file_hash] = data
                except Exception as exc:
                    print(f"Error analyzing {candidate_name}: {exc}")
                    failed_entry = failed_listing_entry(
                        candidate_name,
                        source_image,
                        crop_index,
                        crop_bbox,
                    )
                    write_stamp_output(
                        analysis_path,
                        failed_entry,
                        output_image_name=candidate_name,
                    )
                    csv_rows.append(
                        failed_csv_row(
                            candidate_name,
                            source_image,
                            crop_index,
                            crop_bbox,
                        )
                    )
                    upsert_processed_record(
                        processed_index=processed_index,
                        file_hash=file_hash,
                        source_filename=analysis_path.name,
                        source_image=source_image,
                        crop_index=crop_index,
                        crop_bbox=crop_bbox,
                        output_filename=candidate_name,
                        status="failed",
                    )
                    continue
            else:
                data = normalize_response(data)

            if not is_recognized(data):
                print(f"-> FAILED recognition for {candidate_name}")
                failed_entry = failed_listing_entry(
                    candidate_name,
                    source_image,
                    crop_index,
                    crop_bbox,
                )
                write_stamp_output(
                    analysis_path,
                    failed_entry,
                    output_image_name=candidate_name,
                )
                csv_rows.append(
                    failed_csv_row(
                        candidate_name,
                        source_image,
                        crop_index,
                        crop_bbox,
                    )
                )
                upsert_processed_record(
                    processed_index=processed_index,
                    file_hash=file_hash,
                    source_filename=analysis_path.name,
                    source_image=source_image,
                    crop_index=crop_index,
                    crop_bbox=crop_bbox,
                    output_filename=candidate_name,
                    status="failed",
                )
                continue

            if not should_rename_source:
                output_filename = build_target_filename(analysis_path, data, file_hash)
                output_source_path = output_image_path
                print(f"-> classified segment {crop_index} as {output_filename}")
            else:
                target_filename = build_target_filename(source_path, data, file_hash)
                target_path = INPUT_DIR / target_filename
                target_path = make_unique_path(target_path, source_path)
                output_source_path = output_image_path
                output_filename = source_path.name
                if target_path != source_path:
                    try:
                        source_path.rename(target_path)
                        output_filename = target_path.name
                        source_path = target_path
                        print(f"-> renamed to {output_filename}")
                    except Exception as exc:
                        print(f"Error renaming {source_path.name}: {exc}")
                else:
                    print(f"-> filename unchanged: {output_filename}")
                if not is_single_cropped:
                    output_source_path = source_path
                if is_single_cropped:
                    print(f"-> single stamp crop used for output: {output_image_path.name}")

            listing_entry = format_listing_entry(
                output_filename,
                data,
                source_image=source_image,
                crop_index=crop_index,
                crop_bbox=crop_bbox,
            )
            write_stamp_output(
                output_source_path,
                listing_entry,
                output_image_name=output_filename,
            )
            csv_rows.append(
                success_csv_row(
                    output_filename,
                    data,
                    source_image=source_image,
                    crop_index=crop_index,
                    crop_bbox=crop_bbox,
                )
            )
            upsert_processed_record(
                processed_index=processed_index,
                file_hash=file_hash,
                source_filename=analysis_path.name,
                source_image=source_image,
                crop_index=crop_index,
                crop_bbox=crop_bbox,
                output_filename=output_filename,
                status=processed_status(data),
            )

    with (OUTPUT_DIR / "listings.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS_PL)
        writer.writeheader()
        writer.writerows(csv_rows)

    save_cache(cache)
    save_processed_index(processed_index)
    save_detection_cache(detection_cache)
    print(f"Done. Processed {len(input_files)} files.")
    return 0


def main() -> int:
    try:
        args = build_cli_args()
        return process_images(args)
    except Exception as exc:
        print(f"Fatal error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
