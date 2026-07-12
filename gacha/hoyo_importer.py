from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..settings import WORKFLOW_DB_PATH
from ..storage.workflow_db import WorkflowDB


GENSHIN_GAME_CODE = "hk4e"
GAME_CODE = GENSHIN_GAME_CODE
HOYO_GACHA_GAME_CONFIGS: Dict[str, Dict[str, Any]] = {
    "hk4e": {
        "display_name": "原神",
        "banner_names": {
            "100": "新手祈愿",
            "200": "常驻祈愿",
            "301": "角色活动祈愿",
            "302": "武器活动祈愿",
            "400": "角色活动祈愿",
            "500": "集录祈愿",
        },
        "high_rarity_rank": "5",
        "rank_labels": {"5": "五星", "4": "四星", "3": "三星"},
        "character_item_types": {"角色"},
        "weapon_item_types": {"武器"},
        "pity_groups": {
            "100": "100",
            "200": "200",
            "301": "301",
            "302": "302",
            "400": "301",
            "500": "500",
        },
    },
    "hkrpg": {
        "display_name": "崩坏：星穹铁道",
        "banner_names": {
            "1": "常驻跃迁",
            "2": "新手跃迁",
            "11": "角色活动跃迁",
            "12": "光锥活动跃迁",
        },
        "high_rarity_rank": "5",
        "rank_labels": {"5": "五星", "4": "四星", "3": "三星"},
        "character_item_types": {"角色"},
        "weapon_item_types": {"光锥"},
        "pity_groups": {
            "1": "1",
            "2": "2",
            "11": "11",
            "12": "12",
        },
    },
    "nap": {
        "display_name": "绝区零",
        "banner_names": {
            "1": "常驻频段",
            "2": "独家频段",
            "3": "音擎频段",
            "5": "邦布频段",
            "102": "新手频段-上半",
            "103": "新手频段-下半",
        },
        "high_rarity_rank": "4",
        "rank_labels": {"4": "S级", "3": "A级", "2": "B级"},
        "character_item_types": {"代理人"},
        "weapon_item_types": {"音擎", "邦布"},
        "pity_groups": {
            "1": "1",
            "2": "2",
            "3": "3",
            "5": "5",
            "102": "102",
            "103": "103",
        },
    },
}
BANNER_NAMES = HOYO_GACHA_GAME_CONFIGS[GENSHIN_GAME_CODE]["banner_names"]
HOYO_GAME_OPTIONS = [
    (game_code, str(config.get("display_name", game_code)))
    for game_code, config in HOYO_GACHA_GAME_CONFIGS.items()
]


def get_game_config(game_code: str) -> Dict[str, Any]:
    code = str(game_code or "").strip()
    if code not in HOYO_GACHA_GAME_CONFIGS:
        raise ValueError(f"暂不支持的 HoYo UIGF 游戏代码: {code or '空'}")
    return HOYO_GACHA_GAME_CONFIGS[code]


def get_game_display_name(game_code: str) -> str:
    return str(get_game_config(game_code).get("display_name", game_code))


def get_banner_name(game_code: str, gacha_type: str) -> str:
    code = str(gacha_type or "")
    return str(get_game_config(game_code).get("banner_names", {}).get(code, code or "未知卡池"))


