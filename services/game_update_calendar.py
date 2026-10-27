"""米哈游游戏更新公告日历。

从官方公告接口拉取原神（国服）/ 崩铁 / 绝区零的最新版本更新公告，
解析维护开始时间和预计时长，算出版本开服时间。

- 原神国服：hk4e-api.mihoyo.com/common/hk4e_cn/announcement/api/
- 崩铁：sg-hkrpg-api.hoyoverse.com/common/hkrpg_global/announcement/api/（lang=zh-cn）
- 绝区零：sg-announcement-static.hoyoverse.com/common/nap_global/announcement/api/（lang=zh-cn）

公告时间均为 UTC+8。缓存写到 storage/cache/game_update_calendar.json，
默认 6 小时刷新；网络失败返回旧缓存，没有缓存则返回 None，
调用方回退到固定补跑任务。
"""

import html
import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..settings.paths import CACHE_DIR

logger = logging.getLogger(__name__)

CACHE_FILE = CACHE_DIR / "game_update_calendar.json"
UPDATE_RETRY_FILE = CACHE_DIR / "update_retries.json"
CACHE_TTL_SEC = 6 * 3600
REQUEST_TIMEOUT_SEC = 12
OPEN_BUFFER_MIN = 5
CN_TZ = timezone(timedelta(hours=8))

_SOURCES: Dict[str, Dict[str, Any]] = {
    "genshin": {
        "kind": "mihoyo",
        "base": "https://hk4e-api.mihoyo.com/common/hk4e_cn/announcement/api/",
        "params": {
            "game": "hk4e",
            "game_biz": "hk4e_cn",
            "lang": "zh-cn",
            "bundle_id": "hk4e_cn",
            "platform": "pc",
            "region": "cn_gf01",
            "level": "60",
            "uid": "100000000",
        },
        "referer": "https://www.miyoushe.com/",
    },
    "hsr": {
        "kind": "mihoyo",
        "base": "https://sg-hkrpg-api.hoyoverse.com/common/hkrpg_global/announcement/api/",
        "params": {
            "game": "hkrpg",
            "game_biz": "hkrpg_global",
            "bundle_id": "hkrpg_global",
            "platform": "pc",
            "region": "prod_official_usa",
            "level": "70",
            "uid": "600000000",
            "channel_id": "1",
            "lang": "zh-cn",
        },
        "referer": "https://www.hoyolab.com/",
    },
    "zzz": {
        "kind": "mihoyo",
        "base": "https://sg-announcement-static.hoyoverse.com/common/nap_global/announcement/api/",
        "params": {
            "game": "nap",
            "game_biz": "nap_global",
            "bundle_id": "nap_global",
            "platform": "pc",
            "region": "prod_gf_us",
            "level": "60",
            "uid": "100000000",
            "lang": "zh-cn",
        },
        "referer": "https://www.hoyolab.com/",
    },
    # 鸣潮官方 CMS：ArticleMenu.json 列出文章，article/<id>.json 是正文；
    # 正文含「更新维护时间：2026年9月30日04:00 ~ 2026年9月30日11:00（UTC+8）」
    "wuwa": {
        "kind": "wuwa_cms",
        "base": "https://media-cdn-mingchao.kurogame.com/akiwebsite/website2.0/json/G152/zh/",
        "referer": "https://mc.kurogames.com/",
    },
    # 异环官网公告列表是静态 HTML，des 字段直接带「6:00 - 11:00」维护时间
    "nte": {
        "kind": "nte_html",
        "base": "https://yh.wanmei.com/m/news/gamebroad/index.html",
        "referer": "https://yh.wanmei.com/",
    },
    # 无限暖暖：/api/news 列表拿公告 id，/news/<id> 详情页是服务端渲染，
    # 正文含「2026年10月12日10:00-11:00（UTC+8）进行停服维护更新」
    "nikki": {
        "kind": "nikki_news",
        "base": "https://infinitynikki.nuanpaper.com/api/news",
        "page_base": "https://infinitynikki.nuanpaper.com/news/",
        "referer": "https://infinitynikki.nuanpaper.com/news",
    },
}

