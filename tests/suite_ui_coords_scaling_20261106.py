"""2026-11-06 分辨率无关坐标回归。

背景：11-06 实机探针（data/artifacts/*_stage*.py）写死了 1920x1080 绝对像素。
用户机器是 1080p 桌面 + NVIDIA DSR / 游戏内 2160p 渲染，当前能点中是因为游戏
窗口合成后仍是 1920x1080；一旦换 4K 窗口或改渲染倍率，硬编码必然点空。

正式代码本来就是比例口径（`_frac_point` / `region_frac` / 模板多尺度），
本套件锁死三件事：
  1. 探针的 1080p 基准坐标经 `game_auto.runtime.ui_coords.screen` 按窗口 rect 等比缩放；
  2. 实机量到的正确坐标以**窗口比例**写进两份 flow_configs，不写像素；
  3. 暖暖「晶石兑换」点图标的偏移按 OCR 文字框高（随分辨率缩放），不是 42px。

跑法：.venv\\Scripts\\python.exe tests\\suite_ui_coords_scaling_20261106.py
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.nikki_calendar as nc  # noqa: E402
import game_auto.runtime.ui_coords as sc  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def test_screen_scaling():
    print("[1] 1080p 基准坐标按窗口 rect 等比缩放（任意 16:9）")
    cases = [
        ("1080p", (0, 0, 1920, 1080), (1348, 950)),
        ("720p", (0, 0, 1280, 720), (899, 633)),
        ("1440p", (0, 0, 2560, 1440), (1797, 1267)),
        ("2160p/DSR", (0, 0, 3840, 2160), (2696, 1900)),
    ]
    for label, rect, expect in cases:
        got = sc.screen({"rect": rect}, 1348, 950)
        check(f"{label} → {expect}", got == expect, got)
    check("1080p 窗口换算为恒等变换", sc.screen({"rect": (0, 0, 1920, 1080)}, 149, 732) == (149, 732))

    # 窗口不在屏幕原点时叠加 left/top 偏移（多显示器常见）
    got = sc.screen({"rect": (1920, 100, 3840, 1180)}, 1348, 950)
    check("副屏窗口叠加 left/top", got == (1920 + 1348, 100 + 950), got)

    # 16:10 等非常规比例：宽高独立缩放，不假设 16:9
    got = sc.screen({"rect": (0, 0, 1680, 1050)}, 960, 540)
    check("非 16:9 宽高独立缩放", got == (840, 525), got)


def test_helpers():
    print("[2] 相对偏移 / 底部按钮阈值也随窗口缩放")
    w1080 = {"rect": (0, 0, 1920, 1080)}
    w2160 = {"rect": (0, 0, 3840, 2160)}
    check("1080p 下 40px 偏移不变", sc.offset(w1080, 40, 0)[0] == 40)
    check("2160p 下 40px 偏移翻倍", sc.offset(w2160, 40, 0)[0] == 80)
    check("1080p 底部按钮阈值 = 700", abs(sc.min_confirm_y(w1080) - 700) < 1,
          sc.min_confirm_y(w1080))
    check("2160p 底部按钮阈值 = 1400", abs(sc.min_confirm_y(w2160) - 1400) < 1,
          sc.min_confirm_y(w2160))
    check("y_ratio(700) 与阈值常量一致",
          abs(sc.y_ratio(700) - sc.CONFIRM_MIN_Y_RATIO) < 1e-9)
    check("窗口四元组也能吃", sc.screen((0, 0, 3840, 2160), 100, 100) == (200, 200))


def test_config_is_ratio():
    print("[3] 实机命中点以窗口比例写进两份 flow_configs")
    cfgs = {}
    for name in ("flow_configs.json", "flow_configs.example.json"):
        cfgs[name] = json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))
    a, b = cfgs["flow_configs.json"], cfgs["flow_configs.example.json"]
    check("两份 ZZZ_MONTHLY 完全一致", a["ZZZ_MONTHLY"] == b["ZZZ_MONTHLY"])
    frac = a["ZZZ_MONTHLY"]["steps"][2]["shop_entry_point_frac"]
    check("比例都在 (0,1) 区间", all(0 < v < 1 for v in frac), frac)
    check("换算回 1920x1080 = 实机命中点 (1348,950)",
          abs(frac[0] * 1920 - 1348) < 3 and abs(frac[1] * 1080 - 950) < 3,
          (frac[0] * 1920, frac[1] * 1080))
    check("不再使用 11-05 点偏的 (1376,970)",
          abs(frac[0] * 1920 - 1376) >= 3, frac)
    # ui_coords 与该比例换算结果一致
    got = sc.screen({"rect": (0, 0, 1920, 1080)}, frac[0] * 1920, frac[1] * 1080)
    check("配置比例 → screen() 一致", abs(got[0] - 1348) < 3 and abs(got[1] - 950) < 3, got)


def test_nikki_exchange_icon():
    print("[4] 暖暖「晶石兑换」点文字上方图标，偏移随文字框高缩放")
    rect = (0, 0, 1920, 1080)
    entry = {"text": "晶石兑换", "center": (149, 774), "box": (100, 755, 198, 793)}
    got = nc._exchange_entry_point(entry, rect)
    check("1080p 文字框高 38 → 上移约 42px 命中 (149,732)",
          got[0] == 149 and abs(got[1] - 732) <= 3, got)

    # 4K 窗口：OCR 框高与文字中心同比放大，偏移也应同比放大
    big = {"text": "晶石兑换", "center": (298, 1548), "box": (200, 1510, 396, 1586)}
    got4k = nc._exchange_entry_point(big, (0, 0, 3840, 2160))
    check("4K 窗口偏移同比放大（约 -84px）",
          got4k[0] == 298 and abs(got4k[1] - 1464) <= 6, got4k)

    # box 缺失 → 退窗高比例，不是写死 42px
    bare = {"text": "晶石兑换", "center": (149, 774)}
    p1080 = nc._exchange_entry_point(bare, rect)
    p4k = nc._exchange_entry_point({"text": "晶石兑换", "center": (298, 1548)},
                                   (0, 0, 3840, 2160))
    check("box 缺失时 1080p 退窗高比例", abs(p1080[1] - 732) <= 2, p1080)
    check("box 缺失时 4K 退比例（偏移翻倍）", abs(p4k[1] - 1464) <= 3, p4k)

    src = Path(nc.__file__).read_text(encoding="utf-8")
    check("入口调用改为 _exchange_entry_point，不再直接点文字中心",
          "point = _exchange_entry_point(entry, rect)" in src
          and "_click(point)" in src)
    check("偏移常量是比例而非像素",
          nc.EXCHANGE_ICON_DY_BOX_RATIO < 0 and nc.EXCHANGE_ICON_DY_WINDOW_RATIO < 0)


def test_probes_route_through_scaling():
    print("[5] 实机探针不再直接点 1080p 硬编码像素")
    base = PROJECT_ROOT / "data" / "artifacts"
    check("共享换算模块入库（不放被 gitignore 的 data/）",
          (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "ui_coords.py").exists())
    zzz = (base / "zzz_stage1.py").read_text(encoding="utf-8")
    nte = (base / "nte_stage1.py").read_text(encoding="utf-8")
    nikki = (base / "nikki_stage2.py").read_text(encoding="utf-8")
    check("绝区零 24HOUR 走 click_ref", "click_ref(win, 1348, 950" in zzz)
    check("绝区零底部按钮阈值走 min_confirm_y", "sc.min_confirm_y(win)" in zzz)
    check("异环底部按钮阈值走 min_confirm_y", "sc.min_confirm_y(win)" in nte)
    check("暖暖 ±40px 微调走 offset", "sc.offset(win, 40, 0)" in nikki)
    check("三份探针都导入 ui_coords",
          all("from game_auto.runtime import ui_coords as sc" in s for s in (zzz, nte, nikki)))
    check("没有残留 y > 700 的硬编码过滤",
          all("> 700" not in s for s in (zzz, nte)))
    check("click()/click_at() 仍吃屏幕坐标（OCR center 不二次缩放）",
          "OCR" in zzz and "禁止再缩放" in nikki)


def main():
    test_screen_scaling()
    test_helpers()
    test_config_is_ratio()
    test_nikki_exchange_icon()
    test_probes_route_through_scaling()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
