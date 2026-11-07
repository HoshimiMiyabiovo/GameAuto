"""白荆回廊「拟合回归」每周活动回归（2026-11-07 提权实机探索）。

实机链路与坐标见 src/game_auto/runtime/baijing_fitting.py 模块 docstring，
8 张参考图在 data/artifacts/ref_baijing_fitting_*_20261107.png。

覆盖：
  [1] 8 张真实截图 × 8 个页面判定，逐张断言命中且互斥（页面判定铁律）
  [2] 比例坐标回到 1080p 实机像素 / 4K 缩放，不写死像素
  [3] 本周累积波次解析与「还差几场」（幂等的关键）
  [4] 全流程状态机打桩：2 场 / 已打满 / 已领过 / 页面不对 / 用户停止 / finally 收尾
  [5] 独立周门控：已跑过跳过、成功写标记、失败不写
  [6] 接线：一条龙调用、进度卡片、判定登记表、变更记录

跑法：.venv\\Scripts\\python.exe tests\\suite_baijing_fitting_20261107.py
"""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from game_auto.runtime import baijing_fitting as bf  # noqa: E402
from game_auto.runtime import baijing_flow as flow  # noqa: E402

ART = PROJECT_ROOT / "data" / "artifacts"
FAILURES = []
LOG = logging.getLogger("baijing_fitting_test")
LOG.addHandler(logging.NullHandler())

PAGES = {
    "main": "ref_baijing_fitting_main_20261107.png",
    "field": "ref_baijing_fitting_field_20261107.png",
    "activity": "ref_baijing_fitting_activity_20261107.png",
    "team": "ref_baijing_fitting_team_20261107.png",
    "deploy": "ref_baijing_fitting_deploy_20261107.png",
    "battle": "ref_baijing_fitting_battle_20261107.png",
    "result": "ref_baijing_fitting_result_20261107.png",
    "reward": "ref_baijing_fitting_reward_20261107.png",
}
# 每页允许命中的判定集合：本流程各页互斥，不允许「顺便命中别的页」
EXPECTED = {k: {k} for k in PAGES}


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} — {detail}")
        FAILURES.append(name)


# 实机每页 OCR 文本快照（2026-11-07 提权探索日志），不依赖 OCR 引擎是否可用，
# 保证「判定逻辑」在任何环境都被锁住；真实截图矩阵另见 test_screenshot_matrix。
TEXT_FRAMES = {
    "main": ["活动总览", "联合训练场1天后结束", "New!", "漫巡", "外勤", "叶脉联结计划",
             "故事", "阶段任务154/200", "同调者", "记忆烙痕", "战术刻印", "精神深潜",
             "有猫零售", "活动中心", "功能菜单"],
    "field": ["物资筹备", "定向物资保障", "自由贸易", "显影行动", "光刻协议", "新质领域",
              "漫巡回忆", "队长养成", "卓越道路", "分进合击战", "思维演绎", "外勤作战中心",
              "联合训练场", "天隙勘测", "收起", "拟合回归", "1天6小时56分", "战斗教学"],
    "activity": ["返回", "GD拟合回归", "本期词条", "被动禁用", "伤害增強",
                 "奖励将在1天6小时56分后刷新", "本周累积波次 0/50", "已领 0/60",
                 "已领 0/25000", "本周最高波次", "拟合开始", "MENU"],
    "team": ["返回", "10/20", "队伍10", "载入队列", "品详细属性", "自动编队",
             "选择参战同调者", "查看详情", "查看激励", "作战开始", "MENU"],
    "deploy": ["体质", "防御", "攻击", "专精", "终端", "Esc", "血露薇", "普通",
               "深度等级", "刻印评级", "刻印提供的激励：", "开始战斗", "01", "02", "03"],
    "battle": ["终端没有被摧毁", "19:56", "当前波次：1/25", "战术终端", "后备队", "暂停",
               "旒珠", "谛卡", "珑", "云无月"],
    "result": ["作战完成", "达成波次▼", "25", "转化奖励", "1.25万", "本次编队",
               "分数验证已通过", "C拟合回归"],
    "reward": ["新物品已存入S.E.E.D.仓库", "5000", "海隅点数", "错金拓片", "秘银拓片",
               "X晶粒", "已领30/60", "已领 12500/25000", "15", "20", "23", "25",
               "拟合开始", "点击屏幕继续"],
}


