# -*- coding: utf-8 -*-
"""白荆回廊俱乐部活动「回声猎区」分支回归（2026-11-11 入口 / 2026-11-13 内容）。

背景：俱乐部页右侧「俱乐部活动」卡片出现「回声猎区」。11-11 先落分支入口
（检测 → 点进活动页 → 快照取证 → 立即退出）；11-13 用户实机截图拍板内容口径
「先打 abcde 按顺序打，点进去是编队选择页面，可以复用拟合回归」——地图页
A~E 节点顺序解锁，点节点 → 详情面板「开始收录」→ 编队页（拟合回归同款）→
开战挂机 → 回地图 → 下一个。本套件锁：

[1] 常量守卫：入口词表/检测区域 + 节点标签区域避开详情面板（面板标题同名）；
[2] 入口未命中 → 不点击不快照直接跳过（活动结束后卡片可能消失，禁盲点）；
[3] 入口命中 → 点 OCR center（非固定比例）→ 内容分支 → 快照 → _leave_club_page 退出；
[4] 退出失败 → 分支 status=failed，detail 带人工恢复提示（不污染每日任务主状态）；
[5] 接线：claim_club_daily_reward 领完每日任务后调用分支并挂 result["activity"]；
[6] 用户停止 → 分支 skipped，且 claim 主流程不再调分支；
[7] 内容：地图双证据不满足 → failed + 落取证，零节点点击；
[8] 内容：已清标记跳过 A → 打 B → C 无「开始收录」（锁定）链止，标记保存 A+B；
[9] 节点定位：OCR 字母优先 / x 序兜底（仅 5 标签全在）/ 凑不齐宁可不点；
[10] 节点战斗：编队页（复用拟合）→ 作战开始 → 布阵页离开即算开打（战斗页
     OCR 盲，#283 实锤）→ 卡布阵页补点 3 次报败 / 编队页进不去报败 / 无布阵页
     battle 直进；
[11] 战后自适应：结算页点空白 / 地图特征先到先收，未收敛如实报败。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_baijing_club_activity_20261111.py -q
（经 tests/test_suites.py 子进程跑）
"""

import io
import sys
import unittest.mock as mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.baijing_daily as bd  # noqa: E402
import game_auto.runtime.baijing_fitting as bfit  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_constants():
    print("[1] 常量守卫")
    check("入口词表", bd.CLUB_ACTIVITY_ECHO_TEXTS == ("回声猎区",))
    x0, y0, x1, y1 = bd.CLUB_ACTIVITY_DETECT_REGION
    check("检测区域升序合法", x0 < x1 and y0 < y1, bd.CLUB_ACTIVITY_DETECT_REGION)
    # 实测「回声猎区」标题中心 (0.819, 0.370) 必须落在检测区域内（用户 11-11 截图）
    check("实测标题坐标在区域内",
          x0 <= 0.819 <= x1 and y0 <= 0.370 <= y1, bd.CLUB_ACTIVITY_DETECT_REGION)
    # 顶部避开「今日俱乐部奖励已领取」横幅（y≈0.215~0.245）
    check("区域顶部低于横幅带", y0 >= 0.25, y0)
    check("快照前缀", bd.CLUB_ACTIVITY_SNAPSHOT_PREFIX == "club_activity_echo")
    # 11-13 内容常量：节点标签区右界必须避开详情面板（面板左缘 ≈0.68，面板
    # 标题也叫「纯净回声·X」）；「开始收录」比例兜底必须落在自己的搜索区内。
    nx0, ny0, nx1, ny1 = bd.ECHO_NODE_REGION
    check("节点标签区右界避开详情面板", 0.62 <= nx1 <= 0.67, bd.ECHO_NODE_REGION)
    check("节点标签区升序合法", nx0 < nx1 and ny0 < ny1, bd.ECHO_NODE_REGION)
    cx, cy = bd.ECHO_CLAIM_FALLBACK
    rx0, ry0, rx1, ry1 = bd.ECHO_CLAIM_REGION
    check("「开始收录」兜底比例在搜索区内",
          rx0 <= cx <= rx1 and ry0 <= cy <= ry1, (bd.ECHO_CLAIM_FALLBACK, bd.ECHO_CLAIM_REGION))
    check("节点字母表 A~E", bd.ECHO_NODE_LETTERS == ("A", "B", "C", "D", "E"))
    check("地图判定是双证据（标题+独有词）",
          bd.ECHO_MAP_TITLE == ("回声猎区",) and len(bd.ECHO_MAP_BODY) >= 2,
          (bd.ECHO_MAP_TITLE, bd.ECHO_MAP_BODY))
    check("「回声猎区」不在地图独有词里（防单标题判页）",
          not any("回声猎区" in m for m in bd.ECHO_MAP_BODY), bd.ECHO_MAP_BODY)


