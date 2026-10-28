"""WebUI 账号密码与会话管理。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Dict, Optional

from fastapi import HTTPException, Request

from game_auto.settings import manager as cfg_manager
from game_auto.services.secure_store import is_available as sec_available, protect, unprotect, is_encrypted

# 存储键
WEBUI_SECTION = "webui_auth"
TOKEN_TTL_SEC = 12 * 3600  # 12h
# 内存会话 {token: {username, exp}}
_sessions: Dict[str, dict] = {}
_login_attempts: Dict[str, list] = {}  # ip -> [ts, ...]

def _pbkdf2_hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000)
    return base64.b64encode(dk).decode("ascii")

def _load_section() -> dict:
    cfg = cfg_manager.load()
    sec = cfg.get(WEBUI_SECTION) or {}
    # 解密 password_hash 若为 dpapi: 前缀（兼容）
    if isinstance(sec.get("password_hash"), str) and sec["password_hash"].startswith("dpapi:"):
        try:
            sec["password_hash"] = unprotect(sec["password_hash"])
        except Exception:
            pass
    return sec

def _save_section(sec: dict):
    cfg = cfg_manager.load(force=True)
    # 加密 password_hash 落盘
    to_save = dict(sec)
    if to_save.get("password_hash") and not str(to_save["password_hash"]).startswith("dpapi:"):
        if sec_available():
            try:
                to_save["password_hash"] = protect(str(to_save["password_hash"]))
            except Exception:
                pass
    cfg[WEBUI_SECTION] = to_save
    cfg_manager.save(cfg)

def has_account() -> bool:
    sec = _load_section()
    return bool(sec.get("username") and sec.get("password_hash") and sec.get("salt"))

def get_username() -> str:
    return str(_load_section().get("username") or "")

def ensure_default_account():
    """首次启动无账号时不强制创建，由 GUI 引导设置；此处仅保证结构存在。"""
    sec = _load_section()
    if not sec:
        return
    # 若旧明文密码仍以明文存于 password_hash（无 dpapi 前缀且非空），尝试加密回写
    ph = str(sec.get("password_hash") or "")
    if ph and not ph.startswith("dpapi:") and sec_available():
        try:
            sec["password_hash"] = protect(ph)
            _save_section(sec)
        except Exception:
            pass

def set_account(username: str, password: str):
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        raise ValueError("账号与密码不能为空")
    if len(username) < 3 or len(username) > 32:
        raise ValueError("账号长度 3-32")
    if len(password) < 6 or len(password) > 64:
        raise ValueError("密码长度 6-64")
    salt = secrets.token_hex(16)
    phash = _pbkdf2_hash(password, salt)
    sec = {
        "username": username,
        "salt": salt,
        "password_hash": phash,
        "updated_at": int(time.time()),
    }
    _save_section(sec)
    # 改密/重设账号后让已有 WebUI 会话立即失效
    _sessions.clear()

def verify_password(password: str) -> bool:
    sec = _load_section()
    salt = str(sec.get("salt") or "")
    expect = str(sec.get("password_hash") or "")
    if not salt or not expect:
        return False
    # 若 expect 仍为 dpapi 密文，解密
    if expect.startswith("dpapi:"):
        try:
            expect = unprotect(expect)
        except Exception:
            return False
    calc = _pbkdf2_hash(str(password or ""), salt)
    return hmac.compare_digest(calc, expect)

def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"username": username, "exp": int(time.time()) + TOKEN_TTL_SEC}
    # 清理过期
    now = int(time.time())
    for k in list(_sessions.keys()):
        if _sessions[k].get("exp", 0) < now:
            _sessions.pop(k, None)
    return token

def verify_token(token: str) -> Optional[str]:
    if not token:
        return None
    info = _sessions.get(token)
    if not info:
        return None
    if int(info.get("exp", 0)) < int(time.time()):
        _sessions.pop(token, None)
        return None
    return str(info.get("username"))

def destroy_session(token: str):
    _sessions.pop(token, None)

def check_rate_limit(ip: str) -> bool:
    now = time.time()
    lst = _login_attempts.get(ip, [])
    # 清理 60s 外
    lst = [t for t in lst if now - t < 60]
    if len(lst) >= 5:
        return False
    lst.append(now)
    _login_attempts[ip] = lst
    return True

def get_token_from_request(request: Request) -> str:
    # Header Authorization: Bearer <token>  或 Cookie webui_token
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    ck = request.cookies.get("webui_token") or ""
    if ck:
        return ck.strip()
    return ""

def require_auth(request: Request) -> str:
    token = get_token_from_request(request)
    user = verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user
