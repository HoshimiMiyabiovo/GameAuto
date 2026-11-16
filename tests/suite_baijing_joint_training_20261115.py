"""白荆回廊「联合训练场」入口层回归（2026-11-15）。

背景：新活动「联合训练·捷速难逃」（外勤作战中心 → 联合训练场，2026-12-21
结束）。用户 11-15 拍板玩法口径：3 防区各 3 槽位摆队后纯挂机推演（无战斗），
**每周结算**（周一 04:00 周重置——hub「6小时54分后结算」倒计时即距周重置
剩余时间）；摆队约束**队长不可重复**；防区分期解锁（11-18 04:00）。phase 1
只读侦察：进外勤 → 点「联合训练场」→ 防区页识别取证 → 返回，不点防区/槽位。

实机帧来源：`data/artifacts/ref_baijing_joint_{center,zones}_20261115.png`
（1919x1080 用户截图），OCR 转录 `ref_baijing_joint_ocr_20261115.json`
（onnxocr v6，`data/artifacts/_probe_joint_ocr_20261115.py` 生成）。

节清单：
  [1] 常量守卫（区域合法/实测坐标落区/邻居排除/事件结束日）
  [2] 页面判定矩阵（防区页 True；外勤 hub 帧防串 False；单标题不判页）
  [3] 防区状态解析纯函数（未完成数/进度/结束日）+ hub 结算倒计时
  [4] 入口未命中禁点（skipped 零点击）
  [5] 活动过期 skip（EVENT_END 之后不进外勤页）
  [6] 成功链路（进页→识别→取证→返回，点击序列精确断言）
  [7] 标签点不进 → 图标比例兜底（echo boss「点标题→没面板再点图标」同款）
  [8] 两次都进不去 → failed + 回主界面
  [9] 退出失败 → failed 需人工恢复
  [10] 接线静态断言（baijing_flow 4.6 / games 卡片 / policy REGISTRY /
       ci_suites / changelog）

跑法：`.venv\\Scripts\\python.exe tests/suite_baijing_joint_training_20261115.py`
"""
from __future__ import annotations

import sys
import unittest.mock as mock
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

import io as _io

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from game_auto.runtime import baijing_fitting as bfit
from game_auto.runtime import baijing_joint_training as bjt

FAILURES = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {label}" + ("" if ok else f"  {detail}"))
    if not ok:
        FAILURES.append(label)


# ── 实机 OCR 帧（ref_baijing_joint_ocr_20261115.json 转录，1919x1080）─────
HUB_FRAME = [
    {"text": "物资筹备", "confidence": 1.0, "center": (126, 108)},
    {"text": "光刻协议", "confidence": 1.0, "center": (761, 162)},
    {"text": "自由贸易", "confidence": 1.0, "center": (456, 163)},
    {"text": "定向物资保障", "confidence": 1.0, "center": (306, 165)},
    {"text": "显影行动", "confidence": 1.0, "center": (612, 165)},
    {"text": "新质领域", "confidence": 1.0, "center": (915, 165)},
    {"text": "漫巡回忆", "confidence": 1.0, "center": (127, 263)},
    {"text": "漫巡回忆", "confidence": 1.0, "center": (309, 316)},
    {"text": "队长养成", "confidence": 1.0, "center": (129, 414)},
    {"text": "卓越道路", "confidence": 1.0, "center": (310, 467)},
    {"text": "分进合击战", "confidence": 1.0, "center": (1347, 488)},
    {"text": "思维演绎", "confidence": 1.0, "center": (130, 567)},
    {"text": "外勤作战中心", "confidence": 1.0, "center": (1622, 600)},
    {"text": "联合训练场", "confidence": 1.0, "center": (1220, 616)},
    {"text": "6小时54分后结算", "confidence": 1.0, "center": (1246, 646)},
    {"text": "天隙勘测", "confidence": 1.0, "center": (1130, 767)},
    {"text": "收起", "confidence": 1.0, "center": (1635, 782)},
    {"text": "拟合回归", "confidence": 1.0, "center": (1095, 918)},
    {"text": "6小时54分", "confidence": 1.0, "center": (1096, 950)},
    {"text": "战斗教学", "confidence": 1.0, "center": (1642, 1003)},
]