def test_entry_miss_skips_without_click():
    print("[2] 入口未命中 → 不点击直接跳过")
    clicked, snapped, left = [], [], []
    with mock.patch.object(bd, "_ocr_best_match", return_value=None), \
         mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: clicked.append(a)), \
         mock.patch.object(bd, "_capture_daily_list_snapshot", side_effect=lambda *a, **k: snapped.append(a)), \
         mock.patch.object(bd, "_leave_club_page", side_effect=lambda *a, **k: left.append(a)):
        r = bd._run_club_activity_echo_zone(logging_logger(), hwnd=1)
    check("status=skipped", r["status"] == "skipped", r)
    check("entered=False", r["entered"] is False)
    check("未点击", not clicked, clicked)
    check("未快照", not snapped, snapped)
    check("未触发退出", not left, left)
    check("detail 说明未检测到", "回声猎区" in r["detail"])


def test_entry_hit_clicks_ocr_center_and_exits():
    print("[3] 入口命中 → 点 OCR center → 内容分支 → 快照 → 退出")
    clicked, snapped, left, content_calls = [], [], [], []
    content = {"status": "success", "detail": "节点链走完：本场打 ['A']",
               "fought": ["A"], "skipped": []}
    hit = {"center": (1572, 400), "text": "回声猎区", "confidence": 0.9}
    with mock.patch.object(bd, "_ocr_best_match", return_value=hit), \
         mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: clicked.append(a)), \
         mock.patch.object(bd, "_capture_daily_list_snapshot",
                           side_effect=lambda *a, **k: snapped.append(a) or "snap.jpg"), \
         mock.patch.object(bd, "_leave_club_page", side_effect=lambda *a, **k: left.append(a) or True), \
         mock.patch.object(bd, "_echo_zone_run_content",
                           side_effect=lambda *a, **k:
                               (content_calls.append(a), dict(content))[1]), \
         mock.patch.object(bd.time, "sleep", lambda *_: None):
        r = bd._run_club_activity_echo_zone(logging_logger(), hwnd=1)
    check("entered=True", r["entered"] is True)
    check("点击用的是 OCR center（非固定比例）", clicked == [(1572, 400)], clicked)
    check("内容分支被调用 1 次", len(content_calls) == 1, content_calls)
    check("进页后快照取证", len(snapped) == 1, snapped)
    check("退出走 _leave_club_page", len(left) == 1, left)
    check("returned_main=True", r["returned_main"] is True)
    check("内容成功 → 分支 success", r["status"] == "success", r)
    check("echo 结果挂分支", r.get("echo") == content, r.get("echo"))
    check("detail 带内容摘要", "节点链走完" in r["detail"], r["detail"])


def test_exit_failure_marks_failed():
    print("[4] 退出失败 → 分支 failed 且带人工恢复提示")
    with mock.patch.object(bd, "_ocr_best_match",
                           return_value={"center": (1572, 400), "text": "回声猎区", "confidence": 0.9}), \
         mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: None), \
         mock.patch.object(bd, "_capture_daily_list_snapshot", side_effect=lambda *a, **k: None), \
         mock.patch.object(bd, "_echo_zone_run_content",
                           side_effect=lambda *a, **k:
                               {"status": "success", "detail": "stub", "fought": [], "skipped": []}), \
         mock.patch.object(bd, "_leave_club_page", return_value=False), \
         mock.patch.object(bd.time, "sleep", lambda *_: None):
        r = bd._run_club_activity_echo_zone(logging_logger(), hwnd=1)
    check("status=failed", r["status"] == "failed", r)
    check("entered=True", r["entered"] is True)
    check("returned_main=False", r["returned_main"] is False)
    check("detail 带人工恢复提示", "人工恢复" in r["detail"], r["detail"])


def test_user_stop():
    print("[5] 用户停止 → 分支 skipped")
    with mock.patch.object(bd, "_stopped", return_value=True):
        r = bd._run_club_activity_echo_zone(logging_logger(), hwnd=1)
    check("status=skipped detail=用户停止", r["status"] == "skipped" and r["detail"] == "用户停止", r)


