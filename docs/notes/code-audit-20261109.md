# 全库代码审计报告（2026-11-09）

> 范围：`src/game_auto/` 全部 154 个 Python 文件（约 6.8 万行）+ WebUI static JS + scripts/ bat。
> 方式：五个片区并行深读（runtime 执行链 / UI+WebUI / services+storage+settings / games+clients+gacha /
> app+maintenance+tools），先通读 `docs/conventions/` 排除已修复项，所有发现带上下文验证，
> 关键 P1 结论人工抽查源码复核。本文只记录审计结论，不展开修复实现；修复落地后在
> conventions 对应分篇登记口径。
>
> 严重度口径：**P0** 当场崩/当场泄（本次为零）；**P1** 会造成静默丢数据、误杀健康任务、
> 凭据泄漏或 100% 失效路径；**P2** 长期运行退化、真实竞态窗口、安全面收窄；**P3** 代码卫生。
> 每条标注置信度：确认（代码路径完整核对）/ 疑似（机制确认、触发依赖外部条件）。

## 总体结论

无 P0。项目整体纪律良好：连接即用即关、SQLite WAL、配置原子写、HTTP 全带 timeout、
锁内无阻塞调用、凭据掩码纪律均逐一核查通过。真正的系统性问题有两个：

1. **app_config.json 全链路无锁读-改-写**（P1-1，三个片区独立命中）——全项目最大架构缺口；
2. **"修复没铺满同类位点"模式**：心跳只补了一个循环（P1-2）、读超时只改了两套监控（P2 并发组）、
   先删后插只堵了 pool.json 一条路（P2 数据组）——单点修复后没有横向排查同族位点。

---

## P1（7 项，建议按此顺序修）

### P1-1 app_config.json 全链路无锁读-改-写，四条写路径互相覆盖【确认｜架构】

三个审计片区独立命中同一问题，是本次审计最大发现。

- **核心**：`settings/manager.py:267-351`。`_cache` 全局共享、**全文 0 处锁**（grep 证实）。
  `load()` 返回缓存 dict **本体**（非拷贝），`save()` 经 `_normalize_for_save` 整文档覆盖落盘。
  grep 实测 **24 处 `save()` 调用点**分属 QML 主线程、任务线程、WebUI 线程池——A 线程 load 后、
  save 前 B 线程先 save，A 用旧快照整体覆盖，B 的修改**静默丢失**（用户刚勾的启动项、
  刚写入的签到状态、WebUI 改的账号）。
- **绕行者加剧**：
  - `services/startup_sign_runner.py:193-216` `_write_sign_status_to_config` 绕过 `save()`
    直接 `atomic_write_json` 落**未归一化**全量配置，与 manager 格式漂移、`_cache` 脱节【确认】；
  - `clients/daily_checker.py:337-374` `_persist_device_cache` 直读直写整份配置（解密→改→加密→替换）【确认】；
  - `ui/webui/api/automation.py` / `api/wool.py` 与 `ui/bridge_views/automation.py:235-252`
    双通道读改 `flow_configs.json`【确认】；
  - `services/email_reporter.py` 的 `save_email_section` 读-改-写未走 manager 锁（并入本项修复面）。
- **跨进程更糟**：`maintenance/settings_gui.py:401-421` 是独立 tkinter 进程，其
  `manager._cache` 首次 load 后**永不刷新**（无 force、无跨进程文件锁）。开着它的几十分钟里
  主 GUI 写入的一切（DPAPI 密文 token、签到开关、脚本路径）都会在它第一次点"保存"时被
  陈旧快照整文档回滚——一次保存还连写三次全文档（`update_qbittorrent` + `update_log_config` +
  `update_global`）；`.bak` 写的是它自己的新内容，被覆盖值不留痕【确认】。
- **缓存本体被原地改**：`load()` 返回 `_cache` 本体，`update_app_order` 等直接改的就是全局缓存；
  两个线程同时改同一 dict 时任何一方 `save()` → `_encrypt_for_persist` → `copy.deepcopy`
  可抛 `dictionary changed size during iteration`，save 中断后缓存与磁盘脱节且不回滚【确认】。

