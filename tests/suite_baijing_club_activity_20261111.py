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
[9b] 节点标签区域过滤：右界含住 E 标签 x≈0.667（2026-11-14 事故回归）、
     x>0.70 与面板标题位 y<0.35 出区；
[10] 节点战斗：编队页（复用拟合）→ 作战开始 → 布阵页离开即算开打（战斗页
     OCR 盲，#283 实锤）→ 卡布阵页补点 3 次报败 / 编队页进不去报败 / 无布阵页
     battle 直进；
[11] 战后自适应：结算页点空白 / 地图特征先到先收，未收敛如实报败。
[12] 战后页面实机收口（2026-11-14）：胜利结算页「任务完成/关卡目标」点空白；
     战败页「任务失败+推荐」点空白且回图后报 map_defeated（不写已清标记）；
     战斗目标横幅含「任务失败」但无「推荐」绝不误点（误报陷阱实锤两次）。
[13] boss 面板判定（标题+面板独有词双证据；地图「已收录」条/战后总结页不匹配）
     + 「本期最高分贝」数值读取（label/数字拆条按同列取数）。
[14] boss 战斗闸门与链路：重新降噪/快速降噪在场 → skipped；本期分数 ≥ 顶档
     → skipped；正常链 = 点标题开面板 → 开始降噪 → 编队页（「编队成员数量
     不足」弹窗点确认自动编队）→ 作战开始 → battle 直进 → 作战完成点空白 →
     总结页点返回 → 回面板 = fought。
[15] 声量阶梯领取：OCR 档位金额 → 点右侧图标（实测偏移 +118,+20）按 y 序逐档。
[16] 地图左下「猎区累计分贝奖励」：入口 OCR 定位 → 奖励页双证据（标题+一键
     领取，防地图同名入口字样误判）→ 一键领取 → 关 X → 回地图。
[17] 接线：分支结果挂 result["boss"]；boss 失败只追加 detail 不改节点链状态。

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

import game_auto.runtime.baijing_echo as bd  # noqa: E402
import game_auto.runtime.baijing_common as bcommon  # noqa: E402
import game_auto.runtime.baijing_scene as bscene  # noqa: E402
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
    # 11-14 口径：右界必须含住节点 E 标签中心 x≈0.667（旧 0.66 把 E 滤掉导致
    # 「节点 E 标签未定位」），且避开右侧常驻会员排名面板（左缘 ≈0.78）；
    # 点节点才开的详情面板（左缘 ≈0.68）标题靠 y≥0.35 + 字母优先/5 标签兜底兜住。
    nx0, ny0, nx1, ny1 = bd.ECHO_NODE_REGION
    check("节点标签区右界含住 E(0.667) 且避开常驻排名面板",
          0.667 <= nx1 <= 0.75, bd.ECHO_NODE_REGION)
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


def test_node_label_region_filter():
    print("[9b] 节点标签区域过滤：E 标签(x≈0.667)必须进区（2026-11-14 事故回归）")
    # 桩 _window_region → 1920x1080 虚拟窗口，_abs_region 换算
    # ECHO_NODE_REGION=(0,0.35,0.70,0.95) → 绝对区 (0,378)-(1344,1026)。
    with mock.patch.object(bcommon, "_window_region", return_value=(0, 0, 1920, 1080)):
        items = [
            {"text": "纯净回声·A", "confidence": 0.99, "center": (211, 668)},
            # 2026-11-14 实拍：E 标签中心 (≈1280, 650)，旧右界 0.66 把它滤掉
            {"text": "纯净回声·E", "confidence": 0.98, "center": (1281, 650)},
            # 右界之外（x>0.70）：常驻会员排名面板方向的标签必须滤掉
            {"text": "纯净回声·E", "confidence": 0.98, "center": (1400, 650)},
            # 详情面板标题位（面板上部 y<0.35）：x 再靠内也必须被 y 界挡住
            {"text": "纯净回声·D", "confidence": 1.00, "center": (1325, 300)},
        ]
        got = bd._echo_node_labels(1, items)
    xs = [it["center"][0] for it in got]
    check("E 标签中心 0.667 进区（本次修复的目标行为）", 1281 in xs, xs)
    check("x>0.70 出区（不混常驻排名面板）", 1400 not in xs, xs)
    check("面板标题位 y<0.35 出区", 1325 not in xs, xs)
    check("按 x 从左到右排序", xs == sorted(xs), xs)


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


