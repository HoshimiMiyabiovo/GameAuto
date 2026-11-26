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
2. **watcher 读超时**（`services.utils.read_file_with_timeout`）：四套日志监控（script_error_log_monitor / ok_ww_log_monitor / bettergi_log_monitor / whimbox_log_monitor）的 open/read 全部带超时（默认 10s，配置键 `read_timeout`），超时放弃本轮、offset 不退，下轮重试；读线程 daemon 化，挂起不拖累进程退出。2026-11-09 审计发现 bettergi/whimbox 两套漏接（裸 read 在坏盘 IO 时钉死监控线程——原神 fail-closed 必败、暖暖完成回调丢失），已补齐。**新增第五套监控时必须同样接 `read_file_with_timeout`**
3. **任务线程心跳看门狗**（`services/task_heartbeat_watchdog.py`）：`_monitor_game` 循环每轮 `beat()`，超过 `task_heartbeat_timeout_sec`（默认 600s，远大于 CHECK_INTERVAL=10s）无心跳 → dump 全线程栈到 watchdog.log + 置**任务级** `watchdog_aborted_task` + 杀进程树。与 hang_watchdog（只盯主线程）互补——**主线程 QTimer 照常心跳时任务线程卡死的场景以前完全无感**
- 已验证：入队不阻塞 / 读超时返回 / 看门狗触发+栈落盘 / 持续 beat 不误触发，全部 PASS
### 看门狗止损只管当前任务，不许连坐整批 (2026-11-16 实跑事故)

- **事故**：鸣潮 ok-ww 启动调用卡在 ShellExecuteW，看门狗 606s 无心跳止损；因为回调直接写批次级 `stop_current_task=True`，`task_batch_runner` 把鸣潮判成 `state=stopped`（用户终止）并在 `should_stop_batch` 分支静默 break——后面的终末地/暖暖/白荆/薅羊毛/异环/CJFlow 六个游戏一个没跑，统一重试也被 stop 标志挡掉。日志里甚至没有"用户已请求停止"，因为 break 发生在下一轮循环检查之前。
- **口径**：`stop_current_task` = 用户主动停止/FailSafe，才是整批语义；看门狗改置 `AutomationController.watchdog_aborted_task`，控制器内部所有等待/停止检查统一走 `_task_abort_requested()`（用户停止 OR 看门狗），白荆 `baijing_flow._stop_and_beat` 同样透传。
- **收口**：`_run_single_task` 在单任务结果回来后调 `_settle_watchdog_abort`：清掉任务级标志、强制本任务 `failed`、把 execute_task 可能因任务级中止返回的"用户终止/快捷键"改成"看门狗止损"，使 `stopped=False`。这样后续任务照跑、失败项进入统一重试；用户同时点停止时 `stop_current_task` 保留，整批仍停。
- 新批次入口会同时清两个标志，避免上批异常退出遗留。回归 `tests/suite_watchdog_task_scope_20261116.py`（5 组行为测试，含真 `_run_single_task` 路径）。
### 启动调用必须能被超时打断，不许把整批钉死 (2026-11-26 实跑事故)

- **事故**：无限暖暖 whimbox 的启动器是 `run_one_dragon.bat`，`_launch_normal` 用
  ShellExecuteW 拉起。09:33:26 调用进去后再没返回，任务线程整段卡死；心跳看门狗
  09:43 判定 625s 无心跳、dump 栈后只杀掉了游戏进程（`X6Game`），**杀不掉那个卡在
  Win32 调用里的线程**。12:38 电脑解锁后该调用返回 5（SE_ERR_ACCESS_DENIED，疑似
  安全警告/提权弹窗被取消），任务记「启动失败」；期间整批后续的白荆回廊/薅羊毛/CJ
  与 10:01 的 Qoder 定时任务一个都没跑（10:01 那条当时已排队、`last_fired_date`
  已写当天，随进程退出直接丢失）。
- **口径**：① `.bat/.cmd` 一律 `cmd /c` + `subprocess.Popen`（列表参数、
  `CREATE_NEW_CONSOLE`），不再经 shell 关联；`.py` 走当前解释器；`.exe` 直连
  CreateProcess；只有 `.lnk`/网址才回退 ShellExecuteW。② 所有启动调用统一进
  `_call_with_timeout`（`LAUNCH_CALL_TIMEOUT_SEC`，默认 60s），超时按本任务启动
  失败收口、**批次继续跑后面的游戏**；卡住的旁路线程留 daemon，迟到的返回值只记日志。
- **不许**在任务线程上直接调 ShellExecuteW / CreateProcess 而不设超时——这类 Win32
  调用被系统弹窗挂住时既不抛异常也不返回，Python 侧没有别的办法收口。
- special 启动器（`_launch_special`）走同一套，`hidden=True` 保持「不起控制台」的旧观感。
- 回归 `tests/suite_launch_timeout_20261126.py`（6 组：分派 / 有界调用 / 超时收口 /
  cmd 参数 / shell 回退 / special）；`suite_motw_clear_20261116` 的 exe 分支断言改成行为口径。

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
- ok-ap 的致命标记认 `任务失败标记 |`、`以下任务未完成或失败`、`exception stopped`、
  `DailyTask_Exception`、`Failed to ensure main.`；日报另读
  `%TEMP%/ok-ap/一键日常/一键日常_*.txt`，只有「完成」且无失败项才算成功。
