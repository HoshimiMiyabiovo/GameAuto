#!/usr/bin/env python3
"""Prepare the ok-ww module runtime used by game_auto.

What it does
------------
1. Clone (or update) ok-ww source at a pinned tag into ``data/okww/src``.
2. Point ``data/okww/src/configs`` at the existing ok-ww installation's
   ``working/configs`` via a Windows directory junction, so custom teams,
   hotkeys and task configs stay in one place.
3. Validate the bundled Python and print the next steps.

Run with the project venv::

    .venv\\Scripts\\python.exe tools\\okww_module\\setup_runtime.py --tag v3.7.17

The script only touches the repository's ignored ``data/`` directory and the
junction under it.  It never modifies the ok-ww installation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_CONFIG = PROJECT_ROOT / "app_config.json"
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "data" / "okww" / "src"
CNB_MIRROR = "https://cnb.cool/ok-oldking/ok-ww-update2.git"


def _app_config() -> dict:
    if not APP_CONFIG.is_file():
        return {}
    try:
        return json.loads(APP_CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[setup] app_config.json read failed: {exc}")
        return {}


def _detect_install_root() -> Path:
    config = _app_config()
    wuwa = (config.get("apps") or {}).get("WutheringWaves") or {}
    discovery = wuwa.get("_discovery") or {}
    script_exe = str(discovery.get("script_exe_path") or "").strip()
    if not script_exe:
        return Path()
    return Path(script_exe).resolve().parent


def _detect_app_dir(install_root: Path) -> Path:
    direct = install_root / "data" / "apps" / "ok-ww"
    if (direct / "python" / "python.exe").is_file():
        return direct
    apps_dir = install_root / "data" / "apps"
    if apps_dir.is_dir():
        for child in apps_dir.iterdir():
            if (child / "python" / "python.exe").is_file():
                return child
    return direct


def _run(command: list[str], cwd: Path | None = None) -> None:
    printable = " ".join(str(part) for part in command)
    print(f"[setup] $ {printable}")
    subprocess.run(command, cwd=str(cwd) if cwd else None, check=True)


def _clone_or_update(runtime_root: Path, tag: str, mirror: str, force: bool) -> None:
    if runtime_root.exists() and any(runtime_root.iterdir()):
        if (runtime_root / ".git").is_dir():
            _run(
                ["git", "-c", "http.proxy=", "-c", "https.proxy=", "fetch", "--depth", "1", "origin", f"refs/tags/{tag}"],
                cwd=runtime_root,
            )
            _run(["git", "checkout", "--force", tag], cwd=runtime_root)
            return
        if not force:
            raise SystemExit(
                f"[setup] {runtime_root} exists but is not a git clone; "
                "move it away or pass --force to replace it"
            )
        shutil.rmtree(runtime_root)

    runtime_root.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "git",
            "-c",
            "http.proxy=",
            "-c",
            "https.proxy=",
            "clone",
            "--depth",
            "1",
            "--branch",
            tag,
            mirror,
            str(runtime_root),
        ]
    )


def _ensure_junction(link: Path, target: Path, force: bool, label: str) -> None:
    if link.exists() or link.is_symlink():
        is_junction = getattr(link, "is_junction", lambda: False)()
        if is_junction or link.is_symlink():
            current = Path(os.path.realpath(link))
            if current.resolve() == target.resolve():
                print(f"[setup] {label} junction ok -> {current}")
                return
            if not force:
                raise SystemExit(
                    f"[setup] {label} points to {current}, expected {target}; "
                    "pass --force to replace it"
                )
            link.unlink()
        else:
            if not force:
                raise SystemExit(
                    f"[setup] {link} is a real directory; move it away or pass --force"
                )
            shutil.rmtree(link)

    link.parent.mkdir(parents=True, exist_ok=True)
    print(f"[setup] creating junction {link} -> {target}")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"[setup] mklink failed: {result.stdout} {result.stderr}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare ok-ww module runtime")
    parser.add_argument("--tag", default="v3.7.17", help="ok-ww tag to pin")
    parser.add_argument("--mirror", default=CNB_MIRROR, help="git update source")
    parser.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    parser.add_argument("--install-root", default="", help="ok-ww install root (contains ok-ww.exe)")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-clone", action="store_true")
    args = parser.parse_args()

    runtime_root = Path(args.runtime_root).resolve()
    install_root = Path(args.install_root).resolve() if args.install_root else _detect_install_root()
    if not install_root or not install_root.is_dir():
        raise SystemExit(
            "[setup] ok-ww install root not found; pass --install-root "
            "(the directory containing ok-ww.exe)"
        )

    app_dir = _detect_app_dir(install_root)
    python_exe = app_dir / "python" / "python.exe"
    install_configs = app_dir / "working" / "configs"
    install_logs = app_dir / "working" / "logs"
    if not python_exe.is_file():
        raise SystemExit(f"[setup] bundled python not found: {python_exe}")
    if not install_configs.is_dir():
        raise SystemExit(f"[setup] ok-ww configs not found: {install_configs}")
    if not install_logs.is_dir():
        raise SystemExit(f"[setup] ok-ww logs not found: {install_logs}")

    if not args.skip_clone:
        _clone_or_update(runtime_root, args.tag, args.mirror, args.force)
    if not (runtime_root / "main.py").is_file():
        raise SystemExit(f"[setup] runtime is incomplete: {runtime_root / 'main.py'} missing")

    _ensure_junction(runtime_root / "configs", install_configs, args.force, "configs")
    _ensure_junction(runtime_root / "logs", install_logs, args.force, "logs")

    version = subprocess.run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        cwd=str(runtime_root),
        capture_output=True,
        text=True,
    ).stdout.strip()
    print()
    print("[setup] ready")
    print(f"  runtime : {runtime_root} ({version or 'unknown tag'})")
    print(f"  python  : {python_exe}")
    print(f"  configs : {runtime_root / 'configs'} -> {install_configs}")
    print()
    print("[setup] next:")
    print("  1. copy tools/okww_module/humanize.example.json to data/okww/humanize.json")
    print("  2. run tests/suite_okww_module_20261104.py")
    print("  3. set app_config.json apps.WutheringWaves.module_mode.enabled=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
