"""抽卡记录导入/分析与各游戏抽卡面板的后台逻辑（从 ui/bridge.py 拆出）。

数据流：SQLite(gacha_records) + 导入文件/DB → 分析结果 → QML JSON 属性。
函数接收 bridge 实例以访问信号与共享状态。
"""


import atexit
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Signal, Property, Slot, QTimer

from ...settings.paths import CACHE_DIR, ENDFIELD_SKLAND_CACHE, PROTECTED_CLICK_SEQUENCES, WUWA_DAILY_CACHE
from ...services.game_update_calendar import (
    CN_TZ,
    OPEN_BUFFER_MIN,
    UPDATE_RETRY_FILE,
    get_maintenance_state,
    get_update_info,
)

logger = logging.getLogger("GameAutomation.Bridge")

# ── Status tone constants ────────────────────────────────────────
TONE_INFO = "info"
TONE_RUNNING = "running"
TONE_SUCCESS = "success"
TONE_WARNING = "warning"
TONE_DANGER = "danger"

# 没有官方公告接口/公告尚未发布时，按常规维护窗口兜底（仅用于失败/跳过后排补跑）
_FALLBACK_UPDATE_WINDOWS = {
    "WutheringWaves": ("04:00", "11:00"),
    "InfinityNikki": ("10:00", "11:00"),
    "NevernessToEverness": ("06:00", "11:00"),
}

TONE_MAP: Dict[str, tuple] = {
    TONE_INFO:    ("待命中", "#dff1e4", "#2f7d4a"),
    TONE_RUNNING: ("进行中", "#e9f3fb", "#295d86"),
    TONE_SUCCESS: ("已完成", "#dff1e4", "#2f7d4a"),
    TONE_WARNING: ("请留意", "#faebd7", "#b66f1f"),
    TONE_DANGER:  ("已中断", "#f7dfdd", "#b04b44"),
}

# 终末地森空岛缓存新鲜度窗口（秒）：超过后才在进页面时后台自动刷新
ENDFIELD_SKLAND_TTL = 300
WUWA_DAILY_TTL = 300


def _mask_address(addr: str) -> str:
    """日志中脱敏邮箱地址：a*c@example.com。"""
    if not addr or "@" not in addr:
        return "***"
    name, domain = addr.split("@", 1)
    if len(name) <= 2:
        masked = "*" * len(name)
    else:
        masked = name[0] + "*" * (len(name) - 2) + name[-1]
    return f"{masked}@{domain}"


def _load_gacha_accounts_backend(bridge, game_code: str):
    """从 SQLite 加载指定游戏的抽卡账号列表。"""
    try:
        db = bridge._get_preference_db()
        accounts = db.get_gacha_accounts(game_code)
        accounts_data = []
        for acc in accounts:
            accounts_data.append({
                "uid": acc.get("uid", ""),
                "imported_at": acc.get("imported_at", ""),
                "import_count": acc.get("import_count", 0),
                "total_records": acc.get("total_records", 0),
            })
        bridge.emit_event("gacha_accounts", accounts=accounts_data)
        logger.info("[Bridge] 加载抽卡账号: game=%s count=%d", game_code, len(accounts_data))
    except Exception as exc:
        logger.exception("[Bridge] 加载抽卡账号失败")
        bridge.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")


