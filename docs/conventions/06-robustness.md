# 稳定性、OCR、长等待与失败诊断

> 由 AGENTS.md 拆分；改动相关代码前先读本篇。

### 蓝屏复盘结论 + 任务鲁棒性三层防护 (重要, 2026-10-15)

**2026-10-15 蓝屏根因（微软官方实锤，非 USB 盘）**：BugCheck 0x133，WER 桶名 `0x133_ISR_dxgmms2!UnblockCpuWaiter`，cdb `!analyze -v` 栈回溯确认故障函数 `dxgmms2.sys+0x321bd`（DirectX 图形内核的 GPU 完成等待器），下方是 NVIDIA RTX 5070 驱动 nvlddmkm.sys（2026-07-02 版，注册表 Class\{4d36e968-...}\0000 实锤）——GPU 完成通知未按时回调，ISR 上下文高 IRQL 累计超时，看门狗主动蓝屏。当时在跑终末地自动化（9:21 启动，9:32 脚本异常，9:47:49 崩溃）。USB 盘抽风（UASPStor 129）是并行的另一个硬件问题，与蓝屏无因果。查显卡型号用注册表 `HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\000X`（DriverDesc/DriverVersion/MatchingDeviceId）；**dump 里的 AMDPCIDev.sys 是主板 AMD PCIe 驱动，不代表独显是 AMD**（本次误判教训）。分析工具：`D:\Windows Kits\10\Debuggers\x64\cdb.exe -z <dmp> -y C:\Symbols -sn -c "!analyze -v;q"`（-sn 禁网络符号防卡死；提权读 Minidump 用 ShellExecuteW runas）。

**蓝屏 dump 时间基准铁律（2026-10-15 用两次误判换来）**：
- **日志内容时间戳 = GetLocalTime 墙钟 = 真实时间**，永远可信；cdb 的 Debug session time（崩溃时刻）与崩溃前最后一条日志相差极少（本次 09:46:45 → 09:47:49 仅 1 分钟）
- **dump 文件 mtime / BugCheck 事件时间 ≠ 崩溃时刻**：完整 dump 由下次启动的 crashdmp.sys 补写，WER 事件也是开机后才记录。本次崩溃 09:47:49，但 dump mtime 16:16:28、事件 UTC 08:16:34（=本地 16:16:34）都是用户下午 16:16 开机后产生的
- 判断真实崩溃/重启时间用 `psutil.boot_time()`（LastBootUpTime）；别用 dump mtime 推崩溃时刻，别把"日志停在 X"当成"任务卡了很久"——先算 X 与崩溃时刻的距离
- wevtutil `/f:xml` 的 TimeCreated 带 Z 是 UTC；Kernel-Power 41/EventLog 6008/Kernel-Boot 簇是崩溃重启序列

**任务鲁棒性三层防护（任务线程卡死不再无声无息）**：
1. **日志队列化**（`automation_controller._init_logger`）：GameAutomation logger 只挂 QueueHandler，写盘/UI 转发由 QueueListener 消费线程执行。盘 IO 卡住只卡消费线程，业务线程 `logger.*` 永不阻塞（冒烟：10 条日志入队 0.3ms vs 同步写 20s）
2. **watcher 读超时**（`services.utils.read_file_with_timeout`）：两套日志监控（script_error_log_monitor / ok_ww_log_monitor）的 open/read 全部改带超时（默认 10s，配置键 `read_timeout`），超时放弃本轮、offset 不退，下轮重试；读线程 daemon 化，挂起不拖累进程退出
3. **任务线程心跳看门狗**（`services/task_heartbeat_watchdog.py`）：`_monitor_game` 循环每轮 `beat()`，超过 `task_heartbeat_timeout_sec`（默认 600s，远大于 CHECK_INTERVAL=10s）无心跳 → dump 全线程栈到 watchdog.log + 置 stop_current_task + 杀进程树。与 hang_watchdog（只盯主线程）互补——**主线程 QTimer 照常心跳时任务线程卡死的场景以前完全无感**
- 已验证：入队不阻塞 / 读超时返回 / 看门狗触发+栈落盘 / 持续 beat 不误触发，全部 PASS
### 脚本报错日志监控（ok- 系 pyappify 脚本）(重要)

