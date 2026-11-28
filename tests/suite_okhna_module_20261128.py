"""2026-11-28：因缘精灵接入 ok-hna（module 模式）回归。

背景：GameAuto 的 NexusAnima 档案此前只配了游戏本体启动，没有接 ok-hna，
日常批次裸启动游戏后干等 30 分钟监控超时（11-28 15:40 那次 16:10:38 强杀，
失败记录 #537）。ok-hna 是独立 ok-script 3.0.13 工程，自带
LauncherTask/DailyActivityTask（启动→日常活跃→福利），这里锁住：
档案默认值、module 模式判定、ok-script 路径解析（无 main.py 的 config.py 根）、
ok-ww 的 humanize 兼容不被 ok-hna 配置污染，以及超时/停滞护栏参数。
"""

import importlib.util
import json
import logging
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_auto.games.nexus_anima import PROFILE  # noqa: E402
from game_auto.runtime.automation_controller import AutomationController  # noqa: E402
from game_auto.runtime.okww_module_session import (  # noqa: E402
    _as_module_config,
    _resolve_paths,
)

LOG = logging.getLogger("okhna_module")
FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {extra}")


def _app_config(probe_root: Path, **module_overrides):
    python_exe = probe_root / "python.exe"
    python_exe.write_bytes(b"stub")
    module = {
        "enabled": True,
        "runtime_name": "ok-hna",
        "task": "DailyActivityTask",
        "timeout": 2400,
        "close_game": True,
        "stall_log_sec": 720,
        "runtime_root": str(probe_root),
        "python_exe": str(python_exe),
        "script_exe": str(python_exe),
        "runner_path": str(PROJECT_ROOT / "tools" / "okww_module" / "runner.py"),
        "humanize_config": "",
        "log_path": str(probe_root / "runner.log"),
        "ready_path": str(probe_root / "ready.json"),
    }
    module.update(module_overrides)
    return {"script_name": "ok-hna", "module_mode": module}


def test_profile():
    print("[1] NexusAnima 档案默认值")
    app = PROFILE.default_app
    check("script_name=ok-hna", app.get("script_name") == "ok-hna", app.get("script_name"))
    module = app.get("module_mode") or {}
    check("module_mode 默认关闭", module.get("enabled") is False, module)
    check("task=DailyActivityTask", module.get("task") == "DailyActivityTask", module.get("task"))
    check("硬超时 2400s", module.get("timeout") == 2400, module.get("timeout"))
    check("停滞熔断 720s", module.get("stall_log_sec") == 720, module.get("stall_log_sec"))
    check("close_game 默认开", module.get("close_game") is True, module.get("close_game"))
    check("require_admin 默认开", module.get("require_admin") is True, module.get("require_admin"))
    check("不写死机器路径", not module.get("runtime_root") and not module.get("python_exe"), module)


def test_module_active():
    print("[2] module 模式只对 ok-ww / ok-hna 生效")
    cfg = {"script_name": "ok-hna", "module_mode": {"enabled": True}}
    check("ok-hna 启用 -> True", AutomationController._module_mode_active(cfg))
    cfg_off = {"script_name": "ok-hna", "module_mode": {"enabled": False}}
    check("ok-hna 关闭 -> False", not AutomationController._module_mode_active(cfg_off))
    cfg_other = {"script_name": "whimbox", "module_mode": {"enabled": True}}
    check("其它脚本不误开 -> False", not AutomationController._module_mode_active(cfg_other))
    cfg_ww = {"script_name": "ok-ww", "module_mode": {"enabled": True}}
    check("ok-ww 保持 -> True", AutomationController._module_mode_active(cfg_ww))