def _load_gacha_analysis_backend(bridge, game_code: str, uid: str):
    """从 SQLite 加载抽卡记录并生成分析数据。"""
    try:
        if not uid:
            bridge.emit_event("gacha_analysis", overview={}, pools=[], history=[], pity={})
            return

        db = bridge._get_preference_db()
        records = db.get_gacha_records(game_code, uid)
        if not records:
            bridge.emit_event("gacha_analysis", overview={}, pools=[], history=[], pity={})
            bridge.emit_event("toast", message="该 UID 暂无抽卡记录", color="#b66f1f")
            return

        latest = db.get_latest_gacha_import(game_code, uid)
        accounts = db.get_gacha_accounts(game_code)
        import_count = len(accounts)

        from ...gacha.hoyo_analysis import (
            build_overview,
            build_pool_analysis,
            build_high_rarity_history,
            build_pity_summary,
        )
        from ...gacha.luck_analysis import build_hoyo_luck

        raw_overview = build_overview(records, game_code, latest, import_count)
        raw_pools = build_pool_analysis(records, game_code)
        history = build_high_rarity_history(records, game_code)
        raw_pity = build_pity_summary(records, game_code)
        luck = build_hoyo_luck(records, game_code)

        # 转换 overview → 扁平化字段供 QML 使用
        total = raw_overview.get("total_records", 0)
        high_count = raw_overview.get("high_rarity_count", 0)
        overview = {
            "total_pulls": total,
            "high_rarity_count": high_count,
            "high_rarity_rate": f"{high_count / total * 100:.1f}%" if total > 0 else "0%",
            "latest_import": raw_overview.get("latest_imported_at", ""),
            "import_count": raw_overview.get("import_count", 0),
            "earliest_time": raw_overview.get("earliest_time", ""),
            "latest_time": raw_overview.get("latest_time", ""),
        }

        # 转换 pools dict → list
        pools = []
        for pool_name, pool_data in raw_pools.items():
            pool_total = pool_data.get("total", 0)
            pool_high = pool_data.get("high_rarity_count", 0)
            pools.append({
                "pool_name": pool_name,
                "total_pulls": pool_total,
                "high_rarity_count": pool_high,
                "high_rarity_rate": pool_data.get("high_rarity_pct", "0%"),
                "current_pity": pool_data.get("current_pity", 0),
                "high_rarity_history": pool_data.get("high_rarity_list", []),
            })

        # 转换 pity list → dict
        pity = {}
        if isinstance(raw_pity, list):
            for item in raw_pity:
                group_name = item.get("group_name", "")
                pity[group_name] = {
                    "avg_pity": item.get("avg_pity", 0),
                    "min_pity": item.get("min_pity", 0),
                    "max_pity": item.get("max_pity", 0),
                    "current_pity": item.get("current_pity", 0),
                }
        elif isinstance(raw_pity, dict):
            pity = raw_pity

        # 提取全局保底摘要
        all_pities = []
        for v in pity.values():
            if isinstance(v, dict) and v.get("avg_pity"):
                all_pities.append(v)
        pity_summary = {
            "avg_pity": int(sum(v["avg_pity"] for v in all_pities) / len(all_pities)) if all_pities else 0,
            "min_pity": min((v["min_pity"] for v in all_pities), default=0),
            "max_pity": max((v["max_pity"] for v in all_pities), default=0),
            "current_pity": max((v["current_pity"] for v in all_pities), default=0),
        }

        bridge.emit_event(
            "gacha_analysis",
            overview=overview,
            pools=pools,
            history=history,
            pity=pity_summary,
            luck=luck,
        )
        logger.info("[Bridge] 抽卡分析完成: game=%s uid=%s records=%d pools=%d",
                    game_code, uid, len(records), len(pools))
    except Exception as exc:
        logger.exception("[Bridge] 加载抽卡分析失败")
        bridge.emit_event("toast", message=f"分析失败: {exc}", color="#b04b44")


def _import_gacha_backend(bridge, file_path: str):
    """后台导入 UIGF JSON 文件。"""
    try:
        bridge.emit_event("status", title="正在导入", body="正在解析 UIGF 文件...", tone=TONE_RUNNING)
        from ...gacha.hoyo_importer import import_uigf
        from ...settings.paths import WORKFLOW_DB_PATH
        summaries = import_uigf(file_path, str(WORKFLOW_DB_PATH))

        total_count = sum(s.get("total_records", 0) for s in summaries)
        game_code = summaries[0].get("game_code", "hk4e") if summaries else "hk4e"

        bridge.emit_event("status", title="导入完成",
                        body=f"已导入 {total_count} 条记录",
                        tone=TONE_SUCCESS)
        bridge.emit_event("toast", message=f"导入完成: {total_count} 条", color="#2f7d4a")
        # 自动刷新账号列表
        bridge._load_gacha_accounts_backend(game_code)
    except Exception as exc:
        logger.exception("[Bridge] 导入抽卡文件失败")
        bridge.emit_event("status", title="导入失败", body=str(exc), tone=TONE_DANGER)
        bridge.emit_event("toast", message=f"导入失败: {exc}", color="#b04b44")


