# -*- coding: utf-8 -*-
"""DuMate 每日签到回归（2026-11-17 接入，2026-11-26 改为真实签到）。

覆盖：
[1] 双份 flow_configs：等待主界面 + dumate_claim_daily，复用窗口、默认不关；
[2] 接线：WOOL_FLOW_KEYS / GUI/WebUI 显示名 / friendly_note / exe 路径 / 内建动作；
[3] 纯函数：独立按钮识别、已签到优先、积分入口并块、积分页特征、礼盒坐标；
[4] 打桩流程：旧版首页直点、新版礼盒→积分页、已签到幂等、入口缺失/点击被拦/
    点后无已签到证据均失败。测试绝不调用真实 OCR 或鼠标。
"""

import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.runtime.dumate_claim as dclaim  # noqa: E402
import game_auto.runtime.tray_activation as tray_activation  # noqa: E402
import game_auto.services.launch_task_catalog as catalog  # noqa: E402
from game_auto.runtime.foreground_flow_runner import _BUILTIN_ACTIONS  # noqa: E402

FAILURES = []
STEP_NAMES = ["等待搭子主界面", "执行每日签到"]
STEP_ACTIONS = ["wait_template", "dumate_claim_daily"]


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_flow_configs():
    print("[1] flow_configs 双份配置")
    for name, required in (("flow_configs.example.json", True), ("flow_configs.json", False)):
        path = PROJECT_ROOT / name
        if not required and not path.exists():
            check(f"{name} 不存在则跳过", True)
            continue
        flow = json.loads(path.read_text(encoding="utf-8")).get("DUMATE")
        check(f"{name}: DUMATE 存在", flow is not None)
        if flow is None:
            continue
        check(f"{name}: enabled=true", flow.get("enabled") is True)
        check(
            f"{name}: 标题关键词兼容百度搭子/DuMate",
            "百度搭子" in (flow.get("window_title_keywords") or [])
            and "DuMate" in (flow.get("window_title_keywords") or []),
            flow.get("window_title_keywords"),
        )
        check(f"{name}: 进程名 DuMate.exe", "DuMate.exe" in (flow.get("window_process_names") or []))
        check(f"{name}: 复用已开窗口", flow.get("reuse_existing_window") is True)
        check(f"{name}: 配置托盘图标恢复", flow.get("tray_icon_name") == "百度搭子")
        check(f"{name}: 默认不关闭用户客户端", flow.get("close_process_on_finish") is False
              and flow.get("close_process_on_fail") is False)
        steps = flow.get("steps") or []
        check(f"{name}: 两步顺序", [s.get("name") for s in steps] == STEP_NAMES,
              [s.get("name") for s in steps])
        check(f"{name}: 动作序列", [s.get("action") for s in steps] == STEP_ACTIONS,
              [s.get("action") for s in steps])
        if len(steps) >= 2:
            check(f"{name}: 主界面等待非 optional", steps[0].get("optional_if_missing") is not True)
            check(f"{name}: 主界面特征含搭子帮你/新任务",
                  "搭子帮你" in (steps[0].get("target_text") or [])
                  and "新任务" in (steps[0].get("target_text") or []))
            check(f"{name}: 签到动作可重试", steps[1].get("on_fail") == "retry_step"
                  and (steps[1].get("max_attempts") or 0) >= 2)


def test_wiring():
    print("[2] 接线")
    check("WOOL_FLOW_KEYS 含 DUMATE", "DUMATE" in btr.WOOL_FLOW_KEYS)
    check("DUMATE 在 RACCOON 之前", btr.WOOL_FLOW_KEYS.index("DUMATE")
          < btr.WOOL_FLOW_KEYS.index("RACCOON"))
    from game_auto.ui.bridge_views.automation import AutomationView

    check("GUI 显示名 meta 含 DUMATE",
          AutomationView._WOOL_FLOW_META.get("DUMATE") == "DuMate 每日福利")
    webui_src = (PROJECT_ROOT / "src/game_auto/ui/webui/api/wool.py").read_text(encoding="utf-8")
    check("WebUI meta 含 DUMATE", '"DUMATE":"DuMate 每日福利"' in webui_src)
    note = str(catalog.STANDALONE_TASK_APPS.get("WoolFlow", {}).get("friendly_note") or "")
    check("WoolFlow friendly_note 提及 DuMate", "DuMate" in note)
    check("内建动作已登记", "dumate_claim_daily" in _BUILTIN_ACTIONS)
    check("claim_daily 可导入", hasattr(dclaim, "claim_daily"))
    check("托盘激活模块可导入", hasattr(tray_activation, "activate_tray_icon"))
    for app_cfg_path in (PROJECT_ROOT / "data" / "app_config.json", PROJECT_ROOT / "app_config.json"):
        if not app_cfg_path.exists():
            continue
        app_cfg = json.loads(app_cfg_path.read_text(encoding="utf-8"))
        exe = str((app_cfg.get("flow_exe_paths") or {}).get("DUMATE") or "")
        check(f"flow_exe_paths.DUMATE 已配（{app_cfg_path.name}）", bool(exe), exe)
        if exe:
            check("DuMate.exe 路径存在", os.path.exists(exe), exe)
        break