APP_KEY_TO_GAME = {
    "GenshinImpact": "genshin",
    "genshin": "genshin",
    "HonkaiStarRail": "hsr",
    "hsr": "hsr",
    "ZenlessZoneZero": "zzz",
    "zzz": "zzz",
    "WutheringWaves": "wuwa",
    "wuwa": "wuwa",
    "NevernessToEverness": "nte",
    "nte": "nte",
    "InfinityNikki": "nikki",
    "nikki": "nikki",
}

# 公告写法实测：
#   原神：〓更新时间〓 2026/09/23 06:00 开始，预计5个小时完成
#   崩铁：■更新时间 2026/09/28 06:00:00 开始，预计5个小时完成
#   绝区零 3.2：【更新开始时间】 2026/09/09 06:00（UTC+8） 预计5个小时完成
#   绝区零 3.3：【版本更新时间】 2026/10/21 06:00 (UTC+8) 开始，预计需要5个小时
_RE_UPDATE_TIME = re.compile(
    r"(?:版本)?(?:更新开始时间|更新时间)\s*[】\]」』〓]?\s*[：:]?\s*"
    r"(\d{4}/\d{1,2}/\d{1,2})\s+(\d{1,2}:\d{2})(?::\d{2})?\s*"
    r"(?:[（(]\s*UTC\+8\s*[）)])?\s*(?:开始[，,]?\s*)?"
    r"预计(?:需要)?\s*(\d+)\s*个小时"
)
_RE_UPDATE_TIME_NO_HOURS = re.compile(
    r"(?:版本)?(?:更新开始时间|更新时间)\s*[】\]」』〓]?\s*[：:]?\s*"
    r"(\d{4}/\d{1,2}/\d{1,2})\s+(\d{1,2}:\d{2})(?::\d{2})?\s*"
    r"(?:[（(]\s*UTC\+8\s*[）)])?\s*开始"
)
_RE_RANGE = re.compile(
    r"维护时间\s*[】\]」』〓]?\s*[：:]?\s*"
    r"(\d{4}/\d{1,2}/\d{1,2})\s+(\d{1,2}:\d{2})\s*"
    r"[~～\-—至]\s*(?:(\d{4}/\d{1,2}/\d{1,2})\s+)?(\d{1,2}:\d{2})"
)
_RE_VERSION = re.compile(r"(\d+\.\d+)")
# 鸣潮：更新维护时间： 2026年9月30日04:00 ~ 2026年9月30日11:00（UTC+8）
_RE_WUWA_TIME = re.compile(
    r"更新维护时间[：:]\s*"
    r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})\s*"
    r"[~～\-—至]\s*"
    r"(?:(\d{4})年)?(?:(\d{1,2})月)?(?:(\d{1,2})日)?\s*(\d{1,2}):(\d{2})"
)
# 异环公告列表 des：将于10月15日06:00 - 11:00进行停服维护
_RE_NTE_TIME = re.compile(
    r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})\s*"
    r"[-–—~～至]\s*(\d{1,2}):(\d{2})"
)
# 无限暖暖详情：2026年10月12日10:00-11:00（UTC+8）进行停服维护更新
_RE_NIKKI_TIME = re.compile(
    r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})\s*"
    r"[-–—~～至]\s*(\d{1,2}):(\d{2})\s*[（(]?\s*UTC\+8"
)

_LOCK = threading.RLock()
_MEM_CACHE: Optional[Dict[str, Any]] = None


def _normalize_app_key(app_key: str) -> Optional[str]:
    if not app_key:
        return None
    return APP_KEY_TO_GAME.get(str(app_key)) or APP_KEY_TO_GAME.get(str(app_key).lower())


def _strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_dt(date_str: str, time_str: str) -> datetime:
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M")
    return dt.replace(tzinfo=CN_TZ)


