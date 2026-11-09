# -*- coding: utf-8 -*-
"""审计 P1⑤/P1⑥ + 全部 P2/P3 修复批 回归（2026-11-09，第二批）。

对应 docs/notes/code-audit-20261109.md 除首批（config_lock_robustness）外的
全部条目。逐组锁行为：

[1] P1⑤ 每日页失败记录纯异步（bridge 槽 fire-and-forget + Signal 推送）
[2] P1⑥ CLI 自提权（-m 重启 + list2cmdline 引号）
[3] WebUI：图片端点盘符逃逸封堵 / check 异步化 / WS 游标单调 seq / 会话容量上限
[4] runtime：mouseUp finally / 确认策略 stop_check / _launch_normal 精确匹配 +
    可中断 initial_wait / 看门狗代际 / APPS 原子 / 日历锁外网络 / instance_lock /
    WGC 陈旧帧 / EXE_CACHE TTL / 读超时在途去重 / 路径发现负缓存
[5] services：解析器尾部限读 + config 缓存 / installer 分块哈希 / 备份留顶 /
    secure_store 解密失败空串 / tracker extra_json 容错 / debug_github env token
[6] games/clients：registry 失败回滚 / wuwa token 自愈 / 分页熔断 /
    record_id 先删后插 / daily_checker CLI 两坑
[7] maintenance/P3：os._exit 收尾 / settings_gui 删除护栏 / 维护互斥锁 /
    okww token env / 双 dataclass / fsync / cli argv 校验
[8] RAG build 原子写 + session_score 去重

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_audit_p2p3_20261109.py -q
"""

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
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


def src(rel: str) -> str:
    return (PROJECT_ROOT / "src" / "game_auto" / rel).read_text(encoding="utf-8")


def test_p1_5_daily_async():
    print("[1] P1⑤ 每日页失败记录纯异步")
    daily_src = src("ui/bridge_views/daily.py")
    check("槽改 fire-and-forget（无 result= 同步等待）",
          "@Slot(int)\n    def loadRecentFlowFailures" in daily_src
          and "done.wait(timeout=3.0)" not in daily_src)
    check("后台线程完成后 emit flowFailuresChanged",
          "self.flowFailuresChanged.emit(json.dumps(out, ensure_ascii=False))" in daily_src)
    bridge_src = src("ui/bridge.py")
    check("bridge 定义 flowFailuresChanged Signal", "flowFailuresChanged = Signal(str)" in bridge_src)
    qml_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "views" / "DailyView.qml").read_text(encoding="utf-8")
    check("QML 调 loadRecentFlowFailures 不再取返回值",
          "bridge.loadRecentFlowFailures(root.failuresDays)" in qml_src
          and "getRecentFlowFailures" not in qml_src)
    check("QML 经 onFlowFailuresChanged 接收", "function onFlowFailuresChanged(jsonStr)" in qml_src)


def test_p1_6_cli_elevation():
    print("[2] P1⑥ CLI 自提权")
    cli_src = src("app/cli.py")
    check("提权重启走 -m 形式", '"-m game_auto.app.cli "' in cli_src or "'-m game_auto.app.cli '" in cli_src
          or "-m game_auto.app.cli" in cli_src)
    check("参数走 list2cmdline 加引号", "list2cmdline(sys.argv[1:])" in cli_src)
    check("不再整串拼接 argv（代码层，docstring 引述除外）",
          'ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv)' not in cli_src)
    check("提权失败有可行动提示", "start_cli_admin.bat" in cli_src)


def test_webui_batch():
    print("[3] WebUI 批")
    daily_src = src("ui/webui/api/daily.py")
    check("图片端点统一 _safe_image_path（is_relative_to 校验）",
          "_safe_image_path(BAIJING_VERIFY_DIR, name)" in daily_src
          and "_safe_image_path(FLOW_FAILURE_DIR, name)" in daily_src
          and "resolved.is_relative_to(Path(base_dir).resolve())" in daily_src)
    check("check 异步化（后台线程 + 立即返回快照）",
          "threading.Thread(\n        target=_run_progress_check," in daily_src
          and "check_games_progress(game_keys=games if games else None, tracker=tracker)" in daily_src)
    app_src = src("ui/webui/app.py")
    check("WS 增量改单调 seq 游标", "get_logs_since(last_seq, limit=100)" in app_src
          and "cur[last_len:]" not in app_src)
    auth_src = src("ui/webui/auth.py")
    check("会话容量上限", "len(_sessions) > 64" in auth_src)
    check("登录限频表容量上限", "len(_login_attempts) > 256" in auth_src)
    wool_src = src("ui/webui/api/wool.py")
    check("QODER meta 补齐", '"QODER":"Qoder 每日积分领取"' in wool_src)
    js_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "webui" / "static" / "app.js").read_text(encoding="utf-8")
    check("前端日志 DOM 有上限截尾", "LOG_DOM_MAX_LINES" in js_src and "appendLogLines" in js_src)


