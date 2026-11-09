import sys
import ctypes
import logging

# 强制 stdout/stderr 行缓冲，避免 PowerShell 把输出当管道处理时块缓冲导致日志不及时显示
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    # 非标准流（无 reconfigure）或被重定向时不支持行缓冲
    pass

from ..settings import APP_ORDER, get_app_display_name, resolve_app_key
from ..services.dependency_manager import run_full_dependency_check
from ..services.instance_lock import acquire_instance_lock
from ..services.mem_reduct_detect import warn_once_at_startup
from ..runtime.automation_controller import AutomationController
from ..runtime.task_batch_runner import run_task_batch
from ..services.utils import (
    clear_keyboard_buffer,
    get_saved_helper_enabled,
    get_saved_target_apps,
    get_user_selection,
)

def require_admin():
    """非管理员启动时自提权重启。

    2026-11-09 审计 P1-6：旧实现 `ShellExecuteW(..., " ".join(sys.argv))` 以
    脚本路径直跑重启后的 cli.py——`__package__` 为空，顶部相对导入必抛
    ImportError（提权窗口闪退）；且参数不加引号，路径含空格会切错。
    现改为 `python -m game_auto.app.cli <args>` 形式重启：能执行到本函数
    说明包本就可导入（脚本直跑在相对导入处就死了），-m 重启继承同样的
    PYTHONPATH 环境；参数逐个走 list2cmdline 加引号。
    """
    if ctypes.windll.shell32.IsUserAnAdmin():
        return
    import subprocess as _sp
    args = _sp.list2cmdline(sys.argv[1:]) if len(sys.argv) > 1 else ""
    params = ("-m game_auto.app.cli " + args).strip()
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    if ret <= 32:
        # 提权被用户取消/失败：给出可行动出口而不是无提示闪退
        ctypes.windll.user32.MessageBoxW(
            0,
            "CLI 需要管理员权限运行（合成点击受 UIPI 拦截）。\n"
            "自提权失败（可能取消了 UAC 弹窗）——请右键管理员运行，"
            "或使用 scripts\\launchers\\start_cli_admin.bat。",
            "需要管理员权限",
            0x10,
        )
    sys.exit(0)

def main():
    require_admin()
    bootstrap_logger = logging.getLogger("GameAutoBootstrap")
    if not bootstrap_logger.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format='[%(asctime)s.%(msecs)03d] [%(levelname)-8s] %(name)s:%(funcName)s:%(lineno)d - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    logging.getLogger("GameAutomation").setLevel(logging.DEBUG)

    existing_pid = acquire_instance_lock()
    if existing_pid:
        pid_str = str(existing_pid) if existing_pid > 0 else "未知"
        bootstrap_logger.error("[启动] 拒绝启动，检测到重复实例 PID=%s", pid_str)
        ctypes.windll.user32.MessageBoxW(
            0,
            f"已有 game_auto 实例在运行 (PID {pid_str})，请先关闭它再启动 CLI。",
            "启动失败 - 已有实例",
            0x10,
        )
        return

    warn_once_at_startup(bootstrap_logger)

    dep_report = run_full_dependency_check(bootstrap_logger)
    if dep_report.errors:
        bootstrap_logger.error(
            "检测到关键依赖错误，程序继续运行但部分功能可能不可用；"
            "请以普通权限运行 setup_deps.bat 补齐依赖"
        )

    controller = AutomationController()
    report = []

    try:
        force_rerun = "--force" in sys.argv or "-f" in sys.argv
        if force_rerun:
            sys.argv = [a for a in sys.argv if a not in ("--force", "-f")]

        if len(sys.argv) == 1:
            target_apps = get_saved_target_apps()
            if target_apps:
                print("="*50 + "\n游戏自动化助手 - 使用已保存启动设置\n" + "="*50)
                print("将执行：" + ", ".join(get_app_display_name(app) for app, _ in target_apps))
            else:
                print("="*50 + "\n游戏自动化助手 - 任务选择\n" + "="*50)
                target_apps = get_user_selection()
        else:
            # 未知 app key 直接退出并打印合法清单（审计 P3）：旧口径
            # resolve_app_key 原样透传，靠批处理内部兜错
            raw_key = sys.argv[1]
            resolved = resolve_app_key(raw_key)
            from ..games import registry as _reg
            if resolved == str(raw_key) and str(raw_key) not in {k.lower() for k in _reg.app_keys()} and str(raw_key) not in _reg.app_keys():
                print(f"未知应用: {raw_key}")
                print("可用应用: " + ", ".join(
                    f"{k}({get_app_display_name(k)})" for k in APP_ORDER))
                sys.exit(2)
            script_choice = sys.argv[2] if len(sys.argv) > 2 else None
            target_apps = [(resolved, script_choice)]

        if force_rerun:
            print('[强制重跑] 已启用，跳过"今日已完成"检查')

        batch_result = run_task_batch(
            controller,
            target_apps,
            xm_enabled=get_saved_helper_enabled("GenshinImpact"),
            cj_enabled=get_saved_helper_enabled("InfinityNikki"),
            force_rerun=force_rerun,
        )
        report.extend(batch_result.report)
        report.extend(batch_result.warnings)
    except KeyboardInterrupt:
        report.append("!! 操作被中断 !!")
        controller.logger.warning("用户中断操作")
    except Exception as e:
        report.append(f"!! 系统错误: {str(e)} !!")
        controller.logger.critical(f"系统错误: {str(e)}", exc_info=True)
    finally:
        controller._cleanup_key_listener()
        controller.logger.info("\n" + "="*40 + "\n任务执行报告:")
        for line in report:
            controller.logger.info(line)
       
        try:
            for handler in controller.logger.handlers:
                handler.flush()
        except PermissionError:
            print("警告: 日志刷新失败，已忽略 (权限问题)")
       
        ctypes.windll.user32.MessageBoxW(
            0,
            "自动化任务执行结果:\n\n" + "\n".join(report),
            "任务报告 - 游戏自动化助手",
            0x40
        )

if __name__ == "__main__":
    import logging.handlers
    main()