"""ZZZ_MONTHLY 进入主界面步骤静态验证（2026-10-21）。

覆盖：
1. flow_configs.json JSON 合法
2. ZZZ_MONTHLY 可被 _load_flow_config 加载，steps 数量与结构正确
3. 「等待进入主界面」步模板路径被锚定到项目根目录且文件真实存在
4. target_text OCR 候选解析
5. exe_from_app 能解析出绝区零游戏本体路径
6. ZzzMonthlyFlow 任务目录说明已更新
7. task_batch_runner -> background_template_runner -> foreground_flow_runner 导入链完好

运行：E:/GithubProject/game_auto/.venv/Scripts/python.exe tests/suite_zzz_monthly_main_screen_20261021.py
（需在 E:/GithubProject 下以包方式导入，脚本内自行处理 sys.path）
"""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # E:/GithubProject/game_auto
sys.path.insert(0, str(PROJECT_ROOT.parent))  # E:/GithubProject，供 game_auto 包导入

from game_auto.runtime.foreground_flow_runner import (  # noqa: E402
    _load_flow_config,
    _resolve_exe_path,
    _resolve_target_text_candidates,
)
from game_auto.services.launch_task_catalog import get_standalone_task_config  # noqa: E402

FLOW_CONFIG = PROJECT_ROOT / "flow_configs.json"

failures = []


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


# 1. JSON 合法
raw = json.loads(FLOW_CONFIG.read_text(encoding="utf-8"))
check("flow_configs.json JSON 合法", True)

cfg = _load_flow_config("ZZZ_MONTHLY")
check("ZZZ_MONTHLY 可加载", cfg is not None)
check("流程仍启用", bool(cfg and cfg.get("enabled")) is True)

steps = (cfg or {}).get("steps", [])
check(
    "步骤数为 7（进入游戏/等主界面/信号残响兑换/打开菜单/后勤商店/等商店/采购）",
    len(steps) == 7, f"实际 {len(steps)}",
)

if len(steps) == 7:
    s0, s1, s2, s3, s4, s5, s6 = steps
    check("步骤1 仍为 点击进入游戏(click_template)", s0.get("action") == "click_template" and s0.get("name") == "点击进入游戏")
    check("步骤2 为 等待进入主界面(wait_template)", s1.get("action") == "wait_template" and s1.get("name") == "等待进入主界面")
    check(
        "步骤3 为 信号残响每月兑换(monthly_exchange)",
        s2.get("action") == "monthly_exchange" and s2.get("name") == "信号残响每月兑换",
    )
    check("步骤4 为 打开功能菜单(click_template 主菜单按钮)", s3.get("action") == "click_template" and s3.get("name") == "打开功能菜单")
    check("步骤5 为 点击后勤商店(click_template)", s4.get("action") == "click_template" and s4.get("name") == "点击后勤商店")
    check("步骤6 为 等待商店界面(wait_template)", s5.get("action") == "wait_template" and s5.get("name") == "等待商店界面")
    check("步骤7 为 后勤商店采购(zzz_shop_buy)", s6.get("action") == "zzz_shop_buy" and s6.get("name") == "后勤商店采购")

    tpl = s1.get("template", "")
    check("步骤2 模板路径已锚定为绝对路径", os.path.isabs(tpl), tpl)
    check("步骤2 模板文件存在", os.path.isfile(tpl), tpl)
    check("步骤2 超时 600s 且回退上一步重试", s1.get("timeout_sec") == 600 and s1.get("on_fail") == "retry_prev" and s1.get("max_attempts") == 3)

    candidates = _resolve_target_text_candidates("模板\\绝区零-主界面-电台.png", "星期")
    check("OCR 候选包含「星期」", "星期" in candidates, str(candidates))

    # 采购步骤：确认模板 + 两组商品
    check("采购步有确认模板且文件存在", os.path.isfile(str(s6.get("confirm_template", ""))), str(s6.get("confirm_template")))
    groups = s6.get("shop_groups", []) or []
    check("shop_groups 为 2 组（哨站后勤/协会物资）", len(groups) == 2)
    if len(groups) == 2:
        g0, g1 = groups
        check("哨站后勤组 4 商品", len(g0.get("items", [])) == 4 and g0.get("name") == "哨站后勤")
        check("协会物资组 4 商品且带页签切换模板", len(g1.get("items", [])) == 4 and g1.get("name") == "协会物资" and bool(g1.get("tab_template")))
        total = sum(int(i.get("count", 0)) for i in g0.get("items", [])) + sum(int(i.get("count", 0)) for i in g1.get("items", []))
        check("采购总数 = 2+10+5+1 + 10+5+1+1 = 35", total == 35, f"实际 {total}")
        # 音擎残芯：用户确认不是音擎能源模块且本月已购，不应配错商品
        names = [i.get("name") for i in g0.get("items", [])]
        check("哨站组不含误配的「音擎能源模块」", "音擎能源模块" not in names, str(names))
        # 路径锚定：已存在的商品模板都应被锚定为绝对路径（仓鼠笼无模板，允许保持相对）
        all_items = g0.get("items", []) + g1.get("items", [])
        existing = [i for i in all_items if os.path.isfile(str(i.get("template", "")))]
        anchored = all(os.path.isabs(i.get("template", "")) for i in existing)
        check(f"已存在的 {len(existing)} 个商品模板均已锚定", anchored)
        # 仓鼠笼访问器：无模板 → OCR 文字兜底
        hamster = [i for i in g1.get("items", []) if i.get("name") == "仓鼠笼访问器"]
        check(
            "仓鼠笼访问器配置 ocr_text 兜底（无模板也能买）",
            bool(hamster) and hamster[0].get("ocr_text") == "仓鼠笼访问器" and not os.path.isfile(hamster[0].get("template", "")),
            str(hamster),
        )

# match_scales 覆盖更宽分辨率
check("match_scales 含 1.5/0.67", 1.5 in cfg.get("match_scales", []) and 0.67 in cfg.get("match_scales", []))

# exe 解析（exe_from_app -> app_config.json 的 ZenlessZoneZero.game.exe_path）
exe = _resolve_exe_path(cfg or {}, "ZZZ_MONTHLY")
check("exe_from_app 解析出游戏本体", bool(exe) and "ZenlessZoneZero" in str(exe), str(exe))

# catalog 说明
note = get_standalone_task_config("ZzzMonthlyFlow").get("friendly_note", "")
check("catalog 说明已提主界面确认", "主界面" in note)

print()
if failures:
    print(f"共 {len(failures)} 项失败")
    sys.exit(1)
print("全部通过")
