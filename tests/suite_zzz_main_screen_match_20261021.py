"""ZZZ 主界面模板匹配与导入链补充验证（2026-10-21）。

1. 导入链：task_batch_runner -> background_template_runner -> foreground_flow_runner
   （launch_task_catalog.py 改动的 focused static validation）
2. 模板实跑：把 绝区零-主界面-电台.png 贴到 1920x1080 合成图的 OneDragon 标注位置
   （pc_rect 1696,319-1858,415 中心），验证匹配器在 scale 1.0/1.25/1.5 下均能命中，
   并打印匹配分数，确认带 alpha 通道的 PNG 在管线中行为正常。

运行：cd /e/GithubProject && game_auto/.venv/Scripts/python.exe tests/suite_zzz_main_screen_match_20261021.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

import numpy as np
from PIL import Image

failures = []


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


# 1. 导入链
try:
    from game_auto.runtime import task_batch_runner  # noqa: F401
    from game_auto.runtime.background_template_runner import run_zzz_monthly_background  # noqa: F401
    from game_auto.runtime.foreground_flow_runner import _match_template_cv2_once, _get_cached_template  # noqa: E402
    check("导入链 task_batch_runner/background/foreground 完好", True)
except Exception as exc:
    check("导入链 task_batch_runner/background/foreground 完好", False, repr(exc))
    sys.exit(1)

# 2. 合成底图模拟不同渲染分辨率：屏幕图标与模板同比例缩放
#    （1080p 屏上图标 52x52 用 scale=1.0 匹配；1440p 屏上图标 78x78 用 scale=1.5 匹配）
tpl_path = PROJECT_ROOT / "模板" / "绝区零-主界面-电台.png"
tpl = _get_cached_template(str(tpl_path))
check("模板可加载", tpl is not None)

for scale in (1.0, 1.25, 1.5):
    iw, ih = int(tpl.width * scale), int(tpl.height * scale)
    W, H = int(1920 * scale), int(1080 * scale)
    t_scaled = tpl.resize((iw, ih))
    base = Image.new("RGB", (W, H), (24, 26, 30))  # 深色 UI 底板
    tx, ty = 1777 * scale - iw // 2, 367 * scale - ih // 2  # 按比例缩放 OneDragon 标注位置
    base.paste(t_scaled.convert("RGB"), (int(tx), int(ty)))
    pt = _match_template_cv2_once(base, t_scaled, [0.88, 0.84, 0.8])
    check(f"{int(W)}x{int(H)} 合成图 scale={scale} 命中", pt is not None, f"center={pt}")

# 3. 空白图不应命中（防空图误匹配导致假成功）
blank = Image.new("RGB", (1920, 1080), (24, 26, 30))
pt = _match_template_cv2_once(blank, tpl, [0.88, 0.84, 0.8])
check("纯色空白图不误匹配", pt is None, f"误命中 center={pt}")

# 4. 打印原始匹配分数供阈值参考（1080p 合成图）
base_1080 = Image.new("RGB", (1920, 1080), (24, 26, 30))
base_1080.paste(tpl.convert("RGB"), (1751, 341))
import cv2  # noqa: E402

scr_gray = cv2.cvtColor(np.asarray(base_1080.convert("RGB")), cv2.COLOR_RGB2GRAY)
tpl_gray = cv2.cvtColor(np.asarray(tpl.convert("RGB")), cv2.COLOR_RGB2GRAY)
res = cv2.matchTemplate(scr_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
_, max_val, _, max_loc = cv2.minMaxLoc(res)
print(f"参考：1080p 合成图自匹配分数 = {max_val:.4f} @ {max_loc}（最低阈值 0.8）")

print()
if failures:
    print(f"共 {len(failures)} 项失败")
    sys.exit(1)
print("全部通过")
