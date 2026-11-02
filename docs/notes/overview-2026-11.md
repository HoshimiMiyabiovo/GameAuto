# 白荆回廊动态主界面识别（纯模板匹配 · 连续 5 帧）

## 完成内容
- 将 `runtime/baijing_flow.py` 中「主界面」判定由原本的 **OCR + 模板综合判定** 简化为 **纯模板匹配**：在 3 秒预算内全速循环，每帧 WGC GPU 直采截图后做 `白荆回廊-主界面.png` 模板匹配，**连续命中 5 帧即判定进入主界面**。
- 不再走 OCR，逻辑更直接、确定性强；OCR 引擎与 CUDA 路径仍保留在 `ocr_click_tester.py` 供其它模块使用，本次未动。
- 同步修复了 `runtime/ocr_click_tester.py` 中 WGC（Windows Graphics Capture）的 API 调用错误，使其真正可用。

## 核心改动

### runtime/baijing_flow.py
1. 新增 `detect_baijing_main_ui(logger, hwnd, budget_sec=3.0)`：
   - 每帧 WGC GPU 直采截图，在同一张截图上直接用 OpenCV 做模板匹配（避免重复 GDI 截图）；
   - **连续命中计数**：某帧命中 → 计数 +1；某帧未命中 → 计数清零（应对动态背景动画瞬时失配）；
   - **连续命中达到 `MAIN_UI_CONSECUTIVE_FRAMES`（默认 5）帧即返回 True**（`reason=consecutive_template_matched`）；
   - 超时未达连续命中数返回 False（`reason=budget_exhausted`）；模板缺失直接返回 False（`reason=template_missing`）。
2. 新增配置常量：
   - `MAIN_UI_DETECT_BUDGET_SEC = 3.0`（单次识别最长 3 秒）；
   - `MAIN_UI_CONSECUTIVE_FRAMES = 5`（连续命中帧数阈值）；
   - `MAIN_UI_TEMPLATE_THRESHOLDS = (0.9, 0.85, 0.8)`（`_match_template_in_screenshot` 默认阶梯阈值）。
3. 删除原 OCR 相关：常量 `MAIN_UI_POSITIVE_KEYWORDS` / `MAIN_UI_NEGATIVE_KEYWORDS` / `MAIN_UI_TEMPLATE_SCORE` / `MAIN_UI_DETECT_THRESHOLD` / `MAIN_UI_OCR_MAX_SIDE`，函数 `_ocr_text_matches_keyword` / `_resize_for_ocr`，以及无关 import。
4. `run_baijing_ingame_flow` 在 60s 等待后仅预热 `grab_screen`（WGC），不再预加载 OCR 引擎。
5. 常态匹配循环中，仅 step 1「主界面判定」使用新函数；其余步骤（点击屏幕继续/关闭/ESC）保持不变。

### runtime/ocr_click_tester.py
- 修复 `windows-capture 2.0.1` 调用：
  - `cap.start(callback)` 改为 `@cap.event` 注册 `on_frame_arrived` / `on_closed` 后再 `cap.start()`；
  - `monitor_index` 由 0 改为 1（库要求大于 0）。

## 验证结果
- `runtime/baijing_flow.py` `py_compile` 通过；`python -m game_auto.runtime.baijing_flow` 可正常导入。
- 无窗口/无主界面场景冒烟测试：`budget_sec=1.0` 实测抓 3 帧、连续 0 命中、返回 `False`（`reason=budget_exhausted`），符合预期。
- 全文件已无任何对已删除 OCR 符号的残留引用。

## 调参提示
- `白荆回廊-主界面.png` 模板是角色立绘（动态背景的一部分），若实际主界面该图样与模板差异较大导致连续命中困难，可：
  - 调整 `MAIN_UI_TEMPLATE_THRESHOLDS` 放宽单帧阈值；
  - 或降低 `MAIN_UI_CONSECUTIVE_FRAMES`（如改为 3）；
  - 或替换为更稳定的 UI 元素截图作为新模板。

---

# 异环(nte) ok-nte 每日进度解析修复

## 问题
「每日进度」里异环游戏内进度要么显示占位「待 ok-nte 执行」、要么数据不对。根因**不是路径**，而是解析器对日志格式猜错。