ZONE_FRAME = [
    {"text": "返回", "confidence": 1.0, "center": (193, 52)},
    {"text": "联合训练·捷速难逃", "confidence": 1.0, "center": (210, 156)},
    {"text": "2026-12-21-00:00结束", "confidence": 1.0, "center": (192, 190)},
    {"text": "防区推演进度", "confidence": 1.0, "center": (135, 242)},
    {"text": "0%/180%", "confidence": 1.0, "center": (101, 305)},
    {"text": "0%", "confidence": 1.0, "center": (127, 344)},
    {"text": "防区未完成", "confidence": 1.0, "center": (125, 384)},
    {"text": "0%", "confidence": 0.98, "center": (634, 471)},
    {"text": "0%", "confidence": 0.99, "center": (1303, 472)},
    {"text": "防区未完成", "confidence": 1.0, "center": (632, 512)},
    {"text": "防区未完成", "confidence": 1.0, "center": (1302, 512)},
    {"text": "24919904", "confidence": 1.0, "center": (1659, 1068)},
]

JOINT_LABEL_CENTER = (1220, 616)      # hub 页「联合训练场」标签实测
JOINT_ICON_CENTER = (1047, 605)       # 六边形图标中心（标签点不进时的兜底比例）
ZONE_BACK_CENTER = (193, 52)


class FakeTime:
    """整体顶替 bjt.time：time() 每次 +0.05 防 busy-loop，sleep 记录不真睡。"""

    def __init__(self):
        self._now = 1000.0
        self.sleeps = []

    def time(self):
        self._now += 0.05
        return self._now

    def sleep(self, sec):
        self.sleeps.append(sec)

    def strftime(self, fmt, *a):
        import time as real_time

        return real_time.strftime(fmt)


def _records():
    return {"guarded": [], "ratio": [], "snapshot": [], "ocr_texts": []}


def _patch_env(frames_for_ocr=None, zone_waits=None, ensure_main=True, ft=None,
               entry_hit=True):
    """一次性打桩：窗口/OCR/点击/快照/主界面恢复。

    frames_for_ocr: ocr_window_content side_effect（_wait_zone_page 的轮询帧）
    zone_waits: 若给定，patch bjt._wait_zone_page 的 side_effect
    entry_hit: False 时 OCR 永远未命中（入口未命中禁点用）
    """
    rec = _records()
    ft = ft or FakeTime()
    if isinstance(ensure_main, list):
        ensure_main_side = list(ensure_main)
    else:
        ensure_main_side = ensure_main

    ensure_main_kw = (
        {"side_effect": ensure_main_side}
        if isinstance(ensure_main_side, list)
        else {"return_value": ensure_main_side}
    )

    def _click_ratio_rec(logger, hwnd, point, label):
        rec["ratio"].append((point, label))
        return True

    def _guarded_rec(x, y):
        rec["guarded"].append((x, y))
        return True

    def _snapshot_rec(logger, hwnd, prefix="daily_list"):
        rec["snapshot"].append(prefix)
        return "snap.jpg"

    def _ocr_rec(logger, texts, hwnd, *a, **kw):
        rec["ocr_texts"].append(tuple(texts))
        if not entry_hit:
            return None
        if any("联合训练场" in t for t in texts):
            return {"center": JOINT_LABEL_CENTER, "text": "联合训练场", "confidence": 1.0}
        if any("返回" in t for t in texts):
            return {"center": ZONE_BACK_CENTER, "text": "返回", "confidence": 1.0}
        return None

    patches = [
        mock.patch.object(bjt, "time", ft),
        mock.patch.object(bjt, "_stopped", return_value=False),
        mock.patch.object(bjt, "_ensure_window", return_value=1234),
        mock.patch.object(bjt, "_ensure_main_ui", **ensure_main_kw),
        mock.patch.object(bjt, "_click_ratio", side_effect=_click_ratio_rec),
        mock.patch.object(bjt, "_pyautogui_click_guarded", side_effect=_guarded_rec),
        mock.patch.object(bjt, "_capture_daily_list_snapshot", side_effect=_snapshot_rec),
        mock.patch.object(bjt, "_ocr_best_match", side_effect=_ocr_rec),
    ]
    if frames_for_ocr is not None:
        patches.append(mock.patch.object(bjt, "ocr_window_content",
                                         side_effect=list(frames_for_ocr)))
    if zone_waits is not None:
        patches.append(mock.patch.object(bjt, "_wait_zone_page",
                                         side_effect=list(zone_waits)))
    return patches, rec, ft