- **pyappify 壳脚本有两套日志目录，监控必须指对**：
  - 外壳日志：`<app>/logs/app.*`（只有启动阶段内容，**python 子进程报错不在这里**）
  - python 子进程日志：`<app>/data/apps/<name>/working/logs/ok-script.log`（**常驻主日志，无日期后缀**，报错都在这里）
- watcher 生效需**三个条件同时满足**（2026-10-15 终末地实例全部缺失，导致脚本 09:32 异常后空转 7 小时到蓝屏）：
  1. `_discovery.monitors.<name>` 或顶层 `<name>` 段有 `abort_on_error: true`（`build_script_error_watchers` 只构建有该键的 watcher）
  2. `log_dir` 指向 `working/logs`，`file_pattern` 为 `ok-script*.log`
  3. `require_date_token: false`——`_matching_files` 默认要求文件名含当天日期，`ok-script.log` 无日期后缀会被排除。误判防护靠 watcher 构建时的 `_initialize_offsets` 记录当前偏移，只读启动后新增行
- `error_keywords` 与旧配置字段 `error_markers` 等价（build 已兼容两个名字），别再造第三种命名
- ok-ef 的 `ensure_main(time_out=600)` 超时会抛 `Please start in game world and in team!`，关键字必须包含它
### OCR 防护两条铁律 (重要, 2026-10-17)

**背景事故**：2026-10-17 09:59-10:03 XM 流程整体失败。窗口不可用 → OCR 安静回退全屏 → 记事本里开着的 `auto_runner.log` 被拍进截图 → OCR 把日志文字当成「进入脱机」假命中（0.87）→ 点到记事本上 → 确认失败 → 步骤重试到耗尽。即注释里的"自己干扰自己"。

- **铁律 1：窗口不可用不许回退全屏**。`runtime/ocr_click_tester.py` 的 `ocr_window_content(logger, hwnd, region)`：传了 hwnd 但 `_ensure_window_region` 返回 None（最小化/已关闭）→ 先 `_restore_window_if_minimized()` 尝试 SW_RESTORE 还原；仍失败 → 打 ERROR（30s 节流）+ **返回 `[]`**。上层按"未匹配"重试，`run_flow` 的 `_refresh_flow_window` 会重新枚举窗口——比全屏瞎认正确。区域优先级不变：显式 `region` > hwnd 窗口区域 > 全屏（仅 hwnd=None 且 region=None 时才真全屏）。
- **铁律 2：日志污染硬拦截，不只告警**。`_check_log_contamination(logger, results) -> List[Dict]` 现在**返回剔除后的结果**：命中 `_LOG_SIGNATURE_PATTERNS` 的 OCR 条目一律剔除、禁止参与匹配。阈值策略：≥2 条才节流告警（防噪音）；**任何 1 条命中即剔除**（防漏网）。目标文字都是界面标签（进入脱机/登录/开始），不含日志特征模式，剔除不会误杀目标。
- 回归测试：`artifacts/test_ocr_guard_20261017.py`（7 个用例：干净零误剔 / 污染剔除 / 单条也剔 / 死窗口不抓屏 / 还原成功后继续 / 显式 region 优先）。改这块代码后必须跑。
- 模板匹配路径 `_locate_template_in_window` 本来就是 `rect=None → return None`，没有全屏洞，别改出洞来。
### OCR 结构化匹配：不要靠堆排除词 (2026-10-31)

**背景**：MiniMax 步骤4 目标「签到得」按旧逻辑（目标顺序 + OCR 顺序，先命中先返回）子串命中卡片宣传语「连续签到得更多积分」，点在文案中间，接口日志里根本没有 `signin/claim`。当天先加了 `ocr_exclude_texts`，但每遇到一句宣传语就要加一个排除词，不可持续。