def _parse_update_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = _strip_html(str(item.get("title") or ""))
    content = _strip_html(str(item.get("content") or ""))
    if not content:
        return None
    # 只认版本更新类公告；活动/修复说明即使出现“更新时间”也不当版本维护
    if not any(k in title for k in ("版本更新", "更新说明", "更新公告", "更新通知")):
        return None

    start = open_at = None
    hours = 0
    m = _RE_UPDATE_TIME.search(content)
    if m:
        start = _parse_dt(m.group(1), m.group(2))
        hours = int(m.group(3))
        open_at = start + timedelta(hours=hours)
    else:
        m = _RE_RANGE.search(content)
        if m:
            start = _parse_dt(m.group(1), m.group(2))
            end_date = m.group(3) or m.group(1)
            end = _parse_dt(end_date, m.group(4))
            if end <= start:
                end += timedelta(days=1)
            open_at = end
            hours = max(1, int(round((end - start).total_seconds() / 3600)))
        else:
            m = _RE_UPDATE_TIME_NO_HOURS.search(content)
            if not m:
                return None
            start = _parse_dt(m.group(1), m.group(2))
            hours = 5
            open_at = start + timedelta(hours=hours)

    version = ""
    vm = _RE_VERSION.search(title) or _RE_VERSION.search(content)
    if vm:
        version = vm.group(1)
    return {
        "version": version,
        "title": title,
        "maintenance_start": start.isoformat(timespec="seconds"),
        "estimated_hours": hours,
        "open_at": open_at.isoformat(timespec="seconds"),
        "ann_id": item.get("ann_id"),
    }


