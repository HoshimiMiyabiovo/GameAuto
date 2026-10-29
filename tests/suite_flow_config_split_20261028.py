# -*- coding: utf-8 -*-
"""flow_configs 本地/示例拆分回归（2026-10-28）。

背景：flow_configs.json 同时含共享流程定义和 GUI 写入的个人开关
（enabled / close_process_on_finish），提交个人设置会污染仓库。
拆分后：
- flow_configs.example.json 入库，放默认流程定义和安全默认值；
- flow_configs.json 本地文件，.gitignore 忽略；
- 首次运行本地缺失时 ensure_flow_config_path() 从 example 复制；
- UI/WebUI 只写本地文件。
"""

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

from game_auto.settings import paths as p  # noqa: E402
import game_auto.runtime.foreground_flow_runner as fg  # noqa: E402
from game_auto.ui.bridge import UiBridge  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_ensure_copy_and_preserve():
    print("[1] ensure_flow_config_path：缺失复制 / 已有不覆盖")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        local = td / "flow_configs.json"
        example = td / "flow_configs.example.json"
        example.write_text(json.dumps({"XM": {"enabled": True}}, ensure_ascii=False), encoding="utf-8")
        old = (p.FLOW_CONFIG_PATH, p.FLOW_CONFIG_EXAMPLE_PATH)
        p.FLOW_CONFIG_PATH, p.FLOW_CONFIG_EXAMPLE_PATH = local, example
        try:
            got = p.ensure_flow_config_path()
            check("缺失时复制 example", got == local and local.is_file())
            check("复制内容正确", json.loads(local.read_text(encoding="utf-8")) == {"XM": {"enabled": True}})
            local.write_text(json.dumps({"XM": {"enabled": False}}, ensure_ascii=False), encoding="utf-8")
            p.ensure_flow_config_path()
            check("已有本地文件不被覆盖",
                  json.loads(local.read_text(encoding="utf-8")) == {"XM": {"enabled": False}})
        finally:
            p.FLOW_CONFIG_PATH, p.FLOW_CONFIG_EXAMPLE_PATH = old


def test_loader_uses_ensure():
    print("[2] 流程加载器：本地缺失时从 example 复制并读取")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        local = td / "flow_configs.json"
        example = td / "flow_configs.example.json"
        example.write_text(
            json.dumps({"XM": {"enabled": True, "close_process_on_finish": False}},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        old = (p.FLOW_CONFIG_PATH, p.FLOW_CONFIG_EXAMPLE_PATH)
        p.FLOW_CONFIG_PATH, p.FLOW_CONFIG_EXAMPLE_PATH = local, example
        try:
            cfg = fg._load_flow_config("XM")
            check("加载器读到 example 内容", cfg and cfg.get("enabled") is True, cfg)
            check("加载后本地文件已生成", local.is_file())
        finally:
            p.FLOW_CONFIG_PATH, p.FLOW_CONFIG_EXAMPLE_PATH = old


def test_bridge_write_local_only():
    print("[3] GUI 写开关只写本地文件")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        local = td / "flow_configs.json"
        example = td / "flow_configs.example.json"
        example.write_text(
            json.dumps({"WORKBUDDY": {"enabled": True, "close_process_on_finish": False}},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        old = (p.FLOW_CONFIG_PATH, p.FLOW_CONFIG_EXAMPLE_PATH)
        p.FLOW_CONFIG_PATH, p.FLOW_CONFIG_EXAMPLE_PATH = local, example
        try:
            bridge = UiBridge.__new__(UiBridge)
            bridge.emit_event = lambda *a, **k: None
            bridge._save_wool_flow_field("WORKBUDDY", "close_process_on_finish", True, "测试")
            data = json.loads(local.read_text(encoding="utf-8"))
            check("本地配置写入成功", data["WORKBUDDY"]["close_process_on_finish"] is True, data)
            check("example 未被修改",
                  json.loads(example.read_text(encoding="utf-8"))["WORKBUDDY"]["close_process_on_finish"] is False)
        finally:
            p.FLOW_CONFIG_PATH, p.FLOW_CONFIG_EXAMPLE_PATH = old


def test_repo_files():
    print("[4] 仓库拆分文件/接线")
    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    example = PROJECT_ROOT / "flow_configs.example.json"
    check(".gitignore 忽略本地 flow_configs.json", "flow_configs.json" in ignore.splitlines())
    check("example 已入库", example.is_file())
    if example.is_file():
        data = json.loads(example.read_text(encoding="utf-8"))
        check("example 含共享流程", len(data) >= 10, len(data))
        check("example 的 WORKBUDDY 安全默认",
              (data.get("WORKBUDDY") or {}).get("close_process_on_finish") is False)
        check("example 的 QODER 安全默认",
              (data.get("QODER") or {}).get("close_process_on_finish") is False)
    runner = (PROJECT_ROOT / "runtime" / "foreground_flow_runner.py").read_text(encoding="utf-8")
    bridge = (PROJECT_ROOT / "ui" / "bridge.py").read_text(encoding="utf-8")
    webui = (PROJECT_ROOT / "webui" / "api" / "wool.py").read_text(encoding="utf-8")
    check("流程加载器使用 ensure", "ensure_flow_config_path()" in runner)
    check("GUI 写开关使用 ensure", "ensure_flow_config_path()" in bridge)
    check("WebUI 写开关使用 ensure", "ensure_flow_config_path()" in webui)


def main():
    test_ensure_copy_and_preserve()
    test_loader_uses_ensure()
    test_bridge_write_local_only()
    test_repo_files()
    if FAILURES:
        print(f"\nFAILED ({len(FAILURES)}): {FAILURES}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
