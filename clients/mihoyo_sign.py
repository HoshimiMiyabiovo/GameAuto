"""米游社游戏签到客户端。

仅覆盖原神、崩坏：星穹铁道、绝区零的国服游戏签到，
不包含米游社 BBS 任务。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import string
import time
import uuid
import base64
import gzip
import zlib
from dataclasses import dataclass
from hashlib import md5
from http.cookiejar import CookieJar
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import browser_cookie3  # type: ignore
except Exception:  # pragma: no cover - runtime fallback
    browser_cookie3 = None

LOGGER = logging.getLogger("GameAutomation")


MIHOYO_SIGN_CONFIG_KEY = "mihoyo_sign"

MIYOUSHE_SALT_WEB = "DlOUwIupfU6YespEUWDJmXtutuXV6owG"
MIYOUSHE_VERSION = "2.99.1"
MIYOUSHE_CLIENT_TYPE_WEB = "5"
MIYOUSHE_DEFAULT_REFERER = "https://act.mihoyo.com/"
MIYOUSHE_WEB_API = "https://api-takumi.mihoyo.com"
MIYOUSHE_ACCOUNT_INFO_URL = f"{MIYOUSHE_WEB_API}/binding/api/getUserGameRolesByCookie"
MIYOUSHE_LOGIN_TICKET_INFO_URL = "https://webapi.account.mihoyo.com/Api/cookie_accountinfo_by_loginticket"
MIYOUSHE_LOGIN_TICKET_STOKEN_URL = f"{MIYOUSHE_WEB_API}/auth/api/getMultiTokenByLoginTicket"
MIYOUSHE_STOKEN_COOKIE_URL = f"{MIYOUSHE_WEB_API}/auth/api/getCookieAccountInfoBySToken"


MIYOUSHE_CN_SIGNIN_DS_SALT = "LyD1rXqMv2GJhnwdvCBjFOKGiKuLY3aO"
MIYOUSHE_MOBILE_OTP_URL = "https://passport-api.miyoushe.com/account/ma-cn-verifier/verifier/createLoginCaptcha"

MIYOUSHE_LOGIN_RSA_KEY = b"""
-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDDvekdPMHN3AYhm/vktJT+YJr7
cI5DcsNKqdsx5DZX0gDuWFuIjzdwButrIYPNmRJ1G8ybDIF7oDW2eEpm5sMbL9zs
9ExXCdvqrn51qELbqj0XxtMTIpaCHFSI50PfPpTFV9Xt/hmyVwokoOXFlAEgCn+Q
CgGs52bFoYMtyi+xEQIDAQAB
-----END PUBLIC KEY-----
"""

MIYOUSHE_PASSWORD_RSA_KEY = b"""
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA4PMS2JVMwBsOIrYWRluY
wEiFZL7Aphtm9z5Eu/anzJ09nB00uhW+ScrDWFECPwpQto/GlOJYCUwVM/raQpAj
/xvcjK5tNVzzK94mhk+j9RiQ+aWHaTXmOgurhxSp3YbwlRDvOgcq5yPiTz0+kSeK
ZJcGeJ95bvJ+hJ/UMP0Zx2qB5PElZmiKvfiNqVUk8A8oxLJdBB5eCpqWV6CUqDKQ
KSQP4sM0mZvQ1Sr4UcACVcYgYnCbTZMWhJTWkrNXqI8TMomekgny3y+d6NX/cFa6
6jozFIF4HCX5aW8bp8C8vq2tFvFbleQ/Q3CU56EWWKMrOcpmFtRmC18s9biZBVR/
8QIDAQAB
-----END PUBLIC KEY-----
"""

MIYOUSHE_PASSWORD_LOGIN_URL = "https://passport-api.miyoushe.com/account/ma-cn-passport/web/loginByPassword"
MIYOUSHE_MOBILE_LOGIN_URL = "https://passport-api.miyoushe.com/account/ma-cn-passport/web/loginByMobileCaptcha"
MIYOUSHE_RECORD_APP_ID = "bll8iq97cem8"

CN_GAME_CHECKIN_REWARDS_URL = f"{MIYOUSHE_WEB_API}/event/luna/home?lang=zh-cn"
CN_GAME_SIGN_INFO_URL = f"{MIYOUSHE_WEB_API}/event/luna/info?lang=zh-cn"
CN_GAME_SIGN_URL = f"{MIYOUSHE_WEB_API}/event/luna/sign"

ZZZ_WEB_API = "https://act-nap-api.mihoyo.com"
ZZZ_GAME_CHECKIN_REWARDS_URL = f"{ZZZ_WEB_API}/event/luna/zzz/home?lang=zh-cn"
ZZZ_GAME_SIGN_INFO_URL = f"{ZZZ_WEB_API}/event/luna/zzz/info?lang=zh-cn"
ZZZ_GAME_SIGN_URL = f"{ZZZ_WEB_API}/event/luna/zzz/sign"

GAME_BIZ_MAP = {
    "genshin": "hk4e_cn",
    "honkai_sr": "hkrpg_cn",
    "zzz": "nap_cn",
}

LOGIN_URL_MAP = {
    "genshin": "https://www.miyoushe.com/ys/",
    "honkai_sr": "https://www.miyoushe.com/sr/",
    "zzz": "https://www.miyoushe.com/zzz/",
}

COOKIE_TOKEN_KEYS = (
    "login_ticket",
    "login_ticket_v2",
    "game_token",
    "ltoken_v2",
    "ltuid_v2",
    "account_id_v2",
    "account_mid_v2",
    "cookie_token_v2",
    "login_uid",
    "stoken",
    "stoken_v2",
    "stuid",
    "stuid_v2",
    "mid",
    "ltoken",
    "ltuid",
    "account_id",
    "cookie_token",
)

COOKIE_SEARCH_DOMAINS = (
    "miyoushe.com",
    ".miyoushe.com",
    "www.miyoushe.com",
    "bbs-api.miyoushe.com",
    "mihoyo.com",
    ".mihoyo.com",
    "user.mihoyo.com",
    "account.mihoyo.com",
    "api-takumi.mihoyo.com",
    "api-takumi-record.mihoyo.com",
    "passport-api.mihoyo.com",
    "webapi.account.mihoyo.com",
    "webstatic.mihoyo.com",
)

SENSITIVE_COOKIE_KEYS = COOKIE_TOKEN_KEYS + (
    "_MHYUUID",
    "DEVICEFP_SEED_ID",
    "DEVICEFP_SEED_TIME",
    "DEVICEFP",
    "MIHOYO_LOGIN_PLATFORM_LIFECYCLE_ID",
    "account_mid_v2",
    "ltmid_v2",
)


@dataclass(frozen=True)
class MihoyoGameDefinition:
    key: str
    label: str
    game_biz: str
    act_id: str
    rewards_url: str
    info_url: str
    sign_url: str
    signgame: str = ""
    origin: str = "https://act.mihoyo.com"
    login_url: str = "https://www.miyoushe.com/"


GAME_DEFINITIONS: Dict[str, MihoyoGameDefinition] = {
    "genshin": MihoyoGameDefinition(
        key="genshin",
        label="原神",
        game_biz="hk4e_cn",
        act_id="e202311201442471",
        rewards_url=CN_GAME_CHECKIN_REWARDS_URL,
        info_url=CN_GAME_SIGN_INFO_URL,
        sign_url=CN_GAME_SIGN_URL,
        signgame="hk4e",
        login_url=LOGIN_URL_MAP["genshin"],
    ),
    "honkai_sr": MihoyoGameDefinition(
        key="honkai_sr",
        label="崩坏：星穹铁道",
        game_biz="hkrpg_cn",
        act_id="e202304121516551",
        rewards_url=CN_GAME_CHECKIN_REWARDS_URL,
        info_url=CN_GAME_SIGN_INFO_URL,
        sign_url=CN_GAME_SIGN_URL,
        login_url=LOGIN_URL_MAP["honkai_sr"],
    ),
    "zzz": MihoyoGameDefinition(
        key="zzz",
        label="绝区零",
        game_biz="nap_cn",
        act_id="e202406242138391",
        rewards_url=ZZZ_GAME_CHECKIN_REWARDS_URL,
        info_url=ZZZ_GAME_SIGN_INFO_URL,
        sign_url=ZZZ_GAME_SIGN_URL,
        signgame="zzz",
        login_url=LOGIN_URL_MAP["zzz"],
    ),
}


class MihoyoSignError(RuntimeError):
    """米游社签到错误。"""


def get_supported_games() -> List[str]:
    return list(GAME_DEFINITIONS.keys())


def get_login_url(game_key: str = "genshin") -> str:
    definition = GAME_DEFINITIONS.get(game_key, GAME_DEFINITIONS["genshin"])
    return definition.login_url


def tidy_cookie(cookie: str) -> str:
    cookie = str(cookie or "").strip()
    if not cookie:
        return ""
    cookie_map = _parse_cookie_pairs(cookie)
    return "; ".join(f"{key}={value}" for key, value in cookie_map.items())


def _parse_cookie_pairs(cookie: str) -> Dict[str, str]:
    parts = str(cookie or "").split(";")
    cookie_map: Dict[str, str] = {}
    for raw_part in parts:
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookie_map[key.strip()] = value.strip()
    return cookie_map


def _merge_cookie_fields(cookie: str, **updates: Optional[str]) -> str:
    cookie_map = _parse_cookie_pairs(tidy_cookie(cookie))
    for key, value in updates.items():
        if value is None:
            continue
        raw = str(value).strip()
        if raw:
            cookie_map[str(key).strip()] = raw
    return "; ".join(f"{key}={value}" for key, value in cookie_map.items())


def extract_cookie_fields(cookie: str) -> Dict[str, str]:
    cleaned = tidy_cookie(cookie)
    pair_map = _parse_cookie_pairs(cleaned)
    result: Dict[str, str] = {}
    for key in COOKIE_TOKEN_KEYS:
        value = pair_map.get(key)
        if value:
            result[key] = value
    return result


def _merge_cookie_mapping(cookie: str, mapping: Dict[str, Any]) -> str:
    cookie_map = _parse_cookie_pairs(tidy_cookie(cookie))
    for key, value in mapping.items():
        raw_key = str(key or "").strip()
        raw_value = str(value or "").strip()
        if raw_key and raw_value:
            cookie_map[raw_key] = raw_value
    return "; ".join(f"{key}={value}" for key, value in cookie_map.items())


def _mask_value(value: Any, *, keep_prefix: int = 3, keep_suffix: int = 3) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= keep_prefix + keep_suffix:
        return "*" * len(raw)
    return f"{raw[:keep_prefix]}***{raw[-keep_suffix:]}"


def _extract_qr_login_tokens(payload: Any) -> Dict[str, str]:
    """Recursively scan QR login payloads for usable identifiers/tokens."""
    found: Dict[str, str] = {}
    visited: set[int] = set()

    token_key_alias = {
        "token": "game_token",
        "game_token": "game_token",
        "login_ticket": "login_ticket",
        "login_ticket_v2": "login_ticket_v2",
        "stoken": "stoken",
        "stoken_v2": "stoken_v2",
        "cookie_token": "cookie_token",
        "cookie_token_v2": "cookie_token_v2",
        "uid": "account_id",
        "aid": "account_id",
        "account_id": "account_id",
        "account_id_v2": "account_id",
        "stuid": "account_id",
        "stuid_v2": "account_id",
        "mid": "mid",
        "account_mid_v2": "mid",
        "ltmid_v2": "mid",
    }

    def _remember(key: str, value: Any) -> None:
        mapped_key = token_key_alias.get(str(key).strip().lower())
        if not mapped_key:
            return
        raw = str(value or "").strip()
        if raw and mapped_key not in found:
            found[mapped_key] = raw

    def _walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, dict):
            marker = id(node)
            if marker in visited:
                return
            visited.add(marker)
            for key, value in node.items():
                _remember(str(key), value)
                if isinstance(value, str):
                    stripped = value.strip()
                    if not stripped:
                        continue
                    if stripped[:1] in ("{", "["):
                        try:
                            parsed = json.loads(stripped)
                        except json.JSONDecodeError:
                            continue
                        _walk(parsed)
                elif isinstance(value, (dict, list, tuple)):
                    _walk(value)
            return
        if isinstance(node, (list, tuple)):
            marker = id(node)
            if marker in visited:
                return
            visited.add(marker)
            for item in node:
                _walk(item)

    _walk(payload)
    return found


def _resolve_cookie_account_id(fields: Dict[str, str]) -> str:
    return (
        fields.get("account_id_v2")
        or fields.get("account_id")
        or fields.get("ltuid_v2")
        or fields.get("ltuid")
        or fields.get("login_uid")
        or fields.get("stuid_v2")
        or fields.get("stuid")
        or ""
    )


def _resolve_cookie_account_id_from_mapping(mapping: Dict[str, Any]) -> str:
    normalized = {str(key): str(value) for key, value in mapping.items() if value is not None}
    return _resolve_cookie_account_id(normalized)


def _build_persisted_login_cookie(
    cookies: Dict[str, Any],
    *,
    account_id: str = "",
    cookie_token: str = "",
    stoken: str = "",
    mid: str = "",
) -> str:
    full_cookie = _merge_cookie_mapping("", cookies)
    return _merge_cookie_fields(
        full_cookie,
        account_id=account_id or None,
        cookie_token=cookie_token or None,
        stoken=stoken or None,
        stuid=account_id if stoken and account_id else None,
        mid=mid or None,
    )


def _build_persisted_stoken_cookie(
    cookies: Dict[str, Any],
    *,
    account_id: str = "",
    stoken: str = "",
    mid: str = "",
) -> str:
    stoken_cookie = _merge_cookie_mapping("", cookies)
    return _merge_cookie_fields(
        stoken_cookie,
        stuid=account_id or None,
        stoken=stoken or None,
        mid=mid or None,
    )


def _apply_login_response_data(cookies: Dict[str, str], response_data: Any) -> None:
    if not isinstance(response_data, dict):
        return

    token_info = response_data.get("token") or {}
    if isinstance(token_info, dict):
        token_value = str(token_info.get("token") or "").strip()
        if token_value:
            cookies["stoken"] = token_value

    user_info = response_data.get("user_info") or {}
    if isinstance(user_info, dict):
        aid = str(user_info.get("aid") or "").strip()
        mid = str(user_info.get("mid") or "").strip()
        if aid:
            cookies.setdefault("stuid", aid)
            cookies.setdefault("account_id", aid)
        if mid:
            cookies.setdefault("mid", mid)

    for key in (
        "stoken",
        "stoken_v2",
        "stuid",
        "stuid_v2",
        "mid",
        "account_id",
        "account_id_v2",
        "cookie_token",
        "cookie_token_v2",
        "login_ticket",
        "login_ticket_v2",
        "game_token",
        "ltoken",
        "ltoken_v2",
        "ltuid",
        "ltuid_v2",
        "ltmid_v2",
        "account_mid_v2",
    ):
        value = str(response_data.get(key) or "").strip()
        if value:
            cookies.setdefault(key, value)


def _build_mihoyo_mobile_headers(*, cookie: str = "", extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "x-rpc-app_version": "2.11.2",
        "x-rpc-client_type": "5",
        "Referer": "https://webstatic.mihoyo.com/",
        "Origin": "https://webstatic.mihoyo.com",
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) miHoYoBBS/2.11.1"
        ),
    }
    cleaned_cookie = tidy_cookie(cookie)
    if cleaned_cookie:
        headers["Cookie"] = cleaned_cookie
    if extra_headers:
        headers.update(extra_headers)
    return headers


def _generate_cn_login_ds() -> str:
    timestamp = str(int(time.time()))
    rand = str(random.randint(100001, 200000))
    digest = md5(
        f"salt={MIYOUSHE_CN_SIGNIN_DS_SALT}&t={timestamp}&r={rand}".encode("utf-8")
    ).hexdigest()
    return f"{timestamp},{rand},{digest}"


def _encrypt_credentials(text: str, key_type: int = 2) -> str:
    import rsa
    key = MIYOUSHE_PASSWORD_RSA_KEY if key_type == 1 else MIYOUSHE_LOGIN_RSA_KEY
    public_key = rsa.PublicKey.load_pkcs1_openssl_pem(key)
    crypto = rsa.encrypt(text.encode("utf-8"), public_key)
    return base64.b64encode(crypto).decode("utf-8")


def _build_cn_mobile_login_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "DS": _generate_cn_login_ds(),
        "x-rpc-app_id": MIYOUSHE_RECORD_APP_ID,
        "x-rpc-client_type": "4",
        "x-rpc-source": "v2.webLogin",
        "x-rpc-device_fp": "38d7fff8fd68c",
        "x-rpc-device_id": _random_device_id(),
        "x-rpc-device_model": "iPhone14,3",
        "x-rpc-device_name": "iPhone",
        "x-rpc-game_biz": "bbs_cn",
        "x-rpc-sdk_version": "2.31.0",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    }


def _random_device_id() -> str:
    import uuid
    return str(uuid.uuid4()).upper()


def _build_cn_login_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "DS": _generate_cn_login_ds(),
        "x-rpc-app_id": MIYOUSHE_RECORD_APP_ID,
        "x-rpc-client_type": "4",
        "x-rpc-source": "v2.webLogin",
        "x-rpc-device_fp": "38d7fff8fd68c",
        "x-rpc-device_id": "469af8a4-0754-4a3c-a999-dec592f00894",
        "x-rpc-device_model": "Firefox%20131.0",
        "x-rpc-device_name": "Firefox",
        "x-rpc-game_biz": "bbs_cn",
        "x-rpc-sdk_version": "2.31.0",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/131.0",
    }


def _extract_set_cookies(response) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for header in response.headers.get_all("Set-Cookie") or []:
        for part in header.split(";"):
            part = part.strip()
            if "=" in part and not part.lower().startswith(("domain=", "path=", "expires=", "max-age=", "secure", "httponly", "samesite")):
                key, _, value = part.partition("=")
                cookies[key.strip()] = value.strip()
    return cookies


def _request_json_with_cookies(
    url: str,
    *,
    headers: Dict[str, str],
    timeout: int = 20,
    body: Optional[bytes] = None,
    method: str = "POST",
) -> tuple:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload_text = _decode_response_text(response.read(), response.headers.get("Content-Encoding", ""))
            cookies = _extract_set_cookies(response)
            aigis = response.headers.get("x-rpc-aigis") or ""
    except HTTPError as exc:
        try:
            error_text = _decode_response_text(exc.read(), exc.headers.get("Content-Encoding", "") if exc.headers else "")
        except Exception:
            error_text = ""
        raise MihoyoSignError(_extract_api_message(error_text) or f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise MihoyoSignError(f"网络连接失败：{exc.reason}") from exc
    except Exception as exc:
        raise MihoyoSignError(_sanitize_message_text(str(exc))) from exc

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise MihoyoSignError(f"接口返回不是有效 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise MihoyoSignError("接口返回不是对象结构。")
    return payload, cookies, aigis


def _request_json_simple(
    url: str,
    *,
    headers: Dict[str, str],
    timeout: int = 20,
    query: Optional[Dict[str, Any]] = None,
    body: Optional[bytes] = None,
    method: str = "GET",
) -> Dict[str, Any]:
    request_url = url
    if query:
        separator = "&" if "?" in request_url else "?"
        request_url = f"{request_url}{separator}{urlencode(query)}"
    request = Request(request_url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload_text = _decode_response_text(response.read(), response.headers.get("Content-Encoding", ""))
    except HTTPError as exc:
        try:
            error_text = _decode_response_text(exc.read(), exc.headers.get("Content-Encoding", "") if exc.headers else "")
        except Exception:
            error_text = ""
        raise MihoyoSignError(_extract_api_message(error_text) or f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise MihoyoSignError(f"网络连接失败：{exc.reason}") from exc
    except Exception as exc:
        raise MihoyoSignError(_sanitize_message_text(str(exc))) from exc

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise MihoyoSignError(f"接口返回不是有效 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise MihoyoSignError("接口返回不是对象结构。")
    return payload


def _resolve_login_ticket_account_id(login_ticket: str) -> str:
    response = _request_json_simple(
        MIYOUSHE_LOGIN_TICKET_INFO_URL,
        headers=_build_mihoyo_mobile_headers(),
        timeout=20,
        query={"login_ticket": login_ticket},
        method="GET",
    )
    info = response.get("data") or response.get("cookie_info") or {}
    if not isinstance(info, dict):
        info = {}
    for key in ("account_id", "uid", "cookie_token_uid"):
        value = str(info.get(key) or "").strip()
        if value:
            return value
    return ""


def _get_stoken_by_login_ticket(login_ticket: str, account_id: str) -> str:
    response = _request_json_simple(
        MIYOUSHE_LOGIN_TICKET_STOKEN_URL,
        headers=_build_mihoyo_mobile_headers(),
        timeout=20,
        query={"login_ticket": login_ticket, "token_types": "3", "uid": account_id},
        method="GET",
    )
    token_list = response.get("data", {}).get("list") or []
    for item in token_list:
        if not isinstance(item, dict):
            continue
        token = str(item.get("token") or "").strip()
        if token:
            return token
    message = str(response.get("message") or "无法通过 login_ticket 获取 stoken")
    raise MihoyoSignError(message)


def _get_cookie_token_by_stoken(stoken: str, account_id: str, mid: str = "") -> str:
    # v2 stoken (前缀 "v2_") 必须配 mid 一起发，否则接口返回"登录状态失效"
    cookie_parts = [f"stuid={account_id}", f"stoken={stoken}"]
    if mid:
        cookie_parts.append(f"mid={mid}")
    response = _request_json_simple(
        MIYOUSHE_STOKEN_COOKIE_URL,
        headers=_build_mihoyo_mobile_headers(cookie=";".join(cookie_parts)),
        timeout=20,
        query={"uid": account_id, "stoken": stoken},
        method="GET",
    )
    data = response.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    token = str(data.get("cookie_token") or data.get("cookie_token_v2") or "").strip()
    if token:
        return token
    message = str(response.get("message") or "无法通过 stoken 获取 cookie_token")
    raise MihoyoSignError(message)


def _ensure_fresh_cookie_token(cookie: str) -> str:
    """用 stoken 强制刷新 cookie_token。

    签到接口 `luna/info` / `luna/sign` 需要有效的 cookie_token；stoken_cookie 中
    的 cookie_token 可能已过期。只要 cookie 含有 stoken + account_id，就尝试刷新；
    刷新失败时保留原 cookie 不变（不抛错，让签到流程自己暴露真实错误）。

    v2 stoken (前缀 "v2_") 必须配 mid 才能通过认证。
    """
    cleaned = tidy_cookie(cookie)
    if not cleaned:
        return cleaned
    fields = extract_cookie_fields(cleaned)
    stoken = fields.get("stoken") or fields.get("stoken_v2") or ""
    account_id = _resolve_cookie_account_id(fields)
    mid = fields.get("mid") or fields.get("account_mid_v2") or ""
    if not stoken or not account_id:
        return cleaned
    try:
        fresh_token = _get_cookie_token_by_stoken(stoken, account_id, mid=mid)
    except Exception as exc:
        LOGGER.debug("[米游社] 强制刷新 cookie_token 失败（沿用原 cookie）: %s", exc)
        return cleaned
    if not fresh_token:
        return cleaned
    LOGGER.info("[米游社] 已通过 stoken 刷新 cookie_token")
    return _merge_cookie_fields(cleaned, cookie_token=fresh_token)


def enrich_cookie_for_record_api(cookie: str) -> Dict[str, Any]:
    cleaned = tidy_cookie(cookie)
    fields = extract_cookie_fields(cleaned)
    account_id = _resolve_cookie_account_id(fields)
    added_fields: Dict[str, str] = {}

    login_ticket = fields.get("login_ticket") or fields.get("login_ticket_v2") or ""
    if login_ticket and not account_id:
        account_id = _resolve_login_ticket_account_id(login_ticket)
        if account_id:
            added_fields["account_id"] = account_id

    stoken = fields.get("stoken") or fields.get("stoken_v2") or ""
    game_token = fields.get("game_token") or ""
    if game_token and account_id and not stoken:
        try:
            stoken_info = _get_stoken_by_game_token_via_genshin(game_token, int(account_id))
        except Exception as exc:
            LOGGER.warning("[米游社] game_token -> stoken 自动补齐失败: %s", exc)
        else:
            stoken = str(stoken_info.get("stoken") or "").strip()
            if stoken:
                added_fields["stoken"] = stoken
                added_fields["stuid"] = str(stoken_info.get("account_id") or account_id)
                if stoken_info.get("mid"):
                    added_fields["mid"] = str(stoken_info["mid"])

    if login_ticket and account_id and not stoken:
        stoken = _get_stoken_by_login_ticket(login_ticket, account_id)
        if stoken:
            added_fields["stoken"] = stoken
            added_fields["stuid"] = account_id

    cookie_token = fields.get("cookie_token") or fields.get("cookie_token_v2") or ""
    if stoken and account_id and not cookie_token:
        try:
            mid = fields.get("mid") or fields.get("account_mid_v2") or ""
            cookie_token = _get_cookie_token_by_stoken(stoken, account_id, mid=mid)
            if cookie_token:
                added_fields["cookie_token"] = cookie_token
        except Exception as exc:
            LOGGER.debug("[米游社] cookie_token 获取失败（可忽略，有 stoken 即可）: %s", exc)

    enriched_cookie = _merge_cookie_fields(cleaned, **added_fields)
    enriched_fields = extract_cookie_fields(enriched_cookie)
    return {
        "cookie": enriched_cookie,
        "fields": enriched_fields,
        "account_id": _resolve_cookie_account_id(enriched_fields),
        "added_fields": added_fields,
        "record_ready": bool(
            (
                enriched_fields.get("stoken") or enriched_fields.get("stoken_v2")
                or enriched_fields.get("ltoken") or enriched_fields.get("ltoken_v2")
            )
            and _resolve_cookie_account_id(enriched_fields)
        ),
    }


def _maybe_merge_browser_cookie(
    cookie: str,
    *,
    browser_name: str = "edge",
    expected_account_id: str = "",
    context: str = "",
) -> Dict[str, Any]:
    cleaned = tidy_cookie(cookie)
    if not cleaned:
        return {
            "cookie": "",
            "fields": {},
            "account_id": "",
            "record_ready": False,
            "browser_cookie": "",
            "browser_used": False,
        }

    browser_name = str(browser_name or "").strip().lower()
    if not browser_name:
        enriched = enrich_cookie_for_record_api(cleaned)
        enriched["browser_cookie"] = ""
        enriched["browser_used"] = False
        return enriched

    try:
        browser_result = extract_cookie_from_browser(browser_name)
    except Exception as exc:
        label = context or "登录后"
        LOGGER.warning("[米游社] %s 浏览器 Cookie 自动补齐失败: %s", label, exc)
        enriched = enrich_cookie_for_record_api(cleaned)
        enriched["browser_cookie"] = ""
        enriched["browser_used"] = False
        return enriched

    browser_cookie = tidy_cookie(str(browser_result.get("cookie") or ""))
    browser_fields = dict(browser_result.get("fields") or extract_cookie_fields(browser_cookie))
    browser_account_id = _resolve_cookie_account_id(browser_fields)
    if expected_account_id and browser_account_id and expected_account_id != browser_account_id:
        LOGGER.warning(
            "[米游社] %s 浏览器 Cookie 账号不一致，跳过自动合并：登录账号 %s | 浏览器账号 %s",
            context or "登录后",
            _mask_value(expected_account_id),
            _mask_value(browser_account_id),
        )
        enriched = enrich_cookie_for_record_api(cleaned)
        enriched["browser_cookie"] = browser_cookie
        enriched["browser_used"] = False
        return enriched

    merged_cookie = _merge_cookie_fields(browser_cookie, **extract_cookie_fields(cleaned))
    enriched = enrich_cookie_for_record_api(merged_cookie)
    enriched["browser_cookie"] = browser_cookie
    enriched["browser_used"] = True
    if enriched.get("record_ready"):
        LOGGER.info(
            "[米游社] %s 已从 %s 浏览器 Cookie 自动补齐便笺字段",
            context or "登录后",
            browser_name,
        )
    return enriched


def _get_stoken_by_game_token_via_genshin(game_token: str, account_id: int) -> Dict[str, str]:
    import asyncio
    from genshin.client.manager.cookie import fetch_stoken_with_game_token

    async def _fetch():
        return await fetch_stoken_with_game_token(game_token=game_token, account_id=account_id)

    result = asyncio.run(_fetch())
    return {"stoken": result.token, "account_id": result.aid, "mid": result.mid}


def send_mobile_captcha(mobile: str) -> Dict[str, Any]:
    payload = {
        "mobile": _encrypt_credentials(mobile),
        "area_code": _encrypt_credentials("+86"),
    }
    response, _, aigis = _request_json_with_cookies(
        MIYOUSHE_MOBILE_OTP_URL,
        headers=_build_cn_mobile_login_headers(),
        timeout=20,
        body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        method="POST",
    )
    retcode = response.get("retcode")
    if retcode == -3101:
        raise MihoyoSignError("需要人机验证，请稍后重试或使用 App 扫码登录。")
    if retcode and retcode != 0:
        raise MihoyoSignError(str(response.get("message") or f"发送验证码失败，返回码：{retcode}"))
    data = response.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    LOGGER.info("[米游社] 验证码已发送至 %s，有效期 300 秒", mobile[:3] + "****" + mobile[7:])
    return {"mobile": mobile, "aigis": aigis, "raw": response}


def login_by_mobile_captcha(mobile: str, captcha: str) -> Dict[str, Any]:
    payload = {
        "mobile": _encrypt_credentials(mobile),
        "area_code": _encrypt_credentials("+86"),
        "captcha": captcha,
        "token_types": 6,
    }
    response, cookies, _ = _request_json_with_cookies(
        MIYOUSHE_MOBILE_LOGIN_URL,
        headers=_build_cn_mobile_login_headers(),
        timeout=20,
        body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        method="POST",
    )
    retcode = response.get("retcode")
    if retcode and retcode != 0:
        raise MihoyoSignError(str(response.get("message") or f"手机验证码登录失败，返回码：{retcode}"))
    resp_data = response.get("data") or {}
    LOGGER.info("[米游社] SMS 登录 response data 字段: %s", sorted(resp_data.keys()) if isinstance(resp_data, dict) else type(resp_data).__name__)
    _apply_login_response_data(cookies, resp_data)
    stoken_extracted = False
    if isinstance(resp_data, dict):
        token_info = resp_data.get("token") or {}
        if isinstance(token_info, dict) and token_info.get("token"):
            cookies["stoken"] = str(token_info["token"])
            stoken_extracted = True
            LOGGER.info("[米游社] 从 SMS 登录 token.token 提取到 stoken")
        user_info = resp_data.get("user_info") or {}
        if isinstance(user_info, dict):
            LOGGER.info("[米游社] SMS user_info 字段: %s", sorted(user_info.keys()))
            if user_info.get("aid"):
                cookies["stuid"] = str(user_info["aid"])
                cookies["account_id"] = str(user_info["aid"])
            if user_info.get("mid"):
                cookies["mid"] = str(user_info["mid"])
        for inner_key in ("reactivate_info", "realname_info"):
            inner_data = resp_data.get(inner_key) or {}
            if isinstance(inner_data, dict) and inner_data:
                LOGGER.info("[米游社] SMS %s 字段: %s", inner_key, sorted(inner_data.keys()))
        for key in ("stoken", "stoken_v2", "stuid", "stuid_v2"):
            value = resp_data.get(key)
            if value and key not in cookies:
                cookies[key] = str(value)
                stoken_extracted = True
                LOGGER.info("[米游社] 从 SMS 登录 response data 直接提取到 %s", key)
        if not stoken_extracted:
            for key in ("login_ticket", "login_ticket_v2", "game_token"):
                value = resp_data.get(key)
                if value:
                    cookies[key] = str(value)
                    LOGGER.info("[米游社] 从 SMS 登录 response data 提取到 %s", key)
    LOGGER.info("[米游社] 手机验证码登录成功，收到 %d 个 Cookies", len(cookies))
    return {"cookies": cookies, "raw": response}


def complete_sms_login(mobile: str, captcha: str, *, browser_name: str = "") -> Dict[str, Any]:
    result = login_by_mobile_captcha(mobile, captcha)
    cookies = result.get("cookies") or {}
    if not isinstance(cookies, dict):
        cookies = {}

    stoken = str(cookies.get("stoken") or cookies.get("stoken_v2") or "").strip()
    cookie_token = str(cookies.get("cookie_token") or cookies.get("cookie_token_v2") or "").strip()
    account_id = _resolve_cookie_account_id_from_mapping(cookies)
    mid = str(cookies.get("mid") or "").strip()

    LOGGER.info("[米游社] SMS Cookies 字段: %s", sorted(cookies.keys()))

    login_ticket = str(cookies.get("login_ticket") or cookies.get("login_ticket_v2") or "").strip()
    if login_ticket and not account_id:
        account_id = _resolve_login_ticket_account_id(login_ticket)
    if login_ticket and not stoken:
        try:
            stoken = _get_stoken_by_login_ticket(login_ticket, account_id)
            LOGGER.info("[米游社] 已通过 login_ticket 换取 stoken")
        except Exception as exc:
            LOGGER.warning("[米游社] login_ticket → stoken 失败: %s", exc)
    if stoken and not cookie_token:
        try:
            cookie_token = _get_cookie_token_by_stoken(stoken, account_id)
            LOGGER.info("[米游社] 已通过 stoken 换取 cookie_token")
        except Exception as exc:
            LOGGER.warning("[米游社] stoken → cookie_token 失败: %s", exc)

    if not account_id:
        raise MihoyoSignError("手机验证码登录未获取到 account_id。")
    if not cookie_token:
        LOGGER.error("[米游社] SMS 登录 Cookies: %s",
                     {k: (v[:6] + "***") if len(v) > 6 else "***" for k, v in list(cookies.items())[:15]})
        raise MihoyoSignError("手机验证码登录后有效 cookie_token 缺失，请重试。")

    full_cookie = _build_persisted_login_cookie(
        cookies,
        account_id=account_id,
        cookie_token=cookie_token,
        stoken=stoken,
        mid=mid,
    )
    enriched = _maybe_merge_browser_cookie(
        full_cookie,
        browser_name=browser_name,
        expected_account_id=account_id,
        context="SMS 登录后",
    )
    final_fields = dict(enriched.get("fields") or {})
    final_account_id = str(enriched.get("account_id") or account_id or "").strip()
    final_stoken = str(final_fields.get("stoken") or final_fields.get("stoken_v2") or stoken or "").strip()
    final_mid = str(final_fields.get("mid") or mid or "").strip()
    stoken_cookie = _build_persisted_stoken_cookie(
        _parse_cookie_pairs(str(enriched.get("cookie") or full_cookie)),
        account_id=final_account_id,
        stoken=final_stoken,
        mid=final_mid,
    )
    LOGGER.info("[米游社] SMS 登录完成：账号 %s | 便笺查询 %s",
                final_account_id or account_id, "已就绪" if enriched["record_ready"] else "未就绪")
    return {
        "cookie": enriched["cookie"],
        "stoken_cookie": tidy_cookie(stoken_cookie),
        "fields": enriched["fields"],
        "account_id": enriched["account_id"],
        "record_ready": enriched["record_ready"],
        "method": "sms",
        "browser_cookie": tidy_cookie(str(enriched.get("browser_cookie") or "")),
        "browser_used": bool(enriched.get("browser_used")),
    }


def login_by_password(account: str, password: str) -> Dict[str, Any]:
    import asyncio
    import genshin
    from genshin.utility import auth as _auth_util
    from genshin.utility import ds as _ds_util

    async def _login():
        client = genshin.Client(region=genshin.Region.CHINESE)
        await client.login_with_password(account, password)
        cookies = dict(client.cookie_manager.cookies)
        if cookies.get("stoken"):
            return cookies

        from genshin.utility.auth import encrypt_credentials
        encrypted_pw = encrypt_credentials(password, 2)
        payload = {"account": account, "password": encrypted_pw, "token_types": 6}
        ds_value = _ds_util.generate_dynamic_secret(salt="LyD1rXqMv2GJhnwdvCBjFOKGiKuLY3aO")
        headers = {
            **_auth_util.CN_LOGIN_HEADERS,
            "DS": ds_value,
        }
        resp = await client.cookie_manager._raw_request(
            "POST",
            "https://passport-api.miyoushe.com/account/ma-cn-passport/web/loginByPassword",
            json=payload,
            headers=headers,
        )
        body_data = resp.data.get("data") or {}
        LOGGER.info("[米游社] 密码登录(token_types=6) data 字段: %s",
                    sorted(body_data.keys()) if isinstance(body_data, dict) else type(body_data).__name__)
        _apply_login_response_data(cookies, body_data)
        if cookies.get("stoken"):
            LOGGER.info("[米游社] 从密码登录(token_types=6) response data 提取到 stoken")
        return cookies

    try:
        cookies = asyncio.run(_login())
    except genshin.InvalidCookies:
        raise MihoyoSignError("密码登录失败：账号或密码错误。")
    except genshin.GenshinException as exc:
        retcode = getattr(exc, "retcode", None)
        msg = getattr(exc, "msg", "")
        raise MihoyoSignError(f"密码登录失败：{msg or retcode or str(exc)}")
    except Exception as exc:
        raise MihoyoSignError(f"genshin 密码登录异常：{exc}")

    LOGGER.info("[米游社] genshin 密码登录成功，cookies: %s", sorted(cookies.keys()))
    return {"cookies": cookies, "raw": {"retcode": 0, "message": "ok"}}


def login_by_qrcode_genshin() -> Dict[str, Any]:
    import asyncio
    import genshin
    from genshin.models.auth.qrcode import QRCodeStatus as _QRCodeStatus
    from genshin.utility import auth as _auth_util

    async def _login():
        client = genshin.Client(region=genshin.Region.CHINESE)
        creation = await client._create_qrcode()
        qr_url = creation.url
        ticket = creation.ticket
        LOGGER.info("[米游社] 二维码 URL: %s", qr_url)

        import qrcode as _qrlib
        from qrcode.constants import ERROR_CORRECT_L
        import tempfile
        qr_img = _qrlib.make(qr_url, error_correction=ERROR_CORRECT_L)
        tmp_path = os.path.join(tempfile.gettempdir(), f"mihoyo_qr_{int(time.time())}.png")
        qr_img.save(tmp_path)
        LOGGER.info("[米游社] 二维码图片已保存: %s", tmp_path)
        os.startfile(tmp_path)
        LOGGER.info("[米游社] 已打开本地二维码图片，请用米游社 App 扫码")

        scanned = False
        extracted_tokens: Dict[str, str] = {}
        while True:
            resp = await client.cookie_manager._raw_request(
                "POST",
                "https://passport-api.miyoushe.com/account/ma-cn-passport/web/queryQRLoginStatus",
                json={"ticket": ticket},
                headers=_auth_util.QRCODE_HEADERS,
            )
            body_data = resp.data.get("data") or {}
            if not body_data:
                from genshin import errors as _gerrors
                _gerrors.raise_for_retcode(resp.data)
            if isinstance(body_data, dict) and body_data:
                LOGGER.info("[米游社] queryQRLoginStatus data 字段: %s", sorted(body_data.keys()))
                tokens_raw = body_data.get("tokens")
                LOGGER.info("[米游社] tokens 字段原始值: type=%s, value=%s", type(tokens_raw).__name__, repr(tokens_raw)[:500])
                token_fields = _extract_qr_login_tokens(body_data)
                if token_fields:
                    for key, value in token_fields.items():
                        extracted_tokens.setdefault(key, value)
                    LOGGER.info(
                        "[米游社] queryQRLoginStatus 提取字段: %s",
                        {key: _mask_value(value) for key, value in sorted(token_fields.items())},
                    )
            status = _QRCodeStatus(body_data["status"])

            resp_cookies = {k: v.value for k, v in resp.cookies.items()}
            if resp_cookies:
                client.set_cookies(resp_cookies)

            if status is _QRCodeStatus.SCANNED and not scanned:
                LOGGER.info("[米游社] 二维码已扫描，请在手机上确认...")
                scanned = True
            elif status is _QRCodeStatus.CONFIRMED:
                LOGGER.info("[米游社] 扫码已确认")
                # 直接从 tokens 字段提取 game_token
                tokens_raw = body_data.get("tokens")
                if isinstance(tokens_raw, dict):
                    gt = tokens_raw.get("token") or tokens_raw.get("game_token") or ""
                    if gt:
                        extracted_tokens.setdefault("game_token", str(gt))
                        LOGGER.info("[米游社] 从 tokens dict 提取到 game_token")
                elif isinstance(tokens_raw, list):
                    for item in tokens_raw:
                        if isinstance(item, dict):
                            gt = item.get("token") or item.get("game_token") or ""
                            if gt:
                                extracted_tokens.setdefault("game_token", str(gt))
                                LOGGER.info("[米游社] 从 tokens list 提取到 game_token")
                                break
                if extracted_tokens.get("game_token"):
                    LOGGER.info("[米游社] game_token 已获取")
                else:
                    LOGGER.warning("[米游社] tokens 字段中未找到 game_token: %s", repr(tokens_raw)[:300])
                break
            await asyncio.sleep(1)

        all_cookies = dict(client.cookie_manager.cookies)
        LOGGER.info("[米游社] 扫码后 cookie_manager 累计字段: %s", sorted(all_cookies.keys()))
        if extracted_tokens:
            if extracted_tokens.get("game_token"):
                all_cookies["game_token"] = extracted_tokens["game_token"]
            if extracted_tokens.get("login_ticket"):
                all_cookies.setdefault("login_ticket", extracted_tokens["login_ticket"])
            if extracted_tokens.get("login_ticket_v2"):
                all_cookies.setdefault("login_ticket_v2", extracted_tokens["login_ticket_v2"])
            if extracted_tokens.get("stoken"):
                all_cookies.setdefault("stoken", extracted_tokens["stoken"])
            if extracted_tokens.get("stoken_v2"):
                all_cookies.setdefault("stoken_v2", extracted_tokens["stoken_v2"])
            if extracted_tokens.get("cookie_token"):
                all_cookies.setdefault("cookie_token", extracted_tokens["cookie_token"])
            if extracted_tokens.get("cookie_token_v2"):
                all_cookies.setdefault("cookie_token_v2", extracted_tokens["cookie_token_v2"])
            qr_account_id = extracted_tokens.get("account_id", "")
            if qr_account_id:
                all_cookies.setdefault("account_id", qr_account_id)
                all_cookies.setdefault("stuid", qr_account_id)
            qr_mid = extracted_tokens.get("mid", "")
            if qr_mid:
                all_cookies.setdefault("mid", qr_mid)
        return all_cookies

    try:
        cookies = asyncio.run(_login())
    except genshin.GenshinException as exc:
        msg = getattr(exc, "msg", "")
        raise MihoyoSignError(f"genshin 扫码登录失败：{msg or str(exc)}")
    except Exception as exc:
        raise MihoyoSignError(f"genshin 扫码登录异常：{exc}")

    LOGGER.info("[米游社] genshin 扫码登录成功，cookies: %s", sorted(cookies.keys()))
    return {"cookies": cookies, "raw": {"retcode": 0, "message": "ok"}}


def complete_password_login(account: str, password: str, *, browser_name: str = "") -> Dict[str, Any]:
    result = login_by_password(account, password)
    cookies = result.get("cookies") or {}
    if not isinstance(cookies, dict):
        cookies = {}

    stoken = str(cookies.get("stoken") or cookies.get("stoken_v2") or "").strip()
    cookie_token = str(cookies.get("cookie_token") or cookies.get("cookie_token_v2") or "").strip()
    account_id = _resolve_cookie_account_id_from_mapping(cookies)
    mid = str(cookies.get("mid") or "").strip()

    LOGGER.info("[米游社] 密码登录 Cookies 字段: %s", sorted(cookies.keys()))

    login_ticket = str(cookies.get("login_ticket") or cookies.get("login_ticket_v2") or "").strip()
    if login_ticket and not account_id:
        account_id = _resolve_login_ticket_account_id(login_ticket)
    if login_ticket and not stoken:
        try:
            stoken = _get_stoken_by_login_ticket(login_ticket, account_id)
            LOGGER.info("[米游社] 已通过 login_ticket 换取 stoken")
        except Exception as exc:
            LOGGER.warning("[米游社] login_ticket → stoken 失败: %s", exc)
    if stoken and not cookie_token:
        try:
            cookie_token = _get_cookie_token_by_stoken(stoken, account_id)
            LOGGER.info("[米游社] 已通过 stoken 换取 cookie_token")
        except Exception as exc:
            LOGGER.warning("[米游社] stoken → cookie_token 失败: %s", exc)

    if not account_id:
        raise MihoyoSignError("密码登录未获取到 account_id。")
    if not cookie_token:
        raise MihoyoSignError("密码登录后有效 cookie_token 缺失，请重试。")

    full_cookie = _build_persisted_login_cookie(
        cookies,
        account_id=account_id,
        cookie_token=cookie_token,
        stoken=stoken,
        mid=mid,
    )
    enriched = _maybe_merge_browser_cookie(
        full_cookie,
        browser_name=browser_name,
        expected_account_id=account_id,
        context="密码登录后",
    )
    final_fields = dict(enriched.get("fields") or {})
    final_account_id = str(enriched.get("account_id") or account_id or "").strip()
    final_stoken = str(final_fields.get("stoken") or final_fields.get("stoken_v2") or stoken or "").strip()
    final_mid = str(final_fields.get("mid") or mid or "").strip()
    stoken_cookie = _build_persisted_stoken_cookie(
        _parse_cookie_pairs(str(enriched.get("cookie") or full_cookie)),
        account_id=final_account_id,
        stoken=final_stoken,
        mid=final_mid,
    )
    LOGGER.info("[米游社] 密码登录完成：账号 %s | 便笺查询 %s",
                final_account_id or account_id, "已就绪" if enriched["record_ready"] else "未就绪")
    return {
        "cookie": enriched["cookie"],
        "stoken_cookie": tidy_cookie(stoken_cookie),
        "fields": enriched["fields"],
        "account_id": enriched["account_id"],
        "record_ready": enriched["record_ready"],
        "method": "password",
        "browser_cookie": tidy_cookie(str(enriched.get("browser_cookie") or "")),
        "browser_used": bool(enriched.get("browser_used")),
    }


def _hk4_sdk_ds(body: str = "", query: str = "") -> str:
    """HK4 SDK 扫码登录专用 DS 签名（参考 FufuLauncher）。"""
    from hashlib import md5 as _md5
    salt = "t0qEgfub6cvueAPgR5m9aQWWVciEer7v"
    t = str(int(time.time()))
    r = str(random.randint(100001, 200000))
    raw = f"salt={salt}&t={t}&r={r}&b={body}&q={query}"
    digest = _md5(raw.encode()).hexdigest()
    return f"{t},{r},{digest}"


def _hk4_sdk_headers(body: str = "") -> Dict[str, str]:
    """HK4 SDK 扫码登录请求头。"""
    return {
        "x-rpc-app_version": "2.71.1",
        "DS": _hk4_sdk_ds(body=body),
        "x-rpc-aigis": "",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-rpc-game_biz": "bbs_cn",
        "x-rpc-sys_version": "12",
        "x-rpc-device_id": str(uuid.uuid4().hex),
        "x-rpc-device_name": "Xiaomi MI 6",
        "x-rpc-device_model": "MI 6",
        "x-rpc-app_id": "bll8iq97cem8",
        "x-rpc-client_type": "4",
        "User-Agent": "okhttp/4.9.3",
    }


def _http_post_json(url: str, body: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """发送 POST 请求并返回 JSON。"""
    from urllib.request import Request, urlopen
    data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers=headers, method="POST")
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def login_by_qrcode_app() -> Dict[str, Any]:
    """使用 FufuLauncher 的 App QR 扫码登录（直接返回 stoken）。

    与 login_by_qrcode_genshin 不同，此方式通过 passport-api.mihoyo.com 的
    APP 端二维码接口，扫码确认后直接返回 stoken，不需要 game_token 中转。
    """
    # FufuLauncher LoginQrWindow.xaml.cs 原版参数
    app_salt = "dDIQHbKOdaPaLuvQKVzUzqdeCaxjtaPV"
    app_client_type = "3"
    app_app_id = "ddxf5dufpuyo"
    app_version = "2.90.1"
    device_id = uuid.uuid4().hex[:16].upper()
    device_fp = "".join(random.choices("0123456789abcdef", k=13))

    def _app_ds(body: str = "", query: str = "") -> str:
        from hashlib import md5 as _md5
        t = str(int(time.time()))
        r = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        raw = f"salt={app_salt}&t={t}&r={r}&b={body}&q={query}"
        digest = _md5(raw.encode()).hexdigest()
        return f"{t},{r},{digest}"

    def _app_headers(body: str = "") -> Dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 miHoYoBBS/2.90.1 Capture/2.2.0",
            "Accept": "*/*",
            "Accept-Language": "zh-cn",
            "x-rpc-client_type": app_client_type,
            "x-rpc-app_version": app_version,
            "x-rpc-device_id": device_id,
            "x-rpc-device_fp": device_fp,
            "x-rpc-game_biz": "bbs_cn",
            "x-rpc-app_id": app_app_id,
            "x-rpc-sdk_version": "2.90.1",
            "x-rpc-account_version": app_version,
            "x-rpc-device_model": "Mi 14",
            "x-rpc-device_name": "Mihoyo Capture",
            "DS": _app_ds(body=body),
        }

    # Step 1: 创建 App 二维码
    create_url = "https://passport-api.mihoyo.com/account/ma-cn-passport/app/createQRLogin"
    LOGGER.info("[米游社] App QR 创建二维码...")
    create_resp = _http_post_json(create_url, {}, _app_headers(body="{}"))
    if create_resp.get("retcode") != 0:
        raise MihoyoSignError(f"创建二维码失败: {create_resp.get('message')}")

    create_data = create_resp.get("data") or {}
    qr_url = create_data.get("url") or ""
    ticket = create_data.get("ticket") or ""
    if not qr_url or not ticket:
        raise MihoyoSignError("创建二维码失败: 返回数据不完整")

    LOGGER.info("[米游社] App QR URL: %s", qr_url)
    LOGGER.info("[米游社] App QR ticket: %s", ticket)

    # 打开二维码
    import tempfile
    import subprocess
    try:
        import qrcode as _qrlib
        from qrcode.constants import ERROR_CORRECT_L
        qr_img = _qrlib.make(qr_url, error_correction=ERROR_CORRECT_L)
        tmp_path = os.path.join(tempfile.gettempdir(), f"mihoyo_app_qr_{int(time.time())}.png")
        qr_img.save(tmp_path)
        LOGGER.info("[米游社] App QR 二维码图片已保存: %s", tmp_path)
        subprocess.Popen(["cmd", "/c", "start", "", tmp_path], shell=False)
    except ImportError:
        LOGGER.warning("[米游社] qrcode 库不可用，请手动打开: %s", qr_url)

    # Step 2: 轮询扫码状态
    poll_url = "https://passport-api.mihoyo.com/account/ma-cn-passport/app/queryQRLoginStatus"
    LOGGER.info("[米游社] App QR 等待扫码...")
    stoken = ""
    mid = ""
    aid = ""
    for i in range(150):
        time.sleep(3)
        poll_body = {"ticket": ticket}
        poll_body_str = json.dumps(poll_body)
        poll_headers = _app_headers(body=poll_body_str)
        try:
            poll_resp = _http_post_json(poll_url, poll_body, poll_headers)
        except Exception as exc:
            LOGGER.warning("[米游社] App QR 查询异常: %s", exc)
            continue

        retcode = poll_resp.get("retcode", 0)
        if retcode == -3501 or retcode == -106:
            LOGGER.warning("[米游社] App QR 二维码已过期")
            break
        if retcode != 0:
            LOGGER.warning("[米游社] App QR 查询失败: %s (retcode=%s)", poll_resp.get("message"), retcode)
            continue

        data = poll_resp.get("data") or {}
        status = data.get("status") or ""
        LOGGER.info("[米游社] App QR 状态: %s", status)

        if status.lower() == "scanned":
            LOGGER.info("[米游社] App QR 已扫码，等待确认...")
        elif status.lower() == "confirmed":
            LOGGER.info("[米游社] App QR 扫码已确认！")
            # 直接从 tokens[0].token 提取 stoken
            tokens = data.get("tokens") or []
            user_info = data.get("user_info") or {}
            mid = str(user_info.get("mid") or "")
            aid = str(user_info.get("aid") or "")
            if tokens and isinstance(tokens, list) and len(tokens) > 0:
                stoken = str(tokens[0].get("token") or "")
            if stoken:
                LOGGER.info("[米游社] App QR stoken 获取成功, mid=%s, aid=%s", mid, aid)
            else:
                LOGGER.warning("[米游社] App QR tokens 中未找到 stoken: %s", repr(tokens)[:200])
            break

    if not stoken:
        raise MihoyoSignError("App QR 扫码登录失败：未获取到 stoken")

    # Step 3: 按 FufuLauncher ExchangeV2TokensAndSaveAsync 获取完整 v2 cookies
    cookies: Dict[str, str] = {
        "stoken": stoken,
        "stoken_v2": stoken,
        "mid": mid,
        "account_id": aid,
        "stuid": aid,
        "ltuid": aid,
    }

    def _common_ds(body: str = "", query: str = "", salt: str = app_salt) -> str:
        from hashlib import md5 as _md5x
        t = str(int(time.time()))
        r = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        raw = f"salt={salt}&t={t}&r={r}&b={body}&q={query}"
        return f"{t},{r},{_md5x(raw.encode()).hexdigest()}"

    def _common_headers(body: str = "", query: str = "", cookie: str = "",
                        referer: str = "", client_type: str = "2",
                        app_id: str = "bll8iq97cem8", version: str = "2.90.1") -> Dict[str, str]:
        h = {
            "User-Agent": "Mozilla/5.0 miHoYoBBS/2.90.1 Capture/2.2.0",
            "Accept": "*/*",
            "Accept-Language": "zh-cn",
            "x-rpc-client_type": client_type,
            "x-rpc-app_version": version,
            "x-rpc-device_id": device_id,
            "x-rpc-device_fp": device_fp,
            "x-rpc-game_biz": "bbs_cn",
            "x-rpc-app_id": app_id,
            "x-rpc-sdk_version": "2.90.1",
            "x-rpc-account_version": version,
            "x-rpc-device_model": "Mi 14",
            "x-rpc-device_name": "Mihoyo Capture",
            "DS": _common_ds(body=body, query=query),
        }
        if cookie:
            h["Cookie"] = cookie
        if referer:
            h["Referer"] = referer
        return h

    from urllib.request import Request as _Req, urlopen as _UO

    def _get_json(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
        req = _Req(url, headers=headers, method="GET")
        with _UO(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_json(url: str, body_dict: Dict, headers: Dict[str, str]) -> Dict[str, Any]:
        data = json.dumps(body_dict).encode("utf-8")
        req = _Req(url, data=data, headers=headers, method="POST")
        with _UO(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        # 3a: stoken → cookie_token（genshin.py cn_fetch_cookie_token_with_stoken_v2 方式）
        from hashlib import md5 as _md5_ct
        app_login_salt = "IZPgfb0dRPtBeLuFkdDznSZ6f4wWt6y2"
        ct_t = str(int(time.time()))
        ct_r = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        ct_query = f"uid={aid}&stoken={stoken}"
        ct_raw = f"salt={app_login_salt}&t={ct_t}&r={ct_r}&b=&q={ct_query}"
        ct_ds = f"{ct_t},{ct_r},{_md5_ct(ct_raw.encode()).hexdigest()}"
        ct_headers = {
            "User-Agent": "Mozilla/5.0 miHoYoBBS/2.90.1 Capture/2.2.0",
            "Accept": "*/*",
            "x-rpc-client_type": "2",
            "x-rpc-app_version": "2.90.1",
            "x-rpc-app_id": "bll8iq97cem8",
            "Cookie": f"stuid={aid};stoken={stoken};mid={mid}",
            "DS": ct_ds,
        }
        ct_url = f"https://passport-api.mihoyo.com/account/auth/api/getCookieAccountInfoBySToken?{ct_query}"
        ct_resp = _get_json(ct_url, ct_headers)
        if ct_resp.get("retcode") == 0:
            ct_data = ct_resp.get("data") or {}
            cookie_token = str(ct_data.get("cookie_token") or ct_data.get("cookie_token_v2") or "")
            if cookie_token:
                cookies["cookie_token"] = cookie_token
                cookies["cookie_token_v2"] = cookie_token
                LOGGER.info("[米游社] App QR cookie_token 获取成功")
        else:
            LOGGER.warning("[米游社] App QR cookie_token: %s", ct_resp.get("message"))
    except Exception as exc:
        LOGGER.warning("[米游社] App QR cookie_token 异常: %s", exc)

    # 3b: stoken → ltoken（用 ltoken 查每日便笺不触发 Geetest）
    try:
        ltoken_salt = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"
        lt_t = str(int(time.time()))
        lt_r = str(random.randint(100001, 200000))
        lt_q = f"uid={aid}"
        lt_raw = f"salt={ltoken_salt}&t={lt_t}&r={lt_r}&b=&q={lt_q}"
        lt_ds = f"{lt_t},{lt_r},{_md5_ct(lt_raw.encode()).hexdigest()}"
        lt_headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 miHoYoBBS/2.99.1",
            "DS": lt_ds,
            "x-rpc-app_version": "2.99.1",
            "x-rpc-client_type": "5",
            "Cookie": f"ltuid={aid};stoken={stoken};mid={mid}",
        }
        lt_url = f"https://api-takumi.mihoyo.com/account/auth/api/getLTokenBySToken?uid={aid}"
        lt_resp = _get_json(lt_url, lt_headers)
        if lt_resp.get("retcode") == 0:
            lt_data = lt_resp.get("data") or {}
            ltoken = str(lt_data.get("ltoken") or "")
            if ltoken:
                cookies["ltoken"] = ltoken
                cookies["ltuid"] = aid
                LOGGER.info("[米游社] App QR ltoken 获取成功")
        else:
            LOGGER.warning("[米游社] App QR ltoken: %s", lt_resp.get("message"))
    except Exception as exc:
        LOGGER.warning("[米游社] App QR ltoken 异常: %s", exc)

    # 3c: 按 FufuLauncher ExchangeV2TokensAndSaveAsync 获取 v2 cookies
    # 通过 Web QR 模拟扫码，从 Set-Cookie 提取 ltoken_v2/cookie_token_v2 等
    try:
        # 创建 Web QR
        web_create_url = "https://passport-api.mihoyo.com/account/ma-cn-passport/web/createQRLogin"
        web_create_resp = _post_json(web_create_url, {}, _common_headers(body="{}"))
        web_ticket = ""
        if web_create_resp.get("retcode") == 0:
            web_ticket = (web_create_resp.get("data") or {}).get("ticket") or ""
        LOGGER.info("[米游社] App QR Web ticket: %s", web_ticket[:20] if web_ticket else "空")

        if web_ticket:
            auth_cookie = f"stoken={stoken}; mid={mid}"
            scan_body = {"ticket": web_ticket, "token_types": ["4"]}
            scan_body_str = json.dumps(scan_body)

            # 模拟 APP 扫码
            scan_url = "https://passport-api.mihoyo.com/account/ma-cn-passport/app/scanQRLogin"
            _post_json(scan_url, scan_body,
                       _common_headers(body=scan_body_str, cookie=auth_cookie))
            time.sleep(1)

            # 模拟 APP 确认
            confirm_url = "https://passport-api.mihoyo.com/account/ma-cn-passport/app/confirmQRLogin"
            _post_json(confirm_url, scan_body,
                       _common_headers(body=scan_body_str, cookie=auth_cookie))

            # 查询 Web QR 状态，从 Set-Cookie 提取 v2 cookies
            web_query_url = "https://passport-api.mihoyo.com/account/ma-cn-passport/web/queryQRLoginStatus"
            web_query_body = {"ticket": web_ticket}
            web_query_str = json.dumps(web_query_body)
            web_req = _Req(web_query_url,
                          data=json.dumps(web_query_body).encode("utf-8"),
                          headers=_common_headers(body=web_query_str),
                          method="POST")
            with _UO(web_req, timeout=20) as web_resp:
                set_cookies = web_resp.headers.get_all("Set-Cookie") or []
                for sc in set_cookies:
                    for part in sc.split(";"):
                        part = part.strip()
                        if "=" in part:
                            k, v = part.split("=", 1)
                            k = k.strip()
                            if k in ("ltoken_v2", "cookie_token_v2", "ltuid_v2",
                                     "account_id_v2", "account_mid_v2", "ltmid_v2"):
                                cookies[k] = v.strip()
                LOGGER.info("[米游社] App QR v2 cookies 提取: %s",
                            [k for k in cookies if k.endswith("_v2")])
    except Exception as exc:
        LOGGER.warning("[米游社] App QR v2 cookies 异常: %s", exc)

    LOGGER.info("[米游社] App QR 扫码登录完成: %s", sorted(cookies.keys()))

    # 立即保存 stoken 到配置（即使 cookie_token 获取失败也不丢失）
    try:
        from pathlib import Path as _Path
        cookie_parts = []
        for key in ("account_id", "stuid", "stoken", "stoken_v2", "mid",
                    "cookie_token", "cookie_token_v2", "ltuid",
                    "ltoken", "ltoken_v2", "ltuid_v2",
                    "account_id_v2", "account_mid_v2", "ltmid_v2"):
            val = cookies.get(key) or ""
            if val:
                cookie_parts.append(f"{key}={val}")
        cookie_str = "; ".join(cookie_parts)
        config_path = _Path(__file__).resolve().parent.parent / "app_config.json"
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        else:
            config = {}
        mihoyo_cfg = config.setdefault("mihoyo_sign", {})
        mihoyo_cfg["cookie"] = cookie_str
        mihoyo_cfg["stoken_cookie"] = cookie_str
        from ..settings.manager import atomic_write_json
        atomic_write_json(config_path, config)
        LOGGER.info("[米游社] App QR stoken 已保存到配置文件")
    except Exception as exc:
        LOGGER.warning("[米游社] App QR 保存配置失败: %s", exc)

    return {"cookies": cookies, "raw": {"retcode": 0, "message": "ok"}}


def complete_qrcode_login(*, browser_name: str = "") -> Dict[str, Any]:
    result = login_by_qrcode_app()
    cookies = result.get("cookies") or {}
    if not isinstance(cookies, dict):
        cookies = {}

    stoken = str(cookies.get("stoken") or cookies.get("stoken_v2") or "").strip()
    account_id = str(cookies.get("account_id") or cookies.get("stuid") or "").strip()
    mid = str(cookies.get("mid") or "").strip()

    LOGGER.info("[米游社] 扫码完成: stoken=%s, account_id=%s", bool(stoken), account_id)

    # 构建 cookie 字符串
    cookie_parts = []
    for key in ("account_id", "stuid", "stoken", "stoken_v2", "mid",
                "cookie_token", "cookie_token_v2", "ltoken", "ltuid"):
        val = cookies.get(key) or ""
        if val:
            cookie_parts.append(f"{key}={val}")
    full_cookie = "; ".join(cookie_parts)

    record_ready = bool(stoken and account_id)
    LOGGER.info("[米游社] 扫码登录完成：账号 %s | 便笺查询 %s",
                account_id, "已就绪" if record_ready else "未就绪")
    return {
        "cookie": full_cookie,
        "stoken_cookie": full_cookie,
        "fields": cookies,
        "account_id": account_id,
        "record_ready": record_ready,
        "method": "qrcode_app",
    }


def validate_cookie_text(cookie: str) -> Dict[str, Any]:
    cleaned = tidy_cookie(cookie)
    fields = extract_cookie_fields(cleaned)
    account_id = _resolve_cookie_account_id(fields)
    token_keys = [
        key
        for key in ("cookie_token_v2", "cookie_token", "ltoken_v2", "ltoken", "login_ticket", "login_ticket_v2")
        if fields.get(key)
    ]
    missing: List[str] = []
    if not account_id:
        missing.append("account_id / account_id_v2 / ltuid")
    if not token_keys:
        missing.append("cookie_token_v2 / cookie_token / ltoken_v2 / ltoken")
    return {
        "ok": not missing,
        "cookie": cleaned,
        "fields": fields,
        "account_id": account_id,
        "account_display": _mask_value(account_id) if account_id else "未知账号",
        "token_keys": token_keys,
        "missing": missing,
        "message": "Cookie 关键字段完整，可用于请求。" if not missing else f"Cookie 缺少关键字段：{'；'.join(missing)}",
    }


def extract_cookie_from_browser(browser_name: str = "edge") -> Dict[str, Any]:
    LOGGER.info("[米游社] 开始从 %s 读取浏览器 Cookie", browser_name)
    if browser_cookie3 is None:
        raise MihoyoSignError("未安装 browser-cookie3，无法从浏览器自动读取 Cookie。")
    loader = _get_browser_loader(browser_name)
    cookie_map: Dict[str, str] = {}
    load_errors: List[tuple[str, str, str]] = []
    for domain in COOKIE_SEARCH_DOMAINS:
        try:
            jar = loader(domain_name=domain)
        except Exception as exc:
            error_name = type(exc).__name__
            error_text = str(exc)
            load_errors.append((domain, error_name, error_text))
            LOGGER.warning("[米游社] 读取域名 %s 的 Cookie 失败：%s: %s", domain, error_name, error_text)
            continue
        count = _merge_cookiejar(cookie_map, jar)
        LOGGER.debug("[米游社] 从域名 %s 读取到 %d 项 Cookie", domain, count)
    cookie = tidy_cookie("; ".join(f"{key}={value}" for key, value in cookie_map.items()))
    if not cookie:
        admin_error = next(
            (
                error_text
                for _domain, error_name, error_text in load_errors
                if error_name == "RequiresAdminError" or "requires admin" in error_text.lower()
            ),
            "",
        )
        if admin_error:
            raise MihoyoSignError(
                f"当前环境读取 {browser_name} 浏览器 Cookie 需要管理员权限。请用管理员身份启动 Flet GUI 后重试。"
            )
        decrypt_error = next(
            (
                error_text
                for _domain, error_name, error_text in load_errors
                if error_name == "BrowserCookieError" and "decryption" in error_text.lower()
            ),
            "",
        )
        if decrypt_error:
            raise MihoyoSignError(
                f"当前环境无法解密 {browser_name} 浏览器 Cookie。请改用手动粘贴完整 Cookie，或尝试其他浏览器。"
            )
        browser_error = next(
            (
                error_text
                for _domain, error_name, error_text in load_errors
                if error_name == "BrowserCookieError"
            ),
            "",
        )
        if browser_error:
            raise MihoyoSignError(
                f"当前没有找到 {browser_name} 浏览器可用的米游社 Cookie。请确认已在该浏览器登录米游社。"
            )
        raise MihoyoSignError("未从浏览器中读取到米游社 Cookie，请先在浏览器完成登录。")
    fields = extract_cookie_fields(cookie)
    if not fields:
        raise MihoyoSignError("浏览器里存在米游社 Cookie，但没有读到可用的登录字段。")
    LOGGER.info(
        "[米游社] 已从 %s 读取可用 Cookie，账号 %s，识别到字段 %s",
        browser_name,
        _cookie_identity(cookie),
        "、".join(sorted(fields.keys())),
    )
    enriched = enrich_cookie_for_record_api(cookie)
    if enriched.get("added_fields"):
        LOGGER.info(
            "[米游社] 已自动补齐游戏记录认证字段：%s",
            "、".join(sorted(str(key) for key in enriched.get("added_fields", {}).keys())),
        )
    return {
        "browser": browser_name,
        "cookie": enriched.get("cookie") or cookie,
        "fields": enriched.get("fields") or fields,
        "device_id": get_device_id(str(enriched.get("cookie") or cookie)),
        "record_ready": bool(enriched.get("record_ready")),
        "added_fields": enriched.get("added_fields") or {},
    }


def get_bound_accounts(cookie: str, game_key: str) -> List[Dict[str, str]]:
    definition = _require_game(game_key)
    LOGGER.info("[米游社] 获取 %s 绑定角色列表", definition.label)
    headers = _build_game_headers(cookie, referer=definition.login_url)
    response = _request_json(
        MIYOUSHE_ACCOUNT_INFO_URL,
        headers=headers,
        timeout=20,
        query={"game_biz": definition.game_biz},
        method="GET",
    )
    if response.get("retcode") != 0:
        message = str(response.get("message") or "获取绑定角色失败")
        raise MihoyoSignError(message)
    result: List[Dict[str, str]] = []
    for item in response.get("data", {}).get("list", []) or []:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "nickname": str(item.get("nickname") or "").strip(),
                "uid": str(item.get("game_uid") or "").strip(),
                "region": str(item.get("region") or "").strip(),
            }
        )
    filtered = [item for item in result if item["uid"] and item["region"]]
    LOGGER.info("[米游社] %s 绑定角色数量：%d", definition.label, len(filtered))
    return filtered


def fetch_sign_overview(cookie: str, game_key: str) -> Dict[str, Any]:
    definition = _require_game(game_key)
    LOGGER.info("[米游社] 获取 %s 签到概览", definition.label)
    accounts = get_bound_accounts(cookie, game_key)
    rewards = _get_checkin_rewards(cookie, definition)
    account_states: List[Dict[str, Any]] = []
    for account in accounts:
        info = _get_sign_info(cookie, definition, account["region"], account["uid"])
        account_states.append(
            {
                **account,
                "today": info.get("today"),
                "total_sign_day": int(info.get("total_sign_day") or 0),
                "is_sign": bool(info.get("is_sign")),
                "first_bind": bool(info.get("first_bind")),
                "reward": _resolve_reward_name(rewards, int(info.get("total_sign_day") or 0), signed=bool(info.get("is_sign"))),
            }
        )
    return {
        "game_key": definition.key,
        "game_label": definition.label,
        "accounts": account_states,
        "reward_count": len(rewards),
    }


def sign_game(cookie: str, game_key: str) -> Dict[str, Any]:
    definition = _require_game(game_key)
    LOGGER.info("[米游社] 开始执行 %s 签到", definition.label)
    accounts = get_bound_accounts(cookie, game_key)
    rewards = _get_checkin_rewards(cookie, definition)
    results: List[Dict[str, Any]] = []

    if not accounts:
        LOGGER.warning("[米游社] %s 当前 Cookie 没有绑定可签到角色", definition.label)
        return {
            "game_key": definition.key,
            "game_label": definition.label,
            "accounts": [],
            "success_count": 0,
            "already_count": 0,
            "failed_count": 0,
        }

    for account in accounts:
        account_label = _account_label(account)
        info = _get_sign_info(cookie, definition, account["region"], account["uid"])
        total_sign_day = int(info.get("total_sign_day") or 0)
        if bool(info.get("first_bind")):
            LOGGER.warning("[米游社] %s - %s 首次绑定，需先手动签到一次", definition.label, account_label)
            results.append(
                {
                    **account,
                    "status": "first_bind",
                    "message": "首次绑定账号，请先手动签到一次。",
                    "reward": "",
                    "total_sign_day": total_sign_day,
                }
            )
            continue
        if bool(info.get("is_sign")):
            LOGGER.info("[米游社] %s - %s 今天已经签到过", definition.label, account_label)
            results.append(
                {
                    **account,
                    "status": "already",
                    "message": "今天已经签到过。",
                    "reward": _resolve_reward_name(rewards, total_sign_day, signed=True),
                    "total_sign_day": total_sign_day,
                }
            )
            continue

        response = _post_sign(cookie, definition, account["region"], account["uid"])
        retcode = int(response.get("retcode") or 0)
        data = response.get("data") or {}
        if retcode == 0 and isinstance(data, dict) and int(data.get("success") or 0) == 0:
            reward_name = _resolve_reward_name(rewards, total_sign_day + 1, signed=True)
            LOGGER.info(
                "[米游社] %s - %s 签到成功，奖励 %s，本月已签 %d 天",
                definition.label,
                account_label,
                reward_name or "未知奖励",
                total_sign_day + 1,
            )
            results.append(
                {
                    **account,
                    "status": "success",
                    "message": "签到成功。",
                    "reward": reward_name,
                    "total_sign_day": total_sign_day + 1,
                }
            )
            continue
        if retcode == -5003:
            LOGGER.info("[米游社] %s - %s 接口返回今天已经签到过", definition.label, account_label)
            results.append(
                {
                    **account,
                    "status": "already",
                    "message": "今天已经签到过。",
                    "reward": _resolve_reward_name(rewards, total_sign_day, signed=True),
                    "total_sign_day": total_sign_day,
                }
            )
            continue
        if retcode == 0 and isinstance(data, dict) and int(data.get("success") or 0) == 1:
            message = "触发验证码，请先手动签到一次。"
        else:
            message = str(response.get("message") or "签到失败")
        LOGGER.warning("[米游社] %s - %s 签到失败：%s", definition.label, account_label, message)
        results.append(
            {
                **account,
                "status": "failed",
                "message": message,
                "reward": "",
                "total_sign_day": total_sign_day,
            }
        )

    summary = {
        "game_key": definition.key,
        "game_label": definition.label,
        "accounts": results,
        "success_count": sum(1 for item in results if item["status"] == "success"),
        "already_count": sum(1 for item in results if item["status"] == "already"),
        "failed_count": sum(1 for item in results if item["status"] not in {"success", "already"}),
    }
    LOGGER.info(
        "[米游社] %s 签到结束：成功 %d，已签 %d，失败 %d",
        definition.label,
        summary["success_count"],
        summary["already_count"],
        summary["failed_count"],
    )
    return summary


def sign_games(cookie: str, game_keys: Iterable[str]) -> Dict[str, Any]:
    cleaned_cookie = tidy_cookie(cookie)
    if not cleaned_cookie:
        raise MihoyoSignError("请先提供米游社 Cookie。")
    selected_keys = [key for key in game_keys if key in GAME_DEFINITIONS]
    if not selected_keys:
        raise MihoyoSignError("没有选择需要签到的米游社游戏。")

    # 用 stoken 强制刷新 cookie_token，避免 cookie_token 过期导致签到失败（retcode -100）
    cleaned_cookie = _ensure_fresh_cookie_token(cleaned_cookie)

    LOGGER.info(
        "[米游社] 开始执行签到任务：账号 %s，游戏 %s",
        _cookie_identity(cleaned_cookie),
        _game_key_summary(selected_keys),
    )

    results: List[Dict[str, Any]] = []
    for game_key in selected_keys:
        results.append(sign_game(cleaned_cookie, game_key))

    summary = {
        "cookie_fields": extract_cookie_fields(cleaned_cookie),
        "games": results,
        "selected_keys": selected_keys,
    }
    LOGGER.info(
        "[米游社] 签到任务完成：成功 %d，已签 %d，失败 %d",
        sum(int(item.get("success_count") or 0) for item in results),
        sum(int(item.get("already_count") or 0) for item in results),
        sum(int(item.get("failed_count") or 0) for item in results),
    )
    return summary


def format_sign_report(result: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for game in result.get("games") or []:
        label = str(game.get("game_label") or game.get("game_key") or "米游社游戏")
        accounts = game.get("accounts") or []
        if not accounts:
            lines.append(f"{label}: 当前 Cookie 没有绑定可签到的账号")
            continue
        lines.append(
            f"{label}: 成功 {game.get('success_count', 0)}，已签 {game.get('already_count', 0)}，失败 {game.get('failed_count', 0)}"
        )
        for account in accounts:
            nickname = str(account.get("nickname") or account.get("uid") or "账号")
            uid = str(account.get("uid") or "").strip()
            reward = str(account.get("reward") or "").strip()
            reward_note = f" | 奖励 {reward}" if reward else ""
            sign_day = int(account.get("total_sign_day") or 0)
            sign_day_note = f" | 本月已签 {sign_day} 天" if sign_day else ""
            uid_note = f" ({uid})" if uid else ""
            lines.append(f"  - {nickname}{uid_note}: {account.get('message', '')}{reward_note}{sign_day_note}")
    return lines


def _merge_cookiejar(target: Dict[str, str], cookiejar: CookieJar) -> int:
    count = 0
    for cookie in cookiejar:
        if not getattr(cookie, "name", ""):
            continue
        target[str(cookie.name)] = str(cookie.value)
        count += 1
    return count


def _get_browser_loader(browser_name: str):
    browser_name = str(browser_name or "edge").strip().lower()
    loader_map = {
        "edge": getattr(browser_cookie3, "edge", None),
        "chrome": getattr(browser_cookie3, "chrome", None),
        "brave": getattr(browser_cookie3, "brave", None),
    }
    loader = loader_map.get(browser_name)
    if loader is None:
        raise MihoyoSignError(f"暂不支持浏览器：{browser_name}")
    return loader


def _require_game(game_key: str) -> MihoyoGameDefinition:
    definition = GAME_DEFINITIONS.get(str(game_key or "").strip())
    if definition is None:
        raise MihoyoSignError(f"不支持的米游社游戏：{game_key}")
    return definition


def _get_checkin_rewards(cookie: str, definition: MihoyoGameDefinition) -> List[Dict[str, Any]]:
    headers = _build_game_headers(cookie, referer=_get_event_referer(definition), signgame=definition.signgame)
    response = _request_json(
        definition.rewards_url,
        headers=headers,
        timeout=20,
        query={"act_id": definition.act_id},
        method="GET",
    )
    if response.get("retcode") != 0:
        raise MihoyoSignError(str(response.get("message") or f"获取 {definition.label} 奖励列表失败"))
    awards = response.get("data", {}).get("awards") or []
    return [item for item in awards if isinstance(item, dict)]


def _get_sign_info(cookie: str, definition: MihoyoGameDefinition, region: str, uid: str) -> Dict[str, Any]:
    headers = _build_game_headers(cookie, referer=_get_event_referer(definition), signgame=definition.signgame)
    response = _request_json(
        definition.info_url,
        headers=headers,
        timeout=20,
        query={"act_id": definition.act_id, "region": region, "uid": uid},
        method="GET",
    )
    if response.get("retcode") != 0:
        message = str(response.get("message") or f"获取 {definition.label} 签到状态失败")
        raise MihoyoSignError(message)
    data = response.get("data")
    return data if isinstance(data, dict) else {}


def _post_sign(cookie: str, definition: MihoyoGameDefinition, region: str, uid: str) -> Dict[str, Any]:
    payload = {"act_id": definition.act_id, "region": region, "uid": uid}
    headers = _build_game_headers(
        cookie,
        referer=_get_event_referer(definition),
        signgame=definition.signgame,
        body=payload,
        include_json=True,
    )
    return _request_json(
        definition.sign_url,
        headers=headers,
        timeout=20,
        body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        method="POST",
    )


def _build_game_headers(
    cookie: str,
    *,
    referer: str,
    signgame: str = "",
    body: Optional[Dict[str, Any]] = None,
    include_json: bool = False,
) -> Dict[str, str]:
    cookie = tidy_cookie(cookie)
    user_agent = (
        "Mozilla/5.0 (Linux; Android 12; Unspecified Device) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Version/4.0 Chrome/103.0.5060.129 Mobile Safari/537.36 miHoYoBBS/{MIYOUSHE_VERSION}"
    )
    headers = {
        "Accept": "application/json, text/plain, */*",
        "DS": _get_ds(),
        "x-rpc-channel": "miyousheluodi",
        "Origin": "https://act.mihoyo.com",
        "x-rpc-app_version": MIYOUSHE_VERSION,
        "x-rpc-client_type": MIYOUSHE_CLIENT_TYPE_WEB,
        "Referer": referer or MIYOUSHE_DEFAULT_REFERER,
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,en-US;q=0.8",
        "X-Requested-With": "com.mihoyo.hyperion",
        "Cookie": cookie,
        "x-rpc-device_id": get_device_id(cookie),
        "User-Agent": user_agent,
    }
    if signgame:
        headers["x-rpc-signgame"] = signgame
    if include_json:
        headers["Content-Type"] = "application/json; charset=UTF-8"
    return headers


def _request_json(
    url: str,
    *,
    headers: Dict[str, str],
    timeout: int,
    query: Optional[Dict[str, Any]] = None,
    body: Optional[bytes] = None,
    method: str,
) -> Dict[str, Any]:
    request_url = url
    if query:
        separator = "&" if "?" in request_url else "?"
        request_url = f"{request_url}{separator}{urlencode(query)}"
    LOGGER.debug("[米游社] 请求 %s %s", method, request_url)
    request = Request(request_url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            text = _decode_response_text(response.read(), response.headers.get("Content-Encoding", ""))
    except HTTPError as exc:
        try:
            error_text = _decode_response_text(exc.read(), exc.headers.get("Content-Encoding", "") if exc.headers else "")
        except Exception:
            error_text = ""
        LOGGER.warning("[米游社] 请求 %s %s 失败，HTTP %s", method, request_url, exc.code)
        raise MihoyoSignError(_extract_api_message(error_text) or f"HTTP {exc.code}") from exc
    except URLError as exc:
        LOGGER.warning("[米游社] 请求 %s %s 网络连接失败：%s", method, request_url, exc.reason)
        raise MihoyoSignError(f"网络连接失败：{exc.reason}") from exc
    except Exception as exc:
        LOGGER.exception("[米游社] 请求 %s %s 时发生异常", method, request_url)
        raise MihoyoSignError(_sanitize_message_text(str(exc))) from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MihoyoSignError(f"接口返回不是有效 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise MihoyoSignError("接口返回不是对象结构。")
    return payload


def _get_ds(body: Any = None, query: Optional[Dict[str, str]] = None) -> str:
    """生成国服 DS 签名（genshin.py generate_cn_dynamic_secret 格式）。"""
    timestamp = str(int(time.time()))
    r = str(random.randint(100001, 200000))
    b = json.dumps(body) if body else ""
    q = "&".join(f"{k}={v}" for k, v in sorted(query.items())) if query else ""
    raw = f"salt={MIYOUSHE_SALT_WEB}&t={timestamp}&r={r}&b={b}&q={q}"
    digest = md5(raw.encode("utf-8")).hexdigest()
    return f"{timestamp},{r},{digest}"


def get_device_id(cookie: str) -> str:
    cleaned = tidy_cookie(cookie)
    return str(uuid.uuid3(uuid.NAMESPACE_URL, cleaned or "game_auto_miyoushe"))


def _resolve_reward_name(rewards: List[Dict[str, Any]], total_sign_day: int, signed: bool) -> str:
    if not rewards:
        return ""
    index = max(0, total_sign_day - 1) if signed else max(0, total_sign_day)
    if index >= len(rewards):
        index = len(rewards) - 1
    reward = rewards[index]
    name = str(reward.get("name") or "").strip()
    count = reward.get("cnt")
    return f"「{name}」x{count}" if name and count is not None else name


def _cookie_identity(cookie: str) -> str:
    fields = extract_cookie_fields(cookie)
    account_id = fields.get("account_id_v2") or fields.get("account_id") or fields.get("ltuid_v2") or fields.get("ltuid") or ""
    if not account_id:
        return "未知账号"
    return _mask_value(account_id)


def _mask_value(value: str) -> str:
    raw = str(value or "").strip()
    if len(raw) <= 4:
        return raw or "-"
    if len(raw) <= 8:
        return f"{raw[:2]}***{raw[-2:]}"
    return f"{raw[:3]}***{raw[-3:]}"


def _game_key_summary(game_keys: Iterable[str]) -> str:
    labels = [GAME_DEFINITIONS[key].label for key in game_keys if key in GAME_DEFINITIONS]
    return "、".join(labels) if labels else "无"


def _account_label(account: Dict[str, Any]) -> str:
    nickname = str(account.get("nickname") or "账号").strip()
    uid = str(account.get("uid") or "").strip()
    return f"{nickname}({uid})" if uid else nickname


def _get_event_referer(definition: MihoyoGameDefinition) -> str:
    return f"{definition.origin.rstrip('/')}/"


def _decode_response_text(raw: bytes, content_encoding: str) -> str:
    payload = raw or b""
    encoding = str(content_encoding or "").lower()
    if payload:
        try:
            if "gzip" in encoding or payload.startswith(b"\x1f\x8b"):
                payload = gzip.decompress(payload)
            elif "deflate" in encoding:
                payload = zlib.decompress(payload)
        except Exception as exc:
            LOGGER.debug("[米游社] 响应解压失败，按原始字节解析: %s", exc)
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("utf-8", errors="replace")


def _extract_api_message(text: str) -> str:
    cleaned = _sanitize_message_text(text)
    if not cleaned:
        return ""
    try:
        payload = json.loads(cleaned)
    except Exception:
        return cleaned
    if not isinstance(payload, dict):
        return cleaned
    message = str(payload.get("message") or payload.get("msg") or "").strip()
    return _sanitize_message_text(message) if message else cleaned


def _sanitize_message_text(text: str) -> str:
    message = str(text or "").strip()
    if not message:
        return ""
    pattern = re.compile(
        rf"(?P<key>{'|'.join(re.escape(key) for key in SENSITIVE_COOKIE_KEYS)})=(?P<value>[^;\s]+)",
        re.IGNORECASE,
    )

    def _replace(match: re.Match[str]) -> str:
        key = match.group("key")
        value = match.group("value")
        return f"{key}={_mask_value(value)}"

    return pattern.sub(_replace, message)
