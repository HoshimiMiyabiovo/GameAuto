# -*- coding: utf-8 -*-
"""配置锁收敛 + 心跳接线 + 监控补课 回归（2026-11-09 审计修复批）。

背景：全库审计（docs/notes/code-audit-20261109.md）最大发现是
app_config.json 全链路无锁读-改-写（24 处 save 调用点跨 3 类线程 +
settings_gui 跨进程陈旧快照 + 三条旁路绕行 manager）。本套件锁住修复批：

1. manager 深拷贝语义 + update_config 并发原子性（50 线程各写一 key 不丢）；
2. .bak 恢复兜底：主文件损坏 → 从 .bak 恢复并回写；无 .bak → 默认值；
3. 绕行者收敛：startup_sign_runner / daily_checker / email_reporter /
   webui.auth / scheduler 全部走 manager 锁内入口（静态+行为）；
4. 心跳：thread-local 桥动态行为 + run_flow/确认策略/controller/NTE 链接线（静态）；
5. 凭据日志脱敏：nikki 打日志去 query、mihoyo 不打 tokens 原始值；
6. WebUI try_acquire 并发唯一胜者 + release_running 归属校验 + 两端点接线；
7. wool owner 代际复位 + flow_configs.json 双写方持锁；
8. 监控补课：bettergi/whimbox 读超时、safe_mtime_key 容错、
   task_batch_runner 监控 start() 移进 try。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_config_lock_robustness_20261109.py -q
     （或 tests/test_suites.py 全量）
"""

import io
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def _sandbox_manager():
    """把 manager 指到临时文件并清缓存，返回 (manager 模块, 临时目录)。"""
    from game_auto.settings import manager as m
    tmp = Path(tempfile.mkdtemp(prefix="ga_cfg_"))
    m._CONFIG_FILE = tmp / "app_config.json"
    m._cache = None
    return m, tmp


