from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import List

from fastapi import APIRouter
from fastapi.responses import FileResponse

from game_auto.services.daily_progress_tracker import get_tracker
from game_auto.settings.paths import BASE_DIR, BAIJING_VERIFY_DIR, FLOW_FAILURE_DIR, WORKFLOW_DB_PATH
from game_auto.settings import resolve_game_key
from game_auto.settings.defaults import GAME_KEYS

logger = logging.getLogger("GameAutomation.WebUI")

router = APIRouter(prefix="/api/daily", tags=["daily"])

def _load_snapshot():
    tracker = get_tracker(str(WORKFLOW_DB_PATH))
    snap = tracker.load_today()
    merged = {}
    for game in snap.games:
        gk = resolve_game_key(game.game_key)
        tasks = []
        for task in game.tasks:
            extra = task.extra if isinstance(task.extra, dict) else {}
            v_rel = extra.get("verify_image") if isinstance(extra.get("verify_image"), str) else None
            v_url = None
            if v_rel:
                p = Path(v_rel)
                if not p.is_absolute():
                    p = BASE_DIR / v_rel
                if p.exists():
                    # 转为可通过 /api/daily/verify-image 访问的 URL
                    v_url = f"/api/daily/verify-image/{Path(v_rel).name}"
            tasks.append({
                "task_key": task.task_key,
                "task_name": task.task_name,
                "status": task.status,
                "message": task.message,
                "current": extra.get("current"),
                "total": extra.get("total"),
                "verify_image": v_rel,
                "verify_image_url": v_url,
                "extra": extra,
            })
        if gk in merged:
            merged[gk]["tasks"].extend(tasks)
            merged[gk]["errors"].extend(game.errors or [])
            if game.is_all_success:
                merged[gk]["is_all_success"] = True
            if game.has_failed:
                merged[gk]["has_failed"] = True
        else:
            merged[gk] = {
                "game_key": gk,
                "game_name": game.game_name or GAME_KEYS.get(gk, gk),
                "uid": game.uid,
                "nickname": game.nickname,
                "is_all_success": game.is_all_success,
                "has_failed": game.has_failed,
                "progress_percent": game.progress_percent,
                "tasks": tasks,
                "errors": list(game.errors or []),
            }
    games = list(merged.values())
    # 注入 BaiJing 占位（与桌面桥一致）
    has_baijing = any(resolve_game_key(g.get("game_key",""))=="baijing" for g in games)
    if not has_baijing:
        games.append({
            "game_key": "baijing",
            "game_name": "白荆回廊",
            "uid": "",
            "nickname": "",
            "is_all_success": False,
            "has_failed": False,
            "progress_percent": 0,
            "tasks": [
                {"task_key":"enter_game","task_name":"进入游戏","status":"pending","message":"待一条龙执行"},
                {"task_key":"daily","task_name":"日常任务","status":"pending","message":"待执行"},
                {"task_key":"verify","task_name":"验证","status":"pending","message":"待验证截图"},
                {"task_key":"weekly","task_name":"每周商店购买","status":"pending","message":"待周常（每周一次）"},
            ],
            "errors": []
        })
    return {"date_key": snap.date_key, "games": games}

@router.get("/progress")
def progress():
    return _load_snapshot()

@router.post("/check")
def check(payload: dict):
    # 2026-11-09 审计 P2：check_games_progress 逐游戏打外部 API（桌面端按
    # 120s 口径设计），sync 端点直跑会长时间占用 uvicorn 线程池令牌，多击
    # 拖死 /api/apps/status 轮询。改后台线程跑，响应立即返回当前快照，
    # 进度经轮询 /api/daily/progress 看到（桌面 GUI 同款模式）。
    games = payload.get("games")
    threading.Thread(
        target=_run_progress_check,
        args=(games,),
        daemon=True,
    ).start()
    return _load_snapshot()


def _run_progress_check(games):
    """后台执行每日进度查询并写库；异常只记日志不外抛。"""
    try:
        from game_auto.clients.daily_checker import check_games_progress
        tracker = get_tracker(str(WORKFLOW_DB_PATH))
        check_games_progress(game_keys=games if games else None, tracker=tracker)
    except Exception as exc:
        logger.warning("[WebUI][每日] check_games_progress 失败: %s", exc)


def _safe_image_path(base_dir, name: str):
    """白名单目录内的文件路径；非法名/越界返回 None。

    2026-11-09 审计 P2：仅过滤 / \\ .. 挡不住 pathlib 的盘符相对路径
    语义——`WindowsPath(...) / "C:bootmgr"` 整体替换为盘根。读出前统一
    `resolve().is_relative_to(base)` 校验（桌面端 openFlowFailureImage 同款）。
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    p = Path(base_dir) / name
    try:
        resolved = p.resolve()
        if not resolved.is_relative_to(Path(base_dir).resolve()):
            return None
    except OSError:
        return None
    return resolved


@router.get("/verify-image/{name}")
def verify_image(name: str):
    p = _safe_image_path(BAIJING_VERIFY_DIR, name)
    if p is None or not p.is_file():
        return {"error": "not found"}
    return FileResponse(str(p), media_type="image/jpeg")


# ── 失败诊断记录（flow_failures 表 + artifacts/flow_failures 截图） ──

def _load_failures(days: int, limit: int, flow: str = ""):
    from game_auto.storage.workflow_db import WorkflowDB

    days = max(1, min(int(days or 7), 365))
    limit = max(1, min(int(limit or 50), 200))
    db = WorkflowDB(str(WORKFLOW_DB_PATH))
    summary = db.summarize_flow_failures(days=days, group_limit=20)
    rows = db.query_flow_failures(days=days, limit=limit, flow_key=flow)
    items = []
    for r in rows:
        item = dict(r)
        img = item.get("image_path") or ""
        item["image_url"] = f"/api/daily/failure-image/{img}" if img and (FLOW_FAILURE_DIR / img).exists() else ""
        item["time_text"] = time.strftime("%m-%d %H:%M:%S", time.localtime(item.get("ts") or 0))
        item["ocr_texts"] = (item.get("ocr_texts") or [])[:10]
        items.append(item)
    return {"days": days, "summary": summary, "items": items}


@router.get("/failures")
def failures(days: int = 7, limit: int = 50, flow: str = ""):
    """最近失败记录：聚合排行 + 明细（含证据截图 URL）。"""
    try:
        return _load_failures(days, limit, flow)
    except Exception as exc:
        return {"days": days, "summary": [], "items": [], "error": str(exc)}


@router.get("/failure-image/{name}")
def failure_image(name: str):
    """失败当刻截图（artifacts/flow_failures/）。"""
    p = _safe_image_path(FLOW_FAILURE_DIR, name)
    if p is None or not p.is_file():
        return {"error": "not found"}
    return FileResponse(str(p), media_type="image/jpeg")
