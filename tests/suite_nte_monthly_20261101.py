# -*- coding: utf-8 -*-
"""异环（NTE）每月迷迭兑换回归（2026-11-01）。

用户给的链路：主界面 ESC → 二级菜单「商城」→ 左侧「集市兑换」→ 顶部「迷迭兑换」→
买 质感骰子×5 / 捏造骰子×5 / 三重钥匙×20（50%off，本月库存买满）。

为什么这套配置特殊（别照抄到别的游戏）：
- **页签必须按坐标点**：RapidOCR 把「迷迭兑换」认成「迷送兑换」（conf 0.69），
  按文字定位会失败；点完仍用「资深猎人攻略/无彩染剂/并肩达人/茫昧絮语」四种养成材料
  做内容校验，确认真的在迷迭页。
- **商品必须按坐标点**：质感骰子（红）和捏造骰子（蓝）转灰度后互相匹配 0.976，
  模板和 OCR 都分不开；商品名 OCR 直接糊成「质实服子捏造服三重钥影」。
  坐标点完照样要等「购买」弹窗，点空会暴露成 NO_POPUP。

本套件锁定：流程结构、模板存在、清单数量、坐标合法性、`_frac_point` 换算、
内容标记、月度调度接线。真实点击要游戏窗口，不在这里做。
"""

import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

