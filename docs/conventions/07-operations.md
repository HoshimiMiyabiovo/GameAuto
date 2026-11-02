# 定时任务、更新日历、薅羊毛与月度任务

> 由 AGENTS.md 拆分；改动相关代码前先读本篇。

### 薅羊毛日志核验 (2026-10-24)

- **MiniMax**：`%APPDATA%/MiniMax/logs/api-MM-DD.log` 的
  `POST .../minimax-cloud/api/v1/signin/claim`，HTTP `status 200` 且
  `baseRespStatusCode:0` 才算成功。2026-10-24 12:18:10 日志实锤领取成功，
  但旧 UI「今日已签到」确认误报 ❌ —— **日志比 UI 准**。
- **TRAE**：`%APPDATA%/Trae CN/logs/<YYYYMMDD...>/main.log` 的
  `ttnet fetch response .../checkin_credits/claim 200 OK`。
- **WorkBuddy**：没有可用的本地领取日志（`%LOCALAPPDATA%/WorkBuddy/logs/main.log`
  停在 2026-09-03，`debug.log` 只有 crashpad 错误），继续用 UI「今日已领取」
  非 optional 确认；不要假装日志能覆盖它。
- `flow_configs.json` MINIMAX/TRAE 末步 `action=wool_verify_log`
  （`runtime/wool_claim_logs.py`）：只读当天日志，找不到领取记录就 ❌，
  不回退成假成功；MiniMax 额外用「今日已签到」模板兜底（已签到当天不会重复发 claim）。
- WorkBuddy 默认 `close_process_on_finish/fail=false`；用户显式勾选「完成后关闭」时按其选择执行
  （2026-10-30 修正，不再硬拦截）。回归 `tests/suite_wool_claim_logs_20261024.py`（本机真机脚本）。
### 薅羊毛 2026-10-25 三连 ❌：UI 确认比实际动作更脆弱

当天 12:40 结果 `MINIMAX ❌ WORKBUDDY ❌ TRAE ❌ QODER ✅`，三家根因都不是"没点"：

- **TRAE 是假失败**：12:36:34 日志已有 `checkin_credits/claim 200 OK`（领取成功），
  流程却卡在「等待签到参与抽奖弹窗」——该弹窗不一定出现，且原配置 max_attempts
  让它空等 3 分钟，没走到末步日志核验。修复：抽奖弹窗两步 `optional_if_missing`、
  等待 15s/1 次；成败仍由末步 `wool_verify_log`（非 optional）决定。
- **WORKBUDDY 是确认太早**：`confirm_text_change` 原实现只在 `wait_time`（≤3s）
  后查一次 OCR，`timeout_sec` 只作用于模板；而领取接口实测 ~6s 才回，
  4 次重试全部误报"未检测到文字变化"。修复：`confirm_text_change` 改每 2s 轮询
  到 timeout_sec；配置上点击步直接 `confirm_type=none`，由后续非 optional 的
  「今日已领取」验证（保留 10-21 规则：optional 点击后必须有非 optional 验证）。
- **MINIMAX 是真没领到**：`api-10-25.log` 只有 `signin/status`，没有 `signin/claim`
  （10-24 是有的）。旧侧边栏「签到得」入口已随 3.1.6 新 UI 失效，步骤1 的
  `confirm_type=disappear` 也必然超时。**新 UI 实测（用户截图）**：左下角昵称 →
  菜单「每日签到」→ 签到卡片（卡片显示「本轮已连续签到 N 天」/「今日已签到」）。
  流程已改为 5 步：点昵称 → **悬停**菜单「每日签到」（浮层卡片，不点击）→
  等卡片（OCR 连续签到）→ 可选点「立即签到」→ 末步日志核验；模板
  `minimax-昵称.png` / `minimax-每日签到菜单.png` / `minimax-今日已签到.png`，
  阈值必须 0.9（按钮模板在无关页面有 0.855 误命中，菜单行关闭时只有 0.50）。
  参考截图 `artifacts/ref_minimax_ui_*_20261025.png`。**未签到状态下的按钮文案
  还没有截图**，当前靠 OCR「立即签到」可选点击 + 日志核验；若次日仍无 claim，
  先补一张未签到卡片图。runner 新增 step 级 `hover: true`（只 moveTo 不点击，
  `_hover_window_local`，同样受 fail-safe 保护）。