**修法**：manager 加模块级 `threading.Lock`（load/save 全程持锁）；`load()` 返回深拷贝或提供
锁内 `update(fn)` 收口；`_write_sign_status_to_config` / `_persist_device_cache` /
email_reporter 收敛进该入口；settings_gui 每次面板保存前 `load(force=True)`，跨进程再考虑
msvcrt 文件锁（同 instance_lock 手法）。

### P1-2 任务心跳看门狗误杀健康任务：run_flow 全链 0 处心跳【确认｜并发】

- `grep -c "beat()" foreground_flow_runner.py` = **0**。全仓 `beat()` 仅 4 处
  （`automation_controller.py:475/1283/1496` + `baijing_flow.py:1619`）。
  2026-11-05 异环事故（06-robustness「长等待循环必须每轮打任务心跳」）只补了
  `_wait_for_game_process_snapshot` 一个循环，**prelaunch 链路没接**：
  `automation_controller.py:1763` 武装看门狗（默认阈值 600s）→ `:1794` `_run_prelaunch_flow`
  → `:1803` `_wait_game_window_ready`（300s）→ `:1883` `ensure_game_main` → `:1898`
  `run_nte_post_launch_ocr_click` → `run_flow`（`foreground_flow_runner.py:2480`，全函数无心跳）。
- 本机实测配置：**WuwaLauncher「等待更新完成」wait_template timeout=3600s + 游戏启动确认
  wait_process 600s；NikkiLauncher 900s**——游戏客户端更新/暖慢启动必然超 600s 无心跳 →
  `task_heartbeat_watchdog.py:111` 判卡死 → 置 stop + 杀进程树，健康任务被强杀、
  批次重试被 stop 阻断，即 11-05 事故同族复发。`WutheringWaves.wait_game_window_before_script=true`
  的 300s 窗口 + NTE OCR 点击链（~2min）叠加也逼近阈值。

**修法**：`run_flow` 加可注入 heartbeat 回调，在步骤匹配循环、`_wait_script_update_exe`、
`_wait_game_window_ready`、确认策略轮询的每轮 sleep 前调用（对齐 06-robustness
「新增 while+sleep 必问心跳」口径）。

### P1-3 暖暖 Momo Token 明文落日志【确认｜安全】

- `gacha/nikki_resonance_fetch.py:629`：`verify_momo_token` 把 token 拼进 query
  （`/v1/tlog/verify?token=...`）；`:221` `_request_bytes` 任何非 200 响应把**含 query 的
  完整 path** 打进 `auto_runner.log` ERROR 级。token 过期（401/403）恰是最常触发的失败场景
  ——长效凭据进长期留存的日志（约定「日志长期保留、不自动删除」）。

**修法**：一行——日志只打 `path.split("?")[0]`；或 verify 改 POST body。
另查得 `mihoyo_sign.py:1059` 有一处 `repr(tokens_raw)[:500]` 打 INFO，但属**当前无调用方的
死代码**（`login_by_qrcode_genshin`，实际走 `login_by_qrcode_app`），顺手删即可。

### P1-4 WebUI /api/apps/run TOCTOU：可双开两批任务【确认｜并发】

- `ui/webui/api/automation.py:94`：`if is_running(): return` 与后续 `set_running(True)`
  之间**无锁**（sync 端点跑 uvicorn 线程池，真并发窗口）。GUI 侧的共享置位要等后台线程跑到
  backend 第 402 行才发生，中间隔着 `_split_maintenance_targets()` 逐游戏公告预检（秒级网络）。
  `/api/wool/run:27` 同模式。
- 影响：两批 `run_task_batch` 并发 → 同游戏双脚本抢前台、签到重复、日志互踩；后结束方发
  `finished`（`api/automation.py:194`）把仍在跑的批次 `bridge.isRunning` 错误清零，
  连带击穿 Main.qml 的 `!bridge.isRunning` 释放闸与定时任务排队。
- 同族：`ui/bridge_views/automation.py:265-270` `runWoolCollection` 无 `_run_lock` 的
  check-then-act（线程内 `:275` 才置位，双击可双开 wool）+ `:315` `finally` 无条件清 running
  （wool 先结束会把正在跑的正式批次"关掉"）【确认】。

**修法**：`webui/state.py` 提供锁内原子 `try_acquire()`；GUI `startAutomation` 拿到
`_run_lock` 后**同步先置位再预检**；wool 进入即置位 + owner 代际复位。

