from __future__ import annotations

import threading
from typing import List, Optional

from fastapi import APIRouter, Request

from game_auto.settings import APP_ORDER, APPS, get_app_display_name, get_script_display_name, refresh_runtime_config
from game_auto.storage.workflow_db import WorkflowDB
from game_auto.settings.paths import WORKFLOW_DB_PATH
from game_auto.webui.state import get_controller, get_last_result, get_run_status, is_running, set_last_result, set_running, logger, update_queue_state

router = APIRouter(prefix="/api/apps", tags=["automation"])

def _load_apps():
    refresh_runtime_config(force=True)
    from game_auto.storage.workflow_db import WorkflowDB
    db = WorkflowDB(str(WORKFLOW_DB_PATH))
    prefs = db.get_all_app_launch_preferences()
    items = []
    for idx, app_key in enumerate(APP_ORDER):
        cfg = APPS.get(app_key, {})
        items.append({
            "order": idx + 1,
            "app_key": app_key,
            "display_name": get_app_display_name(app_key),
            "enabled": bool(prefs.get(app_key, {}).get("enabled", False)),
            "script_choice": prefs.get(app_key, {}).get("script_choice", ""),
            "launcher_labels": [o.get("display_name", o.get("name","")) for o in cfg.get("launcher_options", [])],
            "launcher_values": [o.get("name","") for o in cfg.get("launcher_options", [])],
            "task_type": cfg.get("task_type", "常规任务"),
            "description": cfg.get("description", ""),
        })
    return items

@router.get("")
def list_apps():
    return {"apps": _load_apps()}

@router.post("/prefs")
def save_prefs(payload: dict):
    prefs = payload.get("prefs") or []
    from game_auto.storage.workflow_db import WorkflowDB
    db = WorkflowDB(str(WORKFLOW_DB_PATH))
    for p in prefs:
        db.save_app_launch_preference(p.get("app_key",""), bool(p.get("enabled")), p.get("script_choice",""), False)
    return {"ok": True}

@router.post("/reorder")
def reorder(payload: dict):
    order = payload.get("order") or []
    from game_auto.settings import manager as cfg_manager
    known = set(APP_ORDER)
    cleaned = [k for k in order if k in known]
    for k in APP_ORDER:
        if k not in cleaned:
            cleaned.append(k)
    cfg_manager.update_app_order(cleaned)
    return {"ok": True, "order": cleaned}

