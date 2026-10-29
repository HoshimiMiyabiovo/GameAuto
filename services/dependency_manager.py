"""启动依赖检查（只检查，不安装）。

依赖清单唯一来源是仓库根目录的 requirements.txt：
- 本模块运行时解析它，得到「import 名 -> pip 包名」映射做可导入性检查；
- 新增依赖只需改 requirements.txt（符合 AGENTS.md 的依赖规则），
  不再维护第二份硬编码清单；
- 真正安装由 setup_deps.bat 以普通权限执行（uv + requirements.lock，
  --no-deps 精确回放，避免 CPU 版 onnxruntime 覆盖 GPU 版）。GUI/CLI 启动
  路径一律不再 pip install —— 历史行为是提权进程里自动装包，攻击面过大。

注意: 不要在模块顶层 import ..settings / ..settings.paths, 它们会顺带拉起
settings.discovery → runtime.process_supervisor, 后者依赖 psutil 等本模块要检查的包,
会导致 import 阶段就先于 run_full_dependency_check 触发 ModuleNotFoundError。
所有需要 APPS / QBITTORRENT / TESSERACT_EXE 的位置都改成函数内 import。
"""

import importlib.util
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

REQUIREMENTS_FILE = Path(__file__).resolve().parent.parent / "requirements.txt"

# pip 包名 -> import 名；未列出的按 PEP 503 规范化（点/横线转下划线、小写）。
# 只放“包名与 import 名不同”的例外，其余自动推导。
IMPORT_NAME_BY_PACKAGE: Dict[str, str] = {
    "pyside6": "PySide6",
    "pywin32": "win32gui",
    "pillow": "PIL",
    "opencv-python": "cv2",
    "pycryptodome": "Crypto",
    "browser-cookie3": "browser_cookie3",
    "windows-capture": "windows_capture",
    "rapidocr-onnxruntime": "rapidocr_onnxruntime",
    "onnxruntime-gpu": "onnxruntime",
    "ruamel.yaml": "ruamel",
}

# requirements.txt 不可读时的兜底（打包/裁剪环境）。正常源码运行不会走到。
_FALLBACK_PIP_PACKAGES: Tuple[str, ...] = (
    "psutil", "pyautogui", "pytesseract", "pywin32", "opencv-python", "Pillow",
    "flet", "PySide6", "pyhuskarui", "browser-cookie3", "qrcode", "openpyxl",
    "rsa", "genshin", "pycryptodome", "pynput", "fastapi", "uvicorn", "websockets",
    "pydantic", "rapidocr-onnxruntime", "onnxruntime-gpu", "windows-capture", "numpy",
)


@dataclass
class DependencyCheckReport:
    ok: bool = True
    installed_packages: List[str] = field(default_factory=list)
    failed_packages: List[str] = field(default_factory=list)
    missing_packages: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)

    def add_warning(self, msg: str):
        self.warnings.append(msg)

    def add_error(self, msg: str):
        self.errors.append(msg)
        self.ok = False

    def add_info(self, msg: str):
        self.info.append(msg)


def _parse_requirement_name(line: str) -> str:
    """从一行 requirements 里提取分发名（去注释/extras/版本/环境标记）。"""
    text = line.split("#", 1)[0].strip()
    if not text:
        return ""
    text = text.split(";", 1)[0].strip()
    text = re.sub(r"\[.*?\]", "", text)
    name = re.split(r"[<>=!~ ]", text, maxsplit=1)[0].strip()
    return name


def load_requirement_packages() -> List[str]:
    """读取 requirements.txt 的分发名列表；文件缺失时返回兜底清单。"""
    try:
        lines = REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return list(_FALLBACK_PIP_PACKAGES)
    packages = [_parse_requirement_name(line) for line in lines]
    return [p for p in packages if p]


def _import_name_for(package: str) -> str:
    return IMPORT_NAME_BY_PACKAGE.get(package.lower(), re.sub(r"[-.]+", "_", package.lower()))


def build_required_packages() -> Dict[str, str]:
    """构建 import 名 -> pip 包名 映射（供检查与测试共用）。"""
    mapping: Dict[str, str] = {}
    for package in load_requirement_packages():
        mapping[_import_name_for(package)] = package
    return mapping


REQUIRED_PYTHON_PACKAGES: Dict[str, str] = build_required_packages()


