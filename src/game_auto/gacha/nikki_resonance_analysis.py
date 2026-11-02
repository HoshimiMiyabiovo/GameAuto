from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from .nikki_resonance_fetch import GACHA_POOLS
from .nikki_item_catalog import resolve_item_metadata as resolve_nikki_item_metadata


PoolRows = List[Tuple[str, int]]
PearpalRows = List[Dict[str, Any]]
RecentRows = List[Dict[str, Any]]


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="解析无限暖暖 gacha-records-UID.json，输出池子级本地摘要。"
    )
    parser.add_argument("file_path", help="nuan5 兼容的 gacha-records-UID.json 路径")
    parser.add_argument(
        "--top-item-ids",
        type=int,
        default=8,
        help="每个卡池展示前 N 个高频物品 ID，默认 8。",
    )
    return parser.parse_args(argv)


def _normalize_pool_rows(rows: Iterable[Any]) -> PoolRows:
    normalized: PoolRows = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        normalized.append((str(row[0]), int(row[1])))
    return sorted(normalized, key=lambda item: (item[0], item[1]))


def _normalize_pearpal_rows(rows: Iterable[Any]) -> PearpalRows:
    normalized: PearpalRows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pool_cnt = int(row.get("pool_cnt"))
            five_gap = int(row.get("times_from_last_five_stars"))
            four_gap = int(row.get("times_from_last_four_stars"))
        except (TypeError, ValueError):
            continue
        normalized.append(
            {
                "card_pool_id": str(row.get("card_pool_id", "") or ""),
                "pool_cnt": pool_cnt,
                "draw_index": pool_cnt + 1,
                "item_id": str(row.get("result", "") or ""),
                "times_from_last_five_stars": five_gap,
                "times_from_last_four_stars": four_gap,
                "rarity": str(row.get("rarity", "") or ""),
            }
        )
    return sorted(normalized, key=lambda item: item["pool_cnt"])


def _has_non_empty_rows(mapping: Dict[str, Sequence[Any]]) -> bool:
    return any(bool(rows) for rows in mapping.values())


def _extract_document(payload: Dict[str, Any]) -> Dict[str, Any]:
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    profile_label = str(profile.get("label", "") or "")

    if isinstance(payload.get("pulls"), dict) or isinstance(payload.get("pearpal"), dict):
        pulls = payload.get("pulls") or {}
        records_by_pool = {
            str(pool_id): _normalize_pool_rows(rows)
            for pool_id, rows in pulls.items()
            if str(pool_id) in GACHA_POOLS and isinstance(rows, list)
        }
        pearpal = payload.get("pearpal") or {}
        pearpal_by_pool = {
            str(pool_id): _normalize_pearpal_rows(rows)
            for pool_id, rows in pearpal.items()
            if str(pool_id) in GACHA_POOLS and isinstance(rows, list)
        }
        if _has_non_empty_rows(records_by_pool):
            source_format = "site_backup"
            source_label = "nuan5 完整档案"
        elif _has_non_empty_rows(pearpal_by_pool):
            source_format = "site_backup_pearpal"
            source_label = "nuan5 完整档案（pearpal 摘要）"
        else:
            source_format = "site_backup"
            source_label = "nuan5 完整档案"
        return {
            "source_format": source_format,
            "source_label": source_label,
            "profile_label": profile_label,
            "records_by_pool": records_by_pool,
            "pearpal_by_pool": pearpal_by_pool,
            "top_level_keys": sorted(payload.keys()),
        }

    records_by_pool = {
        str(pool_id): _normalize_pool_rows(rows)
        for pool_id, rows in payload.items()
        if str(pool_id) in GACHA_POOLS and isinstance(rows, list)
    }
    pearpal_by_pool = {
        str(pool_id): _normalize_pearpal_rows(rows)
        for pool_id, rows in payload.items()
        if str(pool_id) in GACHA_POOLS and isinstance(rows, list)
    }
    if _has_non_empty_rows(records_by_pool):
        source_format = "raw_records"
        source_label = "gacha-records 原始记录"
    elif _has_non_empty_rows(pearpal_by_pool):
        source_format = "pearpal_summary"
        source_label = "pearpal 摘要记录"
    else:
        source_format = "raw_records"
        source_label = "gacha-records 原始记录"
    return {
        "source_format": source_format,
        "source_label": source_label,
        "profile_label": profile_label,
        "records_by_pool": records_by_pool,
        "pearpal_by_pool": pearpal_by_pool,
        "top_level_keys": sorted(payload.keys()),
    }