def _claim_env(branch_stub, *, stopped=False):
    """claim_club_daily_reward 的全打桩环境：返回 (result, 调用记录)。"""
    calls = {"branch": 0}
    with mock.patch.object(bd, "_stopped", side_effect=lambda c: stopped), \
         mock.patch.object(bd, "_ensure_window", return_value=1), \
         mock.patch.object(bd, "_ensure_main_ui", return_value=True), \
         mock.patch.object(bd, "_enter_club_page", return_value=True), \
         mock.patch.object(bd, "_capture_daily_list_snapshot", return_value=None), \
         mock.patch.object(bd, "_claim_club_daily_task", return_value="purchased"), \
         mock.patch.object(bd, "_run_club_activity_echo_zone",
                           side_effect=lambda *a, **k: (calls.__setitem__("branch", calls["branch"] + 1),
                                                        dict(branch_stub))[1]), \
         mock.patch.object(bd, "_leave_club_page", return_value=True):
        r = bd.claim_club_daily_reward(logging_logger(), hwnd=1)
    return r, calls


def test_wiring_in_claim_club_daily_reward():
    print("[6] 接线：领完每日任务后调分支并挂 result['activity']，主状态不受影响")
    stub = {"status": "skipped", "detail": "stub", "entered": False, "returned_main": None}
    r, calls = _claim_env(stub)
    check("分支被调用 1 次", calls["branch"] == 1, calls)
    check("分支结果挂 activity 键", r.get("activity") == stub, r.get("activity"))
    check("每日任务主状态仍是 success", r["status"] == "success", r)
    check("returned_main 仍是顶层键", r.get("returned_main") is True)
    # 用户停止时分支不再调用
    r2, calls2 = _claim_env(stub, stopped=True)
    check("停止后分支不调用", calls2["branch"] == 0, calls2)
    check("停止时主状态 skipped", r2["status"] == "skipped", r2)


def logging_logger():
    import logging
    return logging.getLogger("suite_baijing_club_activity")


# ── 11-13 内容主体 ────────────────────────────────────────────────
import time as _real_time


class FakeTime:
    """假时钟：锚定 2026-11-13 10:00（过期日断言的基准日）。

    time() 每次调用 +0.05、sleep 按参数推进——防 `_echo_wait_map` 的 25s
    busy loop 真转；strftime/localtime 走真实现（`_echo_reset_expiry` 要算
    「重置时间剩余N天」→ 真日期）。
    """

    T0 = _real_time.mktime((2026, 11, 13, 10, 0, 0, 0, 0, -1))

    def __init__(self):
        self.t = self.T0

    def time(self):
        self.t += 0.05
        return self.t

    def sleep(self, s):
        self.t += float(s)

    def strftime(self, fmt, t=None):
        # 生产调用形如 time.strftime(fmt, time.localtime(ts))——本类被整体顶替
        # 后第二参可能是 None / 时间戳 / 本类 localtime 返回的 struct_time。
        if t is None:
            t = self.t
        elif not isinstance(t, _real_time.struct_time):
            t = _real_time.localtime(t)
        return _real_time.strftime(fmt, t)

    def localtime(self, t=None):
        return _real_time.localtime(self.t if t is None else t)


def map_frame():
    """猎区地图帧（2026-11-13 用户截图转录：标题+重置倒计时+五个节点标签）。"""
    return [
        {"text": "回声猎区", "confidence": 0.9, "center": (400, 200)},
        {"text": "重置时间剩余10天", "confidence": 0.9, "center": (300, 320)},
        {"text": "猎区累计分贝奖励", "confidence": 0.8, "center": (250, 1010)},
        {"text": "纯净回声·A", "confidence": 0.8, "center": (206, 676)},
        {"text": "纯净回声·B", "confidence": 0.8, "center": (444, 819)},
        {"text": "纯净回声·C", "confidence": 0.8, "center": (751, 788)},
        {"text": "纯净回声·D", "confidence": 0.8, "center": (1066, 826)},
        {"text": "纯净回声·E", "confidence": 0.8, "center": (1251, 651)},
    ]


def test_content_map_unconfirmed_fails():
    print("[7] 内容：地图双证据不满足 → failed + 落取证，零节点点击")
    clicked, recorded = [], []
    with mock.patch.object(bd, "_scene_results", return_value=[]), \
         mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_pyautogui_click_guarded",
                           side_effect=lambda *a: clicked.append(a)), \
         mock.patch.object(bd, "_record_step_failure",
                           side_effect=lambda *a, **k: recorded.append(a)):
        out = bd._echo_zone_run_content(logging_logger(), hwnd=1)
    check("status=failed", out["status"] == "failed", out)
    check("零节点点击", not clicked, clicked)
    check("落了地图确认取证", recorded and "地图确认" in recorded[0][2], recorded)
    check("detail 说明双证据不满足", "双证据" in out["detail"], out["detail"])


