"""限定池歪率 / 大保底链条 / 高稀有度出货预测的通用纯函数。

定位：gacha 分析层的"推断性统计"补充。hoyo_analysis / wuwa_analysis 的
build_overview 系列只回答"出了多少金、平均保底几抽"（描述性统计），本模块
回答三个玩家真正关心的问题：
    1) UP 池的 50/50 打赢了没 —— 歪率统计
    2) 下一个金是不是必中       —— 大保底状态
    3) 距离下一个金还差几抽     —— 保底预测（期望 + 最坏上界）

数据流：纯函数，输入是已导入的标准化记录（与各 *_analysis 模块同源），
不碰 IO / 数据库。调用方：
    - ui/bridge.py 的抽卡分析出口（gacha_analysis / wuwa_gacha 事件）
    - artifacts 回归测试直接喂合成记录

覆盖范围（只做机制明确、常量可靠的部分，其余宁缺毋滥不编数据）：
    - hk4e 原神：歪率限"角色活动祈愿"(301/400)，常驻五星名单复用
      genshin_analysis.STANDARD_FIVE_STAR_CHARACTERS
    - hkrpg 星铁：歪率限"角色活动跃迁"(11)
    - wuwa 鸣潮：歪率限"限定角色池/角色联动池"
    - 保底预测覆盖所有 pity_group 有常量的池（含武器/光锥/常驻）
    - 终末地 / 暖暖：无常驻高稀有度名单与可靠保底常量，不做

保底模型说明：硬保底（必出）是官方机制，精确；软保底概率用社区校准的
hazard 近似——基础概率到 soft_start-1，之后每抽线性递增，硬保底必中。
因此 expected_pulls 是估算值（误差 ±2 抽量级），worst_case（硬保底距离）
是精确上界。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

# ── 常驻高稀有度名单（歪/中判定基准）─────────────────────────────────
# 复用原神既有名单（genshin_analysis 已在用，避免两处漂移）
from .genshin_analysis import STANDARD_FIVE_STAR_CHARACTERS as GENSHIN_STANDARD_CHARS

# 崩铁常驻五星角色（开服七人，官方固定，后续新常驻不入池不影响已有判定）
HSR_STANDARD_CHARS = {
    "瓦尔特", "布洛妮娅", "杰帕德", "克拉拉", "姬子", "白露", "彦卿",
}

# 鸣潮常驻五星共鸣者（官方固定五人）
WUWA_STANDARD_CHARS = {
    "维里奈", "卡卡罗", "安可", "鉴心", "凌阳",
}

# ── 歪率统计的池范围（仅限定角色池，50/50 机制明确）──────────────────
# hk4e：301=角色活动祈愿，400=角色活动祈愿-2（同一祈愿类型的两期，歪/中
# 状态跨期继承，所以合并成一条时间线，与 pity_groups 里 400→301 同理）
HOYO_CHAR_EVENT_TYPES = {
    "hk4e": {"301", "400"},
    "hkrpg": {"11"},
}
HOYO_CHAR_ITEM_TYPE = "角色"

# wuwa：限定角色池 + 角色联动池（同为 UP 池）；新旅/忆旅/新手池机制特殊不碰
WUWA_CHAR_EVENT_POOLS = {"限定角色池", "角色联动池"}
WUWA_CHAR_ITEM_TYPE = "角色"

# ── 保底机制模型（pity_group → 参数）────────────────────────────────
# 每组: hard=硬保底(第 hard 抽必出), soft=软保底起始抽序, first=软保底首抽
# 概率, ramp=此后每抽递增量, base=基础概率。抽序 n 从 1 计（距上次出货第
# n 抽）：n < soft → base；soft <= n < hard → min(1, first + ramp*(n-soft))；
# n >= hard → 1.0。未列出的组（新手池等机制特殊）不参与预测。
PITY_MODELS: Dict[str, Dict[str, Dict[str, float]]] = {
    "hk4e": {
        "301": {"hard": 90, "soft": 74, "first": 0.06, "ramp": 0.06, "base": 0.006},
        "200": {"hard": 90, "soft": 74, "first": 0.06, "ramp": 0.06, "base": 0.006},
        "302": {"hard": 80, "soft": 63, "first": 0.06, "ramp": 0.06, "base": 0.007},
        "500": {"hard": 90, "soft": 74, "first": 0.06, "ramp": 0.06, "base": 0.006},
    },
    "hkrpg": {
        "11": {"hard": 90, "soft": 74, "first": 0.06, "ramp": 0.06, "base": 0.006},
        "1": {"hard": 90, "soft": 74, "first": 0.06, "ramp": 0.06, "base": 0.006},
        "12": {"hard": 80, "soft": 66, "first": 0.06, "ramp": 0.06, "base": 0.008},
    },
    "wuwa": {
        # 鸣潮角色/武器池机制相同：硬保底 80，基础 0.8%，软保底 ~66 起
        "*": {"hard": 80, "soft": 66, "first": 0.075, "ramp": 0.075, "base": 0.008},
    },
}

# 歪率 headline 取哪个预测组：限定角色池的 pity_group 代码
CHAR_PREDICTION_GROUP = {"hk4e": "301", "hkrpg": "11"}

# wuwa 预测覆盖的池（新手/新旅/感恩定向机制特殊，排除）
WUWA_PREDICTION_POOLS = {
    "限定角色池", "角色联动池", "限定武器池", "武器联动池",
    "常驻角色池", "常驻武器池",
}


def _pull_rate(pull_index: int, model: Dict[str, float]) -> float:
    """距上次出货第 pull_index 抽（1 基）出高稀有度的概率。"""
    hard = int(model["hard"])
    if pull_index >= hard:
        return 1.0
    soft = int(model["soft"])
    if pull_index < soft:
        return float(model["base"])
    rate = float(model["first"]) + float(model["ramp"]) * (pull_index - soft)
    return min(1.0, rate)


def expected_pulls_to_next(current_pity: int, model: Dict[str, float]) -> float:
    """hazard 模型下，从当前保底计数到下一个高稀有度的期望抽数。

    current_pity 是已垫的抽数（0 基）。返回值估算自软保底近似模型，
    硬保底端点精确（垫到 hard-1 时期望恰为 1）。
    """
    hard = int(model["hard"])
    remaining = hard - int(current_pity)
    if remaining <= 0:
        return 0.0
    expected = 0.0
    survive = 1.0
    for k in range(1, remaining + 1):
        p = _pull_rate(int(current_pity) + k, model)
        expected += survive * k * p
        survive *= (1.0 - p)
        if survive <= 0.0:
            break
    # 浮点兜底：残余概率按走满硬保底计（正常应恒为 0）
    expected += survive * remaining
    return expected


def _predict_rows(
    current_pities: Dict[str, int],
    group_labels: Dict[str, str],
    models: Dict[str, Dict[str, float]],
    sort_order: Dict[str, int] | None = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group_code, model in models.items():
        # 无记录的组按 0 垫展示（UI 未导入数据时也能看到"期望 N 抽"）
        pity = int(current_pities.get(group_code, 0))
        hard = int(model["hard"])
        rows.append(
            {
                "group": group_code,
                "group_label": group_labels.get(group_code, group_code),
                "current_pity": pity,
                "hard_pity": hard,
                "worst_case": max(0, hard - pity),
                "expected_pulls": round(expected_pulls_to_next(pity, model), 1),
                "sort_key": (sort_order or {}).get(group_code, 9999),
            }
        )
    rows.sort(key=lambda item: (item["sort_key"], item["group_label"]))
    for item in rows:
        item.pop("sort_key", None)
    return rows


def _luck_from_five_stars(
    five_star_events: List[Dict[str, Any]],
    standard_names: Set[str],
    pool_label: str,
) -> Dict[str, Any]:
    """从按时间排序的限定角色池五星事件列表计算歪率 / 大保底链条。

    每个 event: {"name", "time", "pity"}。歪 = 命中常驻名单；
    大保底链条 = 上一个歪后必中的那个 UP（机制保证，仅计数供展示）。
    """
    total = len(five_star_events)
    up_events: List[Dict[str, Any]] = []
    lost_events: List[Dict[str, Any]] = []
    guaranteed_won = 0
    prev_lost = False
    for event in five_star_events:
        is_up = str(event.get("name", "")) not in standard_names
        event = dict(event)
        event["is_up"] = is_up
        if is_up:
            up_events.append(event)
            if prev_lost:
                guaranteed_won += 1
        else:
            lost_events.append(event)
        prev_lost = not is_up

    if total > 0:
        last = five_star_events[-1]
        current_guaranteed = str(last.get("name", "")) in standard_names
        state_text = "大保底·下次必中" if current_guaranteed else "小保底"
        win_rate = round(len(up_events) * 100.0 / total, 1)
        win_rate_text = f"{win_rate:.1f}%"
    else:
        current_guaranteed = None
        state_text = "—"
        win_rate = None
        win_rate_text = "-"

    return {
        "pool_label": pool_label,
        "total_5star": total,
        "up_count": len(up_events),
        "lost_count": len(lost_events),
        "win_rate": win_rate,
        "win_rate_text": win_rate_text,
        "guaranteed_won": guaranteed_won,
        "current_guaranteed": current_guaranteed,
        "state_text": state_text,
        "last_5star_name": str(five_star_events[-1].get("name", "")) if five_star_events else "",
        "last_5star_time": str(five_star_events[-1].get("time", "")) if five_star_events else "",
    }


def build_hoyo_luck(records: List[Dict[str, Any]], game_code: str) -> Dict[str, Any]:
    """米哈游系（hk4e/hkrpg）的歪率 + 大保底 + 保底预测汇总。

    records 是 DB 里的标准化记录 dict（gacha_type/rank_type/name/item_type/
    time/count），排序与 pity 走格规则与 hoyo_analysis 一致（count 增量、
    pity_groups 分组）。
    """
    from .hoyo_importer import get_banner_name, get_game_config

    ordered = sorted(
        records,
        key=lambda record: (str(record.get("time", "")), str(record.get("record_id", ""))),
    )
    high_rank = str(get_game_config(game_code).get("high_rarity_rank", "5") or "5")
    pity_groups: Dict[str, str] = dict(get_game_config(game_code).get("pity_groups", {}))

    # ── 保底预测：按 pity_group 走格（与 build_pity_summary 同规则）──
    models = PITY_MODELS.get(str(game_code), {})
    pity_counters: Dict[str, int] = defaultdict(int)
    group_labels: Dict[str, str] = {}
    # 预填所有参与预测的组标签：无记录的池也要显示"角色活动祈愿"而非 "301"
    for group_code in models:
        group_labels[group_code] = get_banner_name(game_code, group_code)
    char_events: List[Dict[str, Any]] = []
    char_types = HOYO_CHAR_EVENT_TYPES.get(str(game_code), set())
    standard = GENSHIN_STANDARD_CHARS if str(game_code) == "hk4e" else HSR_STANDARD_CHARS

    for record in ordered:
        gacha_type = str(record.get("gacha_type", "") or "")
        group_code = str(pity_groups.get(gacha_type, gacha_type or ""))
        if group_code in models:
            group_labels[group_code] = get_banner_name(game_code, gacha_type)
            pity_counters[group_code] += int(record.get("count", 1) or 1)
        if (
            gacha_type in char_types
            and str(record.get("item_type", "") or "") == HOYO_CHAR_ITEM_TYPE
            and str(record.get("rank_type", "") or "") == high_rank
        ):
            # 保底计数器重置前先取值，记录该金实际垫的抽数
            drop_pity = pity_counters.get(group_code, 0) if group_code in models else 0
            char_events.append(
                {
                    "name": str(record.get("name", "") or ""),
                    "time": str(record.get("time", "") or ""),
                    "pity": int(drop_pity),
                }
            )
            if group_code in models:
                pity_counters[group_code] = 0

    prediction_rows = _predict_rows(pity_counters, group_labels, models)
    up_luck = _luck_from_five_stars(char_events, standard, "限定角色池") if char_types else None
    return {
        "up_luck": up_luck,
        "prediction": prediction_rows,
        "headline": _headline(up_luck, prediction_rows, CHAR_PREDICTION_GROUP.get(str(game_code))),
    }


def _headline(
    up_luck: Optional[Dict[str, Any]],
    prediction_rows: List[Dict[str, Any]],
    group_code: Optional[str],
) -> Optional[Dict[str, Any]]:
    """把歪率 + 限定角色池预测拼成 QML 一张卡直用的扁平对象。"""
    if not group_code:
        return None
    row = next((item for item in prediction_rows if item.get("group") == group_code), None)
    if row is None:
        return None
    headline = {
        "group_label": row["group_label"],
        "current_pity": row["current_pity"],
        "expected_pulls": row["expected_pulls"],
        "worst_case": row["worst_case"],
        "win_rate_text": (up_luck or {}).get("win_rate_text", "-"),
        "state_text": (up_luck or {}).get("state_text", "—"),
    }
    return headline


def build_wuwa_luck(records: List[Any]) -> Dict[str, Any]:
    """鸣潮的歪率 + 大保底 + 保底预测汇总。

    records 是 WuwaGachaRecord 列表；走格用 quantity，与
    build_wuwa_pool_analysis 的保底口径一致。
    """
    ordered = sorted(records, key=lambda record: str(getattr(record, "time_str", "")))
    # PITY_MODELS["wuwa"] 用通配键 "*"（全池同机制），预测按具体池名展开
    wildcard = PITY_MODELS.get("wuwa", {}).get("*")
    models = {pool: wildcard for pool in WUWA_PREDICTION_POOLS} if wildcard else {}

    pity_counters: Dict[str, int] = defaultdict(int)
    group_labels: Dict[str, str] = {}
    char_events: List[Dict[str, Any]] = []

    for record in ordered:
        pool_display = str(getattr(record, "pool_display", "") or "")
        quantity = int(getattr(record, "quantity", 1) or 1)
        if pool_display in WUWA_PREDICTION_POOLS:
            group_labels[pool_display] = pool_display
            pity_counters[pool_display] += quantity
        if (
            pool_display in WUWA_CHAR_EVENT_POOLS
            and str(getattr(record, "item_type", "") or "") == WUWA_CHAR_ITEM_TYPE
            and int(getattr(record, "rarity", 0) or 0) == 5
        ):
            drop_pity = pity_counters.get(pool_display, 0) if pool_display in WUWA_PREDICTION_POOLS else 0
            char_events.append(
                {
                    "name": str(getattr(record, "name", "") or ""),
                    "time": str(getattr(record, "time_str", "") or ""),
                    "pity": int(drop_pity),
                }
            )
            if pool_display in WUWA_PREDICTION_POOLS:
                pity_counters[pool_display] = 0

    prediction_rows = _predict_rows(pity_counters, group_labels, models)
    up_luck = (
        _luck_from_five_stars(char_events, WUWA_STANDARD_CHARS, "限定角色池")
        if WUWA_CHAR_EVENT_POOLS
        else None
    )
    return {
        "up_luck": up_luck,
        "prediction": prediction_rows,
        "headline": _headline(up_luck, prediction_rows, "限定角色池"),
    }