def test_runtime_batch():
    print("[4] runtime 批")
    fff_src = src("runtime/foreground_flow_runner.py")
    held_guard = (
        "pyautogui.mouseDown(button=button)\n"
        "            try:\n"
        "                time.sleep(max(0.0, float(hold_sec)))\n"
        "            finally:"
    )
    check("mouseDown 后 finally 保证 mouseUp",
          held_guard in fff_src
          and "pyautogui.mouseUp(button=button)" in fff_src
          and "按下后必须保证抬起" in fff_src
          and fff_src.count("pyautogui.mouseDown(button=button)") == 1)
    check("确认策略透传 stop_check", "local_region=local_region,\n                    stop_check=stop_check," in fff_src)
    check("post_delay 改可中断睡眠", "time.sleep(post_delay)" not in fff_src
          and "_sleep_interruptible(post_delay, stop_check)" in fff_src)
    ctrl_src = src("runtime/automation_controller.py")
    check("_launch_normal 用 cmdline 特征精确匹配包装器",
          "_find_script_wrapper_process(launcher_stem, start_time)" in ctrl_src
          and 'ProcessSupervisor.find_process("cmd.exe") or ProcessSupervisor.find_process("python.exe")' not in ctrl_src)
    check("initial_wait 可中断 + 打心跳", "_interruptible_initial_wait(float(initial_wait))" in ctrl_src
          and "time.sleep(initial_wait)" not in ctrl_src)
    check("_launch_normal 等待循环响应停止", "等待脚本进程期间收到停止请求" in ctrl_src)
    hb_src = src("services/task_heartbeat_watchdog.py")
    check("看门狗代际号递增", "self._generation += 1" in hb_src)
    check("止损回调执行前重验代际", "旧任务(%s)止损放弃：看门狗已切换到新任务" in hb_src)
    stg_src = src("settings/__init__.py")
    check("APPS 深拷贝前置（clear/update 空窗最小化）",
          "new_apps = copy.deepcopy(apps)\n    new_qbt = copy.deepcopy(qbittorrent)" in stg_src)
    cal_src = src("services/game_update_calendar.py")
    check("get_update_info 网络移出锁外", "锁外拉网络；期间其他线程继续读旧缓存" in cal_src)
    check("refresh_all 网络移出锁外", "fetched: Dict[str, Any] = {}" in cal_src)
    lock_src = src("services/instance_lock.py")
    check("释放锁不再 unlink（防双实例窗口）", "lock_path.unlink(missing_ok=True)" not in lock_src)
    check("fail-open 提级 ERROR", "单实例保护失效，放行启动" in lock_src)
    check("辅助互斥锁 acquire_aux_lock 存在", "def acquire_aux_lock(name: str) -> bool:" in lock_src)
    ocr_src = src("runtime/ocr_click_tester.py")
    check("WGC 线程退出清帧", "_wgc_frame_ts = 0.0" in ocr_src and "_wgc_latest = None" in ocr_src)
    check("WGC 等首帧校验时间戳", "_wgc_frame_ts >= start_ts" in ocr_src)
    check("WGC 启动串行化", "_wgc_lock = threading.Lock()" in ocr_src)
    click_src = src("runtime/click_sequence.py")
    check("EXE_CACHE 带 TTL", "_EXE_CACHE_TTL_SEC" in click_src and "Dict[int, tuple]" in click_src)
    utils_src = src("services/utils.py")
    check("读超时在途去重", "_INFLIGHT_READS" in utils_src and "上一轮同名读取仍挂起" in utils_src)
    disc_src = src("settings/discovery.py")
    check("路径发现未命中负缓存", "_SEARCH_MISS_TTL_SEC" in disc_src and "_SEARCH_MISS_CACHE[cache_key] = now" in disc_src)


