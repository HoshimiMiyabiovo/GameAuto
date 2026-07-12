import importlib.util
import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from ..settings import APPS, QBITTORRENT
from ..settings.paths import TESSERACT_EXE


DEFAULT_PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
LOCAL_TESSERACT_EXE = str(TESSERACT_EXE)

# import_name -> pip_package
REQUIRED_PYTHON_PACKAGES: Dict[str, str] = {
    "psutil": "psutil",
    "pyautogui": "pyautogui",
    "pytesseract": "pytesseract",
    "win32gui": "pywin32",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "flet": "flet",
    "PySide6": "PySide6",
    "pyhuskarui": "pyhuskarui",
    "browser_cookie3": "browser-cookie3",
    "qrcode": "qrcode",
    "openpyxl": "openpyxl",
    "rsa": "rsa",
    "genshin": "genshin",
    "Crypto": "pycryptodome",
}


@dataclass
class DependencyCheckReport:
    ok: bool = True
    installed_packages: List[str] = field(default_factory=list)
    failed_packages: List[str] = field(default_factory=list)
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


def _is_module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _install_package(pkg_name: str, mirror_url: str, logger: logging.Logger) -> Tuple[bool, str]:
    cmds = [
        [sys.executable, "-m", "pip", "install", "-i", mirror_url, pkg_name],
        [sys.executable, "-m", "pip", "install", pkg_name],
    ]
    last_err = ""
    for cmd in cmds:
        try:
            logger.info(f"安装依赖: {pkg_name} ({'镜像源' if '-i' in cmd else '官方源'})")
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode == 0:
                logger.info(f"依赖安装成功: {pkg_name}")
                return True, ""
            logger.warning(f"依赖安装失败: {pkg_name}, code={result.returncode}")
            last_err = (result.stderr or result.stdout or "").strip()[:300]
        except Exception as e:
            logger.warning(f"安装依赖异常: {pkg_name}, error={str(e)}")
            last_err = str(e)
    return False, last_err


def _collect_app_paths() -> List[Tuple[str, str, str]]:
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


def _check_tesseract_capability(report: DependencyCheckReport):
    tesseract_bin = LOCAL_TESSERACT_EXE if os.path.exists(LOCAL_TESSERACT_EXE) else shutil.which("tesseract")
    if tesseract_bin is None:
        report.add_warning("未检测到 tesseract.exe，OCR识别点击功能可能不可用")
        return
    report.add_info(f"Tesseract: {tesseract_bin}")
    try:
        result = subprocess.run([tesseract_bin, "--list-langs"], capture_output=True, text=True, check=False)
        langs = (result.stdout or "").lower()
        if "chi_sim" not in langs:
            report.add_warning("Tesseract 未检测到 chi_sim 中文语言包，中文识别准确率会很低")
    except Exception as e:
        report.add_warning(f"无法检测 Tesseract 语言包: {str(e)}")


def _check_external_paths(report: DependencyCheckReport):
    for owner, role, path in _collect_app_paths():
        if not os.path.exists(path):
            report.add_warning(f"[路径缺失] {owner} {role}: {path}")


def _check_runtime_environment(report: DependencyCheckReport):
    report.add_info(f"Python: {sys.version.split()[0]}")
    report.add_info(f"Platform: {platform.platform()}")
    report.add_info(f"Executable: {sys.executable}")
    if sys.version_info < (3, 9):
        report.add_error("Python 版本过低，建议升级到 3.9+")


def ensure_runtime_dependencies(
    logger: logging.Logger,
    auto_install: bool = True,
    mirror_url: str = DEFAULT_PIP_MIRROR,
) -> Tuple[bool, List[str]]:
    """
    启动时检查依赖并尝试自动安装。
    返回: (是否满足运行, 警告列表)
    """
    report = run_full_dependency_check(logger, auto_install=auto_install, mirror_url=mirror_url)
    issues = report.errors + report.warnings
    return report.ok and not report.errors, issues


def run_full_dependency_check(
    logger: logging.Logger,
    auto_install: bool = True,
    mirror_url: str = DEFAULT_PIP_MIRROR,
) -> DependencyCheckReport:
    """
    全面依赖管理检测：
    - Python 包检测 + 自动安装
    - OCR/Tesseract 能力检测
    - 外部程序路径检测（配置中的 launcher/qBittorrent）
    - 运行环境检测（Python/平台）
    """
    report = DependencyCheckReport()
    logger.info("开始执行全面依赖检测")

    _check_runtime_environment(report)

    missing: List[Tuple[str, str]] = []
    for import_name, pip_name in REQUIRED_PYTHON_PACKAGES.items():
        if not _is_module_available(import_name):
            missing.append((import_name, pip_name))

    if missing and auto_install:
        logger.info(f"检测到缺失 Python 依赖 {len(missing)} 个，开始自动安装")
        for import_name, pip_name in missing:
            ok, err = _install_package(pip_name, mirror_url, logger)
            if ok:
                report.installed_packages.append(pip_name)
            else:
                report.failed_packages.append(pip_name)
                report.add_error(f"安装失败: {pip_name} (import: {import_name}) {err}")

    unresolved = [m for m in REQUIRED_PYTHON_PACKAGES if not _is_module_available(m)]
    if unresolved:
        report.add_error("仍缺少 Python 模块: " + ", ".join(unresolved))

    _check_tesseract_capability(report)
    _check_external_paths(report)

    for item in report.info:
        logger.info(item)
    for item in report.warnings:
        logger.warning(item)
    for item in report.errors:
        logger.error(item)

    if report.ok:
        logger.info("全面依赖检测通过")
    else:
        logger.error("全面依赖检测未通过（存在错误项）")
    return report

