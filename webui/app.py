"""GameAuto WebUI — FastAPI 入口。"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from game_auto.webui.auth import has_account, verify_token, get_token_from_request
from game_auto.webui.state import ensure_log_handler, get_recent_logs, logger

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
WEBUI_DEFAULT_PORT = 60171

app = FastAPI(title="GameAuto WebUI", version="0.1.0")

# 挂载 API（auth 无需鉴权，其余由中间件保护）
from .api.auth_api import router as auth_router
from .api.automation import router as auto_router
from .api.daily import router as daily_router
from .api.wool import router as wool_router
from .api.clickseq import router as click_router

app.include_router(auth_router)
app.include_router(auto_router)
app.include_router(daily_router)
app.include_router(wool_router)
app.include_router(click_router)

# GZip 压缩 JSON/日志（500B 以上自动压缩，降低 60-80% 传输）
app.add_middleware(GZipMiddleware, minimum_size=500)
# CORS 只放行 WebUI 自己的精确 origin，避免本机任意端口的页面读取接口
_WEBUI_ORIGINS = [
    f"http://localhost:{WEBUI_DEFAULT_PORT}",
    f"http://127.0.0.1:{WEBUI_DEFAULT_PORT}",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_WEBUI_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 鉴权中间件：除登录/健康检查外，所有 /api/* 一律要求 token
_WHITELIST = ("/api/auth/", "/api/health")

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # CORS 预检不带 Authorization，放行给 CORS 中间件处理
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if path.startswith("/api/") and not any(path.startswith(w) for w in _WHITELIST):
        token = get_token_from_request(request)
        user = verify_token(token) if token else None
        if not user:
            error = "未登录" if has_account() else "尚未设置 WebUI 账号"
            return JSONResponse(status_code=401, content={"ok": False, "error": error})
    return await call_next(request)

# 静态资源
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.on_event("startup")
def _startup():
    ensure_log_handler()
    from game_auto.webui.auth import ensure_default_account
    ensure_default_account()
    if not has_account():
        logger.warning("[WebUI] 尚未设置账号：所有 API 将返回 401，请在桌面 GUI 的 WebUI 设置页创建账号")
    logger.info(f"[WebUI] 启动成功 http://localhost:{WEBUI_DEFAULT_PORT}")

@app.get("/", response_class=HTMLResponse)
def index():
    html = STATIC_DIR / "index.html"
    if html.exists():
        return html.read_text(encoding="utf-8")
    return "<h3>GameAuto WebUI</h3><p>static/index.html 缺失</p>"

@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.1.0"}

# 简单日志轮询（WS 主推，轮询兜底；limit 收敛 + 增量 since 降低 80% 传输）
@app.get("/api/logs")
def logs(limit: int = 100, q: str = "", login_only: bool = False, since: int = 0):
    limit = max(1, min(int(limit), 500))
    if login_only:
        from game_auto.webui.state import get_login_logs
        logs_data = get_login_logs(limit)
        if q:
            logs_data = [l for l in logs_data if q in l]
        return {"logs": logs_data}
    # 增量模式：since>0 时仅返回游标之后的新日志
    if since and not q:
        from game_auto.webui.state import get_logs_since
        data, cursor = get_logs_since(int(since), limit)
        return {"logs": data, "cursor": cursor}
    from game_auto.webui.state import get_log_cursor
    data = get_recent_logs(limit)
    if q:
        q = q[:80]
        data = [l for l in data if q in l]
        return {"logs": data}
    return {"logs": data, "cursor": get_log_cursor()}

@app.get("/api/logs/login")
def login_logs(limit: int = 100):
    from game_auto.webui.state import get_login_logs
    limit = max(1, min(int(limit), 500))
    return {"logs": get_login_logs(limit)}

# WebSocket 日志推送（需鉴权，低频轮询降低 CPU）
@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    token = ws.query_params.get("token") or ws.cookies.get("webui_token") or ws.headers.get("authorization","").replace("Bearer ","")
    if not verify_token(token or ""):
        await ws.close(code=4401)
        return
    await ws.accept()
    ensure_log_handler()
    for line in get_recent_logs(30):
        await ws.send_text(line)
    import asyncio
    # 轻量增量：基于缓冲长度，避免大 limit 拷贝
    last_len = len(get_recent_logs(500))
    try:
        while True:
            await asyncio.sleep(1.5)
            cur = get_recent_logs(500)
            if len(cur) > last_len:
                for line in cur[last_len:]:
                    await ws.send_text(line)
                last_len = len(cur)
            elif len(cur) < last_len:
                # 缓冲被清空或回绕
                last_len = len(cur)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass

_webui_thread = None
_webui_lock = threading.Lock()


def start_webui_background() -> bool:
    """已设置账号时后台启动 WebUI；返回是否已在运行/已启动。"""
    from game_auto.webui.auth import has_account

    if not has_account():
        return False
    global _webui_thread
    with _webui_lock:
        if _webui_thread is not None and _webui_thread.is_alive():
            return True

        def _run():
            try:
                import uvicorn
                uvicorn.run(
                    app,
                    host="127.0.0.1",
                    port=int(WEBUI_DEFAULT_PORT),
                    log_level="warning",
                    access_log=False,
                )
            except Exception as exc:
                logger.warning("[WebUI] 启动失败（端口占用或依赖缺失）: %s", exc)

        _webui_thread = threading.Thread(target=_run, name="WebUI-Uvicorn", daemon=True)
        _webui_thread.start()
        logger.info("[WebUI] 后台服务已启动 http://127.0.0.1:%s", WEBUI_DEFAULT_PORT)
        return True


# 便于 `python -m game_auto.webui.app`
if __name__ == "__main__":
    from game_auto.webui.auth import has_account
    if not has_account():
        raise SystemExit("未设置 WebUI 账号：请先在桌面 GUI 的 WebUI 设置页创建账号")
    import uvicorn
    port = int(os.environ.get("WEBUI_PORT", str(WEBUI_DEFAULT_PORT)))
    uvicorn.run("game_auto.webui.app:app", host="127.0.0.1", port=port, reload=False)
