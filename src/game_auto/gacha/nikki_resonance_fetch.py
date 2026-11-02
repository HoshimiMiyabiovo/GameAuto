from __future__ import annotations

import argparse
import base64
import http.client
import json
import logging
import platform
import re
import sys
import time
import urllib.parse
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("GameAutomation")

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


CLICKHOUSE_DOMAIN = "x6cn-clickhouse.nuanpaper.com"
JWT_PATTERN = re.compile(r"^eyJ[\w_-]+\.(?P<payload>[\w_-]+)\.[\w_-]+$")
MOMO_TOKEN_CONSOLE_SCRIPT = (
    "console.log(JSON.stringify({roleid:[...document.querySelectorAll('div')]"
    ".find(el=>el.textContent.startsWith('UID:'))?.textContent.replace('UID:','').trim(),"
    "token:document.cookie.match(/momoToken=([^;]+)/)?.[1],"
    "id:document.cookie.match(/momoNid=([^;]+)/)?.[1]}));"
)
PEARPAL_API_HOST = "pearpal-api.infoldgames.com"
NUANPAPER_API_HOST = "myl-api.nuanpaper.com"
PEARPAL_USER_INFO_PATH = "/v1/strategy/user/info/get"
PEARPAL_NOTEBOOK_PATH = "/v1/strategy/user/note/book/info"
PEARPAL_GLOBAL_CLIENT_ID = 1116
PEARPAL_CHINA_CLIENT_ID = 1106

GACHA_POOLS: Dict[str, str] = {
    "1": "遥远之海",
    "2": "绘于蝶影的幻梦",
    "3": "留于花间的浮想",
    "4": "呱呱轻语",
    "5": "泡泡心诉",
    "6": "飞向流星的愿望",
    "7": "粉红缎带之舞",
    "8": "追逐流星的鱼",
    "9": "献给夜空的花束",
    "10": "礼花升空的序曲",
    "11": "群芳的剪影",
    "12": "风中的轻歌",
    "13": "彷徨于此的终曲",
    "14": "悠然茶歇之时",
    "15": "迎风疾驰之行",
    "16": "坠入狂欢夜",
    "17": "牧铃声中的曼舞",
    "18": "羽翎笔下的奇思",
    "19": "若留于永雪之境",
    "20": "我心不落",
    "21": "且听流水之音",
    "22": "点染丰饶之梦",
    "23": "聆听仙子的秘语",
    "24": "白日造梦家",
    "25": "向着云顶，冲鸭",
    "26": "于泪中，你与我重逢",
    "27": "自由的第二章",
    "28": "于雾中呼唤",
    "29": "千重云墨千重缘",
    "30": "缘起绘笔万念生",
    "31": "烟雨侠客行",
    "32": "绮梦讹兽语",
    "33": "梦尽于无终幕处",
    "34": "向星光奔赴",
    "35": "趁夜色叛逃",
    "36": "风中奏鸣之花",
    "37": "远方与花期",
    "38": "遥遥晴雨天",
    "39": "我所行之途",
    "40": "菇灯萤梦",
    "41": "何处寻青羽",
    "42": "何人消隐于祷声",
    "43": "何梦破碎于无瑕",
    "44": "甜花絮语的午后",
    "45": "鹿与远森之歌",
    "46": "如一叶长青的诗",
    "47": "孤影栖薄枝",
    "48": "白影织林梦",
    "49": "千靥万相长夜赋",
    "50": "万般尘相入玉心",
    "51": "灯花欲语暖",
    "52": "唯塑此间意",
    "53": "如我斑斓",
    "54": "梦游者的谜底",
    "55": "悲欢秘宴",
    "56": "以森林唤她之名",
    "57": "万声终末的回响",
    "58": "契约终末之时",
    "59": "往复于栖骨之畔",
    "60": "辗转于无归之劫",
    "61": "瑰途终寂",
    "62": "似月长明",
}