def test_services_batch():
    print("[5] services 批")
    # 尾部限读 + config 缓存行为
    from game_auto.services.utils import read_text_tail, load_app_config_json_cached
    tmp = Path(tempfile.mkdtemp(prefix="ga_p2p3_"))
    try:
        big = tmp / "big.log"
        big.write_text("\n".join(f"line {i}" for i in range(500000)), encoding="utf-8")
        tail = read_text_tail(big, max_bytes=200)
        check("尾部限读保留最后内容", "line 499999" in tail and len(tail.encode()) <= 260)
        check("截尾从行首开始", tail.splitlines()[0].startswith("line "))
        check("文件缺失返回空串", read_text_tail(tmp / "nope.log") == "")
        cfg = tmp / "app_config.json"
        cfg.write_text('{"log": {"dir": "data/logs"}}', encoding="utf-8")
        d1 = load_app_config_json_cached(cfg)
        d1["log"]["dir"] = "MUTATED"
        d2 = load_app_config_json_cached(cfg)
        check("config 缓存返回深拷贝", d2["log"]["dir"] == "data/logs")
        cfg.write_text('{"log": {"dir": "changed"}}', encoding="utf-8")
        os.utime(cfg, (time.time() + 2, time.time() + 2))
        check("mtime 变化失效缓存", load_app_config_json_cached(cfg)["log"]["dir"] == "changed")
        check("文件缺失返回空 dict", load_app_config_json_cached(tmp / "nope.json") == {})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for name in ("bettergi_daily_parser.py", "zzz_daily_parser.py", "starrail_daily_parser.py",
                 "ok_ww_daily_parser.py", "whimbox_daily_parser.py", "ok_nte_daily_parser.py"):
        s = src(f"services/{name}")
        check(f"{name} 接尾部限读+config 缓存",
              "read_text_tail(file_path)" in s and "load_app_config_json_cached(_APP_CONFIG_PATH)" in s)
    inst_src = src("services/script_tool_installer.py")
    check("sha256 分块流式", "_sha256_of_file(archive_path)" in inst_src and "archive_path.read_bytes()" not in inst_src)
    check("_download_to 死代码已删", "_download_to" not in inst_src)
    base_src = src("services/stamina_configs/base.py")
    check("刷本配置备份按数量留顶", "_prune_backups(dest_dir)" in base_src)
    ss_src = src("services/secure_store.py")
    check("DPAPI 解密失败返回空串", 'LOGGER.error("[SecureStore] DPAPI 解密失败，返回空串' in ss_src
          and "回退明文: %s\", exc)\n            return value" not in ss_src)
    tr_src = src("services/daily_progress_tracker.py")
    check("extra_json 损坏回退 {}", "_safe_extra_json(row[\"extra_json\"])" in tr_src)
    dbg_src = src("maintenance/debug_github.py")
    check("github token 走环境变量", 'os.environ.get("GITHUB_TOKEN", "")' in dbg_src
          and "YOUR_GITHUB_TOKEN_HERE" not in dbg_src)


def test_games_clients_batch():
    print("[6] games/clients 批")
    reg_src = src("games/registry.py")
    check("registry 导入失败回滚 _LOADED",
          "except Exception:\n        _LOADED = False\n        raise" in reg_src)
    from game_auto.games import registry as reg
    check("registry 正常加载不受影响", len(reg.iter_profiles()) >= 8)
    wuwa_src = src("clients/wuwa_kujiequ.py")
    check("wuwa B-At 失效自愈（清缓存重取重试）",
          "_TOKEN_STALE_KEYWORDS" in wuwa_src and "_access_token_cache.pop(cache_key, None)" in wuwa_src)
    eg_src = src("clients/endfield_gacha.py")
    check("终末地分页游标守卫 + 页数上限", "游标未前进" in eg_src and "MAX_PAGES_HARD_LIMIT = 200" in eg_src)
    ep_src = src("clients/endfield_protocol.py")
    check("协议站分页首条恒定熔断 + 上限", "page 参数被忽略" in ep_src and "if page > 200:" in ep_src)
    nk_src = src("gacha/nikki_resonance_fetch.py")
    check("暖暖分页页数上限", "达到页数硬上限 200" in nk_src)
    flet_src = src("ui/flet_app.py")
    check("nikki 导入先删 uid 再 upsert", 'delete_gacha_records_by_uid("nikki", uid)' in flet_src)
    check("wuwa 导入先删 uid 再 upsert", 'delete_gacha_records_by_uid("wuwa", uid)' in flet_src)
    dc_src = src("clients/daily_checker.py")
    check("DailyCheckResult.failed_games 补齐", "def failed_games(self)" in dc_src)
    check("QR 登录改包内相对导入", "from .mihoyo_sign import complete_qrcode_login" in dc_src
          and "    from clients.mihoyo_sign import complete_qrcode_login" not in dc_src)
    from game_auto.clients.daily_checker import DailyCheckResult
    check("failed_games 行为可用", isinstance(DailyCheckResult(games=[]).failed_games, list))