def test_constants():
    print("[1] 常量守卫")
    for name, region in (("HUB_ENTRY_REGION", bjt.HUB_ENTRY_REGION),):
        check(f"{name} 形状合法", region[0] < region[2] and region[1] < region[3], str(region))
    # 实测标签 (1220,616)@1919x1080 必须落在入口检测区域内
    fx, fy = JOINT_LABEL_CENTER[0] / 1919, JOINT_LABEL_CENTER[1] / 1079
    r = bjt.HUB_ENTRY_REGION
    check("实测标签坐标落在检测区域", r[0] <= fx <= r[2] and r[1] <= fy <= r[3],
          f"frac=({fx:.3f},{fy:.3f}) region={r}")
    # 邻居排除：天隙勘测 (0.589,0.710) / 分进合击战 (0.702,0.452) 都不在区
    for label, center in (("天隙勘测", (1130, 767)), ("分进合击战", (1347, 488))):
        fx, fy = center[0] / 1919, center[1] / 1079
        inside = r[0] <= fx <= r[2] and r[1] <= fy <= r[3]
        check(f"检测区域排除邻居 {label}", not inside, f"frac=({fx:.3f},{fy:.3f})")
    check("EVENT_END 与页面「结束」字样一致", bjt.EVENT_END == "2026-12-21")
    check("防区 body 词不在标题词里（双证据分离）",
          not set(bjt.ZONE_BODY_MARKERS) & set(bjt.ZONE_TITLE_MARKERS))
    check("图标兜底坐标与标签不同（真有第二次尝试）",
          abs(bjt.P_JOINT_ENTRY_ICON[0] - bjt.P_JOINT_ENTRY_LABEL[0]) > 0.03)


def test_page_matrix():
    print("[2] 页面判定矩阵（防串锁）")
    check("防区实机帧判 True", bjt._is_joint_zone_page(ZONE_FRAME))
    check("外勤 hub 实机帧判 False（「联合训练场」超集不得串页）",
          not bjt._is_joint_zone_page(HUB_FRAME))
    # 铁律：单标题不判页
    check("只有标题（无 body 词）判 False",
          not bjt._is_joint_zone_page([
              {"text": "联合训练·捷速难逃", "confidence": 1.0, "center": (210, 156)}]))
    check("只有「联合训练场」判 False",
          not bjt._is_joint_zone_page([
              {"text": "联合训练场", "confidence": 1.0, "center": (1220, 616)}]))
    # 防区全领后「防区未完成」消失、「防区推演进度」仍在 → 判定不退化
    claimed = [r for r in ZONE_FRAME if r["text"] != "防区未完成"]
    claimed.append({"text": "防区已完成", "confidence": 1.0, "center": (125, 384)})
    check("防区全领（无未完成）仍判 True", bjt._is_joint_zone_page(claimed))
    # 低置信度行不参与（MIN_CONF=0.5 口径）
    low = [{"text": "防区未完成", "confidence": 0.4, "center": (1, 1)}]
    check("低置信度 body 行不算证据", not bjt._is_joint_zone_page(low))


