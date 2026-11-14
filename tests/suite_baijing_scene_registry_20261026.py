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
11. 2026-11-12 #279：轮次耗尽但每轮都在推进（收尾当帧已是另一登记场景/
    主界面/面板）≠ 失败，不落假证据；同名场景卡死仍照旧落证据
12. 2026-11-13 #282：好友页登记（detector ≥2 独有词 + OCR 截字变体
    「键领取/赠送」），退出点左上「返回」零 ESC

跑法：.venv\\Scripts\\python.exe artifacts\\test_baijing_scene_registry_20261026.py
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.baijing_scene as bd  # noqa: E402
import game_auto.runtime.baijing_common as bcommon  # noqa: E402
import game_auto.runtime.baijing_tasklist as btasklist  # noqa: E402
import game_auto.runtime.baijing_task_handlers as bhandlers  # noqa: E402

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
                 switch_after=0, next_state="main", switch_from="loading"):
        self.state = state
        self.leave_works = leave_works
        self.panel_marker_too = panel_marker_too
        self.switch_after = switch_after      # ocr_calls 达到后切 next_state（模拟加载完成）
        self.next_state = next_state
        self.switch_from = switch_from
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
            # 2026-10-31～11-06 五次取证帧：加载页 = 拉丁 logo + 轮换提示 + 进度
            data = {"ASHECHOES": (960, 540), "99%": (1797, 1022)}
        elif self.state == "loading_cn":
            # 2026-11-10 10:00:32 生产 onnxocr v6 真实当帧：中央 logo 认成
            # 「白荆廊」(0.85)，拉丁小字整帧读成「24919904」，ASHECHOES 全无
            data = {"白荆廊": (967, 476),
                    "【筑术师】具备极远的射程和较大的攻击范围": (960, 1010),
                    "98%": (1797, 1022), "24919904": (1661, 1068)}
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
        elif self.state == "friend":
            # 2026-11-13 10:18 #282 取证帧：好友页（友情勋章任务领完留在这里）。
            # 注意 OCR 把「一键领取/赠送」的「一」截掉了，只剩「键领取/赠送」。
            data = {"返回": BACK_XY, "好友列表": (233, 167), "列表内查找好友": (600, 120),
                    "键领取/赠送": (1734, 55), "今日赠礼": (400, 300)}
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
        if self.switch_after and self.ocr_calls >= self.switch_after and self.state == self.switch_from:
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
        elif self.state == "friend" and _near((x, y), BACK_XY):
            # 好友入口在主界面左上图标行，返回即到家
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
    # 2026-11-14 拆分后符号分散在 scene/common/tasklist/handlers：同名桩必须
    # 打到**所有拥有该符号的模块**（函数在自己的模块全局里解析名字，只 patch
    # 主模块会漏掉 common 侧的 _window_region/ocr_window_content 等）。
    _MODULES = (bd, bcommon, btasklist, bhandlers)

    def _mset(self, name, value):
        for m in self._MODULES:
            if hasattr(m, name):
                self._saved.setdefault((id(m), name), (m, name, getattr(m, name)))
                setattr(m, name, value)

    def install(self, extra=None):
        self._saved = {}

        class FakePyAuto:
            @staticmethod
            def press(key):
                self.press(key)

        self._mset("ocr_window_content", self.ocr)
        self._mset("_pyautogui_click_guarded", self.click)
        self._mset("_click_ratio", self.click_ratio)
        self._mset("_ensure_window", lambda logger, hwnd: hwnd)
        self._mset("_window_region", lambda hwnd: WIN)
        self._mset("detect_baijing_main_ui",
                   lambda logger, hwnd, budget_sec=2.0: (self.state == "main", "fake"))
        self._mset("_record_step_failure", self.record_failure)
        self._mset("pyautogui", FakePyAuto())
        self._mset("time", FakeTime())
        for name, fn in (extra or {}).items():
            self._mset(name, fn)

    def restore(self):
        for m, name, orig in self._saved.values():
            setattr(m, name, orig)

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
    orig_region, orig_ocr = bhandlers._window_region, bhandlers._ocr_best_match
    bhandlers._window_region = lambda hwnd: (0, 0, 1919, 1079)
    bcommon._window_region = bhandlers._window_region
    def _boom(*a, **k):
        raise AssertionError("有锚点时不应再 OCR")
    bhandlers._ocr_best_match = _boom
    try:
        pos = bhandlers._find_plus_button(LOG, 1, anchor=(1189, 669))
    finally:
        bhandlers._window_region, bhandlers._ocr_best_match = orig_region, orig_ocr
        bcommon._window_region = orig_region
    check("重置锚点推算 + 按钮（截图定标）", pos == (1424, 669), pos)
    check("参考截图已入库", (PROJECT_ROOT / "docs" / "refs" / "baijing_memory_enhance_20261030.png").is_file())
    check("重置 OCR 限定在指定等级行", bhandlers.MEMORY_RESET_REGION == (0.55, 0.58, 0.68, 0.66))
    src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "baijing_task_handlers.py").read_text(encoding="utf-8")
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


