# -*- coding: utf-8 -*-
"""回归：回声猎区「全领齐误报 + 已清节点重打」双修复（2026-11-19，#427/#428）。

事故（2026-11-18 10:23 两连败，用户贴 artifacts/flow_failures 截图）：

1. #428 分贝奖励页：领齐后游戏隐藏「一键领取」按钮、只剩逐档「已领取」，
   旧判定「标题+一键领取」双证据不满足 → 误报「未见奖励页」失败。
   修复：判定抽成 _echo_reward_page_state——标题 +（一键领取 | ≥1 已领取）
   都算页开；仅「已领取」= 幂等收尾关页返回成功。
2. #427 节点 E：已清标记（marks）只记自动化打的，用户手动清的节点不知道；
   已清节点详情面板照样有「开始收录」（可重打），OCR 分不出 → 流程白白重打
   一场，缺轻卫/铁御几十秒即败还报失败（截图里 E 明明带 ✅）。
   修复：_echo_node_cleared 模板匹配标签右侧 ✅ 图标（assets/templates/
   白荆回廊-回声猎区节点已清.png，实测定阈值 0.75：命中 0.81~1.0、噪声
   ≤0.48），命中即跳过并补记 marks，后续周期走 marks 直接跳过。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_baijing_echo_allclaimed_cleared_20261119.py -q
"""

import logging
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.baijing_echo as be  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_reward_page_state():
    print("[1] 分贝奖励页判定（#428：领齐后无一键领取 → claimed 幂等成功）")
    f = be._echo_reward_page_state
    check("标题+一键领取 → claim_all（可领，走一键领取）",
          f(True, True, 0) == "claim_all")
    check("标题+4 行已领取、无一键领取 → claimed（#428 现场帧）",
          f(True, False, 4) == "claimed")
    check("标题+1 行已领取 → claimed（部分已领也按页开，不盲点失败）",
          f(True, False, 1) == "claimed")
    check("仅标题（地图本体同名入口字样）→ unknown，维持双证据防线",
          f(True, False, 0) == "unknown")
    check("无标题 → unknown",
          f(False, True, 4) == "unknown" and f(False, False, 0) == "unknown")


def _patch_env(rect, hit):
    """打桩 _abs_region/_template_center（符号已导入 baijing_echo 命名空间）。"""
    return (
        patch.object(be, "_abs_region", create=True, return_value=rect),
        patch.object(be, "_template_center", create=True, return_value=hit),
    )


def test_node_cleared_probe():
    print("[2] 节点 ✅ 已清探测（#427：模板命中→跳过；窗口/模板异常→fail-open）")
    label = {"center": (1299, 652)}   # #427 实拍 E 标签中心
    log = logging.getLogger("t")
    with patch.object(be, "_abs_region", create=True, return_value=(0, 0, 1920, 1080)):
        with patch.object(be, "_template_center", create=True,
                          return_value=(1371, 653)) as mt:
            got = be._echo_node_cleared(log, "hwnd-stub", label)
            check("✅ 命中 → True", got is True)
            fx0, fy0, fx1, fy1 = mt.call_args.kwargs["frac_region"]
            # 探区 = 标签中心右侧 x +30~+130、y ±60（1920 口径，无缩放偏移）
            exp = ((1299 + 30) / 1920, (652 - 60) / 1080,
                   (1299 + 130) / 1920, (652 + 60) / 1080)
            got_f = (fx0, fy0, fx1, fy1)
            check("探区按 1920 参考偏移换算",
                  all(abs(a - b) < 1e-9 for a, b in zip(got_f, exp)),
                  (got_f, exp))
            check("阈值用 0.75（实拍命中 0.81~1.0、噪声 ≤0.48）",
                  mt.call_args.kwargs["threshold"] == 0.75)
        with patch.object(be, "_template_center", create=True, return_value=None):
            check("模板未命中 → False（当作未清，走补打）",
                  be._echo_node_cleared(log, "hwnd-stub", label) is False)
    with patch.object(be, "_abs_region", create=True, return_value=(100, 200, 1920, 1080)):
        with patch.object(be, "_template_center", create=True,
                          return_value=(1471, 853)) as mt:
            check("窗口有偏移时探区随窗口平移（lx/ly 相对窗口）",
                  be._echo_node_cleared(log, "hwnd-stub",
                                        {"center": (100 + 1299, 200 + 652)}) is True
                  and abs(mt.call_args.kwargs["frac_region"][0] - (1299 + 30) / 1920) < 1e-9)
    with patch.object(be, "_abs_region", create=True, return_value=(0, 0, 1280, 720)):
        with patch.object(be, "_template_center", create=True, return_value=(914, 435)) as mt:
            be._echo_node_cleared(log, "hwnd-stub",
                                  {"center": (866, 435)})
            fx0, _fy0, fx1, _fy1 = mt.call_args.kwargs["frac_region"]
            k = 1280 / 1920
            check("窗宽 1280 时偏移随窗宽缩放（k=2/3）",
                  abs(fx0 * 1280 - (866 + 30 * k)) < 1e-6
                  and abs(fx1 * 1280 - (866 + 130 * k)) < 1e-6,
                  (fx0 * 1280, fx1 * 1280))
    check("hwnd=None → False（fail-open，不抛异常）",
          be._echo_node_cleared(log, None, label) is False)
    with patch.object(be, "_abs_region", create=True, return_value=None):
        check("窗口取不到 → False（fail-open）",
              be._echo_node_cleared(log, "hwnd-stub", label) is False)
    check("标签出窗 → False",
          be._echo_node_cleared(log, "hwnd-stub",
                                {"center": (9999, 9999)}) is False)


def test_template_asset():
    print("[3] 模板资产在位（防误删/改名）")
    from game_auto.settings.paths import TEMPLATES_DIR
    p = TEMPLATES_DIR / "白荆回廊-回声猎区节点已清.png"
    check(f"模板存在：{p.name}", p.is_file())
    if p.is_file():
        check("模板尺寸合理（✅ 图标 ~54x54）",
              b"" != p.read_bytes() and p.stat().st_size > 2000, p.stat().st_size)


def main():
    test_reward_page_state()
    test_node_cleared_probe()
    test_template_asset()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
