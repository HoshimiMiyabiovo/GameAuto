# Changelog

> 约定：每次提交在对应日期下**只追加一行**，写清"做了什么 / 为什么"。
> 实现细节、踩坑与口径放 `docs/conventions/`，这里不展开。
>
> 历史技术说明（原 `overview.md`）已归档到
> [notes/overview-2026-11.md](notes/overview-2026-11.md)。

## 2026-11-05

- 无限暖暖「今天商店又没跑」定位并修复：08:32 `NikkiLauncher` 步骤2 打不开奇想日历（点右上角图标 + 瞬时 `pyautogui.press('l')` 三次全失败），`_run_flow_inner` 却返回 True「已记录，继续后续任务」→ 前置流程报成功，实际晶石兑换/共鸣商店整段没执行，`task_meta` 里本月两个 nikki 商店标记一条都没有；11-04「按 H 后未进入商城」同因。现 `foreground_flow_runner.press_key_scancode` 统一发带扫描码的真实按键（L/H/ESC，与绝区零同口径），日历打不开时保留不阻断 whimbox 的容错但 `logger.error` 明确写「本月晶石兑换/共鸣商店未执行」；新增 `tests/suite_nikki_shop_scancode_20261105.py`。
- 异环 11:05/11:15/11:26 更新补跑连撞 3 次 600s 超时定位并修复：现场栈显示任务线程停在 `automation_controller._wait_for_game_process_snapshot` 的 `time.sleep(2)` 正常轮询等 HTGame.exe（11:15/11:26 已识别「游戏正在更新，按 120 分钟预算等」），但该循环从不 `self._heartbeat.beat()`，被全局任务看门狗（`_global.task_heartbeat_timeout_sec` 未配，默认 600s）误判死锁强杀；异环 `timeouts.launch=600` / `update=7200` 都不短于 600s 注定撞上，补跑队列 `max_attempts=3` 每次到点重排 → 各白烧 600s。现在等待循环每轮上报心跳，与 `_monitor_game` 主循环同口径；新增 `tests/suite_nte_launch_heartbeat_20261105.py`。
- 异环迷迭兑换「只买 1 个骰子 / 0 把钥匙却判成功」定位并修复：采购前 OCR 是「本月库存 5/5、20/20」（根本没买），买完弹的「获得物品 / 点击空白区域关闭」浮层**不会自动散**，而 `_buy_once` 只 sleep 2.5s（原神/崩铁浮层会自动散所以以前够用），后续点击全落在浮层上 → 1/5 + 0/20 被 `ALREADY_BOUGHT_REASONS` 的「已达上限或无弹窗」当成合法已购 → `all_ok=True` → 写月度标记、本月不再补买；现在 `_dismiss_reward_popup` 主动检测浮层并点右下空白关闭（最多 3 次、关不掉返回新失败码 `REWARD_POPUP_STUCK`，两个购买循环都判失败），`NTE_MONTHLY.close_process_on_finish` 改 true 且 `run_flow` 对复用窗口保留游戏不误杀；已备份并清除误写的 `monthly_flow:NTE_MONTHLY=2026-11` 让下批补买；新增 `tests/suite_nte_reward_popup_20261105.py`。
- 绝区零月商店「未找到主菜单商城入口」实机定位并修复：提权单步实测确认 `pyautogui.press`/`keyDown` 的 ESC、F3 对游戏内 UI 完全无效，只有带硬件扫描码的 `win32api.keybd_event(vk, MapVirtualKey(vk))` 按住 0.15s 才被 Unity 接收（`_press_escape` 统一改掉，崩铁/异环退出链路同受益）；主菜单商城是底部「24 HOUR」纯图标，OCR 认成 24/HOUR、中文标签常漏，`_find_shop_entry` 加 `shop_entry_point_frac` 固定比例兜底且只在按过 ESC 之后启用，ZZZ_MONTHLY 步骤3 配置实机命中点 (1376,970)@1920x1080；实机已由脚本自己走通「主世界 → 扫描码 ESC×2 → 主菜单 → 商城 → 信源清单 → 信号残响」，新增 `tests/suite_zzz_shop_entry_20261105.py`。
- 白荆回廊连续失败定位并修两处：①11-05 启动器维护后弹「检测到游戏有新版本，请确认是否更新？」，旧流程只找「开始游戏」从不点「确定」，300s 等不到 `GateMK-Win64-Shipping.exe`（08:53/09:36 两轮）；现在点开始前/等待进程时都会识别更新弹窗并点「确定」，确认更新后等待上限放宽到 30 分钟（可停止），启动器重建窗口会按进程名重新接管，失败诊断写明「仍显示更新提示」。②11-01/11-03/11-04 午后茶憩卡在角色详情页：说明文字「每名同调者一天仅能邀请一次，重复邀请请使用通用邀请函」含「邀请」被当成按钮点，页面原地不动，后续盲点对话框退回休息室；现在只认 ≤12 字的短按钮名并排除说明文字，找不到入口就落证据返回，不再误点正文。新增 `tests/suite_baijing_update_prompt_20261105.py`，扩充 `suite_baijing_tea_invite_20261103.py`。

