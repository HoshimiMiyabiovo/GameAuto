# 每日进度口径、回填与复核

> 由 AGENTS.md 拆分；改动相关代码前先读本篇。

### 签到状态双写铁律 (重要)

- **启动前签到（`services/startup_sign_runner.py`）签完后必须同时写两处**：① SQLite tracker（`record_sign_result`）② `app_config.json` 的 `{section}.last_sign_date/last_sign_status/last_sign_message`（helper `_write_sign_status_to_config`，与 bridge `_update_sign_status` 同字段）。DailyView 的签到行对 wuwa/endfield/nte **只读 last_sign_***，只写 tracker 会导致 UI 显示"今日尚未签到"（2026-10-12 实例：异环实际签到成功 +80金币，last_sign_date 却停在 3 个月前）。
- **nte 分支过滤签到任务禁止用 `endswith("签到")`**：`_inject_external_log_progress` 里只应精确丢弃 `task_name == "游戏签到"`（游戏内由 ok-nte 代表）。`BBS签到`（社区 API 成果）和 `异环签到`（community 源）必须保留，否则成功的签到记录被丢后 UI 只能走"今日尚未签到"兜底。
- 米游社三件套（genshin/honkai_sr/zzz）走 tracker `sign_` 前缀任务，不经 last_sign_*，两条路径不要混。
## 每日进度「完成度」口径 (重要)

- **`progress_percent` 有两个完全不同的口径，禁止混用**：
  1. **脚本子任务比例**（`ok_ww_daily_parser` 等）：ok-ww 跑完 5 步就是 100%，
     与玩家真实游玩进度**无关**
  2. **真实游玩进度**：如鸣潮活跃度 `cur/total`（90/100 → 90%）
- `ui/bridge.py` 的 `UiBridge._real_completion_percent(game_key, tasks)` 是统一取值入口：
  按游戏找"关键进度"任务（鸣潮=今日活跃 / 原神=每日委托 / 绝区零=活跃度 / 暖暖=日活跃），
  用 `cur/total` 算百分比；`cur<=0` 视作未刷新返回 `None`；无关键任务返回 `None` 降级旧逻辑。
- 合并外部日志进度时**必须**用 `_real_completion_percent` 覆盖 `progress_percent`，
  并写 `progress_source`（真实任务名 或 `"script"`），供 UI 区分口径。
### 库街区（鸣潮）活跃度刷新限制

- **活跃度需进入游戏后才会刷新**，未进游戏时 API 返回 `cur=0`。
  直接把 0 写库并标 pending，UI 会显示"今日活跃 0/100"，实际玩家可能已 90/100。
- 写入 tracker 时必须判断 `cur<=0 且 total>0` → extra 加 `stale=True` + `stale_reason`，
  UI 显示"（未刷新）"而不是真实百分比（见 `DailyView.qml`）。
- 历史事故：2026-09-22~09-27 连续 6 天库里都是 0/100，用户投诉后才发现。
- **进过游戏也可能读到 0：服务器结算有延迟（2026-11-01 实跑事故）**。ok-ww 11:53:03 退出，
  11:53:26 `fetch_daily_data`（内部已先调 `refresh_daily_data` 刷缓存）仍返回 0/100，
  13:30 同一接口 100/100。**别把"已刷新缓存"当成"数据已结算"**：`clients/daily_checker.py`
  的 `check_wuwa_daily_with_retry` 在 `今日活跃=0` 时按 `WUWA_STALE_RETRY_DELAYS=(8s,20s)`
  等一会儿重查，最多 3 次；三次都是 0 才交给上层标 stale。重查抛错/无数据时保留上一次
  可用结果，不要把整条链路变成 errors。用户 2026-11-01 明确要求"统一刷新一下"。
- 用户原话纠正过一个错误归因：**「打开每日任务 → 领每日奖励」就是在涨活跃度**，
  所以"脚本只领奖所以活跃度是 0"不成立，遇到 0 先怀疑结算延迟，别先怪任务内容。
### 回答数值类问题必须实跑取证

- 涉及"完成度/进度/活跃度/体力"等数值结论，**先实跑 API 或查 SQLite**，
  禁止凭代码推断或沿用缓存数据下结论。用户在 2026-09-27 明确要求过"自动查一遍"。
### 发邮件前必须实时查进度（禁止发缓存数据）

- `ui/bridge.py::_send_daily_email_report` **不得**直接用 `tracker.load_today()` 发信。
  一条龙跑完往往已过几十分钟~几小时，库里可能还是跑之前的数据
  （2026-09-27 事故：库里活跃度 0/100，真实 100/100）。
