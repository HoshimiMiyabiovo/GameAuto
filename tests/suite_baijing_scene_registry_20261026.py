"""白荆回廊「场景退出登记表」回归（2026-10-26 重构）。

背景：12:50 那轮午后茶憩掉进休息室，旧代码点左下角色头像进入「关注」特写，
之后 `_return_to_task_panel` 连按 5 次 ESC（这两个界面都不吃 ESC）→ 任务列表
反复"重开失败"→ 友情勋章/显影行动/领取/叶脉联结/俱乐部/零售全部跳过，
整轮日常只剩 2/16、活跃度 20/100。

重构：`runtime/baijing_daily.py` 的 `recover_scene()` + `SCENE_RULES` ——
每轮只取一次 OCR，按登记表匹配界面并执行它的专属退出；匹配不上或反复退不出去
就把当帧截图 + 文字落进 flow_failures（下次照证据加一行登记，不靠猜）。

覆盖：
1. 五态判定（panel / main / 休息室 / 关注特写 / 未知）
2. 休息室：左上「返回」→ 确认框「确认」，全程零 ESC
3. 关注特写：先「退出关注」，下一轮再离开休息室
4. 过场遮罩 / 茶憩摘要：各自的点法
5. 一轮恢复只截一次屏（OCR 调用计数）
6. 未知场景 → 落一条证据，返回 unknown 交回兜底
7. 场景反复退不出去 → 证据 + scene:<名称>
8. 登记表顺序：任务面板特征优先，绝不"在面板上点返回"
9. 三个恢复入口（回面板 / 回主界面 / 重开列表）都走登记表且不再 ESC 空转
10. 静态：规则表内容 + 旧的一次性场景函数确已删除

跑法：.venv\\Scripts\\python.exe artifacts\\test_baijing_scene_registry_20261026.py
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.baijing_daily as bd  # noqa: E402

LOG = logging.getLogger("scene-registry-test")
FAILURES = []

WIN = (0, 0, 1920, 1080)
BACK_XY = (194, 52)
CONFIRM_XY = (1196, 684)
FOCUS_XY = (1765, 153)
VISIT_XY = (1338, 1004)
ARRANGE_XY = (1482, 1004)
CONTINUE_XY = (960, 980)
SUMMARY_XY = (700, 300)
TASK_BTN_XY = (1540, 890)
ACTIVITY_XY = (585, 356)
TEA_ROW_XY = (1200, 400)
TEA_ENTRY_XY = (1695, 896)     # 休息室右下「午后茶憩」立牌卡（比例换算值）
CLAIM_XY = (630, 762)          # 定巡之礼弹窗「可领取」（2026-11-06 09:15 实拍帧 OCR 位置）
EVENT_CLOSE_XY = (1752, 258)   # 定巡之礼右上 X（比例 0.913,0.239 换算值）
ANNOUNCE_BLANK_XY = (192, 540)  # 活动公告点空白关闭（比例 0.10,0.50 换算值）


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def time(self):
        self.t += 0.05
        return self.t

    def sleep(self, s):
        self.t += float(s)


def _near(a, b, tol=16):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


class Screen:
    """界面状态机：lounge / focus / confirm / overlay / tea_summary / main / panel / blank。"""

    def __init__(self, state="lounge", leave_works=True, panel_marker_too=False,
                 switch_after=0, next_state="main"):
        self.state = state
        self.leave_works = leave_works
        self.panel_marker_too = panel_marker_too
        self.switch_after = switch_after      # ocr_calls 达到后切 next_state（模拟加载完成）
        self.next_state = next_state
        self.claimed = False
        self.clicks = []
        self.ratios = []
        self.esc = 0
        self.ocr_calls = 0
        self.failures = []
        self._saved = {}

    # ---- OCR：一轮一份结果 ----
    def _texts(self):
        if self.state == "lounge":
            data = {"拜访他人": VISIT_XY, "布置模式": ARRANGE_XY, "返回": BACK_XY}
        elif self.state == "confirm":
            data = {
                "拜访他人": VISIT_XY, "布置模式": ARRANGE_XY,
                "返回": BACK_XY, "确认": CONFIRM_XY,
            }
        elif self.state == "focus":
            data = {"退出关注": FOCUS_XY}
        elif self.state == "overlay":
            data = {"点击屏幕继续": CONTINUE_XY, "拜访他人": VISIT_XY}
        elif self.state == "tea_summary":
            data = {"茶憩结束": SUMMARY_XY, "拜访他人": VISIT_XY, "布置模式": ARRANGE_XY}
        elif self.state == "memory_detail":
            # 2026-10-27 10:18 取证帧：烙痕等级/特质提升/等级提升 + 左上返回
            data = {"烙痕等级": (500, 300), "特质提升": (761, 209), "等级提升": (447, 208), "返回": BACK_XY}
        elif self.state == "memory_detail_v2":
            # 2026-10-29 20:03 取证帧：技能同调页签，没有「特质提升/烙痕等级」
            data = {"基础属性": (900, 300), "漫巡属性": (1100, 300), "漫巡技能": (1240, 300),
                    "烙痕唤醒": (1400, 300), "技能同调": (1560, 300), "返回": BACK_XY}
        elif self.state == "level_up":
            # 2026-10-29 20:12 取证帧：整屏「16 计划等级提升」，点任意处继续
            data = {"计划等级提升": (960, 500)}
        elif self.state == "memory_list":
            # 2026-10-30 10:25 取证帧：烙痕列表（前往技能列表/只看红点/排序与筛选）+ 左上返回
            data = {"前往技能列表": (760, 58), "只看红点": (900, 58), "排序与筛选": (1500, 58), "返回": BACK_XY}
        elif self.state == "event_dialog":
            # 2026-10-30 10:35 取证帧：定巡之礼活动弹窗，右上角 X
            data = {"定巡之礼": (500, 200)}
        elif self.state == "event_claim":
            # 2026-11-06 09:15 取证帧：定巡之礼弹窗，第 2 天「可领取」
            data = {"定巡之礼": (500, 200)}
            if not self.claimed:
                data["可领取"] = CLAIM_XY
        elif self.state == "loading":
            # 2026-10-31～11-06 五次取证帧：加载页 = logo + 轮换提示 + 进度
            data = {"ASHECHOES": (960, 540)}
        elif self.state == "event_announce":
            # 2026-11-04 09:13 取证帧：活动公告横幅（生死轮回×叶脉联结计划），
            # 关闭提示/勾选在横幅下部（960 图 ×2 换算到 1920 屏幕坐标）
            data = {"点击空白区域关闭": (962, 866), "本期不再显示": (1514, 864)}
        elif self.state == "tea_room":
            # 2026-10-27 10:22 取证帧：房间常驻陈设（飞镖/棒球）+ 话题名 + 左上返回
            data = {"茶憩纪念函": (900, 800), "飞镖": (1725, 573), "棒球": (1719, 419), "返回": BACK_XY}
        elif self.state == "retail":
            # 2026-10-26 20:10 取证帧：有猫零售左侧栏目 + 左上返回
            data = {"有猫精选": (1286, 219), "精选礼包": (202, 234), "有猫推荐": (900, 60), "返回": BACK_XY}
        elif self.state == "panel":
            # 任务列表里也有「午后茶憩」任务行文字，绝不能被当成休息室特征
            data = {"累计活跃度": ACTIVITY_XY, "午后茶憩": TEA_ROW_XY}
        elif self.state == "main":
            data = {}
        else:
            data = {"未知界面元素": (900, 500)}
        if self.panel_marker_too:
            data = dict(data)
            data["累计活跃度"] = ACTIVITY_XY
        return data

    def ocr(self, logger, hwnd=None, region=None):
        self.ocr_calls += 1
        if self.switch_after and self.ocr_calls >= self.switch_after and self.state == "loading":
            self.state = self.next_state
        return [
            {"text": t, "confidence": 0.75, "center": xy, "box": (xy[0], xy[1], xy[0], xy[1])}
            for t, xy in self._texts().items()
        ]

    # ---- 点击 ----
    def click(self, x, y, *a, **k):
        self.clicks.append((int(x), int(y)))
        if self.state == "focus" and _near((x, y), FOCUS_XY):
            self.state = "lounge"
        elif self.state == "lounge" and _near((x, y), BACK_XY):
            self.state = "confirm" if self.leave_works else "lounge"
        elif self.state == "confirm" and _near((x, y), CONFIRM_XY):
            self.state = "main" if self.leave_works else "confirm"
        elif self.state == "overlay" and _near((x, y), CONTINUE_XY):
            self.state = "lounge"
        elif self.state == "tea_summary" and _near((x, y), (int(bd.TEA_SUMMARY_CLICK[0] * WIN[2]),
                                                            int(bd.TEA_SUMMARY_CLICK[1] * WIN[3]))):
            self.state = "lounge"
        elif self.state == "memory_detail" and _near((x, y), BACK_XY):
            # 返回后是烙痕列表页——没有独占特征，真实行为就是交回通用兜底（unknown）
            self.state = "blank"
        elif self.state == "memory_detail_v2" and _near((x, y), BACK_XY):
            self.state = "blank"
        elif self.state == "level_up" and _near((x, y), (960, 540)):
            self.state = "main"
        elif self.state == "memory_list" and _near((x, y), BACK_XY):
            self.state = "panel"
        elif self.state == "event_dialog" and _near((x, y), EVENT_CLOSE_XY):
            self.state = "main"
        elif self.state == "event_claim" and _near((x, y), CLAIM_XY):
            # 领取后该天变已领（真实弹窗里「可领取」会消失），弹窗本体仍在
            self.claimed = True
        elif self.state == "event_claim" and _near((x, y), EVENT_CLOSE_XY):
            self.state = "main"
        elif self.state == "event_announce" and _near((x, y), ANNOUNCE_BLANK_XY):
            # 公告是模态的「点击空白区域关闭」，点空白即关
            self.state = "main"
        elif self.state == "tea_room" and _near((x, y), BACK_XY):
            # 与休息室同一套离开交互：返回 → 确认框
            self.state = "confirm"
        elif self.state == "retail" and _near((x, y), BACK_XY):
            self.state = "main"
        elif self.state == "main" and _near((x, y), TASK_BTN_XY):
            self.state = "panel"

    def click_ratio(self, logger, hwnd, ratio, label=""):
        self.ratios.append((tuple(ratio), label))
        x, y = int(ratio[0] * WIN[2]), int(ratio[1] * WIN[3])
        if "返回" in label:
            x, y = BACK_XY
        elif "确认" in label:
            x, y = CONFIRM_XY
        self.click(x, y)
        return (x, y)

    def press(self, key):
        self.esc += 1

    def record_failure(self, logger, hwnd, step, detail):
        self.failures.append((step, detail))

    # ---- 安装 / 还原 ----
    def install(self, extra=None):
        self._saved = {k: getattr(bd, k) for k in (
            "ocr_window_content", "_pyautogui_click_guarded", "_click_ratio",
            "pyautogui", "time", "_ensure_window", "_window_region",
            "detect_baijing_main_ui", "_record_step_failure",
        )}

        class FakePyAuto:
            @staticmethod
            def press(key):
                self.press(key)

        bd.ocr_window_content = self.ocr
        bd._pyautogui_click_guarded = self.click
        bd._click_ratio = self.click_ratio
        bd._ensure_window = lambda logger, hwnd: hwnd
        bd._window_region = lambda hwnd: WIN
        bd.detect_baijing_main_ui = lambda logger, hwnd, budget_sec=2.0: (self.state == "main", "fake")
        bd._record_step_failure = self.record_failure
        bd.pyautogui = FakePyAuto()
        bd.time = FakeTime()
        for name, fn in (extra or {}).items():
            self._saved.setdefault(name, getattr(bd, name))
            setattr(bd, name, fn)

    def restore(self):
        for k, v in self._saved.items():
            setattr(bd, k, v)

    def run(self, fn, *a, **k):
        self.install(k.pop("extra", None))
        try:
            return fn(*a, **k)
        finally:
            self.restore()


# ── 1/2/3/4 单场景退出 ────────────────────────────────────────────
def test_single_scenes():
    print("[1-4] 四个已登记场景各自的专属退出")
    cases = [
        ("lounge", "main", [BACK_XY, CONFIRM_XY], 0),
        ("focus", "main", [FOCUS_XY, BACK_XY, CONFIRM_XY], 0),
        ("overlay", "main", [CONTINUE_XY, BACK_XY, CONFIRM_XY], 0),
        ("tea_summary", "main", [(int(bd.TEA_SUMMARY_CLICK[0] * 1920), int(bd.TEA_SUMMARY_CLICK[1] * 1080)),
                                 BACK_XY, CONFIRM_XY], 0),
        ("panel", "panel", [], 0),
        ("main", "main", [], 0),
        ("blank", "unknown", [], 0),
    ]
    for state, want_state, want_clicks, want_esc in cases:
        scr = Screen(state=state)
        got = scr.run(lambda: bd.recover_scene(LOG, 1, context=f"t-{state}"))
        check(f"{state} → 状态 {want_state}", got[0] == want_state, got)
        check(f"{state} → 点击序列", scr.clicks == want_clicks, (state, scr.clicks))
        check(f"{state} → ESC 次数 {want_esc}", scr.esc == want_esc, scr.esc)


# ── 4.5 2026-10-27 新登记的三个场景 ───────────────────────────────
def test_new_scenes_20261027():
    print("[4.5] 烙痕详情 / 茶憩房间 / 有猫零售（失败取证驱动补登记）")
    # 烙痕详情：点返回 → 烙痕列表无独占特征 → 如实报 unknown（列表交给通用兜底）
    scr = Screen(state="memory_detail")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-memory"))
    check("memory_detail 点了左上返回", BACK_XY in scr.clicks, scr.clicks)
    check("memory_detail 做过动作", acted is True, acted)
    check("memory_detail 后续如实报 unknown", state == "unknown", state)

    # 茶憩房间：返回 → 确认框 → 回到（测试里的）主界面，零 ESC
    scr = Screen(state="tea_room")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-tearoom"))
    check("tea_room → 状态 main", state == "main", state)
    check("tea_room 点击＝返回→确认", scr.clicks == [BACK_XY, CONFIRM_XY], scr.clicks)
    check("tea_room 零 ESC", scr.esc == 0, scr.esc)

    # 有猫零售：点返回直接回主界面
    scr = Screen(state="retail")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-retail"))
    check("retail → 状态 main", state == "main", state)
    check("retail 只点一次返回", scr.clicks == [BACK_XY], scr.clicks)

    # 常量：话题名不能当茶憩特征（每日轮换）
    check("茶憩登记特征不含话题名", not any("余温" in m or "话题" in m for m in bd.TEA_ROOM_REGISTRY_MARKERS),
          bd.TEA_ROOM_REGISTRY_MARKERS)

    # 2026-10-29 20:03：技能同调页签的烙痕详情（旧特征没命中）
    scr = Screen(state="memory_detail_v2")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-memory-v2"))
    check("memory_detail_v2 点了左上返回", BACK_XY in scr.clicks, scr.clicks)
    check("memory_detail_v2 后续如实报 unknown", state == "unknown", state)

    # 2026-10-29 20:12：计划等级提升整屏动画，点继续回主界面
    scr = Screen(state="level_up")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-levelup"))
    check("level_up → 状态 main", state == "main", state)
    check("level_up 点击屏幕中心继续", scr.clicks == [(960, 540)], scr.clicks)
    check("level_up 零 ESC", scr.esc == 0, scr.esc)
    check("烙痕详情特征含今日页签",
          "漫巡属性" in bd.MEMORY_DETAIL_MARKERS and "烙痕唤醒" in bd.MEMORY_DETAIL_MARKERS,
          bd.MEMORY_DETAIL_MARKERS)

    # 2026-10-30 用户实拍弹窗定标 docs/refs/baijing_memory_enhance_20261030.png：
    # 「指定等级」行 [重置][Lv][+][最大]，重置 (1189,669)、+ 约 (1424,669)。
    # 有重置锚点时直接推算 +，不再依赖第二次 OCR。
    orig_region, orig_ocr = bd._window_region, bd._ocr_best_match
    bd._window_region = lambda hwnd: (0, 0, 1919, 1079)
    def _boom(*a, **k):
        raise AssertionError("有锚点时不应再 OCR")
    bd._ocr_best_match = _boom
    try:
        pos = bd._find_plus_button(LOG, 1, anchor=(1189, 669))
    finally:
        bd._window_region, bd._ocr_best_match = orig_region, orig_ocr
    check("重置锚点推算 + 按钮（截图定标）", pos == (1424, 669), pos)
    check("参考截图已入库", (PROJECT_ROOT / "docs" / "refs" / "baijing_memory_enhance_20261030.png").is_file())
    check("重置 OCR 限定在指定等级行", bd.MEMORY_RESET_REGION == (0.55, 0.58, 0.68, 0.66))
    src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "baijing_daily.py").read_text(encoding="utf-8")
    check("重置后弹窗消失时不盲点 +", "重置后强化弹窗消失，停止（不盲点 +）" in src)

    # 2026-10-30 10:25：烙痕列表页（记忆烙痕返回后卡住）
    scr = Screen(state="memory_list")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-memory-list"))
    check("memory_list → 状态 panel", state == "panel", state)
    check("memory_list 点了左上返回", BACK_XY in scr.clicks, scr.clicks)

    # 2026-10-30 10:35：定巡之礼活动弹窗，点右上 X
    scr = Screen(state="event_dialog")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-event"))
    check("event_dialog → 状态 main", state == "main", state)
    check("event_dialog 点了右上关闭", any(_near(c, (1752, 258)) for c in scr.clicks), scr.clicks)
    check("event_dialog 零 ESC", scr.esc == 0, scr.esc)


# ── 4.6 2026-11-07：加载过场 + 定巡之礼先领后关 ───────────────────
def test_loading_and_claim_20261107():
    print("[4.6] 加载过场只等待 / 定巡之礼先领「可领取」再点 X")
    # 加载过场：等待特征消失后交回下一轮识别，全程零点击零 ESC
    scr = Screen(state="loading", switch_after=3)
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-loading"))
    check("loading 加载完成 → 状态 main", state == "main", state)
    check("loading 做过等待动作", acted is True, acted)
    check("loading 零点击（点了也无效）", scr.clicks == [], scr.clicks)
    check("loading 零 ESC", scr.esc == 0, scr.esc)

    # 游戏真卡死：等待有上限，返回 False 落证据交回兜底，不无限等
    scr = Screen(state="loading")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-loading-stuck"))
    check("loading 卡死 → scene:加载过场", state == "scene:加载过场", state)
    check("loading 卡死未做有效动作", acted is False, acted)
    check("loading 卡死零点击", scr.clicks == [], scr.clicks)
    check("loading 卡死落了证据",
          any("反复退出失败" in d for _, d in scr.failures), scr.failures)

    # 定巡之礼弹窗：先点「可领取」，弹窗还在再点右上 X（顺序锁定）
    scr = Screen(state="event_claim")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-event-claim"))
    check("event_claim → 状态 main", state == "main", state)
    check("event_claim 点击序列 = 先领取后关闭",
          scr.clicks == [CLAIM_XY, EVENT_CLOSE_XY], scr.clicks)
    check("event_claim 零 ESC", scr.esc == 0, scr.esc)

    # 真实取证帧（11-06 #197 的原图）两个特征都必须能 OCR 出来
    ref = PROJECT_ROOT / "data" / "artifacts" / "ref_baijing_dingxun_dialog_20261107.jpg"
    check("定巡之礼取证帧已存档为长期参考图", ref.is_file(), ref)
    if ref.is_file():
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            print("  SKIP RapidOCR 未安装，跳过真图断言")
            return
        result, _ = RapidOCR()(str(ref))
        texts = {str(t): float(c) for _, t, c in (result or [])}
        ding = max((c for t, c in texts.items() if "定巡之礼" in t), default=0.0)
        claim = max((c for t, c in texts.items() if "可领取" in t), default=0.0)
        check("真图 OCR 出「定巡之礼」(≥0.55)", ding >= 0.55, ding)
        check("真图 OCR 出「可领取」(≥0.5)", claim >= 0.5, claim)


# ── 4.7 2026-11-07：活动公告横幅点空白关闭 ────────────────────────
def test_event_announce_20261107():
    print("[4.7] 活动公告：点空白关闭（用户拍板「他只是个活动公告」）")
    scr = Screen(state="event_announce")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-announce"))
    check("event_announce → 状态 main", state == "main", state)
    check("点的是左边缘空白而非横幅本体",
          any(_near(c, ANNOUNCE_BLANK_XY) for c in scr.clicks), scr.clicks)
    check("零 ESC", scr.esc == 0, scr.esc)
    check("特征不含「叶脉联结计划」（叶脉页同名，防误点领奖页）",
          all("叶脉联结计划" not in m for m in bd.EVENT_ANNOUNCE_MARKERS),
          bd.EVENT_ANNOUNCE_MARKERS)

    # 真实取证帧（11-04 #163 原图）两个特征都必须能 OCR 出来
    ref = PROJECT_ROOT / "data" / "artifacts" / "ref_baijing_loading_event_banner_20261104.jpg"
    check("公告取证帧已存档为长期参考图", ref.is_file(), ref)
    if ref.is_file():
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            print("  SKIP RapidOCR 未安装，跳过真图断言")
            return
        result, _ = RapidOCR()(str(ref))
        texts = {str(t): float(c) for _, t, c in (result or [])}
        hint = max((c for t, c in texts.items() if "点击空白区域关闭" in t), default=0.0)
        once = max((c for t, c in texts.items() if "本期不再显示" in t), default=0.0)
        check("真图 OCR 出「点击空白区域关闭」(≥0.55)", hint >= 0.55, hint)
        check("真图 OCR 出「本期不再显示」(≥0.55)", once >= 0.55, once)


# ── 5. 一轮只截一次屏 ─────────────────────────────────────────────
def test_single_ocr_per_round():
    print("[5] 一轮恢复只截一次屏（旧实现每个判定各截一次）")
    scr = Screen(state="panel")
    scr.run(lambda: bd.recover_scene(LOG, 1, context="ocr"))
    check("面板态一次 OCR 就返回", scr.ocr_calls == 1, scr.ocr_calls)

    scr2 = Screen(state="lounge")
    scr2.run(lambda: bd.recover_scene(LOG, 1, context="ocr2"))
    # 休息室一轮 = 场景判定 1 次 + 点返回后确认框再识别 1 次；离开后主界面一轮 1 次
    check("休息室恢复全程 OCR 次数 ≤5", scr2.ocr_calls <= 5, scr2.ocr_calls)


# ── 6/7 证据落库 ──────────────────────────────────────────────────
def test_unknown_scene_evidence():
    print("[6] 未知界面：落一条证据并返回 unknown（下次照图加登记）")
    scr = Screen(state="blank")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="未知测试"))
    check("状态 unknown", state == "unknown", state)
    check("零动作", acted is False and not scr.clicks, (acted, scr.clicks))
    check("落了证据记录", any(s == "场景恢复:未知测试" for s, _ in scr.failures), scr.failures)
    detail = scr.failures[0][1] if scr.failures else ""
    check("证据里带当帧文字", "未知界面元素" in detail, detail)


def test_stuck_scene_evidence():
    print("[7] 场景反复退不出去：证据 + scene:<名称>，不无限点")
    scr = Screen(state="lounge", leave_works=False)
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="卡休息室"))
    check("状态 scene:休息室", state == "scene:休息室", state)
    check("做过动作", acted is True, acted)
    check("点击次数有界", len(scr.clicks) <= 2 * bd.SCENE_RECOVERY_ROUNDS, scr.clicks)
    check("落了反复失败证据", any("反复退出失败" in d for _, d in scr.failures), scr.failures)
    check("没有 ESC 空转", scr.esc == 0, scr.esc)


# ── 8. 登记表顺序 ─────────────────────────────────────────────────
def test_rule_order():
    print("[8] 登记表顺序：面板特征优先，绝不在任务面板上点「返回」")
    check("规则顺序", [r["name"] for r in bd.SCENE_RULES] == ["加载过场", "活动公告", "过场遮罩", "计划等级提升",
                                                              "定巡之礼", "茶憩摘要", "关注特写", "休息室",
                                                              "烙痕详情", "烙痕列表", "茶憩房间", "有猫零售"],
          [r["name"] for r in bd.SCENE_RULES])
    check("休息室特征不含「午后茶憩」（任务行也有这四个字）",
          "午后茶憩" not in bd.REST_ROOM_MARKERS, bd.REST_ROOM_MARKERS)
    scr = Screen(state="lounge", panel_marker_too=True)
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1))
    check("同时能看到面板特征 → 判 panel 且零点击", state == "panel" and not scr.clicks, (state, scr.clicks))


# ── 9. 三个恢复入口都走登记表 ─────────────────────────────────────
def test_call_sites():
    print("[9] 三个恢复入口在休息室残留时的行为")
    scr = Screen(state="lounge")
    ok = scr.run(
        lambda: bd._return_to_task_panel(LOG, 1),
        extra={
            "_find_task_button": lambda logger, hwnd, stop_check=None: (TASK_BTN_XY, "ocr"),
            "_ensure_daily_tab": lambda logger, hwnd, stop_check=None: True,
        },
    )
    check("_return_to_task_panel 成功回面板", ok is True, ok)
    check("先离开休息室再点任务按钮",
          scr.clicks[:2] == [BACK_XY, CONFIRM_XY] and TASK_BTN_XY in scr.clicks, scr.clicks)
    check("全程零 ESC", scr.esc == 0, scr.esc)

    scr = Screen(state="lounge")
    ok = scr.run(lambda: bd._ensure_main_ui(LOG, 1))
    check("_ensure_main_ui 成功回主界面", ok is True, ok)
    check("点击＝返回→确认", scr.clicks == [BACK_XY, CONFIRM_XY], scr.clicks)
    check("没有按 ESC / 点关闭", scr.esc == 0, scr.esc)

    scr = Screen(state="lounge")
    opens = {"n": 0}

    def fake_open(logger, hwnd, stop_check=None):
        opens["n"] += 1
        if scr.state == "main":
            scr.click(*TASK_BTN_XY)
            return True
        return False        # 休息室里根本没有「任务」按钮

    ok = scr.run(
        lambda: bd._ensure_task_list_open(LOG, 1),
        extra={
            "_wait_task_list": lambda logger, hwnd, timeout=3.0, stop_check=None, snapshot=False: scr.state == "panel",
            "open_daily_task_list": fake_open,
        },
    )
    check("_ensure_task_list_open 最终拿到列表", ok is True, ok)
    check("休息室先被处理", BACK_XY in scr.clicks and CONFIRM_XY in scr.clicks, scr.clicks)
    check("重开次数有界", opens["n"] <= 3, opens)


# ── 10. 静态：旧的一次性场景函数已删除 ────────────────────────────
def test_cleanup():
    print("[10] 静态检查：登记表是唯一入口")
    for gone in ("_leave_rest_room", "_recover_scene_state", "_in_focus_mode", "_exit_focus_mode"):
        check(f"{gone} 已删除", not hasattr(bd, gone))
    check("休息室坐标来自截图",
          0.82 <= bd.REST_ROOM_TEA_ENTRY[0] <= 0.95 and 0.76 <= bd.REST_ROOM_TEA_ENTRY[1] <= 0.90,
          bd.REST_ROOM_TEA_ENTRY)
    check("茶憩流程仍用休息室判定", hasattr(bd, "_in_rest_room"))
    src = Path("src/game_auto/runtime/baijing_daily.py").read_text(encoding="utf-8")
    check("三个入口都调用 recover_scene", src.count("recover_scene(logger, hwnd") >= 3,
          src.count("recover_scene(logger, hwnd"))


def main():
    test_single_scenes()
    test_new_scenes_20261027()
    test_loading_and_claim_20261107()
    test_event_announce_20261107()
    test_single_ocr_per_round()
    test_unknown_scene_evidence()
    test_stuck_scene_evidence()
    test_rule_order()
    test_call_sites()
    test_cleanup()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