PoolRecords = List[List[int | str]]
StoreData = Dict[str, PoolRecords]
ProgressCallback = Optional[Callable[[str], None]]


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取无限暖暖共鸣记录，输出 nuan5 兼容的 gacha-records-UID.json。",
        epilog=get_momo_token_help_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "jwt",
        nargs="?",
        default="",
        help="可选，抓包得到的 JWT；如果不填，也可改用 --uid + --momo-id + --momo-token。",
    )
    parser.add_argument(
        "force_pool_ids",
        nargs="?",
        default="",
        help="可选，强制刷新卡池 ID，逗号分隔，例如 1,57。",
    )
    parser.add_argument("--uid", default="", help="可选，使用 Momo Token verify 时填写游戏 UID。")
    parser.add_argument("--momo-id", default="", help="可选，Pearpal 页面 cookie 里的 momoNid。")
    parser.add_argument("--momo-token", default="", help="可选，Pearpal 页面 cookie 里的 momoToken。")
    parser.add_argument(
        "--fetch-pearpal",
        action="store_true",
        help="改为抓取 Pearpal notebook 摘要 JSON；该模式只保留 4/5 星事件。",
    )
    parser.add_argument(
        "--domain",
        default=CLICKHOUSE_DOMAIN,
        help="ClickHouse 域名，国服默认 x6cn-clickhouse.nuanpaper.com。",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="输出 JSON 路径；完整记录默认 gacha-records-<UID>.json，Pearpal 摘要默认 nikki-resonance-data-<UID>-pearpal.json。",
    )
    parser.add_argument(
        "--sleep-sec",
        type=float,
        default=1.0,
        help="分页读取之间的等待秒数，默认 1。",
    )
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=20.0,
        help="单次 HTTPS 请求超时秒数，默认 20。",
    )
    return parser.parse_args(argv)


