from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List

from .genshin_importer import BANNER_NAMES


STANDARD_FIVE_STAR_CHARACTERS = {
    "迪卢克",
    "琴",
    "刻晴",
    "莫娜",
    "七七",
    "提纳里",
    "迪希雅",
}
CHARACTER_EVENT_TYPES = {"301", "400"}
WEAPON_EVENT_TYPES = {"302"}
FOCUS_RANK_TYPES = {"4", "5"}


def _sort_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(records, key=lambda record: (str(record.get("time", "")), str(record.get("record_id", ""))))


def _banner_name(gacha_type: str) -> str:
    gacha_type = str(gacha_type or "")
    if gacha_type in CHARACTER_EVENT_TYPES:
        return "角色活动祈愿"
    return BANNER_NAMES.get(gacha_type, gacha_type or "未知卡池")


def _pity_group(gacha_type: str) -> str:
    gacha_type = str(gacha_type or "")
    if gacha_type in CHARACTER_EVENT_TYPES:
        return "角色活动祈愿"
    if gacha_type in WEAPON_EVENT_TYPES:
        return "武器活动祈愿"
    if gacha_type == "200":
        return "常驻祈愿"
    if gacha_type == "100":
        return "新手祈愿"
    if gacha_type == "500":
        return "集录祈愿"
    return _banner_name(gacha_type)


def _five_star_label(record: Dict[str, Any]) -> str:
    gacha_type = str(record.get("gacha_type", "") or "")
    name = str(record.get("name", "") or "")
    item_type = str(record.get("item_type", "") or "")
    if item_type == "角色" and gacha_type in CHARACTER_EVENT_TYPES:
        return "限定角色" if name not in STANDARD_FIVE_STAR_CHARACTERS else "常驻歪出"
    if item_type == "角色" and gacha_type == "200":
        return "常驻角色"
    if item_type == "武器" and gacha_type in WEAPON_EVENT_TYPES:
        return "定轨武器池"
    if item_type == "武器" and gacha_type == "200":
        return "常驻武器"
    return f"{item_type}五星"


def build_overview(records: List[Dict[str, Any]], latest_import: Dict[str, Any] | None = None, import_count: int = 0) -> Dict[str, Any]:
    ordered = _sort_records(records)
    banner_counter = Counter(str(record.get("gacha_type", "") or "") for record in ordered)
    rank_counter = Counter(str(record.get("rank_type", "") or "") for record in ordered)
    five_star_names = Counter(
        str(record.get("name", "") or "")
        for record in ordered
        if str(record.get("rank_type", "") or "") == "5"
    )
    latest_source = latest_import.get("source_path", "") if latest_import else ""
    return {
        "total_records": len(ordered),
        "five_star_count": rank_counter.get("5", 0),
        "four_star_count": rank_counter.get("4", 0),
        "three_star_count": rank_counter.get("3", 0),
        "banner_counts": { _banner_name(code): count for code, count in sorted(banner_counter.items()) },
        "earliest_time": ordered[0].get("time", "") if ordered else "",
        "latest_time": ordered[-1].get("time", "") if ordered else "",
        "latest_source_path": latest_source,
        "latest_imported_at": latest_import.get("imported_at", "") if latest_import else "",
        "import_count": import_count,
        "top_five_star": five_star_names.most_common(1)[0] if five_star_names else ("-", 0),
    }


def build_five_star_history(records: List[Dict[str, Any]], limit: int = 120) -> List[Dict[str, Any]]:
    pity_counters: Dict[str, int] = defaultdict(int)
    history: List[Dict[str, Any]] = []
    for record in _sort_records(records):
        group = _pity_group(str(record.get("gacha_type", "") or ""))
        pity_counters[group] += int(record.get("count", 1) or 1)
        if str(record.get("rank_type", "") or "") != "5":
            continue
        history.append(
            {
                "time": str(record.get("time", "") or ""),
                "name": str(record.get("name", "") or ""),
                "item_type": str(record.get("item_type", "") or ""),
                "banner": _banner_name(str(record.get("gacha_type", "") or "")),
                "pity_group": group,
                "pity": pity_counters[group],
                "label": _five_star_label(record),
            }
        )
        pity_counters[group] = 0
    return list(reversed(history[-limit:]))


def build_limited_character_stats(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in _sort_records(records):
        if str(record.get("item_type", "") or "") != "角色":
            continue
        if str(record.get("rank_type", "") or "") != "5":
            continue
        if str(record.get("gacha_type", "") or "") not in CHARACTER_EVENT_TYPES:
            continue
        name = str(record.get("name", "") or "")
        if name in STANDARD_FIVE_STAR_CHARACTERS:
            continue
        grouped[name].append(record)

    rows: List[Dict[str, Any]] = []
    for name, items in grouped.items():
        rows.append(
            {
                "name": name,
                "copies": len(items),
                "constellation": f"C{min(len(items) - 1, 6)}",
                "first_time": items[0].get("time", ""),
                "last_time": items[-1].get("time", ""),
            }
        )
    rows.sort(key=lambda item: (-item["copies"], item["last_time"], item["name"]))
    return rows


def build_character_constellations(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for record in _sort_records(records):
        if str(record.get("item_type", "") or "") != "角色":
            continue
        rank = str(record.get("rank_type", "") or "")
        if rank not in FOCUS_RANK_TYPES:
            continue
        grouped[(str(record.get("name", "") or ""), rank)].append(record)

    rows: List[Dict[str, Any]] = []
    for (name, rank), items in grouped.items():
        copies = len(items)
        rows.append(
            {
                "name": name,
                "rank": rank,
                "copies": copies,
                "constellation": f"C{min(max(copies - 1, 0), 6)}",
                "extra": max(copies - 7, 0),
                "last_time": items[-1].get("time", ""),
            }
        )
    rows.sort(key=lambda item: (-int(item["rank"]), -item["copies"], item["name"]))
    return rows


def build_weapon_refinements(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for record in _sort_records(records):
        if str(record.get("item_type", "") or "") != "武器":
            continue
        rank = str(record.get("rank_type", "") or "")
        if rank not in FOCUS_RANK_TYPES:
            continue
        grouped[(str(record.get("name", "") or ""), rank)].append(record)

    rows: List[Dict[str, Any]] = []
    for (name, rank), items in grouped.items():
        copies = len(items)
        rows.append(
            {
                "name": name,
                "rank": rank,
                "copies": copies,
                "refinement": f"R{min(copies, 5)}",
                "extra": max(copies - 5, 0),
                "last_time": items[-1].get("time", ""),
            }
        )
    rows.sort(key=lambda item: (-int(item["rank"]), -item["copies"], item["name"]))
    return rows