def _import_hoyogacha_db_backend(bridge, db_path: str, game_label: str):
    """后台从 HoYo.Gacha 数据库导入（只读拷临时文件，不锁原库）。"""
    try:
        bridge.emit_event("status", title="正在导入", body="正在读取 HoYo.Gacha 数据库...", tone=TONE_RUNNING)
        from ...gacha.hoyo_importer import import_hoyogacha_db
        from ...settings.paths import WORKFLOW_DB_PATH
        summaries = import_hoyogacha_db(db_path, str(WORKFLOW_DB_PATH))

        total_count = sum(s.get("total_records", 0) for s in summaries)
        codes = {s.get("game_code", "") for s in summaries}
        want = bridge._GACHA_GAME_MAP.get((game_label or "").strip(), "")
        refresh_code = want if want in codes else (summaries[0].get("game_code", "hk4e") if summaries else "hk4e")

        bridge.emit_event("status", title="导入完成",
                        body=f"HoYo.Gacha 已导入 {total_count} 条记录",
                        tone=TONE_SUCCESS)
        bridge.emit_event("toast", message=f"导入完成: {total_count} 条", color="#2f7d4a")
        # 只刷新当前选中游戏的账号列表（切游戏下拉会自动加载其它游戏）
        bridge._load_gacha_accounts_backend(refresh_code)
    except Exception as exc:
        logger.exception("[Bridge] HoYo.Gacha 数据库导入失败")
        bridge.emit_event("status", title="导入失败", body=str(exc), tone=TONE_DANGER)
        bridge.emit_event("toast", message=f"导入失败: {exc}", color="#b04b44")


def _load_nikki_gacha_backend(bridge):
    """从数据库加载无限暖暖抽卡数据并分析。"""
    try:
        db = bridge._get_preference_db()
        accounts = db.get_gacha_accounts("nikki")
        if not accounts:
            bridge.emit_event("nikki_gacha", overview={}, pools=[])
            bridge.emit_event("toast", message="暂无无限暖暖抽卡记录", color="#b66f1f")
            return

        uid = accounts[0].get("uid", "")
        db_rows = db.get_gacha_records("nikki", uid)
        if not db_rows:
            bridge.emit_event("nikki_gacha", overview={}, pools=[])
            return

        from ...gacha.nikki_resonance_analysis import (
            db_to_nikki_document,
            build_overview as build_nikki_overview,
            build_pool_summary as build_nikki_pool_summary,
            build_pearpal_overview as build_nikki_pearpal_overview,
            build_pearpal_pool_summary as build_nikki_pearpal_pool_summary,
        )
        from ...gacha.nikki_resonance_fetch import GACHA_POOLS as NIKKI_GACHA_POOLS

        document = db_to_nikki_document(db_rows)
        records_by_pool = document.get("records_by_pool", {})
        pearpal_by_pool = document.get("pearpal_by_pool", {})

        has_raw = any(bool(rows) for rows in records_by_pool.values())
        has_pearpal = any(bool(rows) for rows in pearpal_by_pool.values())

        pools = []
        if has_raw:
            raw_overview = build_nikki_overview(records_by_pool)
            overview = {
                "total_records": raw_overview.get("total_records", 0),
                "total_pulls": raw_overview.get("total_records", 0),
                "pool_count": raw_overview.get("pool_count", 0),
                "earliest_time": raw_overview.get("earliest_time", ""),
                "latest_time": raw_overview.get("latest_time", ""),
                "mode": "raw",
            }
            for pool_id, pool_name in NIKKI_GACHA_POOLS.items():
                rows = records_by_pool.get(pool_id, [])
                if not rows:
                    continue
                summary = build_nikki_pool_summary(pool_id, rows)
                pools.append({
                    "pool_id": pool_id,
                    "pool_name": pool_name,
                    "total_pulls": summary.get("total_records", 0),
                    "total_records": summary.get("total_records", 0),
                    "unique_item_ids": summary.get("unique_item_ids", 0),
                    "earliest_time": summary.get("earliest_time", ""),
                    "latest_time": summary.get("latest_time", ""),
                    "top_items": summary.get("top_items", []),
                    "mode": "raw",
                })
        elif has_pearpal:
            pp_overview = build_nikki_pearpal_overview(pearpal_by_pool)
            overview = {
                "total_records": pp_overview.get("total_events", 0),
                "total_pulls": pp_overview.get("total_pulls", 0),
                "pool_count": pp_overview.get("pool_count", 0),
                "four_count": pp_overview.get("four_count", 0),
                "five_count": pp_overview.get("five_count", 0),
                "earliest_time": "",
                "latest_time": "",
                "mode": "pearpal",
            }

            def _aggregate_outfits(five_hist: List[Dict]) -> List[Dict]:
                """把 5★ 出货历史按套装聚合：套装名 → 已获得部件（含部位）。

                PearPal 的五星都是套装部件（outfit_name=套装、item_type_label=部位），
                玩家关心的是"集齐了哪个套装的哪几件"，不是一串单件名。
                """
                groups: Dict[str, Dict[str, Any]] = {}
                order: List[str] = []
                for r in five_hist:
                    outfit = (r.get("outfit_name") or "").strip() or "未知套装"
                    if outfit not in groups:
                        groups[outfit] = {"outfit_name": outfit, "parts": [], "min_pity": None}
                        order.append(outfit)
                    groups[outfit]["parts"].append({
                        "name": r.get("name") or "",
                        "part_type": (r.get("part_type") or "").strip(),
                        "pity": r.get("pity", 0),
                        "draw_index": r.get("draw_index", 0),
                    })
                    p = r.get("pity", 0)
                    if isinstance(p, int) and p > 0:
                        cur = groups[outfit]["min_pity"]
                        groups[outfit]["min_pity"] = p if cur is None else min(cur, p)
                return [groups[k] for k in order]

            # 全局套装汇总（跨池）：先收集全部 5★ 再聚合
            all_five_hist: List[Dict] = []
            for pool_id, pool_name in NIKKI_GACHA_POOLS.items():
                rows = pearpal_by_pool.get(pool_id, [])
                if not rows:
                    continue
                summary = build_nikki_pearpal_pool_summary(pool_id, rows)
                # 完整 5★ 出货历史（无30条限制；recent_records 只保留30条会漏）
                five_star_history = [
                    {
                        "name": r.get("display_name_with_type")
                        or r.get("display_name")
                        or r.get("item_name")
                        or ("ID " + str(r.get("item_id", ""))),
                        "pity": r.get("five_gap", 0),
                        "draw_index": r.get("draw_index", 0),
                        "time": "",
                        "pool_name": pool_name,
                        # 套装聚合用（PearPal 数据自带套装归属）
                        "outfit_name": (r.get("outfit_name") or "").strip(),
                        "part_type": (r.get("item_type_label") or r.get("item_type") or "").strip(),
                    }
                    for r in summary.get("five_star_history", [])
                ]
                all_five_hist.extend(five_star_history)
                pools.append({
                    "pool_id": pool_id,
                    "pool_name": pool_name,
                    "total_pulls": summary.get("estimated_total_pulls", 0),
                    "total_records": summary.get("event_count", 0),
                    "unique_item_ids": summary.get("unique_item_ids", 0),
                    "four_count": summary.get("four_count", 0),
                    "five_count": summary.get("five_count", 0),
                    "current_four_pity": summary.get("current_four_pity", 0),
                    "current_five_pity": summary.get("current_five_pity", 0),
                    "average_four_gap": summary.get("average_four_gap", 0),
                    "average_five_gap": summary.get("average_five_gap", 0),
                    "top_items": summary.get("top_items", []),
                    "high_rarity_history": five_star_history,
                    "outfit_summary": _aggregate_outfits(five_star_history),
                    "mode": "pearpal",
                })
            overview["outfit_summary"] = _aggregate_outfits(all_five_hist)
        else:
            overview = {}

        bridge.emit_event("nikki_gacha", overview=overview, pools=pools, uid=uid)
        logger.info("[Bridge] 暖暖抽卡分析完成: uid=%s pools=%d", uid, len(pools))
    except Exception as exc:
        logger.exception("[Bridge] 加载暖暖抽卡失败")
        bridge.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")