def test_okhna_paths(probe_root: Path):
    print("[3] ok-hna 路径解析（无 main.py）")
    (probe_root / "config.py").write_text("# ok-hna config stub\n", encoding="utf-8")
    (probe_root / "configs").mkdir(parents=True, exist_ok=True)
    app = _app_config(probe_root)
    paths = _resolve_paths(app, _as_module_config(app))
    check("runtime_root 命中 config.py 根", paths.runtime_root == probe_root.resolve(), paths.runtime_root)
    check("runtime_name=ok-hna", paths.runtime_name == "ok-hna", paths.runtime_name)
    check("humanize 关闭（None）", paths.humanize_config is None, paths.humanize_config)
    check("停滞阈值透传", paths.extra_env.get("GAMEAUTO_OKWW_STALL_LOG_SEC") == "720", paths.extra_env)
    check("runner 仍是同一份 headless runner", paths.runner_path.name == "runner.py", paths.runner_path)


def test_okww_compat(probe_root: Path):
    print("[4] ok-ww humanize 兼容不回退")
    (probe_root / "config.py").write_text("# stub\n", encoding="utf-8")
    (probe_root / "configs").mkdir(parents=True, exist_ok=True)
    app = _app_config(probe_root, runtime_name="ok-ww", humanize_config="")
    app["script_name"] = "ok-ww"
    paths = _resolve_paths(app, _as_module_config(app))
    check("ok-ww 空串仍用默认 humanize", paths.humanize_config is not None, paths.humanize_config)
    check("ok-ww runtime_name", paths.runtime_name == "ok-ww", paths.runtime_name)


def test_task_disabled_marker():
    print("[5] TaskDisabledException 不能被当成正常完成")
    runner_path = PROJECT_ROOT / "tools" / "okww_module" / "runner.py"
    spec = importlib.util.spec_from_file_location("okww_runner_for_test", runner_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    check("正常日志不误报", mod._extract_task_disabled_error("normal completion") == "")
    sample = (
        "2026-11-28 21:00:00,000 ERROR TaskExecutor:❌ 作战界面没有找到「挑战」按钮\n"
        "2026-11-28 21:00:00,001 INFO TaskExecutor:TaskDisabledException, continue <日常活跃>\n"
    )
    got = mod._extract_task_disabled_error(sample)
    check("异常行 -> 失败", "TaskDisabledException" in got, got)
    check("带失败上下文", "挑战" in got, got)
    generic = "2026-11-28 21:00:00,001 INFO TaskExecutor:TaskDisabledException, continue <日常活跃>\n"
    check("无上下文也判失败", "TaskDisabledException" in mod._extract_task_disabled_error(generic))


def test_static_wiring():
    print("[6] 静态接线与护栏")
    runner_src = (PROJECT_ROOT / "tools" / "okww_module" / "runner.py").read_text(encoding="utf-8")
    session_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "okww_module_session.py").read_text(encoding="utf-8")
    batch_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "task_batch_runner.py").read_text(encoding="utf-8")
    ctrl_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "automation_controller.py").read_text(encoding="utf-8")
    check("runner 接受 config.py 根", 'self.root / "config.py"' in runner_src)
    check("runner 暴露 --runtime-name", '"--runtime-name"' in runner_src)
    check("runner 成功标记只给 ok-ww", 'self.runtime_name == "ok-ww"' in runner_src)
    check("runner humanize 缺文件跳过", "not Path(path).is_file()" in runner_src)
    check("session 透传 --runtime-name", '"--runtime-name"' in session_src)
    check("session 透传停滞阈值", '"GAMEAUTO_OKWW_STALL_LOG_SEC"' in session_src)
    check(
        "批次 runner 认可 ok-hna",
        'in ("ok-ww", "ok-hna")' in batch_src,
        "module 判定缺 ok-hna",
    )
    check("批次 runner 模块结果优先于日志复核", "not _module_mode" in batch_src)
    check("控制器有管理员预检", "require_admin" in ctrl_src and "_is_admin_process" in ctrl_src)
    check("runner 识别 TaskDisabledException", "_extract_task_disabled_error" in runner_src)


def main():
    probe_root = PROJECT_ROOT / ".temp" / "okhna_module_probe"
    if probe_root.exists():
        shutil.rmtree(probe_root, ignore_errors=True)
    probe_root.mkdir(parents=True, exist_ok=True)
    try:
        test_profile()
        test_module_active()
        test_okhna_paths(probe_root)
        test_okww_compat(probe_root)
        test_task_disabled_marker()
        test_static_wiring()
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