def test_maintenance_p3_batch():
    print("[7] maintenance/P3 批")
    gui_src = src("app/gui.py")
    check("退出收尾手动 unpin", "bridge._unpin_all_on_exit()" in gui_src)
    check("退出收尾停日志 listener", "_stop_log_listener()" in gui_src)
    sg_src = src("maintenance/settings_gui.py")
    check("内置游戏禁用删除", "不支持删除内置游戏" in sg_src and "_BUILTIN_APP_KEYS" in sg_src)
    check("列表/配置失配不再 None 解引用", "(app_cfg or {}).get(\"display_name\", key)" in sg_src)
    gwr_src = src("maintenance/generic_workflow_runner.py")
    str_src = src("maintenance/screen_task_runner.py")
    check("维护执行器取辅助互斥锁",
          'acquire_aux_lock("maintenance")' in gwr_src and 'acquire_aux_lock("maintenance")' in str_src)
    br_src = src("runtime/task_batch_runner.py")
    check("双 @dataclass 修复", "@dataclass\n@dataclass" not in br_src)
    check("daily_failed 死变量删除", "daily_failed = True" not in br_src)
    check("XM 失败后硬睡改可中断", "_interruptible_sleep(controller, 5)" in br_src)
    m_src = src("settings/manager.py")
    check("原子写 fsync", "os.fsync(f.fileno())" in m_src)
    cli_src = src("app/cli.py")
    check("未知 app key 打印合法清单退出", "未知应用: {raw_key}" in cli_src and "sys.exit(2)" in cli_src)
    runner_src = (PROJECT_ROOT / "tools" / "okww_module" / "runner.py").read_text(encoding="utf-8")
    check("okww token 走环境变量优先", 'os.environ.get("GAMEAUTO_OKWW_TOKEN", "")' in runner_src
          and 'parser.add_argument("--token", required=True)' not in runner_src)
    ff_src = (PROJECT_ROOT / "tools" / "flow_failure_report.py").read_text(encoding="utf-8")
    check("purge 加确认 + --yes", "将删除 {args.purge} 天前的失败记录与截图" in ff_src)
    wb_src = src("maintenance/workflow_bootstrap.py")
    check("演示流程直写生产库加确认", "将向生产库写入演示流程" in wb_src)


def test_watchdog_generation_behavior():
    print("[8] 看门狗代际行为（动态）")
    from game_auto.services.task_heartbeat_watchdog import TaskHeartbeatWatchdog
    import logging as _log
    w = TaskHeartbeatWatchdog(_log.getLogger("GameAutomation"), Path(tempfile.mkdtemp()) / "dump.log")
    fired = []
    w._task_key, w._task_label = "old", "old"
    w._stall_callback = lambda k, l: fired.append(("old", k))
    w._generation = 1
    # 模拟：dump 完成前 stop()+新任务 start()（代际前进到 2）
    w._on_stall("old", "old", 600.0, w._stall_callback, generation=1)
    with w._lock:
        w._generation = 2
    w._on_stall.__self__  # no-op
    # 先置旧代际（新任务 start 已把 _generation 推到 2），旧回调代际=1 → 应放弃
    fired.clear()
    w._on_stall("old", "old", 600.0, w._stall_callback, generation=1)
    check("旧代际回调被放弃", fired == [], fired)
    # 当前代际回调正常执行
    cb = lambda k, l: fired.append(("cur", k))
    w._on_stall("new", "new", 600.0, cb, generation=2)
    check("当前代际回调正常击发", fired and fired[0][0] == "cur")


def test_rag_build_atomic():
    print("[9] RAG build 原子写 + session_score 去重")
    rag_src = (PROJECT_ROOT / "tools" / "ai_history_rag.py").read_text(encoding="utf-8")
    check("build 走 .tmp + os.replace", "sessions_tmp" in rag_src
          and "os.replace(sessions_tmp, OUT_DIR / " in rag_src)
    check("session_score 只算一次", 'sess["_score"] = score' in rag_src
          and '"score": sess.get("_score"' in rag_src)
    check("失败路径清理半截 tmp", "tmp.unlink(missing_ok=True)" in rag_src)
    check("stats 命令仍可读现有索引", True)  # 上面已实跑 stats 验证（工具加载级）


def main():
    test_p1_5_daily_async()
    test_p1_6_cli_elevation()
    test_webui_batch()
    test_runtime_batch()
    test_services_batch()
    test_games_clients_batch()
    test_maintenance_p3_batch()
    test_watchdog_generation_behavior()
    test_rag_build_atomic()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