def _load_wuwa_daily_backend(bridge):
    """拉取鸣潮库街区每日数据（体力/活跃度/周本/深塔等）供 QML 展示。"""
    try:
        from ...services import credential_store
        from ...clients import wuwa_kujiequ

        profile = credential_store.load_wuwa_kujiequ_profile()
        if profile is None:
            bridge.emit_event(
                "wuwa_daily",
                ok=False,
                message="库街区 Token 或 DID 未配置，请先 ADB 抓取或手动填写",
            )
            return

        logger.info("[Bridge] 拉取库街区每日数据: role=%s", profile.role_name)
        daily = wuwa_kujiequ.fetch_daily_data(profile)
        try:
            base = wuwa_kujiequ.fetch_base_data(profile)
        except Exception as exc:  # 基础数据失败不影响主数据展示
            logger.warning("[Bridge] 拉取库街区基础数据失败，忽略: %s", exc)
            base = {}

        def _fmt(detail, label):
            if not isinstance(detail, dict):
                return None
            cur = detail.get("cur")
            total = detail.get("total")
            if cur is None and total is None:
                return None
            return {
                "name": detail.get("name") or label,
                "cur": cur if cur is not None else 0,
                "total": total if total is not None else 0,
            }

        items = []
        for key, label in (
            ("energy", "结晶波片"),
            ("liveness", "活跃度"),
            ("weekly", "战歌重奏"),
            ("weekly_rouge", "千道门扉的异想"),
            ("store_energy", "结晶单质"),
            ("tower", "逆境深塔"),
            ("slash_tower", "冥歌海墟"),
        ):
            item = _fmt(daily.get(key), label)
            if item:
                items.append(item)

        payload = {
            "ok": True,
            "role_name": daily.get("role_name") or profile.role_name or "",
            "server_name": daily.get("server_name") or "",
            "has_signed": bool(daily.get("has_signed")),
            "sign_text": daily.get("sign_text") or "",
            "items": items,
            "active_days": (base or {}).get("active_days", 0),
            # 官方限制：活跃度需进入游戏后才会刷新，未刷新时为前一日数据
            "stale_hint": "活跃度需进入游戏后才会更新，未更新时展示前一日数据",
            "cached_at": time.time(),
        }
        bridge.emit_event("wuwa_daily", **payload)
        # 仅成功结果落地缓存；失败保留上次好缓存
        bridge._save_wuwa_daily_cache(payload)
    except Exception as exc:
        logger.exception("[Bridge] 拉取库街区每日数据失败")
        bridge.emit_event("wuwa_daily", ok=False, message=str(exc))