def _load_uigf_json(file_path: str) -> Dict[str, Any]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"找不到 UIGF 文件: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha256_of_file(file_path: str) -> str:
    digest = hashlib.sha256()
    with open(file_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_record(record: Dict[str, Any], lang: str, timezone: Any) -> Dict[str, Any]:
    return {
        "record_id": str(record.get("id", "")).strip(),
        "gacha_type": str(record.get("gacha_type", "") or ""),
        "uigf_gacha_type": str(record.get("uigf_gacha_type", "") or ""),
        "item_id": str(record.get("item_id", "") or ""),
        "name": str(record.get("name", "") or ""),
        "item_type": str(record.get("item_type", "") or ""),
        "rank_type": str(record.get("rank_type", "") or ""),
        "count": int(record.get("count", 1) or 1),
        "time": str(record.get("time", "") or ""),
        "lang": str(record.get("lang", lang) or lang or ""),
        "timezone": str(record.get("timezone", timezone) or timezone or ""),
        "raw_json": json.dumps(record, ensure_ascii=False, separators=(",", ":")),
    }


def _detect_game_codes(payload: Dict[str, Any]) -> List[str]:
    game_codes: List[str] = []
    for key, value in payload.items():
        if key == "info":
            continue
        if key not in HOYO_GACHA_GAME_CONFIGS:
            continue
        if isinstance(value, list) and value:
            game_codes.append(key)
    return game_codes


def _account_summary(game_code: str, uid: str, records: List[Dict[str, Any]], stats: Dict[str, int]) -> Dict[str, Any]:
    config = get_game_config(game_code)
    banner_counter = Counter(record["gacha_type"] for record in records)
    rank_counter = Counter(record["rank_type"] for record in records)
    high_rank = str(config.get("high_rarity_rank", "5") or "5")
    high_label = str(config.get("rank_labels", {}).get(high_rank, f"{high_rank}星"))
    high_count = int(rank_counter.get(high_rank, 0))
    return {
        "game_code": game_code,
        "game_name": get_game_display_name(game_code),
        "uid": uid,
        "total_records": len(records),
        "inserted_count": stats["inserted_count"],
        "updated_count": stats["updated_count"],
        "high_rarity_rank": high_rank,
        "high_rarity_label": high_label,
        "high_rarity_count": high_count,
        "five_star_count": int(rank_counter.get("5", 0)),
        "banner_counts": {
            get_banner_name(game_code, banner_code): count
            for banner_code, count in sorted(banner_counter.items())
        },
        "earliest_time": min((record["time"] for record in records), default=""),
        "latest_time": max((record["time"] for record in records), default=""),
    }


def import_uigf(
    file_path: str,
    db_path: str = WORKFLOW_DB_PATH,
    *,
    allowed_game_codes: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    payload = _load_uigf_json(file_path)
    info = payload.get("info", {})
    game_codes = _detect_game_codes(payload)
    if allowed_game_codes is not None:
        allowed = {str(code) for code in allowed_game_codes}
        game_codes = [code for code in game_codes if code in allowed]
    if not game_codes:
        raise ValueError("文件中未找到受支持的 HoYo UIGF 抽卡数据")

    db = WorkflowDB(str(db_path))
    source_hash = _sha256_of_file(file_path)
    summaries: List[Dict[str, Any]] = []

    for game_code in game_codes:
        accounts = payload.get(game_code)
        if not isinstance(accounts, list) or not accounts:
            continue

        for account in accounts:
            uid = str(account.get("uid", "")).strip()
            if not uid:
                raise ValueError(f"检测到缺少 uid 的 {get_game_display_name(game_code)} 抽卡账号块")
            raw_records = account.get("list") or []
            if not isinstance(raw_records, list):
                raise ValueError(f"{get_game_display_name(game_code)} 账号 {uid} 的 list 字段不是数组")

            lang = str(account.get("lang", "") or "")
            timezone = account.get("timezone", "")
            records = [_normalize_record(record, lang, timezone) for record in raw_records]
            import_id = db.start_gacha_import(
                {
                    "game_code": game_code,
                    "uid": uid,
                    "source_path": str(Path(file_path)),
                    "source_hash": source_hash,
                    "uigf_version": str(info.get("version", "") or ""),
                    "export_app": str(info.get("export_app", "") or ""),
                    "export_app_version": str(info.get("export_app_version", "") or ""),
                    "export_timestamp": str(info.get("export_timestamp", "") or ""),
                    "lang": lang,
                    "timezone": str(timezone or ""),
                    "total_records": len(records),
                    "notes": "UIGF 导入",
                }
            )
            stats = db.upsert_gacha_records(game_code, uid, import_id, records)
            summaries.append(_account_summary(game_code, uid, records, stats))

    return summaries


def import_genshin_uigf(file_path: str, db_path: str = WORKFLOW_DB_PATH) -> List[Dict[str, Any]]:
    return import_uigf(file_path, db_path, allowed_game_codes=[GENSHIN_GAME_CODE])


def _format_summary(summary: Dict[str, Any]) -> str:
    banner_text = "，".join(f"{name}:{count}" for name, count in summary.get("banner_counts", {}).items())
    high_label = str(summary.get("high_rarity_label", "最高稀有度"))
    return (
        f"{summary.get('game_name', summary.get('game_code', 'HoYo'))} UID {summary['uid']} - 总计 {summary['total_records']} 条，"
        f"新增 {summary['inserted_count']} 条，更新 {summary['updated_count']} 条，"
        f"{high_label} {summary.get('high_rarity_count', 0)} 个，"
        f"时间范围 {summary['earliest_time']} ~ {summary['latest_time']}，"
        f"卡池分布 [{banner_text}]"
    )


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("用法: python hoyo_gacha_importer.py <UIGF_JSON_PATH> [DB_PATH]")
        return 1

    file_path = argv[1]
    db_path = argv[2] if len(argv) > 2 else WORKFLOW_DB_PATH
    summaries = import_uigf(file_path, db_path)
    for summary in summaries:
        print(_format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
