"""鸣潮库街区客户端。

封装库街区登录、角色查询、每日数据、基础数据与签到接口，
供 Flet 界面直接调用。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("GameAutomation")

ACCOUNT_LOGIN_URL = "https://api.kurobbs.com/user/sdkLogin"
ACCOUNT_ROLE_LIST_URL = "https://api.kurobbs.com/gamer/role/list"
DAILY_DATA_URL = "https://api.kurobbs.com/gamer/widget/game3/getData"
DAILY_REFRESH_URL = "https://api.kurobbs.com/gamer/widget/game3/refresh"
BASE_DATA_URL = "https://api.kurobbs.com/aki/roleBox/akiBox/baseData"
ROLE_BOX_REFRESH_URL = "https://api.kurobbs.com/aki/roleBox/akiBox/refreshData"
ROLE_ACCESS_TOKEN_URL = "https://api.kurobbs.com/aki/roleBox/requestToken"
SIGNIN_URL = "https://api.kurobbs.com/encourage/signIn/v2"
SIGNIN_QUERY_URL = "https://api.kurobbs.com/encourage/signIn/queryRecordV2"
SIGNIN_INIT_URL = "https://api.kurobbs.com/encourage/signIn/initSignInV2"

GAME_ID = "3"
SERVER_ID = "76402e5b20be2c39f095a152090afddc"

APP_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 9; 23116PN5BC Build/PQ3A.190605.02201427; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/124.0.6367.82 "
    "Mobile Safari/537.36 Kuro/2.5.0 KuroGameBox/2.5.0"
)
LEGACY_SEC_CH_UA = '"Chromium";v="124", "Android WebView";v="124", "Not-A.Brand";v="99"'
LEGACY_SEC_CH_UA_PLATFORM = '"Android"'
PUBLIC_IP_URLS = (
    "https://event.kurobbs.com/event/ip",
    "https://api.ipify.org/?format=json",
    "https://httpbin.org/ip",
)
PUBLIC_IP_CACHE_SECONDS = 86400

_public_ip_cache: Optional[str] = None
_public_ip_cache_time = 0.0
_devcode_cache: Optional[str] = None
_access_token_cache: Dict[str, str] = {}


class WuwaKujiequError(RuntimeError):
    """库街区接口错误。"""


@dataclass
class WuwaKujiequProfile:
    token: str
    did: str
    is_web: bool = False
    user_id: str = ""
    role_id: str = ""
    role_name: str = ""

    def with_role(self, role: Dict[str, Any]) -> "WuwaKujiequProfile":
        return WuwaKujiequProfile(
            token=self.token,
            did=self.did,
            is_web=self.is_web,
            user_id=str(role.get("user_id") or ""),
            role_id=str(role.get("role_id") or ""),
            role_name=str(role.get("role_name") or ""),
        )


def profile_from_dict(data: Optional[Dict[str, Any]]) -> Optional[WuwaKujiequProfile]:
    if not data:
        return None
    token = str(data.get("token") or "").strip()
    did = str(data.get("did") or "").strip()
    if not token or not did:
        return None
    return WuwaKujiequProfile(
        token=token,
        did=did,
        is_web=bool(data.get("is_web", False)),
        user_id=str(data.get("user_id") or "").strip(),
        role_id=str(data.get("role_id") or "").strip(),
        role_name=str(data.get("role_name") or "").strip(),
    )


def profile_to_dict(profile: WuwaKujiequProfile) -> Dict[str, Any]:
    return asdict(profile)


def login_with_code(phone: str, code: str, is_web: bool = False) -> Tuple[WuwaKujiequProfile, List[Dict[str, str]]]:
    phone = str(phone or "").strip()
    code = str(code or "").strip()
    if not phone or not code:
        raise WuwaKujiequError("登录需要手机号和验证码。")

    did = uuid.uuid4().hex
    query = urlencode({"code": code, "mobile": phone, "devCode": did})
    response = _request_json(
        f"{ACCOUNT_LOGIN_URL}?{query}",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "source": _source_name(is_web),
            "devcode": get_devcode_header(),
            "did": did,
        },
        timeout=5,
        body=b"",
    )
    payload = _expect_success(response, default_message="登录库街区失败")
    token = str(payload.get("token") or "").strip()
    if not token:
        raise WuwaKujiequError("登录返回中未包含 token。")

    roles = list_roles_by_token(token, is_web=is_web)
    profile = WuwaKujiequProfile(token=token, did=did, is_web=is_web)
    if roles:
        profile = profile.with_role(roles[0])
    return profile, roles


def login_with_token(
    token: str,
    is_web: bool = False,
    did: Optional[str] = None,
    selected_role_id: Optional[str] = None,
) -> Tuple[WuwaKujiequProfile, List[Dict[str, str]]]:
    token = str(token or "").strip()
    if not token:
        raise WuwaKujiequError("请先填写库街区 token。")

    roles = list_roles_by_token(token, is_web=is_web)
    profile = WuwaKujiequProfile(token=token, did=str(did or uuid.uuid4().hex), is_web=is_web)
    chosen_role = _pick_role(roles, selected_role_id)
    if chosen_role is not None:
        profile = profile.with_role(chosen_role)
    return profile, roles


def list_roles_by_token(token: str, is_web: bool = False) -> List[Dict[str, str]]:
    token = str(token or "").strip()
    if not token:
        raise WuwaKujiequError("请先提供有效 token。")

    response = _request_json(
        f"{ACCOUNT_ROLE_LIST_URL}?gameId={GAME_ID}",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "source": _source_name(is_web),
            "token": token,
        },
        timeout=5,
        body=b"",
    )
    data = _expect_success(response, default_message="找不到游戏账号信息，请检查库街区的游戏绑定")
    if not isinstance(data, list) or not data:
        raise WuwaKujiequError("找不到游戏账号信息，请检查库街区的游戏绑定。")

    roles: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        roles.append(
            {
                "user_id": str(item.get("userId") or "").strip(),
                "role_id": str(item.get("roleId") or "").strip(),
                "role_name": str(item.get("roleName") or "").strip(),
            }
        )
    roles = [role for role in roles if role["user_id"] and role["role_id"]]
    if not roles:
        raise WuwaKujiequError("找不到可用的鸣潮角色绑定信息。")
    return roles


def fetch_dashboard(profile: WuwaKujiequProfile, auto_sign: bool = False) -> Dict[str, Any]:
    _ensure_profile_ready(profile)
    errors: List[str] = []
    sign_result: Optional[Dict[str, Any]] = None

    sign_info: Optional[Dict[str, Any]] = None
    try:
        sign_info = fetch_sign_info(profile)
    except WuwaKujiequError as exc:
        errors.append(f"签到信息：{exc}")

    if auto_sign and sign_info is not None and not sign_info.get("is_signed", False):
        try:
            sign_result = perform_sign(profile)
            sign_info = fetch_sign_info(profile)
        except WuwaKujiequError as exc:
            errors.append(f"自动签到：{exc}")

    daily: Optional[Dict[str, Any]] = None
    try:
        daily = fetch_daily_data(profile)
    except WuwaKujiequError as exc:
        errors.append(f"每日数据：{exc}")

    base: Optional[Dict[str, Any]] = None
    try:
        base = fetch_base_data(profile)
    except WuwaKujiequError as exc:
        errors.append(f"基础数据：{exc}")

    if daily is None and base is None and sign_info is None:
        raise WuwaKujiequError("；".join(errors) or "未获取到任何库街区数据。")

    return {
        "profile": profile_to_dict(profile),
        "daily": daily,
        "base": base,
        "sign": sign_info,
        "sign_result": sign_result,
        "errors": errors,
    }


def fetch_daily_data(profile: WuwaKujiequProfile) -> Dict[str, Any]:
    _ensure_profile_ready(profile)
    access_token = _get_access_token(profile)
    refresh_daily_data(profile, access_token=access_token)
    query = urlencode(
        {
            "type": "2",
            "roleId": profile.role_id,
            "sizeType": "1",
            "gameId": GAME_ID,
            "serverId": SERVER_ID,
        }
    )
    response = _request_json(
        f"{DAILY_DATA_URL}?{query}",
        headers={
            **_base_headers(include_token=True, profile=profile, access_token=access_token),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=5,
        body=b"",
    )
    data = _expect_success(response, default_message="获取每日数据失败")
    if not isinstance(data, dict):
        raise WuwaKujiequError("每日数据返回格式异常。")

    battle_pass = []
    for item in data.get("battlePassData") or []:
        detail = _normalize_detail(item)
        if detail is not None:
            battle_pass.append(detail)

    daily_tasks = []
    for item in data.get("dailyTask") or data.get("dailyTasks") or []:
        detail = _normalize_detail(item)
        if detail is not None:
            daily_tasks.append(detail)

    return {
        "server_name": str(data.get("serverName") or "").strip(),
        "role_name": str(data.get("roleName") or profile.role_name or "").strip(),
        "has_signed": bool(data.get("hasSignIn", False)),
        "sign_text": str(data.get("signInTxt") or "").strip(),
        "energy": _normalize_detail(data.get("energyData")),
        "liveness": _normalize_detail(data.get("livenessData")),
        "daily_tasks": daily_tasks,
        "store_energy": _normalize_detail(data.get("storeEnergyData")),
        "weekly": _normalize_detail(data.get("weeklyData")),
        "weekly_rouge": _normalize_detail(data.get("weeklyRougeData")),
        "tower": _normalize_detail(data.get("towerData")),
        "slash_tower": _normalize_detail(data.get("slashTowerData")),
        "battle_pass": battle_pass,
    }


def refresh_daily_data(profile: WuwaKujiequProfile, access_token: Optional[str] = None) -> None:
    """刷新库街区服务器缓存，让 getData/baseData 返回最新每日与周常数据。"""
    _ensure_profile_ready(profile)
    access_token = access_token or _get_access_token(profile)
    LOGGER.info("[鸣潮库街区] 开始刷新每日/周常数据缓存：UID %s", profile.role_id)

    widget_query = urlencode({"type": "1", "sizeType": "2"})
    widget_response = _request_json(
        f"{DAILY_REFRESH_URL}?{widget_query}",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": APP_USER_AGENT,
            "source": _source_name(profile.is_web),
            "token": profile.token,
        },
        timeout=20,
        body=b"",
    )
    _expect_success(widget_response, default_message="刷新鸣潮每日数据失败")

    content_type, body = _encode_multipart_form(
        {
            "serverId": SERVER_ID,
            "roleId": profile.role_id,
            "gameId": GAME_ID,
        }
    )
    role_box_response = _request_json(
        ROLE_BOX_REFRESH_URL,
        headers={
            **_base_headers(include_token=False, profile=profile, access_token=access_token, source_override="android"),
            "Content-Type": content_type,
        },
        timeout=20,
        body=body,
    )
    _expect_success(role_box_response, default_message="刷新鸣潮角色数据失败")
    LOGGER.info("[鸣潮库街区] 每日/周常数据缓存刷新完成：UID %s", profile.role_id)


def fetch_base_data(profile: WuwaKujiequProfile) -> Dict[str, Any]:
    _ensure_profile_ready(profile)
    access_token = _get_access_token(profile)
    content_type, body = _encode_multipart_form(
        {
            "serverId": SERVER_ID,
            "roleId": profile.role_id,
            "gameId": GAME_ID,
        }
    )
    response = _request_json(
        BASE_DATA_URL,
        headers={
            **_base_headers(include_token=False, profile=profile, access_token=access_token, source_override="android"),
            "Content-Type": content_type,
        },
        timeout=5,
        body=body,
    )
    payload = _expect_success(response, default_message="获取基础数据失败")
    if payload is None:
        raise WuwaKujiequError("返回值为空，请先使用库街区 APP 查看鸣潮数据后再次尝试。")
    if not isinstance(payload, dict):
        raise WuwaKujiequError("基础数据返回格式异常。")

    return {
        "name": str(payload.get("name") or profile.role_name or "").strip(),
        "level": _as_int(payload.get("level")),
        "world_level": _as_int(payload.get("worldLevel")),
        "active_days": _as_int(payload.get("activeDays") or payload.get("loginDays") or payload.get("days") or 0),
        "role_num": _as_int(payload.get("roleNum")),
        "sound_box": _as_int(payload.get("soundBox")),
        "energy": _as_int(payload.get("energy") or payload.get("curEnergy") or payload.get("currentEnergy") or payload.get("stamina") or 0),
        "max_energy": _as_int(payload.get("maxEnergy") or payload.get("maxCurEnergy") or 240),
        "liveness": _as_int(payload.get("liveness") or payload.get("curLiveness") or payload.get("livenessCur") or 0),
        "liveness_max": _as_int(payload.get("livenessMaxCount")),
        "big_count": _as_int(payload.get("bigCount")),
        "small_count": _as_int(payload.get("smallCount")),
        "achievement_count": _as_int(payload.get("achievementCount")),
        "achievement_star": _as_int(payload.get("achievementStar")),
        "weekly_inst_count": _as_int(payload.get("weeklyInstCount")),
        "weekly_inst_limit": _as_int(payload.get("weeklyInstCountLimit")),
        "store_energy": _as_int(payload.get("storeEnergy")),
        "store_energy_limit": _as_int(payload.get("storeEnergyLimit")),
        "rouge_score": _as_int(payload.get("rougeScore")),
        "rouge_score_limit": _as_int(payload.get("rougeScoreLimit")),
        "rouge_title": str(payload.get("rougeTitle") or "").strip(),
        "box_list": _normalize_box_list(payload.get("boxList")),
        "phantom_box_list": _normalize_box_list(payload.get("phantomBoxList")),
        "treasure_box_list": _normalize_box_list(payload.get("treasureBoxList")),
    }


def fetch_sign_info(profile: WuwaKujiequProfile) -> Dict[str, Any]:
    _ensure_profile_ready(profile)
    query = urlencode(
        {
            "gameId": GAME_ID,
            "serverId": SERVER_ID,
            "roleId": profile.role_id,
            "userId": profile.user_id,
        }
    )
    response = _request_json(
        f"{SIGNIN_INIT_URL}?{query}",
        headers=_legacy_headers(profile),
        timeout=20,
        body=b"",
    )
    data = _expect_success(response, default_message="获取签到信息失败")
    if not isinstance(data, dict):
        raise WuwaKujiequError("签到信息返回格式异常。")

    signed_count = _as_int(data.get("sigInNum"))
    goods: List[Dict[str, Any]] = []
    for index, item in enumerate(data.get("signInGoodsConfigs") or [], start=1):
        if not isinstance(item, dict):
            continue
        goods.append(
            {
                "serial_num": _as_int(item.get("serialNum"), default=index),
                "goods_name": str(item.get("goodsName") or "").strip(),
                "goods_num": _as_int(item.get("goodsNum"), default=0),
                "goods_url": str(item.get("goodsUrl") or "").strip(),
                "is_signed": index <= signed_count,
                "is_gain": bool(item.get("isGain", False)),
            }
        )

    return {
        "is_signed": bool(data.get("isSigIn", False)),
        "signed_count": signed_count,
        "goods": goods,
    }


def perform_sign(profile: WuwaKujiequProfile) -> Dict[str, Any]:
    _ensure_profile_ready(profile)
    req_month = time.localtime().tm_mon
    query = urlencode(
        {
            "gameId": GAME_ID,
            "serverId": SERVER_ID,
            "roleId": profile.role_id,
            "userId": profile.user_id,
            "reqMonth": f"{req_month:02d}",
        }
    )
    response = _request_json(
        f"{SIGNIN_URL}?{query}",
        headers=_legacy_headers(profile),
        timeout=20,
        body=b"",
    )
    message = str(response.get("msg") or "").strip()
    code = _as_int(response.get("code"), default=0)
    is_already_signed = "请勿重复签到" in message
    success = is_already_signed or (code == 200 and ("请求成功" in message or bool(response.get("success"))))
    if not success:
        raise WuwaKujiequError(message or f"签到失败，错误码 {code}")
    status = "already" if is_already_signed else "success"
    return {"status": status, "message": message or ("请勿重复签到" if status == "already" else "签到成功")}


def fetch_sign_records(profile: WuwaKujiequProfile) -> List[Dict[str, Any]]:
    _ensure_profile_ready(profile)
    query = urlencode(
        {
            "gameId": GAME_ID,
            "serverId": SERVER_ID,
            "roleId": profile.role_id,
            "userId": profile.user_id,
        }
    )
    response = _request_json(
        f"{SIGNIN_QUERY_URL}?{query}",
        headers=_legacy_headers(profile),
        timeout=20,
        body=b"",
    )
    data = _expect_success(response, default_message="获取签到记录失败")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def get_devcode_header() -> str:
    global _devcode_cache
    if _devcode_cache:
        return _devcode_cache
    _devcode_cache = f"{get_public_ip()}, {APP_USER_AGENT}"
    return _devcode_cache


def get_public_ip() -> str:
    global _public_ip_cache, _public_ip_cache_time
    now = time.time()
    if _public_ip_cache and now - _public_ip_cache_time <= PUBLIC_IP_CACHE_SECONDS:
        return _public_ip_cache

    for url in PUBLIC_IP_URLS:
        try:
            body = _request_text(url, headers={}, timeout=5, method="GET")
        except WuwaKujiequError:
            continue
        ip_value = _extract_ip_text(body)
        if ip_value:
            _public_ip_cache = ip_value
            _public_ip_cache_time = now
            return ip_value

    _public_ip_cache = "127.127.127.127"
    _public_ip_cache_time = now
    return _public_ip_cache


def _get_access_token(profile: WuwaKujiequProfile) -> str:
    cache_key = str(profile.user_id or "")
    if cache_key and cache_key in _access_token_cache:
        return _access_token_cache[cache_key]

    query = urlencode(
        {
            "serverId": SERVER_ID,
            "roleId": profile.role_id,
            "userId": profile.user_id,
        }
    )
    response = _request_json(
        f"{ROLE_ACCESS_TOKEN_URL}?{query}",
        headers={
            "source": "android",
            "B-At": "",
            "token": profile.token,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=5,
        body=b"",
    )
    payload = _expect_success(response, default_message="获取 B-At 令牌失败")
    payload = _parse_embedded_json(payload)
    if not isinstance(payload, dict):
        raise WuwaKujiequError("B-At 令牌返回格式异常。")
    access_token = str(payload.get("accessToken") or "").strip()
    if not access_token:
        raise WuwaKujiequError("B-At 令牌为空。")
    if cache_key:
        _access_token_cache[cache_key] = access_token
    return access_token


def _request_json(url: str, headers: Dict[str, str], timeout: int, body: Optional[bytes], method: str = "POST") -> Dict[str, Any]:
    text = _request_text(url, headers=headers, timeout=timeout, body=body, method=method)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WuwaKujiequError(f"接口返回不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise WuwaKujiequError("接口返回不是对象结构。")
    return data


def _request_text(
    url: str,
    headers: Dict[str, str],
    timeout: int,
    body: Optional[bytes] = None,
    method: str = "POST",
) -> str:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        try:
            body_text = exc.read().decode("utf-8")
        except Exception:
            body_text = ""
        message = body_text or exc.reason or str(exc)
        raise WuwaKujiequError(f"HTTP {exc.code}: {message}") from exc
    except URLError as exc:
        raise WuwaKujiequError(f"网络连接失败：{exc.reason}") from exc
    except Exception as exc:
        raise WuwaKujiequError(str(exc)) from exc


def _expect_success(response: Dict[str, Any], default_message: str) -> Any:
    code = _as_int(response.get("code"), default=0)
    if code != 200:
        message = str(response.get("msg") or "").strip()
        if message == "登录已过期，请重新登录":
            raise WuwaKujiequError("检测到 Token 已过期，请重新登录或更新 Token。")
        raise WuwaKujiequError(message or default_message)
    return _parse_embedded_json(response.get("data"))


def _parse_embedded_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text == "null":
        return None
    if text[0] not in "[{\"":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _legacy_headers(profile: WuwaKujiequProfile) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": APP_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "pragma": "no-cache",
        "cache-control": "no-cache",
        "sec-ch-ua": LEGACY_SEC_CH_UA,
        "source": _source_name(profile.is_web),
        "devcode": profile.did,
        "sec-ch-ua-platform": LEGACY_SEC_CH_UA_PLATFORM,
        "origin": "https://web-static.kurobbs.com",
        "x-requested-with": "com.kurogame.kjq",
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "priority": "u=1, i",
    }
    if profile.token:
        headers["token"] = profile.token
    return headers


def _base_headers(
    *,
    include_token: bool,
    profile: WuwaKujiequProfile,
    access_token: str,
    source_override: Optional[str] = None,
) -> Dict[str, str]:
    headers = {
        "User-Agent": APP_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Source": source_override or _source_name(profile.is_web),
        "Devcode": get_devcode_header(),
        "B-At": access_token,
        "Did": profile.did,
    }
    if include_token and profile.token:
        headers["token"] = profile.token
    return headers


def _encode_multipart_form(fields: Dict[str, Any]) -> Tuple[str, bytes]:
    boundary = f"----GameAuto{uuid.uuid4().hex}"
    parts: List[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def _normalize_detail(detail: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(detail, dict):
        return None
    cur = _as_int_or_none(detail.get("cur"))
    total = _as_int_or_none(detail.get("total"))
    value = str(detail.get("value") or "").strip()
    if not value and cur is not None and total is not None:
        value = f"{cur}/{total}"
    return {
        "name": str(detail.get("name") or detail.get("key") or "").strip(),
        "key": str(detail.get("key") or "").strip(),
        "value": value,
        "status": _as_int(detail.get("status"), default=0),
        "cur": cur,
        "total": total,
        "refresh_time": _as_int(detail.get("refreshTimeStamp"), default=0),
        "expire_time": _as_int(detail.get("expireTimeStamp"), default=0),
    }


def _normalize_box_list(items: Any) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "name": str(item.get("location") or item.get("name") or item.get("boxName") or "").strip(),
                "num": _as_int(item.get("num"), default=0),
            }
        )
    return [item for item in result if item["name"] or item["num"]]


def _pick_role(roles: List[Dict[str, str]], selected_role_id: Optional[str]) -> Optional[Dict[str, str]]:
    selected_role_id = str(selected_role_id or "").strip()
    if selected_role_id:
        for role in roles:
            if role.get("role_id") == selected_role_id:
                return role
    return roles[0] if roles else None


def _ensure_profile_ready(profile: WuwaKujiequProfile) -> None:
    if not profile.token:
        raise WuwaKujiequError("当前未保存库街区 token。")
    if not profile.did:
        raise WuwaKujiequError("当前缺少 DID，请重新登录或重新保存 token。")
    if not profile.role_id or not profile.user_id:
        raise WuwaKujiequError("请先选择一个鸣潮角色。")


def _source_name(is_web: bool) -> str:
    return "h5" if is_web else "android"


def _extract_ip_text(body: str) -> str:
    text = str(body or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text.strip('"')
    if isinstance(payload, dict):
        return str(payload.get("ip") or payload.get("origin") or "").strip()
    if isinstance(payload, str):
        return payload.strip()
    return text


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