def test_parse():
    print("[3] 防区状态解析")
    st = bjt.parse_zone_state(ZONE_FRAME)
    check("未完成防区数 = 3", st["incomplete_zones"] == 3, str(st["incomplete_zones"]))
    check("总进度 0/180", st["progress"] == {"current": 0, "cap": 180}, str(st["progress"]))
    check("活动结束日 2026-12-21", st["event_end"] == "2026-12-21", str(st["event_end"]))
    no_progress = [r for r in ZONE_FRAME if "%" not in r["text"]]
    check("读不到进度时为 None（侦察不盲猜）",
          bjt.parse_zone_state(no_progress)["progress"] is None)
    check("hub 结算倒计时解析", bjt.parse_hub_countdown(HUB_FRAME) == "6小时54分后结算")
    check("无倒计时帧返回 None", bjt.parse_hub_countdown(ZONE_FRAME) is None)


def test_no_entry_no_click():
    print("[4] 入口未命中禁点")
    patches, rec, _ft = _patch_env(frames_for_ocr=None, zone_waits=[(True, ZONE_FRAME)],
                                   entry_hit=False)
    with _multi(patches + [_wait_page_patched(True, "field", HUB_FRAME)]):
        res = bjt.run_joint_training_recon(__import__("logging").getLogger("t"), 1234)
    check("未命中 → skipped", res["status"] == "skipped", res["detail"])
    check("零点击（guarded）", not rec["guarded"], str(rec["guarded"]))
    check("ratio 只有一次外勤入口点击（未命中后不再有任何点击）",
          len(rec["ratio"]) == 1 and rec["ratio"][0][1] == "外勤", str(rec["ratio"]))
    check("未 entered", res["entered"] is False)


def test_event_expired():
    print("[5] 活动过期 skip")
    ft = FakeTime()
    ft.strftime = lambda fmt, *a: "2026-12-22"  # EVENT_END 之后
    patches, rec, _ft = _patch_env(frames_for_ocr=[], ft=ft)
    with _multi(patches):
        res = bjt.run_joint_training_recon(__import__("logging").getLogger("t"), 1234)
    check("过期 → skipped", res["status"] == "skipped", res["detail"])
    check("过期不进外勤（零点击）", not rec["ratio"] and not rec["guarded"])


def test_success_path():
    print("[6] 成功链路")
    patches, rec, _ft = _patch_env(
        frames_for_ocr=None,
        zone_waits=[(True, ZONE_FRAME)],
    )
    with _multi(patches + [_wait_page_patched(True, "field", HUB_FRAME)]):
        res = bjt.run_joint_training_recon(__import__("logging").getLogger("t"), 1234)
    check("status success", res["status"] == "success", res["detail"])
    check("entered", res["entered"] is True)
    check("zones 解析（3 未完成 / 0/180）",
          res["zones"] and res["zones"]["incomplete_zones"] == 3
          and res["zones"]["progress"] == {"current": 0, "cap": 180},
          str(res["zones"]))
    check("hub 倒计时进结果", res["countdown"] == "6小时54分后结算", str(res["countdown"]))
    check("入口点击 = OCR 标签中心（首次 guarded 点击）",
          rec["guarded"][:1] == [JOINT_LABEL_CENTER], str(rec["guarded"]))
    check("退出点左上返回（末次 guarded 点击）",
          rec["guarded"][-1:] == [ZONE_BACK_CENTER], str(rec["guarded"]))
    check("主界面点击外勤走 _click_ratio",
          any(p[1] == "外勤" for p in rec["ratio"]), str(rec["ratio"]))
    check("取证快照一张", rec["snapshot"] == ["joint_training"], str(rec["snapshot"]))
    check("returned_main True", res["returned_main"] is True)