@router.get("/status")
def status(request: Request):
    from fastapi.responses import JSONResponse as _JR
    import hashlib, json as _json
    ctrl = get_controller()
    st = get_run_status()
    stop_req = bool(getattr(ctrl, "stop_current_task", False))
    try:
        from game_auto.ui.bridge import get_active_bridge
        br = get_active_bridge()
        if br is not None and getattr(br, "_controller", None) is not None:
            if bool(getattr(br._controller, "stop_current_task", False)):
                stop_req = True
    except Exception as exc:
        logger.debug("[WebUI] 读取共享控制器停止标志失败: %s", exc)
    etag_base = {
        "running": st.get("running", False),
        "stop_requested": stop_req,
        "queue": st.get("queue", []),
        "current": st.get("current"),
        "source": st.get("source", ""),
        "last_report": st.get("last_report", []),
    }
    etag = hashlib.md5(_json.dumps(etag_base, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    req_etag = request.headers.get("if-none-match") if request else None
    # 运行中 elapsed 每秒变化，不走 304 以保证时长实时；空闲时才 304 节省传输
    if not st.get("running") and req_etag and req_etag == etag:
        return _JR(status_code=304, content=None, headers={"ETag": etag})
    payload = {"running": st.get("running", False), "stop_requested": stop_req, "last": st, "queue": st.get("queue", []), "current": st.get("current"), "elapsed_sec": st.get("elapsed_sec", 0), "source": st.get("source", "")}
    return _JR(content=payload, headers={"ETag": etag, "Cache-Control": "no-cache"})

@router.post("/run")
def run(payload: dict):
    if is_running():
        return {"ok": False, "error": "已有任务运行中"}
    apps = payload.get("apps")  # [{"app_key":..., "script_choice":...}]
    force = bool(payload.get("forceRerun"))
    # 若未指定 apps，则用已启用的偏好
    if not apps:
        items = _load_apps()
        apps = [{"app_key": it["app_key"], "script_choice": it["script_choice"]} for it in items if it["enabled"]]
    if not apps:
        return {"ok": False, "error": "未选择任何任务"}
    # 校验
    target = []
    for it in apps:
        ak = it.get("app_key")
        if ak in APPS:
            target.append((ak, it.get("script_choice") or None))
    if not target:
        return {"ok": False, "error": "无有效任务"}

    ctrl = get_controller()
    # 构建队列用于 WebUI 实时展示（包含显示名与状态）
    queue = []
    for ak, sc in target:
        queue.append({
            "app_key": ak,
            "display_name": get_app_display_name(ak),
            "script_choice": sc or "",
            "state": "pending",
        })
    set_running(True, queue=queue, source="webui")
    logger.info("[WebUI][运行] 发起任务 %s", ", ".join([f"{a[0]}:{a[1] or ''}" for a in target]))
    # 同步到 GUI 桥接（若 GUI 正在运行，前端可实时看到 WebUI 发起的队列）
    try:
        from game_auto.ui.bridge import get_active_bridge
        br = get_active_bridge()
        if br is not None:
            # 将 WebUI 队列映射为 Bridge 的 queue_list 格式
            q_items = []
            for q in queue:
                q_items.append({
                    "app_key": q["app_key"],
                    "task_name": f"{q['display_name']}({q['script_choice'] or ''})".strip("()"),
                    "script_label": q["script_choice"],
                    "state": "待执行",
                })
            br.emit_event("queue_list", items=q_items)
            br.emit_event("running", value=True)
            br.emit_event("status", title="正在运行", body="WebUI 发起任务", tone="running")
    except Exception as exc:
        logger.debug("[WebUI] 同步 GUI 运行状态失败: %s", exc)

    def _bg():
        try:
            from game_auto.runtime.task_batch_runner import run_task_batch, TaskBatchHooks

            def on_before(app_key, disp, script):
                update_queue_state(app_key, "running", disp)
                logger.info("[WebUI][运行] 正在执行 %s", disp)
                try:
                    from game_auto.ui.bridge import get_active_bridge
                    br = get_active_bridge()
                    if br is not None:
                        br.emit_event("queue_state", app_key=app_key, state="运行中")
                        br.emit_event("queue_list_update", app_key=app_key, state="运行中")
                        br.emit_event("status", title="正在运行", body=f"正在执行: {disp}", tone="running")
                except Exception as exc:
                    logger.debug("[WebUI] 同步 GUI 队列状态失败: %s", exc)

            def on_after(res):
                ak = getattr(res, "app_key", "") or ""
                st = getattr(res, "state", "")
                # 映射内部 state 到 WebUI 展示状态
                mapping = {"success": "completed", "failed": "failed", "skipped": "skipped", "stopped": "stopped", "warning": "completed"}
                ws_state = mapping.get(st, "completed" if getattr(res, "success", False) else "failed")
                update_queue_state(ak, ws_state, getattr(res, "message", ""))
                try:
                    from game_auto.ui.bridge import get_active_bridge
                    br = get_active_bridge()
                    if br is not None:
                        q_state = "已完成" if ws_state == "completed" else "失败" if ws_state == "failed" else "待执行" if ws_state == "pending" else "跳过" if ws_state == "skipped" else "已停止"
                        br.emit_event("queue_state", app_key=ak, state=q_state)
                        br.emit_event("queue_list_update", app_key=ak, state=q_state)
                except Exception as exc:
                    logger.debug("[WebUI] 同步 GUI 队列完成状态失败: %s", exc)

            hooks = TaskBatchHooks(on_before_app=lambda k, d, s: on_before(k, d, s), on_after_app=lambda r: on_after(r))
            result = run_task_batch(ctrl, target, force_rerun=force, hooks=hooks)
            set_last_result(result.report, result.warnings)
            for line in result.report:
                logger.info("[WebUI] %s", line)
        except Exception as exc:
            logger.exception("[WebUI] 任务异常")
            set_last_result([f"执行异常: {exc}"], [])
        finally:
            set_running(False)
            logger.info("[WebUI][运行] 任务结束")
            try:
                from game_auto.ui.bridge import get_active_bridge
                br = get_active_bridge()
                if br is not None:
                    br.emit_event("finished", toast=True)
            except Exception as exc:
                logger.debug("[WebUI] 通知 GUI 任务结束失败: %s", exc)

    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "started": [f"{a[0]}:{a[1] or ''}" for a in target]}

@router.post("/stop")
def stop():
    ctrl = get_controller()
    ctrl.stop_current_task = True
    # 同时停止 GUI 侧控制器（同进程双控制器场景）
    try:
        from game_auto.ui.bridge import get_active_bridge
        br = get_active_bridge()
        if br is not None and getattr(br, "_controller", None) is not None:
            try:
                br._controller.stop_current_task = True
            except Exception as exc:
                logger.debug("[WebUI] 同步 GUI 控制器停止标志失败: %s", exc)
            # 若共享状态仍为 running，也标记为停止
            try:
                from game_auto.webui.state import update_queue_state
                # 将当前运行项标记为 stopped
                from game_auto.webui.state import get_run_status
                st = get_run_status()
                cur = st.get("current") or {}
                if cur.get("app_key"):
                    update_queue_state(cur["app_key"], "stopped", "用户停止")
            except Exception as exc:
                logger.debug("[WebUI] 标记队列停止状态失败: %s", exc)
    except Exception as exc:
        logger.debug("[WebUI] 停止 GUI 侧控制器失败: %s", exc)
    logger.info("[WebUI][运行] 收到停止请求")
    return {"ok": True}

@router.get("/logs")
def logs(limit: int = 100, q: str = "", login_only: bool = False, since: int = 0):
    limit = max(1, min(int(limit), 500))
    if login_only:
        from game_auto.webui.state import get_login_logs
        data = get_login_logs(limit)
        if q:
            q = q[:80]
            data = [l for l in data if q in l]
        return {"logs": data}
    if since and not q:
        from game_auto.webui.state import get_logs_since
        data, cursor = get_logs_since(int(since), limit)
        return {"logs": data, "cursor": cursor}
    from game_auto.webui.state import get_recent_logs, get_log_cursor
    data = get_recent_logs(limit)
    if q:
        q = q[:80]
        data = [l for l in data if q in l]
        return {"logs": data}
    return {"logs": data, "cursor": get_log_cursor()}