### P1-5 getRecentFlowFailures 同步等 3 秒，每次进每日页冻结 UI【确认｜并发】

- `ui/bridge_views/daily.py:152`：`done.wait(timeout=3.0)`，调用方 `DailyView.qml:171/182`
  在 `Component.onCompleted` 与每次 `onVisibleChanged`（60s 节流）同步取返回值。
  冷启动/首次建 flow_failures 索引必吃满 3s，QML 主线程整体冻结——与代码注释
  「避免阻塞 QML 主线程」意图相反。

**修法**：改纯异步——槽立即返回，`_load` 完成后 `emit_event("flow_failures", ...)` 推给 QML
（DailyView 已有成熟的 `onDailyXxxChanged` 订阅模式）。

### P1-6 CLI 自提权路径 100% 损坏【确认｜架构】

- `app/cli.py:27-29`：`ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), ...)`
  提权重启后以脚本直跑 `cli.py` → `__package__=""` → `:13` 的
  `from ..settings import APP_ORDER` 相对导入必炸 `ImportError`（提权窗口闪退）；
  `" ".join(sys.argv)` 不加引号，路径含空格时参数切错。目前被
  `scripts/launchers/start_cli_admin.bat:21-25`（先 `net session` 再 PowerShell RunAs）掩盖。

**修法**：提权重启改 `"{exe}" -m game_auto.app.cli` 形式并逐参加引号
（subprocess.list2cmdline 语义，走 ShellExecuteW 的 lpParameters）。

### P1-7 manager.load() 遇损坏 JSON 静默回退默认值 → 凭据被覆盖丢失【确认｜架构】

- `settings/manager.py:284-285`：`JSONDecodeError` 时直接用 defaults，**不尝试 `.bak`**
  （原子写保证 bak 也是合法 JSON）。此后任意一次 save（24 个调用点）就把**仅存于 config 的
  米游社 cookie、塔吉多 token、邮箱密码**用默认值+增量洗掉（`_SENSITIVE_SUBSTRINGS` 命中的
  字段都只存 config；只有库街区 token/did 迁进了 DB）。低概率、高代价。

**修法**：JSONDecodeError 时先读 `.bak` 恢复并告警回写主文件，失败再回退默认值。

---

## P2

### 内存泄漏 / 资源占用组

| 位置 | 问题 | 置信度 |
|---|---|---|
| `runtime/click_sequence.py:121-153` | `_EXE_CACHE` pid→exe 只进不出，GUI 长驻数周累积 + Windows PID 复用后返回错误 exe 名（内存量级小但兼正确性问题） | 确认 |
| `services/utils.py:69-96` | `read_file_with_timeout` 超时后遗弃线程+句柄**无上限**：IO 挂起期间 watcher 每轮轮询泄一个线程一个句柄（有意取舍但缺护栏）；修法：同 (path, offset) 在途去重或并发限 1 | 确认 |
| `ui/webui/static/app.js:275,296` | WebUI 前端日志 DOM 无上限（`textContent +=` 只追加，挂机监控长开必涨，仅 `#btnLogClear` 手动清；后端环形 500 条有界，泄漏纯在前端） | 确认 |
| `tools/ai_history_rag.py:192-202,205-241` | 每次查询全量读 chunks.jsonl（实测 94.5MB / 64030 块）+ 现算 bigram 无倒排缓存，单次 query RSS 150-250MB、约 14s，随语料线性膨胀（不常驻不泄漏）；build 端 `:152` 全量 `json.loads` 峰值 200-300MB、`:171-183` **非原子写**——中断留截断索引且 query 无告警（项目现成的 `atomic_write_json` 没用上） | 确认 |
| services 6 个每日解析器 | 全量 `read_text` 无大小上限（bettergi:81 / zzz:109 四遍正则 / starrail:83 嵌套循环 TASK_MARKERS×lines / ok_ww:113 / whimbox:214 / ok_nte:163），且各 `_xxx_log_dir()` 每次调用重读整个 app_config.json；任务收尾/视图刷新反复触发，几十 MB 级瞬时分配。修法：流式逐行或尾部 N MB（各解析器只认"最后一次 run"，尾部语义等价）+ 按 mtime 缓存 | 确认 |
| `services/script_tool_installer.py:390` | sha256 整包 `read_bytes()`（数百 MB 安装包瞬时尖峰）；`:103-109` `_download_to` 是无大小上限的死代码，删 | 确认 |
| `services/stamina_configs/base.py:248-258` | 刷本配置备份目录无清理策略（45KB/份、低频，以年计缓慢累积） | 确认（低） |