def echo_result_frame():
    """胜利结算页帧（2026-11-14 实机转录）：任务完成 + 关卡目标 + 达成目标列表。"""
    return [
        {"text": "任务完成", "confidence": 0.9, "center": (406, 256)},
        {"text": "关卡目标", "confidence": 0.9, "center": (244, 359)},
        {"text": "在4分内通过关卡", "confidence": 0.9, "center": (288, 409)},
        {"text": "击败所有敌人", "confidence": 0.9, "center": (268, 488)},
    ]


def defeat_frame():
    """战败页帧（2026-11-14 实机转录）：任务失败 + 推荐您卡片（双词同帧才算）。"""
    return [
        {"text": "任务失败", "confidence": 0.9, "center": (400, 260)},
        {"text": "推荐您:", "confidence": 0.9, "center": (230, 515)},
        {"text": "提升刻印", "confidence": 0.9, "center": (255, 720)},
        {"text": "培养同调者", "confidence": 0.9, "center": (460, 720)},
    ]


def banner_frame():
    """战斗内目标横幅帧：含「任务失败」但无「推荐」——误报陷阱（会话实锤两次）。"""
    return [
        {"text": "击败所有敌人", "confidence": 0.9, "center": (420, 45)},
        {"text": "任务失败", "confidence": 0.9, "center": (700, 45)},
        {"text": "1/1", "confidence": 0.9, "center": (1230, 55)},
    ]


def test_battle_settle_echo_result_and_defeat():
    print("[12] 战后页面实机收口：胜利结算页 / 战败页 / 目标横幅误报防护")
    # 胜利结算页 → 点空白 → 回图 = map
    blanks = []
    frames = iter([
        ("battle", []),
        (None, echo_result_frame()),
        (None, map_frame()),
    ])

    def fake_page_now(lg, hw):
        page, items = next(frames)
        return (page or "unknown"), items

    with mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_click_ratio",
                           side_effect=lambda lg, hw, ratio, label="":
                               blanks.append(label) or True), \
         mock.patch.object(bfit, "_page_now", side_effect=fake_page_now):
        ok, state, _items = bd._echo_wait_battle_settled(
            logging_logger(), 1, None, deadline=FakeTime().time() + 300
        )
    check("胜利结算页点空白后回图 = map", ok is True and state == "map", (ok, state))
    check("结算页点击动作带页面语义", blanks == ["回声猎区结算页点空白"], blanks)

    # 战败页 → 点空白 → 回图 = map_defeated（调用方据此不写已清标记）
    blanks = []
    frames = iter([
        ("battle", []),
        (None, defeat_frame()),
        (None, map_frame()),
    ])
    with mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_click_ratio",
                           side_effect=lambda lg, hw, ratio, label="":
                               blanks.append(label) or True), \
         mock.patch.object(bfit, "_page_now", side_effect=fake_page_now):
        ok, state, _items = bd._echo_wait_battle_settled(
            logging_logger(), 1, None, deadline=FakeTime().time() + 300
        )
    check("战败页点空白后回图 = map_defeated",
          ok is True and state == "map_defeated", (ok, state))
    check("战败页点击动作带页面语义", blanks == ["回声猎区战败页点空白"], blanks)

    # 战斗目标横幅含「任务失败」但无「推荐」→ 绝不点击（误报会把过场点没）
    blanks = []
    with mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_click_ratio",
                           side_effect=lambda lg, hw, ratio, label="":
                               blanks.append(label) or True), \
         mock.patch.object(bfit, "_page_now",
                           side_effect=lambda lg, hw: ("battle", banner_frame())):
        ok, state, _items = bd._echo_wait_battle_settled(
            logging_logger(), 1, None, deadline=FakeTime().time() + 1
        )
    check("横幅「任务失败」不触发战败点击", blanks == [], blanks)
    check("横幅帧不收敛 → 如实报败", ok is False and "battle" in state, (ok, state))

    # fight_node 收到 map_defeated → 报败（已清标记由 _echo_zone_run_content
    # 只在 ok=True 时写，报败即不写；这里锁 fight_node 的语义）
    with mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: None), \
         mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_echo_wait_battle_settled",
                           side_effect=lambda lg, hw, sc, dl: (True, "map_defeated", [])), \
         mock.patch.object(bfit, "_wait_page", return_value=(True, "team", [])), \
         mock.patch.object(bfit, "_click", side_effect=lambda lg, hw, p, label: True), \
         mock.patch.object(bfit, "_page_now", side_effect=[("battle", [])]):
        ok, reason = bd._echo_zone_fight_node(logging_logger(), 1, "E", (1500, 900))
    check("战败回图 → fight_node 如实报败不报成功",
          ok is False and "战败" in reason and "未收录" in reason, (ok, reason))


