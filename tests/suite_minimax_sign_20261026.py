"""MiniMax 每日签到回归测试（2026-10-26）。

背景（实锤）：
- 步骤4 目标文案写成「立即签到」，3.1.6 新版卡片实际按钮是「签到得 ⊙ 400」
  → OCR 永远找不到 → 这步从没点过签到。
- 步骤5 UI 兜底用模板 minimax-今日已签到.png，在**未签到**的黑底按钮上
  实测 conf=0.9477（兜底阈值 0.88）→ 假阳性 → 流程报"已签到"但实际没签。

断言：
  1. 配置：步骤4 目标文案 = 签到得；步骤5 用 ui_text 且不再引用 ui_template
  2. 接口日志有 signin/claim 200 → 判成功
  3. 无日志 + UI OCR 出现「今日已签到」→ 判成功
  4. 无日志 + UI 只有「签到得 ⊙ 400」→ **必须判失败**（防假阳性回归）

跑法：.venv\\Scripts\\python.exe artifacts\\test_minimax_sign_20261026.py
"""
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, r"E:\GithubProject")

from game_auto.runtime import wool_claim_logs as wcl  # noqa: E402

FAILURES = []
LOGGER = logging.getLogger("minimax_test")


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_config():
    print("[1] flow_configs.json MINIMAX 配置")
    cfg = json.loads(Path(r"E:\GithubProject\game_auto\flow_configs.json").read_text(encoding="utf-8"))
    flow = cfg.get("MINIMAX") or {}
    steps = flow.get("steps") or []
    by_name = {s.get("name"): s for s in steps}

    step4 = by_name.get("点击签到得按钮")
    check("步骤4 存在", step4 is not None, list(by_name))
    if step4:
        check("步骤4 目标文案 = 签到得", step4.get("target_text") == "签到得", step4.get("target_text"))
        check("步骤4 走 OCR 兜底", step4.get("enable_ocr_fallback") is True)
        check("步骤4 仍可选（已签到时跳过）", step4.get("optional_if_missing") is True)

    step5 = by_name.get("日志确认签到")
    check("步骤5 存在", step5 is not None)
    if step5:
        check("步骤5 用 ui_text=今日已签到", step5.get("ui_text") == "今日已签到", step5.get("ui_text"))
        check("步骤5 不再引用 ui_template", not step5.get("ui_template"), step5.get("ui_template"))

    old = [s for s in steps if s.get("target_text") == "立即签到"]
    check("没有残留的「立即签到」目标文案", not old, old)


def _log_file(with_claim: bool) -> Path:
    # 固定路径：Windows 上临时文件句柄释放慢，测试内删除会 PermissionError
    p = Path(__file__).resolve().parent.parent / "data" / "artifacts" / "_minimax_fake_api.log"
    body = ""
    if with_claim:
        body = (
            'POST https://api.minimax.chat/minimax-cloud/api/v1/signin/claim '
            '{"status":"200","baseRespStatusCode":0,"data":{"credits":400}}'
        )
    p.write_text(body, encoding="utf-8")
    return p


def test_verify_log_hit():
    print("[2] 接口日志命中 signin/claim 200")
    p = _log_file(True)
    with patch.object(wcl, "minimax_log_path", return_value=p):
        ok, msg = wcl.verify_minimax(LOGGER, None, None, None, "", 1)
    check("判成功", ok, msg)
    check("消息是日志路径", "日志" in msg, msg)


def test_verify_ui_signed():
    print("[3] 无日志 + UI 显示「今日已签到」")
    p = _log_file(False)
    fake = [{"text": "本轮已连续签到2天", "confidence": 0.9, "center": (300, 815)},
            {"text": "今日已签到", "confidence": 0.88, "center": (170, 930)}]
    with patch.object(wcl, "minimax_log_path", return_value=p), \
         patch("game_auto.runtime.ocr_click_tester.ocr_window_content", return_value=fake):
        ok, msg = wcl.verify_minimax(LOGGER, 123, None, None, "", 1)
    check("判成功", ok, msg)
    check("消息是 UI 路径", "UI" in msg, msg)


def test_verify_unsigned_not_false_positive():
    print("[4] 无日志 + UI 只有「签到得 ⊙ 400」→ 必须判失败")
    p = _log_file(False)
    fake = [{"text": "每日签到", "confidence": 0.9, "center": (60, 780)},
            {"text": "本轮已连续签到2天", "confidence": 0.9, "center": (300, 815)},
            {"text": "签到得", "confidence": 0.86, "center": (140, 930)},
            {"text": "400", "confidence": 0.85, "center": (185, 930)}]
    with patch.object(wcl, "minimax_log_path", return_value=p), \
         patch("game_auto.runtime.ocr_click_tester.ocr_window_content", return_value=fake):
        ok, msg = wcl.verify_minimax(LOGGER, 123, None, None, "", 1)
    check("判失败（不能假阳性）", not ok, msg)
    check("消息是未确认", "未确认" in msg, msg)


def test_verify_no_ocr_no_log():
    print("[5] 无日志 + 无 OCR 结果 → 判失败")
    p = _log_file(False)
    with patch.object(wcl, "minimax_log_path", return_value=p), \
         patch("game_auto.runtime.ocr_click_tester.ocr_window_content", return_value=[]):
        ok, msg = wcl.verify_minimax(LOGGER, 123, None, None, "", 1)
    check("判失败", not ok, msg)


def main():
    test_config()
    test_verify_log_hit()
    test_verify_ui_signed()
    test_verify_unsigned_not_false_positive()
    test_verify_no_ocr_no_log()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