- **匹配质量分层**（`runtime/ocr_click_tester.py` 的 `_rank_ocr_candidates`）：精确 > 前缀 > 子串 > OCR 漏字反向；同等级再按锚点距离、置信度、目标顺序排。`ocr_find_any_text` 默认仍是 `legacy`（旧行为，避免影响未迁移流程），步骤/流程配 `ocr_match_mode: "smart"` 才启用排序；`exact` / `prefix` / `substring` 是更严格的子集。
- **锚点与几何**：步骤字段 `ocr_anchor`（字符串 / 列表 / `{"texts": [...], "required": bool}`）+ `ocr_near`（`direction: below|above|near`、`max_distance`、`max_dx`、`min_dy`）。先找锚点，再只接受满足方向/距离的候选；锚点找不到时默认回退全量，`required=true` 则直接判未匹配。`ocr_near` 没有 `ocr_anchor` 会被忽略并只告警一次。MiniMax 用「连续签到」锚点 + `direction=below, max_distance=500`，按钮在文案下方，宣传语与锚点同点被方向过滤。
- **歧义**：同等级、同分数附近且相距 >40px 的多个候选会打 `⚠️ 候选歧义` 告警并保存标注图；默认仍按分数选，`ocr_ambiguous: "fail"` 可改成宁可不点。**禁止**用"多候选就随机/瞎点"兜底。
- **候选标注图**：`ocr_debug_snapshot: true`（步骤级）或 `GAMEAUTO_OCR_DEBUG=1`（全局）时，把当帧所有 OCR 框、状态（chosen/anchor/excluded/near_rejected/...）、tier、距离、分数画到 `data/artifacts/ocr_debug/ocr_match_<tag>_<时间>.jpg`；同一 tag 5 秒内最多一张，歧义时强制保存。未命中也会存图，先看图再改配置。
- **`_ocr_scan` 数据缝**：单次 OCR 返回 `(结果, 实际截图, 区域)`，`box` 是截图局部坐标、`center` 已换算屏幕绝对坐标；`ocr_window_content` 仍是只返回结果的薄包装，旧调用方不受影响。测试要 mock 扫描时 patch **`_ocr_scan`**，只 patch `ocr_window_content` 拦不住 `ocr_find_any_text`。
- **已迁移**：`flow_configs(.example).json` 的 MINIMAX / WORKBUDDY 流程级 `ocr_match_mode: "smart"`；MiniMax 步骤4 锚点几何 + 标注图，WorkBuddy 步骤2/6/7（导航与「今日已领取」验证）开标注图。其他流程保持 legacy，验证稳定后再逐步迁移。
- **范围限制**：anchor/near 只作用于步骤的首次匹配（click_template / wait_template）；`confirm_type=disappear/text_change` 仍走旧的全量确认逻辑，目前迁移的两个流程都用 `confirm_type=none`，不冲突。
- 回归：`tests/suite_ocr_structured_match_20261031.py`（legacy 撞词复现 / smart 前缀优先 / 锚点方向距离 / required / exact/prefix/substring/reverse / 排除词 / 歧义与 fail / 标注图与节流 / runner 透传 / 两份配置迁移）；旧 `tests/suite_minimax_ocr_exclude_20261031.py` 已改为 patch `_ocr_scan`。
### 流程结束关闭进程 (重要)

- `runtime/foreground_flow_runner.py` 的 `close_flow_processes(logger, flow_key, pid, process_names)` 是统一收口：先按 PID 杀**整棵进程树**（`ProcessSupervisor.safe_kill_process`），再按 `window_process_names` **按名兜底**清残留。
  - Electron 类应用（TRAE SOLO / MiniMax Code / WorkBuddy）由启动器拉起子进程，只 `terminate()` 启动 PID 会留下真实窗口进程 —— 必须按名兜底。
  - **自身保护**：`_protected_pids()` = 当前进程 + 全部祖先 PID，绝不在清理时自杀（WORKBUDDY 流程与 GUI 宿主同名，靠这个保护）。
