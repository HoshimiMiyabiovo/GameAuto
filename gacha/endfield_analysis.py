from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))

POOL_TYPE_MAP = {
    "E_CharacterGachaPoolType_Beginner": "新手池",
    "E_CharacterGachaPoolType_Special": "限定角色池",
    "E_CharacterGachaPoolType_Standard": "常驻角色池",
}

WEAPON_POOL_TYPE_MAP = {
    "weaponbox_constant_2": "常驻武器池",
}


def _ms_to_datetime(ts_ms: str) -> datetime:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=CST)


def _ms_to_str(ts_ms: str) -> str:
    return _ms_to_datetime(ts_ms).strftime("%Y-%m-%d %H:%M:%S")


def _rarity_label(rarity: int) -> str:
    return {6: "六星", 5: "五星", 4: "四星"}.get(rarity, f"{rarity}星")


def load_endfield_gacha(file_path: str) -> Dict[str, Any]:
    with open(file_path, encoding="utf-8-sig") as f:
        return json.load(f)


def _normalize_character_records(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    character_data = data.get("character", {})
    for pool_key, pool_list in character_data.items():
        if not isinstance(pool_list, list):
            continue
        pool_display = POOL_TYPE_MAP.get(pool_key, pool_key)
        for item in pool_list:
            records.append({
                "name": item.get("charName", ""),
                "rarity": item.get("rarity", 4),
                "pool_id": item.get("poolId", ""),
                "pool_name": item.get("poolName", ""),
                "pool_display": pool_display,
                "gacha_ts": str(item.get("gachaTs", "")),
                "gacha_time": _ms_to_str(str(item.get("gachaTs", ""))),
                "is_new": item.get("isNew", False),
                "seq_id": str(item.get("seqId", "")),
                "category": "character",
            })
    return records


def _normalize_weapon_records(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    weapon_data = data.get("weapon", {})
    for pool_key, pool_list in weapon_data.items():
        if not isinstance(pool_list, list):
            continue
        pool_display = WEAPON_POOL_TYPE_MAP.get(pool_key, "限定武器池")
        for item in pool_list:
            records.append({
                "name": item.get("weaponName", ""),
                "rarity": item.get("rarity", 4),
                "pool_id": item.get("poolId", ""),
                "pool_name": item.get("poolName", ""),
                "pool_display": pool_display,
                "gacha_ts": str(item.get("gachaTs", "")),
                "gacha_time": _ms_to_str(str(item.get("gachaTs", ""))),
                "is_new": item.get("isNew", False),
                "seq_id": str(item.get("seqId", "")),
                "category": "weapon",
            })
    return records


def load_all_records(file_path: str) -> List[Dict[str, Any]]:
    data = load_endfield_gacha(file_path)
    records = _normalize_character_records(data) + _normalize_weapon_records(data)
    records.sort(key=lambda r: (r["gacha_ts"], r["seq_id"]))
    return records


def endfield_to_db_records(records: List[Dict[str, Any]], uid: str = "") -> List[Dict[str, Any]]:
    """将终末地标准化记录转换为数据库行 dict 列表。"""
    db_records: List[Dict[str, Any]] = []
    for i, r in enumerate(records):
        db_records.append({
            "record_id": f"endfield_{uid}_{r.get('gacha_ts', '')}_{r.get('seq_id', i)}",
            "gacha_type": r.get("pool_display", ""),
            "uigf_gacha_type": r.get("pool_id", ""),
            "item_id": "",
            "name": r.get("name", ""),
            "item_type": r.get("category", ""),
            "rank_type": str(r.get("rarity", 4)),
            "count": 1,
            "time": r.get("gacha_time", ""),
            "lang": "zh-cn",
            "timezone": "8",
            "raw_json": json.dumps(r, ensure_ascii=False),
        })
    return db_records


def db_to_endfield_records(db_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从数据库行 dict 列表恢复终末地标准化记录。"""
    records: List[Dict[str, Any]] = []
    for row in db_rows:
        raw = row.get("raw_json", "{}")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if parsed and isinstance(parsed, dict):
                records.append(parsed)
        except (json.JSONDecodeError, TypeError):
            continue
    records.sort(key=lambda r: (r.get("gacha_ts", ""), r.get("seq_id", "")))
    return records


def _sort_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(records, key=lambda r: (r["gacha_ts"], r["seq_id"]))


def build_overview(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = _sort_records(records)
    pool_counter = Counter(r["pool_display"] for r in ordered)
    rarity_counter = Counter(str(r["rarity"]) for r in ordered)
    six_star_names = Counter(
        r["name"] for r in ordered if r["rarity"] == 6
    )
    return {
        "total_records": len(ordered),
        "six_star_count": rarity_counter.get("6", 0),
        "five_star_count": rarity_counter.get("5", 0),
        "four_star_count": rarity_counter.get("4", 0),
        "pool_counts": {name: count for name, count in sorted(pool_counter.items())},
        "earliest_time": ordered[0]["gacha_time"] if ordered else "",
        "latest_time": ordered[-1]["gacha_time"] if ordered else "",
        "top_six_star": six_star_names.most_common(1)[0] if six_star_names else ("-", 0),
    }


def build_six_star_history(records: List[Dict[str, Any]], limit: int = 120) -> List[Dict[str, Any]]:
    pity_counters: Dict[str, int] = defaultdict(int)
    history: List[Dict[str, Any]] = []
    for record in _sort_records(records):
        group = record["pool_display"]
        pity_counters[group] += 1
        if record["rarity"] != 6:
            continue
        history.append({
            "time": record["gacha_time"],
            "name": record["name"],
            "pool": record["pool_display"],
            "pool_name": record["pool_name"],
            "pity_group": group,
            "pity": pity_counters[group],
        })
        pity_counters[group] = 0
    return list(reversed(history[-limit:]))


def build_pool_analysis(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = _sort_records(records)
    pools: Dict[str, Dict[str, Any]] = {}
    pity_counters: Dict[str, int] = defaultdict(int)

    for record in ordered:
        pool_display = record["pool_display"]
        if pool_display not in pools:
            pools[pool_display] = {
                "total": 0,
                "six_star_count": 0,
                "five_star_count": 0,
                "four_star_count": 0,
                "sub_pools": defaultdict(int),
                "earliest_time": record["gacha_time"],
                "latest_time": record["gacha_time"],
                "six_star_list": [],
            }
        p = pools[pool_display]
        p["total"] += 1
        pity_counters[pool_display] += 1
        rarity = record["rarity"]
        if rarity == 6:
            p["six_star_count"] += 1
            p["six_star_list"].append({
                "name": record["name"],
                "time": record["gacha_time"],
                "pool_name": record["pool_name"],
                "is_new": record["is_new"],
                "pity": pity_counters[pool_display],
            })
            pity_counters[pool_display] = 0
        elif rarity == 5:
            p["five_star_count"] += 1
        elif rarity == 4:
            p["four_star_count"] += 1
        p["sub_pools"][record["pool_name"]] += 1
        p["latest_time"] = record["gacha_time"]

    for pool_display in pools:
        p = pools[pool_display]
        p["six_star_pct"] = round(p["six_star_count"] / p["total"] * 100, 2) if p["total"] else 0
        p["sub_pools"] = dict(sorted(p["sub_pools"].items(), key=lambda x: -x[1]))

    return pools


def build_pity_analysis(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = _sort_records(records)
    pity_counters: Dict[str, int] = defaultdict(int)
    pity_history: Dict[str, List[int]] = defaultdict(list)

    for record in ordered:
        group = record["pool_display"]
        pity_counters[group] += 1
        if record["rarity"] == 6:
            pity_history[group].append(pity_counters[group])
            pity_counters[group] = 0

    result: Dict[str, Any] = {}
    for group, pities in pity_history.items():
        if not pities:
            result[group] = {"count": 0, "avg_pity": "-", "min_pity": "-", "max_pity": "-", "pities": []}
            continue
        result[group] = {
            "count": len(pities),
            "avg_pity": round(sum(pities) / len(pities), 1),
            "min_pity": min(pities),
            "max_pity": max(pities),
            "current_pity_no_6s": pity_counters[group],
            "pities": pities,
            "distribution": dict(Counter(pities).most_common()),
        }

    return result


def build_character_stats(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for record in _sort_records(records):
        if record["category"] != "character":
            continue
        grouped[(record["name"], record["rarity"])].append(record)

    rows: List[Dict[str, Any]] = []
    for (name, rarity), items in grouped.items():
        rows.append({
            "name": name,
            "rarity": rarity,
            "rarity_label": _rarity_label(rarity),
            "copies": len(items),
            "first_time": items[0]["gacha_time"],
            "last_time": items[-1]["gacha_time"],
            "pools": list({r["pool_name"] for r in items}),
        })
    rows.sort(key=lambda r: (-r["rarity"], -r["copies"], r["last_time"], r["name"]))
    return rows


def build_weapon_stats(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for record in _sort_records(records):
        if record["category"] != "weapon":
            continue
        grouped[(record["name"], record["rarity"])].append(record)

    rows: List[Dict[str, Any]] = []
    for (name, rarity), items in grouped.items():
        rows.append({
            "name": name,
            "rarity": rarity,
            "rarity_label": _rarity_label(rarity),
            "copies": len(items),
            "first_time": items[0]["gacha_time"],
            "last_time": items[-1]["gacha_time"],
            "pools": list({r["pool_name"] for r in items}),
        })
    rows.sort(key=lambda r: (-r["rarity"], -r["copies"], r["last_time"], r["name"]))
    return rows


def print_divider(char: str = "=", width: int = 60):
    print(char * width)


def print_analysis(file_path: str):
    records = load_all_records(file_path)
    overview = build_overview(records)
    pool_analysis = build_pool_analysis(records)
    pity_analysis = build_pity_analysis(records)
    six_star_history = build_six_star_history(records)
    char_stats = build_character_stats(records)
    weapon_stats = build_weapon_stats(records)

    print_divider()
    print("  明日方舟：终末地 - 抽卡数据分析")
    print_divider()

    print(f"\n[总体概况]")
    print(f"  抽卡总数: {overview['total_records']} 条")
    print(f"  六星: {overview['six_star_count']} 个  |  五星: {overview['five_star_count']} 个  |  四星: {overview['four_star_count']} 个")
    print(f"  时间范围: {overview['earliest_time']}  ~  {overview['latest_time']}")
    if overview["top_six_star"] != ("-", 0):
        print(f"  最多六星: {overview['top_six_star'][0]} ×{overview['top_six_star'][1]}")
    print(f"\n  各卡池分布:")
    for pool_name, count in overview["pool_counts"].items():
        print(f"    - {pool_name}: {count} 抽")

    print(f"\n{'='*60}")
    print(f"[各卡池详细分析]")
    print_divider()

    for pool_display in ["限定角色池", "常驻角色池", "新手池", "限定武器池", "常驻武器池"]:
        if pool_display not in pool_analysis:
            continue
        p = pool_analysis[pool_display]
        if p["total"] == 0:
            print(f"\n  【{pool_display}】 (无记录)")
            continue
        print(f"\n  【{pool_display}】")
        print(f"    总计: {p['total']} 抽")
        print(f"    六星: {p['six_star_count']} 个 ({p['six_star_pct']}%)  |  五星: {p['five_star_count']} 个  |  四星: {p['four_star_count']} 个")
        print(f"    时间: {p['earliest_time']}  ~  {p['latest_time']}")
        if len(p["sub_pools"]) > 1:
            print(f"    子池分布:")
            for sub_name, sub_count in p["sub_pools"].items():
                print(f"      - {sub_name}: {sub_count} 抽")

        if p["six_star_list"]:
            print(f"    六星出货记录:")
            for s in p["six_star_list"]:
                new_tag = " [NEW!]" if s["is_new"] else ""
                print(f"      {s['time']}  {s['name']}{new_tag}  池: {s['pool_name']}")

        if pool_display in pity_analysis:
            pt = pity_analysis[pool_display]
            if pt["count"] > 0:
                print(f"    六星保底分析 (共 {pt['count']} 次出货):")
                print(f"      平均保底: {pt['avg_pity']} 抽  |  最少: {pt['min_pity']} 抽  |  最多: {pt['max_pity']} 抽")
                print(f"      当前距上次六星已抽: {pt['current_pity_no_6s']} 抽")
                if len(pt["pities"]) >= 2:
                    print(f"      最近每抽出六星: {', '.join(str(x) for x in pt['pities'][-10:])}")

    print(f"\n{'='*60}")
    print(f"[六星出货记录 (最近 30 条)]")
    print_divider()
    displayed = 0
    for h in six_star_history[:30]:
        print(f"  [{h['time']}]  {h['name']}  [{h['pool']}] {h['pool_name']}  第 {h['pity']} 抽")
        displayed += 1
    if displayed == 0:
        print("  (无六星出货记录)")

    print(f"\n{'='*60}")
    print(f"[角色统计 (按稀有度/持有数排序)]")
    print_divider()
    current_rarity = None
    for c in char_stats:
        if current_rarity != c["rarity"]:
            current_rarity = c["rarity"]
            print(f"\n  {c['rarity_label']}:")
        print(f"    {c['name']}: {c['copies']} 只  ({c['first_time']} ~ {c['last_time']})")

    print(f"\n{'='*60}")
    print(f"[武器统计 (按稀有度/持有数排序)]")
    print_divider()
    current_rarity = None
    for w in weapon_stats:
        if current_rarity != w["rarity"]:
            current_rarity = w["rarity"]
            print(f"\n  {w['rarity_label']}:")
        print(f"    {w['name']}: {w['copies']} 把  ({w['first_time']} ~ {w['last_time']})")

    print(f"\n{'='*60}")
    print(f"[所有卡池保底分布]")
    print_divider()
    for pool_display, pt in pity_analysis.items():
        if pt["count"] == 0:
            print(f"  {pool_display}: 无六星出货")
            continue
        dist = sorted(pt["distribution"].items())
        dist_str = ", ".join(f"{pity}抽={count}次" for pity, count in dist)
        print(f"  {pool_display}: 共 {pt['count']} 次六星, 平均 {pt['avg_pity']} 抽 (最少 {pt['min_pity']}, 最多 {pt['max_pity']})")
        print(f"    分布: {dist_str}")

    print()


def main():
    import sys
    if len(sys.argv) < 2:
        print("用法: python -m game_auto.gacha.endfield_analysis <gacha_data.json>")
        print("  gacha_data.json 为终末地抽卡数据文件路径")
        sys.exit(1)
    file_path = sys.argv[1]
    print_analysis(file_path)


if __name__ == "__main__":
    main()