"""启动前签到调度器。"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional

from ..settings import manager as cm
from ..clients.mihoyo_sign import (
    MIHOYO_SIGN_CONFIG_KEY,
    format_sign_report,
    sign_games,
    tidy_cookie,
)
from ..clients.wuwa_kujiequ import (
    perform_sign as perform_wuwa_sign,
    profile_from_dict as wuwa_profile_from_dict,
)
from ..clients.tajiduo_sign import (
    TAJIDUO_SIGN_CONFIG_KEY,
    format_sign_report as format_tajiduo_sign_report,
    perform_sign as perform_tajiduo_sign,
)
from ..clients.skendfield_sign import (
    SKENDFIELD_SIGN_CONFIG_KEY,
    format_sign_report as format_skendfield_sign_report,
    perform_sign as perform_skendfield_sign,
)
from .daily_progress_tracker import DailyProgressTracker, get_tracker
from ..settings.paths import WORKFLOW_DB_PATH


LOGGER = logging.getLogger("GameAutomation")


def run_startup_sign_tasks(target_app_keys: Iterable[str]) -> List[str]:
    app_keys = {str(item) for item in target_app_keys}
    config = cm.load()
    lines: List[str] = []
    LOGGER.info("[启动前签到] 开始检查配置，目标应用：%s", "、".join(sorted(app_keys)) if app_keys else "无")

    tracker = get_tracker(WORKFLOW_DB_PATH)

    mihoyo_cfg = config.get(MIHOYO_SIGN_CONFIG_KEY, {})
    if isinstance(mihoyo_cfg, dict) and mihoyo_cfg.get("startup_sign"):
        cookie = tidy_cookie(str(mihoyo_cfg.get("cookie") or mihoyo_cfg.get("stoken_cookie") or ""))
        enabled_games = _resolve_mihoyo_enabled_games(mihoyo_cfg, app_keys)
        if cookie and enabled_games:
            try:
                LOGGER.info("[启动前签到] 开始执行米游社签到，目标游戏：%s", "、".join(enabled_games))
                result = sign_games(cookie, enabled_games)
                for line in format_sign_report(result):
                    LOGGER.info("[启动前签到] %s", line)
                    lines.append(f"米游社启动前签到: {line}")
                _record_mihoyo_sign_to_tracker(tracker, result)
            except Exception as exc:
                LOGGER.error("[启动前签到] 米游社签到失败：%s", str(exc), exc_info=True)
                lines.append(f"米游社启动前签到: {str(exc)}")
                for gk in enabled_games:
                    tracker.record_sign_result(gk, gk, "米游社", "failed", str(exc))
        else:
            if not cookie:
                LOGGER.warning("[启动前签到] 米游社已开启启动前签到，但当前没有可用 Cookie")
            elif not enabled_games:
                LOGGER.info("[启动前签到] 米游社已开启启动前签到，但本轮目标应用不包含已启用游戏")

    wuwa_cfg = config.get("wuwa_kujiequ", {})
    if isinstance(wuwa_cfg, dict) and wuwa_cfg.get("auto_sign") and "WutheringWaves" in app_keys:
        profile = wuwa_profile_from_dict(wuwa_cfg.get("profile"))
        if profile is not None:
            try:
                LOGGER.info("[启动前签到] 开始执行鸣潮库街区签到")
                result = perform_wuwa_sign(profile)
                LOGGER.info("[启动前签到] 鸣潮库街区签到结果：%s", result.get("message") or "签到完成。")
                lines.append(f"鸣潮库街区启动前签到: {result.get('message') or '签到完成。'}")
                raw_status = result.get("status")
                status = "success" if raw_status in ("success", "already") else "failed"
                tracker.record_sign_result("wuwa", "鸣潮", "库街区", status, result.get("message") or "")
            except Exception as exc:
                LOGGER.error("[启动前签到] 鸣潮库街区签到失败：%s", str(exc), exc_info=True)
                lines.append(f"鸣潮库街区启动前签到: {str(exc)}")
                tracker.record_sign_result("wuwa", "鸣潮", "库街区", "failed", str(exc))

    tajiduo_cfg = config.get(TAJIDUO_SIGN_CONFIG_KEY, {})
    if isinstance(tajiduo_cfg, dict) and tajiduo_cfg.get("startup_sign") and "NevernessToEverness" in app_keys:
        token = str(tajiduo_cfg.get("token") or "").strip()
        deviceid = str(tajiduo_cfg.get("deviceid") or "").strip()
        if token and deviceid:
            try:
                LOGGER.info("[启动前签到] 开始执行塔吉多（异环）签到")
                result = perform_tajiduo_sign(
                    token=token,
                    deviceid=deviceid,
                    uid=str(tajiduo_cfg.get("uid") or "0"),
                    do_bbs_signin=True,
                    do_game_signin=True,
                    phone=str(tajiduo_cfg.get("phone") or ""),
                    password=str(tajiduo_cfg.get("password") or ""),
                )
                for line in format_tajiduo_sign_report(result):
                    LOGGER.info("[启动前签到] %s", line)
                    lines.append(f"塔吉多启动前签到: {line}")
                _record_tajiduo_sign_to_tracker(tracker, result)
            except Exception as exc:
                LOGGER.error("[启动前签到] 塔吉多签到失败：%s", str(exc), exc_info=True)
                lines.append(f"塔吉多启动前签到: {str(exc)}")
                tracker.record_sign_result("nte", "异环", "BBS", "failed", str(exc))
                tracker.record_sign_result("nte", "异环", "游戏", "failed", str(exc))
        else:
            LOGGER.warning("[启动前签到] 塔吉多已开启启动前签到，但缺少 token 或 deviceid")

    skendfield_cfg = config.get(SKENDFIELD_SIGN_CONFIG_KEY, {})
    if isinstance(skendfield_cfg, dict) and skendfield_cfg.get("startup_sign") and "ArknightsEndfield" in app_keys:
        token = str(skendfield_cfg.get("token") or "").strip()
        server = str(skendfield_cfg.get("server") or "cn").strip()
        if token:
            try:
                LOGGER.info("[启动前签到] 开始执行森空岛终末地签到 (%s)", server)
                result = perform_skendfield_sign(token=token, server=server)
                for line in format_skendfield_sign_report(result):
                    LOGGER.info("[启动前签到] %s", line)
                    lines.append(f"森空岛启动前签到: {line}")
                _record_skendfield_sign_to_tracker(tracker, result)
            except Exception as exc:
                LOGGER.error("[启动前签到] 森空岛终末地签到失败：%s", str(exc), exc_info=True)
                lines.append(f"森空岛启动前签到: {str(exc)}")
                tracker.record_sign_result("endfield", "终末地", "签到", "failed", str(exc))
        else:
            LOGGER.warning("[启动前签到] 森空岛终末地已开启启动前签到，但缺少 token")

    from ..clients.endfield_protocol import ENDFIELD_PROTOCOL_CONFIG_KEY, perform_query as perform_endfield_protocol_query
    endfield_protocol_cfg = config.get(ENDFIELD_PROTOCOL_CONFIG_KEY, {})
    if isinstance(endfield_protocol_cfg, dict) and endfield_protocol_cfg.get("startup_sign") and "ArknightsEndfield" in app_keys:
        api_key = str(endfield_protocol_cfg.get("api_key") or "").strip()
        framework_token = str(endfield_protocol_cfg.get("framework_token") or "").strip()
        role_id = str(endfield_protocol_cfg.get("role_id") or "").strip()
        server_id = str(endfield_protocol_cfg.get("server_id") or "1").strip()
        if api_key and framework_token and role_id:
            try:
                LOGGER.info("[启动前签到] 开始执行终末地协议终端签到 (role_id=%s)", role_id)
                result = perform_endfield_protocol_query(
                    api_key=api_key, framework_token=framework_token,
                    role_id=role_id, server_id=server_id, query_type="attendance",
                )
                if result and result.get("code") == 0:
                    data = result.get("data", result)
                    msg = f"签到成功: {data.get('message', str(data)[:200])}"
                    lines.append(f"终末地协议终端启动前签到: {msg}")
                    tracker.record_sign_result("endfield", "终末地", "签到(协议终端)", "success", msg)
                else:
                    msg = str(result.get("message", str(result)[:200])) if result else "无响应"
                    lines.append(f"终末地协议终端启动前签到: {msg}")
                    tracker.record_sign_result("endfield", "终末地", "签到(协议终端)", "failed", msg)
            except Exception as exc:
                LOGGER.error("[启动前签到] 终末地协议终端签到失败：%s", str(exc), exc_info=True)
                lines.append(f"终末地协议终端启动前签到: {str(exc)}")
                tracker.record_sign_result("endfield", "终末地", "签到(协议终端)", "failed", str(exc))
        else:
            LOGGER.warning("[启动前签到] 终末地协议终端已开启启动前签到，但缺少 api_key/framework_token/role_id")

    if not lines:
        LOGGER.info("[启动前签到] 当前没有需要执行的签到任务")
    else:
        LOGGER.info("[启动前签到] 本轮共生成 %d 条结果", len(lines))

    return lines


def _record_mihoyo_sign_to_tracker(tracker: DailyProgressTracker, result: dict):
    """将米游社签到结果写入每日进度。"""
    game_names = {
        "genshin": "原神",
        "honkai_sr": "崩坏：星穹铁道",
        "zzz": "绝区零",
    }
    for game in result.get("games") or []:
        game_key = str(game.get("game_key") or "")
        if not game_key:
            continue
        game_name = game_names.get(game_key, game_key)
        accounts = game.get("accounts") or []
        if not accounts:
            tracker.record_sign_result(game_key, game_name, "米游社", "failed", "当前 Cookie 没有绑定可签到的账号")
            continue
        for account in accounts:
            uid = str(account.get("uid") or "")
            msg = str(account.get("message") or "")
            if account.get("status") == "success":
                tracker.record_sign_result(game_key, game_name, "米游社", "success", msg, uid=uid)
            elif account.get("status") == "already":
                tracker.record_sign_result(game_key, game_name, "米游社", "already", msg, uid=uid)
            else:
                tracker.record_sign_result(game_key, game_name, "米游社", "failed", msg, uid=uid)


def _record_tajiduo_sign_to_tracker(tracker: DailyProgressTracker, result: dict):
    """将塔吉多签到结果写入每日进度。

    game_key 统一用 "nte"（与 DailyView 卡片的 game_key 一致）。
    """
    bbs = result.get("bbs") or {}
    if bbs.get("status") == "success":
        tracker.record_sign_result("nte", "异环", "BBS", "success", bbs.get("message", ""))
    elif bbs.get("status") == "already":
        tracker.record_sign_result("nte", "异环", "BBS", "already", bbs.get("message", ""))
    elif bbs.get("status") == "failed":
        tracker.record_sign_result("nte", "异环", "BBS", "failed", bbs.get("message", ""))
    for game in result.get("game") or []:
        if game.get("status") == "success":
            tracker.record_sign_result("nte", "异环", game.get("name", "游戏"), "success", game.get("message", ""))
        elif game.get("status") == "already":
            tracker.record_sign_result("nte", "异环", game.get("name", "游戏"), "already", game.get("message", ""))
        elif game.get("status") == "failed":
            tracker.record_sign_result("nte", "异环", game.get("name", "游戏"), "failed", game.get("message", ""))


def _resolve_mihoyo_enabled_games(mihoyo_cfg: dict, app_keys: set[str]) -> List[str]:
    enabled = mihoyo_cfg.get("enabled_games") or {}
    app_mapping = {
        "GenshinImpact": "genshin",
        "HonkaiStarRail": "honkai_sr",
        "ZenlessZoneZero": "zzz",
        "ArknightsEndfield": "skendfield",
    }
    selected: List[str] = []
    for app_key, game_key in app_mapping.items():
        if app_key not in app_keys:
            continue
        if isinstance(enabled, dict):
            if bool(enabled.get(game_key, False)):
                selected.append(game_key)
    return selected


def _record_skendfield_sign_to_tracker(tracker: DailyProgressTracker, result: dict):
    """将森空岛终末地签到结果写入每日进度。

    game_key 统一用 "endfield"（与 DailyView 卡片的 game_key 一致），
    避免出现 skendfield / endfield 两张终末地卡片。
    """
    status = result.get("status", "")
    role = result.get("role", "")
    channel = result.get("channel", "")
    msg = result.get("message", "")
    detail = f"{role}({channel}): {msg}" if role else msg
    if status == "success":
        tracker.record_sign_result("endfield", "终末地", "签到", "success", detail)
    elif status == "already":
        tracker.record_sign_result("endfield", "终末地", "签到", "already", detail)
    elif status == "login_failed":
        tracker.record_sign_result("endfield", "终末地", "签到", "failed", f"登录失败: {msg}")
    else:
        tracker.record_sign_result("endfield", "终末地", "签到", "failed", detail)