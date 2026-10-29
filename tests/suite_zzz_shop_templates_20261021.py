"""用真实绝区零截图验证商店模板匹配质量（2026-10-21）。

1. 商品/页签/入口模板对各自截图原图匹配，返回坐标应接近裁图原点
2. 「确认」模板对弹窗截图命中；对商店截图（无弹窗）不得命中
3. 打印匹配分数，确认与阈值 0.8 的余量
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

import cv2
import numpy as np
from PIL import Image

from game_auto.runtime.foreground_flow_runner import (
    _get_cached_template,
    _match_template_cv2_once,
)

TEMPLATES = PROJECT_ROOT / "模板"
SHOT_DIR = Path(r"C:/Users/admin/Pictures/Screenshots")
SHOTS = {
    "menu": SHOT_DIR / "屏幕截图 2026-10-21 225210.png",
    "shop": SHOT_DIR / "屏幕截图 2026-10-21 225301.png",
    "confirm": SHOT_DIR / "屏幕截图 2026-10-21 225342.png",
}

# (模板, 截图, 期望中心x, 期望中心y, 说明)
CASES = [
    ("绝区零-后勤商店入口.png", "menu", 1360, 597, "主菜单后勤商店入口"),
    ("绝区零-商店-哨站后勤页签.png", "shop", 230, 237, "哨站后勤页签(选中)"),
    ("绝区零-商店-协会物资页签.png", "shop", 230, 302, "协会物资页签(未选)"),
    ("绝区零-商店-调律校音器.png", "shop", 550, 300, "调律校音器图标"),
    ("绝区零-商店-原装母带.png", "shop", 845, 300, "原装母带图标"),
    ("绝区零-商店-邦布券.png", "shop", 1140, 300, "邦布券图标"),
    ("绝区零-商店-日常业绩.png", "shop", 1430, 300, "日常业绩图标"),
    ("绝区零-商店-音擎能源模块.png", "shop", 1721, 715, "音擎能源模块图标"),
    ("绝区零-商店-确认.png", "confirm", 1065, 760, "确认按钮"),
]

failures = []


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def match_score(screenshot_pil, tpl_pil):
    scr = cv2.cvtColor(np.asarray(screenshot_pil.convert("RGB")), cv2.COLOR_RGB2GRAY)
    tpl = cv2.cvtColor(np.asarray(tpl_pil.convert("RGB")), cv2.COLOR_RGB2GRAY)
    res = cv2.matchTemplate(scr, tpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    h, w = tpl.shape[:2]
    return max_val, (max_loc[0] + w // 2, max_loc[1] + h // 2)


imgs = {k: Image.open(p).convert("RGB") for k, p in SHOTS.items()}

for tpl_name, shot_key, ex, ey, label in CASES:
    tpl = _get_cached_template(str(TEMPLATES / tpl_name))
    if tpl is None:
        check(f"{label} 模板可加载", False)
        continue
    pt = _match_template_cv2_once(imgs[shot_key], tpl, [0.88, 0.84, 0.8])
    score, center = match_score(imgs[shot_key], tpl)
    if pt is None:
        check(f"{label} 匹配命中", False, f"score={score:.3f} center={center} 期望≈({ex},{ey})")
        continue
    # 命中点与裁图中心的偏差（商品卡 300px 宽，图标 240，容忍 ±60px）
    dx, dy = abs(center[0] - ex), abs(center[1] - ey)
    ok = dx <= 60 and dy <= 60 and score >= 0.8
    check(f"{label} 位置准确", ok, f"命中({center[0]},{center[1]}) 期望({ex},{ey}) 偏差({dx},{dy}) score={score:.3f}")

# 反向验证：确认按钮模板在商店界面（无弹窗）不得命中
tpl = _get_cached_template(str(TEMPLATES / "绝区零-商店-确认.png"))
pt = _match_template_cv2_once(imgs["shop"], tpl, [0.88, 0.84, 0.8])
score, center = match_score(imgs["shop"], tpl)
check("商店界面(无弹窗)不误命中确认按钮", pt is None, f"score={score:.3f} center={center}")

# 反向验证：商品图标模板在确认弹窗截图（背景被遮）不得命中
for tpl_name, label in [("绝区零-商店-原装母带.png", "原装母带"), ("绝区零-商店-日常业绩.png", "日常业绩")]:
    tpl = _get_cached_template(str(TEMPLATES / tpl_name))
    pt = _match_template_cv2_once(imgs["confirm"], tpl, [0.88, 0.84, 0.8])
    score, center = match_score(imgs["confirm"], tpl)
    check(f"弹窗截图(背景遮挡)不误命中{label}", pt is None or score < 0.88, f"score={score:.3f} center={center}")

print()
if failures:
    print(f"共 {len(failures)} 项失败")
    sys.exit(1)
print("全部通过")