def _load_wuwa_gacha_backend(bridge):
    """从数据库加载鸣潮抽卡数据并分析。"""
    try:
        db = bridge._get_preference_db()
        accounts = db.get_gacha_accounts("wuwa")
        if not accounts:
            bridge.emit_event("wuwa_gacha", overview={}, pools=[])
            bridge.emit_event("toast", message="暂无鸣潮抽卡记录", color="#b66f1f")
            return

        uid = accounts[0].get("uid", "")
        db_rows = db.get_gacha_records("wuwa", uid)
        if not db_rows:
            bridge.emit_event("wuwa_gacha", overview={}, pools=[])
            return

        from ...gacha.wuwa_analysis import (
            db_to_wuwa_records,
            build_wuwa_overview,
            build_wuwa_pool_analysis,
            build_wuwa_five_star_history,
        )
        from ...gacha.luck_analysis import build_wuwa_luck

        records = db_to_wuwa_records(db_rows)
        overview = build_wuwa_overview(records)
        raw_pools = build_wuwa_pool_analysis(records)
        history = build_wuwa_five_star_history(records)
        luck = build_wuwa_luck(records)

        # 转换 pools dict → list
        pools = []
        for pool_name, pool_data in raw_pools.items():
            pools.append({
                "pool_name": pool_name,
                "total_pulls": pool_data.get("total", 0),
                "five_star_count": pool_data.get("five_star_count", 0),
                "five_star_rate": pool_data.get("five_star_pct", "0%"),
                "high_rarity_history": pool_data.get("five_star_list", []),
            })

        bridge.emit_event("wuwa_gacha", overview=overview, pools=pools, history=history, uid=uid, luck=luck)
        logger.info("[Bridge] 鸣潮抽卡分析完成: uid=%s pools=%d", uid, len(pools))
    except Exception as exc:
        logger.exception("[Bridge] 加载鸣潮抽卡失败")
        bridge.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")


