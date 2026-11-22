# -*- coding: utf-8 -*-
"""回归：白荆回廊「角色试玩（试玩关卡）」接入（2026-11-19 用户截图）。

用户 11-19 口述：活动页（活动中心）左列「试玩关卡」→ 角色试玩页，点「开始试玩」
即正常编队战斗页；约 14 天刷新。本套件锁定：
  [1] 页面判定文本快照矩阵（试玩页/hub/战斗共用页互斥）
  [2] 实机截图矩阵（data/artifacts/ref_baijing_trial_20261119.png，缺图/无引擎 SKIP）
  [3] 14 天周期门控 trial_due
  [4] 接线：一条龙挂载点 + 抽卡入口「获取同调者」禁点防呆
  [5] 打桩全流程状态机（主界面→活动中心→试玩→单槽战斗→返回）
  [6] 2026-11-22 #447/#456：活动中心 OCR 未命中不再抛 UnboundLocalError；
      收尾「返回」先判页——主界面上那个坐标是邮件图标（盲点会开邮件面板）

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_baijing_trial_20261119.py -q
"""

import datetime as dt
import logging
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.baijing_flow as flow  # noqa: E402
import game_auto.runtime.baijing_trial as bt  # noqa: E402

ART = PROJECT_ROOT / "data" / "artifacts"
FAILURES = []
LOG = logging.getLogger("baijing_trial_test")
LOG.addHandler(logging.NullHandler())


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} — {detail}")
        FAILURES.append(name)


def _items(*texts, conf=0.9):
    return [{"text": t, "confidence": conf, "center": (100, 100)} for t in texts]


# 2026-11-19 用户截图（1919x1079）实测 OCR 帧：试玩页本体
TRIAL_FRAME = [
    "返回", "客服", "荣耀巡航", "NEW", "NEW", "NEW", "NEW", "NEW",
    "叶脉联结", "分进合击战", "成长投资", "伊琅相思", "新手教学", "30",
    "体验角色完成关卡领取奖励", "试玩关卡", "13天5小时15分", "烙痕限时满级",
    "获取同调者", "开始试玩", "联合训练场", "MENU", "24919904",
]

# 拟合回归套件同源快照（战斗链共用页）
TEAM_FRAME = ["返回", "队伍10", "载入队列", "自动编队", "选择参战同调者",
              "查看详情", "作战开始", "MENU"]
DEPLOY_FRAME = ["体质", "防御", "深度等级", "刻印评级", "刻印提供的激励：",
                "开始战斗", "01", "02", "03"]
BATTLE_FRAME = ["终端没有被摧毁", "当前波次：1/25", "战术终端", "后备队", "暂停"]
RESULT_FRAME = ["作战完成", "达成波次▼", "25", "转化奖励", "本次编队"]
REWARD_FRAME = ["新物品已存入S.E.E.D.仓库", "海隅点数", "点击屏幕继续", "15", "20"]
ITEM_POPUP_FRAME = ["获取方式:", "库存", "17324", "错金拓片兑换", "前往"]
MAIN_FRAME = ["活动总览", "外勤", "同调者", "记忆烙痕", "战术刻印", "精神深潜",
              "有猫零售", "活动中心", "功能菜单"]
HUB_FRAME = ["返回", "荣耀巡航", "叶脉联结", "分进合击战", "成长投资", "新手教学",
             "烙痕限时满级", "联合训练场", "MENU"]


def test_text_matrix():
    print("[1] 文本快照矩阵（试玩链各页互斥）")
    cases = {
        "trial": TRIAL_FRAME,
        "hub": HUB_FRAME,
        "team": TEAM_FRAME,
        "deploy": DEPLOY_FRAME,
        "battle": BATTLE_FRAME,
        "result": RESULT_FRAME,
        "reward": REWARD_FRAME,
        "item_popup": ITEM_POPUP_FRAME,
        "main": MAIN_FRAME,
    }
    for page, texts in cases.items():
        got = bt.classify_trial_page(_items(*texts))
        check(f"{page} 帧分类正确", got == page, f"got={got}")
    # 试玩页 vs hub：左列菜单同词，只有试玩页带按钮+正文双证据
    check("只有「开始试玩」不判试玩页",
          bt.classify_trial_page(_items("开始试玩", "荣耀巡航", "叶脉联结")) == "unknown")
    check("只有「体验角色完成关卡领取奖励」不判试玩页",
          bt.classify_trial_page(_items("体验角色完成关卡领取奖励")) == "unknown")
    check("左列菜单只有 2 个标记不判 hub",
          bt.classify_trial_page(_items("荣耀巡航", "叶脉联结", "返回")) == "unknown")
    check("单独「联合训练场」不判 hub（外勤页同词，故意不收）",
          bt.classify_trial_page(_items("联合训练场")) == "unknown")
    check("单独「活动中心」不判主界面", bt.classify_trial_page(_items("活动中心")) == "unknown")