- 回归 `artifacts/test_wool_flow_fixes_20261025.py`（33 项：配置约束 +
  text_change 轮询 + 日志解析 + MiniMax 新 UI 三模板在用户截图上的命中/误命中 +
  hover 坐标换算）；既有 confirm/wool/Qoder 4 套件 ALL PASS。
- 改 `flow_configs.json` / Python 后必须重启 GUI 才生效。
### Qoder 每日积分领取流程（2026-10-20 接入薅羊毛集合）

- **任务事实**：Qoder（AI IDE，`C:\Users\admin\AppData\Local\Qoder\Qoder Launcher\Qoder Launcher.exe` 启动，真实进程 `Qoder.exe`）每天 10:00（UTC+8）刷新 100 Credits，领取后 30 天有效。活动弹窗由 app 内嵌 webview（Surface）承载，标题「专属活动权益 立即查收」。
- **弹窗行为模型（日志实证，`%APPDATA%/com.qoder.app.stable/logs/<session>/main.log` 的 `[Campaign]` 行）**：
  - `claimable=true`（今日未领）**不保证启动自动弹**：2026-10-24 全天的会话日志都是
    `hasClaimableCampaign:true` + `shouldShowCampaignPopup:false`，新启动也没有
    `source:automatic` 的 Surface 事件。**不能把「启动自动弹窗」当前提**。
  - 手动入口确实存在：2026-10-24 15:40 用户从 `source:"settings_menu"` 打开了
    Surface（`活动 Surface 已打开 ... "claimable":true,"source":"settings_menu"`）。
    具体菜单项位置仍待截图补模板；不要再用「USAGE placement 礼品图标」的旧猜测。
  - 点「领取」→ 服务端 `claimStatus: CLAIMABLE → CLAIMED`；验证以 Qoder 日志为准。
- **流程设计**（`flow_configs.json` 的 `QODER`）：等待领取按钮（30s，optional）→
  点击领取（optional）→ **`qoder_verify_claim` 非 optional 日志核验**。
  核验读取最新 `main.log` 的 `[Campaign]` 状态：`CLAIMED/claimable=false` 才算成功，
  `CLAIMABLE/claimable=true` 或读不到状态一律失败。**禁止再靠三步全 optional 静默报成功**。
- **默认 `close_process_on_finish/fail=false`**：Qoder 是用户日常 IDE；用户可在 GUI 薅羊毛页
  显式勾选「完成后关闭」（2026-10-30 明确要求），勾选后必须执行，不要用代码硬拦截。
  `reuse_existing_window=true`：已在跑就复用窗口不新拉进程。
- **2026-10-24 事故（用户实锤）**：12:21（薅羊毛）和 12:53（QoderFlow）两次 QODER
  流程都没找到「领取」→ 三步 optional 全跳过 → 报 ✅，并且
  `close_process_on_finish: true` 把 Qoder 进程树杀了两次；当天 credits 一直未领。
  修复：关闭开关改回 false；末步换成日志核验 `runtime/qoder_claim.py`；
  回归 `artifacts/test_qoder_claim_20261024.py`。
- **常驻入口（2026-10-24 用户截图实锤）**：左下角头像 → 菜单「权益中心」
  （gift 图标带红点）→ 活动弹窗。`runtime/qoder_claim.open_campaign` 驱动：
  菜单行模板 `模板/qoder-权益中心.png`（185x36）；活动页只认
  `qoder-领取.png` / `qoder-已领取.png` 模板，**禁止用泛化「领取」OCR**。
- **2026-10-24 实跑假阳性教训**：Qoder 窗口被聊天窗口遮挡时，OCR 把聊天正文里的
  「领取」当成了弹窗按钮 → 等待步命中、点击步模板没命中却跳过，最后靠日志里的
  `hasClaimableCampaign=false` 误判成功。现在：打开/等待/点击全部走模板；
  窗口遮挡 ≥30% 先聚焦重试，仍遮挡直接诚实失败，绝不 OCR 其他窗口。