- `run_flow` 用 `try/finally` 保证收尾：`close_process_on_finish=true` 时成功/失败都关；`close_process_on_fail=false` 可只在失败时保留进程便于排查。
- **例外**：`stop_after_step` 提前返回（XM 后台流程跑到"运行中确认"）**不会**关闭进程 —— 挂机类流程要继续跑。
- 薅羊毛集合（`background_template_runner.run_wool_background`）对每个子流程额外兜底关闭，默认关闭、`close_process_on_finish=false` 才保留，日志尾缀 `（已关闭程序）` / `（程序保留）`。集合含 `WOOL_FLOW_KEYS = MINIMAX / WORKBUDDY / TRAE / QODER`。
- **假成功事故（2026-10-21 WorkBuddy，用户怒点）**：`optional_if_missing: true` 的步骤找不到元素时**直接跳过并报成功**——WorkBuddy 流程 11 秒没等到「立即领取」按钮（单实例应用，用户旧会话的加油站弹窗早已自动消失）→ 跳过 → 报 ✅ 实际没领。**规则：optional 的点击/等待步后面必须跟一个非 optional 的验证步**（确认达成态，如 MINIMAX 的「今日已签到」二步结构），否则弹窗不在时无法区分"已领取"和"没领到"。
- **用户正在用的软件：默认不关，但尊重显式勾选**：WorkBuddy 是日常聊天软件（GUI 还常从其进程树外启动，`_protected_pids` 护不住），默认 `close_process_on_finish/fail=false`；TRAE/QODER 是同类 IDE，默认建议 false。**但用户可以在 GUI 薅羊毛页显式勾选「完成后关闭」，勾选后必须执行**——2026-10-30 用户明确要求关 WorkBuddy/Qoder，曾加过 `PROTECTED_CLOSE_FLOWS` 硬保护并锁 QML，已按要求撤掉。禁止再用代码拦截用户的显式关闭选择。
- **弹窗是自动轮换的展示卡（2026-10-21 用户截图三连修正）**：WorkBuddy「Buddy加油站 每日可领 100 积分」弹窗为左下角**轮换 carousel**，至少三张卡（加油站→积分包购买→做任务赚积分），所以「立即领取」按钮**只有轮到的那个间隙可见**。展示一段时间后会整体退场，**悬停不唤回、二次启动不重弹**（单实例）。靠它领不可靠——用户提的"延迟太高点错"在轮换场景下是真实风险（匹配→点击间隙卡片转走），仅靠确认失败重试兜底不够。
- **常驻入口（2026-10-21 用户指点，流程已重构走这条）**：**左下角头像 → 菜单「Buddy加油站」→ 加油站页面**（可领时显示「立即领取」，已领显示「今日已领取」，积分余额在头像菜单里可见）。头像菜单任何时候都能打开，不依赖轮换弹窗。流程七步：点头像（`assets/templates/hy4-左下角头像.png`，OCR 关）→ 点菜单行（`assets/templates/hy4-Buddy加油站菜单.png`，OCR 兜底）→ 等「立即领取」（optional）→ 点领取+确认（optional）→ **再点头像（optional）→ 再点菜单行（optional）→ 确认「今日已领取」（非 optional，导航失败就老实 ❌）**。
- **领取后加油站页面会整个关闭（2026-10-27 实锤，假失败事故）**：10-27 流程点完「立即领取」（领取成功）后页面退回底下的聊天窗口，验证步在 74 秒里扫了 15+ 轮都找不到「今日已领取」→ 判 ❌，用户实看已领取。修复即在点击与验证之间插两个 optional 重新导航步——页面还开着时头像被盖住、超时跳过，验证照常命中；页面关了则重新进站再看。**不能**删掉非 optional 验证步来"修"这个问题（会退回 10-21 假成功事故）。
  头像模板是小图标、与窗口大小无关；用户换头像后模板失效（诚实失败，重裁即可）。回归 `artifacts/test_wool_workbuddy_20261021.py`（25 组，七步结构），参考截图固化在 `artifacts/ref_workbuddy_*_20261021.png`。
- **挂机类流程禁止开启关闭**：`CJ`（重聚脱机）是挂机流程，跑完必须继续运行，`close_process_on_finish` 与 `close_process_on_fail` 均为 `false`，配置里已加 `_comment` 标注，不要改。XM 走 `stop_after_step="运行中确认"` 提前返回，同样不关。
### UIPI 权限护栏（2026-10-20 白荆实测，重要）

