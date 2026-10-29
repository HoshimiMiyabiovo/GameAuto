"""一次性迁移：把根目录运行时数据收进 data/（2026-10-29）。

迁移内容：
    logs/                      → data/logs/
    artifacts/                 → data/artifacts/
    workflow.sqlite3(+wal/shm) → data/workflow.sqlite3
    storage/cache/             → data/cache/

安全约束：
- 检测到 game_auto 的 GUI/CLI 仍在运行会拒绝执行（Windows 会锁住日志/DB）；
- 目标已存在会拒绝，避免覆盖；
- 任一移动失败自动回滚已移动的项；
- 迁移后修正 app_config 的 log.dir 与本地 artifacts 脚本里的相对路径。

用法：关掉 GUI/CLI 后，用项目 venv 执行：
    .venv\\Scripts\\python.exe tools\\migrate_data_layout_20261029.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

DATA = ROOT / "data"

MOVES = (
    (ROOT / "logs", DATA / "logs"),
    (ROOT / "artifacts", DATA / "artifacts"),
    (ROOT / "workflow.sqlite3", DATA / "workflow.sqlite3"),
    (ROOT / "workflow.sqlite3-wal", DATA / "workflow.sqlite3-wal"),
    (ROOT / "workflow.sqlite3-shm", DATA / "workflow.sqlite3-shm"),
    (ROOT / "storage" / "cache", DATA / "cache"),
)


def running_app_pids() -> list[int]:
    """返回仍在使用本项目（game_auto）的 Python 进程 PID。"""
    import psutil

    me = os.getpid()
    excluded = {me}
    try:
        excluded.update(parent.pid for parent in psutil.Process(me).parents())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    found: list[int] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if proc.pid in excluded:
            continue
        try:
            name = str(proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not name.startswith("python"):
            continue
        if "game_auto" in cmdline or "app.gui" in cmdline or "app.cli" in cmdline:
            found.append(int(proc.pid))
    return found


def patch_artifact_scripts() -> int:
    """修正 data/artifacts 下真机脚本的仓库根路径与截图引用。"""
    base = DATA / "artifacts"
    if not base.is_dir():
        return 0
    fixed = 0
    for path in base.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new = text.replace(
            "Path(__file__).resolve().parent.parent",
            "Path(__file__).resolve().parents[2]",
        ).replace(
            'PROJECT_ROOT / "artifacts"',
            'PROJECT_ROOT / "data" / "artifacts"',
        )
        if new != text:
            path.write_text(new, encoding="utf-8")
            fixed += 1
    return fixed


def patch_tests() -> int:
    fixed = 0
    path = ROOT / "tests" / "suite_baijing_activity_panel_20261027.py"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        new = text.replace(
            'PROJECT_ROOT / "artifacts"',
            'PROJECT_ROOT / "data" / "artifacts"',
        )
        if new != text:
            path.write_text(new, encoding="utf-8")
            fixed += 1
    return fixed


def update_log_config() -> bool:
    from game_auto.settings import manager as cfg_manager

    cfg = cfg_manager.load()
    log_cfg = cfg.get("log") if isinstance(cfg.get("log"), dict) else {}
    current = str(log_cfg.get("dir") or "").replace("\\", "/").strip("/")
    if current == "logs":
        log_cfg["dir"] = "data/logs"
        cfg["log"] = log_cfg
        cfg_manager.save(cfg)
        return True
    return False


def main() -> int:
    pids = running_app_pids()
    if pids:
        print(f"[中止] 检测到 game_auto 仍在运行（PID: {pids}），请先关闭 GUI/CLI 再迁移。")
        return 1

    pending = [(src, dst) for src, dst in MOVES if src.exists()]
    for src, dst in pending:
        if dst.exists():
            print(f"[中止] 目标已存在，拒绝覆盖: {dst.relative_to(ROOT)}（来源 {src.relative_to(ROOT)}）")
            return 1

    moved: list[tuple[Path, Path]] = []
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        for src, dst in pending:
            dst.parent.mkdir(parents=True, exist_ok=True)
            print(f"[移动] {src.relative_to(ROOT)} → {dst.relative_to(ROOT)}")
            shutil.move(str(src), str(dst))
            moved.append((src, dst))
    except Exception as exc:
        print(f"[失败] {exc}，正在回滚…")
        for src, dst in reversed(moved):
            try:
                shutil.move(str(dst), str(src))
            except Exception as rollback_exc:
                print(f"  [回滚失败] {dst} → {src}: {rollback_exc}")
        return 1

    if update_log_config():
        print("[配置] app_config.log.dir: logs → data/logs")
    scripts = patch_artifact_scripts()
    tests = patch_tests()
    print(f"[修正] 本地脚本 {scripts} 个、测试 {tests} 个")

    stale_db = ROOT / "storage" / "workflow.sqlite3"
    if stale_db.is_file() and stale_db.stat().st_size == 0:
        stale_db.unlink()
        print("[清理] 删除 storage/workflow.sqlite3（0 字节占位）")

    print("\n完成。当前根目录可见文件夹：")
    print("  " + ", ".join(sorted(p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith("."))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