- **模板匹配阈值踩坑（2026-10-20 实测）**：`_match_template_cv2_once` 的生效阈值是 `min(match_confidences)`——列表写 `[0.88,0.84,0.8]` 实际按 0.8 匹配！QODER 的「领取」模板在 Qoder **设置页侧边栏**有 0.835 误命中（聊天页 0.595），0.8 档会放过关进而误点侧边栏。QODER 用 `[0.94,0.92,0.9]`（生效 0.9）：真实弹窗命中 1.000、误命中天花板 0.835，分离干净。**新建 Electron 应用流程务必先测误命中再定阈值**。
- 模板：`模板/qoder-领取.png`（黑底白字按钮，48x37）、`模板/qoder-已领取.png`（灰底白字，62x38）。点击步 `enable_ocr_fallback=false`（OCR 子串匹配会把「已领取」当「领取」命中，且描述行「领取后 30 天有效」也含"领取"）。
- **假失败事故（2026-10-21 10:03，服务端日志实锤）**：领取其实**成功了**（点击后 7.5s 服务端 `CLAIMABLE→CLAIMED`），但消失型确认连报 4 次"元素仍存在"→ 假 ❌。根因是 `confirm_disappear` **只在 +3s 单次检查**——Qoder 领取成功后弹窗要 **~30s 才关闭**（昨天实测：点击后 30s 弹窗消失；今天按钮持续可匹配 25s+）。
  修复：`confirm_disappear` 改为**每 2s 轮询直到 timeout_sec**（2026-10-21），QODER 点击步 `confirm_timeout_sec=45`。回归 `artifacts/test_confirm_disappear_poll_20261021.py`（8 组：延迟消失等到/永不消失超时/确认模板组合/立即消失快速返回）。
  阈值防线：QODER `match_confidences=[0.97,0.96,0.95]`（生效 0.95）——黑模板对灰「已领取」按钮真实交叉匹配只有 0.754（干净复测；昨天说的 0.922 是测量 bug：循环里覆盖了模板变量导致复合缩放，作废），真匹配 1.000，分离干净。
- OCR 对「领取」有子串误中风险（描述行含"领取"），仅等待步开 OCR 兜底、点击步纯模板。
## 定时任务补跑 (2026-10-21)

- GUI 定时任务保存在 `app_config.json` 的 `scheduled_tasks`，`UiBridge._sched_timer`
  每 30 秒检查一次；GUI 必须常开才会触发。撞上正在跑的任务会进入
  `_pending_scheduled` 排队，到点不丢。
- 每个任务可带 `skip_if_done_today: true`（UI 勾“仅补跑今日未完成”）：到点后
  `_fire_scheduled_task` 传 `force_rerun=False`，`run_task_batch` 会按
  `_is_game_script_done_today` 逐游戏跳过已完成项；缺省/False 保持旧的强制重跑。
- **`_is_game_script_done_today` 脚本成功 ≠ 当日完成**（2026-10-24 修正）：
  白荆回廊把部分完成（1/15 任务、活跃度 5/100）也记 success（10-20 设计），
  曾导致手动运行被"今日脚本任务已全部成功"跳过。现在脚本行全成功之外还要求
  `completion_state ∈ {completed, unknown}`——关键指标未满（pending）或有关键
  失败一律视为未完成，允许手动/补跑重新执行。副作用：活跃度不满的游戏
  （如鸣潮 40/100）在当天第二次批次会重跑一次脚本（幂等，无害）。
  手动运行想无视跳过逻辑就勾 AutomationView 的"强制重跑"勾选框。
  回归 `artifacts/test_done_today_predicate_20261024.py`（12 项）。
- **qBittorrent 未配置不再报错**（2026-10-24）：`QBITTORRENT["path"]` 默认空，
  `Popen([""])` 曾在每批次抛 WinError 87；`ensure_qbittorrent_running` 现在
  空路径直接跳过（debug 级日志）。要用 qBittorrent 就在设置里配好路径。
- 2026-10-21 晚已删除 11:05 / 11:35 两条固定兜底，只保留 Qoder 10:01：
  固定时间兜底每天空跑，而各游戏更新日期/开服时间不同，靠时间猜不智能。