- 必须走 `self._refresh_daily_snapshot_for_report(tracker_factory)`：
  子线程跑 `check_games_progress(tracker=tracker)` 实时查并写回库，
  `join(timeout=120)`；超时/异常降级到缓存但**绝不阻塞随后的关机流程**。
- 返回 `(snapshot, fresh)`，`fresh` 要传给 `build_daily_report(snapshot_fresh=...)`，
  邮件正文会据此标注"发信前实时查询"或"⚠ 缓存数据，可能不是最新"。
- 单测 `UiBridge` 纯逻辑方法时用 `UiBridge.__new__(UiBridge)` 绕过 QApplication 依赖。
### 每日进度 API 回填门控（2026-10-31）

- **背景**：今天鸣潮脚本实际成功（272s），但批处理按 `MIN_RUN_TIME=300` 降级为
  warning（落库 skipped）；终末地脚本被日志复核改判失败。两者都因为
  `_auto_query_daily_progress` 只在 `state == "success"` 时执行，没有回填
  API，每日进度里完全没有「今日活跃 / 日活跃」。
- **规则**：成功一律回填；`failed` / `warning` 时鸣潮、终末地、异环仍回填
  （官方活跃度与脚本执行是两回事）；`skipped`（维护跳过等）与用户停止不查。
  统一入口 `runtime/task_batch_runner.py::_should_auto_query_api_progress`。
- **顺序铁律（2026-10-31 补）**：对鸣潮/终末地/异环，必须**先查 API、再跑
  `_verify_script_log_result`**。旧顺序是日志复核先读库、API 回填在后，复核时
  库里还是空的，终末地「日活跃 100/100」被 ⭐送礼/⭐造装备 的日志失败改判死。
  预查询只跑一次（`_api_progress_queried`），复核后不重复请求；鸣潮复核也合并
  「今日活跃」，与终末地同口径。
- **鸣潮短时长**：`DEFAULT_APPS["WutheringWaves"].skip_min_run_warning=True`。
  `execute_task` 在 <MIN_RUN_TIME 时已用 ok-ww 日志 `Successfully Executed Task`
  复核，批处理不应再降级 warning，否则 272s 的真实成功会被记成 skipped，
  并连带跳过 API 回填。
- **当天回填**：只读 API 返回鸣潮今日活跃 100/100、终末地日活跃 100/100；
  已用 tracker 补写今日数据，两张卡片 `completion_state=completed`、100%。
  回归 `tests/suite_daily_api_query_gate_20261031.py`。
- **禁止在 `_run_single_task` 函数体内再 import `resolve_game_key`（2026-11-01 实跑事故）**：
  Python 会把函数内任意位置的 import 当成整个函数的局部变量，导致前面的
  `_resolved_game_key = resolve_game_key(app_key)` 直接 `UnboundLocalError`，
  整个批次在任务后处理阶段崩掉。模块顶部第 52 行已 import，函数里直接用；
  回归已加 `"_run_single_task 不局部遮蔽 resolve_game_key"` 静态断言。
  改完必须重启 GUI 再跑批次。
### 每日进度「完成」判定口径（历史统计用）

- **禁止用 `is_all_success` 判定某游戏当天是否完成**：鸣潮/暖暖的任务列表混有
  资源与周本（结晶波片 132/240、千道门扉 0/6000），这些本来就不要求满，
  用"全部任务完成"会导致完成率永远为 0。
- 统一用 `GameDailyRecord.completion_state()`，返回四态：
  `completed` / `unknown`（关键计数未刷新）/ `failed` / `pending`。
  判定优先级：关键任务 `real_progress_percent()` → `is_all_success` → `has_failed`。
- **统计完成率时，分母必须排除 `unknown` 天**（官方 API 未刷新，并非真的没做）。
  鸣潮踩过：修正前 0%，修正后 87%（完成 7 / 有效 8，未刷新 22 天不计入）。