### OCR 防护两条铁律 (重要, 2026-10-17)

**背景事故**：2026-10-17 09:59-10:03 XM 流程整体失败。窗口不可用 → OCR 安静回退全屏 → 记事本里开着的 `auto_runner.log` 被拍进截图 → OCR 把日志文字当成「进入脱机」假命中（0.87）→ 点到记事本上 → 确认失败 → 步骤重试到耗尽。即注释里的"自己干扰自己"。

- **铁律 1：窗口不可用不许回退全屏**。`runtime/ocr_click_tester.py` 的 `ocr_window_content(logger, hwnd, region)`：传了 hwnd 但 `_ensure_window_region` 返回 None（最小化/已关闭）→ 先 `_restore_window_if_minimized()` 尝试 SW_RESTORE 还原；仍失败 → 打 ERROR（30s 节流）+ **返回 `[]`**。上层按"未匹配"重试，`run_flow` 的 `_refresh_flow_window` 会重新枚举窗口——比全屏瞎认正确。区域优先级不变：显式 `region` > hwnd 窗口区域 > 全屏（仅 hwnd=None 且 region=None 时才真全屏）。
- **铁律 2：日志污染硬拦截，不只告警**。`_check_log_contamination(logger, results) -> List[Dict]` 现在**返回剔除后的结果**：命中 `_LOG_SIGNATURE_PATTERNS` 的 OCR 条目一律剔除、禁止参与匹配。阈值策略：≥2 条才节流告警（防噪音）；**任何 1 条命中即剔除**（防漏网）。目标文字都是界面标签（进入脱机/登录/开始），不含日志特征模式，剔除不会误杀目标。
- 回归测试：`artifacts/test_ocr_guard_20261017.py`（7 个用例：干净零误剔 / 污染剔除 / 单条也剔 / 死窗口不抓屏 / 还原成功后继续 / 显式 region 优先）。改这块代码后必须跑。
- 模板匹配路径 `_locate_template_in_window` 本来就是 `rect=None → return None`，没有全屏洞，别改出洞来。
### OCR 引擎：onnxocr v6 单档（2026-11-07 晚定稿）

**背景**：XM「等待数据加载完毕」的目标字打在脚本右下 10px 小日志面板（灰字+花纹水印），老引擎全窗扫描必漏——检测器对大图降采样（limit_side_len 736）+ 密集 UI 小框竞争；用户提供原生截图实测，紧裁剪勉强能读但字符级噪声大。