def _is_module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _collect_app_paths() -> List[Tuple[str, str, str]]:
    # 延迟导入: settings 在被 import 时会触发 discovery / process_supervisor 的副作用链,
    # 那条链需要 psutil 等本模块正在检查的依赖。函数内 import 避开循环依赖。
    from ..settings import APPS, QBITTORRENT

    paths: List[Tuple[str, str, str]] = []
    if QBITTORRENT.get("path"):
        paths.append(("系统组件", "qBittorrent", QBITTORRENT["path"]))
    for app_key, cfg in APPS.items():
        if "launcher" in cfg:
            paths.append((app_key, "launcher", cfg["launcher"]))
        for opt in cfg.get("launcher_options", []):
            if opt.get("launcher"):
                paths.append((app_key, f"launcher_option:{opt.get('name', 'unknown')}", opt["launcher"]))
    return paths


# --list-langs 子进程约 0.3s，按 exe 路径+mtime 缓存，进程内只跑一次
_TESS_LANGS_CACHE: Dict[str, tuple] = {}


def _check_tesseract_capability(report: DependencyCheckReport):
    # 延迟导入: 同 _collect_app_paths 的原因
    from ..settings.paths import TESSERACT_EXE

    local_tesseract = str(TESSERACT_EXE)
    tesseract_bin = local_tesseract if os.path.exists(local_tesseract) else shutil.which("tesseract")
    if tesseract_bin is None:
        report.add_warning("未检测到 tesseract.exe，OCR识别点击功能可能不可用")
        return
    report.add_info(f"Tesseract: {tesseract_bin}")
    try:
        mtime = os.path.getmtime(tesseract_bin)
    except OSError:
        mtime = -1.0
    cached = _TESS_LANGS_CACHE.get(tesseract_bin)
    if cached is not None and cached[0] == mtime:
        has_chi_sim = cached[1]
    else:
        try:
            result = subprocess.run([tesseract_bin, "--list-langs"], capture_output=True, text=True, check=False)
            langs = (result.stdout or "").lower()
            has_chi_sim = "chi_sim" in langs
        except Exception as e:
            report.add_warning(f"无法检测 Tesseract 语言包: {str(e)}")
            return
        _TESS_LANGS_CACHE[tesseract_bin] = (mtime, has_chi_sim)
    if not has_chi_sim:
        report.add_warning("Tesseract 未检测到 chi_sim 中文语言包，中文识别准确率会很低")


def _check_external_paths(report: DependencyCheckReport):
    for owner, role, path in _collect_app_paths():
        if not os.path.exists(path):
            report.add_warning(f"[路径缺失] {owner} {role}: {path}")


def _check_runtime_environment(report: DependencyCheckReport):
    report.add_info(f"Python: {sys.version.split()[0]}")
    report.add_info(f"Platform: {platform.platform()}")
    report.add_info(f"Executable: {sys.executable}")
    if sys.version_info < (3, 10):
        report.add_error("Python 版本过低，需要 3.10+（requirements 与代码语法均按此基线）")


def run_full_dependency_check(logger: logging.Logger) -> DependencyCheckReport:
    """检查 Python 包 / Tesseract / 外部程序路径 / 运行环境。

    只做检查，不安装：缺失包记录到 report.missing_packages 与 errors，
    调用方（GUI/CLI）据此提示用户以普通权限运行 setup_deps.bat。
    """
    report = DependencyCheckReport()
    logger.info("开始执行依赖检测（只检查，不安装）")

    _check_runtime_environment(report)

    for import_name, pip_name in REQUIRED_PYTHON_PACKAGES.items():
        if not _is_module_available(import_name):
            report.missing_packages.append(pip_name)
            report.add_error(f"缺少 Python 包: {pip_name} (import: {import_name})")

    if report.missing_packages:
        report.add_error(
            "依赖不完整：请以普通权限运行 setup_deps.bat 安装（uv + requirements.lock），"
            "完成后重启本程序"
        )

    _check_tesseract_capability(report)
    _check_external_paths(report)

    for item in report.info:
        logger.info(item)
    for item in report.warnings:
        logger.warning(item)
    for item in report.errors:
        logger.error(item)

    if report.ok:
        logger.info("依赖检测通过")
    else:
        logger.error("依赖检测未通过（不自动安装，请运行 setup_deps.bat）")
    return report
