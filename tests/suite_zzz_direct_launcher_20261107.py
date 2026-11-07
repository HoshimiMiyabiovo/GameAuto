# -*- coding: utf-8 -*-
"""绝区零直连 OneDragon 选项回归（2026-11-07）。

背景：2026-11-07 把 ZZZ 日常一条龙从 ScriptChainer 中间壳改为直拉
OneDragon-Launcher.exe -o -c（OneDragon 原生 CLI 一条龙模式）。首次只改了
本机 app_config.json（gitignore 不入库），换机 clone 后 games/zzz.py 的
default_app 模板里没有这个选项 → GUI 下拉只有 Bat/ScriptChainer 两项，
左右脑不一致。本 suite 锁住「模板层也有 OneDragonDirect」，防回退。

覆盖：
1. games/zzz.py default_app.launcher_options 含 OneDragonDirect，结构完整
   （type=normal、params=[-o,-c]、script_process_name=pythonw.exe）；
2. pythonw.exe 在 automation_controller.WRAPPER_PROCESS_NAMES（收尾按包装器
   处理，不误杀其他 pythonw）；
3. -o / -c 是 OneDragon 原生参数（exe_launcher.py 的 argparse 定义）；
4. 本机 app_config.json（若存在）的 launcher_options 顺序与模板一致，
   防止 _merge_defaults 列表整体覆盖后顺序分叉。

跑法：.venv\\Scripts\\python.exe tests\\suite_zzz_direct_launcher_20261107.py
"""

import copy
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_template_has_direct_option():
    print("[1] games/zzz.py default_app 模板含 OneDragonDirect")
    from game_auto.games.zzz import PROFILE

    opts = {o["name"]: o for o in PROFILE.default_app["launcher_options"]}
    check("模板含 OneDragonBat（原有）", "OneDragonBat" in opts)
    check("模板含 OneDragonScriptChainer（原有）", "OneDragonScriptChainer" in opts)
    check("模板含 OneDragonDirect（新增）", "OneDragonDirect" in opts, list(opts))
    if "OneDragonDirect" not in opts:
        return
    d = opts["OneDragonDirect"]
    check("Direct type=normal", d.get("type") == "normal", d.get("type"))
    check("Direct params=[-o, -c]", d.get("launcher_params") == ["-o", "-c"],
          d.get("launcher_params"))
    check("Direct script_process_name=pythonw.exe",
          d.get("script_process_name") == "pythonw.exe", d.get("script_process_name"))


def test_pythonw_is_wrapper():
    print("[2] pythonw.exe 是包装器进程（收尾不按名误杀）")
    from game_auto.runtime.automation_controller import WRAPPER_PROCESS_NAMES

    check("pythonw.exe ∈ WRAPPER_PROCESS_NAMES", "pythonw.exe" in WRAPPER_PROCESS_NAMES,
          sorted(WRAPPER_PROCESS_NAMES))


def test_onedragon_native_args():
    print("[3] -o / -c 是 OneDragon 原生 CLI 参数")
    root = Path("D:/down/ZZZ/ZenlessZoneZero-OneDragon/src/one_dragon/launcher")
    exe_launcher = root / "exe_launcher.py"
    launcher_base = root / "launcher_base.py"
    if not exe_launcher.exists():
        print("  SKIP 本机无 OneDragon 源码（换机环境），只锁本项目侧")
        return
    text = (exe_launcher.read_text(encoding="utf-8")
            + launcher_base.read_text(encoding="utf-8"))
    check("exe_launcher 定义 -o/--onedragon", '"--onedragon"' in text and '"-o"' in text)
    check("launcher_base 定义 -c/--close-game", '"--close-game"' in text and '"-c"' in text)


def test_local_config_order_matches_template():
    print("[4] 本机 app_config.json 与模板顺序一致（若本机文件存在）")
    from game_auto.games.zzz import PROFILE

    cfg_path = PROJECT_ROOT / "app_config.json"
    if not cfg_path.exists():
        print("  SKIP 本机无 app_config.json（干净 clone 场景）")
        return
    local = json.loads(cfg_path.read_text(encoding="utf-8"))
    z = (local.get("apps") or {}).get("ZenlessZoneZero") or {}
    local_names = [o["name"] for o in z.get("launcher_options", [])]
    tmpl_names = [o["name"] for o in PROFILE.default_app["launcher_options"]]
    check("本机选项顺序与模板一致", local_names == tmpl_names,
          f"本机={local_names} 模板={tmpl_names}")
    if "OneDragonDirect" in local_names:
        d = next(o for o in z["launcher_options"] if o["name"] == "OneDragonDirect")
        check("本机 Direct 已填 launcher 路径", bool(d.get("launcher")), d.get("launcher"))


def test_apply_runtime_config_backfills_new_options():
    print("[5] apply_runtime_config 给旧配置补新 launcher_options 项")
    # _merge_defaults 对 list 整体覆盖：旧配置只有 2 项时模板新增的 Direct 会丢。
    # apply_runtime_config 的按 name 合并必须把它补回来，且不碰用户已填路径。
    import copy

    from game_auto.games.zzz import PROFILE
    from game_auto.settings import manager as cm

    old_cfg = copy.deepcopy(cm._default_config())
    old_cfg["apps"]["ZenlessZoneZero"]["launcher_options"] = [
        {"name": "OneDragonBat", "launcher": r"E:\bat.exe", "launcher_params": []},
        {"name": "OneDragonScriptChainer", "launcher": r"E:\chain.exe",
         "launcher_params": ["--chain", "1"]},
    ]
    runtime = cm._merge_defaults(cm._default_config(), old_cfg)
    merged_names = [o["name"] for o in runtime["apps"]["ZenlessZoneZero"]["launcher_options"]]
    check("裸 _merge_defaults 会丢 Direct（复现盲区）",
          "OneDragonDirect" not in merged_names, merged_names)

    # 走 apply_runtime_config 的补项路径（与 settings/__init__.py 同逻辑）
    cfg_apps = runtime["apps"]
    from game_auto.settings import defaults as _d
    for app_key, app_cfg in _d.DEFAULT_APPS.items():
        tmpl_opts = app_cfg.get("launcher_options")
        if not tmpl_opts:
            continue
        cfg_opts = cfg_apps.setdefault(app_key, {}).setdefault("launcher_options", [])
        cfg_names = {o.get("name") for o in cfg_opts if isinstance(o, dict)}
        for tmpl_opt in tmpl_opts:
            if isinstance(tmpl_opt, dict) and tmpl_opt.get("name") not in cfg_names:
                cfg_opts.append(copy.deepcopy(tmpl_opt))
    final = [o["name"] for o in cfg_apps["ZenlessZoneZero"]["launcher_options"]]
    check("补项后 Direct 回来", "OneDragonDirect" in final, final)
    check("旧路径保留（Bat/Chainer 不被模板空 launcher 覆盖）",
          cfg_apps["ZenlessZoneZero"]["launcher_options"][0]["launcher"] == r"E:\bat.exe"
          and cfg_apps["ZenlessZoneZero"]["launcher_options"][1]["launcher"] == r"E:\chain.exe")
    check("不重复补（项数=模板项数）", len(final) == len(PROFILE.default_app["launcher_options"]), final)


def main():
    test_template_has_direct_option()
    test_pythonw_is_wrapper()
    test_onedragon_native_args()
    test_local_config_order_matches_template()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
