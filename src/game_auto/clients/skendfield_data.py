"""森空岛（Skland）终末地数据查询客户端 — 走官方 API。

背景
----
项目原本用第三方协议终端 ``end-api.shallow.ink`` 查终末地数据，需要额外到
https://end.shallow.ink/ 注册拿 ``api_key`` / ``framework_token``。配置不全时
bridge 会报「查询失败或缺少必要参数」，这是终末地查询失败的主要原因。

森空岛官方本身就开放了数据接口，而且用的**就是用户已经填好的签到 token**，
不需要任何额外注册。本模块即封装这条路。

端点
----
    GET  /api/v1/game/player/binding           获取绑定的游戏与角色
    GET  /api/v1/game/endfield/card/detail     终末地角色卡片（含任务进度）

    Base: https://zonai.skland.com/api/v1   国服
          https://zonai.skport.com/api/v1   国际服

认证与参数
----------
card/detail 需要三样东西，缺一不可：

    请求头 cred           login() 换到的 cred
    请求头 sk-game-role   形如 ``3_{roleId}_{serverId}``（3 是终末地的 gameId）
    Query                 roleId、serverId

这与项目 ``skendfield_sign.py`` 里签到组装 ``sk-game-role`` 的方式一致。

⚠️ 踩坑：先前照抄 ak-endfield-mcp 用了
``/api/v1/game/player/gameData?uid=``，实测 **404** —— 该路径对终末地不存在。
正确路径以 skland-kit 的 ``getEndfieldDetails`` 为准。

用法
----
    from clients.skendfield_data import fetch_endfield_data
    data = fetch_endfield_data(token, server="cn")
    if data["ok"]:
        print(data["daily"])   # {"current": 80, "total": 100}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .skendfield_sign import (
    PLATFORM,
    SERVER_CONFIG,
    VNAME,
    SkEndfieldClient,
    SkEndfieldSignError,
    _request,
)

LOGGER = logging.getLogger("GameAutomation")

# 终末地角色卡片（含任务进度）。与 binding 同在 /api/v1 下。
CARD_DETAIL_PATH = "/api/v1/game/endfield/card/detail"

# 终末地在森空岛的 gameId，用于拼 sk-game-role 头
ENDFIELD_GAME_ID = 3

# card/detail 响应里"每日任务"可能出现的容器键
_DAILY_KEYS = (
    "dailyMission", "daily_mission", "daily", "dailies",
    "dailyTask", "daily_task", "dailyTasks", "daily_tasks",
    "task", "tasks", "dailyActivity", "daily_activity",
)
# 进度值键名。前两个是终末地实测字段名（见 skland-kit types/player.ts 的
# EndfieldDetails.dailyMission: { dailyActivation, maxDailyActivation }）
_CURRENT_KEYS = ("dailyActivation", "activation",
                 "current", "cur", "finished", "completed", "done",
                 "completedCount", "completed_count", "value", "progress")
_TOTAL_KEYS = ("maxDailyActivation", "maxActivation", "max_activation",
               "total", "max", "all", "limit",
               "totalCount", "total_count", "target", "need")


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """宽松转 int，失败返回 default。"""
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _sign_with_query(sign_token: str, path: str, query: str, body: str = "") -> tuple:
    """生成**带 query** 的接口签名。

    ⚠️ 森空岛的签名字符串顺序是 ``path + query + body + timestamp + header_json``
    （见 skland-kit 的 ``utils/signature.ts``）。而项目
    ``skendfield_sign._generate_sign`` 只拼 ``path + body + timestamp + header``，
    **漏了 query**——签到用的 binding / attendance 都没 query，所以一直没暴露；
    一旦请求带 query（如 card/detail?roleId=..&serverId=..），
    服务端算出的签名就对不上，返回 ``code=10000 请求异常``。

    这里按官方顺序补上 query，其余算法（HMAC-SHA256 → MD5）保持不变。
    """
    t = str(int(time.time()))
    sign_header = {"platform": PLATFORM, "timestamp": t, "dId": "", "vName": VNAME}
    sign_header_str = json.dumps(sign_header, separators=(",", ":"))
    sign_str = path + query + body + t + sign_header_str
    hmac_hex = hmac.new(
        sign_token.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    md5_sign = hashlib.md5(hmac_hex.encode("utf-8")).hexdigest()
    return md5_sign, sign_header


def _base_url(server: str) -> str:
    """由 SERVER_CONFIG 推导 zonai 基址，避免硬编码域名。"""
    cfg = SERVER_CONFIG.get(server) or SERVER_CONFIG["cn"]
    bind_url = cfg["bind_url"]                       # https://zonai.skland.com/api/v1/game/player/binding
    parsed = urlparse(bind_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _extract_role(binding: Dict[str, Any]) -> tuple[str, str]:
    """从绑定信息里取 (roleId, serverId)。

    card/detail 用 roleId + serverId 定位角色（不是 uid）。
    结构与 ``skendfield_sign.do_sign`` 里取角色的方式保持一致：
    优先 defaultRole，其次 roles[0]。
    """
    if not isinstance(binding, dict):
        return "", ""
    default_role = binding.get("defaultRole") or {}
    if not default_role:
        roles = binding.get("roles") or []
        if roles and isinstance(roles[0], dict):
            default_role = roles[0]
    role_id = str(default_role.get("roleId") or "").strip()
    server_id = str(default_role.get("serverId") or "").strip()
    return role_id, server_id


def _pick_daily(raw_data: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """从 card/detail 响应里提取每日任务进度 {current, total}。"""
    if not isinstance(raw_data, dict):
        return None

    # 依次尝试：顶层 → detail 子层 → data 子层，每层再找 daily 类容器
    # （skland-kit 的 getEndfieldDetails 声明返回 { detail: EndfieldDetails }，
    #  实际接口的 data 可能直接就是 EndfieldDetails，两种都要覆盖）
    containers: List[Any] = [raw_data]
    for layer_key in ("detail", "data"):
        layer = raw_data.get(layer_key)
        if isinstance(layer, dict):
            containers.append(layer)
            for key in _DAILY_KEYS:
                if isinstance(layer.get(key), dict):
                    containers.append(layer[key])
    for key in _DAILY_KEYS:
        if isinstance(raw_data.get(key), dict):
            containers.append(raw_data[key])

    for container in containers:
        if not isinstance(container, dict):
            continue
        current = None
        total = None
        for key in _CURRENT_KEYS:
            current = _as_int(container.get(key))
            if current is not None:
                break
        for key in _TOTAL_KEYS:
            total = _as_int(container.get(key))
            if total is not None:
                break
        if current is not None or total is not None:
            return {"current": current or 0, "total": total or 0}

    return None


def _unwrap(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    """取真正的详情层：接口可能直接在 data 上，也可能包一层 detail。"""
    if not isinstance(raw_data, dict):
        return {}
    detail = raw_data.get("detail")
    if isinstance(detail, dict):
        return detail
    return raw_data


def _extract_extras(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    """从 card/detail 里提取除每日活跃外的常用展示字段。

    字段结构依据 skland-kit ``types/player.ts`` 的 ``EndfieldDetails``。
    """
    d = _unwrap(raw_data)
    if not d:
        return {}

    extras: Dict[str, Any] = {}

    base = d.get("base") if isinstance(d.get("base"), dict) else {}
    if base.get("name"):
        extras["role_name"] = str(base["name"])
    if base.get("level") is not None:
        extras["level"] = _as_int(base.get("level"))
    if base.get("worldLevel") is not None:
        extras["world_level"] = _as_int(base.get("worldLevel"))
    if base.get("serverName"):
        extras["server_name"] = str(base["serverName"])
    if base.get("charNum") is not None:
        extras["char_num"] = _as_int(base.get("charNum"))
    main_mission = base.get("mainMission")
    if isinstance(main_mission, dict) and main_mission.get("description"):
        extras["main_mission"] = str(main_mission["description"])

    # 体力：真实 API 里 dungeon 是 detail 的直接子对象（dict），
    # 形如 {curStamina, maxStamina, maxTs}（与 skland-kit types/player.ts 一致）。
    # 旧假设放在 domain[].dungeon 里，这里两者都兼容。
    dungeon = d.get("dungeon") if isinstance(d.get("dungeon"), dict) else {}
    if dungeon.get("curStamina") is not None:
        extras["stamina"] = _as_int(dungeon.get("curStamina"))
        extras["max_stamina"] = _as_int(dungeon.get("maxStamina"))
        extras["stamina_recover_ts"] = dungeon.get("maxTs")
    else:
        domains = d.get("domain") if isinstance(d.get("domain"), list) else []
        for dm in domains:
            if not isinstance(dm, dict):
                continue
            dg = dm.get("dungeon")
            if isinstance(dg, dict) and dg.get("curStamina") is not None:
                extras["stamina"] = _as_int(dg.get("curStamina"))
                extras["max_stamina"] = _as_int(dg.get("maxStamina"))
                extras["stamina_recover_ts"] = dg.get("maxTs")
                break

    bp = d.get("bpSystem") if isinstance(d.get("bpSystem"), dict) else {}
    if bp.get("curLevel") is not None:
        extras["bp_level"] = _as_int(bp.get("curLevel"))
        extras["bp_max_level"] = _as_int(bp.get("maxLevel"))

    achieve = d.get("achieve") if isinstance(d.get("achieve"), dict) else {}
    if achieve.get("count") is not None:
        extras["achieve_count"] = _as_int(achieve.get("count"))

    return extras


def fetch_endfield_data(token: str, server: str = "cn") -> Dict[str, Any]:
    """查询终末地每日数据（走森空岛官方 API）。

    Args:
        token: 森空岛 token，即签到用的那个
               （web-api.skland.com/account/info/hg 返回的 data.content）
        server: "cn" | "global"

    Returns:
        {
            "ok": bool,
            "uid": str,
            "role_name": str,
            "server_name": str,
            "daily": {"current": int, "total": int} | None,
            "raw": dict,          # 原始 card/detail.data，便于自行解析
            "raw_keys": [str],    # 顶层键名，便于排查结构
            "message": str,       # 失败原因
        }
    """
    result: Dict[str, Any] = {
        "ok": False, "uid": "", "role_name": "", "server_name": "",
        "daily": None, "extras": {}, "raw": {}, "raw_keys": [], "message": "",
    }

    if not (token or "").strip():
        result["message"] = "未配置森空岛 token"
        return result

    client = SkEndfieldClient(token=token, server=server)
    try:
        binding = client.login()
    except SkEndfieldSignError as exc:
        result["message"] = f"森空岛登录失败: {exc}"
        LOGGER.warning("[森空岛数据] 登录失败: %s", exc)
        return result
    except Exception as exc:  # noqa: BLE001 - 网络异常也要收敛成结构化结果
        result["message"] = f"森空岛登录异常: {exc}"
        LOGGER.warning("[森空岛数据] 登录异常: %s", exc)
        return result

    role_id, server_id = _extract_role(binding)
    default_role = (binding or {}).get("defaultRole") or {}
    result["uid"] = role_id
    result["role_name"] = default_role.get("nickname", "")
    result["server_name"] = default_role.get("serverName", "")

    if not role_id or not server_id:
        result["message"] = "未能从绑定信息中解析出 roleId / serverId"
        LOGGER.warning("[森空岛数据] 绑定信息缺少 roleId/serverId，已有键: %s",
                       list((binding or {}).keys()))
        return result

    # query 必须与最终 URL 上的完全一致，否则签名不匹配 -> code=10000
    query = f"roleId={role_id}&serverId={server_id}"

    # 注意：card/detail 带 query，必须用 _sign_with_query（签名串要含 query）；
    # 若沿用 _generate_sign 会因签名不含 query 被服务端判为请求异常。
    sign, sign_header = _sign_with_query(client._sign_token, CARD_DETAIL_PATH, query)
    headers = {
        "cred": client._cred,
        "platform": sign_header["platform"],
        "vName": sign_header["vName"],
        "timestamp": sign_header["timestamp"],
        "sign": sign,
        "sk-game-role": f"{ENDFIELD_GAME_ID}_{role_id}_{server_id}",
        "x-requested-with": "com.hypergryph.skland",
    }

    url = f"{_base_url(server)}{CARD_DETAIL_PATH}?{query}"
    try:
        resp = _request("GET", url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        result["message"] = f"card/detail 请求异常: {exc}"
        LOGGER.warning("[森空岛数据] card/detail 请求异常: %s", exc)
        return result

    code = resp.get("code", -1) if isinstance(resp, dict) else -1
    if code != 0:
        msg = resp.get("message", resp.get("msg", "未知错误")) if isinstance(resp, dict) else str(resp)
        result["message"] = f"card/detail 返回 code={code}: {msg}"
        LOGGER.warning("[森空岛数据] %s", result["message"])
        return result

    data = resp.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    result["ok"] = True
    result["raw"] = data
    result["raw_keys"] = list(data.keys())
    result["daily"] = _pick_daily(data)
    result["extras"] = _extract_extras(data)

    # extras 里的角色名/区服比 binding 的更准（来自游戏内存档），优先用它
    if result["extras"].get("role_name"):
        result["role_name"] = result["extras"]["role_name"]
    if result["extras"].get("server_name"):
        result["server_name"] = result["extras"]["server_name"]

    if result["daily"] is None:
        # 数据拿到了但没识别出每日进度——把键名写进日志，便于对齐真实结构
        LOGGER.info("[森空岛数据] card/detail 成功但未识别到每日任务字段，顶层键: %s",
                    result["raw_keys"])

    return result


def format_endfield_data_report(data: Dict[str, Any]) -> List[str]:
    """把 fetch_endfield_data 的结果整理成可读文本行。"""
    lines: List[str] = []
    if not data.get("ok"):
        return [f"终末地数据查询失败：{data.get('message') or '未知原因'}"]

    extras = data.get("extras") or {}
    role = data.get("role_name") or "未知角色"
    level = extras.get("level")
    channel = data.get("server_name") or ""
    head = f"终末地 {role}"
    if level is not None:
        head += f" Lv.{level}"
    if channel:
        head += f"（{channel}）"
    lines.append(head)

    daily = data.get("daily")
    if daily and daily.get("total"):
        cur, total = daily["current"], daily["total"]
        flag = "已完成" if cur >= total else "未完成"
        lines.append(f"  每日活跃：{cur}/{total}（{flag}）")
    else:
        lines.append("  每日活跃：未识别到数据（把结果里的 raw_keys 发我即可修好解析）")

    if extras.get("stamina") is not None:
        lines.append(f"  体力：{extras['stamina']}/{extras.get('max_stamina') or '?'}")
    if extras.get("bp_level") is not None:
        lines.append(f"  电台等级：{extras['bp_level']}/{extras.get('bp_max_level') or '?'}")
    if extras.get("achieve_count") is not None:
        lines.append(f"  成就：{extras['achieve_count']}")
    if extras.get("char_num") is not None:
        lines.append(f"  干员数：{extras['char_num']}")
    if extras.get("main_mission"):
        lines.append(f"  主线：{extras['main_mission']}")

    return lines


if __name__ == "__main__":  # pragma: no cover - 手动排查用
    import sys

    logging.basicConfig(level=logging.INFO)
    tok = sys.argv[1] if len(sys.argv) > 1 else ""
    svr = sys.argv[2] if len(sys.argv) > 2 else "cn"
    out = fetch_endfield_data(tok, svr)
    print(json.dumps({k: v for k, v in out.items() if k != "raw"},
                     ensure_ascii=False, indent=2))
    if out.get("ok"):
        print("raw 顶层键:", out.get("raw_keys"))
