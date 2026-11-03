"""失败证据「OCR 标注图」回归（2026-11-03 新增）。

背景：flow_failures 只有一张干净截图 + 一堆 `文字(置信度)@x,y` 字符串，
事后排查还得对着坐标脑补哪个框是哪个字。用户要求"在图片上直接 P 上识别
结果留档"，这里用 PIL 把 OCR 框、文字、置信度画到截图上，另存 `*_ocr.jpg`。

本套件只测纯函数（不抓屏、不写库）：
  1. `_annotate_ocr_overlay` 有 box / 只有 center 两种输入都能出图且尺寸不变；
  2. 空结果不炸；
  3. `_results_to_sample` 文本格式与旧 `collect_ocr_sample` 保持一致；
  4. 字体加载失败时退回默认字体（不抛异常）。

跑法：.venv\\Scripts\\python.exe tests\\suite_flow_diagnostics_annotate_20261103.py
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from PIL import Image  # noqa: E402

import game_auto.runtime.flow_diagnostics as fd  # noqa: E402

LOG = logging.getLogger("flow-diagnostics-annotate-test")
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _fake_image(w=1280, h=720):
    img = Image.new("RGB", (w, h), (28, 30, 34))
    return img


def test_1_box_overlay():
    print("[1] 带 box 的结果：画框 + 编号，底部图例区，宽度不变")
    img = _fake_image()
    results = [
        {"text": "快速战斗设置", "confidence": 0.85, "center": (960, 442), "box": (700, 410, 1220, 470)},
        {"text": "确认使用", "confidence": 0.80, "center": (1181, 740), "box": (1080, 710, 1280, 770)},
    ]
    out = fd._annotate_ocr_overlay(img, results, "BaiJingDaily_快速战斗 · 2 个文本区域", scale=0.75)
    check("返回图片", out is not None)
    check("宽度不变", out is not None and out.size[0] == img.size[0], None if out is None else out.size)
    check("高度增加（图例区）", out is not None and out.size[1] > img.size[1], None if out is None else out.size)


def test_2_center_only():
    print("[2] 只有 center 的结果（历史 flow_failures 数据）：用 8x8 小框标注")
    img = _fake_image(800, 450)
    results = [{"text": "ASHECHOES", "confidence": 0.88, "center": (968, 541)}]
    out = fd._annotate_ocr_overlay(img, results, "center-only", scale=0.8)
    check("返回图片", out is not None)
    check("宽度不变", out is not None and out.size[0] == img.size[0])
    check("高度不小于原图", out is not None and out.size[1] >= img.size[1])


def test_3_empty_results():
    print("[3] 空结果/None：原图直接返回，不炸也不加图例")
    img = _fake_image(640, 360)
    out_empty = fd._annotate_ocr_overlay(img, [], "empty")
    out_none = fd._annotate_ocr_overlay(img, None, "none")
    check("空列表可出图", out_empty is not None)
    check("None 可出图", out_none is not None)
    check("无结果时尺寸不变", out_empty is not None and out_empty.size == img.size)


def test_4_sample_format():
    print("[4] _results_to_sample 与旧格式一致")
    results = [
        {"text": "任务完成", "confidence": 0.79, "center": (414, 162)},
        {"text": "结算", "confidence": 0.66, "center": (960, 540)},
    ]
    sample = fd._results_to_sample(results)
    check("条数一致", len(sample) == 2, sample)
    check("格式为 文字(置信度)@x,y", sample[0] == "任务完成(0.79)@414,162", sample[0])
    check("空输入返回空列表", fd._results_to_sample([]) == [], fd._results_to_sample([]))


def test_5_font_fallback():
    print("[5] 字体加载：拿不到中文字体也要返回一个可用字体")
    font = fd._load_font(12)
    check("返回非 None", font is not None)


def test_6_capture_writes_pair():
    print("[6] capture_failure_image：原图 + 标注图都落盘（不抓真实屏幕）")
    import shutil
    import tempfile

    import game_auto.runtime.ocr_click_tester as oct

    tmp = Path(tempfile.mkdtemp(prefix="fd_annotate_"))
    saved_screen = oct.grab_screen
    saved_dir = fd.FLOW_FAILURE_DIR
    oct.grab_screen = lambda region=None: Image.new("RGB", (1920, 1080), (20, 20, 20))
    fd.FLOW_FAILURE_DIR = tmp
    try:
        raw, ann = fd.capture_failure_image(
            LOG,
            hwnd=None,
            tag="test",
            ocr_results=[{"text": "确认使用", "confidence": 0.8, "center": (1181, 740), "box": (1080, 710, 1280, 770)}],
        )
    finally:
        oct.grab_screen = saved_screen
        fd.FLOW_FAILURE_DIR = saved_dir
    check("返回原图名", bool(raw), raw)
    check("返回标注图名", bool(ann), ann)
    check("原图已落盘", (tmp / raw).is_file() if raw else False)
    check("标注图已落盘", (tmp / ann).is_file() if ann else False)
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_1_box_overlay()
    test_2_center_only()
    test_3_empty_results()
    test_4_sample_format()
    test_5_font_fallback()
    test_6_capture_writes_pair()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