def test_manager_deepcopy_and_atomic_update():
    print("[1] manager：load 深拷贝 + update_config 并发原子性")
    m, tmp = _sandbox_manager()
    try:
        cfg1 = m.load()
        cfg1["global"]["min_run_time"] = 999
        cfg2 = m.load()
        check("load 返回深拷贝（改返回值不污染缓存）", cfg2["global"]["min_run_time"] == 300)

        errs = []

        def worker(i):
            try:
                m.update_config(lambda c, i=i: c.__setitem__(f"k{i}", i))
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        final = m.load()
        missing = [i for i in range(50) if final.get(f"k{i}") != i]
        check("50 线程并发 update_config 零异常", not errs, errs[:2])
        check("50 线程并发 update_config 零丢更新", not missing, missing[:5])

        # save 也被锁住：并发 save 与 update 混跑后文件仍是合法 JSON
        def saver():
            try:
                m.save(m.load())
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)
        ts2 = [threading.Thread(target=saver) for _ in range(10)]
        for t in ts2:
            t.start()
        for t in ts2:
            t.join()
        ok_json = True
        try:
            json.loads(m._CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            ok_json = False
        check("并发 save 后落盘仍是合法 JSON", ok_json and not errs, errs[:2])
    finally:
        m._cache = None
        shutil.rmtree(tmp, ignore_errors=True)


def test_bak_recovery():
    print("[2] manager：.bak 恢复兜底")
    m, tmp = _sandbox_manager()
    try:
        # bak 在"已有主文件的 save"时才写（首次 save 落主文件本身），先建一轮
        m.update_config(lambda c: c.__setitem__("warmup", True))
        m.update_config(lambda c: c.__setitem__("marker", "keepme"))
        bak = Path(str(m._CONFIG_FILE) + ".bak")
        check(".bak 已随 save 写出", bak.exists())
        # 主文件损坏（模拟原子写窗口内断电/磁盘满）
        m._CONFIG_FILE.write_text("{broken json", encoding="utf-8")
        m._cache = None
        cfg = m.load()
        check("主文件损坏后从 .bak 恢复数据", cfg.get("marker") == "keepme", cfg.get("marker"))
        ok = True
        try:
            rewritten = json.loads(m._CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            ok = False
            rewritten = {}
        check("恢复后已回写合法主文件", ok and rewritten.get("marker") == "keepme")
        # 无 .bak → 默认值兜底
        bak.unlink(missing_ok=True)
        m._CONFIG_FILE.write_text("{broken", encoding="utf-8")
        m._cache = None
        cfg2 = m.load()
        check("无 .bak 时回退默认值不崩", isinstance(cfg2, dict) and "app_order" in cfg2)
    finally:
        m._cache = None
        shutil.rmtree(tmp, ignore_errors=True)


def test_bypassers_converged():
    print("[3] 绕行者收敛（静态 + 行为）")
    # 行为层：startup_sign_runner 走 manager.save 归一化落盘
    from game_auto.services import startup_sign_runner as ssr
    m, tmp = _sandbox_manager()
    try:
        ssr._write_sign_status_to_config("wuwa", "success", "测试签到")
        raw = json.loads(m._CONFIG_FILE.read_text(encoding="utf-8"))
        sec = raw.get("wuwa") or {}
        check("签到状态三字段落盘", sec.get("last_sign_status") == "success"
              and "last_sign_date" in sec and "last_sign_message" in sec, sec)
        check("落盘是归一化 overrides（不写全量默认）", "_comment" in raw and "setup_completed" not in raw,
              sorted(raw.keys())[:6])
    finally:
        m._cache = None
        shutil.rmtree(tmp, ignore_errors=True)

    # 静态层：各绕行者源码不再直写整份文件
    def src(rel):
        return (PROJECT_ROOT / "src" / "game_auto" / rel).read_text(encoding="utf-8")

    ssr_src = src(Path("services/startup_sign_runner.py"))
    check("startup_sign_runner 走 cm.update_config", "cm.update_config(_apply)" in ssr_src)
    check("startup_sign_runner 不再直写 APP_CONFIG_PATH",
          "atomic_write_json(APP_CONFIG_PATH" not in ssr_src)

    dc_src = src(Path("clients/daily_checker.py"))
    check("daily_checker._persist_device_cache 走 update_config",
          "_cm.update_config(_apply)" in dc_src)
    check("daily_checker 不再自管 tmp_device 直写",
          "tmp_device" not in dc_src)

    em_src = src(Path("services/email_reporter.py"))
    check("email_reporter.save_email_section 走 update_config",
          "_cm.update_config(_apply)" in em_src)

    auth_src = src(Path("ui/webui/auth.py"))
    check("webui.auth._save_section 走 update_config",
          "cfg_manager.update_config(_apply)" in auth_src)

    sch_src = src(Path("ui/bridge_views/scheduler.py"))
    check("scheduler 保存定时任务走 update_config",
          "cfg_manager.update_config(_apply)" in sch_src)
    check("scheduler 30s 读不再 force 全量重读", "cfg_manager.load(force=True)" not in sch_src)

    gui_src = src(Path("maintenance/settings_gui.py"))
    check("settings_gui 保存前 load(force=True) 缩跨进程陈旧窗口",
          "cm.load(force=True)" in gui_src)


def test_heartbeat_wiring():
    print("[4] 心跳：TLS 桥动态 + 接线静态")
    from game_auto.services.task_heartbeat_watchdog import (
        beat_thread_heartbeat, set_thread_heartbeat)

    calls = []
    set_thread_heartbeat(lambda: calls.append(1))
    beat_thread_heartbeat()
    beat_thread_heartbeat()
    check("挂载后每次 beat 透传回调", len(calls) == 2)
    set_thread_heartbeat(None)
    beat_thread_heartbeat()
    check("清除后 beat 是空操作", len(calls) == 2)

    def _bad():
        raise RuntimeError("hb")
    set_thread_heartbeat(_bad)
    try:
        beat_thread_heartbeat()
        ok = True
    except Exception:
        ok = False
    check("回调异常不打断业务循环", ok)
    set_thread_heartbeat(None)

    # 跨线程隔离：新线程看不到本线程挂载的回调
    seen = []
    set_thread_heartbeat(lambda: calls.append(1))
    t = threading.Thread(target=lambda: seen.append(getattr(
        __import__("game_auto.services.task_heartbeat_watchdog", fromlist=["_HB_TLS"])._HB_TLS,
        "cb", None)))
    t.start()
    t.join()
    check("thread-local 按线程隔离（新线程 cb 为 None）", seen[0] is None)
    set_thread_heartbeat(None)

    fff_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "foreground_flow_runner.py").read_text(encoding="utf-8")
    for anchor, label in [
        ("def _heartbeat_beat()", "fff 定义 _heartbeat_beat 透传助手"),
        ("while remaining > 0:\n        if _stop_requested(stop_check):\n            return False\n        _heartbeat_beat()",
         "_sleep_interruptible 每轮心跳"),
        ("if _stop_requested(stop_check):\n            return None\n        _heartbeat_beat()\n        hwnd = _find_window_by_pid(pid)\n        if hwnd:\n            return hwnd",
         "_wait_window_by_pid 每轮心跳"),
        ("if _stop_requested(stop_check):\n            return \"\"\n        _heartbeat_beat()",
         "_wait_script_update_exe 每轮心跳"),
        ("return False, f\"[{flow_key}] 用户已请求停止\"\n                # 每轮匹配续命", "run_flow 步骤匹配循环每轮心跳"),
        ("def _wait_any_template", "_wait_any_template 存在"),
    ]:
        check(label, anchor in fff_src)

    # 2026-11-10：确认策略同时接了 stop_check（审计 P2 批），心跳仍在
    # stop 检查之后每轮执行
    check("confirm_disappear 轮询循环心跳",
          'return False, "用户已请求停止"\n        _heartbeat_beat()\n        still_exists, _ = _check_element_exists(' in fff_src)
    check("confirm_text_change 轮询循环心跳",
          'return False, "用户已请求停止"\n        _heartbeat_beat()\n        if enable_ocr and expected_text_after:' in fff_src)

    ctrl_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "automation_controller.py").read_text(encoding="utf-8")
    check("execute_task 入口挂载 thread-local 心跳",
          "set_thread_heartbeat(self._heartbeat.beat)" in ctrl_src)
    check("execute_task finally 清除挂载",
          "set_thread_heartbeat(None)" in ctrl_src)
    check("_wait_game_window_ready 循环打心跳",
          "self._watchdog_beat()\n            pids = self._game_related_pids(proc_name)" in ctrl_src)
    check("心跳出口对 __new__ 桩实例防御（getattr）",
          "heartbeat = getattr(self, \"_heartbeat\", None)" in ctrl_src)

    okww_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "ok_ww_log_monitor.py").read_text(encoding="utf-8")
    check("NTE 启动后窗口等待循环打心跳",
          "_heartbeat_beat()\n            fallback = _find_window_by_filters(process_names=window_process_names or None)" in okww_src)