- **现象**：目标游戏以**管理员**运行时，普通权限进程的所有合成点击（pyautogui/SendInput/mouse_event）被 Windows **静默拦截**——鼠标会动、游戏没反应，流程表现为"每步匹配超时/未匹配模板"，极易误判成 UI 改版或坐标失效。
- **根因**：UIPI（User Interface Privilege Isolation）阻止低完整性进程向高完整性进程窗口发送输入。
- **解法**：本程序必须同样以管理员运行（`start_gui_admin.bat` / `00_START_HERE_GUI_ADMIN.bat` 自带 `Start-Process -Verb RunAs` 自提权）。GUI 设计上就是提权跑的；手动跑脚本验证时也要提权（UAC 弹窗需用户点"是"）。
- **护栏**：`foreground_flow_runner._uipi_block_reason(pid)`（`_process_is_elevated` 读 TokenElevation）在 `run_flow` 窗口就绪后预检——仅"本程序普通 + 目标管理员"才拦，报可行动的错并直接失败，不再让步骤匹配超时把问题带偏。回归：`artifacts/test_uipi_guard_20261020.py`（8 组权限组合）。
- **排查手法**：`psutil` + `OpenProcessToken`/`GetTokenInformation(TokenElevation=20)` 比对两进程权限；提权后同坐标点击立即生效即可实锤。
- **UIPI 只护 `run_flow` 路径，白荆 `execute_task` 路径没有预检（2026-10-25 实锤）**：用非提权驱动跑白荆（artifacts 单游戏驱动脚本），启动器「开始游戏」能点中（启动器是自己拉的非提权进程），但游戏本体（腾讯反作弊 TQMCenter 全家桶，提权）之后**所有点击/ESC 全被静默拦截**——"已点击关闭公告"打了日志但弹窗纹丝不动，常态匹配 ESC 按 10 次全无效。日志特征签名：`SetWindowPos 置顶失败: (5, 拒绝访问)` + `_terminate_process` 一片"终止进程异常"（OpenProcess 被拒）。**白荆/游戏内流程的任何驱动（CLI、测试、临时脚本）必须和 GUI 一样提权跑**；诊断时看到"点击日志正常但画面不变"先查权限再查模板。
## SQLite / GUI 卡死避坑 (重要)

- **WorkflowDB 必须用 WAL**：`storage/workflow_db.py` 构造时已统一执行 `PRAGMA journal_mode=WAL`。默认的 rollback journal 模式下写事务独占全库，多视图后台线程并发访问时读者全被阻塞 —— 实测 10 线程并发最慢单次 1.88s，开 WAL 后降到 0.55s。**不要**改回 DELETE 模式。
- **`_connect()` 是 `@contextmanager`，不是普通方法**：它 `yield` Connection 并在 `finally` 里 `close()`。调用点保持 `with self._connect() as conn:` 即可自动提交 + 关闭。**禁止**改成 `return conn` 的普通函数（历史 bug：只提交不 close，导致连接与文件句柄持续泄漏）。**禁止**写成 `conn = db._connect()` 裸调用。
- **GUI 卡死定位看 `logs/watchdog_main_thread.log`**：`services/hang_watchdog.py` 由主线程 `QTimer` 2s 心跳驱动，主线程冻结超 15s 时由独立 daemon 线程 `faulthandler.dump_traceback(all_threads=True)` 转储全线程栈。下次报"界面卡死"先读这个文件，能直接定位到代码行。
- **看门狗栈文件按故障类型分家**（2026-10-18）：两类硬故障原先混写 `logs/watchdog.log`，事后只能靠时间戳猜。现在 `settings/paths.py` 两个常量对应两个文件：
  - `WATCHDOG_MAIN_LOG_FILE` = `logs/watchdog_main_thread.log` ← `hang_watchdog`（主线程无心跳，`app/gui.py` 构造）
  - `WATCHDOG_TASK_LOG_FILE` = `logs/watchdog_task_heartbeat.log` ← `task_heartbeat_watchdog`（任务线程无心跳，`automation_controller` 构造）
  两个 dump 文件都受 `MAX_DUMP_BYTES`（5MB）保护，超了归档为带时间戳的副本（不删除历史）。**别再把新看门狗的 dump 指回单一 watchdog.log**，也别改文件名常量而不更新本条笔记。