### 并发 / 竞态组

| 位置 | 问题 | 置信度 |
|---|---|---|
| `services/task_heartbeat_watchdog.py:83-153` | `stop()` 只 join 2s 即返回，旧止损回调可跨代击发：dump 慢（线程多/杀软扫描）时旧线程 dump 完成后执行旧 `on_stall` → **误杀下一个任务的进程树**。修法：回调携带代际号，击发前校验 | 疑似 |
| 三个日志监控 `_matching_files`（ok_ww:425 / bettergi:150 / whimbox:118） | sort key 裸 `stat()`，glob 与 stat 之间日志被轮转 → `FileNotFoundError` → `_watch_loop` 无 try/except 守护线程**静默死亡**（完成回调/自动关游戏全失效无痕迹）；且 `_snapshot_offsets` → `start()` 在 `_run_single_task` 的 try **之外**（`task_batch_runner.py:836-847`），异常穿透烧掉整批。修法：sort key try 降级、watch_loop 整体兜底、start 移进 try | 确认 |
| `runtime/bettergi_log_monitor.py:200-207`、`whimbox_log_monitor.py:215-221` | 这两套监控**没接** `read_file_with_timeout`（10-15 的读超时修复只覆盖 script_error/ok_ww 两套）——坏盘 IO 挂起 → 原神 `log_found=false` fail-closed 当天必败、暖暖完成回调丢失空转到 monitor 超时 | 确认 |
| `runtime/foreground_flow_runner.py:1391-1398` | `_pyautogui_click_held` mouseDown 后无 `finally: mouseUp`——hold 期间 FailSafe/中断 → 合成左键**卡在按下态**用户桌面进拖拽态（同文件键盘版 `:873-877` 有对称 finally，鼠标漏了） | 确认 |
| `settings/__init__.py:79-88` | `apply_runtime_config` 对全局 APPS `clear()+update()` 非原子，与任务线程读 `APPS`/`APP_ORDER` 并发可见空/半新 dict → 单次"app 不存在"误判。修法：局部组装好再一次性替换引用 | 疑似 |
| `settings/discovery.py:157-230,404-418` | 路径发现未命中**无负缓存** → 脚本卸载/路径失效后每次任务启动同步重扫 C/D/E/F 盘（os.walk 限深 4），HDD 数十秒到分钟 | 确认 |
| `services/game_update_calendar.py:507-524,560-575` | **锁内做网络 IO**（`get_update_info`/`refresh_all` 在 `_LOCK` 内拉远端、超时 12s、6 源串行）——缓存过期后第一个维护检查持锁最长 1-2 分钟，全部任务启动排队。修法：锁只护缓存读写，网络移出锁外 | 确认 |
| `runtime/automation_controller.py:916-922` | `_launch_normal` 按创建时间抢裸名 `cmd.exe`/`python.exe` 当脚本 PID——10s 窗口内用户新开任意 cmd 会被记 PID，收尾 `_kill_related_processes` **连坐杀整棵子树**。修法：用 `cleanup_cmd_patterns` 已有的 name+cmdline 精确匹配 | 确认（触发窄） |
| `runtime/foreground_flow_runner.py:1671-1768,3169-3174` | 确认策略轮询与 post_delay sleep 不接 `stop_check`——用户点停止最长 `confirm_timeout_sec + post_delay` 秒不生效（与已登记的 45s 窗口等待同类残留） | 确认 |
| `runtime/automation_controller.py:915-940` | `_launch_normal` 的 10s 查找循环 + 20s initial_wait 不查停止标志（量级小） | 确认 |

### 架构 / 安全组