# ── 4.7 2026-11-10：中文 logo 加载页 + 旧场景名不再冒充当帧 ──────────
def test_loading_cn_and_failure_detail_20261110():
    print("[4.7] 11-10 实锤：加载页中文 logo 可识别 / 失败文案带当帧文字")
    check("加载特征覆盖中文 logo 全部 OCR 变体",
          all(v in bd.LOADING_SCREEN_MARKERS
              for v in ("白荆廊", "白荆回廊", "白荆回廓")),
          bd.LOADING_SCREEN_MARKERS)
    check("不用泛化短词「白荆」（固定页面会白等 60s）",
          "白荆" not in bd.LOADING_SCREEN_MARKERS, bd.LOADING_SCREEN_MARKERS)

    # 组合闸：logo + 进度才算加载；只有游戏名的固定页面不许命中
    logo_only = [{"text": "白荆回廊活动公告", "confidence": 0.9, "center": (300, 300)},
                 {"text": "确认", "confidence": 0.9, "center": (1196, 684)}]
    rule, _ = bd._match_scene_rule(LOG, 1, logo_only)
    check("只有游戏名没进度 → 不判加载过场",
          rule is None or rule["name"] != "加载过场", rule["name"] if rule else None)
    real = [{"text": "白荆廊", "confidence": 0.85, "center": (967, 476)},
            {"text": "98%", "confidence": 0.98, "center": (1797, 1022)}]
    rule2, _ = bd._match_scene_rule(LOG, 1, real)
    check("logo + 进度 → 判加载过场",
          rule2 is not None and rule2["name"] == "加载过场",
          rule2["name"] if rule2 else None)

    # 生产 OCR 当帧只有「白荆廊 / 98% / 提示文字 / 乱码」，也必须命中加载过场
    scr = Screen(state="loading_cn")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-loading-cn"))
    check("中文 logo 加载页命中「加载过场」", state == "scene:加载过场", state)
    check("只等待零点击零 ESC", scr.clicks == [] and scr.esc == 0, (scr.clicks, scr.esc))
    check("证据文案说的是仍在加载",
          any("仍在加载" in d for _, d in scr.failures), scr.failures)

    # 纯文案：最后一轮已变成未登记界面 → 不许再说「场景「休息室」反复退出失败」
    stale = "｜".join(["白荆廊", "【筑术师】具备极远的射程", "98%", "24919904"])
    d1 = bd._scene_failure_detail("休息室", stale)
    check("旧场景名不再冒充当帧结论", "反复退出失败" not in d1, d1)
    check("文案带当帧文字", "98%" in d1 and "白荆廊" in d1, d1)
    d2 = bd._scene_failure_detail("加载过场", "")
    check("等加载不算反复退出失败", "仍在加载" in d2 and "反复退出失败" not in d2, d2)
    d3 = bd._scene_failure_detail("休息室", "")
    check("真·同场景退不出去仍报原结论", "反复退出失败" in d3, d3)


# ── 4.8 2026-11-12：轮次耗尽但每轮都在推进 ≠ 失败 ─────────────────
def test_rounds_exhausted_not_failure_20261112():
    print("[4.8] 11-12 #279 实锤：加载完成后轮次烧光，当帧已是定巡之礼，不落假失败")
    # rounds=1 复刻生产时序：加载等待成功返回 True（实际 ~3s，远没到 60s），
    # 预算烧光，收尾当帧恰是刚弹出的定巡之礼。旧代码在此落「仍在加载」假失败。
    scr = Screen(state="loading", switch_after=3, next_state="event_claim")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="轮次耗尽-弹窗", rounds=1))
    check("返回 scene:加载过场 且 acted=True", state == "scene:加载过场" and acted is True, (state, acted))
    check("不落失败证据（当帧已是登记表内场景）", scr.failures == [], scr.failures)
    check("全程零点击（弹窗留给下一轮恢复处理）", scr.clicks == [], scr.clicks)

    # 交回调用方后的下一轮恢复必须能收敛：领取「可领取」→ 点右上 X
    state2, acted2 = scr.run(lambda: bd.recover_scene(LOG, 1, context="轮次耗尽-后续"))
    check("下一轮恢复处理弹窗 → main", state2 == "main" and acted2 is True, (state2, acted2))
    check("下一轮点击序列 = 先领取后关闭",
          scr.clicks == [CLAIM_XY, EVENT_CLOSE_XY], scr.clicks)

    # 同款假失败的另外两个收尾落点：当帧已漂回主界面 / 任务面板
    scr = Screen(state="loading", switch_after=3, next_state="main")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="轮次耗尽-主界面", rounds=1))
    check("收尾当帧是主界面 → 返回 main 不落证据",
          state == "main" and acted is True and scr.failures == [], (state, acted, scr.failures))
    scr = Screen(state="loading", switch_after=3, next_state="panel")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="轮次耗尽-面板", rounds=1))
    check("收尾当帧是任务面板 → 返回 panel 不落证据",
          state == "panel" and acted is True and scr.failures == [], (state, acted, scr.failures))

    # 纯文案：加载完成后的耗尽不许再说「仍在加载」；超时（exit 返回 False）仍要说
    d_done = bd._scene_failure_detail("加载过场", "", last_acted=True)
    check("加载完成+轮次耗尽 ≠ 仍在加载", "仍在加载" not in d_done, d_done)
    d_stuck = bd._scene_failure_detail("加载过场", "", last_acted=False)
    check("真超时仍报仍在加载", "仍在加载" in d_stuck, d_stuck)

    # 反向：仍卡在同一场景（同名规则再命中）必须照旧落「反复退出失败」证据
    scr = Screen(state="lounge", leave_works=False)
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="轮次耗尽-卡死", rounds=1))
    check("同名场景卡死仍落证据", any("反复退出失败" in d for _, d in scr.failures), scr.failures)


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
    check("loading 卡死落了证据（不再说成反复退出失败）",
          any("仍在加载" in d for _, d in scr.failures)
          and not any("反复退出失败" in d for _, d in scr.failures), scr.failures)

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