## 2026-11-04

- ok-ww 接入模块化调度（阶段 1）：新增 `OkwwModuleSession` + `tools/okww_module/runner.py`，用 ok-ww bundled python 起常驻 headless 进程，通过本机 JSON 协议调度 `DailyTask`，不再拉起 `ok-ww.exe`；运行时固定 v3.7.17（`data/okww/src`），`configs` 用 junction 指向现有安装保留本地队伍；`module_mode.enabled` 默认关闭，旧链路可回滚；新增战斗输入节流 `humanize.py`（5 CPS、±25% 抖动、只作用于战斗中心左键）和 `tests/suite_okww_module_20261104.py`。
- 月商店"看不到≠已买过"严格化 + 失败可观测：翻页查找开着但一屏都没滚成功（滚轮对列表无效）时商品"看不到"改判失败、不写月度标记（11-03 崩铁 0/5 被当已买完的静默漏买通道），翻遍了仍没有才维持"已买过"口径；「剩余数量」判据加邻卡守卫（崩铁商品到上限整卡消失，点中邻卡的「可兑换: 80/80」不再误判未买满）；dispatch 类步骤失败统一打日志+落 flow_failures（"用户停止"除外），自塑尘脂失败带坐标与等待时长；月商店每步结果同步进 runner 日志（以前只在邮件日报）；GENSHIN/ZZZ/NTE 兑换步显式 `item_scroll_search: false`（单屏页维持旧口径），HSR 保持翻页吃严格语义；已误写的 `monthly_flow:GENSHIN_MONTHLY=2026-11` 标记已备份清除，本月原神会自动补买。