- `KEY_PROGRESS_TASKS` 的关键任务名**必须与实际数据源返回一致**，
  否则未刷新数据会被误判成"真未完成"。已验证：
  `wuwa=今日活跃`、`nikki=朝夕心愿`（whimbox 日志口径；Pearpal API 侧叫"每日心愿"
  满值 500，常年 0/500 不刷新——**Pearpal 落库已于 2026-10-24 停用**，5 项假进度
  任务（每日心愿/周常奖励/能量体力/派遣/登录天数）删除，名字仅为兼容历史记录）、
  `genshin=每日委托`、`zzz=活跃度`、`endfield=日活跃`、
  `nte=活跃度`（塔吉多 roleHome 的 dayvalue，2026-10-24 接入，上限 100）。
  脚本型游戏无计数，用 `KEY_STATUS_TASKS` 按状态判定：`nte=日常领取`、
  `nikki=朝夕心愿`（success=完成，failed=失败，优先级高于计数口径）。
### 每日进度 UI 呈现口径（2026-10-19 用户拍板，快赢档）

- **卡片结论（徽标/底色/汇总条）只认 `completion_state`**：活跃度满→"全部完成"
  绿底；未满→"未完成"米白底；关键指标失败→"有失败"红底；计数未刷新→"未刷新"。
- **禁止在 `pending` 分支回退 `is_all_success/has_failed`**：白荆活跃度 15/100 +
  俱乐部每日任务失败，旧回退逻辑把卡片染成红底"有失败"——细枝末节失败只留在
  任务明细，不拖卡片结论。绝区零"活跃度已满"+5 个边角失败同理：必须"全部完成"，
  汇总条计入完成而非失败（旧口径把这种计进"失败"不计"完成"）。
- `DailyView.qml` 主列表徽标/底色/汇总条（`buildStatusText`）与歷史回看段
  四态映射必须一致；`progress_source`（`ui/bridge.py::_attach_completion_state` 补）
  区分百分比口径：真实计数（活跃度）= 游玩进度直接显示，脚本子任务比例加
  "脚本"前缀——白荆活跃度 15/100 曾被误显示"脚本 15%"。
- 已完成卡片的头部右侧只显示完成度，不翻边角失败的旧账（"失败 N 项"仅出现在
  失败结论卡片；未完成卡片以"完成度 · 失败 N 项"后缀形式带出）。
### 事后日志复核口径 (重要)

- `task_batch_runner._verify_script_log_result` 只做 success→failed 改判，
  读取的是各脚本的**当日报告文件**，与森空岛/官方 API 数据无关。
- **终末地复核必须合并查询接口的日活跃**（`_load_api_key_progress_tasks`，
  用户拍板口径 2026-10-05）：API 日活跃 100/100 时即使 ⭐送礼 等边角任务失败
  也不改判；API 数据缺失时退回纯脚本口径（含"ok-ef 未正常结束"兜底）。
- **无限暖暖复核以朝夕心愿为关键判定**（`KEY_STATUS_TASKS["nikki"]`，用户拍板
  2026-10-06）：whimbox 解析器恒定输出 8 项任务，朝夕心愿 success 即整体完成、
  failed 才改判；美鸭梨挖掘/星海拾光等边角任务失败不触发整轮重试。
- **原神判定以 BetterGI 日志为准，双向覆盖**（`_apply_bettergi_verdict`，
  用户拍板 2026-10-24）：监控跟踪「委托奖励 + 探索派遣」两项标记
  （`bettergi_log_monitor._process_line`）。未确认全部完成 → 失败（只降不升）；
  **日志已确认全部完成 → 覆盖超时/退出异常一票否决**（超时只是收尾被杀），
  判成功且天然不触发统一重试。用户主动停止不覆盖；监控不可用保持 fail-closed。
  米游社便笺（每日委托计数）不再作为原神判定依据——cookie 过期时它整月无数据，
  曾把判定退化成"BetterGI 脚本行说了算"（原神 68% 完成率的根因）。
  回归 `artifacts/test_genshin_verdict_20261024.py`（14 项）。
