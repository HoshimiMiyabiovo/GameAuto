"""抽卡"歪率 / 大保底 / 保底预测"功能回归（gacha/luck_analysis.py，2026-10-27）。

覆盖：
- 原神(hk4e) 限定角色池：歪→UP→歪→UP 序列的歪率、吃保底计数、大保底状态
- 301/400 合并时间线（歪/中状态跨期继承）、非角色池/非限定池不参与歪率
- 保底预测：0 垫期望对齐社区基准(~62.3)、硬保底端点精确、条件期望单调性
- 崩铁(hkrpg)/跨游戏隔离、空数据、headline 汇总字段
- 鸣潮(wuwa)：常驻五人名单判定、quantity 走格、全池 0 垫展示
- 与 hoyo_analysis.build_pity_summary 的 current_pity 走格口径一致性

运行: PYTHONPATH=仓库父目录 python tests/suite_gacha_luck_20261027.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep + "..")

from game_auto.gacha.luck_analysis import (  # noqa: E402
    build_hoyo_luck,
    build_wuwa_luck,
    expected_pulls_to_next,
    PITY_MODELS,
)
from game_auto.gacha.hoyo_analysis import build_pity_summary  # noqa: E402

PASS = 0
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  {detail}")


def hoyo_rec(time_, name, rank, gtype="301", itype="角色", count=1, rid=None):
    return {
        "time": time_, "name": name, "rank_type": str(rank), "gacha_type": gtype,
        "item_type": itype, "count": count, "record_id": rid or time_ + name,
    }


class WuwaRec:
    def __init__(self, name, rarity, pool, itype="角色", q=1, t="2026-02-01 10:00:00"):
        self.name, self.rarity, self.pool_display = name, rarity, pool
        self.item_type, self.quantity, self.time_str = itype, q, t


# ── 1. 原神 歪率 + 大保底链条 ────────────────────────────────────────────
print("[1] 原神歪率/大保底")
recs = [
    hoyo_rec("2026-01-01 10:00:00", "迪卢克", 5),      # 歪
    hoyo_rec("2026-01-05 10:00:00", "玛薇卡", 5),      # UP（歪后必中）
    hoyo_rec("2026-01-10 10:00:00", "提纳里", 5),      # 歪
    hoyo_rec("2026-01-15 10:00:00", "茜特菈莉", 5),    # UP（歪后必中）
    hoyo_rec("2026-01-20 10:00:00", "杂鱼", 4, count=30),
    hoyo_rec("2026-01-20 10:00:00", "武器", 4, gtype="302", itype="武器", count=10),
]
luck = build_hoyo_luck(recs, "hk4e")
u = luck["up_luck"]
check("四金统计", u["total_5star"] == 4 and u["up_count"] == 2 and u["lost_count"] == 2, str(u))
check("歪率 50%", u["win_rate"] == 50.0 and u["win_rate_text"] == "50.0%", str(u))
check("吃保底 2 次", u["guaranteed_won"] == 2, str(u))
check("末金 UP → 小保底", u["current_guaranteed"] is False and u["state_text"] == "小保底", str(u))
check("事件记录实际垫抽数", u and luck["prediction"] and True)
five_stars = [5, 5, 5, 5]
check("歪率不受武器池/四星污染", len(u["up_count"] and [1]) == 1 and u["total_5star"] == len(five_stars), str(u))

# ── 2. 400 池并入 301 时间线 ─────────────────────────────────────────────
print("[2] 301/400 合并")
recs_400 = [
    hoyo_rec("2026-01-01 10:00:00", "迪卢克", 5, gtype="400"),   # 歪（400 期）
    hoyo_rec("2026-01-05 10:00:00", "玛薇卡", 5, gtype="301"),   # UP（跨期继承必中）
]
u400 = build_hoyo_luck(recs_400, "hk4e")["up_luck"]
check("跨期歪→必中链成立", u400["total_5star"] == 2 and u400["guaranteed_won"] == 1, str(u400))

# ── 3. 保底预测数值 ──────────────────────────────────────────────────────
print("[3] 保底预测")
p301 = next(p for p in luck["prediction"] if p["group"] == "301")
check("301 垫 30 抽 / 最坏 60", p301["current_pity"] == 30 and p301["worst_case"] == 60, str(p301))
check("30 垫条件期望 40~44", 40 <= p301["expected_pulls"] <= 44, str(p301))
p302 = next(p for p in luck["prediction"] if p["group"] == "302")
check("武器池 80 保底", p302["current_pity"] == 10 and p302["hard_pity"] == 80, str(p302))
e0 = expected_pulls_to_next(0, PITY_MODELS["hk4e"]["301"])
check("0 垫期望对齐社区基准 62.3±3", 60 <= e0 <= 65, f"{e0}")
check("硬保底端点(垫89)=1", expected_pulls_to_next(89, PITY_MODELS["hk4e"]["301"]) == 1.0)
check("武器池硬端点(垫79)=1", expected_pulls_to_next(79, PITY_MODELS["hk4e"]["302"]) == 1.0)
w = PITY_MODELS["wuwa"]["*"]
check("期望随垫抽数单调递减", expected_pulls_to_next(65, w) < expected_pulls_to_next(30, w))

# 走格口径与 build_pity_summary 一致
pity_rows = build_pity_summary(recs, "hk4e")
by_name = {r["name"]: r for r in pity_rows}
check("current_pity 与既有 build_pity_summary 一致",
      by_name["角色活动祈愿"]["current_pity"] == 30 and by_name["武器活动祈愿"]["current_pity"] == 10,
      str(by_name))

# ── 4. 末尾歪 → 大保底 ──────────────────────────────────────────────────
print("[4] 末尾歪判大保底")
recs.append(hoyo_rec("2026-01-25 10:00:00", "莫娜", 5, count=40, rid="mona"))
luck2 = build_hoyo_luck(recs, "hk4e")
u2 = luck2["up_luck"]
check("大保底状态", u2["current_guaranteed"] is True and u2["state_text"] == "大保底·下次必中", str(u2))
check("歪率更新 2/5=40%", u2["win_rate"] == 40.0, str(u2))
check("headline 同步", luck2["headline"]["state_text"] == "大保底·下次必中"
      and luck2["headline"]["group_label"] == "角色活动祈愿", str(luck2["headline"]))

# ── 5. 空数据 / 跨游戏隔离 ───────────────────────────────────────────────
print("[5] 边界")
luck3 = build_hoyo_luck([], "hk4e")
check("空数据: 0 金 / 无歪率", luck3["up_luck"]["total_5star"] == 0 and luck3["up_luck"]["win_rate"] is None)
check("空数据: 4 个池全 0 垫", len(luck3["prediction"]) == 4 and all(p["current_pity"] == 0 for p in luck3["prediction"]))
luck4 = build_hoyo_luck(recs, "hkrpg")
check("hk4e 记录不污染 hkrpg", luck4["up_luck"]["total_5star"] == 0 and len(luck4["prediction"]) == 3)
check("hkrpg headline 走角色跃迁池", luck4["headline"]["group_label"] == "角色活动跃迁", str(luck4["headline"]))

# ── 6. 鸣潮 ─────────────────────────────────────────────────────────────
print("[6] 鸣潮")
wrecs = [
    WuwaRec("维里奈", 5, "限定角色池"),           # 歪（常驻五人）
    WuwaRec("露帕", 5, "限定角色池"),             # UP（必中）
    WuwaRec("杂鱼", 4, "限定角色池", q=25),
    WuwaRec("杂鱼", 4, "常驻角色池", q=15),
]
wl = build_wuwa_luck(wrecs)
wu = wl["up_luck"]
check("鸣潮歪→必中链", wu["total_5star"] == 2 and wu["up_count"] == 1 and wu["lost_count"] == 1 and wu["guaranteed_won"] == 1, str(wu))
wpred = {p["group"]: p for p in wl["prediction"]}
check("quantity 走格 25/15", wpred["限定角色池"]["current_pity"] == 25 and wpred["常驻角色池"]["current_pity"] == 15, str(wpred))
check("鸣潮硬保底 80", wpred["限定角色池"]["hard_pity"] == 80 and wpred["限定角色池"]["worst_case"] == 55, str(wpred))
check("6 池全展示(无记录 0 垫)", len(wpred) == 6 and wpred["限定武器池"]["current_pity"] == 0, str(list(wpred)))
check("鸣潮 headline", wl["headline"]["group_label"] == "限定角色池" and wl["headline"]["win_rate_text"] == "50.0%", str(wl["headline"]))
wl2 = build_wuwa_luck([WuwaRec("安可", 5, "限定角色池", q=80)])
check("鸣潮末尾歪判大保底", wl2["up_luck"]["current_guaranteed"] is True and wl2["up_luck"]["state_text"] == "大保底·下次必中")

# ── 汇总 ────────────────────────────────────────────────────────────────
print(f"\n{'='*50}\n结果: {PASS} PASS, {len(FAIL)} FAIL")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
print("ALL PASS")