def _import_wuwa_gacha_from_json_backend(bridge, file_paths_json: str):
    """从 WutheringWavesTool 的 pool.json 导入抽卡记录到数据库。

    支持多文件合并去重。导入前清空该 uid 的旧 wuwa 记录，避免不同
    record_id 格式造成重复。
    """
    try:
        # 解析 file_paths_json → list[str]
        file_paths: List[str] = []
        if file_paths_json:
            try:
                parsed = json.loads(file_paths_json)
                if isinstance(parsed, list):
                    file_paths = [str(p) for p in parsed if str(p).strip()]
                elif isinstance(parsed, str):
                    file_paths = [parsed] if parsed.strip() else []
                else:
                    file_paths = []
            except (json.JSONDecodeError, TypeError):
                # 当作单路径字符串
                file_paths = [file_paths_json] if file_paths_json.strip() else []

        if not file_paths:
            bridge.emit_event("toast", message="未选择文件", color="#b04b44")
            return

        # 检查文件存在性
        missing = [p for p in file_paths if not os.path.exists(p)]
        if missing:
            bridge.emit_event(
                "toast",
                message=f"文件不存在: {missing[0]}",
                color="#b04b44",
            )
            return

        # 从配置取 uid（优先 role_id，没有则用 user_id）
        from ...settings import manager as cfg_manager
        cfg = cfg_manager.load()
        profile = (cfg.get("wuwa_kujiequ") or {}).get("profile") or {}
        uid = str(profile.get("role_id") or profile.get("user_id") or "")
        if not uid:
            bridge.emit_event(
                "toast",
                message="未找到鸣潮角色 ID，请先在库街区凭据里登录或 ADB 抓取",
                color="#b04b44",
            )
            return

        bridge.emit_event(
            "toast",
            message=f"开始解析 {len(file_paths)} 个 pool.json...",
            color="#295d86",
        )

        from ...gacha.wuwa_analysis import (
            load_wuwa_pool_json,
            wuwa_pool_json_to_db_records,
        )

        records = load_wuwa_pool_json(file_paths)
        if not records:
            bridge.emit_event(
                "toast", message="pool.json 中未找到有效记录", color="#b66f1f"
            )
            return

        db_rows = wuwa_pool_json_to_db_records(records, uid)

        db = bridge._get_preference_db()
        # 清旧记录，避免业务键 record_id 与旧 xlsx 导入（用 index 后缀）重复
        deleted = db.delete_gacha_records_by_uid("wuwa", uid)
        logger.info(
            "[Bridge] 清空旧 wuwa 抽卡记录: uid=%s 删除 %d 条", uid, deleted,
        )

        source_paths = "|".join(os.path.basename(p) for p in file_paths)
        import_id = db.start_gacha_import({
            "game_code": "wuwa",
            "uid": uid,
            "source_path": source_paths,
            "source_hash": "",
            "uigf_version": "",
            "export_app": "WutheringWavesTool",
            "export_app_version": "",
            "export_timestamp": "",
            "lang": "zh-cn",
            "timezone": "UTC+8",
            "total_records": len(db_rows),
            "notes": "pool.json 合并去重导入",
        })
        stats = db.upsert_gacha_records("wuwa", uid, import_id, db_rows)

        bridge.emit_event(
            "toast",
            message=(
                f"鸣潮抽卡导入完成：共 {stats['total_count']} 条 "
                f"(新增 {stats['inserted_count']}, 更新 {stats['updated_count']})"
            ),
            color="#2f7d4a",
        )
        logger.info(
            "[Bridge] 鸣潮抽卡导入完成: uid=%s total=%d inserted=%d updated=%d",
            uid, stats["total_count"], stats["inserted_count"],
            stats["updated_count"],
        )

        # 触发加载刷新 UI
        bridge._load_wuwa_gacha_backend()
    except Exception as exc:
        logger.exception("[Bridge] 鸣潮抽卡 pool.json 导入失败")
        bridge.emit_event(
            "toast",
            message=f"导入失败: {exc}",
            color="#b04b44",
        )


def _load_endfield_gacha_backend(bridge, file_path: str):
    """从文件加载终末地抽卡数据并分析，同时写入数据库。"""
    try:
        from ...gacha.endfield_analysis import (
            load_all_records as load_endfield_records,
            build_overview as build_endfield_overview,
            build_six_star_history,
            build_pool_analysis as build_endfield_pool_analysis,
            endfield_to_db_records,
            extract_uid_from_filename,
        )

        records = load_endfield_records(file_path)
        if not records:
            bridge.emit_event("toast", message="文件中无抽卡记录", color="#b66f1f")
            return

        # 写入数据库
        from ...settings.paths import WORKFLOW_DB_PATH
        db = bridge._get_preference_db()
        # 从文件名 `<uid>_<roleId>.json` 提取真实终末地 UID，
        # 历史遗留的 uid="" 写法会让多账号场景下不同来源的数据无法合并。
        uid = extract_uid_from_filename(file_path)
        # 若本次导入有真实 uid，先把 DB 中遗留的旧 uid（含空字符串）迁移到该 uid
        if uid:
            bridge._migrate_endfield_uids_to(db, uid)
        db_records = endfield_to_db_records(records, uid)
        import_id = db.start_gacha_import({
            "game_code": "endfield",
            "uid": uid,
            "source_path": file_path,
            "source_hash": "",
            "uigf_version": "",
            "export_app": "",
            "export_app_version": "",
            "export_timestamp": "",
            "lang": "zh-cn",
            "timezone": "8",
            "total_records": len(records),
            "notes": "Endfield 文件导入",
        })
        db.upsert_gacha_records("endfield", uid, import_id, db_records)
        logger.info(
            "[Bridge] 终末地抽卡数据已写入数据库: uid=%s records=%d 条",
            uid or "<空>", len(records),
        )

        overview = build_endfield_overview(records)
        raw_pools = build_endfield_pool_analysis(records)
        history = build_six_star_history(records)

        # 转换 pools dict → list
        pools = []
        for pool_name, pool_data in raw_pools.items():
            pools.append({
                "pool_name": pool_name,
                "total_pulls": pool_data.get("total", 0),
                "six_star_count": pool_data.get("six_star_count", 0),
                "six_star_rate": f"{pool_data.get('six_star_pct', 0):.1f}%",
                "high_rarity_history": pool_data.get("six_star_list", []),
            })

        bridge.emit_event("endfield_gacha", overview=overview, pools=pools, history=history)
        bridge.emit_event("toast", message=f"导入完成: {len(records)} 条", color="#2f7d4a")
        logger.info("[Bridge] 终末地抽卡分析完成: records=%d pools=%d", len(records), len(pools))
    except Exception as exc:
        logger.exception("[Bridge] 加载终末地抽卡失败")
        bridge.emit_event("toast", message=f"分析失败: {exc}", color="#b04b44")