- **多视图并发是常态**：`Main.qml` 每个 Loader 都是 `active: ... || xxEverLoaded`（切过即永不卸载），所有切过的视图后台线程仍在跑。新增视图的后台线程**必须**是 daemon 线程 + 网络请求带 timeout，否则会拖死主线程。
- **不要误判为网络问题**：IO 等待会释放 GIL，不会阻塞主线程。GUI 冻结的真正嫌疑是「后台线程长时间占 GIL」—— 通常是重活 import、CPU 密集计算、或 SQLite 写等待。
## 长等待必须可中断 (重要)

- **流程里的每一处长等待都必须接受停止回调**：`runtime/foreground_flow_runner.py` 的 `run_flow` 启动阶段有两处历史黑洞 —— `time.sleep(startup_wait_sec)` 与 `_wait_window_for_flow(pid, timeout=45)`。后者是纯 `time.sleep(0.6)` 轮询，**不看停止标志**，导致用户点「停止」后最长要等 45 秒才生效，表现为"按钮失灵 → 狂点 → 日志刷屏"（2026-09-11 实例：点了 27 次，40.3 秒后才关进程）。
- **新增等待一律传 `stop_check`**：`run_flow(..., stop_check=...)` / `_wait_window_for_flow(..., stop_check=...)` / `_wait_window_by_pid(..., stop_check=...)` 已支持；流程内用 `_sleep_interruptible(seconds, stop_check)` 与 `_stop_requested(stop_check)`，轮询粒度 0.25s。调用方传 `lambda: controller.stop_current_task`。
- **`stop_check=None` 必须保持旧行为**（向后兼容，勿改成必填）。中断时的收尾仍由既有 `close_on_exit` / `close_on_fail` 决定 —— 不要为"停止"另开一套关进程规则，否则会破坏 CJ 的"禁止自动关闭"。
- 仍未接线：`run_wool_background` / `_run_monthly_flow` 内部 `run_flow` 未传 `stop_check`（签名无 controller），其 45s 窗口等待仍不可中断。
### 失败诊断记录 (重要, 2026-10-18)

**为什么要做**：改动前"步骤没匹配上 / 任务失败"只落 `logs/auto_runner.log` 的 warning
自由文本 —— 没有截图、没法按流程/步骤聚合、跨天回看只能全文 grep 日志；
`workflow_runs` 表字段齐全却**零调用**（恒空），`daily_progress_errors` 只记 API 查询失败；
`artifacts/ocr_debug/` 恒空（唯一写它的入口属于已停用的 Flet 测试路径）。

- **一站式入口**：`runtime/flow_diagnostics.record_failure(logger, flow_key, step, error,
  hwnd=, game_key=, retry_count=, ocr_results=, ocr_sample=, save_image=, extra=)`
  → ① 抓失败当刻窗口截图 `artifacts/flow_failures/{时间}_{流程}_{步骤}.jpg`（宽 960 / JPEG q72，约 50KB）
  → ② 抓当帧 OCR 文本（`文字(0.90)@x,y` 形式，最多 15 条）
  → ③ 写 `workflow.sqlite3` 的 **`flow_failures`** 表（结构化：ts/date_key/flow_key/game_key/
  step/error/retry_count/hwnd/window_title/image_path/ocr_texts/extra_json）
  → ④ 按 mtime 清 14 天前截图（`FLOW_FAILURE_KEEP_DAYS`，同进程 10 分钟节流）
- **DB 侧**：`WorkflowDB.record_flow_failure / query_flow_failures / summarize_flow_failures /
  purge_old_flow_failures`。建表用 `CREATE TABLE IF NOT EXISTS`，老库首次打开自动迁移，无版本号。
- **埋点（新增失败务必接）**：`foreground_flow_runner.run_flow` 步骤重试耗尽（模板/OCR 未匹配）、
  `task_batch_runner._run_single_task` 状态为 failed、`baijing_daily._record_step_failure`
  （白荆俱乐部每日任务 / 每日外勤赠礼失败）。
