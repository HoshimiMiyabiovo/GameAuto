from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Set, Tuple

from .hoyo_importer import (
    HOYO_GACHA_GAME_CONFIGS,
    get_banner_name,
    get_game_config,
)


def _sort_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(records, key=lambda record: (str(record.get("time", "")), str(record.get("record_id", ""))))


def _format_percent(count: int, total: int) -> str:
    if total <= 0:
        return "0"
    value = count * 100 / total
    return f"{value:.2f}".rstrip("0").rstrip(".")


def get_rank_order(game_code: str) -> List[str]:
    config = get_game_config(game_code)
    labels = config.get("rank_labels", {})
    return sorted((str(rank) for rank in labels.keys()), key=lambda rank: int(rank), reverse=True)


def get_rank_label(game_code: str, rank: str) -> str:
    config = get_game_config(game_code)
    labels = config.get("rank_labels", {})
    rank_str = str(rank or "")
    return str(labels.get(rank_str, f"{rank_str}星"))


def get_high_rarity_rank(game_code: str) -> str:
    return str(get_game_config(game_code).get("high_rarity_rank", "5") or "5")


def get_high_rarity_label(game_code: str) -> str:
    return get_rank_label(game_code, get_high_rarity_rank(game_code))


def _pity_group_code(game_code: str, gacha_type: str) -> str:
    config = get_game_config(game_code)
    pity_groups = config.get("pity_groups", {})
    gacha_type = str(gacha_type or "")
    return str(pity_groups.get(gacha_type, gacha_type or ""))


def _pity_group_name(game_code: str, gacha_type: str) -> str:
    return get_banner_name(game_code, _pity_group_code(game_code, gacha_type))


def _item_types(game_code: str, key: str) -> Set[str]:
    config = get_game_config(game_code)
    return {str(item_type) for item_type in config.get(key, set())}


def build_overview(
    records: List[Dict[str, Any]],
    game_code: str,
    latest_import: Dict[str, Any] | None = None,
    import_count: int = 0,
) -> Dict[str, Any]:
    ordered = _sort_records(records)
    high_rank = get_high_rarity_rank(game_code)
    high_label = get_high_rarity_label(game_code)
    banner_counter = Counter(str(record.get("gacha_type", "") or "") for record in ordered)
    rank_counter = Counter(str(record.get("rank_type", "") or "") for record in ordered)
    high_names = Counter(
        str(record.get("name", "") or "")
        for record in ordered
        if str(record.get("rank_type", "") or "") == high_rank
    )
    return {
        "total_records": len(ordered),
        "high_rarity_rank": high_rank,
        "high_rarity_label": high_label,
        "high_rarity_count": rank_counter.get(high_rank, 0),
        "rank_counts": dict(rank_counter),
        "banner_counts": {
            get_banner_name(game_code, code): count for code, count in sorted(banner_counter.items())
        },
        "earliest_time": ordered[0].get("time", "") if ordered else "",
        "latest_time": ordered[-1].get("time", "") if ordered else "",
        "latest_source_path": latest_import.get("source_path", "") if latest_import else "",
        "latest_imported_at": latest_import.get("imported_at", "") if latest_import else "",
        "import_count": import_count,
        "top_high_rarity": high_names.most_common(1)[0] if high_names else ("-", 0),
    }


def build_high_rarity_history(records: List[Dict[str, Any]], game_code: str, limit: int = 120) -> List[Dict[str, Any]]:
    pity_counters: Dict[str, int] = defaultdict(int)
    history: List[Dict[str, Any]] = []
    high_rank = get_high_rarity_rank(game_code)
    high_label = get_high_rarity_label(game_code)

    for record in _sort_records(records):
        gacha_type = str(record.get("gacha_type", "") or "")
        group_code = _pity_group_code(game_code, gacha_type)
        pity_counters[group_code] += int(record.get("count", 1) or 1)
        if str(record.get("rank_type", "") or "") != high_rank:
            continue
        history.append(
            {
                "time": str(record.get("time", "") or ""),
                "name": str(record.get("name", "") or ""),
                "item_type": str(record.get("item_type", "") or ""),
                "banner": get_banner_name(game_code, gacha_type),
                "pity_group": _pity_group_name(game_code, gacha_type),
                "pity": pity_counters[group_code],
                "rank_label": high_label,
            }
        )
        pity_counters[group_code] = 0

    return list(reversed(history[-limit:]))


