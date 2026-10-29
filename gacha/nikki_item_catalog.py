from __future__ import annotations

import json
import logging
import time
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable

from ..settings.paths import ARTIFACTS_DIR

LOGGER = logging.getLogger("GameAutomation.Gacha")


CACHE_DIR = ARTIFACTS_DIR / "nikki_catalog"
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 8.0
REQUEST_HEADERS = {
    "User-Agent": "game_auto/1.0",
    "Accept": "application/json, text/plain, */*",
}

ITEM_LOCALE_URLS = (
    "https://raw.githubusercontent.com/dastrokes/gongeo.us-nikki-tracker/master/app/locales/zh/item.json",
    "https://cdn.jsdelivr.net/gh/dastrokes/gongeo.us-nikki-tracker@master/app/locales/zh/item.json",
)
OUTFIT_LOCALE_URLS = (
    "https://raw.githubusercontent.com/dastrokes/gongeo.us-nikki-tracker/master/app/locales/zh/outfit.json",
    "https://cdn.jsdelivr.net/gh/dastrokes/gongeo.us-nikki-tracker@master/app/locales/zh/outfit.json",
)

ITEM_TYPE_MAP = {
    "10": "hair",
    "90": "dresses",
    "20": "outerwear",
    "30": "tops",
    "41": "bottoms",
    "50": "socks",
    "60": "shoes",
    "71": "hairAccessories",
    "72": "headwear",
    "73": "earrings",
    "74": "neckwear",
    "75": "bracelets",
    "76": "chokers",
    "77": "gloves",
    "78": "handhelds",
    "79": "bodyPaint",
    "92": "faceDecorations",
    "93": "chestAccessories",
    "94": "pendants",
    "95": "backpieces",
    "96": "rings",
    "97": "armDecorations",
    "98": "chokers",
    "81": "baseMakeup",
    "82": "eyebrows",
    "83": "eyelashes",
    "84": "contactLenses",
    "85": "lips",
}

ITEM_TYPE_LABELS = {
    "hair": "发型",
    "dresses": "连衣裙",
    "outerwear": "外套",
    "tops": "上装",
    "bottoms": "下装",
    "socks": "袜子",
    "shoes": "鞋子",
    "hairAccessories": "发饰",
    "headwear": "头饰",
    "earrings": "耳饰",
    "neckwear": "颈饰",
    "bracelets": "手饰",
    "chokers": "颈链",
    "gloves": "手套",
    "handhelds": "手持物",
    "bodyPaint": "纹身",
    "faceDecorations": "面饰",
    "chestAccessories": "胸饰",
    "pendants": "挂饰",
    "backpieces": "背饰",
    "rings": "戒指",
    "armDecorations": "臂饰",
    "baseMakeup": "妆容",
    "eyebrows": "眉形",
    "eyelashes": "睫毛",
    "contactLenses": "美瞳",
    "lips": "唇妆",
    "unknown": "未知部位",
}

ITEM_VARIANT_SUFFIX_BY_PREFIX = {
    "1022": "01",
    "1026": "01",
    "1023": "02",
    "1024": "03",
    "1025": "04",
}


def _cache_path(kind: str) -> Path:
    return CACHE_DIR / f"zh_{kind}.json"


def _read_json_file(file_path: Path) -> Dict[str, Any]:
    return json.loads(file_path.read_text(encoding="utf-8"))


def _write_json_file(file_path: Path, payload: Dict[str, Any]):
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _is_cache_fresh(file_path: Path) -> bool:
    return file_path.exists() and (time.time() - file_path.stat().st_mtime) <= CACHE_TTL_SECONDS


def _download_json(urls: Iterable[str]) -> Dict[str, Any]:
    last_error: Exception | None = None
    for url in urls:
        try:
            request = urllib.request.Request(url, headers=REQUEST_HEADERS)
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return json.loads(response.read().decode(charset))
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return {}


def _load_locale_payload(kind: str, urls: Iterable[str]) -> Dict[str, Any]:
    cache_file = _cache_path(kind)
    if _is_cache_fresh(cache_file):
        try:
            return _read_json_file(cache_file)
        except Exception as exc:
            LOGGER.debug("[图鉴] 读取本地缓存失败，改为联网刷新: %s", exc)

    try:
        payload = _download_json(urls)
    except Exception:
        if cache_file.exists():
            try:
                return _read_json_file(cache_file)
            except Exception:
                return {}
        return {}

    if isinstance(payload, dict):
        try:
            _write_json_file(cache_file, payload)
        except Exception as exc:
            LOGGER.debug("[图鉴] 写入本地缓存失败: %s", exc)
        return payload
    return {}


def _extract_name_map(payload: Dict[str, Any], prefix: str) -> Dict[str, str]:
    marker = f"{prefix}."
    suffix = ".name"
    name_map: Dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(value, str) or not key.startswith(marker) or not key.endswith(suffix):
            continue
        name_map[key[len(marker) : -len(suffix)]] = value
    return name_map


@lru_cache(maxsize=1)
def load_item_catalog() -> Dict[str, Dict[str, str]]:
    item_payload = _load_locale_payload("item", ITEM_LOCALE_URLS)
    outfit_payload = _load_locale_payload("outfit", OUTFIT_LOCALE_URLS)
    return {
        "item_names": _extract_name_map(item_payload, "item"),
        "outfit_names": _extract_name_map(outfit_payload, "outfit"),
    }


def get_item_type_key(item_id: str | int) -> str:
    item_id_str = str(item_id)
    if item_id_str == "1020790033":
        return "rings"
    if len(item_id_str) >= 6:
        return ITEM_TYPE_MAP.get(item_id_str[4:6], "unknown")
    return "unknown"


def derive_outfit_id(item_id: str | int) -> str:
    item_id_str = str(item_id)
    if len(item_id_str) != 10 or not item_id_str.isdigit():
        return ""
    base_outfit_id = f"1{item_id_str[-4:]}"
    variant_suffix = ITEM_VARIANT_SUFFIX_BY_PREFIX.get(item_id_str[:4], "")
    return f"{base_outfit_id}{variant_suffix}" if variant_suffix else base_outfit_id


@lru_cache(maxsize=8192)
def resolve_item_metadata(item_id: str | int) -> Dict[str, str]:
    item_id_str = str(item_id)
    catalog = load_item_catalog()
    item_name = catalog["item_names"].get(item_id_str, "")
    outfit_id = derive_outfit_id(item_id_str)
    outfit_name = catalog["outfit_names"].get(outfit_id, "")
    item_type = get_item_type_key(item_id_str)
    item_type_label = ITEM_TYPE_LABELS.get(item_type, ITEM_TYPE_LABELS["unknown"])

    if item_name:
        display_name = item_name
        display_name_with_type = f"{item_name} · {item_type_label}"
    elif outfit_name:
        display_name = f"{outfit_name} · {item_type_label}"
        display_name_with_type = display_name
    else:
        display_name = f"ID {item_id_str}"
        display_name_with_type = display_name

    subtitle_parts = []
    if outfit_name:
        subtitle_parts.append(outfit_name)
    if item_type_label:
        subtitle_parts.append(item_type_label)
    subtitle_parts.append(f"ID {item_id_str}")

    return {
        "item_id": item_id_str,
        "item_name": item_name,
        "item_type": item_type,
        "item_type_label": item_type_label,
        "outfit_id": outfit_id,
        "outfit_name": outfit_name,
        "display_name": display_name,
        "display_name_with_type": display_name_with_type,
        "subtitle": " / ".join(part for part in subtitle_parts if part),
    }