## 根因（已核实）
- 数据路径正确：来自 `app_config.json` 的 `apps.NevernessToEverness._discovery.monitors.ok_nte_daily_log_monitor.log_dir` = `E:\GithubProject\autoGame\ok-nte\data\apps\ok-nte\working\logs`（确认存在今天的 `ok-script.log`）。
- 旧解析器 `LOG_LINE_PATTERN` 写死 `DailyTask:`，但真实日志标签是 **`DailyRoutineTask:`**（带 Routine）→ 正则完全匹配不上 → `parse_ok_nte_log` 返回 `None` → bridge 一直塞占位。
- 旧 `KEY_TASKS = ("领取活跃度奖励",)` 是虚构任务名；ok-nte 实际任务名（异象界域/一咖舍/日常领取/影院约会/喷泉签到/羁遇赠礼/异象家具/异象追猎）完全不同，收尾汇总行的权威状态在 `success:/failed:/skipped: [...]`（`failed:`/`skipped:` 有时是续行、无时间戳前缀）。即使匹配上 `is_all_success` 也永远 `False`。

## 改动（services/ok_nte_daily_parser.py）
1. 重写解析：**优先**用收尾汇总行 `SUMMARY_PATTERN = \b(success|failed|skipped)\s*:?\s*(\[.*\])\s*$`（容忍带/不带 INFO 前缀与冒号、续行无时间戳），`ast.literal_eval` 解析列表；三类都为空时**回退**逐任务事件 `TASK_LINE_PATTERN = DailyRoutineTask:(开始任务|任务完成|任务失败): 任务名`。
2. 合并去重：`failed > completed(success) > skipped`；tasks 状态用 `success/failed/skipped`。
3. `is_all_success = len(failed)==0`（无失败=整体成功；skipped 视为「未执行」非失败）；`has_failed = len(failed)>0`；`progress_percent = completed/total*100`。删除虚构的 `KEY_TASKS` 判定。
4. `_detect_log_date` 改为扫描整文件取**最后一个**日期（兼容跨日滚动的当前日志）。

## 验证（真实日志实测）
- `parse_ok_nte_log(today ok-script.log)`：`is_all_success=False, has_failed=True, progress=75`，tasks = 6 success + 异象家具 failed + 异象追猎 skipped，全部正确。
- `parse_latest_ok_nte_log()` 走完整 app_config 链路同样正确。
- 模拟 bridge nte 合并分支：社区任务保留、非社区「异环签到」被丢弃、ok-nte 8 项并入，最终整体 not-all-success（异象家具 failed）。
- `ok_nte_daily_parser.py` 与 `ui/bridge.py` `py_compile` 通过。

## 调参提示
- 若后续 ok-nte 把「整体成功」定义成「关键任务完成即可、个别失败无所谓」，在 `parse_ok_nte_log` 里把 `is_all_success = len(failed)==0` 改成基于具体关键任务名判定即可（目前按真实日志语义：有失败即整体未完成）。

---

# 固定点击序列「录制 / 回放」模块（runtime/click_sequence.py）

## 用途
机械性地按"用户点击顺序"重放固定流程：录制鼠标点击位置（带顺序），回放时按序点击。补充了项目原本缺的"录制"环节（回放引擎 `maintenance/screen_task_runner.py` 已有雏形）。

## 关键设计
- **坐标策略**：录制时同时保存屏幕绝对坐标 `(x,y)` 与目标窗口相对坐标 `(rel_x,rel_y)`。回放优先用窗口相对坐标（当前窗口左上角 + rel，**窗口移动也不怕**），找不到目标窗口则回退绝对坐标（双保险）。
- **录制**：`ClickRecorder` 用 pynput 全局鼠标钩子监听点击（只记按下），按窗口标题关键字过滤（默认仅录该窗口内点击），按 **ESC / q** 停止，保存为 `click_sequences/<name>.json`。
- **回放**：`ClickSequencePlayer.play()` 按序 `pyautogui.click`，支持 `loop` 循环、`min_gap/max_gap` 间隔裁剪、`default_gap` 兜底、`dry_run`（只打印坐标不真点）、`stop_event` 线程安全中止；启用 `pyautogui.FAILSAFE`（鼠标甩到屏幕角落即停）。
- **存储目录**：`settings/paths.CLICK_SEQUENCES_DIR` = `BASE_DIR/click_sequences`（新增常量）。