- **三条铁律**：
  1. **永不抛异常** —— 调用点都在失败分支里，诊断自己炸了必须被吞掉（连 logger 抛异常也不能
     把已写库的 id 吞掉）；
  2. **不重复识别** —— 调用方已有当帧 OCR 结果时务必传 `ocr_results` 复用，一次失败重试动辄
     几秒，别为了记录再跑一遍 RapidOCR；
  3. **窗口不可用不退化全屏** —— 传了 hwnd 但 `GetWindowRect` 失败就直接不存图，否则会把用户
     桌面截图存进证据目录（与 `ocr_click_tester` 的全屏 OCR 防护同源教训）。
- **出报告**：`tools/flow_failure_report.py`（`--days 7 --top 20 --verbose --json --purge 30
  --flow BaiJing`）。按 (flow_key, step) 聚合次数 → "哪一步最常失败、最该优化"，
  附最近一次时间、是否有证据截图、样例错误、失败当帧屏幕文字与图片路径。
- **GUI/WebUI 入口**：`DailyView.qml` 底部「最近失败记录」卡片（bridge
  `getRecentFlowFailures(days) -> str` JSON，后台线程读库最多等 3s；截图缩略图点击
  `openFlowFailureImage` 开系统看图片、`openFlowFailureDir` 开证据目录）；WebUI
  `GET /api/daily/failures?days&limit&flow` + `GET /api/daily/failure-image/{name}`
  （`webui/static/app.js` 的 `loadFailures()`）。**`getRecentFlowFailures` 必须写
  `@Slot(int, result=str)`**，漏 `result` 时 QML 拿到 undefined 且不报错。
- **脚本级失败没有截图**：`task_batch_runner` 埋点拿不到 hwnd（进程已退出），只记文字；
  `capture_failure_image` 传了 hwnd 但 rect 取不到时**直接不存图**，绝不退化成全屏截图。
- 回归：`artifacts/test_flow_diagnostics_20261018.py`（落库字段/查询过滤/聚合/清理/报告渲染/
  不抛异常/埋点仍在/三个 bridge slot 的 MetaObject 返回类型，共 48 项断言）。
### pyautogui fail-safe (重要)

- 本版本 pyautogui 的 fail-safe 触发条件是**鼠标位置精确等于 (0,0)**（`FAILSAFE_POINTS = [(0,0)]`），
  不是"角落 100px 区域"。鼠标顶到屏幕左上角 = 用户请求紧急停止，pyautogui 拒绝执行。
- **禁止裸调 `pyautogui.click`**：一律走 `runtime/foreground_flow_runner.py` 的
  `_pyautogui_click_guarded(x, y, clicks, interval, button)`。它把 FailSafeException
  转成带中文说明的 `FailSafeAborted`，由 task_batch_runner 收口成优雅失败
  （`should_stop_batch=True`，不重试不继续），而不是 traceback 崩溃。
  2026-09-29 CJ 事故：窗口贴屏幕左上角 + 用户鼠标随手放 (0,0) → 整个任务崩。
- **测试进程难以复现触发**：DPI 虚拟化会让 moveTo(0,0) 落到屏幕中心、
  SetCursorPos(0,0) 后 position() 返回 (1,1)。验证收口逻辑用 mock 抛 FailSafeAborted。
- click_sequence.py 自己管理 FAILSAFE（点击序列暂停期间禁用），不在本约定范围。
### 收尾动作必须 finally 兜底 (重要)

- `ui/bridge.py::_run_automation_backend` 的**邮件、自动关机、每日进度刷新、
  WebUI 状态复位、finished/running 事件全部放在 finally 里**，禁止移回 try 主体。
- 原因：任何任务内未捕获异常都会穿透 run_task_batch；写在 try 主体里的收尾
  会被整体跳过 —— 2026-09-29 中午事故：CJ 抛 FailSafeException，
  shutdown_on_finish=true 却没关机、邮件也没发，用户挂机跑完机器整夜没关。
- 异常路径会往报告追加 `❌ 任务异常中断（收尾兜底已执行）: <异常>`，
  邮件照发、关机照调，UI 弹"运行失败"提示。
- 验证方式：mock `task_batch_runner.run_task_batch` 抛异常，
  断言 `_schedule_shutdown` / `_send_daily_email_report` 各被调用一次、
  finished 事件发出、running 复位为 False。