- 维护日改为**开批前预检**：`UiBridge._split_maintenance_targets` 在
  `run_task_batch` 前逐个 `get_maintenance_state`，维护中的游戏从本批剔除并按
  公告开服 +5min 排队补跑，其余游戏照常执行；全部维护中时传空列表（仍执行启动前签到）。
- 批次串行：`startAutomation` 在锁内同步置 `_is_running`，同一 30s tick 内
  定时任务/更新补跑不会并发启动；`email/shutdown` 作为批次参数传入，全部任务
  结束后统一发一封日报、只调度一次关机。
- 改 `ui/bridge.py` / QML 后必须重启 GUI 进程才生效（运行中的 GUI 不会热加载
  Python/QML）。
## 游戏更新日历与动态补跑 (2026-10-21)

- `services/game_update_calendar.py` 拉官方公告 `getAnnContent`：
  原神国服 `hk4e_cn`、崩铁 `sg-hkrpg-api.hoyoverse.com/common/hkrpg_global`、
  绝区零 `sg-announcement-static.hoyoverse.com/common/nap_global`，`lang=zh-cn`。
  公告时间为 UTC+8，解析“更新时间 / 更新开始时间 … 06:00 … 预计 N 个小时”，
  `开服 = 开始 + N 小时`。缓存 `storage/cache/game_update_calendar.json`（6 小时 TTL）。
- 2026-10-21 实测：原神 7.1 2026/09/23 06:00+5h、崩铁 4.6 2026/09/28 06:00+5h、
  绝区零 3.3 2026/10/21 06:00+5h（今天 11:00 开服）。
- `automation_controller._monitor_game` 给三款米游加 `progress_hint`：公告维护窗口内
  快速跳过（`GAME_UPDATING_SKIP`），不白等 launch 超时。
- `UiBridge` 动态补跑：维护跳过/失败 → `_handle_update_retry_result` 按公告开服 +5min
  写入 `storage/cache/update_retries.json`；`_check_update_retries` 到点用
  `force_rerun=False` 补跑（最多 3 次）；补跑成功/已无需补跑/达到上限后自动移除；
  `_unfired_scheduled_today` / 关机顺延包含补跑条目。
- 维护窗口边界用严格小于：11:00 开服、buffer 5 分钟 → 11:05 视为已开服，
  避免 11:05 的补跑又被维护检查跳过。
- 2026-10-21 扩展：鸣潮用官方 CMS
  `media-cdn-mingchao.kurogame.com/akiwebsite/website2.0/json/G152/zh/`
  （ArticleMenu.json + article/<id>.json，正文「更新维护时间：2026年9月30日04:00 ~ 11:00」）；
  异环用官网静态公告列表 `yh.wanmei.com/m/news/gamebroad/index.html`
  （des 含「10月15日06:00 - 11:00」）；无限暖暖用官方
  `https://infinitynikki.nuanpaper.com/api/news?offset=0&limit=30` 列表 +
  `/news/<id>` 详情页（服务端渲染），解析「2026年10月12日10:00-11:00（UTC+8）
  进行停服维护更新」；实测 2.10 的 10/12 更新为 10:00-11:00。只有接口失败时
  才退回 10:00-11:00 窗口兜底。日期来自公告本身，不要再用固定窗口猜日期。
- 维护检测有两层：`UiBridge._split_maintenance_targets` **开批前预检**（维护中的
  游戏不进入本批，直接排队，避免空跑/半开启动器）；`task_batch_runner._run_single_task`
  **启动前**再查一次（覆盖 GUI 未预检、WebUI/CLI 调用、批次中途进入维护的情况）。
  公告维护窗口内直接 skipped（不启动 launcher/脚本），桥接层按公告开服 +5min 排队；
  `automation_controller` 的 progress_hint 覆盖三款米游 + 鸣潮 + 无限暖暖，
  异环保留 ok-nte 更新日志检测并叠加公告维护状态。