- 所有前台点击统一改为极短按住式：`_pyautogui_click_guarded` 默认按住 0.06s（`GAMEAUTO_CLICK_HOLD_SEC` 可调/关，`hold_sec=0` 显式瞬时），`run_flow`/`_click_window_local` 只透传；原先 11 处裸 `pyautogui.click`（automation_controller / ocr_click_tester / click_sequence / zzz_battle_assist / maintenance 两个 runner）全部收口，战斗连点用 0.02s、后台 PostMessage 天然带 20ms 间隔；新增全局默认与无裸点击回归断言。
- 绝区零「点击进入游戏」点不动实锤并修复：用户当场实机验证，普通权限被 UIPI 静默拦截、提权后瞬时 `pyautogui.click` 仍无效，只有前台 `mouseDown` 按住 80ms 再松开才出现「登录成功」；新增 `_pyautogui_click_held` 与步骤级 `click_hold_sec`（默认 0 不影响其它流程），ZZZ_MONTHLY / ZZZ_NOTORIOUS_HUNT 第 1 步改前台按住并停用后台首发与 click_fallback，新增 `tests/suite_zzz_click_hold_20261104.py` 并把 `suite_zzz_monthly_main_screen_20261021.py` 里锁死「后台首发 + 兜底」的旧断言同步为实机口径；GUI 仍必须用管理员启动。
- 崩铁每月事项整体交接给三月七：打开其 `asset_ember_tracks_of_destiny_enable`（timestamp 保持 0，下次运行补买 2 个），本项目 `HSR_MONTHLY` 停用且调度器对 disabled 流程安静跳过（否则每批刷 ❌ 并反复补跑），其它游戏月度流程不受影响；新增 `tests/suite_hsr_monthly_handoff_20261104.py`，并把 `suite_monthly_auto_schedule_20261101.py` 的流程配置查询与本机业务配置解耦（避免交接后旧调度断言因 enabled=false 误红）。
- 更正翻页归因并照抄三月七实机做法：`module/automation/local_input.py::mouse_scroll` 是一次一格带 pause 的循环（旧 `scroll(-5)` 大 delta 被 Unity 吞掉），`tasks/daily/ember_exchange.py::_scroll_to_bottom_for_retry` 是移到列表中心后 `mouse_scroll(120,-1,False)` 一次滚到底再补扫；本项目改逐格 12 格/屏、新增「一次到底补扫」前置、文字签名去掉 y 分桶改 12% Jaccard 抖动容忍，并确认「命运的足迹」是 `asset_ember_tracks_of_destiny_enable` 开关关着（源码有实现、上限 2 个），不是没实现；20:17 实跑暴露的 `effective=8` 假滚动已堵住。
- 取证三月七小助手：`tasks/daily/synthesis.py` 已有自塑尘脂与余烬兑换的完整实现，本机 11-01 全部成功、本月已刷新，`HSR_MONTHLY` 两步属重复实现；参考路径/实测坐标/可复用模板与对 11-04「翻页找不到」事故的重新归因记入 07-operations.md。
- 白荆活跃度实图回归在 `2026-10-27.jpg` 证据图已被本机清理时跳过实图断言（有图时断言不变），不再让全套 pytest 因缺文件挂掉。
- 原神更新日协议弹窗改为脚本自动点「同意」（短文本 OCR + 按钮模板 + 固定比例三级兜底，堵住正文「将无法进入游戏 / 同意」被子串误点、连等 600s 主界面超时）；月商店翻页改以 OCR 文字签名判列表是否真滚动、滚轮无效自动拖右缘滚动条（背景动画的像素差不再把漏买骗成「已买过」）；自塑尘脂失败前先存背包页截图并加网格扫描逐格定位（三个按缩略图推算的坐标真机未命中，合成按钮坐标经 1920 参考图核对吻合不动）；新增 `tests/suite_login_agreement_scroll_20261104.py`。
- 补齐四类「跑完没结论」的洞：confirm 型确认失败重试耗尽改为落截图 + 当帧 OCR + flow_failures（绝区零三次「元素 10s 未消失」此前全无图）；`run_flow` 加窗口连续不可用止损（NTE_MONTHLY 45s，只判最小化/句柄失效、不判被遮挡，异环启动器 12 分钟空转不再没落失败，真更新等待仍留 1200s）；米游社便笺遇 retcode 10001 时用 stoken 刷新 cookie_token 重试一次，并修掉 `stuid_v2` 误报告警（签到刷新结果没回写配置导致 20:15 崩铁每日数据取不到）；`AutomationController._init_logger` 加 `GAMEAUTO_LOG_FILE_DISABLED` 开关、由 `tests/test_suites.py` 统一开启，FakeSession 等测试日志不再混进生产 `auto_runner.log`（生产路径不变）；新增 `tests/suite_failure_evidence_cookie_20261104.py`。
- ok-ww 上游骤变取证与不可再生资产固化：v3.7.16/17 同日两版砍掉「角色代码」「多账号」tab 并删下载/QQ/赞助链接，GitHub Releases 只剩这两版，故把 D 盘 v3.7.13 全量安装（含 428MB 安装包与已装依赖）打成 `D:\backup\ok-ww\ok-ww_v3.7.13_full_20261104.zip`（15347/15347、0 失败、关键文件 sha256 回环一致、`H2_R2_GAP=0.5` 补丁在包内）；工坊老域名 `cfokwwchar.ok-script.com` 经权威 SOA 确认是上游删记录（NXDOMAIN，非本地网络），数据本体已迁 `okwwcharcode.ok-script.com` = 公开仓库 `ok-oldking/ok-ww-char-code`（49 队/69 轴），本机已存整仓镜像并核清「重导只丢行尾、补丁可用 `Hsin_h2_r2_gap_0.5.patch` 机械恢复」；提权 `icacls /reset` 修掉 `Aemeath__Chisa__Denia` 的受限 ACL（原始 ACL 已存档），这条坑以后不再静默漏备份；系统 `tar.exe` 打归档会出现「退出码 0 但只写入 1693/15337」的假成功，归档一律改逐文件写入 + 独立校验；v3.7.13 源码经两个 update remote 的 tag 仍可取，唯独安装包不可再生。用户确认零氪号封号属实（上游 #1824），本机号暂安全，主程序维持 v3.7.13 不升级。
- 备份补齐 + 关掉外壳自动更新：把工坊整仓镜像以 `workshop_char_code_20261104/` 并入上面那个 zip（15347→15509 条、162 文件全进、含 69 个轴包，`teams.json` 回环 sha 一致，包体 2,131,861,071 字节）；ok-ww 外壳的 `app.json` 里 `update_method` 由 `AUTO_UPDATE` 改 `MANUAL_UPDATE`（`pyappify/app_config.py:80` 缺省即 AUTO，必须显式写死），改前双备份、18 个键只动这 1 个，`auto_start` 未动；随后把归档里那条 `app.json` 也改成 `MANUAL_UPDATE`（`data/artifacts/_okww_zip_set_manual_update_20261104.py`，字符串级替换、其余条目逐条搬运，15509→15509、文件名集合一致、`testzip()` 全档 CRC 通过、`ok-ww.exe`/`Hsin.py`/`teams.json` 抽查 sha256 与磁盘一致），换机后不必再手动关自动更新；最终包 2,131,861,074 字节，sha256 `3f9fdec2e755f555b86e019ca854cab5390d99ffbbd8dd01541abaff758b995e`。
- ok-ww v3.7.13 主程序角色代码工坊复活 + 自动更新关闭核验：实测上游最新仍是 v3.7.17（今日 14:54 发布，Releases 只剩 16/17 两版），老工坊域名 `cfokwwchar` 确认 NXDOMAIN 且 v3.7.13 的 `fetch_all_workshop_teams`/`fetch_workshop_codes` 无回退逻辑，故把 `CharacterCodeTab.py` 三个工坊 URL 常量改指新域名 `okwwcharcode.ok-script.com`（repo+working 两份同改、改前 `.bak_20261104` 备份；`/teams.json`、单队 JSON、`/codes/*.zip` 路径与 schema 实测全兼容，py_compile + 按应用同款逻辑端到端连通 49 队/单队/zip 下载全通）；核验 `update_method=MANUAL_UPDATE` 无静默重开路径（应用侧无 `update_app_config` 调用、壳每次从盘读 app.json，仅手改/UI 切换/重装三种显式动作会回 AUTO），口径与「重装冲补丁要重打」记入 08-game-config.md。

