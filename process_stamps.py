#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import shutil
import statistics
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

try:
    import requests
except Exception:
    requests = None


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
CACHE_DIR = BASE_DIR / "cache"
CACHE_FILE = CACHE_DIR / "hash.json"
PROCESSED_INDEX_FILE = CACHE_DIR / "processed_index.json"
PRICING_CACHE_FILE = CACHE_DIR / "pricing.json"
ORIGINAL_HEIC_DIR = INPUT_DIR / "original_heic"

PROCESSABLE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ALLOWED_TYPES = {"single", "series", "block", "sheet", "fdc", "unknown"}
ALLOWED_CONDITIONS = {"mint", "used", "cto", "unknown"}

DEFAULT_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini")
MAX_FILENAME_LENGTH = 120
PRICING_CACHE_TTL_DAYS = int(os.getenv("PRICING_CACHE_TTL_DAYS", "7"))
ALLEGRO_API_BASE = os.getenv("ALLEGRO_API_BASE", "https://api.allegro.pl").rstrip("/")
ALLEGRO_TOKEN_URL = os.getenv("ALLEGRO_TOKEN_URL", "https://allegro.pl/auth/oauth/token")
ALLEGRO_LISTING_LIMIT = int(os.getenv("ALLEGRO_LISTING_LIMIT", "20"))
CONDITION_PRICE_FACTOR = {
    "mint": 1.12,
    "used": 0.95,
    "cto": 0.9,
    "unknown": 0.85,
}
CSV_HEADERS_PL = [
    "nazwa_pliku",
    "tytul",
    "opis",
    "tagi",
    "typ",
    "stan",
    "pewnosc",
    "wymaga_weryfikacji",
    "cena_sugerowana_pln",
    "zakres_ceny_pln",
    "zrodlo_ceny",
    "pewnosc_ceny",
    "wymaga_recznej_wyceny",
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


def ensure_directories() -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not CACHE_FILE.exists():
        CACHE_FILE.write_text("{}", encoding="utf-8")
    if not PROCESSED_INDEX_FILE.exists():
        PROCESSED_INDEX_FILE.write_text("{}", encoding="utf-8")
    if not PRICING_CACHE_FILE.exists():
        PRICING_CACHE_FILE.write_text("{}", encoding="utf-8")


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


def load_pricing_cache() -> dict[str, dict[str, Any]]:
    if not PRICING_CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(PRICING_CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        parsed: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, dict):
                parsed[key] = value
        return parsed
    except Exception as exc:
        print(f"Warning: failed to load pricing cache file {PRICING_CACHE_FILE.name}: {exc}")
        return {}


def save_pricing_cache(pricing_cache: dict[str, dict[str, Any]]) -> None:
    tmp_file = PRICING_CACHE_FILE.with_suffix(".tmp")
    tmp_file.write_text(
        json.dumps(pricing_cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_file.replace(PRICING_CACHE_FILE)


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
        return value.strip().lower() in {"true", "1", "yes", "y", "tak", "t"}
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


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_pln(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    text = re.sub(r"[^0-9.]", "", text)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def build_price_query(data: dict[str, Any]) -> str:
    parts = [
        str(data.get("country") or "").strip(),
        str(data.get("era") or "").strip(),
        str(data.get("year") or "").strip(),
        str(data.get("topic") or data.get("series_name") or "").strip(),
        str(TYPE_TO_PL.get(str(data.get("type") or "unknown"), "")).strip(),
        "znaczek",
    ]
    normalized_parts = [p for p in parts if p]
    query = " ".join(normalized_parts)
    query = re.sub(r"\s+", " ", query).strip()
    return query[:120]


def parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def price_cache_fresh(entry: dict[str, Any], ttl_days: int) -> bool:
    created_at = parse_iso_datetime(entry.get("created_at"))
    if created_at is None:
        return False
    age = datetime.now(timezone.utc) - created_at
    return age.total_seconds() <= max(1, ttl_days) * 86400


def extract_allegro_prices(payload: Any) -> list[float]:
    prices: list[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            selling_mode = node.get("sellingMode")
            if isinstance(selling_mode, dict):
                price_obj = selling_mode.get("price")
                if isinstance(price_obj, dict):
                    currency = str(price_obj.get("currency") or "").upper().strip()
                    amount = to_pln(price_obj.get("amount"))
                    if currency == "PLN" and amount is not None and amount > 0:
                        prices.append(amount)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    unique_sorted = sorted(set(round(x, 2) for x in prices))
    return unique_sorted


def get_allegro_access_token(token_state: dict[str, Any]) -> str | None:
    cached_token = str(token_state.get("access_token") or "").strip()
    expires_at = parse_iso_datetime(token_state.get("expires_at"))
    if cached_token and expires_at is not None:
        if (expires_at - datetime.now(timezone.utc)).total_seconds() > 30:
            return cached_token

    if requests is None:
        return None

    client_id = os.getenv("ALLEGRO_CLIENT_ID")
    client_secret = os.getenv("ALLEGRO_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None

    try:
        response = requests.post(
            ALLEGRO_TOKEN_URL,
            params={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if response.status_code >= 400:
            print(
                f"Warning: Allegro token request failed ({response.status_code}): {response.text[:240]}"
            )
            return None
        payload = response.json()
        token = str(payload.get("access_token") or "").strip()
        expires_in = int(payload.get("expires_in") or 0)
        if not token or expires_in <= 0:
            return None
        token_state["access_token"] = token
        token_state["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=max(30, expires_in - 30))
        ).isoformat()
        return token
    except Exception as exc:
        print(f"Warning: Allegro token request error: {exc}")
        return None


def fetch_allegro_market_prices(query: str, token_state: dict[str, Any]) -> tuple[list[float], str]:
    if requests is None:
        return ([], "brak_requests")
    token = get_allegro_access_token(token_state)
    if not token:
        return ([], "brak_konfiguracji_allegro")

    url = f"{ALLEGRO_API_BASE}/offers/listing"
    params = {"phrase": query, "limit": max(5, min(ALLEGRO_LISTING_LIMIT, 60))}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.allegro.public.v1+json",
    }
    try:
        response = requests.get(url, params=params, headers=headers, timeout=20)
        if response.status_code >= 400:
            print(
                f"Warning: Allegro listing request failed ({response.status_code}): {response.text[:240]}"
            )
            if response.status_code == 403:
                return ([], "allegro_brak_uprawnien_aplikacji")
            return ([], "allegro_blad_api")
        payload = response.json()
        prices = extract_allegro_prices(payload)
        if not prices:
            return ([], "allegro_brak_ofert")
        return (prices, "allegro_api")
    except Exception as exc:
        print(f"Warning: Allegro listing request error: {exc}")
        return ([], "allegro_blad_sieci")


def trimmed_prices(values: list[float]) -> list[float]:
    ordered = sorted(values)
    if len(ordered) < 10:
        return ordered
    trim_size = max(1, int(len(ordered) * 0.1))
    if trim_size * 2 >= len(ordered):
        return ordered
    return ordered[trim_size:-trim_size]


def format_price(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"


def pricing_unavailable_result(source: str, query: str) -> dict[str, Any]:
    return {
        "cena_sugerowana_pln": "",
        "zakres_ceny_pln": "",
        "zrodlo_ceny": source,
        "pewnosc_ceny": "0.00",
        "wymaga_recznej_wyceny": True,
        "liczba_ofert_cenowych": 0,
        "zapytanie_cenowe": query,
    }


def compute_price_result(data: dict[str, Any], prices: list[float], source: str, query: str) -> dict[str, Any]:
    if not prices:
        return pricing_unavailable_result(source, query)

    selected = trimmed_prices(prices)
    if not selected:
        return pricing_unavailable_result(source, query)

    median_base = statistics.median(selected)
    min_base = min(selected)
    max_base = max(selected)

    condition = str(data.get("condition") or "unknown")
    confidence_model = to_float(data.get("confidence"), 0.0)
    factor_condition = CONDITION_PRICE_FACTOR.get(condition, CONDITION_PRICE_FACTOR["unknown"])
    factor_confidence = 0.85 + 0.15 * confidence_model
    suggested = median_base * factor_condition * factor_confidence

    low = max(0.01, min_base * factor_condition * 0.95)
    high = max(low, max_base * factor_condition * 1.05)
    confidence_price = min(
        0.99,
        0.45 + min(len(selected), 20) / 40.0 + confidence_model * 0.2,
    )

    return {
        "cena_sugerowana_pln": format_price(suggested),
        "zakres_ceny_pln": f"{low:.2f}-{high:.2f}",
        "zrodlo_ceny": source,
        "pewnosc_ceny": f"{confidence_price:.2f}",
        "wymaga_recznej_wyceny": False,
        "liczba_ofert_cenowych": len(selected),
        "zapytanie_cenowe": query,
    }


def get_price_suggestion(
    data: dict[str, Any],
    pricing_cache: dict[str, dict[str, Any]],
    token_state: dict[str, Any],
    force_refresh: bool = False,
) -> dict[str, Any]:
    query = build_price_query(data)
    if not query:
        return pricing_unavailable_result("brak_zapytania", "")

    cache_key = hashlib.sha1(query.encode("utf-8")).hexdigest()
    cached_entry = pricing_cache.get(cache_key)
    if (
        not force_refresh
        and isinstance(cached_entry, dict)
        and price_cache_fresh(cached_entry, PRICING_CACHE_TTL_DAYS)
    ):
        cached_result = cached_entry.get("result")
        if isinstance(cached_result, dict):
            return cached_result

    prices, source = fetch_allegro_market_prices(query, token_state)
    result = compute_price_result(data, prices, source, query)
    pricing_cache[cache_key] = {
        "created_at": now_utc_iso(),
        "query": query,
        "result": result,
    }
    return result


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


def format_listing_entry(
    filename: str,
    data: dict[str, Any],
    pricing: dict[str, Any],
) -> str:
    tags = ", ".join(data.get("tags") or [])
    confidence = to_float(data.get("confidence"), 0.0)
    review = bool_value(data.get("needs_manual_review")) or confidence < 0.7
    stype = TYPE_TO_PL.get(str(data.get("type") or "unknown"), "nieznany")
    condition = CONDITION_TO_PL.get(
        str(data.get("condition") or "unknown"), "nieznany"
    )
    price_value = str(pricing.get("cena_sugerowana_pln") or "")
    price_range = str(pricing.get("zakres_ceny_pln") or "")
    price_source = str(pricing.get("zrodlo_ceny") or "")
    price_confidence = str(pricing.get("pewnosc_ceny") or "0.00")
    manual_price = "tak" if bool_value(pricing.get("wymaga_recznej_wyceny")) else "nie"

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
        "CENA_SUGEROWANA_PLN:",
        price_value,
        "",
        "ZAKRES_CENY_PLN:",
        price_range,
        "",
        "ZRODLO_CENY:",
        price_source,
        "",
        "PEWNOSC_CENY:",
        price_confidence,
        "",
        "WYMAGA_RECZNEJ_WYCENY:",
        manual_price,
        "",
    ]
    return "\n".join(lines)


def flatten_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def failed_listing_entry(filename: str) -> str:
    return (
        f"=== {filename} ===\n\n"
        "NIEPOWODZENIE\n\n"
        "CENA_SUGEROWANA_PLN:\n\n\n"
        "ZAKRES_CENY_PLN:\n\n\n"
        "ZRODLO_CENY:\nbrak\n\n"
        "PEWNOSC_CENY:\n0.00\n\n"
        "WYMAGA_RECZNEJ_WYCENY:\ntak\n"
    )


def failed_csv_row(filename: str) -> dict[str, str]:
    return {
        "nazwa_pliku": filename,
        "tytul": "NIEPOWODZENIE",
        "opis": "Nie udalo sie sklasyfikowac znaczka.",
        "tagi": "",
        "typ": "nieznany",
        "stan": "nieznany",
        "pewnosc": "0.00",
        "wymaga_weryfikacji": "tak",
        "cena_sugerowana_pln": "",
        "zakres_ceny_pln": "",
        "zrodlo_ceny": "brak",
        "pewnosc_ceny": "0.00",
        "wymaga_recznej_wyceny": "tak",
    }


def success_csv_row(
    filename: str,
    data: dict[str, Any],
    pricing: dict[str, Any],
) -> dict[str, str]:
    confidence = to_float(data.get("confidence"), 0.0)
    needs_review = bool_value(data.get("needs_manual_review")) or confidence < 0.7
    return {
        "nazwa_pliku": filename,
        "tytul": str(data.get("title_pl") or ""),
        "opis": flatten_text(str(data.get("description_pl") or "")),
        "tagi": ", ".join(data.get("tags") or []),
        "typ": TYPE_TO_PL.get(str(data.get("type") or "unknown"), "nieznany"),
        "stan": CONDITION_TO_PL.get(str(data.get("condition") or "unknown"), "nieznany"),
        "pewnosc": f"{confidence:.2f}",
        "wymaga_weryfikacji": "tak" if needs_review else "nie",
        "cena_sugerowana_pln": str(pricing.get("cena_sugerowana_pln") or ""),
        "zakres_ceny_pln": str(pricing.get("zakres_ceny_pln") or ""),
        "zrodlo_ceny": str(pricing.get("zrodlo_ceny") or ""),
        "pewnosc_ceny": str(pricing.get("pewnosc_ceny") or "0.00"),
        "wymaga_recznej_wyceny": "tak"
        if bool_value(pricing.get("wymaga_recznej_wyceny"))
        else "nie",
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
        "--pricing-force-refresh",
        action="store_true",
        help="Pomin cache cen i pobierz ceny internetowe ponownie.",
    )
    parser.add_argument(
        "--no-online-pricing",
        action="store_true",
        help="Wylacz pobieranie cen z internetu i ustaw tylko flage recznej wyceny.",
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
) -> None:
    processed_index[file_hash] = {
        "file_hash": file_hash,
        "filename": source_filename,
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


def write_stamp_output(image_path: Path, listing_text: str) -> None:
    try:
        folder_base = safe_ascii(image_path.stem, fallback="znaczek")
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
        shutil.copy2(image_path, target_dir / image_path.name)
    except Exception as exc:
        print(f"Error writing output package for {image_path.name}: {exc}")


def process_images(args: argparse.Namespace) -> int:
    ensure_directories()
    convert_heic_files()
    prepare_output_stamp_dirs()

    cache = load_cache()
    processed_index = load_processed_index()
    pricing_cache = load_pricing_cache()
    client = get_openai_client()
    model = DEFAULT_MODEL
    allegro_token_state: dict[str, Any] = {}

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

    def resolve_price(data: dict[str, Any]) -> dict[str, Any]:
        query = build_price_query(data)
        if args.no_online_pricing:
            return pricing_unavailable_result("wylaczone", query)
        return get_price_suggestion(
            data=data,
            pricing_cache=pricing_cache,
            token_state=allegro_token_state,
            force_refresh=args.pricing_force_refresh,
        )

    for image_path in input_files:
        print(f"Processing {image_path.name}")
        try:
            file_hash = sha1_of_file(image_path)
        except Exception as exc:
            print(f"Error hashing file {image_path.name}: {exc}")
            failed_entry = failed_listing_entry(image_path.name)
            write_stamp_output(image_path, failed_entry)
            csv_rows.append(failed_csv_row(image_path.name))
            continue

        indexed_record = processed_index.get(file_hash, {})
        existing_status = str(indexed_record.get("status") or "").strip().lower()
        if existing_status and not should_retry_by_status(existing_status, args):
            if existing_status == "done":
                print(f"Skipping {image_path.name} (already processed)")
                cached_data = cache.get(file_hash)
                if cached_data is None:
                    print(
                        f"Warning: missing cache for skipped file {image_path.name}, marking as failed."
                    )
                    failed_entry = failed_listing_entry(image_path.name)
                    write_stamp_output(image_path, failed_entry)
                    csv_rows.append(failed_csv_row(image_path.name))
                else:
                    data = normalize_response(cached_data)
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
                    pricing = resolve_price(data)
                    listing_entry = format_listing_entry(final_path.name, data, pricing)
                    write_stamp_output(final_path, listing_entry)
                    csv_rows.append(success_csv_row(final_path.name, data, pricing))
                    upsert_processed_record(
                        processed_index=processed_index,
                        file_hash=file_hash,
                        source_filename=image_path.name,
                        output_filename=final_path.name,
                        status=processed_status(data),
                    )
                continue

            if existing_status == "failed":
                print(f"Skipping {image_path.name} (failed before, use --retry-failed)")
                failed_entry = failed_listing_entry(image_path.name)
                write_stamp_output(image_path, failed_entry)
                csv_rows.append(failed_csv_row(image_path.name))
                continue

            if existing_status == "review":
                print(f"Skipping {image_path.name} (review before, use --recheck-review)")
                cached_data = cache.get(file_hash)
                if cached_data is None:
                    failed_entry = failed_listing_entry(image_path.name)
                    write_stamp_output(image_path, failed_entry)
                    csv_rows.append(failed_csv_row(image_path.name))
                else:
                    data = normalize_response(cached_data)
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
                    pricing = resolve_price(data)
                    listing_entry = format_listing_entry(final_path.name, data, pricing)
                    write_stamp_output(final_path, listing_entry)
                    csv_rows.append(success_csv_row(final_path.name, data, pricing))
                    upsert_processed_record(
                        processed_index=processed_index,
                        file_hash=file_hash,
                        source_filename=image_path.name,
                        output_filename=final_path.name,
                        status=processed_status(data),
                    )
                continue

        if args.force:
            print(f"Reprocessing {image_path.name} (force)")
        elif existing_status == "failed" and args.retry_failed:
            print(f"Reprocessing {image_path.name} (retry failed)")
        elif existing_status == "review" and args.recheck_review:
            print(f"Reprocessing {image_path.name} (recheck review)")
        elif existing_status == "done" and args.no_skip_processed:
            print(f"Reprocessing {image_path.name} (no skip processed)")

        data = cache.get(file_hash)
        if data is None:
            if client is None:
                print(f"Error: cannot analyze {image_path.name} without OpenAI client and cache.")
                failed_entry = failed_listing_entry(image_path.name)
                write_stamp_output(image_path, failed_entry)
                csv_rows.append(failed_csv_row(image_path.name))
                upsert_processed_record(
                    processed_index=processed_index,
                    file_hash=file_hash,
                    source_filename=image_path.name,
                    output_filename=image_path.name,
                    status="failed",
                )
                continue

            try:
                data = analyze_image(client, image_path, model)
                cache[file_hash] = data
            except Exception as exc:
                print(f"Error analyzing {image_path.name}: {exc}")
                failed_entry = failed_listing_entry(image_path.name)
                write_stamp_output(image_path, failed_entry)
                csv_rows.append(failed_csv_row(image_path.name))
                upsert_processed_record(
                    processed_index=processed_index,
                    file_hash=file_hash,
                    source_filename=image_path.name,
                    output_filename=image_path.name,
                    status="failed",
                )
                continue
        else:
            data = normalize_response(data)

        if not is_recognized(data):
            print(f"-> FAILED recognition for {image_path.name}")
            failed_entry = failed_listing_entry(image_path.name)
            write_stamp_output(image_path, failed_entry)
            csv_rows.append(failed_csv_row(image_path.name))
            upsert_processed_record(
                processed_index=processed_index,
                file_hash=file_hash,
                source_filename=image_path.name,
                output_filename=image_path.name,
                status="failed",
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

        pricing = resolve_price(data)
        listing_entry = format_listing_entry(final_path.name, data, pricing)
        write_stamp_output(final_path, listing_entry)
        csv_rows.append(success_csv_row(final_path.name, data, pricing))
        upsert_processed_record(
            processed_index=processed_index,
            file_hash=file_hash,
            source_filename=image_path.name,
            output_filename=final_path.name,
            status=processed_status(data),
        )

    with (OUTPUT_DIR / "listings.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS_PL)
        writer.writeheader()
        writer.writerows(csv_rows)

    save_cache(cache)
    save_processed_index(processed_index)
    save_pricing_cache(pricing_cache)
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