- **单档架构**（`runtime/ocr_click_tester.py`）：唯一引擎 `_get_onnxocr_engine()` = onnxocr PP-OCRv6（CUDA）。RapidOCR（v3 mobile）与 v4 server / v5 onnxocr 已于 2026-11-07 全部删除——基准实测 v3 全窗漏检、v4 慢（窗口 1019ms）、v5 det box_thresh=0.6 漏检；v6 全窗命中且最快（窗口 239ms / panel 15ms，相似度 1.00）。Tesseract 仅作可选兜底。
- **步骤 opt-in**：`"ocr_precision": true` 步骤走高精度链（`_locate_with_ocr_fallback` → `ocr_find_any_text` 透传 `engine=`）；失败取证同步骤也用高精度引擎。引擎不可用时 `_get_onnxocr_engine` 返回 None，上层按未匹配重试。
- **CUDA 依赖**：onnxruntime-gpu + NVIDIA 运行库 pip 轮子（cublas/cudnn9/cufft/curand/cusolver/cusparse/cuda-runtime/nvjitlink，共约 2.5GB DLL）。`_ensure_nvidia_cuda_dlls(force=True)` 把 site-packages/nvidia/*/bin 加进 DLL 搜索路径 + PATH（必须在 session 创建前；CUDA EP 缺 DLL 会**静默回退 CPU**）。全局 env 门控 `GAMEAUTO_OCR_CUDA=1`。
- **显存错峰**：v6 走 CUDA EP 单进程常驻 ~730MB（nvidia-smi 实测）。GUI 常驻但 OCR 只在批次任务时用，`run_task_batch` 收尾调 `_release_onnxocr_engine()` 卸载引擎（断引用 + gc），与 ok-ww（鸣潮）/ok-ef 等独立 GPU 进程错峰；下次批次首次 OCR 自动重建（冷启动 ~450ms）。实测释放回落 ~81%、残留 ~137MB（onnxruntime CUDA 显存池固有，Python 侧无法完全消除，要 100% 归还需子进程隔离）。
- **基准**（`tools/bench_ocr_engines.py`，RTX 5070，XM 窗口 901×610，预热后 best of 3）：v6 窗口 239ms 命中（整行 1.00）、panel 15ms。结果 `data/artifacts/ocr_bench_20261107.json`。
- 回归：`tests/suite_onnxocr_precision_20261107.py`（vendored 无 one_dragon 残留 / v6 命中与格式 / CUDA EP / 工厂单档 / runner 接线）；`suite_step_ocr_region_20261107.py` 锁 v6 端到端命中与区域换算。
### OCR 空格拆字：子串匹配前先去空白归一化（2026-11-08 XM 实锤）

- **事实**：onnxocr v6 会把部分英文标签**稳定**拆开——"Unity" 两天三次取证都是
  "Uni ty"（11-07 10:37 conf 0.78、11-08 08:17 conf 0.96），不是偶发抖动；同帧其余
  标签（Flash/精灵因子/巅峰圣战…）全部正常。中文标签未见此现象。
- **规则**：拿 OCR 文本做**子串包含类**匹配（标签页/目标词/锚点）时，两边先
  `_norm_text`（去空白+去截断省略号）再比，原始子串匹配会在稳定拆字上永远落空。
  `_rank_ocr_candidates` 的分级匹配有漏字反向兜底不受影响；这里是手写子串的地方，
  别照抄原始 `in`。
- **事故**：2026-11-08 08:17 XM 步骤 6「同步限时活动勾选」（清单同步功能首跑），
  `script_checklist._click_tab` 匹配不到 "Uni ty" → 流程失败终止，脚本本体登录/页面
  全正常。修复后实锤帧正常命中并按顶部坐标点击。
- 回归：`tests/suite_script_checklist_tab_20261108.py`（实锤帧复现 / 精确文本 /
  多候选取 y 最小 / 无匹配不点击 / 静态防退化）。
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
### 流程窗口选择：置顶小窗劫持 (2026-11-17)

- **现象**：薅羊毛 QODER 步骤 1「打开权益中心」连败两轮，失败截图分别拍到 CrystalDiskMark / 桌面 / 黑屏（用户问「这是点到哪里去了」）。实锤是日志里的搜索区域 `region=(1768, 928, 51, 79)`——选中拿来识别的是 **Qoder Desktop Pet**（右下角 51×79 的置顶宠物窗），不是 Qoder 主窗口；在 51×79 区域里找 `qoder-权益中心.png`，模板图比整个区域还大，报 `needle dimension(s) exceed the haystack image or region dimensions`，重试 16 次全败。这三个步骤**都没有产生有效点击**（等待型步骤不点；点击型步骤在 `_ensure_visible` 就放弃）。同批 TELEAGENT/AStudio 报「目标窗口被遮挡约 100%」、TRAE 被 `'Agent [管理员]'` 遮挡，都是窗口选择/前台归属的同族问题。
- **根因**：`foreground_flow_runner._choose_best_window_for_flow` 旧排序 `(是否前台, 标题关键词, 面积, 当前句柄)` 把「前台」放第一位。置顶宠物窗 / 通知气泡 / 小部件长期占据前台，于是稳定胜出，面积只在第三顺位救不回来。`_find_window_by_filters`（跨进程按进程名/标题找窗口）同款排序同病。
- **修法**：统一 `_flow_window_sort_key` = `(标题关键词, 面积, 前台, 当前句柄)`；并在**关键词过滤之前**先过 `_filter_flow_window_candidates` 尺寸门槛（`MIN_FLOW_WINDOW_WIDTH/HEIGHT` = 200×120，小于阈值视为宠物/气泡/工具窗；全是小窗时原样回退，迷你模式应用不会没窗口可用）。**顺序关键**：反过来先按关键词过滤的话，「标题含关键词的宠物窗」会把不含关键词的真正主窗挤出候选，门槛白设。
- **排查手法**：`flow_failures.window_title` + 失败日志里的 `region=(left, top, width, height)` 能直接看出选中的是不是主窗口——客户端主窗口至少几百像素，出现个两位数宽高必是宠物/气泡/工具窗。
- 回归：`tests/suite_flow_window_pick_20261117.py`（尺寸门槛纯函数 / 事故复现 / 先尺寸后关键词 / 双大窗取面积 / 只有小窗回退 / 关键词优先 / 句柄稳定性 / `_find_window_by_filters` 同源）。

### 同 PID 模态弹窗不得算遮挡（2026-11-18 #431）
- **事故**：鸣潮 WuwaLauncher 11:37 启动官方启动器后弹「启动器版本更新」模态框（独立顶层窗 `krupdatereminderdlg`，同 `launcher_main.exe` PID）。`_window_obscured_ratio` 按 `WindowFromPoint` 归属采样，把同 PID 弹窗全部算成遮挡 → 比例 100% → OCR 防护拒绝识别；同时该窗口标题 `krupdatereminderdlg` 不含 `window_title_keywords`，`_find_window_by_filters` 也按归属不符跳过。于是步骤1 扫不到「更新」按钮，步骤3 点「进入游戏」被模态框吞掉，步骤4 等进程空转 40 分钟。
- **口径**：遮挡防护的目标是拦截**其他进程**窗口的文字污染；目标应用自己的模态/提醒窗属于同一应用内容，必须放行。`_window_obscured_ratio` 对 `WindowFromPoint` 命中窗口与目标窗口 PID 相同的情况不计覆盖（`_foreground_click_block_reason` 早在 11-17 已同口径放行同 PID 点击）。
- **配置侧**：标题「启动器版本更新」同样含「更新」，`legacy` 子串匹配会先点标题。步骤配 `ocr_match_mode: "exact"` 只匹配正文按钮；WuwaLauncher 步骤1 已接线。
- 回归：`tests/suite_wuwa_launcher_update_modal_20261118.py`（同 PID/跨进程遮挡比例、exact 避开标题、两份 flow_configs 接线）。

### 应用自己弹的活动遮罩：流程级 overlay_guard 兜底（2026-11-26 WorkBuddy 实锤）

- **事故**：11-26 21:03 WorkBuddy 每日领取，步骤4 点完「立即领取」（21:03:46）后应用弹出
  「WorkBuddy 成长计划」**整屏遮罩**（背景全暗、左侧头像与菜单被压住、卡片右上角只有一个
  不带文字的 ✕）。步骤6「重新点击菜单里的Buddy加油站」纯 OCR 找字，21:03:56→21:05:14
  连续 4 轮只读到遮罩内 22 个文本（WorkBuddy成长计划 / 我的 Buddy / 查看 Buddy图鉴…），
  0 命中；on_fail=retry_prev 回退的步骤5（头像模板）同样被遮罩盖住 → 死循环后记
  失败 **#514**，当晚 WoolFlow 汇总 WORKBUDDY 失败（#516）。证据截图
  data/artifacts/flow_failures/20261126_210514_WORKBUDDY_重新点击菜单里的Buddy加.jpg。
- **为什么步骤级 dismiss_texts 不够**：那套机制要求「弹窗上有字可点」（「今日到账」/「点击屏幕继续」）；
  这种活动遮罩的关闭键是纯 ✕ 图标，OCR 读不到，只能靠按键或模板。
- **口径**：run_flow 每个步骤开始前，若流程配了 **overlay_guard**（流程级，步骤级可覆盖），
  先做一次窗口 OCR，按「归一化后包含」数命中条数；达到 require 条就依次执行 actions
  （key 扫描码按键 / template 点关闭钮 / click_frac 比例点），每执行一条 settle_sec
  后重扫，特征消失即判「遮罩已关」并**继续当前步骤**；全部动作无效则只告警、按既有
  「未匹配 → 重试」路径走，绝不静默改流程状态。不配 overlay_guard 的流程零开销（一次 OCR 都不做）。
- **预算**：max_dismiss 只在**真的识别到遮罩**时才计一次（不是每步都计），否则 7 个步骤各扫一次
  就把预算耗光、真弹遮罩时反而不兜底；关不掉的遮罩最多拖两次，避免每步都白等 settle_sec。
- **✕ 模板怎么来的、为什么只用 1.0 尺度**：assets/templates/hy4-成长计划关闭.png 是从事故截图
  实锤帧裁的 40×40（弹窗右上角 ✕ 圆形钮，中心 (1395,232)@1920×1035）。同帧自匹配 1.0，但
  **0.8/1.25 尺度会以 0.79~0.81 分数命中别处**（(1548,498)），所以配置里把该动作锁成
  scales:[1.0] / confidences:[0.9]——多尺度搜索对这个低对比小圆钮是纯风险。
- **ESC 尚未实机验证**：11-26 晚想复现时 WorkBuddy 拉不起来（桌面正在跑游戏/其他应用），
  ESC 作为第一动作是先验最可能有效的通用手段，✕ 模板是有实锤位置的兜底。**首次实跑要看日志**
  [WORKBUDDY] 遮罩兜底：… / 全屏遮罩已关闭（剩余特征 …）；若 ESC 既没关掉也没副作用，
  日志会接着打 遮罩兜底：点击 hy4-成长计划关闭.png @ (x,y) -> True，两种都失败才告警
  遮罩关闭失败。届时补新的关闭手段只需往 overlay_guard.actions 里加，不用改执行器。
- **顺带堵住「闭眼点模板」（同批问题3）**：步骤4 的 hy4-立即领取.png 是 58×30 的黑色小按钮，
  以前只要模板相似就在 (229,958) 点下去，没有页面上下文校验。新增步骤字段
  **template_require_texts**：模板命中后必须再用 OCR 看到指定页面特征（WorkBuddy 配
  「Buddy加油站 / 每日可领 / 今日已领取」），看不到就丢弃本次命中、改走 OCR 判定
  （OCR 侧另有 ocr_anchor: {texts:[...], required:true} 同口径拦截）。步骤3/4 同时开
  ocr_debug_snapshot，下次再出问题先看标注图再改配置。
- **回归**：tests/suite_wool_overlay_guard_20261126.py（实锤帧 22 条 OCR 命中 3 条特征 / 聊天页
  「做任务赢积分好礼」不误报 / 动作顺序与预算 / settle 被停止 / 模板门控四态 / 双份配置接线）。
- 配置位置：flow_configs(.example).json → WORKBUDDY.overlay_guard（texts / require /
  actions / settle_sec / max_dismiss）。

### 流程结束关闭进程 (重要)

- `runtime/foreground_flow_runner.py` 的 `close_flow_processes(logger, flow_key, pid, process_names)` 是统一收口：先按 PID 杀**整棵进程树**（`ProcessSupervisor.safe_kill_process`），再按 `window_process_names` **按名兜底**清残留。
  - Electron 类应用（TRAE SOLO / MiniMax Code / WorkBuddy）由启动器拉起子进程，只 `terminate()` 启动 PID 会留下真实窗口进程 —— 必须按名兜底。
  - **自身保护**：`_protected_pids()` = 当前进程 + 全部祖先 PID，绝不在清理时自杀（WORKBUDDY 流程与 GUI 宿主同名，靠这个保护）。
- `run_flow` 用 `try/finally` 保证收尾：`close_process_on_finish=true` 时成功/失败都关；`close_process_on_fail=false` 可只在失败时保留进程便于排查。
- **例外**：`stop_after_step` 提前返回（XM 后台流程跑到"运行中确认"）**不会**关闭进程 —— 挂机类流程要继续跑。
- 薅羊毛集合（`background_template_runner.run_wool_background`）对每个子流程额外兜底关闭，默认关闭、`close_process_on_finish=false` 才保留，日志尾缀 `（已关闭程序）` / `（程序保留）`。集合清单以代码为准：`WOOL_FLOW_KEYS = MINIMAX / WORKBUDDY / TRAE / QODER / TELEAGENT / CODEARTS / ASTUDIO / DUMATE / RACCOON`（9 来源，2026-11-22 对齐；本行写于 4 来源时代，后续新增见 07 分篇各节）。**注意这是按进程名批量清理**——目标应用可能正承载本程序（例如商汤小浣熊流程的宿主就是它自己），这类流程必须 `close_process_on_finish/fail=false` + 内建动作按 pid 精确关闭，见 07 分篇「商汤小浣熊 每日打开」。
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
### 绝区零 Unity 启动器：提权 + 按住，两个条件缺一不可（2026-11-04 实机）

- 用户当场开着游戏，做了两次实验：普通权限 `data/artifacts/probe_zzz_click_20261104.py`、
  管理员权限 `data/artifacts/probe_zzz_click_admin_20261104.py`（由
  `Start-Process -Verb RunAs` 启动，结果写 `zzz_click_probe_admin_result.json`，
  前后截图同目录）。窗口 `ZenlessZoneZero.exe` PID 37532 (0,0,1920,1080)，
  OCR 命中「点击进入游戏」@ (961,887)。
- **普通权限**：`_uipi_block_reason(pid)` 实锤「游戏管理员 / 脚本普通权限，
  UIPI 静默拦截所有合成点击」；前台 guarded、前台按住 80ms、后台 PostMessage
  三种全无效，3s 后按钮仍在。截图只有登录页电视宣传画面轮播（再次说明
  背景动画会骗过像素差判据），按钮没动。
- **提权后**：`elevated_self=true` / `uipi_reason=""`，但**瞬时 `pyautogui.click`
  仍然点不动**（guarded 步骤 `button_still_there=true`）；只有 `mouseDown`
  按住 80ms 再 `mouseUp` 才在 3s 内出现「登录成功」+ `UID:21054491`。
- **结论**：11-04 `ZZZ_MONTHLY` 08:10 / 10:10 / 14:25 三次
  「点击进入游戏：元素 10s 未消失」不是模板/坐标/确认策略问题，是
  **权限 + 点击注入**：GUI 必须提权（护栏在 `run_flow:2144`），且 Unity
  启动器按钮必须按住。后台 PostMessage 首发 + 前台瞬时兜底（11-03 的改法）
  两个条件都不满足，所以三次全废。
- **修法**：新增 `foreground_flow_runner._pyautogui_click_held`、
  `_click_window_local(hold_sec=)`、`run_flow` 读步骤级 `click_hold_sec`；
  两份 `flow_configs` 的 `ZZZ_MONTHLY` / `ZZZ_NOTORIOUS_HUNT` 第 1 步改成
  `background_click=false` / `click_fallback=false` / `click_hold_sec=0.08`。
  默认 0 保持其它流程旧行为；后台 PostMessage 成功时自动忽略 hold。
- 回归 `tests/suite_zzz_click_hold_20261104.py`（含本机实机结果断言）。
### 所有前台点击默认「极短按住」（2026-11-04 用户要求）

- **动机**：瞬时 down+up 既是典型脚本输入特征，又点不动 Unity 启动器。
  用户要求把 21:35 实机验证的按住式点击推广到所有点击。
- **唯一收口**：`foreground_flow_runner._pyautogui_click_guarded` 现在默认走
  `_pyautogui_click_held`，`DEFAULT_CLICK_HOLD_SEC=0.06`（可用环境变量
  `GAMEAUTO_CLICK_HOLD_SEC` 全局调/关）。参数语义：
  `hold_sec=None` → 全局默认按住；`0` → **显式瞬时**（逃生口）；`>0` → 指定时长。
  `_click_window_local` / `run_flow` 只负责透传，不再自己分叉；
  `run_flow` 未配置 `click_hold_sec` 时传 `None`，日志会打 `[按住: 0.06s]`。
- **覆盖面**：`run_flow` 所有前台点击、白荆 / 猫兑换等几十处直接调用
  `_pyautogui_click_guarded` 的模块，以及原先 11 处裸 `pyautogui.click`：
  `automation_controller`（扩 import）、`ocr_click_tester`(4)、`click_sequence`(1)、
  `zzz_battle_assist._click`(1)、`maintenance/{generic_workflow_runner,screen_task_runner}`(3)。
  后 5 个模块用 `_guarded_click` 小 wrapper 做**函数内局部 import**，避免和
  `foreground_flow_runner` 循环依赖；`click_sequence` 自己的 FAILSAFE /
  回放节奏管理不变，只是把 click 调用换成统一收口。
- **两处特殊**：战斗高频连点 `zzz_battle_assist._click` 用 `hold_sec=0.02`
  （仍带按住语义但几乎不拖慢 APM）；后台 `PostMessage` 的
  `WM_LBUTTONDOWN` / `WM_LBUTTONUP` 之间本来就有 20ms 间隔，天然是按住式，
  不需要改。
- **回退**：`GAMEAUTO_CLICK_HOLD_SEC=0` 全局瞬时，或单个步骤
  `click_hold_sec: 0`。默认 0 之外的值才影响行为，别把它当成"越大越像人"。
- 回归：`suite_zzz_click_hold_20261104.py` [3][4][6][8][9]（默认按住、
  显式 0 瞬时、透传、`src` 里不再有裸 `pyautogui.click`）。
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
- 2026-11-10 补课（审计 P2）：确认策略 `execute_confirm_strategy` / `confirm_disappear` / `confirm_text_change` / `_wait_any_template` 已接 `stop_check`（命中停止返回 `"用户已请求停止"`，由外层步骤循环收尾）；步骤间 `post_delay` 改 `_sleep_interruptible`；controller `_launch_normal` 的 10s 进程发现循环响应停止、`initial_wait`（默认 20s）改 `_interruptible_initial_wait`（分片睡眠 + `_watchdog_beat`）。
- 仍未接线：`run_wool_background` / `_run_monthly_flow` 内部 `run_flow` 未传 `stop_check`（签名无 controller），其 45s 窗口等待仍不可中断。
### 本机代理未启动要单独判别（2026-11-21）

- Windows 上 urllib 自动读系统代理；代理软件没开时请求本机 `127.0.0.1:7897`
  得到 `WinError 10061 目标计算机积极拒绝`，但日志表现像"外网断了"。
- `services/network_preflight.py`：只探测指向 127.0.0.1/localhost/::1 的代理端口
  （远程代理不判定，避免误报），不可连时文案直接写「系统代理 X 未启动或无法连接」。
  启动前签到的五个网络分支共用 `make_proxy_guard()`，整批只探测一次。
- 区分 10061（端口无监听，先查本机代理）与 10054（连接被重置，可能是代理/上游抖动）。
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
  `task_batch_runner._run_single_task` 状态为 failed、`baijing_common._record_step_failure`
  （白荆俱乐部每日任务 / 每日外勤赠礼失败；2026-11-14 拆分后归 common）。
- **三条铁律**：
  1. **永不抛异常** —— 调用点都在失败分支里，诊断自己炸了必须被吞掉（连 logger 抛异常也不能
     把已写库的 id 吞掉）；
  2. **不重复识别** —— 调用方已有当帧 OCR 结果时务必传 `ocr_results` 复用，一次失败重试动辄
     几秒，别为了记录再跑一遍 OCR；
  3. **窗口不可用不退化全屏** —— 传了 hwnd 但 `GetWindowRect` 失败就直接不存图，否则会把用户
     桌面截图存进证据目录（与 `ocr_click_tester` 的全屏 OCR 防护同源教训）。
  4. **测试进程不取证（2026-11-22 补）** —— `is_test_process()` 命中（pytest 用例内 /
     `GAMEAUTO_TEST_MODE=1` / argv 是 `tests/suite_*.py`）**且目标路径是生产
     `data/`** 时 `record_failure` 直接返回 None：套件用 hwnd=1/1234 之类的假句柄
     调流程，截图必然取不到，写进生产库就是"只有一行没有截图的失败"。事故：
     11-15~11-22 有 138 条（联合训练场 60 + 白荆日常 78）这样的记录，真机同期却
     天天 success，用户看到的是"失败了一周、一张证据都没有"。套件若要断言取证被
     调用，patch 自己模块的 `_record_failure` 数调用次数；要把记录写进临时库，
     改写路径即可（守卫只拦生产路径）。同一守卫已铺到 `daily_progress` 与
     `task_meta` 门控，见 05-daily-progress。
- **出报告**：`tools/flow_failure_report.py`（`--days 7 --top 20 --verbose --json --purge 30
  --flow BaiJing --purge-stub-rows`）。按 (flow_key, step) 聚合次数 → "哪一步最常失败、最该优化"，
  附最近一次时间、是否有证据截图、样例错误、失败当帧屏幕文字与图片路径。
  `--purge-stub-rows` 按键（默认 `1,1234`）清掉历史测试桩记录（真机 HWND 不会
  是这两个值，先打印将删条目再 `--yes` 才动）。
- **GUI/WebUI 入口**：`DailyView.qml` 底部「最近失败记录」卡片（bridge
  `getRecentFlowFailures(days) -> str` JSON，后台线程读库最多等 3s；截图缩略图点击
  `openFlowFailureImage` 开系统看图片、`openFlowFailureDir` 开证据目录）；
  没有 `image_path` 时卡片直接写出原因（"失败当刻窗口句柄 X 取不到画面"/
  "没有可用窗口"），不再只留一个点进去空空如也的目录按钮；WebUI
  `GET /api/daily/failures?days&limit&flow` + `GET /api/daily/failure-image/{name}`
  （`ui/webui/static/app.js` 的 `loadFailures()`）。**`getRecentFlowFailures` 必须写
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
- **启动前清理遗留角落鼠标（2026-11-10 实跑事故）**："只有人类会停原点"的假设不成立——
  whimbox 每次页面切换后 `move_to((0,0))` 避悬停（whimbox/ui/ui.py:185）；暖暖超时被强杀后
  遗留光标被 pyautogui 当急停，紧接着 Qoder 每日积分 / ZZZ 后勤月商店两个前台流程首点击连续中止。
  `_clear_parked_failsafe_cursor` 在 `run_flow` / `execute_task` 两个启动边界检测鼠标精确在 (0,0)
  就用 win32 `SetCursorPos` 直挪屏幕中心并记一行日志（不能走 pyautogui：它的动作函数在 (0,0)
  会先触发 fail-safe）。**流程内 fail-safe 不变**，人类甩角落急停仍收口 FailSafeAborted 整批终止。
  回归 `tests/suite_failsafe_cursor_park_20261110.py`。
- click_sequence.py 自己管理 FAILSAFE（点击序列暂停期间禁用），不在本约定范围。
### 收尾动作必须 finally 兜底 (重要)

- `ui/bridge_views/automation.py::_run_automation_backend` 的**邮件、自动关机、每日进度刷新、
  WebUI 状态复位、finished/running 事件全部放在 finally 里**，禁止移回 try 主体。
- 原因：任何任务内未捕获异常都会穿透 run_task_batch；写在 try 主体里的收尾
  会被整体跳过 —— 2026-09-29 中午事故：CJ 抛 FailSafeException，
  shutdown_on_finish=true 却没关机、邮件也没发，用户挂机跑完机器整夜没关。
- 异常路径会往报告追加 `❌ 任务异常中断（收尾兜底已执行）: <异常>`，
  邮件照发、关机照调，UI 弹"运行失败"提示。
- 验证方式：mock `task_batch_runner.run_task_batch` 抛异常，
  断言 `_schedule_shutdown` / `_send_daily_email_report` 各被调用一次、
  finished 事件发出、running 复位为 False。

### dispatch 类步骤失败必须打日志 + 落 flow_failures (2026-11-04)

- `run_flow` 里 `action in _BUILTIN_ACTIONS` 的分支（monthly_exchange /
  hsr_resin_craft / zzz_shop_buy / notorious_hunt / enter_main 等）失败返回
  `(False, msg)` 时，**必须在返回前打 `logger.error` 并调
  `_collect_failure_evidence(error=msg)`**（截图 + 当帧 OCR 落库）。这类失败以前
  三层静默（模块内不打日志、dispatch 直接 return、run_flow 直接 return），11-03
  自塑尘脂点歪坐标只能靠邮件日报一行 ❌ 发现，日志和 flow_failures 全空。
- **"用户停止"不算失败**：dispatch_msg 含"用户停止"时只打一行 info，不写
  flow_failures——手动停止落假失败记录会污染失败报告。
- 新增 dispatch 分支时不要绕过这段自己 return；失败原因字符串要写清楚（带坐标 /
  等待时长更好），取证图才有用。
- 回归：`tests/suite_monthly_shop_observability_20261104.py` [2][3]。

### 确认失败取证 / 窗口不可用止损 / Cookie 过期 / 测试日志隔离 (2026-11-04 晚)

- **取证路径一共有三条，别只补两条**：`run_flow` 里「未匹配模板/OCR 重试耗尽」
  (2456 附近) 与「内建动作 dispatch 失败」(2574 附近) 早就调
  `_collect_failure_evidence`，但 `execute_confirm_strategy` 返回 False 的分支
  重试耗尽直接 `return False`。结果 11-04 绝区零三次「点击进入游戏：元素 10s
  未消失」一张图都没有、`flow_failures` 里连一条 `ZZZ_MONTHLY` 都没有。现在
  第三条也统一打 `logger.error` + 取证（`error="确认失败: {reason}"`），
  **"用户停止"仍然不算失败、不落库**。新增失败返回分支时先对照这三条。
- **窗口不可用要止损，但不能一刀切缩短 timeout**：异环 `NTE_MONTHLY` 步骤2
  `wait_template` 的 `timeout_sec=1200` 是给"游戏真在下载/更新"留的，改成 300
  会误伤。11-04 20:35:31→20:47:23 的空转是启动器/游戏本体互抢窗口导致 HWND
  失效或最小化，`ocr_click_tester._ocr_scan` 恒返回空结果，上层却一直按"未匹配"
  轮询。现在 `run_flow` 每轮用 `_window_available_for_ocr`（复用
  `_ensure_window_region` 这个唯一判据）计时，连续 `window_dead_timeout_sec`
  秒不可用就 break 出匹配循环 → 走既有「未匹配 → 重试 → 取证」，既留痕又不空转。
  配置为流程级/步骤级可选，默认 45s，`0` 可退回旧行为；只写在 `NTE_MONTHLY`。
  **只判窗口不可用，不判被遮挡**：异环可后台点击，被别的窗口挡住不影响操作，
  把遮挡当失败是过度保守。
- **米游社 cookie_token 有两份来源**：签到链路
  `mihoyo_sign._ensure_fresh_cookie_token` 会用 stoken 刷新，但结果只用于当次
  签到、不回写 `app_config.json`；`check_games_progress` 每次 `load(force=True)`
  读到的还是旧 token → 20:15:41 `[星铁] Cookie 已过期`。`enrich_cookie_for_record_api`
  只在 `cookie_token` **缺失**时补，过期不会补。现在 `_query_hoyo_daily_note`
  遇到 retcode 10001 才现场刷新并重试一次（正常路径不多发请求、不写配置），
  三个米游社游戏共用。另外 `_check_hoyo_accounts` 旧口径把
  `stoken/stoken_v2/stuid/stuid_v2` 全当必需还用子串判断，本机每天误报
  「缺少 stuid_v2」；改成只检查账号标识 + cookie_token。
- **测试不许写生产日志**：`AutomationController._init_logger()` 以前无条件挂
  `TimedRotatingFileHandler(LOG_FILE)`，套件里 `ac.AutomationController()` 就会把
  `FakeSession` 行写进 `data/logs/auto_runner.log`（11-04 21:21-21:58 一堆，
  次日排障分不清真假运行）。现在 `GAMEAUTO_LOG_FILE_DISABLED=1` 或
  `PYTEST_CURRENT_TEST` 存在时只挂 console/UI handler；`tests/test_suites.py`
  给所有子进程套件统一设开关，`suite_okww_module_20261104.py` 顶部再
  `setdefault` 兜住手动单跑。**生产 GUI/CLI 两个变量都不满足，文件日志行为不变**
  （「日志长期保留、不自动删除」的约定不受影响）。验证口径：跑一次套件前后
  比较 `auto_runner.log` 字节数，应完全不变。
- 回归：`tests/suite_failure_evidence_cookie_20261104.py`（26 项，含 AST 检查
  confirm 分支必调取证、窗口可用性判据、过期重试的三种分支、日志两条路径）。

### 长等待循环必须每轮打任务心跳 (2026-11-05)

- **两套预算会打架**：`TaskHeartbeatWatchdog` 盯的是**任务线程主动 `beat()`**，
  全局阈值 `_global.task_heartbeat_timeout_sec` 未配时是 **600s**；而
  `app_config.apps.<游戏>.timeouts` 里的 `launch` / `update` 是「等多久算超时」。
  只要等待预算 ≥ 600s 且等待循环不打心跳，线程明明在正常轮询也会被误判死锁。
- **11-05 异环实锤**：更新补跑 11:05:27 / 11:15:57 / 11:26:27 三次都是
  `_wait_for_game_process_snapshot` 等 `HTGame.exe`。11:15 / 11:26 已明确打出
  「游戏正在更新，按更新预算等待（最长 120 分钟），更新完继续跑」，结果每次
  到 600s 就被 `_on_task_stall` 强杀。看门狗现场栈
  (`data/logs/watchdog_task_heartbeat.log`) 三次都停在
  `automation_controller.py:470 in _wait_for_game_process_snapshot`
  → `_monitor_game` → `execute_task`，也就是 `time.sleep(2)`，**不是死锁**。
  异环 `launch=600`、`update=7200` 都不短于 600s，所以必然撞。
- **为什么正好 3 次**：`scheduler._check_update_retries` 每 30s 扫队列，
  `max_attempts=3`；被强杀的任务没写完成标记，attempt 已 +1 但到点又重排，
  间隔约 10 分 30 秒（600s 等待 + 30s 排队），三次耗尽才清空队列。
- **修法**：`_wait_for_game_process_snapshot` 的轮询循环每轮
  `self._heartbeat.beat()`，与 `_monitor_game` 主循环同口径。`beat()` 只是更新
  monotonic 时间戳，不要求 `start()`，也不影响 `stop_current_task` 的停止检查。
  **以后新增任何 `while True` + `sleep` 的等待循环，都要先问有没有打心跳**；
  只加日志/只 sleep 不算活着。
- **2026-11-09 起心跳已内建（审计 P1-2 铺满）**：`task_heartbeat_watchdog` 新增
  thread-local 桥——`execute_task` 入口 `set_thread_heartbeat(self._heartbeat.beat)`
  / finally 清除，任务线程内**所有**长等待循环调 `beat_thread_heartbeat()` 续命
  （foreground_flow_runner 的 `_heartbeat_beat()` 包装：步骤匹配循环 /
  `_sleep_interruptible` / `_wait_window_by_pid` / `_wait_window_for_flow` /
  `_wait_script_update_exe` / 确认策略三函数；controller `_wait_game_window_ready`
  走 `_watchdog_beat()` 防御式出口——`__new__` 测试桩没有 `_heartbeat` 时静默跳过；
  ok_ww 的 NTE 启动后点击链窗口等待同样接线）。run_flow 此前全函数 0 处 beat，
  本机 WuwaLauncher 等待模板 3600s > 600s 阈值必然被误杀——11-05 只补一个循环
  是同类位点的第一个。**新增等待循环调 `_heartbeat_beat()`（fff 内）或
  `self._watchdog_beat()`（controller 内）即可，不用透传 watchdog 实例**。
- **日志监控线程三道护栏（2026-11-09 审计 P2-1，三套监控统一）**：
  ① `_matching_files` 排序 key 用 `services.utils.safe_mtime_key`——BetterGI 会
  自己清旧日志，glob 与 stat 之间文件消失时裸 `stat()` 抛 FileNotFoundError；
  ② `_watch_loop` 每轮整体 try/except 记 ERROR 继续下轮——守护线程静默死亡 =
  完成检测/自动关游戏全失效且无日志痕迹；
  ③ `task_batch_runner._run_single_task` 里监控 `start()`（含初始化读日志）在
  try **内**调用——旧代码裸露在 try 外，异常穿透烧掉整批；现在失败降级为
  "监控不可用"（原神走 fail-closed，暖暖走 monitor 超时兜底）。
- 回归：`tests/suite_nte_launch_heartbeat_20261105.py`（AST 断言 beat 在 sleep
  之前、动态 mock 跑 3 轮验证 beat 次数、预算不匹配证据、补跑队列口径）。