def build_pool_analysis(records: List[Dict[str, Any]], game_code: str) -> Dict[str, Dict[str, Any]]:
    ordered = _sort_records(records)
    high_rank = get_high_rarity_rank(game_code)
    pity_counters: Dict[str, int] = defaultdict(int)
    pools: Dict[str, Dict[str, Any]] = {}

    for record in ordered:
        gacha_type = str(record.get("gacha_type", "") or "")
        pool_name = get_banner_name(game_code, gacha_type)
        group_code = _pity_group_code(game_code, gacha_type)
        pity_counters[group_code] += int(record.get("count", 1) or 1)

        pool = pools.setdefault(
            pool_name,
            {
                "pool_name": pool_name,
                "gacha_types": set(),
                "total": 0,
                "rank_counts": Counter(),
                "item_type_counts": Counter(),
                "earliest_time": "",
                "latest_time": "",
                "high_rarity_list": [],
            },
        )

        count = int(record.get("count", 1) or 1)
        rank = str(record.get("rank_type", "") or "")
        item_type = str(record.get("item_type", "") or "")
        record_time = str(record.get("time", "") or "")

        pool["gacha_types"].add(gacha_type)
        pool["total"] += count
        pool["rank_counts"][rank] += count
        pool["item_type_counts"][item_type] += count
        if not pool["earliest_time"] or record_time < pool["earliest_time"]:
            pool["earliest_time"] = record_time
        if record_time > pool["latest_time"]:
            pool["latest_time"] = record_time

        if rank == high_rank:
            pool["high_rarity_list"].append(
                {
                    "time": record_time,
                    "name": str(record.get("name", "") or ""),
                    "item_type": item_type,
                    "pity": pity_counters[group_code],
                    "pool_name": pool_name,
                }
            )
            pity_counters[group_code] = 0

    results: Dict[str, Dict[str, Any]] = {}
    for pool_name, pool in pools.items():
        total = int(pool["total"])
        rank_counts = {str(rank): int(count) for rank, count in pool["rank_counts"].items()}
        results[pool_name] = {
            "pool_name": pool_name,
            "total": total,
            "rank_counts": rank_counts,
            "rank_metrics": [
                {
                    "rank": rank,
                    "label": get_rank_label(game_code, rank),
                    "count": int(rank_counts.get(rank, 0)),
                    "pct": _format_percent(int(rank_counts.get(rank, 0)), total),
                }
                for rank in get_rank_order(game_code)
            ],
            "item_type_counts": {str(item_type): int(count) for item_type, count in pool["item_type_counts"].items()},
            "earliest_time": pool["earliest_time"],
            "latest_time": pool["latest_time"],
            "high_rarity_count": int(rank_counts.get(high_rank, 0)),
            "high_rarity_pct": _format_percent(int(rank_counts.get(high_rank, 0)), total),
            "high_rarity_list": list(pool["high_rarity_list"]),
        }
    return results


def _build_item_stats(records: List[Dict[str, Any]], game_code: str, item_types: Set[str]) -> List[Dict[str, Any]]:
    focus_ranks = set(get_rank_order(game_code)[:2])
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for record in _sort_records(records):
        if str(record.get("item_type", "") or "") not in item_types:
            continue
        rank = str(record.get("rank_type", "") or "")
        if rank not in focus_ranks:
            continue
        grouped[(str(record.get("name", "") or ""), rank)].append(record)

    rows: List[Dict[str, Any]] = []
    for (name, rank), items in grouped.items():
        rows.append(
            {
                "name": name,
                "rank": rank,
                "rank_label": get_rank_label(game_code, rank),
                "copies": len(items),
                "first_time": str(items[0].get("time", "") or ""),
                "last_time": str(items[-1].get("time", "") or ""),
            }
        )
    rows.sort(key=lambda item: (-int(item["rank"]), -item["copies"], item["name"]))
    return rows


def build_character_stats(records: List[Dict[str, Any]], game_code: str) -> List[Dict[str, Any]]:
    return _build_item_stats(records, game_code, _item_types(game_code, "character_item_types"))


def build_weapon_stats(records: List[Dict[str, Any]], game_code: str) -> List[Dict[str, Any]]:
    return _build_item_stats(records, game_code, _item_types(game_code, "weapon_item_types"))


def build_pity_summary(records: List[Dict[str, Any]], game_code: str) -> List[Dict[str, Any]]:
    ordered = _sort_records(records)
    high_rank = get_high_rarity_rank(game_code)
    pity_counters: Dict[str, int] = defaultdict(int)
    pity_values: Dict[str, List[int]] = defaultdict(list)
    group_names: Dict[str, str] = {}

    for record in ordered:
        gacha_type = str(record.get("gacha_type", "") or "")
        group_code = _pity_group_code(game_code, gacha_type)
        group_names[group_code] = _pity_group_name(game_code, gacha_type)
        pity_counters[group_code] += int(record.get("count", 1) or 1)
        if str(record.get("rank_type", "") or "") != high_rank:
            continue
        pity_values[group_code].append(pity_counters[group_code])
        pity_counters[group_code] = 0

    config = get_game_config(game_code)
    order_map = {
        str(code): index
        for index, code in enumerate(dict.fromkeys(str(value) for value in config.get("pity_groups", {}).values()))
    }
    rows: List[Dict[str, Any]] = []
    for group_code, group_name in group_names.items():
        values = pity_values.get(group_code, [])
        rows.append(
            {
                "name": group_name,
                "count": len(values),
                "avg_pity": round(sum(values) / len(values), 2) if values else 0,
                "min_pity": min(values) if values else 0,
                "max_pity": max(values) if values else 0,
                "current_pity": int(pity_counters.get(group_code, 0)),
                "recent": values[-10:],
                "sort_key": order_map.get(group_code, 9999),
            }
        )
    rows.sort(key=lambda item: (item["sort_key"], item["name"]))
    for item in rows:
        item.pop("sort_key", None)
    return rows
