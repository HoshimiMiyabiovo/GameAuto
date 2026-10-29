# -*- coding: utf-8 -*-
"""依赖清单单一来源 / 锁文件 / 安装入口 回归（2026-10-29）。

锁定四条不变量：
1. requirements.txt 是唯一人工维护清单，dependency_manager 从它解析，
   不再硬编码第二份包列表；
2. requirements.lock 全量精确锁定，且必须是 onnxruntime-gpu、绝不含 CPU 版
   onnxruntime（否则 RapidOCR 静默退回 CPU）；
3. setup_deps.bat 以 --no-deps 安装锁文件（避免依赖解析带回 CPU onnxruntime），
   启动路径不再 pip install；
4. PySide6 保持 <6.9 上界（6.10 会炸 PyHuskarUI 的 QML）。

跑法：.venv\\Scripts\\python.exe tests\\suite_dependency_manifest_20261029.py
"""

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

from game_auto.services import dependency_manager as dm  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _requirement_names():
    text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        line = line.split(";", 1)[0].strip()
        line = re.sub(r"\[.*?\]", "", line)
        names.append(re.split(r"[<>=!~ ]", line, maxsplit=1)[0].strip())
    return names


def _lock_pins():
    text = (PROJECT_ROOT / "requirements.lock").read_text(encoding="utf-8")
    pins = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        pins[name.strip().lower()] = version.strip()
    return pins


def main():
    print("[1] requirements.txt 与 dependency_manager 单一来源")
    names = _requirement_names()
    check("requirements.txt 非空", len(names) >= 20, str(len(names)))
    check(
        "映射数量等于清单数量（无重复 import 名）",
        len(dm.REQUIRED_PYTHON_PACKAGES) == len(names),
        f"{len(dm.REQUIRED_PYTHON_PACKAGES)} vs {len(names)}",
    )
    mapped_pips = {p.lower() for p in dm.REQUIRED_PYTHON_PACKAGES.values()}
    check("每个 requirement 都被映射", all(n.lower() in mapped_pips for n in names),
          str([n for n in names if n.lower() not in mapped_pips]))
    check("PySide6 import 名大小写正确", dm.REQUIRED_PYTHON_PACKAGES.get("PySide6") == "PySide6")
    check("onnxruntime-gpu 映射到 onnxruntime",
          dm.REQUIRED_PYTHON_PACKAGES.get("onnxruntime") == "onnxruntime-gpu")
    check("win32gui 映射到 pywin32", dm.REQUIRED_PYTHON_PACKAGES.get("win32gui") == "pywin32")

    print("[2] requirements.lock 完整且 GPU 版优先")
    pins = _lock_pins()
    check("锁文件存在且非空", len(pins) >= 50, str(len(pins)))
    check("所有锁行均为 ==", all(v for v in pins.values()))
    check("含 onnxruntime-gpu", "onnxruntime-gpu" in pins)
    check("不含 CPU 版 onnxruntime", "onnxruntime" not in pins)
    lock_names = set(pins)
    missing_in_lock = [n.lower() for n in names if n.lower() not in lock_names]
    check("每个直接依赖都在锁里", not missing_in_lock, str(missing_in_lock))
    check("PySide6 锁定在 6.8.x", pins.get("pyside6", "").startswith("6.8."), pins.get("pyside6", ""))

    print("[3] requirements.txt 的版本约束")
    req_text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    check("PySide6 保留 <6.9 上界", re.search(r"PySide6>=6\.8.*<6\.9", req_text) is not None)
    check("onnxruntime-gpu 在运行时清单中", "onnxruntime-gpu" in req_text)
    check("不声明 CPU 版 onnxruntime", re.search(r"(?m)^onnxruntime\s*[<>=]", req_text) is None)

    print("[4] 安装入口与启动路径")
    bat = (PROJECT_ROOT / "setup_deps.bat").read_text(encoding="utf-8", errors="ignore")
    check("setup_deps.bat 使用 --no-deps", "--no-deps" in bat)
    check("setup_deps.bat 安装 requirements.lock", "requirements.lock" in bat)
    check("setup_deps.bat 提权检查", "net session" in bat)
    check("dependency_manager 不再自动安装", not hasattr(dm, "_install_package"))
    check("dependency_manager 提供 missing_packages",
          "missing_packages" in dm.DependencyCheckReport.__dataclass_fields__)
    gui_src = (PROJECT_ROOT / "app" / "gui.py").read_text(encoding="utf-8")
    cli_src = (PROJECT_ROOT / "app" / "cli.py").read_text(encoding="utf-8")
    check("GUI 启动不自动装包", "auto_install" not in gui_src)
    check("CLI 启动不自动装包", "auto_install" not in cli_src)
    check("GUI 提示 setup_deps.bat", "setup_deps.bat" in gui_src)

    print("[5] YAML 适配依赖（刷本配置第二期）")
    check("requirements 含 ruamel.yaml", "ruamel.yaml" in req_text)
    check("ruamel 映射 import 名", dm.REQUIRED_PYTHON_PACKAGES.get("ruamel") == "ruamel.yaml")
    check("锁文件含 ruamel.yaml", "ruamel.yaml" in pins)

    print("[6] WebUI 只监听回环")
    webui_src = (PROJECT_ROOT / "webui" / "app.py").read_text(encoding="utf-8")
    check("宿主固定 127.0.0.1", webui_src.count('host="127.0.0.1"') >= 2, str(webui_src.count('host="127.0.0.1"')))
    check("不监听 0.0.0.0", "0.0.0.0" not in webui_src)

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