# ── [2] 实机截图矩阵 ───────────────────────────────────────────────────
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


def test_screenshot_matrix():
    print("[2] 实机截图矩阵（ref_baijing_trial_20261119.png）")
    shot = ART / "ref_baijing_trial_20261119.png"
    if not shot.exists():
        print("  SKIP 实机参考图不存在（data/artifacts 被 gitignore，文本快照已覆盖）")
        return
    from game_auto.runtime import ocr_click_tester as oc

    getter = getattr(oc, "_get_onnxocr_engine", None)
    engine = getter() if getter else None
    if not engine:
        print("  SKIP onnxocr v6 引擎不可用（文本快照已覆盖判定逻辑）")
        return
    rows = _rows_from_image(engine, shot)
    hits = {name for name, fn in bt.TRIAL_PAGE_CHECKS if fn(rows)}
    check("截图分类为 trial", bt.classify_trial_page(rows) == "trial",
          f"got={bt.classify_trial_page(rows)} ocr={[r['text'] for r in rows][:12]}")
    check("截图不误命中别的页", hits == {"trial"}, f"hits={hits}")


# ── [3] 14 天周期门控 ─────────────────────────────────────────────────
def test_trial_due():
    print("[3] 14 天周期门控")
    today = dt.date(2026, 11, 19)
    check("首跑（无记录）→ 到期", bt.trial_due(None, today))
    check("空串 → 到期", bt.trial_due("", today))
    check("今天跑过 → 未到期", not bt.trial_due("2026-11-19", today))
    check("13 天前 → 未到期", not bt.trial_due("2026-11-06", today))
    check("14 天前 → 到期", bt.trial_due("2026-11-05", today))
    check("脏数据 → 到期（不卡死流程）", bt.trial_due("not-a-date", today))
    check("周期常量 = 14 天", bt.TRIAL_CYCLE_DAYS == 14)


# ── [4] 接线与防呆 ────────────────────────────────────────────────────
def test_wiring():
    print("[4] 一条龙接线 + 抽卡入口禁点")
    flow_src = (PROJECT_ROOT / "src/game_auto/runtime/baijing_flow.py").read_text(encoding="utf-8")
    trial_src = (PROJECT_ROOT / "src/game_auto/runtime/baijing_trial.py").read_text(encoding="utf-8")
    check("flow 定义 _maybe_run_trial_periodic", "def _maybe_run_trial_periodic(" in flow_src)
    call = flow_src.find("_maybe_run_trial_periodic(logger, hwnd, stop_check=stop_check)")
    fitting = flow_src.find("_maybe_run_fitting_weekly(logger, hwnd, stop_check=stop_check)")
    joint = flow_src.find("run_joint_training_recon(logger, hwnd, stop_check=stop_check)")
    check("调用挂在拟合回归之后", call > fitting > 0, f"call={call} fitting={fitting}")
    check("调用在联合训练场之前（joint 的 _ensure_main_ui 兜底恢复）", 0 < call < joint,
          f"call={call} joint={joint}")
    check("meta 键独立", bt.TRIAL_META_KEY == "periodic_flow:BAIJING_TRIAL")
    check("「获取同调者」只作禁点常量/文档，不作点击目标",
          '_ocr_click(logger, hwnd, "获取同调者"' not in trial_src
          and "FORBIDDEN_TEXTS" in trial_src)
    check("5 个槽位、x 递增、y 在缩略图条带内",
          len(bt.SLOT_POINTS) == 5
          and all(a[0] < b[0] for a, b in zip(bt.SLOT_POINTS, bt.SLOT_POINTS[1:]))
          and all(0.2 < y < 0.45 for _, y in bt.SLOT_POINTS))
    ratios = [bt.P_TRIAL_TAB, bt.P_BACK, bt.P_START_TRIAL, *bt.SLOT_POINTS]
    check("全部比例坐标在窗口内", all(0 < x < 1 and 0 < y < 1 for x, y in ratios))


def test_forbidden_click():
    print("[4b] 抽卡入口禁点防呆")
    clicks = []
    with patch.object(bt, "_ocr_best_match", return_value={"text": "获取同调者", "center": (5, 5)}), \
         patch.object(bt, "_pyautogui_click_guarded", side_effect=lambda *a: clicks.append(a)), \
         patch.object(bt, "_click", side_effect=lambda *a, **k: clicks.append(a) or (1, 1)):
        ok = bt._ocr_click(LOG, 123, "获取同调者", "抽卡", (0, 0, 1, 1), fallback=(0.5, 0.5))
    check("「获取同调者」拒点返回 False", ok is False)
    check("拒点后没有任何点击发生", clicks == [], clicks)