def test_pure_functions():
    print("[3] 纯函数")
    check("独立立即签到按钮可识别",
          dclaim.claim_button_block([{"text": "立即签到", "center": (100, 200)}]) is not None)
    check("宣传语不能冒充立即签到按钮",
          dclaim.claim_button_block([{"text": "快来签到领积分啦"}]) is None)
    check("已签到文案优先识别",
          dclaim.claimed_text([{"text": "立即签到"}, {"text": "今日已签到"}]) == "今日已签到")
    check("已领取不冒充已签到",
          dclaim.claimed_text([{"text": "已领取"}]) is None)
    check("可用积分并块可识别",
          dclaim.points_entry_block([{"text": "可用积分 1,100 >"}]) is not None)
    check("积分页专属文案可识别",
          dclaim.signin_page_visible([{"text": "积分账单"}]))
    check("普通主页不冒充积分页",
          not dclaim.signin_page_visible([{"text": "新任务"}]))
    point = dclaim.gift_local_point((0, 0, 1936, 1048))
    check("礼盒坐标落在左下角用户卡片", point is not None and 270 <= point[0] <= 320
          and 990 <= point[1] <= 1015, point)
    check("无窗口区域返回 None", dclaim.gift_local_point(None) is None)
    check("礼盒横坐标不随窗口宽度缩放",
          dclaim.gift_local_point((0, 0, 1366, 768))[0] == dclaim.GIFT_X_LOCAL)


def _run_flow(ocr_sequence, *, rect=(0, 0, 1936, 1048), click_results=None, timeout_sec=30):
    click_results = click_results or [True]
    ocr_iter = iter(ocr_sequence)
    last_ocr = []

    def next_ocr(*_args, **_kwargs):
        nonlocal last_ocr
        try:
            last_ocr = next(ocr_iter)
        except StopIteration:
            pass
        return last_ocr

    if click_results == [True]:
        click_side_effect = lambda *_args, **_kwargs: True
    else:
        click_side_effect = click_results
    with mock.patch.object(dclaim, "_window_rect", return_value=rect), \
         mock.patch.object(dclaim, "_ensure_visible", return_value=(True, "")), \
         mock.patch.object(dclaim, "_ocr", side_effect=next_ocr), \
         mock.patch.object(dclaim, "_click", side_effect=click_side_effect) as click, \
         mock.patch.object(dclaim, "POLL_SEC", 0.001), \
         mock.patch.object(dclaim, "MENU_WAIT_SEC", 0.01), \
         mock.patch.object(dclaim, "POINTS_PAGE_WAIT_SEC", 0.01), \
         mock.patch.object(dclaim, "VERIFY_POLL_SEC", 0.01):
        result = dclaim.claim_daily(mock.Mock(), 1234, timeout_sec=timeout_sec)
    return result, click


def test_flows():
    print("[4] 打桩流程")
    button = [{"text": "立即签到", "center": (500, 700)}]
    claimed = [{"text": "今日已签到", "center": (500, 700)}]

    result, click = _run_flow([button, button, claimed])
    check("旧版首页：点击后核验通过", result[0] and "签到成功" in result[1], result)
    check("旧版首页：只点击一次", click.call_count == 1, click.call_count)

    home = [{"text": "搭子帮你"}]
    menu = [{"text": "可用积分 1,100 >", "center": (150, 612)}]
    page = [{"text": "积分账单"}, {"text": "立即签到", "center": (900, 500)}]
    result, click = _run_flow([home, menu, page, page, claimed])
    check("新版：礼盒→可用积分→签到通过", result[0], result)
    check("新版：礼盒、积分入口、签到共三次点击", click.call_count == 3, click.call_count)

    result, click = _run_flow([claimed])
    check("已签到：幂等通过且零点击", result[0] and click.call_count == 0, (result, click.call_count))

    result, click = _run_flow([home, home])
    check("新版礼盒后无可用积分：失败", not result[0] and "可用积分" in result[1], result)

    result, click = _run_flow([button, button, button], timeout_sec=30)
    check("点后无已签到证据：失败", not result[0] and "未出现" in result[1], result)

    result, click = _run_flow([button, button], click_results=[False])
    check("签到点击被防护闸拦截：失败", not result[0] and "拦截" in result[1], result)

    result, click = _run_flow([home, menu, [{"text": "积分账单"}]])
    check("积分页无按钮且无已签到文案：失败", not result[0] and "既无" in result[1], result)


def test_tray_activation():
    print("[5] 托盘激活")
    proc = mock.Mock()
    proc.poll.side_effect = [None, 0, 0]
    proc.returncode = 0
    with mock.patch.object(tray_activation.subprocess, "Popen", return_value=proc), \
         mock.patch.object(tray_activation.time, "sleep"):
        check("托盘图标激活成功", tray_activation.activate_tray_icon(mock.Mock(), "百度搭子"))

    proc = mock.Mock()
    proc.poll.side_effect = [None, 6, 6]
    proc.returncode = 6
    with mock.patch.object(tray_activation.subprocess, "Popen", return_value=proc), \
         mock.patch.object(tray_activation.time, "sleep"):
        check("托盘图标未找到时失败", not tray_activation.activate_tray_icon(mock.Mock(), "百度搭子"))

    with mock.patch.object(tray_activation.subprocess, "Popen", side_effect=OSError("no powershell")):
        check("PowerShell 不可用时失败",
              not tray_activation.activate_tray_icon(mock.Mock(), "百度搭子"))

    check("停止请求下不启动 PowerShell",
          not tray_activation.activate_tray_icon(mock.Mock(), "百度搭子", stop_check=lambda: True))


def main():
    test_flow_configs()
    test_wiring()
    test_pure_functions()
    test_flows()
    test_tray_activation()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