# ── 11-14 固化：中央 boss + 奖励领取 ──────────────────────────────


def boss_strip_frame():
    """地图点 boss 标题后的「已收录」条帧：有 boss 名无面板独有词 → 非面板。"""
    return [
        {"text": "不协和音·鱼叉将军", "confidence": 1.0, "center": (837, 543)},
        {"text": "已收录：", "confidence": 1.0, "center": (687, 610)},
        {"text": "纯净回声·A", "confidence": 1.0, "center": (225, 674)},
    ]


def boss_panel_frame():
    """boss 面板帧（11-14 实机转录）：开始降噪 + 本期最高分贝 0 + 五档阶梯金额。"""
    return [
        {"text": "不协和音·鱼叉将军", "confidence": 1.0, "center": (754, 695)},
        {"text": "开始降噪", "confidence": 1.0, "center": (1392, 946)},
        {"text": "个人声量奖励", "confidence": 1.0, "center": (180, 240)},
        {"text": "本期最高分贝", "confidence": 1.0, "center": (1559, 580)},
        {"text": "0", "confidence": 1.0, "center": (1561, 618)},
        {"text": "9000", "confidence": 1.0, "center": (145, 353)},
        {"text": "7000", "confidence": 1.0, "center": (145, 477)},
        {"text": "5000", "confidence": 1.0, "center": (145, 601)},
        {"text": "3000", "confidence": 1.0, "center": (145, 724)},
        {"text": "1000", "confidence": 1.0, "center": (145, 848)},
    ]


def boss_panel_done_frame():
    """boss 面板战后帧：重新降噪/快速降噪在场 = 本期已打过。"""
    return [
        {"text": "不协和音·鱼叉将军", "confidence": 1.0, "center": (754, 695)},
        {"text": "重新降噪", "confidence": 1.0, "center": (1728, 944)},
        {"text": "快速降噪", "confidence": 1.0, "center": (1390, 945)},
        {"text": "本期最高分贝", "confidence": 1.0, "center": (1559, 580)},
        {"text": "19532", "confidence": 1.0, "center": (1561, 618)},
    ]


def boss_panel_scored_frame():
    """boss 面板分数达标帧：无重新降噪（闸门走分数分支）。"""
    return [
        {"text": "不协和音·鱼叉将军", "confidence": 1.0, "center": (754, 695)},
        {"text": "开始降噪", "confidence": 1.0, "center": (1392, 946)},
        {"text": "本期最高分贝", "confidence": 1.0, "center": (1559, 580)},
        {"text": "19532", "confidence": 1.0, "center": (1561, 618)},
    ]


def boss_dialog_frame():
    """boss 编队页空编队弹窗：「编队成员数量不足，是否自动编队？」（11-14 实锤）。"""
    return [
        {"text": "自动编队确认", "confidence": 1.0, "center": (582, 311)},
        {"text": "编队成员数量不足，是否自动编队？", "confidence": 1.0, "center": (951, 477)},
        {"text": "取消", "confidence": 1.0, "center": (762, 683)},
        {"text": "确认", "confidence": 1.0, "center": (1196, 683)},
    ]


def boss_result_frame():
    """boss 计分结算页：作战完成/有效分贝/总分贝（无「达成波次」）。"""
    return [
        {"text": "作战完成", "confidence": 1.0, "center": (1450, 260)},
        {"text": "有效分贝：", "confidence": 1.0, "center": (1250, 505)},
        {"text": "19532", "confidence": 1.0, "center": (1650, 505)},
        {"text": "总分贝", "confidence": 1.0, "center": (1440, 755)},
    ]