## 2026-11-03

- 新增脚本管理页与自动下载安装：侧边栏「脚本管理」逐工具卡片展示版本状态/安装路径/进度条，ok 系（5MB 在线包）/March7th/BetterGI 支持一键下载安装与换血式更新（zip-slip 防护、ok 系 sha256 对账、运行中与托管目录外拒绝、磁盘余量预检、进度可取消），配置向导页版本卡片对未安装的可下载工具有"一键安装"入口（首启可选自动下载），安装根目录默认 data/script_tools 且全局/每工具可自定义（app_config 新增 script_tools 段）；新增依赖 py7zr（BetterGI/March7th 只有 7z 包）。
- 绝区零进入游戏点不上/傻等 40 分钟修复：ZZZ_MONTHLY/ZZZ_NOTORIOUS_HUNT 的「点击进入游戏」改为后台 PostMessage 首发、失败后切前台点击（`click_fallback`），点击后用文字消失做校验，单轮 30s×2；「等待进入主界面」由 600s×3 降到 100s×（1 次回退），进入阶段最坏约 5 分钟即失败。同时点击前抬起 Alt/Ctrl/Shift/Win，模板定位失败日志改为每轮一条并带模板名。
- 暖暖奇想日历打开方式修正：11-02 的「ESC 收起再按 L」在 11-02/11-03 实盘全失败（主世界 ESC 会打开暂停菜单、菜单盖着时 L 全部无效）；改为优先点右上角「奇想日历」图标（OCR 定位标签或固定比例 (0.802,0.061)），点不到再退回按 L，按键前先抬起修饰键。
- 白荆关卡战斗「体力/行动力不足」修复：11-03 定向物资保障点「快速战斗」后没弹设置窗，而是弹了带「确认使用」的行动力补充框（限时药，用户确认可直接用）；旧逻辑按比例盲点后把当前页当结算页点掉并返回 True，导致该任务 0/1（-5）且「完成5次战斗」停在 4/5（-20），活跃度 80 而非 105。现在等不到设置窗先点「确认使用」再重试快速战斗，仍失败则走统一失败取证（flow_failures 表 + 截图）并返回 False（不再盲点、不再谎报完成，后续关卡自动用指定次数=2 补场次）。
- 月商店「只买 1 个就停」修复：11-03 原神星尘兑换的纠缠之缘/相遇之缘各只买到 1 个（采购前后截图「本月剩余数量 5→4」），崩铁命运的足迹停在 1/2，日志却记「1/5（已达上限或无弹窗）」当成功。根因是兑换后的「获得物品」浮层还挂着，下一轮 OCR 直接命中浮层上的商品名（原神日志：纠缠之缘 @ (960,607)，正是浮层位置），点过去不是商品卡、等不到确认弹窗，被误判成已达上限。修复：`_buy_once` 成功后多等 2.5s 让浮层散掉；点商品等不到确认弹窗时等 2s 重新定位再点一次；仍无弹窗时读卡片上的「本月剩余数量:N」/「可兑换: X/Y」，**剩余 > 0 判「未买满」记失败**（用户口径：还写着 4/5 就是未完成），只有剩余为 0 或读不到才按已达上限处理。
- 月商店采购截图叠加 OCR 标注：采购后除原图外另存 `*_采购后_ocr.jpg`（复用失败取证的「框 + 编号 + 图例」渲染，`flow_diagnostics.annotate_ocr_overlay` 对外暴露），`index.jsonl` 增 `ocr_file` 字段。买完直接看图就能核对「本月剩余数量:4」这类文字，不用再翻 OCR 日志。
- P0 月商店翻页查找实测无效修复：11-02 用 32x18 缩略图 MD5 判断"滚没滚"，被商品卡角标闪烁/光效骗过——签名每帧都变，「列表内容没变化」的熔断一次没触发，每件商品 8 轮 × 25s OCR 全跑满（3 件烧 12 分钟），而滚轮对崩铁商城网格是否有效从未被验证。改为灰度缩略图像素平均差（真实滚动 >10，动画噪声 <1）+ 连续两次低于阈值熔断；翻页时每屏 OCR 从 25s 缩到 6s；翻页失败落 `flow_failures`（截图 + 当帧文字），不再静默按"已购买处理"。
- P0 步骤失败零日志修复：崩铁自塑尘脂（`hsr_resin_craft`）首跑失败时，craft 返回、dispatch 分支（`foreground_flow_runner` 的 `if not dispatch_ok: return`）、run_flow 三层都不打日志，`flow_failures` 也无记录，失败只剩日报一行 ❌，无法定位点歪在哪。现在 dispatch 失败打 `logger.error` 并按统一取证写 `flow_failures`（截图 + OCR 标注图，`error` 参数记录 action 返回原因）；`hsr_resin_craft.fail()` 与「点格子后详情未出现」「分类标题未确认」都在日志里带坐标和等待时长。
- 白荆关键点击改「OCR 定位优先」：关卡页「快速战斗」先点 OCR 命中的按钮中心（限右下区域），认不出才退回固定比例 `(0.784,0.893)`；茶憩点角色卡后补一次「邀请/开始茶憩/一同品茶/邀约」入口点击，两样都没有就不再盲点底部对话框（11-03 就是那一下把角色详情页退回休息室的），改为落 flow_failures 并跳过。根因：以前只有判页面用 OCR，真正点击用写死比例，页面布局一漂就点到邻居按钮（物资保障弹出了「进入关卡/行动力」确认框）。
- 失败证据支持 OCR 标注图：`record_failure` 抓完截图后把当帧 OCR 框画在副本上，框内标编号、图片下方扩展区排图例（序号 + 文字 + 置信度），另存 `*_ocr.jpg`；`image_path` 指向标注图、原图记在 `extra.raw_image`。框内只标号不贴文字，避免和游戏原生文字糊在一起（首版把标签贴在框上，密集界面不可读）；一次 OCR 同时供文本样本和标注，不增加识别开销。
- 新增第三方脚本版本检测与更新提示：`GameProfile.script_tools` 填各脚本的 GitHub 仓库与本地版本源策略，配置向导页出"脚本版本"卡片对照 releases/latest 检查落后（24h 缓存、失败回退、启动静默检查 + 同版本只提醒一次）。首版拿目录名正则判本地版本被用户质疑后实盘查证：原地自更新不改目录名，4 个"有更新"3 个误报（March7th/ok 系早已自更新到最新），已按逐工具真值源重构（pyappify app.json / version.txt / exe 元数据），误报清零；顺手修正 ClickSequenceView 误用不存在的 HusTag.State_Info。