def test_content_marks_skip_and_lock_chain_end():
    print("[8] 内容：标记跳过 A → 打 B → C 无「开始收录」链止，标记保存 A+B")
    clicked, fights, saves, recorded = [], [], [], []
    claim_calls = {"n": 0}

    def fake_claim(*a, **k):
        # 面板等待是轮询式：B 第一次就命中；C 锁死会反复问到期满，恒 None
        claim_calls["n"] += 1
        if claim_calls["n"] == 1:
            return {"center": (1500, 900), "text": "开始收录", "confidence": 0.9}
        return None

    with mock.patch.object(bd, "_scene_results", side_effect=[map_frame()] * 5), \
         mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_echo_load_marks",
                           return_value={"period_end": "2026-12-01", "cleared": ["A"]}), \
         mock.patch.object(bd, "_echo_save_marks",
                           side_effect=lambda lg, pe, cl: saves.append((pe, cl))), \
         mock.patch.object(bd, "_echo_zone_fight_node",
                           side_effect=lambda lg, hw, letter, xy, stop_check=None:
                               (fights.append(letter), (True, ""))[1]), \
         mock.patch.object(bd, "_ocr_best_match", side_effect=fake_claim), \
         mock.patch.object(bd, "_pyautogui_click_guarded",
                           side_effect=lambda *a: clicked.append(a)), \
         mock.patch.object(bd, "_record_step_failure",
                           side_effect=lambda *a, **k: recorded.append(a)):
        out = bd._echo_zone_run_content(logging_logger(), hwnd=1)
    check("标记跳过 A", out["skipped"] == ["A"], out["skipped"])
    check("只打了 B", out["fought"] == ["B"] and fights == ["B"], (out["fought"], fights))
    check("点击序列 = B 标签 + C 标签（A 被跳过未点）",
          clicked == [(444, 819), (751, 788)], clicked)
    check("C 锁定 → 顺序链正常收尾（非失败）",
          out["status"] == "success" and "开始收录" in out["detail"], out)
    check("无失败取证", not recorded, recorded)
    check("已清标记保存为 A+B（含本场打的 B，过期日=今天+剩余10天）",
          saves == [("2026-11-23", ["A", "B"])], saves)


def test_node_letter_and_fallback():
    print("[9] 节点定位：OCR 字母优先 / x 序兜底 / 凑不齐宁可不点")
    check("字母·分隔", bd._echo_letter_of("纯净回声·A") == "A")
    check("字母-分隔", bd._echo_letter_of("纯净回声-B") == "B")
    check("无字母 → None", bd._echo_letter_of("纯净回声") is None)
    labels = [{"text": "纯净回声", "confidence": 0.8, "center": (100 * (i + 1), 700)}
              for i in range(5)]
    check("x 序兜底取第 3 个为 C",
          bd._echo_pick_label(labels, "C")["center"] == (300, 700),
          bd._echo_pick_label(labels, "C"))
    check("标签不全且无字母 → 宁可不点（None）",
          bd._echo_pick_label(labels[:3], "E") is None)
    check("字母命中优先于 x 序",
          bd._echo_pick_label(
              [{"text": "纯净回声·D", "confidence": 0.8, "center": (900, 700)}] + labels,
              "D")["center"] == (900, 700))


