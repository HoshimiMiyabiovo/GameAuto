from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List

from fastapi import APIRouter
from fastapi.responses import FileResponse

from game_auto.services.daily_progress_tracker import get_tracker
from game_auto.settings.paths import BASE_DIR, BAIJING_VERIFY_DIR, FLOW_FAILURE_DIR, WORKFLOW_DB_PATH
from game_auto.settings import resolve_game_key
from game_auto.settings.defaults import GAME_KEYS

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
    games = payload.get("games")
    # 复用桌面逻辑：check_games_progress + 持久化
    try:
        from game_auto.clients.daily_checker import check_games_progress
        tracker = get_tracker(str(WORKFLOW_DB_PATH))
        result = check_games_progress(game_keys=games if games else None, tracker=tracker)
        # check_games_progress 已写入 tracker，直接返回最新快照
        return _load_snapshot()
    except Exception as exc:
        return {"error": str(exc), **_load_snapshot()}

@router.get("/verify-image/{name}")
def verify_image(name: str):
    # 仅允许 jpg/png，避免路径穿越
    if "/" in name or "\\" in name or ".." in name:
        return {"error": "invalid name"}
    p = BAIJING_VERIFY_DIR / name
    if not p.exists():
        # 尝试按日期前缀模糊
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
    if "/" in name or "\\" in name or ".." in name:
        return {"error": "invalid name"}
    p = FLOW_FAILURE_DIR / name
    if not p.exists():
        return {"error": "not found"}
    return FileResponse(str(p), media_type="image/jpeg")