# ── [5] 打桩全流程状态机 ──────────────────────────────────────────────
class Script:
    """按脚本返回页面；记录点击，不碰真实游戏。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.clicks = []

    def page_now(self, logger, hwnd):
        page = self.pages.pop(0) if self.pages else "unknown"
        return page, _items("开始试玩" if page == "trial" else page)

    def click(self, logger, hwnd, point, label=""):
        self.clicks.append(label)
        return (1, 1)

    def guarded(self, x, y):
        self.clicks.append(f"abs({x},{y})")


def _run_script(pages, slot_points=(bt.SLOT_POINTS[0],), wait_team=None):
    script = Script(pages)
    wait_team = wait_team or (True, "team", _items("作战开始"))
    with patch.object(bt, "_trial_page_now", side_effect=script.page_now), \
         patch.object(bt, "_ocr_best_match", return_value={"text": "ok", "center": (5, 5)}), \
         patch.object(bt, "_pyautogui_click_guarded", side_effect=script.guarded), \
         patch.object(bt, "_click", side_effect=script.click), \
         patch.object(bt, "_wait_page", return_value=wait_team), \
         patch.object(bt, "_wait_deploy_settled", return_value=True), \
         patch.object(bt, "SLOT_POINTS", slot_points), \
         patch.object(bt, "_record_failure"), \
         patch.object(bt.time, "sleep"), \
         patch("game_auto.runtime.baijing_scene._ensure_main_ui", return_value=True):
        out = bt.run_trial_activity(LOG, 123)
    return out, script


def test_flow_done():
    print("[5] 打桩全流程：主界面 → 活动中心 → 试玩 → 单槽战斗 → 返回")
    # 调用序：hub → trial → (槽位点击后)trial → deploy → battle → result → trial
    # 末尾多一帧 trial：收尾判页通过才点「返回」（2026-11-22 #447/#456 新增闸门）
    out, script = _run_script(
        ["hub", "trial", "trial", "deploy", "battle", "result", "trial", "trial"]
    )
    check("整链成功", out.get("ok") is True, out.get("detail"))
    check("完成 1 场并记录", out.get("fought") == ["开始试玩"] or len(out.get("fought") or []) == 1,
          out.get("fought"))
    # 活动中心/试玩关卡/开始试玩 走 OCR 命中坐标（abs 记录），其余走比例 _click
    check("活动中心/试玩关卡/开始试玩 三次 OCR 命中点击",
          script.clicks.count("abs(5,5)") == 3, script.clicks)
    check("点击了「试玩页返回」", "试玩页返回" in script.clicks, script.clicks)
    check("点击了槽位/开战动作", any("槽位" in c or "作战开始" in c or "开始战斗" in c
                                    for c in script.clicks), script.clicks)
    check("退场后确认回主界面", out.get("returned_main") is True)


def test_flow_skip_slot():
    print("[5b] 打桩：点「开始试玩」原地不动 → 跳过槽位不判失败")
    out, _script = _run_script(["hub", "trial", "trial"],
                               wait_team=(False, "trial", _items("开始试玩")))
    check("跳过后整链仍成功", out.get("ok") is True, out.get("detail"))
    check("完成 0 场、槽位记 skipped",
          (out.get("fought") or []) == []
          and (out.get("slots") or [{}])[0].get("state") == "skipped", out.get("slots"))




# ── [6] 2026-11-22 #447/#456：OCR 未命中 → 不抛异常，收尾不盲点 ──────────
def test_ocr_miss_no_crash():
    print("[6] 活动中心 OCR 未命中：不抛 UnboundLocalError，收尾不盲点（#447/#456）")
    script = Script(["main", "main"])
    failures = []
    with patch.object(bt, "_trial_page_now", side_effect=script.page_now), \
         patch.object(bt, "_ocr_best_match", return_value=None), \
         patch.object(bt, "_click", side_effect=script.click), \
         patch.object(bt, "_pyautogui_click_guarded", side_effect=script.guarded), \
         patch.object(bt, "_record_failure",
                      side_effect=lambda *a, **k: failures.append(a)), \
         patch.object(bt.time, "sleep"), \
         patch("game_auto.runtime.baijing_scene._ensure_main_ui", return_value=True):
        out = bt.run_trial_activity(LOG, 123)
    check("整链不抛异常（UnboundLocalError 已修）", out.get("ok") is False, out)
    check("停在「打开活动中心」", out.get("step") == "打开活动中心", out.get("step"))
    check("失败取证带当帧 OCR（不再是未绑定变量）",
          bool(failures) and isinstance(failures[0][4], list) and failures[0][4],
          failures)
    check("收尾在主界面 → 不点「试玩页返回」",
          "试玩页返回" not in script.clicks, script.clicks)
    check("收尾仍恢复主界面", out.get("returned_main") is True, out.get("returned_main"))


def main():
    test_text_matrix()
    test_screenshot_matrix()
    test_trial_due()
    test_wiring()
    test_forbidden_click()
    test_flow_done()
    test_flow_skip_slot()
    test_ocr_miss_no_crash()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