## CLI 用法
```bash
# 录制（先打开目标游戏窗口，只录该窗口内点击）
python -m game_auto.runtime.click_sequence record 我的流程 --win 游戏窗口标题关键字

# 回放（--loop 2 循环两次；--dry 先试跑看坐标不真点）
python -m game_auto.runtime.click_sequence play 我的流程 --loop 1 --dry
python -m game_auto.runtime.click_sequence list
```

## 依赖与验证
- 新增依赖 `pynput`（已装进 venv，并登记 `requirements.txt` 与 `services/dependency_manager.py` 的 `REQUIRED_PYTHON_PACKAGES`）。
- 自测：构造序列 → 保存/加载 round-trip、`_resolve_coord` 窗口相对换算、`dry_run` 不真点、`stop_event` 中止、`list` 全部通过；`py_compile` 通过。

## 时间轴修正（gap_after 归属）
- 每条点击存两类时间：绝对时间戳 `ts`（墙钟，审计用）+ 到下一步的真实间隔 `gap_after`（回放 sleep 还原节奏）。
- 修复 off-by-one：原实现把"上一步→本步"的间隔挂在**本步**上，导致首步等待丢失、末步等待错位（点击触发长加载时等待挂错步）。现改为收到下一步时把间隔回填给**上一步**，`stop()` 给末步置 0。
- 验证：`gaps[i] ≈ ts[i+1]-ts[i]` 偏差 0.000。

---

# 点击录制 UI（QML 视图 + bridge 接线）

## 界面结构（新增 `ui/qml/views/ClickSequenceView.qml`）
侧边栏新增「点击录制」项（图标 `HusIcon.VideoCameraOutlined`），视图分两列：

**左列 · 录制控制**
- 序列名称
- **目标窗口下拉**（见下节「录制时可选窗口」）：列出当前所有可见窗口，选中后只录该窗口内点击并记录相对坐标；首项「无」= 记录全屏所有点击
- 开始/停止录制按钮（绿 ↔ 红切换，录制中禁用回放按钮防冲突）
- 状态点 + 实时计数「已记录 N 次」
- 最近一次点击：绝对坐标 / 相对坐标 / 按键 / 距上步间隔
- 提示：热键 ESC / q 停止；鼠标甩到屏幕角落可紧急停止回放

**右列 · 序列列表 + 步骤时间轴（核心数据展示）**
- 序列列表：名称 / 步数 / 创建时间 / 目标窗口，行内「回放 / 试跑 / 删除」，点名称区加载明细并高亮
- **步骤时间轴**：每次点击 = 一个节点，**节点间连线宽度 ∝ `gap_after`**（1s≈6px，夹 [14,120]px），≥2s 的等待标橙色 —— 一眼看出哪步在等加载/过场；节点可点击
- 选中步骤详情面板：绝对/相对坐标、按键、距上步间隔、时间戳
- 下方可滚动步骤表（精确数值），与 timeline 联动高亮
- 循环次数按钮组 1/3/5 + 回放 / 试跑（不真点）/ 停止回放

## 接线（ui/bridge.py）
- 信号 + 只读属性：`clickSeqRecording` / `clickSeqCount` / `clickSeqLastJson` / `clickSeqListJson` / `clickSeqDetailJson` / `clickSeqPlaying`（QML 侧 `JSON.parse` 后绑定）
- Slot：`startClickRecording(name,win,hwnd)` / `stopClickRecording()` / `playClickSequence(name,loop,dry)` / `stopClickPlayback()` / `deleteClickSequence(name)` / `refreshClickSequences()` / `loadClickSequenceDetail(name)` / `refreshWindowList()` / `focusWindow(hwnd)`
- 录制：`_make_ui_click_recorder_class()` 懒加载 `ClickRecorder` 并返回子类，覆写 `_on_click` 回调 bridge 推 UI（pynput 监听线程 → Qt 信号跨线程自动排队）；`start()` 非阻塞，另起 watcher 线程等热键 ESC/q 或 UI 停止后 `_finalize_click_recording()` 收尾保存（`finalized` 标志防重复收尾）
- 回放：daemon 线程 + `threading.Event` 支持中止
- `switchView()` 白名单已加 `"click_sequence"`（漏了会导致侧边栏点了不切视图）

## 验证（offscreen QML，无需启动 GUI）
- `Main.qml` 以 `currentView='click_sequence'` 加载：rootObjects=1，**0 条 ClickSequence 相关警告**
- 视图独立实例化：**0 errors**；喂 6 步含 12s 长等待的数据后渲染 **0 警告**（时间轴/连接线宽度/required 属性/步骤表均正常）
- bridge 断言：新属性与方法齐全、序列列表与步骤明细 JSON 正确、`switchView` 白名单生效、删除后列表清空