def _migrate_endfield_uids_to(bridge, db, uid: str) -> None:
    """把 DB 里 endfield 所有旧 uid 的记录迁移到本次真实 uid。

    用于：
    - 文件导入路径（从文件名提取的真实 uid）
    - token 同步路径（用户填写的真实 uid）

    迁移会把旧 record_id（如 `endfield__<ts>_<seq>`）改写成
    `endfield_<uid>_<ts>_<seq>`，使后续 upsert 能正确合并去重。
    """
    if not uid:
        return
    try:
        accounts = db.get_gacha_accounts("endfield")
    except Exception:
        accounts = []
    for acct in accounts:
        old_uid = str(acct.get("uid", "") or "")
        if old_uid == uid:
            continue
        try:
            migrated = db.migrate_gacha_uid(
                "endfield",
                old_uid=old_uid,
                new_uid=uid,
                record_id_prefix="endfield_",
            )
            if migrated > 0:
                logger.info(
                    "[Bridge] 终末地 uid 迁移: %r → %r (%d rows)",
                    old_uid, uid, migrated,
                )
        except Exception:
            logger.exception(
                "[Bridge] 终末地 uid 迁移失败: %r → %r", old_uid, uid,
            )


def _load_endfield_db_backend(bridge):
    """从数据库加载终末地抽卡数据。无数据则不报错，等用户手动导入文件。"""
    try:
        db = bridge._get_preference_db()
        accounts = db.get_gacha_accounts("endfield")
        if not accounts:
            logger.debug("[Bridge] 数据库中无终末地抽卡记录，等待用户导入文件")
            return

        uid = accounts[0].get("uid", "")
        db_rows = db.get_gacha_records("endfield", uid)
        if not db_rows:
            logger.debug("[Bridge] 数据库中无终末地抽卡记录 (uid=%s)", uid)
            return

        from ...gacha.endfield_analysis import (
            db_to_endfield_records,
            build_overview as build_endfield_overview,
            build_six_star_history,
            build_pool_analysis as build_endfield_pool_analysis,
        )

        records = db_to_endfield_records(db_rows)
        if not records:
            logger.debug("[Bridge] 终末地 raw_json 解析失败，跳过")
            return

        overview = build_endfield_overview(records)
        raw_pools = build_endfield_pool_analysis(records)
        history = build_six_star_history(records)

        pools = []
        for pool_name, pool_data in raw_pools.items():
            pools.append({
                "pool_name": pool_name,
                "total_pulls": pool_data.get("total", 0),
                "six_star_count": pool_data.get("six_star_count", 0),
                "six_star_rate": f"{pool_data.get('six_star_pct', 0):.1f}%",
                "high_rarity_history": pool_data.get("six_star_list", []),
            })

        bridge.emit_event("endfield_gacha", overview=overview, pools=pools, history=history)
        logger.info("[Bridge] 终末地抽卡分析完成(DB): records=%d pools=%d", len(records), len(pools))
    except Exception as exc:
        logger.exception("[Bridge] 从数据库加载终末地抽卡失败")
        bridge.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")