def load_resonance_document(file_path: str) -> Dict[str, Any]:
    payload = json.loads(Path(file_path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("JSON 根节点不是对象")
    document = _extract_document(payload)
    document["file_path"] = str(file_path)
    return document


def load_resonance_records(file_path: str) -> Dict[str, PoolRows]:
    return load_resonance_document(file_path)["records_by_pool"]


def _average_gap(rows: PearpalRows, gap_key: str) -> float:
    gaps = [row[gap_key] + 1 for row in rows]
    if not gaps:
        return 0.0
    return sum(gaps) / len(gaps)


def _build_pearpal_item_counter(rows: PearpalRows) -> Counter[str]:
    return Counter(row["item_id"] for row in rows)


def _build_named_item_counts(item_counter: Counter[str]) -> List[Dict[str, Any]]:
    named_rows: List[Dict[str, Any]] = []
    for item_id, count in item_counter.most_common():
        named_rows.append({**resolve_nikki_item_metadata(item_id), "count": count})
    return named_rows


def build_pearpal_overview(pearpal_by_pool: Dict[str, PearpalRows]) -> Dict[str, Any]:
    all_rows = [row for rows in pearpal_by_pool.values() for row in rows]
    total_pulls = sum(rows[-1]["draw_index"] for rows in pearpal_by_pool.values() if rows)
    rarity_counter = Counter(str(row.get("rarity", "")) for row in all_rows)
    return {
        "total_events": len(all_rows),
        "total_pulls": total_pulls,
        "pool_count": sum(1 for rows in pearpal_by_pool.values() if rows),
        "four_count": rarity_counter.get("4", 0),
        "five_count": rarity_counter.get("5", 0),
    }



def build_overview(records_by_pool: Dict[str, PoolRows]) -> Dict[str, Any]:
    all_rows = [row for rows in records_by_pool.values() for row in rows]
    times = [row[0] for row in all_rows]
    return {
        "total_records": len(all_rows),
        "pool_count": sum(1 for rows in records_by_pool.values() if rows),
        "earliest_time": min(times) if times else "",
        "latest_time": max(times) if times else "",
    }


def build_recent_records(records_by_pool: Dict[str, PoolRows], limit: int = 80) -> RecentRows:
    rows: RecentRows = []
    for pool_id, pool_rows in records_by_pool.items():
        pool_name = GACHA_POOLS.get(pool_id, pool_id)
        for time_str, item_id in pool_rows:
            metadata = resolve_nikki_item_metadata(item_id)
            rows.append(
                {
                    "pool_id": pool_id,
                    "pool_name": pool_name,
                    "time": time_str,
                    "item_id": item_id,
                    **metadata,
                }
            )
    rows.sort(key=lambda item: (item["time"], item["item_id"]), reverse=True)
    return rows[:limit]


def build_pool_summary(pool_id: str, rows: PoolRows) -> Dict[str, Any]:
    item_counter = Counter(item_id for _, item_id in rows)
    duplicate_counter = Counter(count for count in item_counter.values())
    return {
        "pool_id": pool_id,
        "pool_name": GACHA_POOLS.get(pool_id, pool_id),
        "total_records": len(rows),
        "unique_item_ids": len(item_counter),
        "earliest_time": rows[0][0] if rows else "",
        "latest_time": rows[-1][0] if rows else "",
        "top_item_ids": item_counter.most_common(),
        "top_items": _build_named_item_counts(item_counter),
        "duplicate_distribution": dict(sorted(duplicate_counter.items())),
        "recent_records": [
            {"time": time_str, "item_id": item_id, **resolve_nikki_item_metadata(item_id)}
            for time_str, item_id in sorted(rows, key=lambda item: (item[0], item[1]), reverse=True)[:30]
        ],
    }


def build_pearpal_pool_summary(pool_id: str, rows: PearpalRows) -> Dict[str, Any]:
    item_counter = _build_pearpal_item_counter(rows)
    duplicate_counter = Counter(count for count in item_counter.values())
    four_rows = [row for row in rows if row.get("rarity") == "4"]
    five_rows = [row for row in rows if row.get("rarity") == "5"]
    estimated_total_pulls = rows[-1]["draw_index"] if rows else 0
    last_four_draw = four_rows[-1]["draw_index"] if four_rows else 0
    last_five_draw = five_rows[-1]["draw_index"] if five_rows else 0
    return {
        "pool_id": pool_id,
        "pool_name": GACHA_POOLS.get(pool_id, pool_id),
        "event_count": len(rows),
        "estimated_total_pulls": estimated_total_pulls,
        "unique_item_ids": len(item_counter),
        "four_count": len(four_rows),
        "five_count": len(five_rows),
        "average_four_gap": _average_gap(four_rows, "times_from_last_four_stars"),
        "average_five_gap": _average_gap(five_rows, "times_from_last_five_stars"),
        "current_four_pity": max(estimated_total_pulls - last_four_draw, 0),
        "current_five_pity": max(estimated_total_pulls - last_five_draw, 0),
        "top_item_ids": item_counter.most_common(),
        "top_items": _build_named_item_counts(item_counter),
        "duplicate_distribution": dict(sorted(duplicate_counter.items())),
        "recent_records": [
            {
                "draw_index": row["draw_index"],
                "item_id": row["item_id"],
                "rarity": row["rarity"],
                "four_gap": row["times_from_last_four_stars"] + 1,
                "five_gap": row["times_from_last_five_stars"] + 1,
                **resolve_nikki_item_metadata(row["item_id"]),
            }
            for row in sorted(rows, key=lambda item: item["draw_index"], reverse=True)[:30]
        ],
        # 完整 5★ 出货历史（无条数限制，供 UI 展示；recent_records 只保留30条会漏）
        "five_star_history": [
            {
                "draw_index": row["draw_index"],
                "item_id": row["item_id"],
                "rarity": row["rarity"],
                "four_gap": row["times_from_last_four_stars"] + 1,
                "five_gap": row["times_from_last_five_stars"] + 1,
                **resolve_nikki_item_metadata(row["item_id"]),
            }
            for row in sorted(five_rows, key=lambda item: item["draw_index"], reverse=True)
        ],
    }


def print_divider(char: str = "=", width: int = 60):
    print(char * width)


def print_analysis(file_path: str, top_item_ids: int = 8):
    document = load_resonance_document(file_path)
    records_by_pool = document["records_by_pool"]
    pearpal_by_pool = document.get("pearpal_by_pool") or {}
    has_raw_records = _has_non_empty_rows(records_by_pool)
    has_pearpal_records = _has_non_empty_rows(pearpal_by_pool)
    overview = build_overview(records_by_pool) if has_raw_records else None
    pearpal_overview = build_pearpal_overview(pearpal_by_pool) if has_pearpal_records else None

    print_divider()
    print("  无限暖暖 - 共鸣记录摘要")
    print_divider()
    print(f"\n[整体概况]")
    print(f"  文件: {file_path}")
    print(f"  格式: {document['source_label']}")
    if document.get("profile_label"):
        print(f"  档案标签: {document['profile_label']}")
    if overview is not None:
        print(f"  总记录数: {overview['total_records']} 条")
        print(f"  有记录卡池: {overview['pool_count']} 个")
        print(f"  时间范围: {overview['earliest_time'] or '-'}  ~  {overview['latest_time'] or '-'}")
    elif pearpal_overview is not None:
        print(f"  估算总抽数: {pearpal_overview['total_pulls']} 抽")
        print(f"  摘要事件数: {pearpal_overview['total_events']} 条")
        print(f"  有记录卡池: {pearpal_overview['pool_count']} 个")
        print(f"  4星 / 5星: {pearpal_overview['four_count']} / {pearpal_overview['five_count']}")
        print("  说明: pearpal 只保留 4/5 星事件摘要，不含逐抽时间轴。")
    if overview is not None and overview["total_records"] == 0 and document["source_format"] == "site_backup":
        print("  说明: 这是站点完整档案备份，但 pulls 为空，因此当前文件没有共鸣记录。")
    elif overview is not None and overview["total_records"] == 0:
        print("  说明: 这个 JSON 中没有可识别的共鸣记录。")
    if overview is not None:
        print("  说明: 该原始格式只包含时间和物品 ID，本地脚本先做池子级解析；")
        print("       若要看网页那种五星/套装/部件级可视化，可把同一 JSON 拖到 nuan5.pro/resonance-records。")
    elif pearpal_overview is not None:
        print("  说明: 这个摘要模式可以估算每池总抽数、4/5 星事件次数和当前保底进度；")
        print("       但因为缺少逐抽时间与低星记录，无法完全还原网页的完整统计。")

    print(f"\n[卡池摘要]")
    print_divider()
    if overview is not None:
        for pool_id in GACHA_POOLS:
            rows = records_by_pool.get(pool_id) or []
            if not rows:
                continue
            summary = build_pool_summary(pool_id, rows)
            print(f"\n  【{summary['pool_name']}】 (ID {pool_id})")
            print(f"    总计: {summary['total_records']} 抽")
            print(f"    不同物品 ID: {summary['unique_item_ids']} 个")
            print(f"    时间: {summary['earliest_time']}  ~  {summary['latest_time']}")
            if summary["duplicate_distribution"]:
                dist = ", ".join(
                    f"出现 {copies} 次的物品 {count} 个"
                    for copies, count in summary["duplicate_distribution"].items()
                )
                print(f"    重复分布: {dist}")
            top_items = summary.get("top_items", [])[:top_item_ids]
            if top_items:
                print("    高频物品:")
                for item in top_items:
                    print(f"      - {item['display_name_with_type']}: {item['count']} 次")
                    print(f"        {item['subtitle']}")
    elif pearpal_overview is not None:
        for pool_id in GACHA_POOLS:
            rows = pearpal_by_pool.get(pool_id) or []
            if not rows:
                continue
            summary = build_pearpal_pool_summary(pool_id, rows)
            print(f"\n  【{summary['pool_name']}】 (ID {pool_id})")
            print(f"    估算总抽数: {summary['estimated_total_pulls']} 抽")
            print(f"    摘要事件: {summary['event_count']} 条")
            print(f"    4星 / 5星: {summary['four_count']} / {summary['five_count']}")
            print(
                f"    平均间隔: 4星 {summary['average_four_gap']:.2f} 抽 / 5星 {summary['average_five_gap']:.2f} 抽"
            )
            print(
                f"    当前保底: 4星 {summary['current_four_pity']} 抽 / 5星 {summary['current_five_pity']} 抽"
            )
            top_items = summary.get("top_items", [])[:top_item_ids]
            if top_items:
                print("    高频物品:")
                for item in top_items:
                    print(f"      - {item['display_name_with_type']}: {item['count']} 次")
                    print(f"        {item['subtitle']}")

    print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    print_analysis(args.file_path, top_item_ids=args.top_item_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def nikki_to_db_records(document: Dict[str, Any], uid: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    idx = 0
    for pool_id, rows in document.get("records_by_pool", {}).items():
        pool_name = GACHA_POOLS.get(pool_id, pool_id)
        for time_str, item_id in rows:
            meta = resolve_nikki_item_metadata(item_id)
            results.append({
                "record_id": f"nikki_{uid}_{pool_id}_{idx}",
                "gacha_type": pool_name,
                "uigf_gacha_type": "",
                "item_id": str(item_id),
                "name": meta.get("display_name", str(item_id)),
                "item_type": meta.get("item_type", ""),
                "rank_type": str(meta.get("rarity", "")),
                "count": 1,
                "time": str(time_str),
                "lang": "zh-cn",
                "timezone": "UTC+8",
                "raw_json": json.dumps({"pool_id": pool_id, "item_id": str(item_id), "time": str(time_str)}, ensure_ascii=False),
            })
            idx += 1
    for pool_id, rows in document.get("pearpal_by_pool", {}).items():
        pool_name = GACHA_POOLS.get(pool_id, pool_id)
        for row in rows:
            draw_index = row.get("draw_index", idx)
            item_id = str(row.get("item_id", ""))
            meta = resolve_nikki_item_metadata(item_id)
            raw_data = dict(row)
            raw_data["pool_id"] = pool_id
            results.append({
                "record_id": f"nikki_{uid}_{pool_id}_pearpal_{draw_index}",
                "gacha_type": pool_name,
                "uigf_gacha_type": "",
                "item_id": item_id,
                "name": meta.get("display_name", item_id),
                "item_type": meta.get("item_type", ""),
                "rank_type": str(row.get("rarity", "")),
                "count": 1,
                "time": "",
                "lang": "zh-cn",
                "timezone": "UTC+8",
                "raw_json": json.dumps(raw_data, ensure_ascii=False, default=str),
            })
            idx += 1
    return results


def db_to_nikki_document(db_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    records_by_pool: Dict[str, List[Tuple[str, int]]] = {}
    pearpal_by_pool: Dict[str, List[Dict[str, Any]]] = {}
    for row in db_rows:
        raw_str = row.get("raw_json", "{}")
        try:
            raw = json.loads(raw_str)
        except (json.JSONDecodeError, TypeError):
            continue
        pool_id = str(raw.get("pool_id") or raw.get("card_pool_id") or "")
        if not pool_id:
            continue
        if "draw_index" in raw or "pool_cnt" in raw or "card_pool_id" in raw:
            pearpal_by_pool.setdefault(pool_id, []).append(raw)
        else:
            time_str = str(raw.get("time", row.get("time", "")))
            item_id = int(raw.get("item_id", 0))
            records_by_pool.setdefault(pool_id, []).append((time_str, item_id))
    for pool_id in records_by_pool:
        records_by_pool[pool_id] = sorted(records_by_pool[pool_id], key=lambda x: x[0])
    for pool_id in pearpal_by_pool:
        pearpal_by_pool[pool_id] = sorted(pearpal_by_pool[pool_id], key=lambda x: x.get("pool_cnt", 0))
    return {
        "source_format": "database",
        "source_label": "数据库缓存",
        "profile_label": "",
        "records_by_pool": records_by_pool,
        "pearpal_by_pool": pearpal_by_pool,
        "top_level_keys": sorted(set(list(records_by_pool.keys()) + list(pearpal_by_pool.keys()))),
    }