---

# 录制时可选窗口（下拉选择 + 句柄锁定）

原实现只能手填「窗口标题关键字」，用户需要先知道自己要填什么、还容易填错/匹配到同名窗口。改为下拉点选。

## 数据流
1. `runtime/click_sequence.py` 新增 `list_windows()`：枚举可见窗口，返回 `hwnd/title/exe/pid/rect/width/height/minimized/foreground`
   - 过滤：无标题、尺寸 < 200×150、DWM cloaked 的 UWP 幽灵窗口（`DwmGetWindowAttribute(DWMWA_CLOAKED=14)`）、**本程序自己的窗口**（按 `os.getpid()` 比对，避免选到自身）
   - 进程 exe 用 ctypes `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `QueryFullProcessImageNameW` 取，带 pid 级缓存，**不引 psutil 依赖**
   - 按 EnumWindows 的 Z 序返回（前台窗口在前，标记 `▶`），最小化窗口沉底
2. bridge：`windowListJson` 属性 + `windowListChanged` 信号 + `refreshWindowList()` / `focusWindow(hwnd)` 两个 slot
3. QML：`HusSelect` 下拉（`ListModel` 动态重建，label = `标题 [宽×高] · exe`）+ 「刷新」按钮 + 选中窗口状态卡（位置/尺寸/exe）+「把该窗口置前」按钮
4. 录制：`startClickRecording(name, title, hwnd)` 多传一个句柄 → `ClickRecorder(window_hwnd=...)`

## 句柄锁定（关键）
`_resolve_window(keyword, hwnd)` 优先按句柄定位，句柄失效才退回标题模糊匹配。
- 多开同名窗口（如两个游戏窗口）时锁定到选中的那一个，不会跑到另一个上去
- 录制中游戏重启（句柄失效）：bridge 在 `startClickRecording` 里检测 `_is_window_alive(hwnd)`，失效则退回标题匹配 + 橙色提示
- 回放仍按标题匹配（JSON 里存的是标题，句柄跨进程重启无意义）

## 交互细节
- 开始录制后延迟 0.6s 自动把目标窗口置前（省去用户手动切窗口），录制结束时 `_focus_own_window()`（按 pid 找本程序主窗口）把本程序叫回前台
- 录制中锁定下拉不可改（`enabled: !bridge.clickSeqRecording`）
- 刷新列表后按句柄 → 按标题的顺序尝试保住原选中项

## 验证
- `list_windows()` 实测 14 个窗口，游戏窗口识别为 `HTGame.exe 异环`、工具窗口 `pythonw.exe ok-nte`
- offscreen QML：`Main.qml` 加载 **0 警告**；视图实例化 **0 警告**；`reloadWindows()` 后 `winModel.count = 窗口数+1`，`applyWindow(1)` 后 `selHwnd/selTitle` 与状态文案正确，切回 0 复原
- 录制端：锁句柄后窗口内点击记录且 `rel=(10,10)` 正确、窗口外点击被过滤
- bridge 三参数启停：`startClickRecording(name,'',0)` → 2 步 → `stopClickRecording()` 正常保存并复位状态（测试文件已清理）

## 坑位记录（本节）
- **`HusSpin` 在本项目是加载转圈（BusyIndicator），不是数字输入框**，循环次数改用按钮组。
- offscreen 测试里 `QMetaObject.invokeMethod` 调 QML JS 函数静默失败，**用 `QQmlExpression` 更可靠**；且 context 必须取 `QQmlEngine.contextForObject(root)`，直接用 `engine.rootContext()` 解析不到 QML 里的 `id`（`winModel` 会 undefined）。
- 最大化窗口的 `GetWindowRect` 会超出屏幕（如 `(-8,-8,1928,1040)`），写"窗口外点"的测试用例别用 `(5,5)`。
- `HusIcon` 的可用图标名定义在 `.venv/.../pyhuskarui/controls/husiconfont.py`（如 `VideoCameraOutlined = 0xED3B`），不在 `HusIconText.json`。
- QML 校验可用 `QT_QPA_PLATFORM=offscreen` + `QQmlComponent` 加载，`engine.warnings` 捕获运行期警告，不必真开窗口。