- **2026-10-26 复核：原神失败 = BetterGI 自己跑坏 + 委托本来就没人做，两件事同时成立**
  - BetterGI 一条龙配置（`User/OneDragon/默认配置.json`）实际启用的 6 个任务是
    **领取邮件 / 每日地脉花 / 领取每日奖励 / 领取尘歌壶奖励 / 狗粮 / 至冬一条龙**，
    里面**没有"做每日委托"的任务**，只有"领取"。所以委托必须在那之前被别人做完，
    `▶ "领取『每日委托』奖励"` 才会出现；10-24 09:50、10-25 10:08 都成功领取，
    10-26 09:10 两次都是 `未完成或者已领取` → 当时确实没有可领的委托。
  - 同时今天 BetterGI 自己坏了：真实日志里 **43 条 `[ERR]`**，样本
    `assets/icon/add_button.jpg匹配异常`、`我辣么大一个地脉花哪去了？`、
    `地脉花处理失败 (重试3/3): 开启地脉花失败，已重试3次，终止处理`、
    `未能返回主界面`、`打开大地图失败，请检查按键绑定「打开地图」`、`传送失败`、
    `找不到出战角色编号块`；狗粮 09:11→09:21 十分钟就异常收尾（平时约 45 分钟），
    09:38 游戏退出。
  - **判定口径不变**（不放宽）：`委托奖励 + 探索派遣` 未被日志确认 → 原神判失败。
    但 `failure_message()` 现在会把两件事分开写清楚，并把 BetterGI 自身报错带进文案：
    `BetterGI 日志未确认今日委托奖励完成 —— 委托奖励：BetterGI 判定「未完成」并跳过领取
    （一条龙 6 个任务里只有「领取每日奖励」，没有做每日委托的任务）。本次 BetterGI
    自身报错 43 条（示例：…）`。监控新增 `error_count` / `error_samples`（最多 3 条，
    取 `[ERR]` 行的下一行正文；下一行还是日志头就不采），日报 `format_bettergi_daily_reward_summary`
    也会多一行报错。回归 `test_genshin_verdict_20261024.py` 用例 [6][7]（[7] 直接回放
    今天的真实日志，锁住"报错被抓到 + 委托没被误判成完成"）。
  - 排查入口：BetterGI 日志目录 `D:\Program Files\genshin ai\BetterGI\BetterGI\log\better-genshin-impact<日期>.log`，
    报错截图 `avatar_side_classify_error.png`、`User\OneDragon\*.json` 看一条龙任务清单。
- **崩铁（March7th）活跃度数值已接入判定**（用户拍板 2026-10-24）：
  解析器取日志 INFO 行「当前累计分数：X/500」（一天只涨不跌，取最后一次；
  无分数行但「每日实训已完成」→ 500/500 兜底）产出「活跃度」计数任务
  （extra current/total），`KEY_PROGRESS_TASKS['honkai_sr']` 早已注册，
  复核自动走真实进度口径：达标 → completed 不改判。超时侧由
  `_apply_starrail_score_verdict` 兜底（仅 script=March7th 启用）：超时但分数
  已达标 → 改判成功、不触发统一重试；未达标/无分数行 → 原判。
  注意既有行为：无计数时「每日实训」布尔任务名命中 KEY_PROGRESS_TASKS →
  completion_state=unknown → 复核从不改判（改判失败侧全靠超时兜底）。
  回归 `artifacts/test_starrail_activity_20261024.py`（22 项）。
- **绝区零（OneDragon）超时兜底 + KEY_STATUS 口径**（用户拍板 2026-10-24）：
  OneDragon 日志只有枚举状态（活跃度已满/未满/领取成功），**没有数值**，
  无法像崩铁那样算百分比。超时侧由 `_apply_zzz_activity_verdict` 兜底：
  超时 + 「活跃度已满」→ 改判成功不重试；仅认"已满"（"日常奖励领取成功"
  可能是中途领箱，不算）；未满/无数据 → 原判。`KEY_STATUS_TASKS['zzz']=
  ("活跃度奖励",)` 让复核不被体力刷本等边角失败拖垮（已满 → completed）。
  回归 `artifacts/test_zzz_activity_verdict_20261024.py`（14 项）。
- **异环（塔吉多）dayvalue 接入 + 社区任务/暖暖假进度删除**（用户拍板 2026-10-24）：
  塔吉多 H5 游戏名片接口 `GET /apihub/awapi/yh/roleHome?roleId=`（从
  yh-game-record 前端 bundle 挖掘并实测）返回 `dayvalue`（每日活跃度，上限 100）、
  本性像素、都市活力。`TajiduoClient.fetch_role_home()` 封装，`check_tajiduo_daily`
  产出「活跃度 X/100」+「本性像素/都市活力」（计量不参与完成判定）+ 游戏签到。
  字段映射以 H5 名片前端为准：`staminaValue/staminaMaxValue` = 本性像素，
  `citystaminaValue/citystaminaMaxValue` = 都市活力；**不要叫体力/城市体力**。
  复核：nte 分支合并 `_load_api_key_progress_tasks`，**dayvalue 满 → 直接
  return None**（ok-nte 边角失败不再改判烧重试）；不满/无数据 → 旧口径。
  同批删除：①社区任务（点赞/浏览/分享，恒 pending 无信息量）停止查询与落库，
  getUserTasks 仍供签到流程内部使用；②无限暖暖 Pearpal 5 项假进度停止查询
  与落库（check_nikki_daily/NIKKI_EXPECTED_TASKS/_record_nikki_to_tracker 整体删除），
  暖暖进度只来自 whimbox 日志。注意：`_load_api_key_progress_tasks` 只取
  source != "script" 的记录，塔吉多侧落库 source 必须 community。
  回归 `artifacts/test_nte_dayvalue_20261024.py`（25 项）。