def test_icon_fallback():
    print("[7] 标签点不进 → 图标比例兜底")
    patches, rec, _ft = _patch_env(
        frames_for_ocr=None,
        zone_waits=[(False, HUB_FRAME), (True, ZONE_FRAME)],
    )
    with _multi(patches + [_wait_page_patched(True, "field", HUB_FRAME)]):
        res = bjt.run_joint_training_recon(__import__("logging").getLogger("t"), 1234)
    check("兜底后 success", res["status"] == "success", res["detail"])
    ratio_points = [p for p, _label in rec["ratio"]]
    check("图标兜底点击发生",
          any(abs(p[0] - bjt.P_JOINT_ENTRY_ICON[0]) < 1e-6
              and abs(p[1] - bjt.P_JOINT_ENTRY_ICON[1]) < 1e-6 for p in ratio_points),
          str(ratio_points))


def test_entry_never_opens():
    print("[8] 两次都进不去 → failed")
    patches, rec, _ft = _patch_env(
        frames_for_ocr=None,
        zone_waits=[(False, HUB_FRAME), (False, HUB_FRAME)],
    )
    with _multi(patches + [_wait_page_patched(True, "field", HUB_FRAME)]):
        res = bjt.run_joint_training_recon(__import__("logging").getLogger("t"), 1234)
    check("status failed", res["status"] == "failed", res["detail"])
    check("未取证快照（没进页不快照）", not rec["snapshot"], str(rec["snapshot"]))
    check("退回主界面", res["returned_main"] is True)


def test_exit_fail():
    print("[9] 退出失败 → failed 需人工恢复")
    patches, rec, _ft = _patch_env(
        frames_for_ocr=None,
        zone_waits=[(True, ZONE_FRAME)],
        ensure_main=[True, False],   # 进流程时回得到主界面，退出时回不去
    )
    with _multi(patches + [_wait_page_patched(True, "field", HUB_FRAME)]):
        res = bjt.run_joint_training_recon(__import__("logging").getLogger("t"), 1234)
    check("status failed", res["status"] == "failed", res["detail"])
    check("detail 标注需人工恢复", "需人工恢复" in res["detail"], res["detail"])
    check("returned_main False", res["returned_main"] is False)


def test_wiring():
    print("[10] 接线静态断言")
    flow_src = (PROJECT / "src/game_auto/runtime/baijing_flow.py").read_text(encoding="utf-8")
    check("baijing_flow 调用 run_joint_training_recon", "run_joint_training_recon" in flow_src)
    check("baijing_flow 写 joint_training 卡片", '"joint_training"' in flow_src)
    game_src = (PROJECT / "src/game_auto/games/baijing.py").read_text(encoding="utf-8")
    check("games/baijing.py 有 joint_training 占位", '"task_key": "joint_training"' in game_src)
    policy_src = (PROJECT / "tests/suite_page_judgement_policy.py").read_text(encoding="utf-8")
    check("policy REGISTRY 登记防区页判定",
          '("baijing_joint_training.py", "_is_joint_zone_page")' in policy_src)
    ci = (PROJECT / "tests/ci_suites.txt").read_text(encoding="utf-8")
    check("ci_suites.txt 登记本套件", "suite_baijing_joint_training_20261115.py" in ci)
    changelog = (PROJECT / "docs/changelog.md").read_text(encoding="utf-8")
    check("changelog 提及联合训练场", "联合训练场" in changelog)


# ── 助手 ─────────────────────────────────────────────────────────────────
class _multi:
    def __init__(self, patches):
        self._patches = patches
        self._started = []

    def __enter__(self):
        for p in self._patches:
            self._started.append(p.start())
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


def _wait_page_patched(ok, page, items):
    return mock.patch.object(bfit, "_wait_page", return_value=(ok, page, items))


def main():
    tests = [
        test_constants, test_page_matrix, test_parse, test_no_entry_no_click,
        test_event_expired, test_success_path, test_icon_fallback,
        test_entry_never_opens, test_exit_fail, test_wiring,
    ]
    for t in tests:
        t()
    print()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} 项失败 -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