## 2026-11-02

- 提交既有改动：绝区零恶名狩猎/自动战斗、崩铁自塑尘脂、月商店滚动查找、每日进度脚本失败态。
- 新增插件式 `games/` 游戏档案注册表，收敛 nte 等按游戏分支与三处重复更新窗口。
- `ui/bridge.py` 拆为 `ui/bridge_views/` 按视图 mixin（bridge 745 行，各文件均 <1000 行）。
- 文档：AGENTS.md 拆为 `docs/conventions/` 索引 + 8 篇；原 overview 技术说明归档，本文件改作 changelog。
- 修复拆分后 mixin 内相对导入层级（`.bridge_backends` → `..bridge_backends`），并清理 bridge 无用导入。
- 根目录整理：`模板/` → `assets/templates/`、`click_sequences/` → `assets/click_sequences/`、`start/`+`launchers/` → `scripts/`；模板路径与本地 flow/app 配置一次性迁移。
- 视图回收套件改为轮询等待释放（离屏首次编译 1-2s 时固定 0.75s 断言会误报）。
- `%TEMP%` 参考副本移到 `data/reference/temp_project/`；`webui/` 并入 `ui/webui/`，导入与文档路径同步。
- 改为 src 布局：Python 包移入 `src/game_auto/`，启动脚本把 `src` 注入 PYTHONPATH，pytest 配 `pythonpath=src`，包内 `__file__` 路径常量上移一层。
- 修复 11-04 批次中断：`wool_dismiss_update` 把 `dismiss_minimax_update_popup` 的 bool 返回值按 `(bool, str)` 解包，TypeError 让整批在薅羊毛处退出（异环等后续任务没跑）。
- 崩铁日志监控新增 `ignore_keywords`：March7th 的 "Failed to set window foreground / 激活窗口时发生错误" 不再被当致命错误提前杀任务（11-04 07:45 崩铁 35 秒误杀）。
- 修复"停止后仍自动跑/关不掉"：用户点停止会暂停定时任务与更新补跑并清空排队（直到手动点开始）；`execute_task` 不再无条件清掉停止标志，启动前/拉脚本前都检查；关窗时等后台任务收尾 8 秒，避免退出瞬间又拉起 BetterGI 留下孤儿进程。
- 修异环 11-04 三轮全卡在「等待开始游戏」（10:11 那次空转 60 分钟失败 #168）：等的是**启动器**上的「开始游戏」，而更新完游戏自己起来、游戏本体登录页的按钮是「**进入游戏**」（用户真实截图实测：模板 `异环-进入游戏.png` 0.961 @scale1.0、RapidOCR 0.72 @ (960,931)；`异环-开始游戏.png` 仅 0.430、`异环-确定.png` 仅 0.551）。步骤2 目标文字改 "开始游戏 / 进入游戏"，启动器旧按钮「开始游戏」「确定」改 `optional_if_missing` 并压缩超时；通用补「窗口句柄失效后按进程名重新接管窗口」，避免 OCR 扫 0 区域再空转 11 分钟。
- 统一入口（用户定规）：流程必须从主世界开始。`run_flow` 新增 `start_from_main`（主界面模板命中跳到指定步骤）与 `skip_steps_if_process`（游戏进程已在运行则跳过启动器步骤）；ZZZ_MONTHLY 接电台模板，NTE_MONTHLY 接 HTGame.exe 守卫，「进入游戏」改可选。
- 暖暖页面判定改为「专属特征 + 最少证据数」：`_is_stage_page` 不再因单个「晶石兑换」命中（兑换页/弹窗页误判），`_is_exchange_page` 需同时有「晶石兑换」标题与「本月限购」（共鸣商店页误判）；新增 `tests/suite_nikki_page_matrix_20261104.py`，17 张实际截图 × 10 个判定逐张断言互斥。
- 页面判定强制规范：新增 `tests/suite_page_judgement_policy.py`（判定函数登记表 + AST 拦裸单关键词 + ref_nikki_* 截图矩阵覆盖）；崩铁 `_bag_visible` 的「背包」改为只认左上角标题区（ESC 菜单会误判）；规则写入 docs/conventions/04-game-flows.md「页面判定铁律」。
