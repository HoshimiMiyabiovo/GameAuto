"""绝区零角色状态条识别（移植自 OneDragon，2026-11-01）。

照抄来源：``zzz_od/auto_battle/agent_state/agent_state_checker.py`` 的
``check_length_by_foreground_color``：裁剪状态条区域 → 按 HSV 前景色过滤 →
取所有命中像素的**外接矩形宽度** → 按 ``宽度 / 条宽 * max_length`` 换算成数值。

为什么需要它：这支配队（蕾米埃尔 / 菲欧妮 / 维琳娜）都是异常角色，各自的爆发
都挂在一条"资源条"上，不懂条就打不出伤害：

| 角色 | 状态条 | 满值 | 关键阈值 | 爆发动作 |
|---|---|---|---|---|
| 菲欧妮 phoenix | 燔焰 | 120 | **≥90** | 长按普攻 3.5s 发动「普攻:焚罪」，命中异常敌人结算异放 |
| 维琳娜 velina | 风华 | 135 | **≥85** | 长按普攻（高风华）；低风华走强化特殊技合轴 |
| 蕾米埃尔 remielle | 浮晖 | 100 | **=100 或 虚曜=3** | 长按强化特殊技 / 整套入场技 |

HSV 与上限直接抄 ``zzz_od/game_data/agent.py``（官方参数，不要自己调）：
- 菲欧妮-燔焰 ``hsv(4,128,255)±(9,128,55)``，max 120
- 维琳娜-风华 ``hsv(0,255,255)±(90,220,200)``，max 135
- 蕾米埃尔-浮晖 ``hsv(143,140,174)±(7,37,81)``，max 100

取样坐标抄 ``assets/template/agent_state/*/config.yml`` 的 point_list（1920×1080 基准）。
**注意**：OneDragon 只给了部分「角色位置」的坐标（菲欧妮只有 1 号位、维琳娜只有 1 号位和
2 人队 2 号位），其余位置的起点按同位置其他角色的坐标推出来，**首次实跑必须用日志核对数值**。
读不到时返回 ``None``，调用方要能退回固定节奏轮转，不能因为读不到就不动。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import numpy as np

LOGGER = logging.getLogger("GameAutomation")

LABEL = "角色状态"

# 基准分辨率（坐标都是按 1920×1080 量的）
BASE_W, BASE_H = 1920, 1080


@dataclass
class AgentBar:
    """一条角色资源条的定义。"""

    key: str
    name: str
    hsv: Tuple[int, int, int]
    diff: Tuple[int, int, int]
    max_length: int
    # 位置 → (x1, y1, x2, y2)，位置从 1 开始（队伍里第几个）
    rects: Dict[int, Tuple[int, int, int, int]] = field(default_factory=dict)

    def rect_for(self, position: int) -> Optional[Tuple[int, int, int, int]]:
        return self.rects.get(position)


# ---------------------------------------------------------------------------
# 状态条定义：坐标来自 agent_state/*/config.yml，HSV / 满值来自 agent.py
# ---------------------------------------------------------------------------
PHOENIX_FLAME = AgentBar(
    key="phoenix_flame",
    name="菲欧妮-燔焰",
    hsv=(4, 128, 255),
    diff=(9, 128, 55),
    max_length=120,
    rects={
        # 1 号位：phoenix_3_1 原坐标 (139,124)-(186,125)，条宽 47
        1: (139, 124, 186, 126),
        # 2/3 号位：OneDragon 没给，按同位置其他角色的 y 和条宽推出来，实跑要核对
        2: (584, 97, 631, 99),
        3: (766, 97, 813, 99),
    },
)

VELINA_GRACE = AgentBar(
    key="velina_grace",
    name="维琳娜-风华",
    hsv=(0, 255, 255),
    diff=(90, 220, 200),
    max_length=135,
    rects={
        # 1 号位：velina_3_1 (132,126)-(204,127)，条宽 72
        1: (132, 126, 204, 128),
        2: (584, 97, 656, 99),
        3: (766, 97, 838, 99),
    },
)

REMIELLE_RADIANCE = AgentBar(
    key="remielle_radiance",
    name="蕾米埃尔-浮晖",
    hsv=(143, 140, 174),
    diff=(7, 37, 81),
    max_length=100,
    rects={
        # 1 号位：remielle_floating_radiance_3_1 (142,120)-(212,121)，条宽 70
        1: (142, 120, 212, 122),
        2: (584, 97, 654, 99),
        3: (766, 97, 836, 99),
    },
)

# 队伍位置 → 角色 key（用户 2026-11-01 的固定配队：蕾米埃尔/菲欧妮/维琳娜）
POSITION_AGENTS: Dict[int, str] = {1: "remielle", 2: "phoenix", 3: "velina"}

# 角色 key → 该角色的资源条
AGENT_BARS: Dict[str, AgentBar] = {
    "phoenix": PHOENIX_FLAME,
    "velina": VELINA_GRACE,
    "remielle": REMIELLE_RADIANCE,
}

# 关键阈值（照抄 OneDragon 策略里的窗口）
PHOENIX_BURST_AT = 90       # 燔焰 ≥90 放焚罪
PHOENIX_DONE_BELOW = 30     # 焚罪后掉到 30 以下 → 切走
VELINA_HIGH_AT = 85         # 风华 ≥85 长按普攻
VELINA_DONE_BELOW = 30      # 低于 30 认为这一轮打完
REMIELLE_FULL_AT = 100      # 浮晖满点
REMIELLE_LOW_BELOW = 20     # 低于 20 认为这一轮打完


def _hsv_bounds(hsv: Tuple[int, int, int], diff: Tuple[int, int, int]):
    """算出 HSV 上下界，处理 OpenCV 的 H 环绕（0-179）。"""
    h, s, v = [int(x) for x in hsv]
    dh, ds, dv = [int(x) for x in diff]
    lower_h, upper_h = h - dh, h + dh
    s_lo, s_hi = max(0, s - ds), min(255, s + ds)
    v_lo, v_hi = max(0, v - dv), min(255, v + dv)
    return (lower_h, upper_h), (s_lo, s_hi), (v_lo, v_hi)


def read_bar(
    screen_rgb: np.ndarray,
    rect: Tuple[int, int, int, int],
    hsv: Tuple[int, int, int],
    diff: Tuple[int, int, int],
    max_length: int,
) -> Optional[int]:
    """读一条状态条的数值（0~max_length）。区域无效/无命中返回 None。

    算法与 OneDragon ``check_length_by_foreground_color`` 一致：
    HSV 过滤 → 所有命中像素的外接矩形宽度 → 宽度/条宽 × max_length。
    """
    import cv2

    if screen_rgb is None or not rect:
        return None
    h, w = screen_rgb.shape[:2]
    scale_x = w / BASE_W
    scale_y = h / BASE_H
    x1 = max(0, int(rect[0] * scale_x))
    y1 = max(0, int(rect[1] * scale_y))
    x2 = min(w, int(rect[2] * scale_x))
    y2 = min(h, int(rect[3] * scale_y))
    if x2 - x1 < 4 or y2 - y1 < 1:
        return None

    part = screen_rgb[y1:y2, x1:x2]
    try:
        hsv_img = cv2.cvtColor(part, cv2.COLOR_RGB2HSV)
    except Exception as exc:  # noqa: BLE001 - 颜色空间转换失败按读不到处理
        LOGGER.debug(f"[{LABEL}] HSV 转换失败: {exc}")
        return None

    (h_lo, h_hi), (s_lo, s_hi), (v_lo, v_hi) = _hsv_bounds(hsv, diff)
    if h_lo < 0:
        # H 环绕：拆两段（例如 -5~13 要拆成 0~13 和 175~179）
        mask1 = cv2.inRange(hsv_img, (0, s_lo, v_lo), (h_hi, s_hi, v_hi))
        mask2 = cv2.inRange(hsv_img, (179 + h_lo, s_lo, v_lo), (179, s_hi, v_hi))
        mask = cv2.bitwise_or(mask1, mask2)
    elif h_hi > 179:
        mask1 = cv2.inRange(hsv_img, (h_lo, s_lo, v_lo), (179, s_hi, v_hi))
        mask2 = cv2.inRange(hsv_img, (0, s_lo, v_lo), (h_hi - 179, s_hi, v_hi))
        mask = cv2.bitwise_or(mask1, mask2)
    else:
        mask = cv2.inRange(hsv_img, (h_lo, s_lo, v_lo), (h_hi, s_hi, v_hi))

    coords = cv2.findNonZero(mask)
    if coords is None:
        return 0
    bar_width = max(1, part.shape[1])
    fg_width = int(cv2.boundingRect(coords)[2])
    fg_width = max(0, min(fg_width, bar_width))
    return int(fg_width * max_length / bar_width)


def read_agent_state(
    screen_rgb: np.ndarray,
    agent_key: str,
    position: int,
) -> Optional[int]:
    """读某个角色在自己位置上的资源条数值。没有该角色的条 / 坐标缺失返回 None。"""
    bar = AGENT_BARS.get(agent_key)
    if bar is None:
        return None
    rect = bar.rect_for(position)
    if rect is None:
        return None
    return read_bar(screen_rgb, rect, bar.hsv, bar.diff, bar.max_length)


def agent_at_position(position: int) -> Optional[str]:
    """队伍位置（1~3）对应的角色 key。"""
    return POSITION_AGENTS.get(position)


def next_position(position: int, total: int = 3) -> int:
    """按空格切「下一位」后的位置（循环）。"""
    return position % max(1, total) + 1