- Qoder：`flow_configs.json` QODER 加 `not_before: "10:00"`；新增独立
  `QoderFlow`（`STANDALONE_TASK_APPS`）只跑 QODER，定时 10:01；QODER 的
  `close_process_on_finish=false`，**不许关 Qoder**；定时任务支持任务级
  `email_on_finish=false`，补跑任务不重复发邮件。
- 定时配置（2026-10-21）：Qoder 10:01（QoderFlow）；固定兜底 11:05 / 11:35 已删除，
  维护补跑完全由动态队列驱动（`storage/cache/update_retries.json`）。
- 改 bridge/QML 后必须重启 GUI 才生效。
## WorkBuddy 2026-10-29：验证假失败 + 误关进程

- **现象**：WoolFlow 两次（10:14、20:16）都报 `WORKBUDDY ❌`。20:16 日志显示
  步骤 1-6 全部命中并点击，步骤 7「确认今日已领取」连续 3 次超时。
- **根因**：
  1. 点完「立即领取」后加油站页会关闭（10-27 已知），重新导航的步骤 5/6
     用模板匹配，`hy4-Buddy加油站菜单.png` 同时命中聊天侧栏的促销卡
     「做任务赢积分好礼」——失败截图左下角就是这张卡；点它不会打开加油站页，
     于是验证步找不到「今日已领取」→ **假失败**（领取动作本身大概率已成功）；
  2. 收尾 `_close_wool_flow` 只认 `close_process_on_finish`，WorkBuddy 即使
     `close_process_on_fail=false` 仍被强制关闭（它是用户日常聊天软件，
     本地配置的 finish 还被误设成 true）。
- **修复**：
  - 步骤 2/6 改为**纯 OCR** 定位「Buddy加油站」（去掉 template、
    `enable_ocr_fallback=true`、`on_fail=retry_prev` 回退重点头像）。
    OCR 实测：菜单行 0.81；侧栏促销卡文本是「连登抽取Buddy周边」，
    不会命中「Buddy加油站」，不会再点错。
  - WORKBUDDY / QODER 默认 false（example 保持安全默认）；**用户可在 GUI 自行勾选
    「完成后关闭」**。2026-10-30 用户明确要求关闭这两个软件，本地配置已按选择设为
    `finish=true / fail=false`；曾加的 `PROTECTED_CLOSE_FLOWS` 硬保护与 QML 锁定已撤销，
    禁止再用代码拦截用户选择。
  - `_close_wool_flow(logger, flow_key, cfg, ok)`：成功看 finish、失败看 fail，
    WorkBuddy 失败时仍按 `fail=false` 保留进程。
- **回归**：`tests/suite_wool_workbuddy_20261029.py`（7 步 OCR 配置、
  两个软件关闭开关、收尾四种组合）。
- **待实机确认**：20:16 那次点击「立即领取」后页面关闭，是否真的到账需要
  下次运行用「今日已领取」验证；若仍失败，先看新截图再改。
## 崩铁自塑尘脂每月合成（HSR_MONTHLY 第 3 步，2026-11-01）

- **路径**（用户 4 张截图 + 实机确认）：主界面 `B` → 背包 → 顶部**倒数第二个**图标「贵重物」
  → 第一行第 5 格 自塑尘脂 → 右侧「获得途径」第二条「万能合成机 - 使用遗器残骸合成」
  → 合成界面（材料合成）→ 点到「本月还可合成 0」。
- **成本与上限**：一次消耗**遗器残骸 ×800**（2026-11-01 实机：持有 20940/800）；自塑尘脂
  **每月限购**，本月满了之后右侧显示「本月还可合成：0」、底部按钮变红「已达上限」。
  用户口径（2026-11-01）：**能合多少合多少**，材料用完或到月上限都算正常结束。
- **判据只有一个**：右侧「本月还可合成：N」。
  - N=0 / 按钮「已达上限」/「材料不足」→ 一次都不点，正常返回；
  - N>0 → 点底部合成 → 重读 N，每减 1 记一次成功；**连续两次不减**判定卡住并失败（不无限点）。
  - 注意「材料合成」标题里也有「合成」二字，按钮 OCR 必须限定 `craft_button_region_frac`
    （默认窗口下半部中间），否则会把标题当按钮点。
