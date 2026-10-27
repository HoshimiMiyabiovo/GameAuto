from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from game_auto.webui.auth import (
    check_rate_limit,
    create_session,
    destroy_session,
    get_token_from_request,
    has_account,
    set_account,
    verify_password,
    verify_token,
)

from game_auto.webui.state import logger as webui_logger

router = APIRouter(prefix="/api/auth", tags=["auth"])

class LoginIn(BaseModel):
    username: str
    password: str

class AccountIn(BaseModel):
    username: str
    password: str

@router.get("/status")
def status(request: Request):
    token = get_token_from_request(request)
    user = verify_token(token) if token else None
    return {"has_account": has_account(), "logged_in": bool(user), "username": user or ""}

@router.post("/login")
def login(payload: LoginIn, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    uname = (payload.username or "").strip()
    # 速率限制先记录
    if not check_rate_limit(ip):
        webui_logger.warning("[WebUI][登录] 频率限制 ip=%s user=%s", ip, uname or "-")
        return JSONResponse(status_code=429, content={"ok": False, "error": "请求过于频繁，请稍后再试"})
    if not has_account():
        webui_logger.warning("[WebUI][登录] 失败 ip=%s user=%s 原因=尚未设置账号", ip, uname or "-")
        return JSONResponse(status_code=400, content={"ok": False, "error": "尚未设置账号，请先在桌面 GUI 设置"})
    if uname != __import__("game_auto.webui.auth", fromlist=["get_username"]).get_username():
        # 仍走 verify 以防时序攻击，但直接比对用户名
        verify_password(payload.password)
        webui_logger.warning("[WebUI][登录] 失败 ip=%s user=%s 原因=账号或密码错误", ip, uname)
        return JSONResponse(status_code=401, content={"ok": False, "error": "账号或密码错误"})
    if not verify_password(payload.password):
        webui_logger.warning("[WebUI][登录] 失败 ip=%s user=%s 原因=账号或密码错误", ip, uname)
        return JSONResponse(status_code=401, content={"ok": False, "error": "账号或密码错误"})
    token = create_session(uname)
    webui_logger.info("[WebUI][登录] 成功 ip=%s user=%s", ip, uname)
    # HttpOnly Cookie + 返回 token 供 JS 存储
    response.set_cookie("webui_token", token, httponly=True, samesite="lax", max_age=12*3600, path="/")
    return {"ok": True, "token": token}

@router.post("/logout")
def logout(request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    token = get_token_from_request(request)
    user = verify_token(token) if token else None
    if token:
        destroy_session(token)
    webui_logger.info("[WebUI][登出] ip=%s user=%s", ip, user or "unknown")
    response.delete_cookie("webui_token", path="/")
    return {"ok": True}

@router.get("/me")
def me(request: Request):
    token = get_token_from_request(request)
    user = verify_token(token) if token else None
    if not user and has_account():
        return JSONResponse(status_code=401, content={"ok": False})
    return {"ok": True, "username": user or "", "has_account": has_account()}