def boss_summary_frame():
    """boss 战后总结页：出战队伍/本轮分贝/重新降噪——无 boss 标题，面板判定不认。"""
    return [
        {"text": "出战队伍", "confidence": 1.0, "center": (175, 224)},
        {"text": "本轮分贝", "confidence": 1.0, "center": (1090, 845)},
        {"text": "重新降噪", "confidence": 1.0, "center": (1440, 948)},
        {"text": "19532", "confidence": 1.0, "center": (1000, 880)},
    ]


def map_reward_frame():
    """地图左下「猎区累计分贝奖励」页帧：标题 + 档位 + 一键领取。"""
    return [
        {"text": "猎区累计分贝奖励", "confidence": 1.0, "center": (971, 213)},
        {"text": "一级响度", "confidence": 1.0, "center": (439, 338)},
        {"text": "领取》", "confidence": 1.0, "center": (1514, 340)},
        {"text": "一键领取", "confidence": 1.0, "center": (1538, 877)},
    ]


def test_boss_panel_and_score():
    print("[13] boss 面板判定 + 本期最高分贝读取")
    check("地图「已收录」条不是面板（无面板独有词）",
          bd._echo_boss_panel_confirmed(boss_strip_frame()) is False)
    check("boss 面板双证据命中", bd._echo_boss_panel_confirmed(boss_panel_frame()) is True)
    check("战后总结页不是面板（无 boss 标题）",
          bd._echo_boss_panel_confirmed(boss_summary_frame()) is False)
    check("分数读取：label/数字拆条按同列取数",
          bd._echo_read_top_score(boss_panel_frame()) == 0,
          bd._echo_read_top_score(boss_panel_frame()))
    check("分数读取：战后帧 19532",
          bd._echo_read_top_score(boss_panel_done_frame()) == 19532)
    check("分数读取：无 label → None",
          bd._echo_read_top_score(map_frame()) is None)