import game_auto.runtime.foreground_flow_runner as ffr  # noqa: E402
import game_auto.runtime.task_batch_runner as tbr  # noqa: E402
import game_auto.runtime.zzz_monthly_shop as shop  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def main():
    print("[1] NTE_MONTHLY 流程可加载")
    cfg = ffr._load_flow_config("NTE_MONTHLY")
    check("流程存在", bool(cfg), cfg)
    if not cfg:
        print("FAILED: 配置没加载出来")
        return 1
    steps = cfg.get("steps") or []
    check("7 步（6 步进游戏 + 1 步兑换）", len(steps) == 7, len(steps))
    check("最后一步是 monthly_exchange", steps[-1].get("action") == "monthly_exchange", steps[-1].get("name"))
    check("前六步复用了 NTE 进游戏链路",
          [s.get("action") for s in steps[:6]] ==
          ["click_template", "wait_template", "click_template", "click_template", "wait_process", "click_template"],
          [s.get("action") for s in steps[:6]])

    print("[2] 模板就位")
    for rel in ("assets/templates/异环-商城入口.png", "assets/templates/异环-购买确认.png"):
        check(f"{rel} 存在", (PROJECT_ROOT / rel).is_file(), rel)

    print("[3] 兑换清单与坐标")
    step = steps[-1]
    items = step.get("items") or []
    names = [it.get("name") for it in items]
    counts = {it.get("name"): it.get("count") for it in items}
    # 2026-11-01 16:06 用户确认：捏造骰子不换，只买质感骰子 + 三重钥匙
    check("两件商品（捏造骰子不换）", names == ["质感骰子", "三重钥匙"], names)
    check("数量 5/20", counts == {"质感骰子": 5, "三重钥匙": 20}, counts)
    check("两件都开彩色匹配（红/蓝骰子灰度分不开）",
          all(it.get("color_match") is True for it in items), [it.get("color_match") for it in items])
    check("两件都有彩色模板",
          all((PROJECT_ROOT / str(it.get("template", "")).replace("\\", "/")).is_file() for it in items),
          [it.get("template") for it in items])
    check("坐标保留作兜底",
          all(isinstance(it.get("point_frac"), list) and len(it["point_frac"]) == 2 for it in items), items)
    for it in items:
        fx, fy = it["point_frac"]
        check(f"{it['name']} 坐标在窗口内", 0 < fx < 1 and 0 < fy < 1, it["point_frac"])
    check("两件商品横向排开（x 递增）",
          items[0]["point_frac"][0] < items[1]["point_frac"][0],
          [it["point_frac"][0] for it in items])
    check("不再包含捏造骰子", "捏造骰子" not in names, names)

    print("[4] 页签与内容校验")
    check("页签按坐标点", isinstance(step.get("tab_point_frac"), list) and len(step["tab_point_frac"]) == 2,
          step.get("tab_point_frac"))
    check("不依赖页签模板（OCR 会认错）", not (step.get("tab_template") or "").strip(), step.get("tab_template"))
    markers = step.get("tab_content_markers") or []
    check("含四种养成材料做页面特征",
          set(markers) >= {"资深猎人攻略", "无彩染剂", "并肩达人", "茫昧絮语"}, markers)
    check("左侧导航限制在左 18%", step.get("nav_region_frac") == [0.0, 0.15, 0.18, 0.95], step.get("nav_region_frac"))

    print("[5] 比例坐标换算")
    with patch.object(ffr, "_window_rect", return_value=(100, 50, 1920, 1080)):
        pt = shop._frac_point(1, [0.5, 0.5])
        check("窗口中心", pt == (960, 540), pt)
        pt2 = shop._frac_point(1, [0.256, 0.371])
        check("质感骰子坐标", pt2 == (int(0.256 * 1920), int(0.371 * 1080)), pt2)
    with patch.object(ffr, "_window_rect", return_value=None):
        check("取不到窗口返回 None", shop._frac_point(1, [0.5, 0.5]) is None)
    with patch.object(ffr, "_window_rect", return_value=(0, 0, 1920, 1080)):
        check("非法比例返回 None", shop._frac_point(1, [1.2, 0.5]) is None)

    print("[6] 月度调度接线")
    entry = tbr._MONTHLY_FLOW_BY_APP.get("NevernessToEverness")
    check("异环映射到 NTE_MONTHLY", entry is not None and entry[0] == "NTE_MONTHLY", entry)
    check("执行入口存在", "NTE_MONTHLY" in tbr._MONTHLY_RUNNERS, sorted(tbr._MONTHLY_RUNNERS))

    print("[7] confirm 模板分离度（弹窗 vs 商品页）")
    ref_dialog = Path(r"C:\Users\admin\.workbuddy\clipboard-images\clipboard-2026-11-01T07-38-05-578Z-8ca4364f.jpg")
    try:
        import cv2
        import numpy as np
        from PIL import Image

        def _gray(path):
            with Image.open(path) as im:
                return cv2.cvtColor(np.asarray(im.convert("RGB")), cv2.COLOR_RGB2GRAY)

        tpl = _gray(PROJECT_ROOT / "assets/templates/异环-购买确认.png")   # PIL 读中文路径，别用 cv2.imread
        if ref_dialog.is_file():
            dialog = _gray(ref_dialog)
            r = cv2.matchTemplate(dialog, tpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, _ = cv2.minMaxLoc(r)
            check("弹窗上 ≥0.99", mx >= 0.99, round(float(mx), 3))
        else:
            print("  SKIP 缺少参考截图")
    except ImportError:
        print("  SKIP 无 cv2")

    print("[8] 彩色匹配：商品撤架后不会误命中")
    ref_list = Path(r"C:\Users\admin\.workbuddy\clipboard-images\clipboard-2026-11-01T07-38-05-576Z-089c1775.jpg")
    if ref_list.is_file():
        from PIL import Image as _Image

        from game_auto.runtime.foreground_flow_runner import _match_template_cv2_once

        confs = (0.9, 0.85, 0.8)
        shot = _Image.open(ref_list)
        red_tpl = _Image.open(PROJECT_ROOT / "assets/templates/异环-质感骰子.png")
        blue_tpl = _Image.open(PROJECT_ROOT / "assets/templates/异环-捏造骰子.png")
        hit = _match_template_cv2_once(shot, red_tpl, confs, use_color=True)
        check("红骰子彩色定位命中", hit is not None, hit)

        # 模拟"质感骰子买满撤架、该位置换成蓝骰子"：灰度会认成还在，彩色不会
        gone = shot.copy()
        gone.paste(blue_tpl, (420, 280))
        gray_hit = _match_template_cv2_once(gone, red_tpl, confs, use_color=False)
        color_hit = _match_template_cv2_once(gone, red_tpl, confs, use_color=True)
        check("灰度对照：会误命中", gray_hit is not None, gray_hit)
        check("彩色正确判定已消失", color_hit is None, color_hit)
    else:
        print("  SKIP 缺少商品列表截图")

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项 → {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