def decode_jwt_payload(jwt: str) -> Dict[str, Any]:
    match = JWT_PATTERN.match(jwt.strip())
    if not match:
        raise ValueError("无法识别 JWT 格式，期望形如 eyJXXX.XXX.XXX")

    payload = match.group("payload")
    if len(payload) % 4:
        payload += "=" * (4 - len(payload) % 4)
    decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
    data = json.loads(decoded.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("sub"):
        raise ValueError("JWT payload 无效，缺少 sub")
    return data


def get_momo_token_console_script() -> str:
    return MOMO_TOKEN_CONSOLE_SCRIPT


def get_momo_token_help_lines() -> List[str]:
    return [
        "Momo Token 获取方法:",
        "1. 登录美鸭梨 / Pearpal：海外服 https://pearpal.infoldgames.com/en/home ，国服 https://myl.nuanpaper.com/home",
        "2. 在同域页面按 F12 打开控制台，执行下面这段脚本：",
        MOMO_TOKEN_CONSOLE_SCRIPT,
        "3. 控制台会输出 JSON，其中 roleid 对应 UID，token 对应 Momo Token，id 对应 Momo ID。",
    ]


def get_momo_token_help_text() -> str:
    return "\n".join(get_momo_token_help_lines())


def _build_platform_version() -> str:
    if platform.system() == "Windows":
        return f"Windows/{platform.version()}.1.256.64bit"
    return f"{platform.system()}/{platform.release()}"


def _request_bytes(
    method: str,
    path: str,
    *,
    domain: str,
    timeout_sec: float,
    body: Optional[bytes] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> bytes:
    platform_version = _build_platform_version()
    conn = http.client.HTTPSConnection(domain, 443, timeout=timeout_sec)
    headers = {
        "Host": domain,
        "Accept": "*/*",
        "Accept-Encoding": "deflate, gzip",
        "User-Agent": f"X6Game/UE5-CL-0 (http-legacy) {platform_version}",
        "Content-Length": str(len(body) if body is not None else 0),
    }
    if extra_headers:
        headers.update(extra_headers)

    try:
        conn.request(method.upper(), path, body=body, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            LOGGER.error("[nikki] HTTP 请求失败 %s %s -> %s %s", method.upper(), path, response.status, response.reason)
            raise ConnectionError(f"HTTP {response.status}: {response.reason}")
        response_body = response.read()
        encoding = response.getheader("Content-Encoding")
    finally:
        conn.close()

    return _decompress_response(response_body, encoding)


def _request_json(
    path: str,
    *,
    domain: str,
    timeout_sec: float,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    data = _request_bytes(
        "GET",
        path,
        domain=domain,
        timeout_sec=timeout_sec,
        extra_headers=extra_headers,
    )
    return json.loads(data.decode("utf-8"))


def _request_json_post(
    path: str,
    payload: Dict[str, Any],
    *,
    domain: str,
    timeout_sec: float,
) -> Dict[str, Any]:
    data = _request_bytes(
        "POST",
        path,
        domain=domain,
        timeout_sec=timeout_sec,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        extra_headers={"Content-Type": "application/json;charset=utf-8"},
    )
    return json.loads(data.decode("utf-8"))


def _read_store_file(path: Path) -> StoreData:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"读取已有缓存失败: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("已有缓存 JSON 不是对象")

    result: StoreData = {}
    for pool_id, rows in payload.items():
        if pool_id not in GACHA_POOLS or not isinstance(rows, list):
            continue
        normalized: PoolRecords = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 2:
                continue
            normalized.append([str(row[0]), int(row[1])])
        result[str(pool_id)] = normalized
    return result


def _write_store_file(path: Path, data: StoreData):
    ordered = {pool_id: data[pool_id] for pool_id in GACHA_POOLS if pool_id in data}
    path.write_text(json.dumps(ordered, ensure_ascii=False, indent=4), encoding="utf-8")


def _decompress_response(body: bytes, content_encoding: Optional[str]) -> bytes:
    if content_encoding == "deflate":
        try:
            return zlib.decompress(body, -zlib.MAX_WBITS)
        except zlib.error:
            return zlib.decompress(body)
    if content_encoding == "gzip":
        return zlib.decompress(body, zlib.MAX_WBITS | 16)
    if content_encoding is None:
        return body
    raise ValueError(f"不支持的 Content-Encoding: {content_encoding}")


def _read_snappy_varint(data: bytes, index: int) -> Tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if index >= len(data):
            raise ValueError("Snappy 数据损坏：读取长度时提前结束")
        current = data[index]
        index += 1
        value |= (current & 0x7F) << shift
        if not (current & 0x80):
            return value, index
        shift += 7
        if shift > 35:
            raise ValueError("Snappy 数据损坏：长度 varint 过长")


def _copy_from_output(buffer: bytearray, length: int, offset: int):
    if offset <= 0 or offset > len(buffer):
        raise ValueError("Snappy 数据损坏：copy offset 非法")
    for _ in range(length):
        buffer.append(buffer[-offset])


def decompress_snappy_block(data: bytes) -> bytes:
    if not data:
        return b""

    expected_length, index = _read_snappy_varint(data, 0)
    output = bytearray()
    while index < len(data):
        tag = data[index]
        index += 1
        tag_type = tag & 0x03

        if tag_type == 0:
            length = tag >> 2
            if length < 60:
                length += 1
            else:
                extra_length_bytes = length - 59
                if index + extra_length_bytes > len(data):
                    raise ValueError("Snappy 数据损坏：literal 长度越界")
                length = int.from_bytes(data[index:index + extra_length_bytes], "little") + 1
                index += extra_length_bytes

            if index + length > len(data):
                raise ValueError("Snappy 数据损坏：literal 内容越界")
            output.extend(data[index:index + length])
            index += length
            continue

        if tag_type == 1:
            length = 4 + ((tag >> 2) & 0x07)
            if index >= len(data):
                raise ValueError("Snappy 数据损坏：copy1 offset 越界")
            offset = data[index] | ((tag & 0xE0) << 3)
            index += 1
            _copy_from_output(output, length, offset)
            continue

        if tag_type == 2:
            length = 1 + (tag >> 2)
            if index + 2 > len(data):
                raise ValueError("Snappy 数据损坏：copy2 offset 越界")
            offset = int.from_bytes(data[index:index + 2], "little")
            index += 2
            _copy_from_output(output, length, offset)
            continue

        length = 1 + (tag >> 2)
        if index + 4 > len(data):
            raise ValueError("Snappy 数据损坏：copy4 offset 越界")
        offset = int.from_bytes(data[index:index + 4], "little")
        index += 4
        _copy_from_output(output, length, offset)

    if len(output) != expected_length:
        raise ValueError(f"Snappy 解压长度不匹配: {len(output)} != {expected_length}")
    return bytes(output)


def _is_china_domain(domain: str) -> bool:
    normalized = str(domain or "").strip().lower()
    return "nuanpaper.com" in normalized or normalized.startswith("x6cn-")


def _get_pearpal_api_config(domain: str) -> Dict[str, Any]:
    if _is_china_domain(domain):
        return {
            "api_host": NUANPAPER_API_HOST,
            "client_id": PEARPAL_CHINA_CLIENT_ID,
            "region_label": "CN",
        }
    return {
        "api_host": PEARPAL_API_HOST,
        "client_id": PEARPAL_GLOBAL_CLIENT_ID,
        "region_label": "GLOBAL",
    }


def fetch_pearpal_user_info(
    momo_token: str,
    momo_id: str,
    *,
    domain: str = CLICKHOUSE_DOMAIN,
    timeout_sec: float = 20.0,
) -> Dict[str, Any]:
    normalized_momo_token = str(momo_token or "").strip()
    normalized_momo_id = str(momo_id or "").strip()
    if not normalized_momo_token or not normalized_momo_id:
        raise ValueError("抓取 Pearpal notebook 摘要时必须提供 Momo Token 和 Momo ID。")

    config = _get_pearpal_api_config(domain)
    payload = _request_json_post(
        PEARPAL_USER_INFO_PATH,
        {
            "client_id": config["client_id"],
            "token": normalized_momo_token,
            "openid": normalized_momo_id,
        },
        domain=config["api_host"],
        timeout_sec=timeout_sec,
    )
    if payload.get("code") != 0:
        LOGGER.error("[nikki] 用户信息校验失败 code=%s info=%s", payload.get("code"), payload.get("info"))
        raise RuntimeError(f"Pearpal 用户信息校验失败: {payload.get('code')} {payload.get('info')}")
    return payload


def decode_pearpal_notebook_payload(raw_body: bytes) -> Dict[str, Any]:
    if not raw_body:
        raise ValueError("Pearpal notebook 返回为空")

    stripped = raw_body.lstrip()
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        return json.loads(raw_body.decode("utf-8"))

    decoded = decompress_snappy_block(raw_body)
    return json.loads(decoded.decode("utf-8"))


def fetch_pearpal_notebook_payload(
    momo_token: str,
    momo_id: str,
    *,
    domain: str = CLICKHOUSE_DOMAIN,
    timeout_sec: float = 20.0,
) -> Dict[str, Any]:
    normalized_momo_token = str(momo_token or "").strip()
    normalized_momo_id = str(momo_id or "").strip()
    if not normalized_momo_token or not normalized_momo_id:
        raise ValueError("抓取 Pearpal notebook 摘要时必须提供 Momo Token 和 Momo ID。")

    config = _get_pearpal_api_config(domain)
    raw_body = _request_bytes(
        "POST",
        PEARPAL_NOTEBOOK_PATH,
        domain=config["api_host"],
        timeout_sec=timeout_sec,
        body=json.dumps(
            {
                "client_id": config["client_id"],
                "token": normalized_momo_token,
                "openid": normalized_momo_id,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        extra_headers={"Content-Type": "application/json;charset=utf-8"},
    )
    return decode_pearpal_notebook_payload(raw_body)


def build_pearpal_document(
    resolved_uid: str,
    notebook_payload: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    info_from_self = notebook_payload.get("info_from_self") if isinstance(notebook_payload, dict) else None
    gacha_list = info_from_self.get("gacha_list") if isinstance(info_from_self, dict) else []
    if gacha_list is None:
        gacha_list = []
    if not isinstance(gacha_list, list):
        raise ValueError("Pearpal notebook 返回格式异常：gacha_list 不是数组")

    pearpal_by_pool: Dict[str, List[Dict[str, Any]]] = {}
    for row in gacha_list:
        if not isinstance(row, dict):
            continue
        pool_id = str(row.get("card_pool_id", "") or "")
        if pool_id not in GACHA_POOLS:
            continue
        try:
            normalized_row = {
                "card_pool_id": pool_id,
                "pool_cnt": int(row.get("pool_cnt")),
                "result": str(row.get("result", "") or ""),
                "times_from_last_five_stars": int(row.get("times_from_last_five_stars")),
                "times_from_last_four_stars": int(row.get("times_from_last_four_stars")),
                "rarity": str(row.get("rarity", "") or ""),
            }
        except (TypeError, ValueError):
            continue
        pearpal_by_pool.setdefault(pool_id, []).append(normalized_row)

    for rows in pearpal_by_pool.values():
        rows.sort(key=lambda item: (item["pool_cnt"], item["result"]))

    estimated_total_pulls = sum(rows[-1]["pool_cnt"] + 1 for rows in pearpal_by_pool.values() if rows)
    document = {
        "profile": {
            "label": f"Pearpal notebook 摘要 UID {resolved_uid}",
        },
        "pearpal": pearpal_by_pool,
    }
    summary = {
        "uid": resolved_uid,
        "pool_count": sum(1 for rows in pearpal_by_pool.values() if rows),
        "event_count": sum(len(rows) for rows in pearpal_by_pool.values()),
        "estimated_total_pulls": estimated_total_pulls,
        "source_mode": "pearpal_notebook",
        "auth_mode": "momo_token",
    }
    return document, summary


def fetch_pearpal_notebook_summary(
    *,
    uid: Optional[str] = None,
    momo_token: Optional[str] = None,
    momo_id: Optional[str] = None,
    domain: str = CLICKHOUSE_DOMAIN,
    output_path: Optional[str | Path] = None,
    timeout_sec: float = 20.0,
    progress_callback: ProgressCallback = None,
) -> Tuple[Path, Dict[str, Any], Dict[str, Any]]:
    normalized_uid = str(uid or "").strip()
    normalized_momo_token = str(momo_token or "").strip()
    normalized_momo_id = str(momo_id or "").strip()
    if not normalized_momo_token or not normalized_momo_id:
        raise ValueError("抓取 Pearpal notebook 摘要时必须提供 Momo Token 和 Momo ID。")

    config = _get_pearpal_api_config(domain)
    if progress_callback is not None:
        progress_callback(f"Pearpal notebook 认证方式: Momo Token（区域 {config['region_label']}）")
        progress_callback("正在校验 Pearpal 用户信息...")

    user_info = fetch_pearpal_user_info(
        normalized_momo_token,
        normalized_momo_id,
        domain=domain,
        timeout_sec=timeout_sec,
    )
    role = user_info.get("data", {}).get("role", {}) if isinstance(user_info, dict) else {}
    resolved_uid = str(role.get("uid") or normalized_uid or "")
    if not resolved_uid:
        raise ValueError("Pearpal 用户信息返回中缺少 UID。")
    if normalized_uid and normalized_uid != resolved_uid:
        raise ValueError(f"Pearpal 返回的 UID 与输入 UID 不一致: {resolved_uid} != {normalized_uid}")

    if progress_callback is not None:
        progress_callback(f"Pearpal 用户校验成功，UID {resolved_uid}。正在读取 notebook 摘要...")

    notebook_payload = fetch_pearpal_notebook_payload(
        normalized_momo_token,
        normalized_momo_id,
        domain=domain,
        timeout_sec=timeout_sec,
    )
    document, summary = build_pearpal_document(resolved_uid, notebook_payload)
    output = Path(output_path) if output_path else Path(f"nikki-resonance-data-{resolved_uid}-pearpal.json")
    if output.parent and str(output.parent) not in {"", "."}:
        output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    summary["output_path"] = str(output)
    if progress_callback is not None:
        progress_callback(
            f"Pearpal 摘要导出完成：{summary['event_count']} 条事件，估算 {summary['estimated_total_pulls']} 抽，输出文件: {output}"
        )
    return output, document, summary


def fetch_tlog_page(
    jwt: str,
    pool_id: str,
    page: int,
    domain: str = CLICKHOUSE_DOMAIN,
    timeout_sec: float = 20.0,
) -> Dict[str, Any]:
    path = f"/v1/tlog/query?page={page}&args={pool_id}&name=gacha&etime={int(time.time())}"
    return _request_json(
        path,
        domain=domain,
        timeout_sec=timeout_sec,
        extra_headers={"X-Authority": jwt},
    )


def verify_momo_token(
    uid: str,
    momo_token: str,
    momo_id: str,
    *,
    domain: str = CLICKHOUSE_DOMAIN,
    timeout_sec: float = 20.0,
) -> str:
    normalized_uid = str(uid or "").strip()
    normalized_momo_token = str(momo_token or "").strip()
    normalized_momo_id = str(momo_id or "").strip()
    if not normalized_uid or not normalized_momo_token or not normalized_momo_id:
        raise ValueError("使用 verify 时必须同时提供 UID、Momo Token 和 Momo ID。")

    query = urllib.parse.urlencode(
        {
            "token": normalized_momo_token,
            "roleid": normalized_uid,
            "id": normalized_momo_id,
        }
    )
    payload = _request_json(
        f"/v1/tlog/verify?{query}",
        domain=domain,
        timeout_sec=timeout_sec,
    )
    if payload.get("code") != 0:
        raise RuntimeError(f"verify 失败: {payload.get('code')} {payload.get('info')}")

    verified_jwt = str(payload.get("data") or "").strip()
    if not verified_jwt:
        raise ValueError("verify 成功，但没有返回可用的查询令牌。")

    verified_payload = decode_jwt_payload(verified_jwt)
    verified_uid = str(verified_payload.get("sub") or "")
    if verified_uid and verified_uid != normalized_uid:
        raise ValueError(f"verify 返回的 UID 与输入 UID 不一致: {verified_uid} != {normalized_uid}")
    return verified_jwt


def resolve_query_authority(
    jwt: Optional[str] = None,
    *,
    uid: Optional[str] = None,
    momo_token: Optional[str] = None,
    momo_id: Optional[str] = None,
    domain: str = CLICKHOUSE_DOMAIN,
    timeout_sec: float = 20.0,
) -> Tuple[str, Dict[str, Any]]:
    normalized_jwt = str(jwt or "").strip()
    if normalized_jwt:
        payload = decode_jwt_payload(normalized_jwt)
        return normalized_jwt, {
            "uid": str(payload["sub"]),
            "auth_mode": "jwt",
        }

    normalized_uid = str(uid or "").strip()
    normalized_momo_token = str(momo_token or "").strip()
    normalized_momo_id = str(momo_id or "").strip()
    provided_any_momo = any((normalized_uid, normalized_momo_token, normalized_momo_id))
    if provided_any_momo:
        missing = [
            label
            for label, value in (
                ("UID", normalized_uid),
                ("Momo Token", normalized_momo_token),
                ("Momo ID", normalized_momo_id),
            )
            if not value
        ]
        if missing:
            raise ValueError("使用 Momo Token 抓取时缺少字段: " + "、".join(missing))
        verified_jwt = verify_momo_token(
            normalized_uid,
            normalized_momo_token,
            normalized_momo_id,
            domain=domain,
            timeout_sec=timeout_sec,
        )
        payload = decode_jwt_payload(verified_jwt)
        return verified_jwt, {
            "uid": str(payload["sub"]),
            "auth_mode": "momo_verify",
        }

    raise ValueError("请填写 JWT，或同时填写 UID、Momo Token 和 Momo ID。")


def _normalize_page_rows(rows: Iterable[Any]) -> PoolRecords:
    normalized: PoolRecords = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        normalized.append([str(row[0]), int(row[1])])
    return normalized


def _align_with_existing(existing: PoolRecords, current_page: PoolRecords) -> Optional[int]:
    if not existing or not current_page or len(existing) < len(current_page):
        return None
    limit = len(existing) - len(current_page)
    for index in range(limit + 1):
        if existing[index:index + len(current_page)] == current_page:
            return index + len(current_page)
    return None


def fetch_pool_records(
    jwt: str,
    pool_id: str,
    domain: str,
    timeout_sec: float,
    sleep_sec: float,
    existing: Optional[PoolRecords] = None,
) -> PoolRecords:
    records: PoolRecords = []
    page = 1
    while True:
        print(f" - 读取第 {page} 页...")
        payload = fetch_tlog_page(jwt, pool_id, page, domain=domain, timeout_sec=timeout_sec)
        if payload.get("code") != 0:
            raise RuntimeError(f"接口返回失败: {payload.get('code')} {payload.get('info')}")

        page_data = payload.get("data") or {}
        current_page = _normalize_page_rows(page_data.get("datas") or [])
        records.extend(current_page)

        if existing:
            aligned_index = _align_with_existing(existing, current_page)
            if aligned_index is not None:
                records.extend(existing[aligned_index:])
                print(f" - 已和本地缓存对齐，共 {len(records)} 条")
                break

        if page_data.get("end", False):
            print(f" - 读取完毕，共 {len(records)} 条")
            break

        page += 1
        time.sleep(sleep_sec)

    return records


def resolve_force_pool_ids(force_pool_ids: str) -> List[str]:
    result: List[str] = []
    for item in (force_pool_ids or "").split(","):
        pool_id = item.strip()
        if pool_id and pool_id in GACHA_POOLS:
            result.append(pool_id)
    return result


def fetch_all_records(
    jwt: Optional[str] = None,
    force_pool_ids: Optional[Sequence[str]] = None,
    *,
    uid: Optional[str] = None,
    momo_token: Optional[str] = None,
    momo_id: Optional[str] = None,
    domain: str = CLICKHOUSE_DOMAIN,
    output_path: Optional[str | Path] = None,
    sleep_sec: float = 1.0,
    timeout_sec: float = 20.0,
    progress_callback: ProgressCallback = None,
) -> Tuple[Path, StoreData, Dict[str, Any]]:
    if progress_callback is not None:
        if str(jwt or "").strip():
            progress_callback("认证方式: 直接使用 JWT。")
        else:
            progress_callback("认证方式: 使用 Momo Token 先调用 verify，再读取 query。")
            progress_callback("如果未填写 Momo 三件套，请检查 UID、Momo ID、Momo Token 是否完整。")

    query_jwt, auth_summary = resolve_query_authority(
        jwt,
        uid=uid,
        momo_token=momo_token,
        momo_id=momo_id,
        domain=domain,
        timeout_sec=timeout_sec,
    )
    resolved_uid = str(auth_summary["uid"])
    resolved_force_pool_ids = [pool_id for pool_id in (force_pool_ids or []) if pool_id in GACHA_POOLS]
    output = Path(output_path) if output_path else Path(f"gacha-records-{resolved_uid}.json")
    if output.parent and str(output.parent) not in {"", "."}:
        output.parent.mkdir(parents=True, exist_ok=True)

    if progress_callback is not None:
        progress_callback(f"查询结果将保存到 {output}")
        progress_callback(
            "强制刷新: "
            + (", ".join(GACHA_POOLS[pool_id] for pool_id in resolved_force_pool_ids) if resolved_force_pool_ids else "(无)")
        )

    result = _read_store_file(output)
    for pool_id, pool_name in GACHA_POOLS.items():
        if pool_id in result and pool_id not in resolved_force_pool_ids:
            if progress_callback is not None:
                progress_callback(f"数据已存在，跳过查询: {pool_name}")
            continue
        if progress_callback is not None:
            progress_callback(f"正在{'刷新' if pool_id in result else '查询'}: {pool_name}")
        result[pool_id] = fetch_pool_records(
            query_jwt,
            pool_id,
            domain=domain,
            timeout_sec=timeout_sec,
            sleep_sec=sleep_sec,
            existing=result.get(pool_id),
        )
        _write_store_file(output, result)

    total = sum(len(rows) for rows in result.values())
    summary = {
        "uid": resolved_uid,
        "total_records": total,
        "pool_count": sum(1 for rows in result.values() if rows),
        "output_path": str(output),
        "auth_mode": auth_summary["auth_mode"],
    }
    if progress_callback is not None:
        progress_callback(f"全部卡池处理完成，总记录数 {total}，输出文件: {output}")
    return output, result, summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    force_pool_ids = resolve_force_pool_ids(args.force_pool_ids)

    def _printer(message: str):
        print(message)

    if args.fetch_pearpal:
        fetch_pearpal_notebook_summary(
            uid=args.uid or None,
            momo_token=args.momo_token or None,
            momo_id=args.momo_id or None,
            domain=args.domain,
            output_path=args.output,
            timeout_sec=args.timeout_sec,
            progress_callback=_printer,
        )
    else:
        fetch_all_records(
            args.jwt or None,
            force_pool_ids,
            uid=args.uid or None,
            momo_token=args.momo_token or None,
            momo_id=args.momo_id or None,
            domain=args.domain,
            output_path=args.output,
            sleep_sec=args.sleep_sec,
            timeout_sec=args.timeout_sec,
            progress_callback=_printer,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())