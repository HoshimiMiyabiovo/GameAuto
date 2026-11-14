from __future__ import annotations

import glob, os, threading
from fastapi import APIRouter
from game_auto.ui.webui.state import logger, release_running, try_acquire_running

router = APIRouter(prefix="/api/wool", tags=["wool"])

@router.get("/flows")
def flows():
    import json
    from game_auto.runtime.foreground_flow_runner import _load_flow_config
    from game_auto.runtime.background_template_runner import WOOL_FLOW_KEYS
    from game_auto.settings import manager as cm
    overrides = cm.load().get("flow_exe_paths") or {}
    meta = {"MINIMAX":"MiniMax Code 每日签到","WORKBUDDY":"WorkBuddy 每日领取","TRAE":"TRAE SOLO 每日签到","QODER":"Qoder 每日积分领取","TELEAGENT":"TeleAgent 每日签到"}
    out=[]
    for key in WOOL_FLOW_KEYS:
        cfg = _load_flow_config(key) or {}
        exe = str(overrides.get(key) or "").strip()
        exe_ready = bool(exe) and bool(os.path.exists(exe) or glob.glob(exe))
        out.append({"key":key,"name":meta.get(key,key),"enabled":bool(cfg.get("enabled",True)),"close_on_finish":bool(cfg.get("close_process_on_finish",True)),"exe_ready":exe_ready,"exe_path":exe})
    return {"flows": out}

@router.post("/run")
def run():
    # 锁内原子「检查空闲→置运行」（审计 P1-4 同款修复，见 api/automation.py）
    if not try_acquire_running(
        "webui",
        queue=[{"app_key": "WoolFlow", "display_name": "薅羊毛集合", "script_choice": "", "state": "running"}],
    ):
        return {"ok": False, "error": "已有任务运行中"}
    logger.info("[WebUI][运行] 发起薅羊毛")
    def _bg():
        try:
            from game_auto.runtime.background_template_runner import run_wool_background
            ok, msg = run_wool_background(logger)
            from game_auto.ui.webui.state import set_last_result
            set_last_result([msg], [])
            logger.info("[WebUI][薅羊毛] %s %s", "成功" if ok else "部分失败", msg)
        except Exception as exc:
            logger.exception("[WebUI][薅羊毛] 异常")
            from game_auto.ui.webui.state import set_last_result
            set_last_result([f"薅羊毛异常: {exc}"], [])
        finally:
            release_running("webui")
            logger.info("[WebUI][运行] 薅羊毛结束")
    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True}

@router.post("/flows/{key}/toggle")
def toggle(key: str, payload: dict):
    enabled = bool(payload.get("enabled"))
    field = payload.get("field") or "enabled"
    if field not in ("enabled", "close_process_on_finish"):
        return {"ok": False, "error": "unsupported field"}
    from game_auto.settings.paths import ensure_flow_config_path, FLOW_CONFIG_LOCK
    from game_auto.settings.manager import atomic_write_json
    import json
    path = ensure_flow_config_path()
    # flow_configs.json 读-改-写全程持锁：WebUI toggle 与 GUI 侧
    # _save_wool_flow_field 并发时后写者会丢更新（审计 P2-1 同族）
    with FLOW_CONFIG_LOCK:
        with open(path, encoding="utf-8") as f:
            all_cfg = json.load(f)
        sec = all_cfg.get(key)
        if not isinstance(sec, dict):
            return {"ok": False, "error": "unknown flow"}
        sec[field] = bool(enabled)
        atomic_write_json(path, all_cfg)
    return {"ok": True}