def _rows_from_image(engine, path):
    import numpy as np
    from PIL import Image

    img = Image.open(path).convert("RGB")
    result, _ = engine(np.asarray(img))
    rows = []
    for box, text, conf in result or []:
        x1 = min(p[0] for p in box)
        x2 = max(p[0] for p in box)
        y1 = min(p[1] for p in box)
        y2 = max(p[1] for p in box)
        rows.append({
            "text": str(text), "confidence": float(conf),
            "center": (int((x1 + x2) // 2), int((y1 + y2) // 2)),
        })
    return rows


# ── [1] 判定矩阵：文本快照 + 真实截图 ────────────────────────────────────
def test_text_matrix():
    print("[1] 8 页实机文本快照 × 8 个判定（互斥）")
    for page, texts in TEXT_FRAMES.items():
        rows = _items(*texts)
        hits = {name for name, fn in bf.PAGE_CHECKS if fn(rows)}
        check(f"{page} 文本快照分类正确", bf.classify_page(rows) == page,
              f"got={bf.classify_page(rows)} hits={hits}")
        check(f"{page} 文本快照不误命中别的页", hits == EXPECTED[page], f"hits={hits}")
    # 「拟合回归」同时出现在外勤页与活动页，绝不能单独判页
    shared = _items("拟合回归")
    check("光有「拟合回归」不判任何页", bf.classify_page(shared) == "unknown",
          bf.classify_page(shared))
    # 「点击屏幕继续」在白荆其它过场也会出现，不能单独判领奖页
    check("光有「点击屏幕继续」不判领奖页", bf.classify_page(_items("点击屏幕继续")) == "unknown")


def test_screenshot_matrix():
    print("[1b] 8 张实机截图页面判定矩阵（onnxocr v6 可用时）")
    # data/artifacts/ 被 .gitignore 排除，干净 checkout 上没有这些本机取证图；
    # 判定逻辑已由 [1] 文本快照矩阵锁住，这里缺图就整体 SKIP，不误报失败。
    if not all((ART / f).exists() for f in PAGES.values()):
        print("  SKIP 本机实机参考图不存在（文本快照矩阵已覆盖判定逻辑）")
        return
    from game_auto.runtime import ocr_click_tester as oc

    getter = getattr(oc, "_get_onnxocr_engine", None)
    engine = getter() if getter else None
    if not engine:
        print("  SKIP onnxocr v6 引擎不可用（文本快照矩阵已覆盖判定逻辑）")
        return
    for page, filename in PAGES.items():
        path = ART / filename
        rows = _rows_from_image(engine, path)
        hits = {name for name, fn in bf.PAGE_CHECKS if fn(rows)}
        check(f"{page} 分类正确", bf.classify_page(rows) == page,
              f"got={bf.classify_page(rows)} ocr={[r['text'] for r in rows][:12]}")
        check(f"{page} 不误命中别的页", hits == EXPECTED[page], f"hits={hits}")


# ── [2] 比例坐标 ────────────────────────────────────────────────────────
def test_ratio_points():
    print("[2] 窗口比例坐标（换分辨率不失效）")
    points = {
        "外勤": (bf.P_FIELD_ENTRY, (1735, 700)),
        "拟合回归": (bf.P_FITTING_ENTRY, (1096, 919)),
        "拟合开始": (bf.P_FITTING_START, (1606, 964)),
        "作战开始": (bf.P_TEAM_START, (1579, 1001)),
        "开始战斗": (bf.P_BATTLE_START, (1746, 980)),
        "左下角奖励": (bf.P_WAVE_REWARD, (150, 960)),
        "点击屏幕继续": (bf.P_REWARD_CONTINUE, (960, 1001)),
        "活动页返回": (bf.P_BACK, (193, 50)),
    }
    for name, (frac, pix) in points.items():
        check(f"{name} 比例在窗口内", 0 < frac[0] < 1 and 0 < frac[1] < 1, frac)
        back = (round(frac[0] * 1920), round(frac[1] * 1080))
        check(f"{name} 1080p 还原实机坐标", back == pix, f"{back} != {pix}")
    # 4K 窗口：比例不变、像素翻倍
    back4k = (round(bf.P_BATTLE_START[0] * 3840), round(bf.P_BATTLE_START[1] * 2160))
    check("4K 下开始战斗坐标等比放大", back4k == (3492, 1960), back4k)
    src = (PROJECT_ROOT / "src/game_auto/runtime/baijing_fitting.py").read_text(encoding="utf-8")
    check("点击统一走 _click_ratio", "_click_ratio" in src)
    check("源码没有写死屏幕像素点击", "pyautogui.click(" not in src)


# ── [3] 波次解析与补打场次 ──────────────────────────────────────────────
def _items(*texts):
    return [{"text": t, "confidence": 0.9, "center": (100, 100)} for t in texts]


def test_wave_parsing():
    print("[3] 本周累积波次解析 / 幂等补打")
    check("同条文本 0/50", bf.parse_weekly_wave(_items("本周累积波次 0/50")) == (0, 50))
    check("同条文本 25/50", bf.parse_weekly_wave(_items("本周累积波次 25/50")) == (25, 50))
    check("同条文本 50/50", bf.parse_weekly_wave(_items("本周累积波次 50/50")) == (50, 50))
    check("标签与数字拆条", bf.parse_weekly_wave(_items("本周累积波次", "25/50")) == (25, 50))
    check("同页别的比分不误读", bf.parse_weekly_wave(
        _items("本周累积波次", "已领 30/60", "1/2 下一条")) is None)
    check("读不到返回 None", bf.parse_weekly_wave(_items("作战完成", "达成波次")) is None)
    check("0/50 → 打 2 场", bf.runs_needed((0, 50)) == 2)
    check("25/50 → 只补 1 场", bf.runs_needed((25, 50)) == 1)
    check("50/50 → 0 场", bf.runs_needed((50, 50)) == 0)
    check("读不到保守打 2 场", bf.runs_needed(None) == 2)
    check("异常上限也不超过 2 场", bf.runs_needed((0, 500)) == 2)


# ── [4] 全流程状态机（打桩，不碰真实游戏）────────────────────────────────
class Script:
    """按脚本返回页面；activity 帧带「本周累积波次」，供场次计算。"""

    def __init__(self, pages, wave="0/50"):
        self.pages = list(pages)
        self.wave = wave
        self.calls = []

    def page_now(self, logger, hwnd):
        page = self.pages.pop(0) if self.pages else "unknown"
        self.calls.append(page)
        extra = [f"本周累积波次 {self.wave}"] if page == "activity" else []
        return page, _items(page, *extra)


def _harness(script, stop_check=None):
    clicks, failures, leaves = [], [], []

    def fake_click(logger, hwnd, point, label):
        clicks.append((label, point))
        return True

    patches = [
        mock.patch.object(bf, "_page_now", side_effect=script.page_now),
        mock.patch.object(bf, "_click", side_effect=fake_click),
        mock.patch.object(bf, "_record_failure",
                          side_effect=lambda *a, **k: failures.append(a[2])),
        mock.patch.object(bf, "_leave_to_hub",
                          side_effect=lambda *a, **k: leaves.append(True)),
        mock.patch.object(bf.time, "sleep", lambda *_: None),
    ]
    for p in patches:
        p.start()
    try:
        result = bf.run_fitting_regression(LOG, 1, stop_check=stop_check)
    finally:
        for p in reversed(patches):
            p.stop()
    return result, clicks, failures, leaves


def test_state_machine():
    print("[4] 全流程状态机")
    # 两场全打 + 领奖
    s = Script([
        "main", "field", "activity",
        "team", "deploy", "battle", "result", "activity",
        "team", "deploy", "battle", "result", "activity",
        "reward", "activity",
    ], wave="0/50")
    res, clicks, failures, leaves = _harness(s)
    check("2 场 + 领奖成功", res["ok"] and res["runs"] == 2 and res["claimed"] is True, res)
    # 收尾 _leave_to_hub 被打了桩（只记 leaves），所以「活动页返回」不出现在 clicks 里
    check("关键点击顺序正确", [c[0] for c in clicks] == [
        "外勤", "拟合回归", "拟合开始", "作战开始", "开始战斗", "结算页点空白",
        "拟合开始", "作战开始", "开始战斗", "结算页点空白",
        "左下角波次奖励", "点击屏幕继续",
    ], [c[0] for c in clicks])
    check("成功后 finally 收尾", leaves == [True], leaves)
    check("成功不写失败取证", failures == [], failures)

    # 本周已打满：只补领奖，一场不打
    s2 = Script(["main", "field", "activity", "reward", "activity"], wave="50/50")
    res2, clicks2, _, _ = _harness(s2)
    check("50/50 只领奖不打", res2["ok"] and res2["runs"] == 0 and res2["claimed"], res2)
    check("50/50 不点任何开始按钮", not any("开始" in c[0] for c in clicks2), clicks2)

    # 奖励本周已领过：点图标没弹窗，按 already 处理，不算失败
    s3 = Script(["main", "field", "activity", "activity"], wave="50/50")
    res3, _, failures3, _ = _harness(s3)
    check("奖励已领过按成功", res3["ok"] and res3["claimed"] is False, res3)
    check("已领过不写失败", failures3 == [], failures3)

    # 中途页面不对：判失败、落取证、仍然收尾
    s4 = Script(["main", "field", "unknown"])
    res4, _, failures4, leaves4 = _harness(s4)
    check("未进入活动页判失败", not res4["ok"] and res4["step"] == "进入拟合回归", res4)
    check("失败落取证", failures4 == ["进入拟合回归"], failures4)
    check("失败也 finally 收尾", leaves4 == [True], leaves4)

    # 用户停止：立刻失败，不继续点
    s5 = Script(["main", "field", "activity"])
    res5, clicks5, _, _ = _harness(s5, stop_check=lambda: True)
    check("停止标志立即生效", not res5["ok"], res5)
    check("停止后不再点击", all(c[0] == "活动页返回" for c in clicks5), clicks5)


# ── [5] 独立周门控 ──────────────────────────────────────────────────────
def test_weekly_gate():
    print("[5] 独立周门控")
    fake_db = mock.MagicMock()

    def run(already, ok):
        fake_db.reset_mock()
        fake_db.get_task_meta.return_value = "2026-W45" if already else ""
        with mock.patch("game_auto.storage.workflow_db.WorkflowDB", return_value=fake_db), \
             mock.patch.object(flow, "_record_baijing_task"), \
             mock.patch.object(flow, "_ensure_baijing_window_valid", return_value=1), \
             mock.patch("game_auto.runtime.baijing_fitting.run_fitting_regression",
                        return_value={"ok": ok, "detail": "测试", "runs": 1, "claimed": True}):
            return flow._maybe_run_fitting_weekly(LOG, 1)

    line = run(True, True)
    check("本周已跑过直接跳过", line is None, line)
    check("跳过时不写标记", not fake_db.set_task_meta.called)

    line = run(False, True)
    check("成功写周标记", fake_db.set_task_meta.called and "✅" in line, line)
    check("标记键是 weekly_flow:BAIJING_FITTING",
          "weekly_flow:BAIJING_FITTING" in str(fake_db.set_task_meta.call_args),
          fake_db.set_task_meta.call_args)

    line = run(False, False)
    check("失败不写标记（下次重试）", not fake_db.set_task_meta.called and "❌" in line, line)


# ── [6] 接线 ───────────────────────────────────────────────────────────
def test_wiring():
    print("[6] 接线与文档")
    flow_src = (PROJECT_ROOT / "src/game_auto/runtime/baijing_flow.py").read_text(encoding="utf-8")
    profile_src = (PROJECT_ROOT / "src/game_auto/games/baijing.py").read_text(encoding="utf-8")
    policy_src = (PROJECT_ROOT / "tests/suite_page_judgement_policy.py").read_text(encoding="utf-8")
    changelog = (PROJECT_ROOT / "docs/changelog.md").read_text(encoding="utf-8")
    check("一条龙主流程调用拟合回归", "_maybe_run_fitting_weekly(logger, hwnd" in flow_src)
    check("门控函数已定义", "def _maybe_run_fitting_weekly(" in flow_src)
    check("周标记用 weekly_flow_week_key", "weekly_flow_week_key()" in flow_src)
    check("进度卡片加了 fitting_weekly", '"fitting_weekly"' in profile_src)
    check("8 个判定都登记了证据", policy_src.count('("baijing_fitting.py"') == 8,
          policy_src.count('("baijing_fitting.py"'))
    check("变更记录有本轮", "拟合回归" in changelog)
    check("8 张参考图都列进矩阵", set(PAGES.values()) == {
        "ref_baijing_fitting_main_20261107.png",
        "ref_baijing_fitting_field_20261107.png",
        "ref_baijing_fitting_activity_20261107.png",
        "ref_baijing_fitting_team_20261107.png",
        "ref_baijing_fitting_deploy_20261107.png",
        "ref_baijing_fitting_battle_20261107.png",
        "ref_baijing_fitting_result_20261107.png",
        "ref_baijing_fitting_reward_20261107.png",
    })


def main():
    test_text_matrix()
    test_screenshot_matrix()
    test_ratio_points()
    test_wave_parsing()
    test_state_machine()
    test_weekly_gate()
    test_wiring()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