def test_credential_log_sanitized():
    print("[5] 凭据日志脱敏")
    nikki_src = (PROJECT_ROOT / "src" / "game_auto" / "gacha" / "nikki_resonance_fetch.py").read_text(encoding="utf-8")
    check("nikki HTTP 失败日志只打 path 不带 query",
          'path.split("?", 1)[0]' in nikki_src and
          'method.upper(), path, response.status' not in nikki_src)
    mihoyo_src = (PROJECT_ROOT / "src" / "game_auto" / "clients" / "mihoyo_sign.py").read_text(encoding="utf-8")
    check("mihoyo 不再把 tokens 原始值打进日志", "repr(tokens_raw)" not in mihoyo_src)
    check("mihoyo tokens 改打类型与个数",
          'len(tokens_raw) if hasattr(tokens_raw, "__len__")' in mihoyo_src)


def test_webui_try_acquire():
    print("[6] WebUI try_acquire 原子性 + 端点接线")
    from game_auto.ui.webui import state as ws
    wins = []
    barrier = threading.Barrier(16)

    def racer(i):
        barrier.wait()
        if ws.try_acquire_running("webui"):
            wins.append(i)

    ts = [threading.Thread(target=racer, args=(i,)) for i in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("16 线程并发抢占恰好 1 个成功", len(wins) == 1, wins)
    check("抢占后 running=True", ws.is_running())
    ws.release_running("gui")
    check("release 校验 source（gui 清不掉 webui 的状态）", ws.is_running())
    ws.release_running("webui")
    check("归属匹配才清 running", not ws.is_running())
    ws.release_running("webui")
    check("空闲时 release 幂等", not ws.is_running())

    auto_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "webui" / "api" / "automation.py").read_text(encoding="utf-8")
    check("/api/apps/run 用 try_acquire_running", "if not try_acquire_running(\"webui\", queue=queue):" in auto_src)
    check("/api/apps/run 抢占后失败路径归还", "release_running(\"webui\")" in auto_src)
    check("/api/apps/run 不再有裸 check-then-act",
          "if is_running():\n        return {\"ok\": False, \"error\": \"已有任务运行中\"}" not in auto_src)
    wool_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "webui" / "api" / "wool.py").read_text(encoding="utf-8")
    check("/api/wool/run 用 try_acquire_running", "if not try_acquire_running(" in wool_src)
    check("/api/wool/run finally 用 release_running", "release_running(\"webui\")" in wool_src)


def test_wool_owner_and_flow_lock():
    print("[7] wool owner 代际复位 + flow_configs 双写方持锁")
    auto_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "bridge_views" / "automation.py").read_text(encoding="utf-8")
    wool_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "webui" / "api" / "wool.py").read_text(encoding="utf-8")
    check("runWoolCollection 锁内进入即占位",
          "with self._run_lock:\n            if self._is_running:\n                self.emit_event(\"toast\", message=\"有任务正在运行，请先等待完成\", color=\"#b66f1f\")\n                return\n            self._is_running = True" in auto_src)
    check("wool 后台线程携带代际号", "args=(wool_generation,)" in auto_src)
    check("wool finally 持有者校验复位",
          "is_owner = self._run_generation == wool_generation" in auto_src)
    check("wool 不再无条件 isRunning = False",
          "finally:\n            self.isRunning = False" not in auto_src)
    check("wool WebUI 释放改 release_running('gui')", "_ws_release_w4(\"gui\")" in auto_src)
    check("startAutomation 锁内同步置位 WebUI running", "_ws_set_running_early(True, source=\"gui\")" in auto_src)

    paths_src = (PROJECT_ROOT / "src" / "game_auto" / "settings" / "paths.py").read_text(encoding="utf-8")
    check("FLOW_CONFIG_LOCK 定义在 settings/paths", "FLOW_CONFIG_LOCK = _threading.Lock()" in paths_src)
    check("WebUI toggle 持 FLOW_CONFIG_LOCK", "with FLOW_CONFIG_LOCK:" in wool_src)
    check("GUI _save_wool_flow_field 持 FLOW_CONFIG_LOCK",
          "with FLOW_CONFIG_LOCK:" in auto_src)