| 位置 | 问题 | 置信度 |
|---|---|---|
| `ui/webui/api/daily.py:105-114,147-155` | 图片端点 name 过滤 `/`、`\`、`..`，但 **pathlib 盘符相对路径逃逸**：`WindowsPath(...) / "C:bootmgr"` 整体替换为盘根，三者过滤项都不含 → 可读各盘符当前目录下名字不含分隔符的文件。需持有效 token 且仅本机。修法：读出后 `resolved.is_relative_to(BASE_DIR)`（桌面端 `bridge_views/daily.py:167-187` 已是正确示范） | 疑似 |
| `games/registry.py:35-48` | `_discover()` 先置 `_LOADED=True` 再 import，任一 games 模块顶层抛错 → 半初始化注册表**永不重试**，消费方在宽 except 里降级 → "部分游戏悄无声息消失"（新游戏调试期最险）。修法：失败回滚 `_LOADED=False` 重试并显式报错 | 确认（机制） |
| `clients/wuwa_kujiequ.py:53,500-532` | `_access_token_cache` 命中即返回**无失效路径**——B-At 过期后每 300s 鸣潮刷新 + 重试全撞旧缓存，只能重启 GUI。修法：登录类错误关键字时 pop 重取一次 | 确认（代码层） |
| `clients/endfield_gacha.py:234-291`、`endfield_protocol.py:155-173`、`gacha/nikki_resonance_fetch.py:715-749` | 分页 `while True` 无页数上限/游标守卫——服务端改版忽略游标恒回 hasMore 时死循环 + 无限请求 + 内存无界。修法：MAX_PAGES 熔断 + 游标未前进即退 | 疑似 |
| `gacha/nikki_resonance_analysis.py:392`、`gacha/wuwa_analysis.py:479` | record_id 用**迭代下标**（`f"nikki_{uid}_{pool_id}_{idx}"` 全局累加器 / `f"wuwa_{uid}_{r.resource_id}_{i}"` xlsx 平铺下标）——导出键序位移 → 旧记录拿新 id → UNIQUE 失配 → 整库翻倍、保底计数失真；消费方 `ui/flet_app.py:3426,4386` 不清库直插，而 `bridge_backends/gacha.py:569-573` 的 pool.json 路径已先删后插（项目自己登记过该风险，只堵了一条路）。修法：稳定业务键（time+draw_index/resource_id）或统一先删后插 | 确认 |
| `runtime/ocr_click_tester.py:92-147` | WGC 捕获线程中途死亡后 finally 只置 `_wgc_running=False` **不清 `_wgc_latest`** → 下次 `_wgc_ensure_started` 被旧帧秒过 → OCR 拿陈旧帧做匹配点击（误点击类高危）；持续失败时每次抓屏各生成一个失败线程（线程风暴）；`_wgc_ensure_started` 无锁可双会话。修法：退出清帧 + 校验帧时间戳晚于启动 + 加锁 | 确认（逻辑） |
| `maintenance/generic_workflow_runner.py:36-94`、`screen_task_runner.py:61-115` | 维护执行器**不取任何实例锁**即实点击抢前台——与生产 GUI 批处理互踩、互相把对方窗口切前台点错按钮。修法：共享互斥锁文件或启动前读生产锁 PID 拒绝 | 确认 |
| `services/instance_lock.py:99-115,63-67` | 解锁后 `unlink` 有双实例窗口（实例 C 已 open 旧文件 → A 解锁删除 → C 对孤儿 inode 加锁成功 → D 新建再锁也成功）；锁文件创建失败 **fail-open** 放行（单例保护静默失效）。修法：不 unlink（锁语义绑 inode）或 CreateMutexW；fail-open 至少提级告警 | 确认 |
| `maintenance/settings_gui.py:555-563` + `settings/manager.py:188-217,403-408` | 删除内置游戏**永远不生效**：`_extract_overrides` 只能表达"存在差异"表达不了"删除"，重启后 `apply_runtime_config` 把缺失 key 补回（`settings/__init__.py:49-53`）→ 条目带默认配置复活、可能重新参与批次。修法：删除墓碑（`{"_removed":true}`）或 GUI 对内置条目禁用删除 | 确认 |
| `app/gui.py:234` + `ui/bridge.py:222` | 退出 `os._exit` 跳过 atexit：`_unpin_all_on_exit` 不执行——录制中退出**目标窗口永久 topmost**（历史事故形态）；`_stop_log_listener` 不执行——队列里最后几行日志丢失。修法：`_cleanup_on_quit` 显式调这两个 | 确认（逻辑层） |
| `ui/bridge_views/scheduler.py:54` | 每 30s 主线程 `load(force=True)` 全量重读 + 逐字段 DPAPI 解密 + 全树明文扫描（叠加 P1-1 无锁竞态）。修法：定时任务元数据单独文件或 mtime 校验缓存 | 确认 |
| `ui/webui/api/daily.py:92-103` | `/api/daily/check` sync 端点直打外部 API（逐游戏、120s 口径），多击可耗尽 uvicorn 线程池（40 令牌）拖死 status 轮询。修法：照 `/api/apps/run` 模式起线程即返 | 确认 |
| `maintenance/debug_github.py:11` | 硬编码占位 token 模板（`token YOUR_GITHUB_TOKEN_HERE`），诱导填真 token 随仓库提交。改读 `GITHUB_TOKEN` 环境变量 | 确认 |
| `tools/ai_history_rag.py:157,178` | build 对每个入选会话重复算两遍 `session_score`（纯浪费，顺带修） | 确认 |

---

## P3（低危杂项，一并列出）

- `app/gui.py:229-234` `os._exit` 跳过 QueueListener 收尾（并入 P2 架构组 unpin 一并修）。
- `runtime/task_batch_runner.py:459-460` `TaskBatchResult` 叠了两个 `@dataclass`（当前无害易踩坑）；`:920` `daily_failed` 死变量；`:1146` XM 失败后硬睡 5s 不接 `_interruptible_sleep`。
- `services/secure_store.py:76-78` `resolve` 解密失败把**密文当值返回**——下游拿 `dpapi:...` 当 token 业务失败难排查，应抛错或返空串。
- `services/daily_progress_tracker.py:484` `load_date` 的 `json.loads(row["extra_json"])` 不捕获——库内 extra_json 损坏会让整张 DailyView 加载失败，应 try 回退 `{}`。
- `services/mem_reduct_detect.py:28` 加载的 `Psapi.dll` 从未使用（实际走 Toolhelp32）；`_read_memory_load_percent(pid)` 的 pid 参数被忽略（查的是系统级负载）。
- `settings/manager.py:50` `_SENSITIVE_EXCLUDE` 的 `subject_prefix` 不命中任何敏感子串（冗余排除项）。
- `tools/flow_failure_report.py:43-44` `--purge` 直接清失败记录+截图无交互确认（显式 opt-in 且有天数护栏，建议加 `--yes`）；`maintenance/workflow_bootstrap.py:95-98` `__main__` 直写生产库 Demo 流程（upsert 幂等，风险低）。反面典型 `tools/migrate_data_layout_20261029.py` 做得好（运行检测/拒绝覆盖/失败回滚）。
- `tools/okww_module/runner.py:704` 控制 token 走命令行参数（同用户进程可读；仅绑 127.0.0.1 + 单客户端互斥，协议面小）。建议改 ready 文件/环境变量。
- `app/cli.py:80-81` 未知 app key 原样进 `run_task_batch` 靠内部兜错，建议打印合法清单退出。
- `scripts/launchers/start_cli_admin.bat:23`（gui/flet 同型）`Start-Process '%~f0'` 路径含单引号会破坏 PS 字符串（当前路径固定，隐患）。
- `maintenance/settings_gui.py:560` `_del_app` 的 `cm.get_app(key).get(...)` 列表与配置失配窗口 None 解引用。
- `maintenance/screen_task_runner.py:106-112` Popen 从不 wait（DEVNULL 无管道死锁，仅建议保留退出码）。
- WebUI 小项：`app.py:130-146` WS 游标用 `len(get_recent_logs(500))` 内存缓冲 <50 行时回填文件行，漏发/重发（建议同 `/api/logs` 用单调 `_log_seq`）；`auth.py:24-25` `_sessions`/`_login_attempts` 无容量上限（本机实际风险极低）；`api/auth_api.py:30-34` `/api/auth/status` 未鉴权可探测 `has_account`；`api/wool.py:16` meta 表缺 QODER 项（WebUI 显示裸 key）。
- QML 观察项：`Main.qml:57-69` 延迟释放只覆盖 5 个重页面（已知设计），但 `EndfieldCenterView.qml`（1217 行、持有 gacha pools/history 大 JSON 属性）与 `ClickSequenceView.qml`（858 行）不在清单内且常驻——后续再加重量需重新评估覆盖面。
- `runtime/ocr_click_tester.py:217-243` OCR 引擎释放：Python 侧断引用干净（唯一挂载点模块全局，包内无其他缓存；`_precision_engine` 是未赋值死变量），显存残留 ~137MB 为 ORT CUDA arena 平台限制（代码已声明，非缺陷）。唯一改进点：释放后若 WebUI/定时任务立刻又触发 OCR，~733MB 显存重新占住，"错峰"落空——可加冷却窗口。
- `services/utils.py` `atomic_write_json` 的 `os.replace` 前无 fsync（断电丢最近一次配置，P3 级）。

---

## 已排查、确认干净的高危模式（可放心）

**内存/泄漏**
- 真正的"只进不出"容器：`_TEMPLATE_IMAGE_CACHE` >64 全清、`_MONTHLY_CHECKED` 每批 clear、
  三监控 `_offsets` 生命周期单实例、okww `_pending` 统一 pop、`remember_process_snapshot`
  按 (app,role) 键控、wuwa/dependency 缓存均有界。未发现 per-pid/per-flow 无界长期容器。
- SQLite：`_connect()` 上下文管理器 + finally close（历史泄漏已修无回归）、WAL、无跨线程
  复用 Connection、无事务内慢操作。
- Windows 句柄：`process_supervisor.py:171-173` OpenProcess→Terminate→Close 顺序、
  `_process_is_elevated` 双层 finally、click_sequence/mem_reduct 均有 finally CloseHandle。无泄漏。
- HTTP：clients/gacha 全部 16 个 urlopen/http.client 点带 timeout（5-30s）；全程 urllib 无
  Session 堆积；签到重试有界（米游社单次、森空岛 2 次+1.5s、塔吉多一次性 guard、QR 150 轮封顶）。
- QML 侧：声明式 Connections 随 Loader 销毁重建不叠加；`uiEvent.connect` 仅连一次；
  UILogHandler/ensure_log_handler 有去重；QML ListModel 全部 clear 后 append；
  日志 250 行上限 + 300ms 节流；无 PIL/numpy/QPixmap/base64 常驻（截图走磁盘+file:// URL）。

**死锁/竞态**
- 全项目锁内无阻塞调用、无交叉锁序；`TaskHeartbeatWatchdog._lock` 只护时间戳；subprocess 全
  DEVNULL 无管道缓冲死锁；okww socket 长读全在 daemon reader 线程 + `event.wait(timeout)`。
- Qt 亲和性：后台→UI 全走 `emit_event(kind, **payload)` JSON dict queued 投递，无大对象信号。
- 单线程进入批处理 + `_run_lock` 兜定时任务双触发；watchdog 回调与下一任务时间窗经核算不重叠。

**安全**
- WebUI：鉴权中间件全覆盖（白名单仅 auth/health）、WS 单独验 token、`hmac.compare_digest`
  恒时比较、改密清会话、cookie httponly+samesite、JSON body 需预检（CSRF/DNS-rebinding 面
  基本封死）、绑定 127.0.0.1、CORS 精确白名单。
- 凭据：config 侧 `_walk_encrypt`+`atomic_write_json`（含 .bak）落盘前 DPAPI；库街区走
  DB+DPAPI；日志只打 token_len/掩码；ADB 抓取显式 pop 旧明文键；QML token 输入全走
  CredentialInput。遗留注意点：`bridge._credentials_json` 属性含明文 JSON 常驻并推给 QML
  （回填所需的设计），**禁止在 QML 里 console.log 它**，建议在 03 篇明令。
- 注入：HoYo 表名白名单、UIGF 顶层键白名单、LIKE 通配已转义、snappy/deflate 有边界校验、
  ruamel/ast.literal_eval 安全加载；无 yaml.load、无裸 eval。

---

## 修复优先级建议

1. **一把锁修一片**：P1-1 manager 线程锁 + load 深拷贝 + 收敛绕行者（同时消掉 P1-7 的
   高代价面与 email/reporter 等旁路；settings_gui force 重读一并做）
2. **P1-2** `run_flow` 接心跳回调（否则鸣潮/暖暖 prelaunch 长等待场景必复发）
3. **P1-3**（一行）+ **P1-4** WebUI try_acquire + wool 置位口径
4. **监控三件套补课**：读超时、sort key 容错、start() 移进 try（P2 并发组前三条，同属"修复没铺满"）
5. **P1-5** 异步化 + **P1-6** CLI 提权路径
6. 其余 P2 按表顺批，P3 顺手清
