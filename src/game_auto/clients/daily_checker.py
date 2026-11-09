"""每日任务完成情况检测模块。

通过各游戏的官方/社区 API 查询每日任务完成状态，
用于脚本执行后的成功判定。

覆盖游戏：
- 原神 (Genshin Impact) — HoYoLab 实时便笺 API
- 崩坏：星穹铁道 (Honkai: Star Rail) — HoYoLab 实时便笺 API
- 绝区零 (Zenless Zone Zero) — HoYoLab 实时便笺 API
- 鸣潮 (Wuthering Waves) — 库街区每日数据 API
- 无限暖暖 (Infinity Nikki) — Pearpal notebook API
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .mihoyo_sign import (
    get_bound_accounts,
    tidy_cookie,
    GAME_DEFINITIONS as SIGN_GAME_DEFS,
    enrich_cookie_for_record_api,
)
from .wuwa_kujiequ import (
    WuwaKujiequProfile,
    fetch_daily_data,
    fetch_base_data,
)
from ..services.credential_store import load_wuwa_kujiequ_profile

LOGGER = logging.getLogger("GameAutomation")



def _extract_cookie_fields(cookie: str) -> Dict[str, str]:
    """从 cookie 字符串中提取所有字段。"""
    fields = {}
    for part in cookie.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k.strip()] = v.strip()
    return fields


def _hoyo_record_ds(query: str = "", body: str = "") -> str:
    """生成国服游戏记录 API 的 DS 签名（TeyvatGuide X4 格式）。"""
    from hashlib import md5 as _md5
    salt = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"
    t = str(int(time.time()))
    r = str(random.randint(100000, 200000))
    raw = f"salt={salt}&t={t}&r={r}&b={body}&q={query}"
    return f"{t},{r},{_md5(raw.encode()).hexdigest()}"


# ---------------------------------------------------------------------------
# 米游社设备指纹绑定（应对 1034/10041 风控）
#
# 方案来源：gsuid_core BindDevice（早柚核心官方文档，实测长期有效）：
# 请求头携带与登录该 cookie 的真实手机一致的 x-rpc-device_id / x-rpc-device_fp，
# UA 型号与设备一致。用户从 copy-device-info APK 复制设备参数 dict 放进
# app_config.json 的 mihoyo_sign.device，本模块用这些参数调官方 getFp 换取
# device_fp 并缓存复用。gsuid_core 对应实现：
# deal_fp / generate_fp / device_login_and_save（base_request.py）。
# ---------------------------------------------------------------------------

_MHY_GET_FP_URL = "https://public-data-api.mihoyo.com/device-fp/api/getFp"
_MHY_DEVICE_LOGIN_URL = "https://bbs-api.miyoushe.com/apihub/api/deviceLogin"
_MHY_SAVE_DEVICE_URL = "https://bbs-api.miyoushe.com/apihub/api/saveDevice"
_APP_VERSION = "2.106.2"


def recognize_device_input(raw: Any) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """识别用户粘贴的原始设备字段集合（自动兼容两种来源）。

    支持两种格式：
    1. copy-device-info APK 复制的参数 dict：deviceModel / androidVersion /
       deviceFingerprint / deviceName / deviceBoard / deviceProduct / oaid
       （deviceFingerprint 是安卓 build 指纹，米游社 fp 由程序调 getFp 换取）
    2. 抓包得到的 {"fp": ..., "device_id": ...}（fp 即米游社 device_fp，
       可带 device_info/deviceInfo）

    返回 (device, info)：device 为归一化后的字段 dict（None 表示无法识别），
    info 含 format（"apk_params" / "captured_fp" / None）、fields（识别到的
    原始字段名）、error（失败原因）。
    """
    info: Dict[str, Any] = {"format": None, "fields": [], "error": ""}
    if isinstance(raw, str):
        cleaned = raw.strip().strip(";；,，")
        if not cleaned:
            info["error"] = "内容为空"
            return None, info
        try:
            raw = json.loads(cleaned)
        except (ValueError, TypeError):
            info["error"] = "不是合法 JSON（请粘贴 APK 复制的原始字段集合）"
            return None, info
    if not isinstance(raw, dict):
        info["error"] = "内容不是 JSON 对象"
        return None, info

    # 键名归一（大小写/下划线别名），按 gsuid_core deal_fp 的两套键位兼容
    lowered = {str(k).strip().lower(): (str(v).strip() if v is not None else "") for k, v in raw.items()}

    def pick(*names: str) -> str:
        for n in names:
            val = lowered.get(n.lower(), "")
            if val:
                return val
        return ""

    recognized: Dict[str, str] = {
        "fp": pick("fp", "deviceFp", "device_fp"),
        "device_id": pick("device_id", "deviceId"),
        "deviceModel": pick("deviceModel", "device_model", "model"),
        "androidVersion": pick("androidVersion", "android_version"),
        "deviceFingerprint": pick("deviceFingerprint", "device_info", "deviceInfo"),
        "deviceName": pick("deviceName", "device_name"),
        "deviceBoard": pick("deviceBoard", "device_board", "board"),
        "deviceProduct": pick("deviceProduct", "device_product", "productName", "product"),
        "oaid": pick("oaid"),
    }
    device: Dict[str, Any] = {k: v for k, v in recognized.items() if v}
    info["fields"] = [k for k, v in raw.items() if str(k).strip() and v not in (None, "")]

    # 缓存键透传（写回配置里的 fp/device_id/seed/registered，重复粘贴不丢）
    for cache_key in ("seed_id", "seed_time"):
        if raw.get(cache_key):
            device[cache_key] = raw[cache_key]
    if "registered" in raw:
        device["registered"] = bool(raw.get("registered"))

    if device.get("fp"):
        info["format"] = "captured_fp"
        return device, info
    if device.get("deviceFingerprint"):
        info["format"] = "apk_params"
        return device, info
    info["error"] = "未识别到有效字段：需要 deviceFingerprint（APK 参数）或 fp（抓包设备指纹）"
    return None, info


def _normalize_device_config(raw: Any) -> Optional[Dict[str, Any]]:
    """把 mihoyo_sign.device 配置规整成 dict；无法识别返回 None。"""
    device, _info = recognize_device_input(raw)
    return device


def _device_model(device: Dict[str, Any]) -> str:
    return str(device.get("deviceModel") or "").strip() or "M2012K11AC"


def _device_android(device: Dict[str, Any]) -> str:
    return str(device.get("androidVersion") or "").strip() or "13"


def _android_sdk_level(android_version: str) -> str:
    try:
        return str(20 + int(android_version.split(".")[0]))
    except (ValueError, TypeError):
        return "34"


def _build_device_ua(device: Dict[str, Any]) -> str:
    """仿米游社安卓 App 的 UA，型号取自真机参数。"""
    return (
        "Mozilla/5.0 (Linux; Android {android}; {model} Build/UKQ1.230917.001; wv)"
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0.6478.133"
        "Mobile Safari/537.36 miHoYoBBS/{version}"
    ).format(
        android=_device_android(device),
        model=_device_model(device),
        version=_APP_VERSION,
    )


def _random_device_fp() -> str:
    return "".join(random.choices("0123456789abcdef", k=13))


def _build_fp_ext_fields(device: Dict[str, Any]) -> str:
    """构造 getFp 的 ext_fields 设备画像，模板沿用 gsuid_core 实测版本。"""
    fingerprint = str(device.get("deviceFingerprint")).strip()
    brand = fingerprint.split("/")[0] or _device_model(device)
    android = _device_android(device)
    now_ms = int(time.time() * 1000)
    random_kb = random.randint(400000, 600000)
    ext = {
        "proxyStatus": 0, "isRoot": 1, "romCapacity": "512",
        "deviceName": str(device.get("deviceName") or "私人手机"),
        "productName": str(device.get("deviceProduct") or _device_model(device)),
        "romRemain": "491", "hostname": "dg02-pool06-kvm82",
        "screenSize": "1264x2640", "isTablet": 0,
        "aaid": uuid.uuid4().hex, "model": _device_model(device),
        "brand": brand, "hardware": "qcom",
        "deviceType": str(device.get("deviceName") or brand),
        "devId": "REL", "serialNumber": "unknown",
        "sdCapacity": random_kb, "buildTime": "1717740969000", "buildUser": "root",
        "simState": 5, "ramRemain": random.randint(150000, 300000),
        "appUpdateTimeDiff": now_ms, "deviceInfo": fingerprint,
        "vaid": uuid.uuid4().hex, "buildType": "user",
        "sdkVersion": _android_sdk_level(android),
        "ui_mode": "UI_MODE_TYPE_NORMAL", "isMockLocation": 0,
        "cpuType": "arm64-v8a", "isAirMode": 0, "ringMode": 1, "chargeStatus": 1,
        "manufacturer": brand, "emulatorStatus": 0, "appMemory": "512",
        "osVersion": android, "vendor": "中国联通",
        "accelerometer": "-1.3004991x6.38764x7.19103",
        "sdRemain": random.randint(150000, 300000), "buildTags": "release-keys",
        "packageName": "com.mihoyo.hyperion", "networkType": "WiFi",
        "oaid": str(device.get("oaid") or "").strip() or uuid.uuid4().hex,
        "debugStatus": 1, "ramCapacity": str(random_kb),
        "magnetometer": "27.1084x-48.5804x-24.8758",
        "display": f"{_device_model(device)}_{android}.0.0.810(CN01)",
        "appInstallTimeDiff": now_ms, "packageVersion": "2.20.2",
        "gyroscope": "-0.02543317x0.005725792x0.003195791",
        "batteryStatus": 50, "hasKeyboard": 0,
        "board": str(device.get("deviceBoard") or brand),
    }
    return json.dumps(ext, separators=(",", ":"), ensure_ascii=False)


def _fetch_device_fp(device: Dict[str, Any], device_id: str, seed_id: str, seed_time: str) -> Optional[str]:
    """用真机设备参数调官方 getFp 换取 device_fp，失败返回 None。"""
    from urllib.request import Request, urlopen

    body = {
        "device_id": device_id,
        "seed_id": seed_id,
        "platform": "2",
        "seed_time": str(seed_time),
        "ext_fields": _build_fp_ext_fields(device),
        "app_name": "bbs_cn",
        "bbs_device_id": device_id,
        "device_fp": _random_device_fp(),
    }
    headers = {
        "user-agent": _build_device_ua(device),
        "x-rpc-app_version": _APP_VERSION,
        "x-rpc-client_type": "5",
        "x-requested-with": "com.mihoyo.hyperion",
        "referer": "https://webstatic.mihoyo.com",
        "origin": "https://webstatic.mihoyo.com",
    }
    req = Request(_MHY_GET_FP_URL, data=json.dumps(body).encode("utf-8"),
                  headers=headers, method="POST")
    with urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    payload = data.get("data") or {}
    if isinstance(payload, dict) and payload.get("code") == 200:
        fp = str(payload.get("device_fp") or "").strip()
        if fp:
            return fp
    LOGGER.warning("[设备绑定] getFp 未返回有效 device_fp：%s", str(data.get("message", data))[:120])
    return None


def _register_device_to_account(cookie: str, device: Dict[str, Any],
                                device_id: str, device_fp: str) -> bool:
    """best-effort：把设备注册到账号（apihub deviceLogin + saveDevice）。

    对应 gsuid_core 的 device_login_and_save。失败不影响主流程。
    """
    from urllib.request import Request, urlopen
    from hashlib import md5

    fields = _extract_cookie_fields(cookie)
    stoken = fields.get("stoken") or fields.get("stoken_v2") or ""
    stuid = fields.get("stuid") or fields.get("stuid_v2") or ""
    mid = fields.get("mid") or fields.get("mid_v2") or ""
    if not stoken or not stuid:
        LOGGER.info("[设备绑定] Cookie 缺少 stoken/stuid，跳过设备注册")
        return False
    try:
        from genshin.constants import DS_SALT
        passport_salt = DS_SALT.get("cn_passport", "JwYDpKvLj6MrMqqYU6jTKF17KNO2PXoS")
    except Exception:
        passport_salt = "JwYDpKvLj6MrMqqYU6jTKF17KNO2PXoS"

    fingerprint = str(device.get("deviceFingerprint")).strip()
    brand = fingerprint.split("/")[0] or _device_model(device)
    body = {
        "app_version": _APP_VERSION,
        "device_id": device_id,
        "device_name": f"{brand} {_device_model(device)}",
        "os_version": _android_sdk_level(_device_android(device)),
        "platform": "Android",
        "registration_id": uuid.uuid4().hex[:19],
    }
    body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    t = str(int(time.time()))
    r = str(random.randint(100000, 200000))
    raw = f"salt={passport_salt}&t={t}&r={r}&b={body_str}&q="
    ds = f"{t},{r},{md5(raw.encode()).hexdigest()}"

    cookie_str = f"stoken={stoken};stuid={stuid};"
    if mid:
        cookie_str += f"mid={mid};"
    base_headers = {
        "user-agent": _build_device_ua(device),
        "x-rpc-app_version": _APP_VERSION,
        "x-rpc-client_type": "5",
        "x-requested-with": "com.mihoyo.hyperion",
        "referer": "https://app.mihoyo.com",
        "x-rpc-device_id": device_id,
        "x-rpc-device_fp": device_fp,
        "x-rpc-device_name": f"{brand} {_device_model(device)}",
        "x-rpc-device_model": _device_model(device),
        "x-rpc-csm_source": "myself",
        "ds": ds,
        "cookie": cookie_str,
    }
    ok = True
    for url in (_MHY_DEVICE_LOGIN_URL, _MHY_SAVE_DEVICE_URL):
        try:
            req = Request(url, data=body_str.encode("utf-8"),
                          headers={**base_headers, "content-type": "application/json"},
                          method="POST")
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("retcode") != 0:
                LOGGER.warning("[设备绑定] 设备注册失败 %s：%s", url.rsplit("/", 1)[-1],
                               str(data.get("message", ""))[:120])
                ok = False
        except Exception as exc:
            LOGGER.warning("[设备绑定] 设备注册请求异常：%s", str(exc)[:160])
            ok = False
    return ok


def _persist_device_cache(device: Dict[str, Any]) -> None:
    """把生成的 device_id/fp 等缓存写回 app_config.json，保证跨次运行稳定。

    2026-11-09 收敛：旧实现自管"读盘→解密→改→加密→tmp+replace"整条链，
    与 settings.manager 的缓存/其他写线程互踩丢更新（审计 P2-4）。现走
    manager.update_config 锁内原子写，加密/归一化由 manager 统一负责。
    """
    try:
        from ..settings import manager as _cm

        def _apply(cfg: Dict[str, Any]) -> None:
            section = cfg.get("mihoyo_sign")
            if not isinstance(section, dict):
                section = {}
                cfg["mihoyo_sign"] = section
            section["device"] = device

        _cm.update_config(_apply)
    except Exception as exc:
        LOGGER.warning("[设备绑定] 缓存写入 app_config.json 失败（不影响本次查询）：%s", str(exc)[:160])


def ensure_device_headers(mihoyo_cfg: Dict[str, Any],
                          cookie_for_registration: Optional[str] = None) -> Optional[Dict[str, str]]:
    """从 mihoyo_sign 配置得到设备绑定请求头；未配置返回 None（行为与旧版一致）。

    返回 dict 含 x-rpc-device_id / x-rpc-device_fp / user-agent（小写键，
    可直接 update 进请求头）。device_fp 只在首次生成，随后缓存进配置复用；
    提供 cookie_for_registration（含 stoken）时会顺带把设备注册到账号
    （best-effort，成功一次后不再重复）。
    """
    device = _normalize_device_config(mihoyo_cfg.get("device"))
    if device is None:
        return None

    cached_fp = str(device.get("fp") or "").strip()
    device_id = str(device.get("device_id") or "").strip()
    needs_persist = False
    if cached_fp and device_id:
        LOGGER.info("[设备绑定] 使用缓存的设备指纹（fp=%s…）", cached_fp[:8])
        fp = cached_fp
    elif cached_fp:
        # 抓包格式只给了 fp：补一个稳定 device_id，不再调 getFp
        device_id = str(uuid.uuid4()).lower()
        device["device_id"] = device_id
        fp = cached_fp
        needs_persist = True
        LOGGER.info("[设备绑定] 使用自带设备指纹（fp=%s…），已补充 device_id", fp[:8])
    else:
        device_id = device_id or str(uuid.uuid4()).lower()
        seed_id = str(uuid.uuid4()).lower()
        seed_time = str(int(time.time() * 1000))
        fp = None
        try:
            fp = _fetch_device_fp(device, device_id, seed_id, seed_time)
        except Exception as exc:
            LOGGER.warning("[设备绑定] getFp 请求异常，退回随机指纹：%s", str(exc)[:160])
        if fp:
            device.update({"fp": fp, "device_id": device_id,
                           "seed_id": seed_id, "seed_time": seed_time})
            LOGGER.info("[设备绑定] getFp 成功，设备指纹已缓存（fp=%s…）", fp[:8])
        else:
            fp = _random_device_fp()
            device.update({"fp": fp, "device_id": device_id})
            LOGGER.info("[设备绑定] 使用随机设备指纹（fp=%s…）", fp[:8])
        needs_persist = True

    if cookie_for_registration and not device.get("registered"):
        try:
            if _register_device_to_account(cookie_for_registration, device, device_id, fp):
                device["registered"] = True
                needs_persist = True
        except Exception as exc:
            LOGGER.warning("[设备绑定] 设备注册流程异常（不影响查询）：%s", str(exc)[:160])

    if needs_persist:
        _persist_device_cache(device)

    return {
        "x-rpc-device_id": device_id,
        "x-rpc-device_fp": fp,
        "user-agent": _build_device_ua(device),
    }


def _query_hoyo_daily_note(cookie: str, uid: int, server: str, game: str,
                           device_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """直接调用米游社实时便笺 API（TeyvatGuide 方式：account_id + cookie_token）。

    device_headers 为设备绑定请求头（见 ensure_device_headers），配置了
    mihoyo_sign.device 时携带，可显著降低 1034/10041 风控触发概率。

    2026-11-04：签到链路 `_ensure_fresh_cookie_token` 会用 stoken 刷新
    cookie_token，但刷新结果只用于当次签到、没有回写 app_config；每日检查每次
    `load(force=True)` 读到的还是旧 token → 20:15:41 `[星铁] Cookie 已过期`。
    这里在真的遇到 10001 时现场刷新并重试一次：不写配置、不改正常路径，
    只有 token 确实过期才多发一次请求。
    """
    try:
        return _query_hoyo_daily_note_once(cookie, uid, server, game, device_headers)
    except RuntimeError as exc:
        if "Cookie 已过期" not in str(exc):
            raise
        from .mihoyo_sign import _ensure_fresh_cookie_token

        fresh = _ensure_fresh_cookie_token(cookie)
        if not fresh or fresh == cookie:
            LOGGER.warning(
                "[每日检查] %s 便笺 Cookie 已过期，stoken 刷新未生效，需重新登录米游社", game
            )
            raise
        LOGGER.info("[每日检查] %s 便笺 Cookie 已过期，已用 stoken 刷新 cookie_token 并重试", game)
        return _query_hoyo_daily_note_once(fresh, uid, server, game, device_headers)


def _query_hoyo_daily_note_once(cookie: str, uid: int, server: str, game: str,
                                device_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """单次便笺请求；过期重试的编排在 `_query_hoyo_daily_note`。"""
    from urllib.request import Request, urlopen

    fields = _extract_cookie_fields(cookie)
    account_id = fields.get("account_id") or fields.get("stuid") or ""
    cookie_token = fields.get("cookie_token") or fields.get("cookie_token_v2") or ""
    if not account_id or not cookie_token:
        raise RuntimeError("缺少 account_id/cookie_token")

    query_str = f"role_id={uid}&server={server}"
    ds = _hoyo_record_ds(query=query_str)

    paths = {
        "genshin": "/game_record/app/genshin/api/dailyNote",
        "honkai_sr": "/game_record/app/hkrpg/api/note",
        "zzz": "/event/game_record_zzz/api/zzz/note",
    }
    path = paths.get(game)
    if not path:
        raise RuntimeError(f"未知游戏: {game}")

    url = f"https://api-takumi-record.mihoyo.com{path}?{query_str}"
    ck = f"account_id={account_id};cookie_token={cookie_token}"
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) miHoYoBBS/2.106.2",
        "x-rpc-app_version": "2.106.2",
        "x-rpc-client_type": "5",
        "x-requested-with": "com.mihoyo.hyperion",
        "referer": "https://webstatic.mihoyo.com",
        "ds": ds,
        "cookie": ck,
    }
    if device_headers:
        headers.update(device_headers)

    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    retcode = data.get("retcode", -1)
    if retcode == 5003:
        raise RuntimeError("账号未在米游社开启「实时便笺」数据共享（请在米游社 App → 我的 → 设置 → 隐私设置中开启）")
    if retcode == 10001:
        raise RuntimeError("Cookie 已过期，请重新登录")
    if retcode == 10102:
        raise RuntimeError("数据不存在，请先在游戏内登录一次")
    if retcode in (1034, 10035, 10041):
        raise RuntimeError(
            f"触发米游社风控验证 (retcode={retcode})。"
            "可在 app_config.json 的 mihoyo_sign.device 配置真机设备指纹后重试"
        )
    if retcode != 0:
        raise RuntimeError(f"API 错误 (retcode={retcode}): {data.get('message', '')}")

    return data.get("data") or {}


def _query_genshin_notes(cookie: str, uid: int,
                         device_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """查询原神实时便笺。"""
    return _query_hoyo_daily_note(cookie, uid, "cn_gf01", "genshin", device_headers)


def _query_starrail_notes(cookie: str, uid: int,
                          device_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """查询星铁实时便笺。"""
    return _query_hoyo_daily_note(cookie, uid, "prod_gf_cn", "honkai_sr", device_headers)


def _query_zzz_notes(cookie: str, uid: int,
                     device_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """查询绝区零实时便笺。"""
    return _query_hoyo_daily_note(cookie, uid, "prod_gf_cn", "zzz", device_headers)


def _check_hoyo_accounts(cookie: str, game_key: str) -> List[Dict[str, Any]]:
    # 2026-11-04：旧口径把 stoken/stoken_v2/stuid/stuid_v2 四个全当必需，还用
    # `f not in cookie` 做子串判断（v2 变体本来就不一定存在），本机每天刷
    # 「缺少 stuid_v2」但 get_bound_accounts 照常拿到角色——纯误报，反而把后面
    # 真正的「Cookie 已过期」淹没。便笺查询真正需要的是账号标识 + cookie_token。
    fields = _extract_cookie_fields(cookie)
    has_id = any(fields.get(k) for k in (
        "account_id", "account_id_v2", "stuid", "stuid_v2", "ltuid", "ltuid_v2"))
    has_token = any(fields.get(k) for k in ("cookie_token", "cookie_token_v2"))
    if not has_id or not has_token:
        LOGGER.warning(
            "[每日检查] Cookie 缺少查询所需字段（账号标识/cookie_token），%s 可能查询失败",
            game_key,
        )
    return get_bound_accounts(cookie, game_key)


# ---------------------------------------------------------------------------
# 游戏每日状态数据结构
# ---------------------------------------------------------------------------

@dataclass
class DailyTaskItem:
    """单个每日任务项。"""
    name: str
    current: int
    total: int
    is_completed: bool

    @property
    def progress_percent(self) -> int:
        try:
            cur = int(self.current) if not isinstance(self.current, dict) else 0
            tot = int(self.total) if not isinstance(self.total, dict) else 1
        except (ValueError, TypeError):
            return 100 if self.is_completed else 0
        if tot <= 0:
            return 100 if self.is_completed else 0
        return int(cur / tot * 100)


@dataclass
class GameDailyStatus:
    """单个游戏的每日任务完成状态。"""
    game_key: str
    game_name: str
    uid: str
    nickname: str
    is_daily_completed: bool
    tasks: List[DailyTaskItem] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    errors: List[str] = field(default_factory=list)

    @property
    def progress_percent(self) -> int:
        if not self.tasks:
            return 100 if self.is_daily_completed else 0
        total_progress = sum(t.progress_percent for t in self.tasks)
        return int(total_progress / len(self.tasks))


# ---------------------------------------------------------------------------
# 原神
# ---------------------------------------------------------------------------

def check_genshin_daily(cookie: str, device_headers: Optional[Dict[str, str]] = None) -> List[GameDailyStatus]:
    """检查原神每日任务完成情况。"""
    accounts = _check_hoyo_accounts(cookie, "genshin")
    results: List[GameDailyStatus] = []
    for account in accounts:
        uid = account.get("uid", "")
        nickname = account.get("nickname", "")
        try:
            result = _query_genshin_notes(cookie, int(uid), device_headers)
            finished = int(result.get("finished_task_num") or 0)
            total = int(result.get("total_task_num") or 4)
            claimed = bool(result.get("is_extra_task_reward_received"))
            tasks: List[DailyTaskItem] = []
            tasks.append(DailyTaskItem(
                name="每日委托",
                current=finished,
                total=total,
                is_completed=claimed or finished >= total,
            ))
            results.append(GameDailyStatus(
                game_key="genshin", game_name="原神", uid=uid, nickname=nickname,
                is_daily_completed=claimed or finished >= total, tasks=tasks,
                extra={"resin": int(result.get("current_resin") or 0),
                       "max_resin": int(result.get("max_resin") or 160)},
                raw=result,
            ))
        except Exception as exc:
            msg = str(exc)
            LOGGER.error("[原神] %s", msg)
            results.append(GameDailyStatus(
                game_key="genshin", game_name="原神", uid=uid, nickname=nickname,
                is_daily_completed=False, errors=[msg],
            ))
    return results


# ---------------------------------------------------------------------------
# 崩坏：星穹铁道
# ---------------------------------------------------------------------------

def check_hsr_daily(cookie: str, device_headers: Optional[Dict[str, str]] = None) -> List[GameDailyStatus]:
    """检查崩坏：星穹铁道每日任务完成情况。"""
    accounts = _check_hoyo_accounts(cookie, "honkai_sr")
    results: List[GameDailyStatus] = []
    for account in accounts:
        uid = account.get("uid", "")
        nickname = account.get("nickname", "")
        try:
            result = _query_starrail_notes(cookie, int(uid), device_headers)
            score = int(result.get("current_train_score") or 0)
            max_score = int(result.get("max_train_score") or 500)
            tasks: List[DailyTaskItem] = []
            tasks.append(DailyTaskItem(
                name="每日实训",
                current=score,
                total=max_score,
                is_completed=score >= max_score,
            ))
            results.append(GameDailyStatus(
                game_key="honkai_sr", game_name="崩坏：星穹铁道", uid=uid, nickname=nickname,
                is_daily_completed=score >= max_score, tasks=tasks,
                extra={"stamina": int(result.get("current_stamina") or 0),
                       "max_stamina": int(result.get("max_stamina") or 240)},
                raw=result,
            ))
        except Exception as exc:
            msg = str(exc)
            LOGGER.error("[星铁] %s", msg)
            results.append(GameDailyStatus(
                game_key="honkai_sr", game_name="崩坏：星穹铁道", uid=uid, nickname=nickname,
                is_daily_completed=False, errors=[msg],
            ))
    return results


# ---------------------------------------------------------------------------
# 绝区零
# ---------------------------------------------------------------------------

def check_zzz_daily(cookie: str, device_headers: Optional[Dict[str, str]] = None) -> List[GameDailyStatus]:
    """检查绝区零每日任务完成情况。"""
    accounts = _check_hoyo_accounts(cookie, "zzz")
    results: List[GameDailyStatus] = []
    for account in accounts:
        uid = account.get("uid", "")
        nickname = account.get("nickname", "")
        try:
            result = _query_zzz_notes(cookie, int(uid), device_headers)
            engagement = result.get("engagement") or {}
            battery = result.get("battery_charge") or {}
            eng_current = int(engagement.get("current") or 0) if isinstance(engagement, dict) else 0
            eng_max = int(engagement.get("max") or 400) if isinstance(engagement, dict) else 400
            bat_current = int(battery.get("current") or 0) if isinstance(battery, dict) else 0
            bat_max = int(battery.get("max") or 240) if isinstance(battery, dict) else 240
            tasks: List[DailyTaskItem] = []
            tasks.append(DailyTaskItem(
                name="活跃度",
                current=eng_current,
                total=eng_max,
                is_completed=eng_current >= eng_max,
            ))
            results.append(GameDailyStatus(
                game_key="zzz", game_name="绝区零", uid=uid, nickname=nickname,
                is_daily_completed=eng_current >= eng_max, tasks=tasks,
                extra={"energy": bat_current, "max_energy": bat_max},
                raw=result,
            ))
        except Exception as exc:
            msg = str(exc)
            LOGGER.error("[绝区零] %s", msg)
            results.append(GameDailyStatus(
                game_key="zzz", game_name="绝区零", uid=uid, nickname=nickname,
                is_daily_completed=False, errors=[msg],
            ))
    return results


# ---------------------------------------------------------------------------
# 鸣潮
# ---------------------------------------------------------------------------

def check_wuwa_daily(profile: WuwaKujiequProfile) -> GameDailyStatus:
    """检查鸣潮每日任务完成情况。"""
    try:
        daily_data = fetch_daily_data(profile)
        base_data = fetch_base_data(profile)
        tasks: List[DailyTaskItem] = []

        def _detail_progress(detail: Any) -> tuple[int, int]:
            if not isinstance(detail, dict):
                return 0, 0
            current = _safe_int(detail.get("cur"), 0)
            total_value = _safe_int(detail.get("total"), 0)
            if total_value <= 0:
                value = str(detail.get("value") or "")
                if "/" in value:
                    left, right = value.split("/", 1)
                    current = _safe_int(left.strip(), current)
                    total_value = _safe_int(right.strip(), total_value)
            return current, total_value

        liveness = daily_data.get("liveness") or {}
        completed, total = _detail_progress(liveness)
        tasks.append(DailyTaskItem(
            name="今日活跃", current=completed, total=total, is_completed=completed >= total if total else False,
        ))

        def _append_detail_task(name: str, detail: Any) -> None:
            cur, tot = _detail_progress(detail)
            if tot > 0:
                tasks.append(DailyTaskItem(
                    name=name, current=cur, total=tot, is_completed=cur >= tot,
                ))

        # 资源/周本详情上任务列表（与 WuwaView 每日数据卡片同一批字段），
        # 仅在有 total 时添加，避免 0/0 占位噪音。
        _append_detail_task("结晶波片", daily_data.get("energy"))
        _append_detail_task("结晶单质", daily_data.get("store_energy"))
        _append_detail_task("战歌重奏", daily_data.get("weekly"))
        _append_detail_task("千道门扉的异想", daily_data.get("weekly_rouge"))
        _append_detail_task("逆境深塔", daily_data.get("tower"))
        _append_detail_task("冥歌海墟", daily_data.get("slash_tower"))

        energy = daily_data.get("energy") or {}
        current_energy, max_energy = _detail_progress(energy)
        if max_energy <= 0:
            current_energy = base_data.get("energy", 0) or 0
            max_energy = base_data.get("max_energy", 0) or 240
        weekly = daily_data.get("weekly") or {}
        weekly_current, weekly_total = _detail_progress(weekly)
        LOGGER.info("[鸣潮] 每日检查成功：%s", profile.role_name)
        return GameDailyStatus(
            game_key="wuwa", game_name="鸣潮", uid=str(profile.role_id), nickname=profile.role_name,
            is_daily_completed=completed >= total if total else False, tasks=tasks,
            extra={
                "energy": current_energy,
                "max_energy": max_energy,
                "active_days": base_data.get("active_days", 0),
                "liveness_int": completed,
                "liveness_max": total,
                "liveness_data": liveness,
                "weekly": {
                    "current": weekly_current,
                    "max": weekly_total,
                    "detail": weekly,
                },
            },
            raw={"daily": daily_data, "base": base_data},
        )
    except Exception as exc:
        LOGGER.error("[鸣潮] 检查失败: %s", exc, exc_info=True)
        return GameDailyStatus(
            game_key="wuwa", game_name="鸣潮", uid=str(profile.role_id), nickname=profile.role_name,
            is_daily_completed=False, errors=[str(exc)],
        )


# 库街区活跃度由服务器在「进过游戏」后结算，脚本刚退出时立即查询经常还是
# 0/100（2026-11-01 实跑：ok-ww 11:53:03 退出，11:53:26 刷新缓存后查到 0/100，
# 13:30 再查已是 100/100，中间只打了几把副本）。fetch_daily_data 本身每次都会
# 先调 refresh_daily_data 刷缓存，所以问题不在缓存，而在结算需要时间——
# 这里在拿到 0 时等一会儿重查，而不是直接把 0 落库标 stale。
WUWA_STALE_RETRY_DELAYS: Tuple[float, ...] = (8.0, 20.0)


def _wuwa_liveness_is_stale(status: GameDailyStatus) -> bool:
    """今日活跃为 0（且 total>0）时视为还没结算，需要重查。"""
    for task in status.tasks:
        if task.name == "今日活跃" and task.total > 0 and task.current <= 0:
            return True
    return False


def check_wuwa_daily_with_retry(profile: WuwaKujiequProfile) -> GameDailyStatus:
    """查鸣潮每日数据；活跃度仍是 0 时等几秒重查，避免把「未结算」当「0 活跃」。

    最多 1 + len(WUWA_STALE_RETRY_DELAYS) 次，全部为 0 才原样返回（上层照旧标 stale）。
    重查抛错时保留上一次能用的结果，不让整条链路变成 errors。
    """
    status = check_wuwa_daily(profile)
    total_attempts = len(WUWA_STALE_RETRY_DELAYS) + 1
    for attempt, delay in enumerate(WUWA_STALE_RETRY_DELAYS, start=2):
        if not _wuwa_liveness_is_stale(status):
            break
        LOGGER.info(
            "[鸣潮] 今日活跃仍是 0，%d 秒后重查（第 %d/%d 次，等库街区结算）",
            int(delay), attempt, total_attempts,
        )
        time.sleep(delay)
        try:
            retry_status = check_wuwa_daily(profile)
        except Exception as exc:  # noqa: BLE001 - 重查失败沿用上一次结果
            LOGGER.warning("[鸣潮] 活跃度重查失败，沿用上一次结果: %s", exc)
            break
        if retry_status.errors and not status.errors:
            LOGGER.warning("[鸣潮] 活跃度重查无数据，沿用上一次结果: %s", retry_status.errors)
            break
        status = retry_status
    return status


# ---------------------------------------------------------------------------
# 塔吉多 / 异环（游戏侧 roleHome 数值 + 游戏签到）
# ---------------------------------------------------------------------------

# 异环每日活跃度上限（塔吉多 roleHome 的 dayvalue；登录 20 点 + 日常累积，满 100 领全部奖励）
NTE_DAILY_ACTIVITY_MAX = 100


def check_tajiduo_daily(
    token: str,
    deviceid: str,
    uid: str = "0",
    phone: str = "",
    password: str = "",
) -> GameDailyStatus:
    """检查塔吉多（异环）每日任务完成情况。

    复用**签到用的 token / deviceid**，不需要额外凭据。数据两路：
    - 游戏侧：roleHome（H5 游戏名片接口）的 dayvalue（每日活跃度）与
      本性像素/都市活力——2026-10-24 接入，判定口径 dayvalue >= 100 → 当日完成；
    - 游戏签到状态：awapi 的 signin 接口。

    2026-10-24 用户拍板：社区任务（点赞/浏览/分享）不再查询与落库
    （恒 pending 无信息量）；getUserTasks 仍供签到流程内部使用。
    """
    try:
        from .tajiduo_sign import TajiduoClient

        client = TajiduoClient(token=token, deviceid=deviceid, uid=uid,
                               phone=phone, password=password)
        tasks: List[DailyTaskItem] = []

        # 游戏侧数值（roleHome）：活跃度 dayvalue / 体力 / 城市体力
        role_id = ""
        try:
            cards = client.fetch_game_cards() or []
            if cards:
                role_id = str((cards[0].get("bindRoleInfo") or {}).get("roleId") or "")
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("[塔吉多] 游戏名片获取失败: %s", exc)
        if role_id:
            try:
                home = client.fetch_role_home(role_id) or {}

                def _num(key: str) -> int:
                    try:
                        return int(home.get(key) or 0)
                    except (TypeError, ValueError):
                        return 0

                dayvalue = _num("dayvalue")
                # 2026-11-05 事故：roleHome 响应无任何日期归属字段，且实测三个数值
                # 字段各自滞后、互相矛盾（同一次查询里 stamina 停在副本消耗前、
                # dayvalue 与游戏内实时活跃度双向不符）——dayvalue 不能再作为
                # 「当天完成」依据，永远只展示；完成判定回归 ok-nte 日志的日常领取。
                tasks.append(DailyTaskItem(
                    name="活跃度", current=min(dayvalue, NTE_DAILY_ACTIVITY_MAX),
                    total=NTE_DAILY_ACTIVITY_MAX,
                    is_completed=False,
                ))
                # 字段名沿用接口原义：H5 名片把 stamina 显示为「本性像素」，
                # citystamina 显示为「都市活力」。不要叫「体力/城市体力」，
                # 否则会把 360/360 误认成玩家口中的普通体力。
                # is_completed 只用于展示是否已满；这两项在 daily_completed
                # 里被显式排除，不参与每日完成判定。
                stamina_cur, stamina_max = _num("staminaValue"), _num("staminaMaxValue")
                tasks.append(DailyTaskItem(
                    name="本性像素", current=stamina_cur, total=stamina_max,
                    is_completed=stamina_max > 0 and stamina_cur >= stamina_max,
                ))
                city_cur, city_max = _num("citystaminaValue"), _num("citystaminaMaxValue")
                tasks.append(DailyTaskItem(
                    name="都市活力", current=city_cur, total=city_max,
                    is_completed=city_max > 0 and city_cur >= city_max,
                ))
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("[塔吉多] roleHome 查询失败: %s", exc)

        # 游戏签到状态：纳入每日完成判定
        game_states: List[Dict[str, Any]] = []
        try:
            game_states = client.game_signin_states() or []
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("[塔吉多] 游戏签到状态查询失败: %s", exc)

        for gs in game_states:
            gname = str(gs.get("game") or "游戏")
            tasks.append(DailyTaskItem(
                name=f"{gname}签到",
                current=1 if gs.get("signed") else 0,
                total=1,
                is_completed=bool(gs.get("signed")),
            ))

        # 完成语义（2026-11-05 收窄）：roleHome 三项数值（活跃度/本性像素/都市
        # 活力）均不参与「每日完成」，本接口已无法判定游戏内日常是否完成——
        # 恒 False；游戏内日常的完成判定走 ok-nte 日志的「日常领取」
        # （KEY_STATUS_TASKS），任务明细照常返回供展示。
        daily_completed = False

        # 金币任务进度（塔吉多社区金币，非游戏内货币）
        coin_state: Dict[str, Any] = {}
        try:
            coin_state = client.get_coin_state() or {}
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("[塔吉多] 金币状态查询失败: %s", exc)

        LOGGER.info("[塔吉多] 每日检查成功：%d 项任务，%s",
                    len(tasks), "全部完成" if daily_completed else "仍有未完成")
        # 角色信息取第一个绑定了角色的游戏卡片，用于界面展示
        nickname = ""
        for gs in game_states:
            if gs.get("role_name"):
                nickname = str(gs.get("role_name"))
                break

        return GameDailyStatus(
            game_key="nte", game_name="异环", uid=str(uid or ""), nickname=nickname,
            is_daily_completed=daily_completed, tasks=tasks,
            extra={"game_states": game_states,
                   "coin": {
                       "today": coin_state.get("todayGet", 0),
                       "today_total": coin_state.get("todayTotal", 0),
                       "total": coin_state.get("total", 0),
                   }},
            raw=None,
        )
    except Exception as exc:
        LOGGER.error("[塔吉多] 检查失败: %s", exc, exc_info=True)
        return GameDailyStatus(
            game_key="nte", game_name="异环", uid=str(uid or ""), nickname="",
            is_daily_completed=False, errors=[str(exc)],
        )


# ---------------------------------------------------------------------------
def _record_zzz_script_tasks(tracker) -> bool:
    """把 OneDragon 日志解析出的绝区零关键任务写入 tracker。

    「活跃度奖励」是纯状态型任务（日志只写"活跃度已满"/"未满"，无 X/Y 计数），
    写入空 extra，由 `KEY_STATUS_TASKS["zzz"]` 按 status 判定完成与否。
    """
    try:
        from ..services.zzz_daily_parser import parse_latest_zzz_log
        data = parse_latest_zzz_log()
    except Exception:
        LOGGER.debug("[每日检查] zzz 日志解析跳过", exc_info=True)
        return False
    if not data:
        return False
    written = 0
    for t in data.get("tasks") or []:
        name = str(t.get("task_name") or t.get("task_key") or "").strip()
        if not name:
            continue
        extra = t.get("extra") if isinstance(t.get("extra"), dict) else {}
        try:
            cur_i = int(extra.get("current", t.get("current", 0)) or 0)
            total_i = int(extra.get("total", t.get("total", 0)) or 0)
        except (TypeError, ValueError):
            cur_i, total_i = 0, 0
        tracker.record_task(
            game_key="zzz", game_name="绝区零",
            task_key=name, task_name=name,
            status=str(t.get("status") or "pending"), source="script",
            message=str(t.get("message") or ""),
            extra={"current": cur_i, "total": total_i},
        )
        written += 1
    if written:
        LOGGER.info("[每日检查] 绝区零脚本任务已落库: %d 项", written)
    return written > 0


def _record_whimbox_script_tasks(tracker) -> bool:
    """把 whimbox 日志解析出的关键任务（含朝夕心愿真实计数）写入 tracker。

    背景（2026-10-07）：Pearpal notebook 的「每日心愿」常年返回 0/500（该接口不刷新），
    而真实进度只存在于 whimbox 脚本日志里。此前该解析结果只在内存中被用于
    「失败复核」和「页面显示」，从不落库 → daily_progress 表里 nikki 只有那条
    恒 0 的 Pearpal 数据 → 完成率判定为 unknown → 统计显示"无有效数据"。
    """
    try:
        from ..services.whimbox_daily_parser import parse_latest_whimbox_log
        data = parse_latest_whimbox_log()
    except Exception:
        LOGGER.debug("[每日检查] whimbox 日志解析跳过", exc_info=True)
        return False
    if not data:
        return False
    written = 0
    for t in data.get("tasks") or []:
        name = str(t.get("task_name") or t.get("task_key") or "").strip()
        if not name:
            continue
        extra = t.get("extra") if isinstance(t.get("extra"), dict) else {}
        cur = extra.get("current", t.get("current", 0))
        total = extra.get("total", t.get("total", 0))
        try:
            cur_i, total_i = int(cur or 0), int(total or 0)
        except (TypeError, ValueError):
            cur_i, total_i = 0, 0
        tracker.record_task(
            game_key="nikki", game_name="无限暖暖",
            task_key=name, task_name=name,
            status=str(t.get("status") or "pending"), source="script",
            message=str(t.get("message") or ""),
            extra={"current": cur_i, "total": total_i},
        )
        written += 1
    if written:
        LOGGER.info("[每日检查] whimbox 脚本任务已落库: %d 项（含朝夕心愿计数）", written)
    return written > 0


# ---------------------------------------------------------------------------
# 统一接口
# ---------------------------------------------------------------------------

@dataclass
class DailyCheckResult:
    """所有游戏的每日检查结果汇总。"""
    games: List[GameDailyStatus] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)

    @property
    def all_completed(self) -> bool:
        """所有已检查游戏是否全部完成每日任务。"""
        if not self.games:
            return False
        return all(g.is_daily_completed for g in self.games)

    @property
    def completed_games(self) -> List[GameDailyStatus]:
        return [g for g in self.games if g.is_daily_completed]

    @property
    def failed_games(self) -> List[GameDailyStatus]:
        """未完成每日的游戏（2026-11-09 补：CLI --success-check 一直在引用
        这个不存在的属性，有游戏未完成时必抛 AttributeError，成功分支恰好
        掩盖了它）。"""
        return [g for g in self.games if not g.is_daily_completed]

    @property
    def progress_percent(self) -> int:
        if not self.games:
            return 0
        total = sum(g.progress_percent for g in self.games)
        return int(total / len(self.games))


def _load_app_config() -> Dict[str, Any]:
    """加载应用配置（直接读 JSON，避免相对导入问题）。自动解密敏感字段。"""
    import json as _json
    from pathlib import Path
    config_path = Path(__file__).resolve().parents[3] / "app_config.json"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = _json.load(f)
        # 解密敏感字段（token / cookie 等），兼容旧明文
        try:
            from ..settings.manager import decrypt_config
            return decrypt_config(raw)
        except Exception:
            try:
                from game_auto.settings.manager import decrypt_config as _dec
                return _dec(raw)
            except Exception:
                return raw
    except Exception:
        return {}


def _safe_int(v, default=0):
    if isinstance(v, dict):
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _build_missing_config_status(game_key: str, reason: str) -> GameDailyStatus:
    """构建配置缺失时的状态。"""
    from ..settings.defaults import GAME_KEYS
    return GameDailyStatus(
        game_key=game_key,
        game_name=GAME_KEYS.get(game_key, game_key),
        uid="",
        nickname="",
        is_daily_completed=False,
        errors=[reason],
    )


def _normalize_requested_game_keys(
    game_keys: Optional[List[str]],
) -> tuple[Optional[List[str]], List[str]]:
    """规范化请求的游戏 key 列表，支持别名。"""
    from ..settings import resolve_game_key
    if game_keys is None:
        return None, []
    normalized: List[str] = []
    unknown: List[str] = []
    for key in game_keys:
        gk = resolve_game_key(key)
        from ..settings.defaults import GAME_KEYS
        if gk in GAME_KEYS:
            normalized.append(gk)
        else:
            unknown.append(key)
    return normalized, unknown


def _check_endfield_via_skland(
    config: Dict[str, Any],
    result: "DailyCheckResult",
    tracker: Optional[Any],
    errors: List[str],
) -> bool:
    """协议终端不可用时，改用森空岛官方 API 查终末地每日进度。

    复用用户已填的森空岛**签到 token**，不需要 api_key / framework_token，
    因此不必额外到 end.shallow.ink 注册。

    返回 True 表示成功取到数据并已写入 result.games。
    """
    sk_cfg = config.get("skendfield_sign", {}) if isinstance(config, dict) else {}
    token = str(sk_cfg.get("token", "") if isinstance(sk_cfg, dict) else "").strip()
    server = str(sk_cfg.get("server", "cn") if isinstance(sk_cfg, dict) else "cn").strip() or "cn"

    if not token:
        return False

    try:
        from .skendfield_data import fetch_endfield_data
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[每日检查] 森空岛数据模块导入失败：%s", exc)
        return False

    try:
        data = fetch_endfield_data(token, server=server)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[每日检查] 终末地（森空岛）查询失败：%s", exc)
        errors.append(f"endfield_skland: {exc}")
        return False

    if not data.get("ok"):
        message = str(data.get("message") or "未知原因")
        LOGGER.info("[每日检查] 终末地（森空岛）未取到数据：%s", message)
        errors.append(f"endfield_skland: {message}")
        return False

    tasks: List[DailyTaskItem] = []
    daily_completed = False
    daily = data.get("daily")
    if daily and daily.get("total"):
        cur = _safe_int(daily.get("current", 0))
        total = _safe_int(daily.get("total", 0))
        daily_completed = cur >= total
        tasks.append(DailyTaskItem(name="日活跃", current=cur, total=total,
                                   is_completed=daily_completed))

    # 体力（detail.dungeon 的 curStamina/maxStamina，skendfield_data 已解析）
    extras = data.get("extras") or {}
    stamina_cur = _safe_int(extras.get("stamina", 0))
    stamina_max = _safe_int(extras.get("max_stamina", 0))
    if stamina_max > 0:
        tasks.append(DailyTaskItem(name="体力", current=stamina_cur, total=stamina_max,
                                   is_completed=stamina_cur >= stamina_max))

    uid = data.get("uid", "")
    status = GameDailyStatus(
        game_key="endfield",
        game_name="终末地",
        uid=uid,
        nickname=data.get("role_name", ""),
        is_daily_completed=daily_completed,
        tasks=tasks,
        extra={"source": "skland", "raw_keys": data.get("raw_keys", []),
               "stamina": stamina_cur, "max_stamina": stamina_max},
    )
    result.games.append(status)

    if tracker is not None:
        for task in status.tasks:
            tracker.record_task(
                game_key="endfield", game_name="终末地",
                task_key=task.name, task_name=task.name,
                status="success" if task.is_completed else "pending",
                source="community",
                message=f"{task.current}/{task.total}",
                uid=uid,
                extra={"current": task.current, "total": task.total},
            )
    return True


def check_games_progress(
    config: Optional[Dict[str, Any]] = None,
    game_keys: Optional[List[str]] = None,
    tracker=None,
) -> DailyCheckResult:
    """按游戏列表查询每日 API 进度。

    config 应包含 mihoyo_sign.cookie、wuwa_kujiequ.profile、tajiduo_sign 的
    token/deviceid 等字段（无限暖暖已不走 API，进度来自 whimbox 日志落库）。
    game_keys 为 None 时按默认配置检查全部可用游戏；
    传入列表时只查询指定游戏，并支持别名如 genshin/hsr/zzz/wuwa/nikki。
    tracker: DailyProgressTracker 实例，用于持久化记录结果。
    """
    if config is None:
        config = _load_app_config()

    result = DailyCheckResult()
    errors: List[str] = []
    requested_games, unknown_games = _normalize_requested_game_keys(game_keys)
    if unknown_games:
        LOGGER.warning("[每日检查] 忽略未知游戏 key：%s", "、".join(unknown_games))

    def _wants(game_key: str) -> bool:
        return requested_games is None or game_key in requested_games

    # --- 米哈游游戏（原神/星铁/绝区零） ---
    mihoyo_cfg = config.get("mihoyo_sign", {}) if isinstance(config, dict) else {}
    cookie = str(mihoyo_cfg.get("cookie", "") if isinstance(mihoyo_cfg, dict) else "").strip()
    stoken_cookie = str(mihoyo_cfg.get("stoken_cookie", "") if isinstance(mihoyo_cfg, dict) else "").strip()

    hoyo_games = [
        ("genshin", "原神", check_genshin_daily),
        ("honkai_sr", "崩坏：星穹铁道", check_hsr_daily),
        ("zzz", "绝区零", check_zzz_daily),
    ]

    base_cookie = stoken_cookie or cookie
    device_headers: Optional[Dict[str, str]] = None
    if base_cookie:
        enriched = enrich_cookie_for_record_api(base_cookie)
        record_cookie = enriched["cookie"]
        record_ready = enriched["record_ready"]

        # 设备指纹绑定：配置了 mihoyo_sign.device 时注入，降低便笺风控触发
        try:
            device_headers = ensure_device_headers(
                mihoyo_cfg, record_cookie if record_ready else None)
        except Exception as exc:
            LOGGER.warning("[设备绑定] 生成设备请求头失败，本次不携带：%s", str(exc)[:160])
            device_headers = None

        enabled_games = mihoyo_cfg.get("enabled_games", {}) if isinstance(mihoyo_cfg, dict) else {}
        for game_key, game_name, check_fn in hoyo_games:
            if not _wants(game_key):
                # 未勾选也要让脚本日志任务落库：zzz 的关键判定任务「活跃度奖励」
                # 只存在于 OneDragon 日志里，不落库会被判成"数据未刷新"
                if game_key == "zzz" and tracker is not None:
                    try:
                        _record_zzz_script_tasks(tracker)
                    except Exception:
                        LOGGER.debug("[每日检查] 绝区零脚本任务落库跳过", exc_info=True)
                continue
            if requested_games is None and isinstance(enabled_games, dict) and not enabled_games.get(game_key):
                if game_key == "zzz" and tracker is not None:
                    try:
                        _record_zzz_script_tasks(tracker)
                    except Exception:
                        LOGGER.debug("[每日检查] 绝区零脚本任务落库跳过", exc_info=True)
                continue
            if not record_ready:
                LOGGER.warning("[每日检查] %s 跳过：缺少 stoken，便笺 API 需密码/短信登录。", game_name)
                status = GameDailyStatus(
                    game_key=game_key, game_name=game_name, uid="", nickname="",
                    is_daily_completed=False,
                    errors=["缺少 stoken，无法查询实时便笺。请使用短信验证码登录米游社获取 stoken。"],
                )
                result.games.append(status)
                if tracker is not None:
                    tracker.record_task(
                        game_key, game_name, "daily_commissions", "每日委托", "skipped",
                        message="缺少 stoken，无法查询", uid="", source="community",
                    )
                continue
            try:
                statuses = check_fn(record_cookie, device_headers)
                result.games.extend(statuses)
                if tracker is not None:
                    for st in statuses:
                        task_status = "success" if st.is_daily_completed else "pending"
                        for task in st.tasks:
                            if task.is_completed:
                                tracker.record_task(
                                    game_key=st.game_key, game_name=st.game_name,
                                    task_key=task.name, task_name=task.name,
                                    status="success", source="community",
                                    message=f"{task.current}/{task.total}",
                                    uid=st.uid, nickname=st.nickname,
                                    extra={"current": task.current, "total": task.total},
                                )
                            else:
                                tracker.record_task(
                                    game_key=st.game_key, game_name=st.game_name,
                                    task_key=task.name, task_name=task.name,
                                    status="pending", source="community",
                                    message=f"{task.current}/{task.total}",
                                    uid=st.uid, nickname=st.nickname,
                                    extra={"current": task.current, "total": task.total},
                                )
            except Exception as exc:
                LOGGER.warning("[每日检查] %s 检查失败：%s", game_key, exc)
                errors.append(f"{game_key}: {exc}")
                if tracker is not None:
                    tracker.record_error(game_key, str(exc))
            finally:
                # 脚本日志任务与便笺 API 相互独立：便笺查失败时也要落库
                # （绝区零的「活跃度奖励」只在 OneDragon 日志里）
                if game_key == "zzz" and tracker is not None:
                    try:
                        _record_zzz_script_tasks(tracker)
                    except Exception:
                        LOGGER.debug("[每日检查] 绝区零脚本任务落库跳过", exc_info=True)
    elif requested_games is not None:
        for game_key, _, _ in hoyo_games:
            if _wants(game_key):
                result.games.append(_build_missing_config_status(game_key, "未配置 mihoyo_sign.cookie，无法查询实时便笺 API"))

    # --- 鸣潮 ---
    # token/did 存数据库（DPAPI 加密），config 只留角色等非密钥信息
    profile = load_wuwa_kujiequ_profile(config if isinstance(config, dict) else None)
    if profile is not None and _wants("wuwa"):
        try:
            # 活跃度 0 时自动等一会儿重查（游戏刚退出，服务器还没结算）
            wuwa_status = check_wuwa_daily_with_retry(profile)
            result.games.append(wuwa_status)
            if tracker is not None:
                _record_wuwa_to_tracker(tracker, wuwa_status)
        except Exception as exc:
            LOGGER.warning("[每日检查] 鸣潮检查失败：%s", exc)
            errors.append(f"wuwa: {exc}")
            if tracker is not None:
                tracker.record_error("wuwa", str(exc))
    elif requested_games is not None and _wants("wuwa"):
        result.games.append(_build_missing_config_status("wuwa", "未配置库街区 profile，无法查询鸣潮每日 API"))

    # --- 塔吉多 / 异环 ---
    # 复用签到用的 token + deviceid 查社区每日任务进度，无需额外凭据
    tajiduo_cfg = config.get("tajiduo_sign", {}) if isinstance(config, dict) else {}
    tajiduo_token = str(tajiduo_cfg.get("token", "") if isinstance(tajiduo_cfg, dict) else "").strip()
    tajiduo_deviceid = str(tajiduo_cfg.get("deviceid", "") if isinstance(tajiduo_cfg, dict) else "").strip()
    tajiduo_uid = str(tajiduo_cfg.get("uid", "0") if isinstance(tajiduo_cfg, dict) else "0").strip()
    tajiduo_phone = str(tajiduo_cfg.get("phone", "") if isinstance(tajiduo_cfg, dict) else "").strip()
    tajiduo_password = str(tajiduo_cfg.get("password", "") if isinstance(tajiduo_cfg, dict) else "").strip()

    if tajiduo_token and tajiduo_deviceid and _wants("nte"):
        try:
            nte_status = check_tajiduo_daily(
                token=tajiduo_token,
                deviceid=tajiduo_deviceid,
                uid=tajiduo_uid,
                phone=tajiduo_phone,
                password=tajiduo_password,
            )
            result.games.append(nte_status)
            if tracker is not None:
                for task in nte_status.tasks:
                    extra = {"current": task.current, "total": task.total}
                    # roleHome 三项数值各自滞后且无日期归属（2026-11-05 事故），
                    # 标 stale 让 completion_state/real_progress_percent 不把它们
                    # 当成当天完成依据，UI 走「未刷新」展示。
                    if task.name in ("活跃度", "本性像素", "都市活力"):
                        extra["stale"] = True
                        extra["stale_reason"] = "roleHome 接口数值无日期归属且实测滞后，仅供展示"
                    tracker.record_task(
                        game_key="nte", game_name="异环",
                        task_key=task.name, task_name=task.name,
                        status="success" if task.is_completed else "pending",
                        source="community",
                        message=f"{task.current}/{task.total}",
                        uid=tajiduo_uid,
                        extra=extra,
                    )
                # ok-nte 日志证据落库（2026-11-05 补）：「日常领取」是异环完成判定
                # 的关键状态（KEY_STATUS_TASKS），此前只在 bridge 展示层注入、从未
                # 落库，补跑/单跑路径的脚本证据会整段丢失。解析按天过滤（当天日志
                # 才有效），record_task 幂等 UPSERT，与批处理路径互不冲突。
                try:
                    from ..services.ok_nte_daily_parser import parse_latest_ok_nte_log

                    parsed = parse_latest_ok_nte_log()
                except Exception as exc:
                    parsed = None
                    LOGGER.debug("[每日检查] ok-nte 日志解析失败: %s", exc)
                for t in (parsed or {}).get("tasks") or []:
                    tracker.record_task(
                        game_key="nte", game_name="异环",
                        task_key=str(t.get("task_name") or ""),
                        task_name=str(t.get("task_name") or ""),
                        status=str(t.get("status") or "pending"),
                        source="script",
                        message=str(t.get("message") or ""),
                        uid=tajiduo_uid,
                    )
        except Exception as exc:
            LOGGER.warning("[每日检查] 塔吉多检查失败：%s", exc)
            errors.append(f"nte: {exc}")
            if tracker is not None:
                tracker.record_error("nte", str(exc))
    elif requested_games is not None and _wants("nte"):
        result.games.append(_build_missing_config_status(
            "nte", "未配置塔吉多 token / deviceid，无法查询异环每日任务"))

    # --- 无限暖暖 ---
    # 2026-10-24 用户拍板：Pearpal notebook 的 5 项任务（每日心愿/周常奖励/
    # 能量体力/派遣/登录天数）常年 0 占位、没有有效进度，停止查询与落库；
    # 暖暖的真实进度只来自 whimbox 日志（朝夕心愿等，含计数）。
    if tracker is not None:
        try:
            _record_whimbox_script_tasks(tracker)
        except Exception:
            LOGGER.debug("[每日检查] whimbox 脚本任务落库跳过", exc_info=True)

    # --- 终末地 ---
    endfield_cfg = config.get("endfield_protocol", {}) if isinstance(config, dict) else {}
    endfield_api_key = str(endfield_cfg.get("api_key", "") if isinstance(endfield_cfg, dict) else "").strip()
    endfield_framework_token = str(endfield_cfg.get("framework_token", "") if isinstance(endfield_cfg, dict) else "").strip()
    endfield_role_id = str(endfield_cfg.get("role_id", "") if isinstance(endfield_cfg, dict) else "").strip()
    endfield_server_id = str(endfield_cfg.get("server_id", "1") if isinstance(endfield_cfg, dict) else "1").strip()

    # 统一优先走森空岛官方 API（复用签到 token，无需 end.shallow.ink 注册）；
    # 仅当森空岛没取到数据、且协议终端配置完整时，才用协议终端兜底。
    endfield_added = False
    if _wants("endfield") and _check_endfield_via_skland(config, result, tracker, errors):
        LOGGER.debug("[每日检查] 终末地每日数据已由森空岛官方 API 提供")
        endfield_added = True
    elif endfield_api_key and endfield_framework_token and endfield_role_id and _wants("endfield"):
        try:
            from ..clients.endfield_protocol import perform_query as _perform_endfield_query
            note_result = _perform_endfield_query(
                endfield_api_key, endfield_framework_token, endfield_role_id, endfield_server_id, "note"
            )
            stamina_result = _perform_endfield_query(
                endfield_api_key, endfield_framework_token, endfield_role_id, endfield_server_id, "stamina"
            )

            tasks: List[DailyTaskItem] = []
            daily_completed = False

            def _ef_get_val(d, *keys):
                for k in keys:
                    v = d.get(k) if isinstance(d, dict) else None
                    if v is not None and v != "":
                        return v
                return None

            def _ef_get_dict(d, *keys):
                for k in keys:
                    v = d.get(k) if isinstance(d, dict) else None
                    if isinstance(v, dict):
                        return v
                return None

            if note_result and isinstance(note_result, dict):
                note_data = note_result.get("data", note_result)
                if isinstance(note_data, dict):
                    daily_mission = _ef_get_dict(note_data, "dailyMission", "daily_mission")
                    dailies = daily_mission or _ef_get_dict(note_data, "dailies", "daily_quest", "daily_quests",
                                                           "daily_task", "daily_tasks", "daily", "quests")
                    if dailies:
                        completed = _safe_int(_ef_get_val(dailies, "activation", "finished", "completed", "done",
                                                          "completed_count", "current", "cur") or 0)
                        total = _safe_int(_ef_get_val(dailies, "maxActivation", "max_activation", "total", "all",
                                                      "total_count", "max") or 0)
                        tasks.append(DailyTaskItem(name="日活跃", current=completed, total=total,
                                                   is_completed=completed >= total if total else False))
                        daily_completed = completed >= total if total else False
                    else:
                        LOGGER.debug("[终末地] note 返回不含 dailies，已有键: %s",
                                      list(note_data.keys())[:15] if isinstance(note_data, dict) else type(note_data))

            if stamina_result and isinstance(stamina_result, dict):
                st_data = stamina_result.get("data", stamina_result)
                if isinstance(st_data, dict):
                    rec = _ef_get_dict(st_data, "recovery", "stamina", "energy", "stamina_info")
                    src = rec if rec else st_data
                    current = _safe_int(_ef_get_val(src, "current", "cur", "value", "stamina") or 0)
                    maximum = _safe_int(_ef_get_val(src, "max", "max_value", "limit", "max_stamina", "maxStamina") or 999, 999)
                    LOGGER.debug("[终末地] 理智状态：%s/%s（不参与每日活跃完成判定）", current, maximum)
                    if rec is not None and maximum > 0:
                        tasks.append(DailyTaskItem(name="体力", current=current, total=maximum,
                                                   is_completed=current >= maximum))

            ef_status = GameDailyStatus(
                game_key="endfield",
                game_name="终末地",
                uid=endfield_role_id,
                nickname="",
                is_daily_completed=daily_completed,
                tasks=tasks,
                extra={"note": note_result, "stamina": stamina_result},
            )
            result.games.append(ef_status)
            endfield_added = True

            if tracker is not None:
                for task in ef_status.tasks:
                    tstatus = "success" if task.is_completed else "pending"
                    tracker.record_task(
                        game_key="endfield", game_name="终末地",
                        task_key=task.name, task_name=task.name,
                        status=tstatus, source="community",
                        message=f"{task.current}/{task.total}",
                        uid=endfield_role_id,
                        extra={"current": task.current, "total": task.total},
                    )
        except Exception as exc:
            LOGGER.warning("[每日检查] 终末地检查失败：%s", exc)
            errors.append(f"endfield: {exc}")
            if tracker is not None:
                tracker.record_error("endfield", str(exc))

    # 终末地没有任何一条路径取到数据时给出可见的失败/缺配置行：
    # 已配置凭据 → 显示失败原因；完全未配置且显式点名 → 显示配置指引。
    if _wants("endfield") and not endfield_added:
        sk_cfg_check = config.get("skendfield_sign", {}) if isinstance(config, dict) else {}
        skland_configured = bool(str(sk_cfg_check.get("token", "") if isinstance(sk_cfg_check, dict) else "").strip())
        ef_configured = bool(endfield_api_key and endfield_framework_token and endfield_role_id)
        if skland_configured or ef_configured:
            skland_reasons = [e for e in errors if e.startswith("endfield_skland") or e.startswith("endfield:")]
            reason = ("森空岛/协议终端均未取到数据。" +
                      ("；".join(skland_reasons) if skland_reasons else "查询失败，原因未知。"))
            result.games.append(_build_missing_config_status("endfield", reason))
        elif requested_games is not None:
            reason = ("森空岛查询未取到数据。请在「森空岛签到」卡片填写 token"
                      "（web-api.skland.com/account/info/hg 的 data.content）。")
            result.games.append(_build_missing_config_status("endfield", reason))

    if errors:
        LOGGER.warning("[每日检查] 部分游戏检查出错：%s", "；".join(errors))

    return result


def _record_wuwa_to_tracker(tracker, status: GameDailyStatus):
    """将鸣潮每日状态写入 tracker。

    注意：库街区 API 的活跃度需「进入游戏后」才会刷新，未进游戏时返回 cur=0。
    此时不能把 0 当真实进度写入并标 pending（会被 UI 显示成"活跃度 0/100"，
    实际玩家可能已 90/100），要额外带 stale 标记让上层能识别。
    """
    for task in status.tasks:
        tstatus = "success" if task.is_completed else "pending"
        extra = {"current": task.current, "total": task.total}
        # 关键计数（活跃度）拿到 0 时视为"官方未刷新"，而非真实 0
        if task.name == "今日活跃" and task.total > 0 and task.current <= 0:
            extra["stale"] = True
            extra["stale_reason"] = "库街区活跃度需进入游戏后才会刷新，当前为未刷新值"
        tracker.record_task(
            game_key=status.game_key, game_name=status.game_name,
            task_key=task.name, task_name=task.name,
            status=tstatus, source="community",
            message=f"{task.current}/{task.total}",
            uid=status.uid, nickname=status.nickname,
            extra=extra,
        )


def check_all_games(config: Optional[Dict[str, Any]] = None) -> DailyCheckResult:
    """兼容旧接口：检查已配置的所有游戏的每日完成状态。"""
    return check_games_progress(config=config, game_keys=None)


def is_script_success(result: DailyCheckResult, required_games: Optional[List[str]] = None) -> bool:
    """基于每日检查结果判定脚本执行是否成功。

    required_games: 要求必须完成的游戏 key 列表。为 None 表示所有检查的游戏都需要完成。
    """
    if required_games:
        for game_key in required_games:
            match = [g for g in result.games if g.game_key == game_key]
            if not match or not match[0].is_daily_completed:
                return False
        return True
    return result.all_completed


# ---------------------------------------------------------------------------
# 格式输出
# ---------------------------------------------------------------------------

def format_daily_status(status: GameDailyStatus) -> str:
    """格式化单个游戏的每日状态为可读字符串。"""
    lines = [f"【{status.game_name}】{status.nickname} ({status.uid})"]
    if status.errors:
        lines.append(f"  错误：{'；'.join(status.errors)}")
        return "\n".join(lines)
    lines.append(f"  每日完成：{'是' if status.is_daily_completed else '否'}")
    for task in status.tasks:
        lines.append(f"  {task.name}：{task.current}/{task.total}")
    return "\n".join(lines)


def format_daily_result(result: DailyCheckResult) -> str:
    """格式化每日检查结果为可读字符串。"""
    lines = [f"每日检查汇总（{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(result.checked_at))}）"]
    for status in result.games:
        lines.append(format_daily_status(status))
    lines.append(f"全部完成：{'是' if result.all_completed else '否'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def _qr_login_and_update_config() -> bool:
    """扫码登录获取 stoken 并更新 app_config.json。"""
    import json as _json
    from pathlib import Path
    # 包内相对导入（审计 P2-6）：旧写法 `from clients.mihoyo_sign import ...`
    # 只有从 src/game_auto 目录裸跑脚本才成立，`python -m game_auto.clients.
    # daily_checker --qr-login` 必 ModuleNotFoundError
    from .mihoyo_sign import complete_qrcode_login

    print("=" * 50)
    print("米游社扫码登录 - 获取 stoken")
    print("=" * 50)
    print()
    print("即将生成二维码，请用米游社 App 扫码登录。")
    print("扫码后请在手机上确认登录。")
    print()

    try:
        result = complete_qrcode_login()
    except Exception as exc:
        print(f"扫码登录失败：{exc}")
        return False

    cookies = result.get("cookies") or {}
    record_ready = result.get("record_ready", False)
    account_id = result.get("account_id", "")
    stoken = result.get("stoken", "")

    print(f"登录结果：账号 {account_id}")
    print(f"  stoken: {'已获取' if stoken else '未获取'}")
    print(f"  便笺查询: {'已就绪' if record_ready else '未就绪'}")

    if not stoken:
        print("未能获取 stoken，无法查询实时便笺。")
        return False

    # 构建 cookie 字符串
    cookie_parts = []
    for key in ("account_id", "stuid", "stoken", "stoken_v2", "mid",
                "cookie_token", "cookie_token_v2", "ltoken", "ltoken_v2",
                "ltuid", "ltuid_v2", "login_ticket"):
        val = cookies.get(key) or cookies.get(f"{key}_v2") or ""
        if val:
            cookie_parts.append(f"{key}={val}")
    cookie_str = "; ".join(cookie_parts)

    # 更新 app_config.json
    config_path = Path(__file__).resolve().parents[3] / "app_config.json"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            config = _json.load(f)
    else:
        config = {}

    mihoyo_cfg = config.setdefault("mihoyo_sign", {})
    mihoyo_cfg["cookie"] = cookie_str
    mihoyo_cfg["stoken_cookie"] = cookie_str

    from ..settings.manager import atomic_write_json
    atomic_write_json(config_path, config)

    print(f"\nCookie 已保存到 {config_path}")
    print(f"Cookie 字段: {list(cookies.keys())}")
    return True


def main() -> int:
    """命令行入口。"""
    import argparse
    import logging as _logging

    parser = argparse.ArgumentParser(description="每日任务完成情况检测")
    parser.add_argument("--qr-login", action="store_true",
                        help="扫码登录获取 stoken 并更新配置")
    parser.add_argument("--game", nargs="*", default=None,
                        help="指定游戏 key（genshin/honkai_sr/zzz/wuwa/nikki/endfield）")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--success-check", action="store_true",
                        help="仅输出成功/失败判定（退出码）")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    args = parser.parse_args()

    _logging.basicConfig(
        level=_logging.DEBUG if args.verbose else _logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.qr_login:
        ok = _qr_login_and_update_config()
        return 0 if ok else 1

    result = check_games_progress(game_keys=args.game)

    if args.json:
        import json as _json
        import dataclasses

        class _Encoder(_json.JSONEncoder):
            def default(self, o):
                if dataclasses.is_dataclass(o) and not isinstance(o, type):
                    return dataclasses.asdict(o)
                return super().default(o)

        print(_json.dumps(result, cls=_Encoder, ensure_ascii=False, indent=2))
        return 0

    if args.success_check:
        if result.all_completed:
            print("全部完成")
            return 0
        else:
            for g in result.failed_games:
                print(f"未完成: {g.game_name}")
            return 1

    print(format_daily_result(result))
    return 0 if result.all_completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