def test_fight_node_team_chain():
    print("[10] 节点战斗：编队页复用拟合 → 作战开始 → 布阵页离开即算开打")
    clicks = []
    with mock.patch.object(bd, "_pyautogui_click_guarded",
                           side_effect=lambda *a: clicks.append(a)), \
         mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_echo_wait_battle_settled",
                           side_effect=lambda lg, hw, sc, dl: (True, "map", [])), \
         mock.patch.object(bfit, "_wait_page",
                           side_effect=lambda lg, hw, want, to, sc, **k:
                               (True, "team", []) if want == "team" else (False, "?", [])), \
         mock.patch.object(bfit, "_click", side_effect=lambda lg, hw, p, label:
                           clicks.append(("fitting", label)) or True), \
         mock.patch.object(bfit, "_wait_deploy_settled",
                           side_effect=lambda lg, hw, sc: True), \
         mock.patch.object(bfit, "_page_now",
                           side_effect=[("deploy", []), ("unknown", [])]):
        ok, reason = bd._echo_zone_fight_node(logging_logger(), 1, "A", (1500, 900))
    check("布阵页离开即算开打（战斗页 OCR 盲，#283 实锤）",
          ok is True and reason == "", (ok, reason))
    check("开始战斗只点一次（不再 3 连补点）",
          clicks.count(("fitting", "开始战斗")) == 1, clicks)
    check("先点「开始收录」再点「作战开始」",
          ("fitting", "作战开始") in clicks and (1500, 900) in clicks, clicks)

    # 点完仍停在布阵页 → 补点满 3 次如实报败（交证据）
    with mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: None), \
         mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bfit, "_wait_page", return_value=(True, "team", [])), \
         mock.patch.object(bfit, "_click", side_effect=lambda lg, hw, p, label: True), \
         mock.patch.object(bfit, "_wait_deploy_settled", side_effect=lambda lg, hw, sc: True), \
         mock.patch.object(bfit, "_page_now",
                           side_effect=[("deploy", [])] + [("deploy", [])] * 6):
        ok, reason = bd._echo_zone_fight_node(logging_logger(), 1, "B", (1500, 900))
    check("布阵页未离开 → 报败带点击次数",
          ok is False and "布阵页未离开" in reason and "3 次" in reason, (ok, reason))

    # 编队页进不去 → 如实报败（不补点不重试，交证据）
    with mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: None), \
         mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bfit, "_wait_page", return_value=(False, "unknown", [])):
        ok, reason = bd._echo_zone_fight_node(logging_logger(), 1, "C", (1500, 900))
    check("编队页进不去 → 报败带页面", ok is False and "编队页" in reason, (ok, reason))

    # 没有布阵页：作战开始后直接进战斗（deploy/battle 双等，battle 先到不点开始战斗）
    started = []
    with mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: None), \
         mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_echo_wait_battle_settled",
                           side_effect=lambda lg, hw, sc, dl: (True, "map", [])), \
         mock.patch.object(bfit, "_wait_page", return_value=(True, "team", [])), \
         mock.patch.object(bfit, "_click",
                           side_effect=lambda lg, hw, p, label: started.append(label) or True), \
         mock.patch.object(bfit, "_page_now", side_effect=[("battle", [])]):
        ok, reason = bd._echo_zone_fight_node(logging_logger(), 1, "D", (1500, 900))
    check("battle 先到 → 跳过布阵开打链", ok and started == ["作战开始"], started)


def test_battle_settle_adaptive():
    print("[11] 战后自适应：结算页点空白 / 地图先到先收")
    blanks = []
    frames = iter([
        ("battle", []),
        ("result", []),
        (None, map_frame()),      # None 占位：结算点空白后下一帧直接回图
    ])
    def fake_page_now(lg, hw):
        page, items = next(frames)
        return (page or "unknown"), items
    with mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_click_ratio",
                           side_effect=lambda lg, hw, ratio, label="":
                               blanks.append(label) or True), \
         mock.patch.object(bfit, "_page_now", side_effect=fake_page_now):
        ok, state, items = bd._echo_wait_battle_settled(
            logging_logger(), 1, None, deadline=FakeTime().time() + 300
        )
    check("结算页点过空白后回图收敛", ok is True and state == "map", (ok, state))
    check("点空白用的是拟合同款结算动作", blanks == ["回声猎区结算页点空白"], blanks)
    check("地图帧带节点标签", any("纯净回声·A" in str(it.get("text")) for it in items))

    # 超时未收敛 → 如实报败带末次页面
    with mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bfit, "_page_now", side_effect=lambda lg, hw: ("battle", [])):
        ok, state, _items = bd._echo_wait_battle_settled(
            logging_logger(), 1, None, deadline=FakeTime().time() + 1
        )
    check("超时报败带末次页面", ok is False and "battle" in state, (ok, state))


def main():
    test_constants()
    test_entry_miss_skips_without_click()
    test_entry_hit_clicks_ocr_center_and_exits()
    test_exit_failure_marks_failed()
    test_user_stop()
    test_wiring_in_claim_club_daily_reward()
    test_content_map_unconfirmed_fails()
    test_content_marks_skip_and_lock_chain_end()
    test_node_letter_and_fallback()
    test_fight_node_team_chain()
    test_battle_settle_adaptive()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