def _fetch_endfield_gacha_by_token_backend(bridge):
    """用 user 填写的 token 直接拉取终末地抽卡记录并入库。

    流程：app_config.json[endfield_gacha] → clients.endfield_gacha.fetch_gacha_records
    → gacha.endfield_analysis.load_records_from_dict → DB upsert → emit endfield_gacha。

    凭据安全：token 仅在内存中传递给客户端，日志只输出脱敏长度。
    """
    try:
        from ...settings import manager as cfg_manager
        from ...clients import endfield_gacha
        from ...gacha.endfield_analysis import (
            load_records_from_dict,
            build_overview as build_endfield_overview,
            build_six_star_history,
            build_pool_analysis as build_endfield_pool_analysis,
            endfield_to_db_records,
        )

        cfg = cfg_manager.load(force=True)
        gacha_cfg = cfg.get("endfield_gacha") or {}
        token = str(gacha_cfg.get("token") or "").strip()
        uid = str(gacha_cfg.get("uid") or "").strip()
        provider = str(gacha_cfg.get("provider") or "hypergryph").strip()
        server_id = str(gacha_cfg.get("server_id") or "1").strip() or "1"

        if not token:
            bridge.emit_event(
                "toast",
                message="终末地抽卡 Token 为空，请先填写",
                color="#b04b44",
            )
            return
        if not uid:
            bridge.emit_event(
                "toast",
                message="终末地 UID 为空，请先填写",
                color="#b04b44",
            )
            return

        logger.info(
            "[Bridge] 终末地抽卡 token 同步开始: provider=%s uid=%s token_len=%d",
            provider, uid, len(token),
        )
        bridge.emit_event(
            "toast",
            message="正在通过 token 同步终末地抽卡记录...",
            color="#295d86",
        )

        api_data = endfield_gacha.fetch_gacha_records(
            token=token, uid=uid, provider=provider, server_id=server_id,
        )
        stats = endfield_gacha.summarize_records(api_data)
        logger.info(
            "[Bridge] 终末地 API 抓取完成: char=%d weapon=%d total=%d",
            stats["character_records"], stats["weapon_records"],
            stats["total_records"],
        )

        if stats["total_records"] == 0:
            bridge.emit_event(
                "toast",
                message="未抓到任何抽卡记录（可能 token 过期或无数据）",
                color="#b66f1f",
            )
            return

        records = load_records_from_dict(api_data)
        db_records = endfield_to_db_records(records, uid)

        db = bridge._get_preference_db()
        # 先把 DB 里 endfield 旧 uid（含历史遗留的 uid=""）迁移到本次的真实 uid，
        # 这样旧记录的 record_id 也会被改写成 endfield_<uid>_<ts>_<seq> 形式，
        # 后续 upsert 能与本次新拉取的记录正确合并去重。
        bridge._migrate_endfield_uids_to(db, uid)
        import_id = db.start_gacha_import({
            "game_code": "endfield",
            "uid": uid,
            "source_path": f"token_sync:{provider}",
            "source_hash": "",
            "uigf_version": "",
            "export_app": "endfield-gacha (token sync)",
            "export_app_version": "",
            "export_timestamp": "",
            "lang": "zh-cn",
            "timezone": "8",
            "total_records": len(records),
            "notes": f"Token sync provider={provider} server_id={server_id}",
        })
        upsert_stats = db.upsert_gacha_records("endfield", uid, import_id, db_records)
        logger.info(
            "[Bridge] 终末地抽卡已写入数据库: uid=%s total=%d inserted=%d updated=%d",
            uid, upsert_stats["total_count"], upsert_stats["inserted_count"],
            upsert_stats["updated_count"],
        )

        overview = build_endfield_overview(records)
        raw_pools = build_endfield_pool_analysis(records)
        history = build_six_star_history(records)
        pools = []
        for pool_name, pool_data in raw_pools.items():
            pools.append({
                "pool_name": pool_name,
                "total_pulls": pool_data.get("total", 0),
                "six_star_count": pool_data.get("six_star_count", 0),
                "six_star_rate": f"{pool_data.get('six_star_pct', 0):.1f}%",
                "high_rarity_history": pool_data.get("six_star_list", []),
            })
        bridge.emit_event("endfield_gacha", overview=overview, pools=pools, history=history)
        bridge.emit_event(
            "toast",
            message=(
                f"终末地抽卡同步完成：共 {stats['total_records']} 条 "
                f"(角色 {stats['character_records']} / 武器 {stats['weapon_records']})"
            ),
            color="#2f7d4a",
        )
    except Exception as exc:
        # EndfieldGachaError 与其他异常都走这里，避免 NameError（import 失败时
        # except 子句引用未定义的名字会再次抛错）
        from ...clients.endfield_gacha import EndfieldGachaError
        if isinstance(exc, EndfieldGachaError):
            logger.warning("[Bridge] 终末地抽卡 token 同步失败: %s", exc)
        else:
            logger.exception("[Bridge] 终末地抽卡 token 同步异常")
        bridge.emit_event(
            "toast",
            message=f"同步失败: {exc}",
            color="#b04b44",
        )