def test_boss_fight_gate_and_chain():
    print("[14] boss 战斗闸门与链路")
    # 闸门 1：重新降噪/快速降噪在场 → skipped（本期已打过）
    with mock.patch.object(bd, "_echo_wait_map",
                           return_value=(True, map_frame(), "2026-12-01")), \
         mock.patch.object(bd, "_ocr_best_match",
                           return_value={"center": (837, 543), "text": "不协和音·鱼叉将军",
                                         "confidence": 1.0}), \
         mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: None), \
         mock.patch.object(bd, "_scene_results", return_value=boss_panel_done_frame()):
        status, detail = bd._echo_boss_fight(logging_logger(), 1)
    check("已打过 → skipped 只领奖励", status == "skipped" and "已打过" in detail, (status, detail))

    # 闸门 2：本期最高分贝 ≥ 顶档 → skipped
    with mock.patch.object(bd, "_echo_wait_map",
                           return_value=(True, map_frame(), "2026-12-01")), \
         mock.patch.object(bd, "_ocr_best_match",
                           return_value={"center": (837, 543), "text": "不协和音·鱼叉将军",
                                         "confidence": 1.0}), \
         mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: None), \
         mock.patch.object(bd, "_scene_results", return_value=boss_panel_scored_frame()):
        status, detail = bd._echo_boss_fight(logging_logger(), 1)
    check("分数达标 → skipped", status == "skipped" and "顶档" in detail, (status, detail))

    # 正常链：点标题开面板 → 开始降噪 → 编队页 → 作战开始 → battle 直进 →
    # 收敛回面板 = fought（结算/总结页交互由 _echo_boss_wait_settled 的桩代表）
    guarded, ratios, fitting_clicks = [], [], []
    title_hit = {"center": (837, 543), "text": "不协和音·鱼叉将军", "confidence": 1.0}
    fight_hit = {"center": (1392, 946), "text": "开始降噪", "confidence": 1.0}
    with mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_echo_wait_map",
                           return_value=(True, map_frame(), "2026-12-01")), \
         mock.patch.object(bd, "_ocr_best_match", side_effect=[title_hit, fight_hit]), \
         mock.patch.object(bd, "_pyautogui_click_guarded",
                           side_effect=lambda *a: guarded.append(a)), \
         mock.patch.object(bd, "_click_ratio",
                           side_effect=lambda lg, hw, ratio, label="":
                               ratios.append(label) or True), \
         mock.patch.object(bd, "_scene_results", return_value=boss_panel_frame()), \
         mock.patch.object(bfit, "_wait_page", return_value=(True, "team", [])), \
         mock.patch.object(bfit, "_click",
                           side_effect=lambda lg, hw, p, label:
                               fitting_clicks.append(label) or True), \
         mock.patch.object(bfit, "_page_now", side_effect=[("battle", [])]), \
         mock.patch.object(bd, "_echo_boss_wait_settled",
                           side_effect=lambda lg, hw, sc, dl: (True, "boss_panel", [])):
        status, detail = bd._echo_boss_fight(logging_logger(), 1)
    check("正常链 fought", status == "fought", (status, detail))
    check("先点 boss 标签再点开始降噪",
          guarded == [(837, 543), (1392, 946)], guarded)
    check("作战开始已点", "作战开始" in fitting_clicks, fitting_clicks)

    # 空编队弹窗：等编队页失败 → 「编队成员数量不足」在场 → 点确认 → 重等成功
    guarded2 = []
    with mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_echo_wait_map",
                           return_value=(True, map_frame(), "2026-12-01")), \
         mock.patch.object(bd, "_ocr_best_match", side_effect=[title_hit, fight_hit]), \
         mock.patch.object(bd, "_pyautogui_click_guarded",
                           side_effect=lambda *a: guarded2.append(a)), \
         mock.patch.object(bd, "_scene_results",
                           side_effect=[boss_panel_frame(), boss_panel_frame(),
                                        boss_dialog_frame(), boss_dialog_frame()]), \
         mock.patch.object(bfit, "_wait_page",
                           side_effect=[(False, "unknown", []), (True, "team", [])]), \
         mock.patch.object(bd, "_pick_result",
                           return_value={"center": (1196, 683), "text": "确认",
                                         "confidence": 0.9}), \
         mock.patch.object(bfit, "_click",
                           side_effect=lambda lg, hw, p, label: True), \
         mock.patch.object(bfit, "_page_now", side_effect=[("battle", [])]), \
         mock.patch.object(bd, "_echo_boss_wait_settled",
                           side_effect=lambda lg, hw, sc, dl: (True, "boss_panel", [])):
        status, detail = bd._echo_boss_fight(logging_logger(), 1)
    check("空编队弹窗点确认后照常开打", status == "fought", (status, detail))
    check("确认弹窗点击坐标 = OCR center", (1196, 683) in guarded2, guarded2)


def test_boss_ladder_claim():
    print("[15] 声量阶梯领取：金额 OCR → 图标偏移点击")
    clicks = []
    with mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_scene_results", return_value=boss_panel_frame()), \
         mock.patch.object(bd, "_pyautogui_click_guarded",
                           side_effect=lambda *a: clicks.append(a)):
        clicked, detail = bd._echo_boss_claim_ladder(logging_logger(), 1)
    check("五档全部点击", clicked == 5, (clicked, detail))
    check("点击坐标 = 金额位置 + 实测偏移 (+118,+20)，按 y 序",
          clicks == [(263, 373), (263, 497), (263, 621), (263, 744), (263, 868)], clicks)