def test_monitor_hardening():
    print("[8] 监控三件套补课")
    from game_auto.services.utils import safe_mtime_key
    tmp = Path(tempfile.mkdtemp(prefix="ga_mon_"))
    try:
        f = tmp / "a.log"
        f.write_text("x", encoding="utf-8")
        key = safe_mtime_key(f)
        check("活文件 key=(mtime, name)", key[1] == "a.log" and key[0] > 0)
        f.unlink()
        key2 = safe_mtime_key(f)
        check("文件消失后 key 容错为 (0.0, name)", key2 == (0.0, "a.log"), key2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bg_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "bettergi_log_monitor.py").read_text(encoding="utf-8")
    check("bettergi 引入 read_file_with_timeout + safe_mtime_key",
          "from ..services.utils import read_file_with_timeout, safe_mtime_key" in bg_src)
    check("bettergi 裸 open/read 已移除", "handle.read()" not in bg_src)
    check("bettergi 排序 key 容错", "sorted(todays, key=safe_mtime_key)" in bg_src)
    check("bettergi watch loop 有轮询兜底", "监控轮询异常（继续下一轮）" in bg_src)
    check("bettergi 启动函数透传 read_timeout", "read_timeout=float(monitor_config.get(\"read_timeout\", 10.0)" in bg_src)

    wb_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "whimbox_log_monitor.py").read_text(encoding="utf-8")
    check("whimbox 引入 read_file_with_timeout + safe_mtime_key",
          "from ..services.utils import read_file_with_timeout, safe_mtime_key" in wb_src)
    check("whimbox 裸 open/read 已移除", "with open(path, \"r\", encoding=self.encoding" not in wb_src)
    check("whimbox 排序 key 容错", "sorted(files, key=safe_mtime_key)" in wb_src)
    check("whimbox watch loop 有轮询兜底", "监控轮询异常（继续下一轮）" in wb_src)
    check("whimbox 启动函数透传 read_timeout", "read_timeout=float(monitor_config.get(\"read_timeout\", 10.0)" in wb_src)

    okww_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "ok_ww_log_monitor.py").read_text(encoding="utf-8")
    check("ok_ww 排序 key 容错", "sorted(files, key=safe_mtime_key)" in okww_src)

    batch_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "task_batch_runner.py").read_text(encoding="utf-8")
    check("bettergi 监控 start() 移进 try（异常降级 None）",
          "bettergi_monitor = start_bettergi_log_monitor(app_config, controller.logger)" in batch_src
          and "except Exception as exc:\n                controller.logger.error(f\"[GI监控] BetterGI 日志监控启动失败" in batch_src)
    check("whimbox 监控 start() 移进 try（异常降级 None）",
          "except Exception as exc:\n                controller.logger.error(f\"[CJ流程] 奇想盒日志监控启动失败" in batch_src)

    # 行为层：bettergi 监控单轮轮询不炸（目录缺失 / 存在但空 两种路径）
    from game_auto.runtime.bettergi_log_monitor import BetterGILogMonitor
    import logging as _log
    mon = BetterGILogMonitor(_log.getLogger("GameAutomation"), log_dir=str(tmp),
                             file_pattern="*.log")
    check("bettergi read_timeout 默认 10s", mon.read_timeout == 10.0)
    check("目录缺失单轮安全（返回 warned=True）", mon._watch_once(False) is True)
    empty_dir = Path(tempfile.mkdtemp(prefix="ga_mon_empty_"))
    try:
        mon2 = BetterGILogMonitor(_log.getLogger("GameAutomation"), log_dir=str(empty_dir),
                                  file_pattern="*.log")
        check("空目录单轮安全（返回 warned=False）", mon2._watch_once(False) is False)
    finally:
        shutil.rmtree(empty_dir, ignore_errors=True)


def main():
    test_manager_deepcopy_and_atomic_update()
    test_bak_recovery()
    test_bypassers_converged()
    test_heartbeat_wiring()
    test_credential_log_sanitized()
    test_webui_try_acquire()
    test_wool_owner_and_flow_lock()
    test_monitor_hardening()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
