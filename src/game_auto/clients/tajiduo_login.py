"""塔吉多（异环社区）登录模块。

复刻 https://github.com/SkyBlue997/tjd-daily 的登录逻辑，
支持：
- 账号密码登录（推荐，可自动重登）
- 短信验证码登录
- 用已有 token 直接登录

依赖：pycryptodome（AES 加密手机号/密码/验证码）
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import logging
import os
import random
import secrets
import string
import sys
import time
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("GameAutomation")

# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------
LAOHU_HOST = "https://user.laohu.com"
BBS_HOST = "https://bbs-api.tajiduo.com"
APP_ID = "10551"
BID = "com.pwrd.htassistant"
CHANNEL_ID = "2"
SDK_VER = "4.131.0"
APP_VER = "1.2.2"
LH_SECRET = "5fd254cc0c8740d7a57376415ce40ede"
AES_KEY = b"a57376415ce40ede"
BBS_SECRET = "pUds3dfMkl"
UA_LH = "HTAssistant/1.2.2 (iPhone; iOS 26.1; Scale/3.00)"
UA_BBS = "HTAssistant/106 CFNetwork/3860.200.71 Darwin/25.1.0"
ALPH = string.ascii_letters + string.digits


class TajiduoLoginError(RuntimeError):
    """塔吉多登录错误。"""


# ---------------------------------------------------------------------------
# AES 加密
# ---------------------------------------------------------------------------

def _require_aes():
    try:
        from Crypto.Cipher import AES  # type: ignore
        return AES
    except ImportError:
        raise TajiduoLoginError("需要 pycryptodome 才能加密手机号/密码/验证码: pip install pycryptodome")


def _aes_encrypt(plaintext: str) -> str:
    AES = _require_aes()
    raw = plaintext.encode("utf-8")
    pad = 16 - (len(raw) % 16)
    raw += bytes([pad]) * pad
    return base64.b64encode(AES.new(AES_KEY, AES.MODE_ECB).encrypt(raw)).decode()


# ---------------------------------------------------------------------------
# 签名与 ds
# ---------------------------------------------------------------------------

def _laohu_sign(params: Dict[str, str]) -> str:
    concat = "".join(params[k] for k in sorted(params))
    return hashlib.md5((concat + LH_SECRET).encode("utf-8")).hexdigest()


def _make_ds() -> str:
    ts = str(int(time.time()))
    nonce = "".join(random.choice(ALPH) for _ in range(8))
    sig = hashlib.md5(f"{ts}{nonce}{APP_VER}{BBS_SECRET}".encode()).hexdigest()
    return f"{ts},{nonce},{sig}"


# ---------------------------------------------------------------------------
# 设备指纹
# ---------------------------------------------------------------------------

def gen_fingerprint() -> Dict[str, str]:
    return {
        "deviceid": secrets.token_hex(16).upper(),
        "openudid": str(uuid.uuid4()).upper(),
        "vendorid": str(uuid.uuid4()).upper(),
    }


# ---------------------------------------------------------------------------
# 基础参数
# ---------------------------------------------------------------------------

def _base_params(deviceid: str, openudid: str, vendorid: str, *, t_ms: bool = False) -> Dict[str, str]:
    return {
        "adid": deviceid,
        "adm": "",
        "appId": APP_ID,
        "bid": BID,
        "channelId": CHANNEL_ID,
        "deviceId": deviceid,
        "iOSAppOnMac": "0",
        "mac": deviceid,
        "openudid": openudid,
        "osType": "1",
        "sdkVersion": SDK_VER,
        "t": str(int(time.time() * (1000 if t_ms else 1))),
        "vendorid": vendorid,
        "version": APP_VER,
    }


# ---------------------------------------------------------------------------
# HTTP 请求
# ---------------------------------------------------------------------------

def _laohu_post(path: str, params: Dict[str, str]) -> Tuple[int, Any]:
    body = dict(params)
    body["sign"] = _laohu_sign(body)
    data = urlencode(body).encode()
    req = Request(
        LAOHU_HOST + path,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UA_LH,
            "Host": "user.laohu.com",
        },
    )
    try:
        with urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {"code": e.code, "message": str(e)}


def _bbs_login_post(laohu_token: str, user_id: str, deviceid: str) -> Tuple[int, Any]:
    data = urlencode({
        "token": laohu_token,
        "userIdentity": str(user_id),
        "appId": APP_ID,
    }).encode()
    req = Request(
        BBS_HOST + "/usercenter/api/login",
        data=data,
        method="POST",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Authorization": "",
            "appversion": APP_VER,
            "platform": "ios",
            "uid": "0",
            "deviceid": deviceid,
            "ds": _make_ds(),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UA_BBS,
            "Host": "bbs-api.tajiduo.com",
        },
    )
    try:
        with urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {"code": e.code, "message": str(e)}


# ---------------------------------------------------------------------------
# 登录步骤
# ---------------------------------------------------------------------------

def send_captcha(phone: str, deviceid: str, openudid: str, vendorid: str) -> Tuple[int, Any]:
    """发送短信验证码。"""
    p = _base_params(deviceid, openudid, vendorid)
    p.update({"areaCodeId": "1", "cellphone": phone, "type": "18"})
    return _laohu_post("/m/newApi/sendPhoneCaptchaWithOutLogin", p)


def login_by_password(
    username: str,
    password: str,
    deviceid: str,
    openudid: str,
    vendorid: str,
) -> Tuple[int, Any]:
    """账号密码登录（返回 laohu 响应）。"""
    p = _base_params(deviceid, openudid, vendorid, t_ms=True)
    p.update({
        "deviceModel": "iPhone",
        "deviceName": "iPhone",
        "deviceSys": "26.1",
        "deviceType": "iPhone17,2",
        "idfa": "00000000-0000-0000-0000-000000000000",
        "username": _aes_encrypt(username),
        "password": _aes_encrypt(password),
    })
    return _laohu_post("/openApi/secureLogin", p)


def login_by_sms(
    phone: str,
    sms: str,
    deviceid: str,
    openudid: str,
    vendorid: str,
) -> Tuple[int, Any]:
    """短信验证码登录（返回 laohu 响应）。"""
    p = _base_params(deviceid, openudid, vendorid, t_ms=True)
    p.update({
        "areaCodeId": "1",
        "captcha": _aes_encrypt(sms),
        "cellphone": _aes_encrypt(phone),
        "deviceModel": "iPhone",
        "deviceName": "iPhone",
        "deviceSys": "26.1",
        "deviceType": "iPhone17,2",
        "idfa": "00000000-0000-0000-0000-000000000000",
        "type": "18",
    })
    return _laohu_post("/openApi/sms/new/login", p)


def exchange_bbs_token(laohu_token: str, user_id: str, deviceid: str) -> Tuple[int, Any]:
    """用 laohu token 换取 BBS accessToken。"""
    return _bbs_login_post(laohu_token, user_id, deviceid)


# ---------------------------------------------------------------------------
# 完整登录流程
# ---------------------------------------------------------------------------

def complete_password_login(
    phone: str,
    password: str,
    deviceid: str,
    openudid: str,
    vendorid: str,
) -> Dict[str, str]:
    """完整的账号密码登录流程，返回 BBS token 与设备指纹。"""
    code, j = login_by_password(phone, password, deviceid, openudid, vendorid)
    if not isinstance(j, dict) or j.get("code") != 0:
        raise TajiduoLoginError(f"laohu 登录失败: {j}")
    r = j.get("result", {})
    laohu_token = r.get("token")
    user_id = r.get("userId")
    if not laohu_token or not user_id:
        raise TajiduoLoginError("laohu 登录返回缺少 token 或 userId")

    code2, j2 = exchange_bbs_token(laohu_token, user_id, deviceid)
    if not isinstance(j2, dict) or not j2.get("ok"):
        raise TajiduoLoginError(f"BBS 换 token 失败: {j2}")
    d = j2.get("data", {})
    access_token = d.get("accessToken")
    if not access_token:
        raise TajiduoLoginError("BBS 登录返回缺少 accessToken")

    return {
        "token": access_token,
        "deviceid": deviceid,
        "openudid": openudid,
        "vendorid": vendorid,
        "uid": str(d.get("uid", "0")),
        "phone": phone,
        "password": password,
    }


def complete_sms_login(
    phone: str,
    sms: str,
    deviceid: str,
    openudid: str,
    vendorid: str,
) -> Dict[str, str]:
    """完整的短信验证码登录流程，返回 BBS token 与设备指纹。"""
    code, j = login_by_sms(phone, sms, deviceid, openudid, vendorid)
    if not isinstance(j, dict) or j.get("code") != 0:
        raise TajiduoLoginError(f"laohu 短信登录失败: {j}")
    r = j.get("result", {})
    laohu_token = r.get("token")
    user_id = r.get("userId")
    if not laohu_token or not user_id:
        raise TajiduoLoginError("laohu 短信登录返回缺少 token 或 userId")

    code2, j2 = exchange_bbs_token(laohu_token, user_id, deviceid)
    if not isinstance(j2, dict) or not j2.get("ok"):
        raise TajiduoLoginError(f"BBS 换 token 失败: {j2}")
    d = j2.get("data", {})
    access_token = d.get("accessToken")
    if not access_token:
        raise TajiduoLoginError("BBS 登录返回缺少 accessToken")

    return {
        "token": access_token,
        "deviceid": deviceid,
        "openudid": openudid,
        "vendorid": vendorid,
        "uid": str(d.get("uid", "0")),
        "phone": phone,
        "password": "",
    }


# ---------------------------------------------------------------------------
# 自动重登（token 失效时调用）
# ---------------------------------------------------------------------------

def relogin_with_password(phone: str, password: str, deviceid: str) -> str:
    """用账密重新登录，返回新的 BBS token。"""
    fp = gen_fingerprint()
    result = complete_password_login(phone, password, deviceid, fp["openudid"], fp["vendorid"])
    return result["token"]