# ── 4.9 2026-11-13：好友页登记（#282 取证驱动） ───────────────────
def test_friend_page_20261113():
    print("[4.9] 好友页：detector ≥2 独有词，退出点左上「返回」")
    scr = Screen(state="friend")
    state, acted = scr.run(lambda: bd.recover_scene(LOG, 1, context="t-friend"))
    check("friend → 状态 main", state == "main", state)
    check("friend 点了左上返回", BACK_XY in scr.clicks, scr.clicks)
    check("friend 零 ESC", scr.esc == 0, scr.esc)

    # 单一「好友列表」不许命中（别的页面可能同名词，铁律 ≥2 证据）
    only_list = [{"text": "好友列表", "confidence": 0.9, "center": (233, 167)}]
    rule, _ = bd._match_scene_rule(LOG, 1, only_list)
    check("只有「好友列表」不判好友页",
          rule is None or rule["name"] != "好友页", rule["name"] if rule else None)
    # OCR 截字变体也要能凑满双证据：好友列表 + 键领取/赠送
    real_frame = [{"text": "好友列表", "confidence": 0.9, "center": (233, 167)},
                  {"text": "键领取/赠送", "confidence": 0.95, "center": (1734, 55)},
                  {"text": "今日赠礼", "confidence": 0.9, "center": (400, 300)}]
    rule2, _ = bd._match_scene_rule(LOG, 1, real_frame)
    check("真实取证帧（含截字变体）判好友页",
          rule2 is not None and rule2["name"] == "好友页",
          rule2["name"] if rule2 else None)
    check("好友页登记在规则表末尾", bd.SCENE_RULES[-1]["name"] == "好友页",
          [r["name"] for r in bd.SCENE_RULES])


# ── 8. 登记表顺序 ─────────────────────────────────────────────────
def test_rule_order():
    print("[8] 登记表顺序：面板特征优先，绝不在任务面板上点「返回」")
    check("规则顺序", [r["name"] for r in bd.SCENE_RULES] == ["加载过场", "活动公告", "过场遮罩", "计划等级提升",
                                                              "定巡之礼", "茶憩摘要", "关注特写", "休息室",
                                                              "烙痕详情", "烙痕列表", "茶憩房间", "有猫零售",
                                                              "好友页"],
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
        lambda: btasklist._return_to_task_panel(LOG, 1),
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
        lambda: btasklist._ensure_task_list_open(LOG, 1),
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
          0.82 <= bhandlers.REST_ROOM_TEA_ENTRY[0] <= 0.95 and 0.76 <= bhandlers.REST_ROOM_TEA_ENTRY[1] <= 0.90,
          bhandlers.REST_ROOM_TEA_ENTRY)
    check("茶憩流程仍用休息室判定", hasattr(bd, "_in_rest_room"))
    # 2026-11-14 拆分：三个恢复入口分属 scene（_ensure_main_ui）与 tasklist
    # （_return_to_task_panel/_ensure_task_list_open），跨两个文件计数。
    src_scene = Path("src/game_auto/runtime/baijing_scene.py").read_text(encoding="utf-8")
    src_tasklist = Path("src/game_auto/runtime/baijing_tasklist.py").read_text(encoding="utf-8")
    n_calls = (src_scene.count("recover_scene(logger, hwnd")
               + src_tasklist.count("recover_scene(logger, hwnd"))
    check("三个入口都调用 recover_scene", n_calls >= 3, n_calls)


def main():
    test_single_scenes()
    test_new_scenes_20261027()
    test_loading_and_claim_20261107()
    test_loading_cn_and_failure_detail_20261110()
    test_rounds_exhausted_not_failure_20261112()
    test_friend_page_20261113()
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