- **模板**：`模板/崩铁-自塑尘脂-已达上限.png`（873×47，红条 + 白字，从实机截图
  `data/artifacts/ref_starrail_resin_synth_20261101.png` 裁，自匹配 **1.0000 @ (666,847)**）。
  贵重物页签 / 自塑尘脂格子 / 万能合成机条**没有裁模板**——用户给的是 1091×613 缩略图，
  到 1920 画面是 1.76 倍，现有 `match_scales=[1.0,1.25,0.8]` 匹配不上，所以改用 `point_frac`。
- **比例坐标是唯一依据，未实机跑过（务必知道）**：`tab/item/source/craft_button` 四个
  `point_frac` 按 1920×1080 截图量的，2026-11-01 用户选择「先不验，等 12 月实跑」。跑偏的后果可控：
  页签/格子点歪 → 详情不显示「自塑尘脂」→ 整步失败；来源条点歪 → 看不到「本月还可合成/所需材料」
  → 整步失败；**任何一步失败都会先 `_leave_to_main` 退回主界面**再返回，不会把游戏留在背包页
  （留在背包页会让下一次 `starrail_enter_main` 认不出主界面）。
- **接线**：`runtime/hsr_resin_craft.py::craft_self_modeling_resin`；
  `foreground_flow_runner._dispatch_step_action` 新增 `hsr_resin_craft` 分支；
  `flow_configs[.example].json` 的 `HSR_MONTHLY.steps` 追加第 3 步（余烬兑换之后）。
  不独立成任务，随 `HSR_MONTHLY` 每月一次。
- **回归**：`tests/suite_hsr_resin_craft_20261101.py`（47 项断言，ALL PASS）：
  「0 次不点」「N 递减到 0」「材料不足」「已达上限模板」「卡住失败」「用户停止」
  「背包已开不按 B」「按钮区域排除标题」「失败必退回主界面」「dispatch 接线」「配置/模板自匹配」。
- **首次实跑看什么**：日志 `[自塑尘脂]` 前缀会逐条打印「已在/已切到」「模板/比例坐标 @ (x,y)」。
  12 月 1 日若失败，先看命中坐标与失败截图，再按实机画面补模板（补了模板就会自动优先走模板，
  比例坐标自动降级为兜底）。
## 绝区零恶名狩猎每周三次 + 自动战斗移植（2026-11-01）

- **路径**（用户 9 张截图逐步确认）：主界面 `F2` → 快捷手册「训练」页签 → 左侧「恶名狩猎」
  → 「库萨里库」行「前往」→「是否立即传送？」确认 → 详情页「下一步」→ 配队页「出战」
  （**配队不改**）→ 等右上角开始计时 → 向前 `W` 走 2 秒 → 自动战斗 → 结算「完成」
  → 循环到本周 3 次 → 左下「奖励入口」→「周期挑战 → 全部领取」→ 返回。
- **自动战斗是照抄 OneDragon 的代码，不是调它的 exe/接口**（用户 2026-11-01 原话：
  「照抄自动战斗代码，那玩意不一定能接出来」）。移植清单：
  - `one_dragon/yolo/yolov8_onnx_cls.py` + `onnx_utils.py` → `FlashClassifier`
    （letterbox 640×640、左上补 114、/255、CHW）；
  - `zzz_od/auto_battle/auto_battle_dodge_context.py` → 闪光闪避（1=红→闪避，2=黄→格挡，都是空格）；
  - `screen_info/battle.yml` → `zzz_battle_assist.py` 顶部的 4 个区域常量（1920×1080 基准）；
  - `template/battle/btn_*.png` → `模板/绝区零-战斗-*.png`（9 张）；
  - `models/flash_classifier/yolov8n-640-flash-20250921/model.onnx` →
    `data/models/flash_classifier/...`（5.7MB，走 `data/` 不进版本库）。
  - **GPL-3.0 本地自用**，与之前复制 OneDragon 模板同一口径。
- **主动砍掉的（不是忘做）**：Yolo 距离识别 + 自动转向（用户给了「W 走两秒」）；
  音频闪避（soundcard/librosa/scipy/sklearn 四个重依赖）；24k 行 YAML 配队策略 + 条件状态机
  （换成**针对固定配队手写的轮转**，见下条）；逐角色 agent_state 层数识别（先用固定节奏代替）；
  连携技头像模板匹配（换成区域亮度，**实测不可靠所以默认关闭**）。
