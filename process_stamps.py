#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
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
ORIGINAL_HEIC_DIR = INPUT_DIR / "original_heic"

PROCESSABLE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ALLOWED_TYPES = {"single", "series", "block", "sheet", "fdc", "unknown"}
ALLOWED_CONDITIONS = {"mint", "used", "cto", "unknown"}

DEFAULT_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini")
MAX_FILENAME_LENGTH = 120

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


def ensure_directories() -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not CACHE_FILE.exists():
        CACHE_FILE.write_text("{}", encoding="utf-8")


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


def format_listing_entry(filename: str, data: dict[str, Any]) -> str:
    tags = ", ".join(data.get("tags") or [])
    confidence = to_float(data.get("confidence"), 0.0)
    review = bool_value(data.get("needs_manual_review")) or confidence < 0.7

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
        "TYPE:",
        str(data.get("type") or "unknown"),
        "",
        "CONDITION:",
        str(data.get("condition") or "unknown"),
        "",
        "CONFIDENCE:",
        f"{confidence:.2f}",
        "",
        "REVIEW:",
        "true" if review else "false",
        "",
    ]
    return "\n".join(lines)


def flatten_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def process_images() -> int:
    ensure_directories()
    convert_heic_files()

    cache = load_cache()
    client = get_openai_client()
    model = DEFAULT_MODEL

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
        print("No files to process in stamps/input/")
        (OUTPUT_DIR / "listings.txt").write_text("", encoding="utf-8")
        with (OUTPUT_DIR / "listings.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "filename",
                    "title",
                    "description",
                    "tags",
                    "type",
                    "condition",
                    "confidence",
                    "needs_manual_review",
                ],
            )
            writer.writeheader()
        return 0

    listing_text_entries: list[str] = []
    csv_rows: list[dict[str, str]] = []

    for image_path in input_files:
        print(f"Processing {image_path.name}")
        try:
            file_hash = sha1_of_file(image_path)
        except Exception as exc:
            print(f"Error hashing file {image_path.name}: {exc}")
            listing_text_entries.append(f"=== {image_path.name} ===\n\nFAILED\n")
            csv_rows.append(
                {
                    "filename": image_path.name,
                    "title": "FAILED",
                    "description": "FAILED",
                    "tags": "",
                    "type": "unknown",
                    "condition": "unknown",
                    "confidence": "0.00",
                    "needs_manual_review": "true",
                }
            )
            continue

        data = cache.get(file_hash)
        if data is None:
            if client is None:
                print(f"Error: cannot analyze {image_path.name} without OpenAI client and cache.")
                listing_text_entries.append(f"=== {image_path.name} ===\n\nFAILED\n")
                csv_rows.append(
                    {
                        "filename": image_path.name,
                        "title": "FAILED",
                        "description": "FAILED",
                        "tags": "",
                        "type": "unknown",
                        "condition": "unknown",
                        "confidence": "0.00",
                        "needs_manual_review": "true",
                    }
                )
                continue

            try:
                data = analyze_image(client, image_path, model)
                cache[file_hash] = data
            except Exception as exc:
                print(f"Error analyzing {image_path.name}: {exc}")
                listing_text_entries.append(f"=== {image_path.name} ===\n\nFAILED\n")
                csv_rows.append(
                    {
                        "filename": image_path.name,
                        "title": "FAILED",
                        "description": "FAILED",
                        "tags": "",
                        "type": "unknown",
                        "condition": "unknown",
                        "confidence": "0.00",
                        "needs_manual_review": "true",
                    }
                )
                continue
        else:
            data = normalize_response(data)

        if not is_recognized(data):
            print(f"-> FAILED recognition for {image_path.name}")
            listing_text_entries.append(f"=== {image_path.name} ===\n\nFAILED\n")
            csv_rows.append(
                {
                    "filename": image_path.name,
                    "title": "FAILED",
                    "description": "FAILED",
                    "tags": ", ".join(data.get("tags") or []),
                    "type": str(data.get("type") or "unknown"),
                    "condition": str(data.get("condition") or "unknown"),
                    "confidence": f"{to_float(data.get('confidence'), 0.0):.2f}",
                    "needs_manual_review": "true",
                }
            )
            continue

        target_filename = build_target_filename(image_path, data, file_hash)
        target_path = INPUT_DIR / target_filename
        target_path = make_unique_path(target_path, image_path)

        final_path = image_path
        if target_path != image_path:
            try:
                image_path.rename(target_path)
                final_path = target_path
                print(f"-> renamed to {final_path.name}")
            except Exception as exc:
                print(f"Error renaming {image_path.name}: {exc}")
        else:
            print(f"-> filename unchanged: {final_path.name}")

        listing_text_entries.append(format_listing_entry(final_path.name, data))
        csv_rows.append(
            {
                "filename": final_path.name,
                "title": str(data.get("title_pl") or ""),
                "description": flatten_text(str(data.get("description_pl") or "")),
                "tags": ", ".join(data.get("tags") or []),
                "type": str(data.get("type") or "unknown"),
                "condition": str(data.get("condition") or "unknown"),
                "confidence": f"{to_float(data.get('confidence'), 0.0):.2f}",
                "needs_manual_review": "true"
                if bool_value(data.get("needs_manual_review"))
                or to_float(data.get("confidence"), 0.0) < 0.7
                else "false",
            }
        )

    (OUTPUT_DIR / "listings.txt").write_text(
        "\n".join(listing_text_entries).rstrip() + "\n",
        encoding="utf-8",
    )

    with (OUTPUT_DIR / "listings.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "filename",
                "title",
                "description",
                "tags",
                "type",
                "condition",
                "confidence",
                "needs_manual_review",
            ],
        )
        writer.writeheader()
        writer.writerows(csv_rows)

    save_cache(cache)
    print(f"Done. Processed {len(input_files)} files.")
    return 0


def main() -> int:
    try:
        return process_images()
    except Exception as exc:
        print(f"Fatal error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