- **ok-ef 一天可产出多份报告**（异常重跑、执行中快照），`find_today_ok_ef_report()`
  必须取时间戳最新一份；报告里空列表的占位符「无」不是任务名，解析时必须滤掉
  （2026-10-05 事故：读到 15:00 异常快照 +「无」被当任务名 → "无: 未知原因" 改判失败并烧掉一次重试）。
### 历史数据

- `daily_progress` 表自 2026-05-19 起有 132 天 / 2556 条记录。
- `clear_old_records(keep_days=7)` **当前无任何调用点**（所以历史才保留至今）。
  若将来接上自动清理，历史回看功能会只剩 7 天数据 —— 接入前务必确认。
### 脚本日志解析结果必须落库 (重要)

- `services/whimbox_daily_parser` 等日志解析器产出任务时**必须带 `extra={current,total}`**：
  bridge 的注入合并与 `tracker.record_task` 都从 `extra` 取计数，缺 extra 会让
  「朝夕心愿 500/500」这类真实进度被当成 0（2026-10-07 暖暖"无有效数据"根因之一）。
- **内存消费 ≠ 数据落地**：`bridge._inject_external_log_progress` 只改当次页面显示，
  `task_batch_runner._recheck` 只用于失败复核——两者都不写 tracker。凡是"完成判定关键任务"
  （见 `KEY_STATUS_TASKS` / `KEY_PROGRESS_TASKS`）的解析结果，都必须有落库路径
  （如 `daily_checker._record_whimbox_script_tasks`），否则统计/历史/邮件永远拿不到。
- Pearpal notebook 的「每日心愿」常年返回 0/500（接口不刷新），
  暖暖真实进度**只存在于 whimbox 日志**，判定必须以 whimbox 侧为准。
- `KEY_PROGRESS_TASKS` 的任务名必须与解析器产出的**精确字符串一致**（大小写、全角）：
  zzz 实测是「活跃度奖励」不是「活跃度」，nikki 是「朝夕心愿」（whimbox 侧）。
- **ok-ww 子任务开始行 ≠ 成功（2026-11-02 实跑事故）**：`ok_ww_daily_parser` 旧逻辑
  在 `DailyTask:<task>` 开始行就记 success，导致 80 秒退出的脚本仍算出 100%；
  现在只在 `DailyTask:Task completed` / already 时记 success，开始行记 pending。
  `script_ok-ww=failed` 时 bridge 用 `progress_source=script_failed` 覆盖上一次 run 的
  100%，旧子任务标「上次运行」，`今日活跃` 的 stale_reason 写「脚本未完成，活跃度未采集」，
  `completion_state` 记 failed（不能因为活跃度 0/100 显示「未刷新」而掩盖失败）。
  **查询成功但返回 0 不是「未刷新」**，只有查询失败/接口频繁才是。
  回归 `tests/suite_daily_progress_script_failed_20261102.py`。
### 关键判定任务分两类口径 (重要)

- **计数型** → `KEY_PROGRESS_TASKS`：API 返回 X/Y（wuwa 今日活跃、nikki 朝夕心愿、
  endfield 日活跃、genshin 每日委托）。拿不到计数（cur=0/缺 extra）→ `unknown`，
  统计时**排除出分母**（真的可能是官方未刷新）。
- **纯状态型** → `KEY_STATUS_TASKS`：日志/接口只给"已满/未满/成功/失败"、
  **天然没有 X/Y 计数**（zzz 活跃度奖励、nte 日常领取、nikki 朝夕心愿 success 分支）。
  按 status 判定，**绝不能走计数口径**——否则"活跃度已满"会被误判成"数据未刷新"
  （2026-10-08 实例）。
- `completion_state()` 里 KEY_STATUS_TASKS 优先级最高，其次 real_progress_percent()。
- **判定关键任务所在的脚本日志必须落库**（`daily_checker._record_*_script_tasks`）：
  只在 bridge 内存注入/失败复核里用完就丢的话，统计与历史回看永远拿不到。