def test_map_rewards_claim():
    print("[16] 地图左下「猎区累计分贝奖励」：入口 → 一键领取 → 关 X")
    guarded, ratios = [], []
    with mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_ocr_best_match", side_effect=[
             {"center": (209, 1015), "text": "猎区累计分贝奖励", "confidence": 1.0},
             {"center": (1538, 877), "text": "一键领取", "confidence": 1.0},
         ]), \
         mock.patch.object(bd, "_scene_results", return_value=map_reward_frame()), \
         mock.patch.object(bd, "_pyautogui_click_guarded",
                           side_effect=lambda *a: guarded.append(a)), \
         mock.patch.object(bd, "_click_ratio",
                           side_effect=lambda lg, hw, ratio, label="":
                               ratios.append(label) or True), \
         mock.patch.object(bd, "_echo_wait_map",
                           return_value=(True, map_frame(), "2026-12-01")):
        ok, detail = bd._echo_map_rewards_claim(logging_logger(), 1)
    check("奖励页领取完成", ok is True, (ok, detail))
    check("先点入口再点一键领取", guarded == [(209, 1015), (1538, 877)], guarded)
    check("收尾关 X 走比例点击", ratios == ["猎区分贝奖励页关闭"], ratios)

    # 奖励页双证据不满足（点入口后仍在地图）→ 不盲点一键领取
    guarded2, ratios2 = [], []
    with mock.patch.object(bd, "time", FakeTime()), \
         mock.patch.object(bd, "_ocr_best_match",
                           return_value={"center": (209, 1015), "text": "猎区累计分贝奖励",
                                         "confidence": 1.0}), \
         mock.patch.object(bd, "_scene_results", return_value=map_frame()), \
         mock.patch.object(bd, "_pyautogui_click_guarded",
                           side_effect=lambda *a: guarded2.append(a)), \
         mock.patch.object(bd, "_click_ratio",
                           side_effect=lambda lg, hw, ratio, label="":
                               ratios2.append(label) or True), \
         mock.patch.object(bd, "_record_step_failure",
                           side_effect=lambda *a, **k: None):
        ok, detail = bd._echo_map_rewards_claim(logging_logger(), 1)
    check("奖励页未确认 → 不点一键领取不关页",
          ok is False and guarded2 == [(209, 1015)] and ratios2 == [], (ok, guarded2, ratios2))


def test_boss_wiring():
    print("[17] 接线：分支挂 result['boss']；boss 失败不改节点链状态")
    boss_stub = {"status": "success", "detail": "boss：计分战完成；声量阶梯点击 5 档",
                 "fought": True, "claims": "声量阶梯点击 5 档"}
    echo_stub = {"status": "success", "detail": "节点链走完：本场打 ['A']",
                 "fought": ["A"], "skipped": []}
    with mock.patch.object(bd, "_ocr_best_match",
                           return_value={"center": (1572, 400), "text": "回声猎区",
                                         "confidence": 0.9}), \
         mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: None), \
         mock.patch.object(bd, "_capture_daily_list_snapshot", side_effect=lambda *a, **k: None), \
         mock.patch.object(bd, "_echo_zone_run_content",
                           side_effect=lambda *a, **k: dict(echo_stub)), \
         mock.patch.object(bd, "_echo_boss_and_map_rewards",
                           side_effect=lambda *a, **k: dict(boss_stub)), \
         mock.patch.object(bd, "_leave_club_page", return_value=True), \
         mock.patch.object(bd.time, "sleep", lambda *_: None):
        r = bd._run_club_activity_echo_zone(logging_logger(), hwnd=1)
    check("boss 结果挂 result['boss']", r.get("boss") == boss_stub, r.get("boss"))
    check("节点链状态不受 boss 影响", r["status"] == "success", r)
    check("detail 拼接 boss 摘要", "boss：success" in r["detail"], r["detail"])

    boss_fail = {"status": "failed", "detail": "boss：面板未打开", "fought": False, "claims": ""}
    with mock.patch.object(bd, "_ocr_best_match",
                           return_value={"center": (1572, 400), "text": "回声猎区",
                                         "confidence": 0.9}), \
         mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: None), \
         mock.patch.object(bd, "_capture_daily_list_snapshot", side_effect=lambda *a, **k: None), \
         mock.patch.object(bd, "_echo_zone_run_content",
                           side_effect=lambda *a, **k: dict(echo_stub)), \
         mock.patch.object(bd, "_echo_boss_and_map_rewards",
                           side_effect=lambda *a, **k: dict(boss_fail)), \
         mock.patch.object(bd, "_leave_club_page", return_value=True), \
         mock.patch.object(bd.time, "sleep", lambda *_: None):
        r = bd._run_club_activity_echo_zone(logging_logger(), hwnd=1)
    check("boss 失败 → 节点链状态仍 success", r["status"] == "success", r)
    check("detail 含 boss 失败原因", "boss：failed" in r["detail"], r["detail"])


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
    test_node_label_region_filter()
    test_fight_node_team_chain()
    test_battle_settle_adaptive()
    test_battle_settle_echo_result_and_defeat()
    test_boss_panel_and_score()
    test_boss_fight_gate_and_chain()
    test_boss_ladder_claim()
    test_map_rewards_claim()
    test_boss_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