- **固定配队是紊乱队（2026-11-01 核实）**：用户配队是 蕾米埃尔 / 菲欧妮 / 维琳娜。查
  `src/zzz_od/game_data/agent.py` 确认三人**全是 `AgentTypeEnum.ANOMALY`**：
  - 蕾米埃尔 `remielle`：`DmgTypeEnum.LUMIFLUX`（流光），状态「浮晖」层数条 +「虚曜」连接；
  - 菲欧妮 `phoenix`：`FIRE`（火），状态「燔焰」层数条 +「升变」连接；
  - 维琳娜 `velina`：`WIND`（风），状态「风华」层数条。
  三种属性 = 典型**紊乱队**：A 打出异常 → B 用另一种属性再打出异常 → 触发紊乱高伤。
  所以正确打法不是「谁站场谁连点」，而是**三人轮流上场各自把 E 打出去**。
  **更正（2026-11-01 22:25）**：上一版这里写成「通用策略一次都没引用这三个人的状态模板」，
  是**搜错了关键字**（搜英文 `remielle/phoenix` 未命中，配置里用的是中文状态名）。实际上
  `config/auto_battle/全配队通用.merged.yml` 里 蕾米埃尔 52 行、菲欧妮 20 行、维琳娜 24 行引用，
  另有 `auto_battle_state_handler/速切模板-{菲欧妮,维琳娜}.sample.yml`、
  `蕾米埃尔-入场.sample.yml`、`连携模板-蕾米埃尔.sample.yml`、
  `auto_battle_operation/{菲欧妮-长按普攻-焚罪,蕾米埃尔-入场技}.sample.yml`。
  **OneDragon 是给这三人做过适配的**，而且写得比固定节奏轮转精细得多（见下）。
- **战斗循环（`zzz_battle_assist.py`）**：优先级 闪避 > 终结技 Q > 到点切人 > 特殊技 E > 普攻。
- **键位（2026-11-01 二次核实，之前写反过一次，务必按这个来）**：ZZZ PC 默认
  **闪避 = Shift（或鼠标右键）、切换下一位 = 空格、切换上一位 = C、特殊技 = E、
  终结技 = Q、普攻 = 鼠标左键；1/2/3 是连携队友，不是切人**。
  依据：OneDragon `_KEY_DEFAULTS`（`dodge: shift` / `switch_next: space` / `switch_prev: c`）
  与 ZZZ 官方默认完全一致；用户截图里 Space 标在黄色圆环按钮上，那个是**切人**按钮。
  踩过的坑：曾误判成「空格闪避 + 1/2/3 切人」，那样会变成该闪避时切人、该切人时按连携。
  配置项 `dodge_key` / `switch_key` 可覆盖（改过键位的用户）。
  `dwell_sec=6` 秒到点按空格切下一位；切人动画 0.6s 内不出招。
