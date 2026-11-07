"""2026-11-05 异环「获得物品」浮层导致漏买 / 误判成功 / 买完不关 回归。

事故：20:23 迷迭兑换采购前 OCR「本月库存 5/5 / 20/20」，采购后画面是
「获得物品 / 点击空白区域关闭」浮层 —— 异环这个浮层不点不会自己散。
`_buy_once` 只 sleep 2.5s 就当散了，后续点击全落在浮层上，结果
质感骰子 1/5、三重钥匙 0/20，理由「已达上限或无弹窗」被
`ALREADY_BOUGHT_REASONS` 当成合法已购 → all_ok=True → 写月度标记，
本月再也不会补买。

跑法：.venv\\Scripts\\python.exe tests\\suite_nte_reward_popup_20261105.py
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.foreground_flow_runner as ff  # noqa: E402
import game_auto.runtime.monthly_exchange as me  # noqa: E402
import game_auto.runtime.zzz_monthly_shop as zs  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def _ocr_seq(seq):
    it = iter(seq)
    return lambda *a, **k: [
        {"text": t, "center": (0, 0), "confidence": 0.9} for t in next(it)
    ]


def _popup_env(seq):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(mock.patch.object(ff, "_window_rect", return_value=(0, 0, 1920, 1080)))
    stack.enter_context(mock.patch(
        "game_auto.runtime.ocr_click_tester.ocr_window_content", _ocr_seq(seq)))
    click = stack.enter_context(mock.patch.object(ff, "_click_window_local"))
    stack.enter_context(mock.patch.object(zs.time, "sleep", lambda *_: None))
    return stack, click


def test_popup_three_states():
    print("[1] _dismiss_reward_popup 三态")
    logger = mock.MagicMock()
    with _popup_env([["商城", "迷迭兑换"]])[0]:
        check("本来没有浮层 → None", zs._dismiss_reward_popup(logger, 1, "t") is None)
    stack, click = _popup_env([["获得物品", "点击空白区域关闭"], ["商城"]])
    with stack:
        got = zs._dismiss_reward_popup(logger, 1, "t")
    check("点一次关掉 → True", got is True, got)
    check("只点 1 次且点的是右下空白", click.call_count == 1, click.call_count)
    if click.call_count:
        args = click.call_args[0]
        check("点在 (0.92,0.93)*1920x1080 附近",
              abs(args[1] - 1766) < 5 and abs(args[2] - 1004) < 5, args)
    with _popup_env([["获得物品"]] * 5)[0]:
        check("连点 3 次仍在 → False", zs._dismiss_reward_popup(logger, 1, "t") is False)


def test_buy_once_stuck():
    print("[2] 浮层关不掉时 _buy_once 返回失败码")
    logger = mock.MagicMock()
    with mock.patch.object(zs, "_locate_item", return_value=(491, 400)), \
            mock.patch.object(zs, "_locate", return_value=(900, 700)), \
            mock.patch.object(zs, "_wait_gone", return_value=True), \
            mock.patch.object(zs, "_dismiss_reward_popup", return_value=False), \
            mock.patch.object(zs.time, "sleep", lambda *_: None):
        got = zs._buy_once(logger, 1, {"name": "质感骰子"}, "tpl",
                           [1.0], [0.8], None, label="迷迭兑换")
    check("返回 REWARD_POPUP_STUCK", got == zs.REWARD_POPUP_STUCK, got)
    check("新失败码不在「本月已买过」理由里",
          zs.REWARD_POPUP_STUCK not in zs.ALREADY_BOUGHT_REASONS
          and all("浮层" not in r for r in zs.ALREADY_BOUGHT_REASONS))


def test_buy_items_marks_failed():
    print("[3] 两个购买循环都把浮层卡住判成失败")
    src_me = Path(me.__file__).read_text(encoding="utf-8")
    src_zs = Path(zs.__file__).read_text(encoding="utf-8")
    check("monthly_exchange._buy_items 处理 REWARD_POPUP_STUCK",
          "result == REWARD_POPUP_STUCK" in src_me
          and "「获得物品」浮层未关闭，无法继续采购" in src_me)
    check("zzz_monthly_shop._buy_items 处理 REWARD_POPUP_STUCK",
          "result == REWARD_POPUP_STUCK" in src_zs
          and "「获得物品」浮层未关闭，无法继续采购" in src_zs)
    check("失败理由不属于 ALREADY_BOUGHT_REASONS",
          "「获得物品」浮层未关闭，无法继续采购" not in zs.ALREADY_BOUGHT_REASONS)
    check("monthly_exchange 已导入新常量", "REWARD_POPUP_STUCK" in src_me)


def test_close_and_config():
    print("[4] 买完关闭自己启动的进程，但保留复用的窗口")
    src_ff = Path(ff.__file__).read_text(encoding="utf-8")
    check("run_flow finally 有 reused_window 保护",
          "close_on_exit and reused_window and not cfg.get(\"close_reused_window\", False)" in src_ff)
    check("保护分支只是保留，不调 close_flow_processes",
          "复用已运行窗口，保留游戏进程" in src_ff)

    cfgs = {
        name: json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        for name in ("flow_configs.json", "flow_configs.example.json")
    }
    a, b = cfgs["flow_configs.json"], cfgs["flow_configs.example.json"]
    check("两份 NTE_MONTHLY 一致", a["NTE_MONTHLY"] == b["NTE_MONTHLY"])
    check("close_process_on_finish=true", a["NTE_MONTHLY"]["close_process_on_finish"] is True)
    # 非月度流程（XM/MINIMAX/WORKBUDDY/TRAE/QODER）本来就是 true，不是本次改动；
    # 只检查其它「月度」流程没被一起打开。
    # 2026-11-06 例外：绝区零两条月流程拆分后都显式设 close_process_on_finish=true，
    # 目的是各自启动/关闭、避免上一条失败把游戏停在商城页影响下一条。
    zzz_split = ("ZZZ_MONTHLY", "ZZZ_LOGISTICS_MONTHLY")
    others = [
        k for k, v in a.items()
        if k.endswith("_MONTHLY") and k != "NTE_MONTHLY" and k not in zzz_split
        and isinstance(v, dict) and v.get("close_process_on_finish") is True
    ]
    check("其它月度流程收尾配置未被改动（ZZZ 拆分例外）", others == [], others)


def test_evidence():
    print("[5] 误写标记的备份与采购取证")
    out = PROJECT_ROOT / "data" / "artifacts"
    check("月度标记清除前已备份",
          (out / "monthly_flow_nte_marker_backup_20261105.json").exists())
    check("采购后浮层截图在", (out / "monthly_shop" / "20261105_202348_迷迭兑换_采购后.jpg").exists())
    check("可复用的浮层文字常量",
          "点击空白区域关闭" in zs.REWARD_POPUP_TEXTS
          and "获得物品" in zs.REWARD_POPUP_TEXTS)


def main():
    test_popup_three_states()
    test_buy_once_stuck()
    test_buy_items_marks_failed()
    test_close_and_config()
    test_evidence()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