def _select_latest(infos: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not infos:
        return None
    infos.sort(
        key=lambda i: (
            i.get("maintenance_start") or "",
            1 if ("更新说明" in i.get("title", "") or "更新公告" in i.get("title", "")) else 0,
        ),
        reverse=True,
    )
    return infos[0]


def _http_get(url: str, referer: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": referer,
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        return resp.read()


def _fetch_json(url: str, referer: str) -> Any:
    return json.loads(_http_get(url, referer).decode("utf-8", errors="replace"))


def _fetch_text(url: str, referer: str) -> str:
    return _http_get(url, referer).decode("utf-8", errors="replace")


def _fetch_mihoyo(game_key: str, src: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = src["base"] + "getAnnContent?" + urllib.parse.urlencode(src["params"])
    data = _fetch_json(url, src["referer"])
    if data.get("retcode") != 0:
        raise RuntimeError(f"retcode={data.get('retcode')} message={data.get('message')}")
    items = (data.get("data") or {}).get("list") or []
    infos = []
    for item in items:
        try:
            info = _parse_update_item(item)
        except Exception:
            continue
        if info:
            infos.append(info)
    return _select_latest(infos)


def _fetch_wuwa(src: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    menu = _fetch_json(src["base"] + "ArticleMenu.json", src["referer"])
    candidates = []
    for item in menu or []:
        title = _strip_html(str(item.get("articleTitle") or ""))
        if "更新维护预告" in title or "维护预告" in title:
            candidates.append(item)
    if not candidates:
        return None
    latest = max(
        candidates,
        key=lambda a: (str(a.get("createTime") or ""), int(a.get("articleId") or 0)),
    )
    ann_id = int(latest.get("articleId") or 0)
    detail = _fetch_json(f"{src['base']}article/{ann_id}.json", src["referer"])
    title = _strip_html(str(detail.get("articleTitle") or latest.get("articleTitle") or ""))
    content = _strip_html(
        str(detail.get("articleContent") or latest.get("articleContent") or "")
    )
    m = _RE_WUWA_TIME.search(content)
    if not m:
        return None
    start = datetime(
        int(m.group(1)), int(m.group(2)), int(m.group(3)),
        int(m.group(4)), int(m.group(5)), tzinfo=CN_TZ,
    )
    end = datetime(
        int(m.group(6) or m.group(1)), int(m.group(7) or m.group(2)),
        int(m.group(8) or m.group(3)), int(m.group(9)), int(m.group(10)),
        tzinfo=CN_TZ,
    )
    if end <= start:
        end += timedelta(days=1)
    hours = max(1, int(round((end - start).total_seconds() / 3600)))
    vm = _RE_VERSION.search(title)
    return {
        "version": vm.group(1) if vm else "",
        "title": title,
        "maintenance_start": start.isoformat(timespec="seconds"),
        "estimated_hours": hours,
        "open_at": end.isoformat(timespec="seconds"),
        "ann_id": ann_id,
    }


def _fetch_nte(src: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    html_text = _fetch_text(src["base"], src["referer"])
    rows = re.findall(
        r'<a href="([^"]+)">\s*<div class="listItem">.*?'
        r'<h2 class="title">(.*?)</h2>.*?'
        r'<div class="des">(.*?)</div>.*?'
        r'<p class="date">(.*?)</p>',
        html_text,
        re.S,
    )
    candidates = []
    for href, title_html, desc_html, date_html in rows:
        title = _strip_html(title_html)
        desc = _strip_html(desc_html)
        date_str = _strip_html(date_html)
        if "不停服" in title or "不停服" in desc:
            continue
        if not (any(k in title for k in ("停服维护", "版本更新维护", "更新维护", "更新公告"))
                or "维护" in desc):
            continue
        m = _RE_NTE_TIME.search(desc)
        if not m:
            continue
        try:
            year = int(date_str[:4]) if len(date_str) >= 4 else datetime.now(CN_TZ).year
            start = datetime(
                year, int(m.group(1)), int(m.group(2)),
                int(m.group(3)), int(m.group(4)), tzinfo=CN_TZ,
            )
            end = start.replace(hour=int(m.group(5)), minute=int(m.group(6)))
            if end <= start:
                end += timedelta(days=1)
        except Exception:
            continue
        vm = _RE_VERSION.search(title)
        candidates.append({
            "version": vm.group(1) if vm else "",
            "title": title,
            "maintenance_start": start.isoformat(timespec="seconds"),
            "estimated_hours": max(1, int(round((end - start).total_seconds() / 3600))),
            "open_at": end.isoformat(timespec="seconds"),
            "ann_id": href,
            "date_key": date_str,
        })
    if not candidates:
        return None
    latest = max(
        candidates,
        key=lambda i: (i.get("date_key", ""), i.get("maintenance_start", "")),
    )
    latest.pop("date_key", None)
    return latest


def _fetch_nikki(src: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = _fetch_json(src["base"] + "?offset=0&limit=30", src["referer"])
    items = ((data.get("data") or {}).get("data") or [])
    candidates = []
    for item in items:
        title = _strip_html(str(item.get("title") or ""))
        if any(k in title for k in ("更新完成", "更新结束")):
            continue
        if not any(k in title for k in ("更新预告", "停服维护", "更新公告", "维护公告")):
            continue
        candidates.append(item)
    if not candidates:
        return None
    latest = max(candidates, key=lambda i: str(i.get("publish_time") or ""))
    news_id = latest.get("id")
    if news_id is None:
        return None
    page = _fetch_text(f"{src['page_base']}{news_id}", src["referer"])
    text = _strip_html(page)
    m = _RE_NIKKI_TIME.search(text)
    if not m:
        return None
    start = datetime(
        int(m.group(1)), int(m.group(2)), int(m.group(3)),
        int(m.group(4)), int(m.group(5)), tzinfo=CN_TZ,
    )
    end = start.replace(hour=int(m.group(6)), minute=int(m.group(7)))
    if end <= start:
        end += timedelta(days=1)
    title = _strip_html(str(latest.get("title") or ""))
    vm = _RE_VERSION.search(title) or _RE_VERSION.search(text)
    return {
        "version": vm.group(1) if vm else "",
        "title": title,
        "maintenance_start": start.isoformat(timespec="seconds"),
        "estimated_hours": max(1, int(round((end - start).total_seconds() / 3600))),
        "open_at": end.isoformat(timespec="seconds"),
        "ann_id": news_id,
    }


def _fetch_game(game_key: str) -> Optional[Dict[str, Any]]:
    src = _SOURCES[game_key]
    kind = src.get("kind", "mihoyo")
    if kind == "wuwa_cms":
        info = _fetch_wuwa(src)
    elif kind == "nte_html":
        info = _fetch_nte(src)
    elif kind == "nikki_news":
        info = _fetch_nikki(src)
    else:
        info = _fetch_mihoyo(game_key, src)
    if info:
        info["source"] = src["base"]
        logger.info(
            "[更新日历] %s: %s %s 开始，预计 %sh，开服 %s",
            game_key,
            info.get("title"),
            info.get("maintenance_start"),
            info.get("estimated_hours"),
            info.get("open_at"),
        )
    return info


def _load_cache() -> Dict[str, Any]:
    global _MEM_CACHE
    if _MEM_CACHE is not None:
        return _MEM_CACHE
    try:
        if CACHE_FILE.exists():
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _MEM_CACHE = data
                return data
    except Exception as e:
        logger.warning("[更新日历] 读取缓存失败: %s", e)
    _MEM_CACHE = {"fetched_at": "", "games": {}}
    return _MEM_CACHE


def _save_cache(cache: Dict[str, Any]) -> None:
    global _MEM_CACHE
    _MEM_CACHE = cache
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception as e:
        logger.warning("[更新日历] 写缓存失败: %s", e)


def _cache_fresh(cache: Dict[str, Any], now: datetime) -> bool:
    try:
        fetched = datetime.fromisoformat(str(cache.get("fetched_at") or ""))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=CN_TZ)
        return (now - fetched).total_seconds() < CACHE_TTL_SEC
    except Exception:
        return False


def _with_dt(info: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(info)
    for key in ("maintenance_start", "open_at"):
        try:
            dt = datetime.fromisoformat(str(out.get(key) or ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CN_TZ)
            out[f"{key}_dt"] = dt
        except Exception:
            out[f"{key}_dt"] = None
    return out


def get_update_info(
    app_key: str,
    *,
    now: Optional[datetime] = None,
    refresh_if_stale: bool = True,
) -> Optional[Dict[str, Any]]:
    """返回某游戏最近一次版本更新的信息（带 *_dt 字段）。"""
    game = _normalize_app_key(app_key)
    if not game:
        return None
    now = now or datetime.now(CN_TZ)
    with _LOCK:
        cache = _load_cache()
        info = (cache.get("games") or {}).get(game)
        if info and _cache_fresh(cache, now):
            return _with_dt(info)
        if not refresh_if_stale:
            return _with_dt(info) if info else None
        try:
            fresh = _fetch_game(game)
            if fresh:
                cache.setdefault("games", {})[game] = fresh
                # 用真实拉取时间，不能用调用方传入的 now（测试/模拟时间会写到未来）
                cache["fetched_at"] = datetime.now(CN_TZ).isoformat(timespec="seconds")
                _save_cache(cache)
                info = fresh
        except Exception as e:
            logger.warning("[更新日历] 拉取 %s 公告失败（使用旧缓存）: %s", game, e)
        return _with_dt(info) if info else None


def get_maintenance_state(
    app_key: str,
    *,
    now: Optional[datetime] = None,
    buffer_min: int = OPEN_BUFFER_MIN,
) -> Optional[Dict[str, Any]]:
    """当前是否在该游戏最近一次维护窗口内。返回带开服时间的状态或 None。"""
    info = get_update_info(app_key, now=now)
    if not info:
        return None
    start = info.get("maintenance_start_dt")
    open_at = info.get("open_at_dt")
    if not start or not open_at:
        return None
    now = now or datetime.now(CN_TZ)
    # 开服时间 + buffer 用严格小于：11:00 开服、buffer=5 时 11:05 视为已开服，
    # 避免 11:05 的补跑又被维护检查跳过。
    if start - timedelta(minutes=buffer_min) <= now < open_at + timedelta(minutes=buffer_min):
        return {
            "under_maintenance": True,
            "version": info.get("version") or "",
            "title": info.get("title") or "",
            "maintenance_start": info.get("maintenance_start"),
            "open_at": info.get("open_at"),
            "open_at_dt": open_at,
        }
    return None


def is_under_maintenance(app_key: str, *, now: Optional[datetime] = None) -> bool:
    return bool(get_maintenance_state(app_key, now=now))


def refresh_all() -> Dict[str, Any]:
    """强制刷新三款游戏公告，返回缓存内容。GUI 启动时可后台预热。"""
    now = datetime.now(CN_TZ)
    with _LOCK:
        cache = _load_cache()
        cache.setdefault("games", {})
        for game in _SOURCES:
            try:
                info = _fetch_game(game)
                if info:
                    cache["games"][game] = info
            except Exception as e:
                logger.warning("[更新日历] 刷新 %s 失败: %s", game, e)
        cache["fetched_at"] = now.isoformat(timespec="seconds")
        _save_cache(cache)
        return cache