- **状态驱动（2026-11-01 补齐，`runtime/zzz_agent_state.py`）**：按 OneDragon
  `agent_state_checker.check_length_by_foreground_color` 的算法实现「HSV 前景色过滤 →
  外接矩形宽度 / 条宽 × max_length」，H 环绕（如菲欧妮 H=4±9 → -5~13）拆两段掩码处理。
  参数全抄官方：菲欧妮-燔焰 hsv(4,128,255)±(9,128,55) 上限 120；维琳娜-风华
  hsv(0,255,255)±(90,220,200) 上限 135；蕾米埃尔-浮晖 hsv(143,140,174)±(7,37,81) 上限 100。
  取样坐标：1 号位来自 agent_state/*/config.yml 原值，2/3 号位按同位置其他角色推算
  （OneDragon 只给了菲欧妮 1 号位、维琳娜 1 号位 + 2 人队 2 号位），**首跑必须用日志核对数值**。
- **决策循环（`_decide`）**：站场角色资源条读数值，按 OneDragon 策略分支——
  菲欧妮 燔焰≥90 长按普攻 3.5s 放焚罪（7×0.5s，异放伤害）、<30 切走；
  维琳娜 风华≥85 长按普攻 3s（6×0.5s）、<30 切走；
  蕾米埃尔 浮晖=100 长按强化特殊技 2.5s（模板结尾自带切下一位）、<20 切走。
  长按实现照抄「按下 → 每 0.5s 重复按下 → 松开」；长按期间阻塞主循环不闪避
  （OneDragon 同样用「无视闪光」屏蔽，刻意为之）。
  **资源条读不到（None）自动退回固定 6 秒节奏轮转**，保证不会站着不动。
- **仍未做**：黄光「招架支援」的支援攻击键（简化成同闪避）；连携技触发的主动选择；
  蕾米埃尔「整套入场技」的 3 次切人细节。这些不影响能否打死 boss，影响的是上限。
- **实测阈值（2026-11-01 用户战斗截图 1919×1079）**：普攻 0.982/0.966；特殊技 0.865/0.876；
  大招**可用 0.820 / 灰 0.602**。OneDragon 用 0.9 是因为它自己截 PNG 无损，这里源图有压缩，
  所以拆成 `ATTACK_THRESHOLD=0.85 / SKILL_THRESHOLD=0.78 / ULTIMATE_THRESHOLD=0.72`。
  另：OneDragon 的「禁用 X」图标模板在当前 UI 完全不命中（得分 ≈0），只能靠亮/灰模板区分。
- **周门控**：`weekly_flow:ZZZ_NOTORIOUS_HUNT=YYYY-Www`，键由 `weekly_flow_week_key()` 生成，
  **整体后移 4 小时**（游戏周一 04:00 刷新）——周一 01:00 仍算上一周，否则会把真正的新一周跳过。
  实测：周日 23:00→W44、周一 01:00→W44、周一 05:00→W45。
- **接线**：`runtime/zzz_notorious_hunt.py`（导航+领奖）、`runtime/zzz_battle_assist.py`（战斗）、
  `foreground_flow_runner` 的 `zzz_notorious_hunt` 分支 + `_BUILTIN_ACTIONS`；
  `flow_configs[.example].json` 新增流程 `ZZZ_NOTORIOUS_HUNT`（3 步：点进入游戏 → 等主界面 →
  恶名狩猎）；`task_batch_runner` 新增 `_WEEKLY_FLOW_BY_APP`/`_WEEKLY_RUNNERS`/`_append_weekly_report`，
  在**两个出口**（正常跑完 / 今日已完成）都调用，与 `_append_monthly_report` 并列。
- **打几次**：流程自己读「本周核心材料掉落次数 N/3」，`min(每周3次, 3-N)`；读不到就按 3 次打；
  读到已满则一次都不打、直接成功返回（并让上层写本周标记，避免每天白启动客户端）。
- **定位策略**：比例坐标点击 + **每步 OCR 校验**（`P_TRAINING_TAB/P_HUNT_ENTRY/P_GO/P_CONFIRM/
  P_NEXT/P_START/P_REWARD_ENTRY/P_CLAIM_ALL`，来自 1080×613 截图）；点完看不到预期文字就整步失败，
  绝不盲点。任何一步失败都 `_leave_to_world()` 退回大世界，避免把游戏留在菜单里。
- **回归**：`tests/suite_zzz_notorious_hunt_20261101.py`（ALL PASS）：周门控三个边界、
  次数解析四种写法、真实截图两帧识别分离、模型缺失降级、3/3 跳过零点击、读不到次数打 3 次、
  进战斗失败判失败并退菜单、dispatch/内建动作/批次调度接线、配置与资源存在。
- **未实机跑过（务必知道）**：8 个导航比例坐标和「W 两秒是否够到 boss」都还没在真机上验证
  （2026-11-01 游戏已关）。首次实跑看 `[恶名狩猎]` 日志的命中坐标与 `[自动战斗]` 的
  「帧/普攻/闪避/特殊技/终结技」统计：帧数高但普攻为 0 说明按钮识别没命中；闪避一直不触发
  说明闪光模型没吃到画面。跑偏时优先补模板、再改 `*_frac`。
