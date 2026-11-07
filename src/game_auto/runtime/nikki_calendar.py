"""无限暖暖 · 奇想日历领奖 + 晶石兑换/共鸣商店（2026-11-01）。

由 NikkiLauncher 流程的 action=nikki_calendar 驱动，固定在 nikki_enter_game 之后：
主界面 → 按 L 打开奇想日历 → 奇迹之冠 → 奇迹之冠搭配赛 → 领取奖励（如有）
→「空白区域继续」收尾 → 晶石兑换（每月一次）→ 回主界面 → 按 H 打开商城 →
左列滚动找「共鸣商店」→ 静谧水滴兑换（每月一次）→ ESC 回主界面。

几何定标（2026-11-01 用户截图实跑 RapidOCR，参考图 data/artifacts/ref_nikki_*_20261101.png，
1919x1079）：
  - 奇想日历标题 (0.094,0.047)；日历页「奇迹之冠」区块 (0.506,0.556)，
    页面特征：每日灵感 / 幻境挑战 / 奇迹之冠 / 星光的追寻（命中 ≥2 个）
  - 奇迹之冠 hub：「奇迹之冠搭配赛」(0.277,0.618)、「奇迹之冠争锋赛」(0.789,0.363)
  - 搭配赛页：「领取奖励」(0.501,0.697)、左列「晶石兑换」(0.078,0.718)、「赛事嘉奖」(0.078,0.830)
  - 奖励页：「恭喜获得」(0.501,0.259)、「F/空白区域继续」(0.902,0.962) → 点下方空白 (0.50,0.82)
  - 晶石兑换：共鸣水晶 (0.221,0.353) / 启示水晶 (0.364,0.353)，价格 120；
    「本月限购 N/M」在商品名上方、中心右移约 0.026 窗宽（N 是剩余次数）。
    价格 OCR 会被图标污染（120 常识别成 4120），所以**购买只用「本月限购」N 递减作到账证据**，
    不解析价格。
  - 兑换确认弹窗（用户 2026-11-01 截图）：标题「兑换」(0.501,0.209)、数量默认 1、
    底部按钮「确认」(0.500,0.751)、「本月限购 3/3」(0.622,0.538)、「合计」(0.580,0.651)。
    **标题也叫「兑换」且 conf 常高于按钮（0.66 vs 0.64）**：确认词表必须去掉「兑换」，
    按「确认」找按钮，否则会点标题、弹窗不动却被当成已确认。
  - 商城 → 共鸣商店（用户 2026-11-01 截图）：主界面 H 进商城（商城 (0.077,0.047) /
    购物车 (0.168,0.046)）；左列「美鸭梨优选」初始 (0.115,0.830)，滚轮向下后
    (0.115,0.287)，「共鸣商店」出现在 (0.105,0.787)。共鸣商店页两页签
    澎湃浪花兑换 (0.313,0.119) / 静谧水滴兑换 (0.500,0.119)。
    **必须切到静谧水滴兑换**：用专属商品 万象星实/虹彩棱镜/流辉棱镜/璨光棱镜 确认页签
    （澎湃页没有），没确认到就**不买**，防止买错货币；静谧页 启示水晶 (0.292,0.383) /
    共鸣水晶 (0.436,0.384)，本月限购 5/5 在名上方 (0.320,0.204)/(0.465,0.204)，单价 40。

设计约束：
- 领奖/兑换是「尽力而为」：失败只记 ERROR + flow_failures，**不阻断**后续 whimbox 一条龙
  （prelaunch 步骤失败会让整个暖暖任务不跑，代价远大于漏领一次）。流程内部异常也吞成
  警告，但用户在日志/失败记录里能查到。
- 晶石兑换 / 共鸣商店都按月门控（WorkflowDB meta `monthly_sequence:nikki_crystal_exchange`
  / `monthly_sequence:nikki_resonance_shop`），逐件购买（点商品 → 确认 → 空白区域继续），
  库存不足/没点到就如实记 shortfall/failed。
- **交接规则（2026-11-01 用户叮嘱）**：后续接 whimbox 前必须保证游戏在主界面；确认不了
  就调用 `_close_game_for_whimbox_restart` 关闭 `X6Game-Win64-Shipping.exe`，让 whimbox
  自己重新启动进入。**不要**「日历没跑成但把游戏留在菜单里」直接交出去。
- 「空白区域继续」是暖暖奖励界面的通用收尾：识别到就点屏幕下方空白处，
  **不要**按 ESC 或乱点按钮。
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..settings.paths import WORKFLOW_DB_PATH

# ── 页面文字特征 ────────────────────────────────────────────────────
CALENDAR_TITLE = "奇想日历"
CALENDAR_MARKERS = ("每日灵感", "幻境挑战", "奇迹之冠", "星光的追寻")
CROWN_ENTRY = "奇迹之冠"
CROWN_HUB_MARKERS = ("奇迹之冠搭配赛", "奇迹之冠争锋赛")
PAIR_ENTRY = "奇迹之冠搭配赛"
STAGE_MARKERS = ("赛事嘉奖", "晶石兑换", "第4场", "第5场", "第6场")
STAGE_CLAIM_MODAL = "巅峰赛速报"
CLAIM_TEXTS = ("领取奖励",)
CONTINUE_TEXTS = ("空白区域继续", "空白区域", "点击屏幕继续")
EXCHANGE_TITLE = "晶石兑换"
LIMIT_KEYWORD = "本月限购"
CONFIRM_TEXTS = ("确认兑换", "确认", "确定", "购买")
CANCEL_TEXTS = ("取消",)

# ── 顶栏图标点击：已停用，勿再接回主世界入口（2026-11-05 23:28 用户拍板）────
# 主世界鼠标被 UE5 锁在屏幕中心、按 Alt 才有光标；而顶栏每个图标都有官方
# 快捷键（商城 H / 奇想日历 L，见 nikki_topbar_ref_20261105.png）。
# `_open_calendar` / `_open_shop` 现在只按 L / H 三次，焦点由
# `_ensure_game_foreground` 保证。下面这些常量与 `_top_bar_icon_point`、
# `_calendar_icon_point` / `_shop_icon_point` / `_click_with_alt` 仅留给
# **纯 UI 页面内**的点击（那种页面鼠标自动可用），主世界入口不再调用。
CALENDAR_ICON_POINT = (0.802, 0.061)
CALENDAR_LABEL_OFFSET_RATIO = 0.035
CALENDAR_ICON_ATTEMPT = 2
SHOP_ICON_POINT = (0.637, 0.061)  # 1222/1920, 66/1080
SHOP_ICON_ATTEMPT = 2
# 主世界顶部菜单文字（OCR 兜底判主界面，日历页没有这些）
MAIN_OCR_MARKERS = ("商城", "换装", "任务", "设计图", "无限之心")

# ── 区域（窗口比例）────────────────────────────────────────────────
CALENDAR_CROWN_REGION = (0.40, 0.45, 0.75, 0.63)
HUB_PAIR_REGION = (0.12, 0.52, 0.48, 0.72)
STAGE_MENU_REGION = (0.00, 0.55, 0.22, 0.92)
DIALOG_REGION = (0.20, 0.15, 0.80, 0.88)
EXCHANGE_GRID_REGION = (0.15, 0.10, 0.90, 0.78)
BLANK_CONTINUE_POINT = (0.50, 0.82)
# 奇迹之冠 hub 左上角返回箭头（参考图 ref_nikki_crown_hub_20261101.png 实测）
CROWN_BACK_POINT = (0.031, 0.045)
# 点「奇迹之冠搭配赛」的点击重试次数（2026-11-02：点一次没进去）
CROWN_PAIR_CLICK_ATTEMPTS = 3
# 「奇迹之冠搭配赛」文字本身不是按钮（2026-11-02 用户实锤：点文字没反应），
# 真正可点的是文字**左上方**的多边形玻璃碎片。ref_nikki_crown_hub_20261101.png
# 实测文字框 (369,645)-(695,690)，亮白碎片重心 (541,569)（像素 247,247,250），
# 即文字中心上方约 2 个字高、x 几乎不变。
# 偏移按文字框宽/高算（换分辨率自适应），算不出来时退固定比例。
PAIR_CLICK_DX_RATIO = 0.03
PAIR_CLICK_DY_RATIO = -2.2
PAIR_FALLBACK_POINT = (0.28, 0.53)

# 搭配赛页左列「晶石兑换」也是「圆形图标在上、文字在下」，文字本身不是按钮。
# 2026-11-06 实机：文字中心 (149,774) 及左右/上下各偏移都点不动，点上移 42px 的
# (149,732) 一次就进兑换页（随后共鸣水晶×3 / 启示水晶×3 全部买满）。文字框高约
# 38px → 上移 1.1 个框高。偏移按 OCR 文字框高度算（换分辨率自适应），box 缺失时
# 退窗高比例 42/1080。与 `_pair_click_point` 同一口径，别再写死像素。
EXCHANGE_ICON_DY_BOX_RATIO = -1.1
EXCHANGE_ICON_DY_WINDOW_RATIO = -0.039

# 商品名 → 本月限购的相对位置（实测：名中心上方 0.18 窗高、右移 0.026 窗宽）
LIMIT_DX = 0.026
LIMIT_BAND_DX = 0.10
LIMIT_DY_MIN = -0.26
LIMIT_DY_MAX = -0.10

# ── 商城 → 共鸣商店（2026-11-01 用户截图定标，1919x1079）────────────
# 商城左列：初始「美鸭梨优选」(0.115,0.830)，滚动后 (0.115,0.287)；
# 「共鸣商店」滚动后 (0.105,0.787)。页面标志用 商城+购物车（主界面只有商城，没有购物车）。
SHOP_MARKERS = ("商城", "购物车")
SHOP_MENU_ITEM = "美鸭梨优选"
RESONANCE_ENTRY = "共鸣商店"
RESONANCE_TABS = ("澎湃浪花兑换", "静谧水滴兑换")
RESONANCE_TAB_TARGET = "静谧水滴兑换"
# 两个页签的**商品指纹**（2026-11-06 用户提出，对齐绝区零/崩铁/原神的
# tab_content_markers + forbidden_texts 双向闸）。两页都卖「启示水晶/共鸣水晶/
# 无敌碎星闪粉」，只是单价不同（澎湃 20 / 静谧 40），**这三个不能当指纹**；
# 必须靠各自独占的商品。参考图 ref_nikki_resonance_{surge,serene}_20261101.png。
# 静谧页独占（澎湃页没有），用来**正向确认**已切到静谧水滴兑换。
SERENE_MARKERS = ("万象星实", "璨光棱镜", "流辉棱镜", "虹彩棱镜",
                  "噗灵", "纯真丝线", "闪亮泡泡")
# 澎湃页独占（静谧页没有），用来**反向排除**；命中即说明还在澎湃浪花兑换，
# 绝不能按 40 静谧水滴的口径去点购买，否则花错货币。
SURGE_MARKERS = ("慢慢恢复花露", "精萃慢慢恢复花露", "缓速花露",
                 "眩晕花露", "日光果", "星光果", "盛宴毛毡")
SHOP_MENU_REGION = (0.00, 0.15, 0.22, 0.95)
RESONANCE_TAB_REGION = (0.22, 0.07, 0.78, 0.18)
SHOP_GRID_REGION = (0.15, 0.14, 0.92, 0.95)
# 左列滚动（2026-11-06 实机）：滚轮逐格、拖左列滚动条**完全无效**——
# 20:26 十三轮滚轮、20:51 三次拖条，左列 OCR 恒定「越冬之约…美鸭梨优选」。
# 必须按住左键**从下往上一次大位移**拖动；分成多小步时 UE5 只认第一个位移，
# 后面全被吞（22:19 分 30 小步时拖 3 次才滚到底，22:22 一次大位移就够）。
SHOP_MENU_DRAG = (0.08, 0.88, 0.23)  # x / y起点 / y终点，窗口比例
SHOP_MENU_DRAG_TIMES = 3             # 最多拖 3 次，每次复扫 OCR

NAME_MIN_CONF = 0.45
FUZZY_MIN_RATIO = 0.85
FUZZY_MARGIN = 0.05

MONTHLY_META_KEY = "monthly_sequence:nikki_crystal_exchange"
RESONANCE_META_KEY = "monthly_sequence:nikki_resonance_shop"
_LIMIT_RE = re.compile(r"本月限购\s*(\d+)\s*/\s*(\d+)")


def _stop(stop_check: Optional[Callable[[], bool]]) -> bool:
    return bool(stop_check and stop_check())


def _norm(text: str) -> str:
    from .ocr_click_tester import _normalize_text

    return _normalize_text(str(text or ""))


def _ocr_full(logger: logging.Logger, hwnd: int, region=None) -> List[Dict]:
    try:
        from .ocr_click_tester import ocr_window_content

        return ocr_window_content(logger, hwnd=hwnd, region=region) or []
    except Exception:  # noqa: BLE001 - OCR 失败按空帧处理，由等待逻辑重试
        logger.warning("[无限暖暖·奇想日历] OCR 失败", exc_info=True)
        return []


def _window_rect(hwnd: int):
    from .foreground_flow_runner import _window_rect as impl

    return impl(hwnd)


def _focus(hwnd: int) -> None:
    from .foreground_flow_runner import _focus_window

    _focus_window(hwnd)


def _click(center) -> None:
    from .foreground_flow_runner import _click_screen

    _click_screen(int(center[0]), int(center[1]))


def _click_with_alt(point) -> None:
    """主世界顶栏点击：先按住 Alt 呼出被 UE5 锁定的鼠标，点完松开。

    用户 2026-11-05 指出：暖暖正常游玩时鼠标被锁在屏幕正中，**按 Alt 才有
    可自由移动的光标**。项目里 `_click_screen` 是 `pyautogui.moveTo(x,y)` +
    按住点击，光标被游戏拉回中心时点击就落在世界里，顶栏图标收不到
    （11-05 08:32 点「奇想日历」图标、11-04 点「商城」都没反应）。
    顶栏放大图 `data/artifacts/nikki_topbar_ref_20261105.png` 证实每个图标
    都有官方快捷键（商城 H / 奇想日历 L / 共鸣 O / 任务 U / 换装 C…），
    **键盘仍是主路径**；这里只给鼠标兜底加一道保险。

    「按一下 Alt 切换显示」和「必须按住 Alt 才显示」两种机制都覆盖：
    按下 → 等一帧 → 点击 → 松开，点击全程 Alt 处于按下状态。
    finally 保证异常时也松开，不把修饰键留在系统里。
    """
    import win32api
    import win32con

    scan = win32api.MapVirtualKey(win32con.VK_MENU, 0)
    win32api.keybd_event(win32con.VK_MENU, scan, 0, 0)
    try:
        time.sleep(0.15)
        _click(point)
        time.sleep(0.10)
    finally:
        win32api.keybd_event(win32con.VK_MENU, scan, win32con.KEYEVENTF_KEYUP, 0)


def _click_ratio(logger: logging.Logger, hwnd: int, frac: Tuple[float, float], label: str,
                 unlock_mouse: bool = False) -> bool:
    rect = _window_rect(hwnd)
    if not rect:
        logger.warning("[无限暖暖·奇想日历] 窗口区域不可用，无法点击%s", label)
        return False
    left, top, width, height = rect
    point = (left + int(width * frac[0]), top + int(height * frac[1]))
    if unlock_mouse:
        _click_with_alt(point)
    else:
        _click(point)
    logger.info("[无限暖暖·奇想日历] 点击%s @ %s%s", label, point,
                "（Alt 呼出鼠标）" if unlock_mouse else "")
    return True


def _press_key(key: str) -> None:
    """游戏内按键（L 奇想日历 / H 商城）：统一发带扫描码、按住 0.15s 的真实按键。

    2026-11-05 更正归因：扫描码是**加固**，不是「日历打不开」的根因。
    11-01 12:26:10 用 `pyautogui.press('l')` 第 2 下就打开了日历（OCR 区域数
    18→33，随后进到奇迹之冠 hub），证明瞬时按键对暖暖并非一概无效；暖暖是
    **UE5**，与今天绝区零（Unity）按键被吞是两件事，别再合并解释。
    11-05 真正的退化在 `_open_calendar` 的尝试顺序与图标坐标（见该函数注释）。
    """
    from .foreground_flow_runner import press_key_scancode

    press_key_scancode(key)


def _foreground_snapshot() -> Dict:
    """当前**前台窗口**的硬证据：hwnd / pid / exe / title。

    `record_failure` 的 `window_title` 记的是**目标**窗口，不是前台窗口；
    截图又是按目标 hwnd 矩形裁剪的全屏画面。所以历史失败记录能证明
    「游戏画面没变」，却回答不了「按键发给了谁」（用户 23:50 追问）。
    这里专门把前台窗口本身查出来，随日志/快照一起留证。
    """
    import win32gui
    import win32process

    try:
        fg = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(fg) if fg else (0, 0)
        exe = ""
        if pid:
            try:
                import psutil

                exe = psutil.Process(pid).name()
            except Exception:  # noqa: BLE001 - 取不到进程名不影响其它字段
                pass
        return {
            "hwnd": int(fg or 0),
            "pid": int(pid or 0),
            "exe": exe,
            "title": win32gui.GetWindowText(fg) if fg else "",
        }
    except Exception as exc:  # noqa: BLE001 - 取证自身不能拖垮按键链路
        return {"error": str(exc)}


def _record_key_no_response(logger: logging.Logger, key: str, hwnd: int) -> None:
    """三次按键都没打开目标页面：落**全屏**快照 + 前台窗口硬证据。

    故意 `hwnd=None`：`capture_failure_image` 传了 hwnd 会按窗口矩形裁剪，
    只能看到游戏；不传才是整屏，能看出有没有别的窗口盖着 / 谁在前台。
    只在三次全失败这条罕见路径抓一次，避免把用户桌面常态化存盘。
    """
    snap = _foreground_snapshot()
    logger.error("[无限暖暖] 按 %s 三次无反应｜前台窗口=%s｜目标 hwnd=%s",
                 str(key).upper(), snap, hwnd)
    try:
        from .flow_diagnostics import capture_failure_image

        raw, annotated = capture_failure_image(
            logger, hwnd=None,
            tag=f"NikkiLauncher_按{key.upper()}三次无反应_前台取证",
        )
        logger.info("[无限暖暖] 全屏取证快照: %s", raw or annotated or "无")
    except Exception as exc:  # noqa: BLE001 - 取证失败不影响流程
        logger.debug("[无限暖暖] 全屏取证失败: %s", exc)


def _ensure_game_foreground(logger: logging.Logger, hwnd: int) -> bool:
    """按键前把游戏窗口拉到前台；按 **PID** 比较，兼容 UE5 多窗口。

    2026-11-05 23:50 更正：`keybd_event` 只发给前台窗口是事实，但
    **11-04 按 H 那一刻前台是谁，现有日志/快照都没有记录**：
    - 08:49 两次 `SetForegroundWindow 失败` 发生在**启动器阶段**（游戏窗口
      08:50:09 才就绪），不能拿来说 08:52 的 H；
    - 08:50:40「窗口已最小化、还原失败」之后 08:51:44 起 OCR 正常、08:52:16
      报「已进入主界面」，按 H 时窗口是可用的；
    - 失败快照是按目标 hwnd 裁剪的全屏图，画面是干净的游戏主世界、没有被
      别的窗口遮挡 —— 反而**不支持**「窗口被盖住」。
    所以「H 发丢了」只是候选解释之一（另有按键形式、游戏状态、页面判定
    假阴性），不是定论。这个函数是**加固 + 取证**：每轮记录真实前台窗口，
    三次失败另有全屏快照（见 `_record_key_no_response`）。

    不用 `_focus_window`：它会发一次 Alt 短按绕过 Windows 前台锁，而 Alt 正是
    暖暖「呼出鼠标」的切换键，会顺手把游戏状态切走（用户 23:11 指出）。
    这里改用 `AttachThreadInput` 正经抢前台，finally 保证一定 detach。
    """
    import win32api
    import win32con
    import win32gui
    import win32process

    try:
        _, target_pid = win32process.GetWindowThreadProcessId(hwnd)
        snap = _foreground_snapshot()
        logger.info("[无限暖暖] 按键前前台窗口: %s（目标 hwnd=%s pid=%s）",
                    snap, hwnd, target_pid)
        fg = win32gui.GetForegroundWindow()
        _, fg_pid = win32process.GetWindowThreadProcessId(fg) if fg else (0, 0)
        if fg_pid == target_pid:
            return True
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        tid_fg = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        tid_me = win32api.GetCurrentThreadId()
        attached = False
        try:
            if tid_fg and tid_fg != tid_me:
                win32process.AttachThreadInput(tid_me, tid_fg, True)
                attached = True
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            if attached:
                win32process.AttachThreadInput(tid_me, tid_fg, False)
        time.sleep(0.35)
        fg2 = win32gui.GetForegroundWindow()
        _, fg2_pid = win32process.GetWindowThreadProcessId(fg2) if fg2 else (0, 0)
        ok = fg2_pid == target_pid
        if not ok:
            snap2 = _foreground_snapshot()
            logger.warning(
                "[无限暖暖] 前台不是游戏：目标 hwnd=%s pid=%s，前台窗口=%s",
                hwnd, target_pid, snap2,
            )
        return ok
    except Exception as exc:  # noqa: BLE001 - 聚焦失败仍按键，但必须留下证据
        logger.warning("[无限暖暖] 聚焦游戏窗口失败：%s", exc)
        return False


def _focus_and_press(hwnd: int, key: str) -> None:
    """L / H 按键：先确保游戏在前台，再发带扫描码的真实按键。

    11-02 曾把「按 L 三次打不开」归因为焦点没切过来（方向对，但实现只是
    `_focus()` 后 sleep 0.35 再查一次 hwnd 是否相等，抢不回来照样静默按键）。
    现在改成 PID 校验 + AttachThreadInput 抢前台，抢不回来明确告警。
    """
    from .foreground_flow_runner import _release_modifier_keys

    logger = logging.getLogger("GameAutomation")
    ok = _ensure_game_foreground(logger, hwnd)
    # 先抬起 Alt/Ctrl/Shift/Win：残留修饰键会让 L/H 被当成组合键吞掉
    _release_modifier_keys()
    _press_key(key)
    logger.info("[无限暖暖] 已按 %s（前台确认=%s，目标 hwnd=%s）",
                str(key).upper(), ok, hwnd)


def _press_esc() -> None:
    from .nikki_launcher import _press_esc as impl

    impl()


def _has_main(hwnd: int, scales, confidences) -> bool:
    from .nikki_launcher import _has_main as impl

    try:
        return bool(impl(hwnd, scales, confidences))
    except Exception:  # noqa: BLE001 - 模板缺失/窗口失效按非主界面处理
        return False


def _abs_region(rect, frac) -> Tuple[int, int, int, int]:
    left, top, width, height = rect
    fx0, fy0, fx1, fy1 = frac
    return (
        left + int(width * fx0),
        top + int(height * fy0),
        int(width * (fx1 - fx0)),
        int(height * (fy1 - fy0)),
    )


def _in_abs(center, region) -> bool:
    if not center or not region:
        return False
    x, y = int(center[0]), int(center[1])
    left, top, width, height = region
    return left <= x <= left + width and top <= y <= top + height


def _pick(
    items: List[Dict],
    texts,
    min_conf: float = 0.5,
    region=None,
    exclude=(),
) -> Optional[Dict]:
    """在 OCR 结果里挑目标文字：精确优先，其次子串；排除黑名单（如「取消」）。"""
    exact = None
    partial = None
    for item in items or []:
        text = _norm(item.get("text") or "")
        if not text:
            continue
        try:
            conf = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        if any(_norm(x) in text for x in exclude if x):
            continue
        center = item.get("center")
        if region and not _in_abs(center, region):
            continue
        for cand in texts:
            cnorm = _norm(cand)
            if not cnorm:
                continue
            if text == cnorm:
                if exact is None or conf > exact[0]:
                    exact = (conf, item)
                break
            if cnorm in text:
                if partial is None or conf > partial[0]:
                    partial = (conf, item)
    best = exact or partial
    return best[1] if best else None


def _pick_confirm(items: List[Dict], region) -> Optional[Dict]:
    """兑换弹窗按钮：按词序找（确认兑换 > 确认 > 确定 > 购买）。

    弹窗标题也叫「兑换」且置信度常高于按钮（实测 0.66 vs 0.64），词表里不能含
    「兑换」，否则会点标题、弹窗不动却被当成已确认。
    """
    for cand in CONFIRM_TEXTS:
        hit = _pick(items, (cand,), min_conf=0.5, region=region, exclude=CANCEL_TEXTS)
        if hit:
            return hit
    return None


def _markers_present(items: List[Dict], markers, min_conf: float = 0.45) -> set:
    found = set()
    for item in items or []:
        text = _norm(item.get("text") or "")
        if not text:
            continue
        try:
            conf = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            continue
        if conf < min_conf:
            continue
        for marker in markers:
            if _norm(marker) in text:
                found.add(marker)
    return found


def _is_calendar_page(items: List[Dict]) -> bool:
    # 主世界顶部 HUD 也有「奇想日历」图标标签，OCR 常把整排连读成
    # 「奇想之旅·活动大厅·奇想日历·无羁之心」→ 只看标题会误判"已在日历页"，
    # 流程不再点图标/按 L，后面全在主世界空找（2026-11-04 08:52 截图实锤）。
    if _is_main_world(items):
        return False
    # 标题「奇想日历」也算页面特征：正文特征（每日灵感/幻境挑战…）是艺术字，
    # OCR 认不出来时靠标题兜底（2026-11-02 失败截图停在主界面就是这么丢的）
    if _pick(items, (CALENDAR_TITLE,), min_conf=0.5):
        return True
    return len(_markers_present(items, CALENDAR_MARKERS)) >= 2


def _is_main_world(items: List[Dict]) -> bool:
    """OCR 兜底判断是否主世界（顶部菜单文字命中 ≥2 个）。"""
    return len(_markers_present(items, MAIN_OCR_MARKERS)) >= 2


def _top_bar_icon_point(items: List[Dict], rect, label: str) -> Optional[Tuple[int, int]]:
    """顶栏图标点击点：只认**独立短标签**，命中后上移到图标中心。

    标签在图标下方（参考图实测标签 y≈105、图标中心 y≈66，1919x1079），
    所以按窗口高度的 0.035 上移；连读/无标签返回 None，由调用方退回固定比例。

    **顶栏连读不能当标签**（2026-11-05 08:32 实锤）：RapidOCR 常把整排图标读成
    「奇迹之旅活动大厅 奇想日历 无限之心」@(1502,103)，其 center 是整排中心，
    旧代码据此算出 (1503,65) 点下去 —— 落在相邻图标/空白处，日历没开，
    后续按 L 也没恢复。「商城」标签同样可能与相邻图标连读。
    """
    if not rect:
        return None
    left, top, width, height = rect
    # 连读特征词要排除 label 自身，否则「奇想日历 L」「商城 H」这类带键位提示的
    # 独立标签会被自己误杀（2026-11-05 回归 [5] 抓到）
    connected_words = tuple(
        word for word in MAIN_OCR_MARKERS
        + ("活动大厅", "奇迹之旅", "奇想日历", "共鸣")
        if word != label
    )
    for item in items or []:
        text = _norm(item.get("text") or "")
        if label not in text:
            continue
        # 连读块含其它顶栏词或明显过长：中心不代表该图标的位置
        if text != label and (
            len(text) > len(label) + 2
            or any(word in text for word in connected_words)
        ):
            continue
        center = item.get("center")
        if not center:
            continue
        x, y = center
        # 只要顶部 16% 高度内、右半边的那一个，避免命中页面里的同名文字
        if y <= top + height * 0.16 and x >= left + width * 0.55:
            return (int(x), int(y - height * CALENDAR_LABEL_OFFSET_RATIO))
    return None


def _calendar_icon_point(items: List[Dict], rect) -> Optional[Tuple[int, int]]:
    """右上角「奇想日历」图标点击点；连读时返回 None 由调用方用固定比例。"""
    return _top_bar_icon_point(items, rect, CALENDAR_TITLE)


def _shop_icon_point(items: List[Dict], rect) -> Optional[Tuple[int, int]]:
    """主世界顶栏「商城」图标点击点：按 H 打不开商城时的兜底入口。"""
    return _top_bar_icon_point(items, rect, "商城")


def _is_stage_page(items: List[Dict]) -> bool:
    # 「晶石兑换」「领取奖励」在兑换页/奖励页也会出现，单条命中不能证明是搭配赛页。
    # 2026-11-04 页面矩阵实测：旧写法把 exchange/dialog 页全判成搭配赛页。
    # 现在的证据必须至少两条：巅峰赛速报、第4/5/6场、赛事嘉奖、晶石兑换…
    if _pick(items, (STAGE_CLAIM_MODAL,), 0.5):
        return True
    return len(_markers_present(items, STAGE_MARKERS, 0.55)) >= 2


def _is_exchange_page(items: List[Dict]) -> bool:
    # 共鸣商店页也有「本月限购」；必须同时有「晶石兑换」标题才算兑换页。
    return bool(
        _pick(items, (EXCHANGE_TITLE,), 0.5)
        and _pick(items, (LIMIT_KEYWORD,), 0.5)
    )


def _wait_for(
    logger: logging.Logger,
    hwnd: int,
    predicate: Callable[[List[Dict]], bool],
    timeout: float,
    stop_check=None,
    interval: float = 1.0,
) -> Optional[List[Dict]]:
    end = time.time() + max(0.5, float(timeout))
    while time.time() < end:
        if _stop(stop_check):
            return None
        items = _ocr_full(logger, hwnd)
        if predicate(items):
            return items
        time.sleep(interval)
    return None


def _dismiss_continue_screens(
    logger: logging.Logger,
    hwnd: int,
    stop_check=None,
    max_rounds: int = 4,
) -> int:
    """暖暖奖励界面通用收尾：识别「空白区域继续」→ 点屏幕下方空白处。

    多次出现（连续领奖）时循环处理；没有该提示就立即返回 0。
    """
    dismissed = 0
    for _ in range(max(1, int(max_rounds))):
        if _stop(stop_check):
            break
        items = _ocr_full(logger, hwnd)
        hit = _pick(items, CONTINUE_TEXTS, min_conf=0.5)
        if not hit:
            break
        if not _click_ratio(logger, hwnd, BLANK_CONTINUE_POINT, "「空白区域继续」空白处"):
            break
        dismissed += 1
        time.sleep(1.0)
    return dismissed


def _record_failure(
    logger: logging.Logger,
    step: str,
    message: str,
    hwnd,
    ocr_results: Optional[List[Dict]] = None,
) -> None:
    try:
        from .flow_diagnostics import record_failure

        record_failure(
            logger,
            flow_key="NikkiLauncher",
            step=step,
            error=message,
            hwnd=hwnd,
            ocr_results=ocr_results,
            extra={"kind": "nikki_calendar"},
        )
    except Exception as exc:  # noqa: BLE001 - 诊断自身失败不能影响主流程
        logger.debug("[无限暖暖·奇想日历] 记录失败诊断异常 %s: %s", step, exc)


def _load_nikki_shop_plan(logger: logging.Logger, section_name: str, label: str) -> Dict:
    """读 app_config.json → nikki.<section_name> 的通用清单解析。

    返回 {"enabled": bool, "items": [{"name","qty"}]}；qty 缺省/非法/<1 按 1，
    上限 99（防配置手滑把循环开太大）。读不到配置返回空清单（跳过兑换，不乱买东西）。
    """
    plan: Dict = {"enabled": True, "items": []}
    try:
        from ..settings import manager as cfg_manager

        cfg = cfg_manager.load(force=True) or {}
        section = (cfg.get("nikki") or {}).get(section_name) or {}
    except Exception as e:  # noqa: BLE001 - 配置读失败只降级，不炸一条龙
        logger.warning("[无限暖暖·%s] 读取配置失败（按空清单处理）: %s", label, e)
        return plan

    plan["enabled"] = bool(section.get("enabled", True))
    raw_items = section.get("items") or []
    if not isinstance(raw_items, list):
        logger.warning("[无限暖暖·%s] 配置 items 不是列表，按空清单处理", label)
        raw_items = []
    for raw in raw_items:
        if isinstance(raw, str):
            raw = {"name": raw}
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        try:
            qty = int(raw.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1
        qty = max(1, min(99, qty))
        plan["items"].append({"name": name, "qty": qty})
    logger.info(
        "[无限暖暖·%s] 清单载入 %s 项%s",
        label,
        len(plan["items"]),
        "：" + "、".join(
            f"{i['name']}×{i['qty']}" if i["qty"] > 1 else i["name"] for i in plan["items"]
        )
        if plan["items"]
        else "（空清单，跳过兑换）",
    )
    return plan


def load_crystal_exchange_plan(logger: logging.Logger) -> Dict:
    """晶石兑换清单（app_config.json → nikki.crystal_exchange）。"""
    return _load_nikki_shop_plan(logger, "crystal_exchange", "晶石兑换")


def load_resonance_shop_plan(logger: logging.Logger) -> Dict:
    """共鸣商店清单（app_config.json → nikki.resonance_shop）。"""
    return _load_nikki_shop_plan(logger, "resonance_shop", "共鸣商店")


def _month_key() -> str:
    return time.strftime("%Y-%m")


def _monthly_done(logger: logging.Logger, meta_key: str = MONTHLY_META_KEY) -> bool:
    try:
        from ..storage.workflow_db import WorkflowDB

        db = WorkflowDB(str(WORKFLOW_DB_PATH))
        return db.get_task_meta(meta_key) == _month_key()
    except Exception as exc:  # noqa: BLE001 - 读不到就当没做过
        logger.debug("[无限暖暖·月度标记] 读取失败 %s: %s", meta_key, exc)
        return False


def _mark_monthly_done(logger: logging.Logger, meta_key: str = MONTHLY_META_KEY) -> None:
    try:
        from ..storage.workflow_db import WorkflowDB

        db = WorkflowDB(str(WORKFLOW_DB_PATH))
        db.set_task_meta(meta_key, _month_key())
        logger.info("[无限暖暖·月度标记] 已写入 %s=%s", meta_key, _month_key())
    except Exception as exc:  # noqa: BLE001 - 写标记失败下次会重试，不阻塞
        logger.warning("[无限暖暖·月度标记] 写入失败 %s: %s", meta_key, exc)


def _find_item_name(
    items: List[Dict],
    target: str,
    rect,
    grid_frac=EXCHANGE_GRID_REGION,
) -> Optional[Dict]:
    """在商店网格里按名称找商品卡（精确优先，其次唯一模糊匹配）。"""
    tnorm = _norm(target)
    if not tnorm:
        return None
    grid = _abs_region(rect, grid_frac)
    exact: List[Dict] = []
    fuzzy: List[Tuple[float, Dict]] = []
    for item in items or []:
        try:
            conf = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            continue
        center = item.get("center")
        if conf < NAME_MIN_CONF or not _in_abs(center, grid):
            continue
        text = _norm(item.get("text") or "")
        if not text:
            continue
        if text == tnorm:
            exact.append(item)
            continue
        ratio = difflib.SequenceMatcher(None, tnorm, text).ratio()
        if ratio >= FUZZY_MIN_RATIO:
            fuzzy.append((ratio, item))
    if exact:
        return max(exact, key=lambda r: float(r.get("confidence") or 0.0))
    if not fuzzy:
        return None
    fuzzy.sort(key=lambda x: x[0], reverse=True)
    if len(fuzzy) > 1 and (fuzzy[0][0] - fuzzy[1][0]) < FUZZY_MARGIN:
        return None  # 两个候选都像 → 宁可不买
    return fuzzy[0][1]


def _read_limit_near(items: List[Dict], name_center, rect) -> Optional[Tuple[int, int, str]]:
    """读商品卡上方的「本月限购 N/M」，返回 (剩余, 上限, 原文)。"""
    if not rect or not name_center:
        return None
    _, _, width, height = rect
    nx, ny = int(name_center[0]), int(name_center[1])
    for item in items or []:
        text = str(item.get("text") or "")
        match = _LIMIT_RE.search(text)
        if not match:
            continue
        center = item.get("center")
        if not center:
            continue
        x, y = int(center[0]), int(center[1])
        dx = (x - nx) / float(width)
        dy = (y - ny) / float(height)
        if -LIMIT_BAND_DX <= dx <= LIMIT_BAND_DX and LIMIT_DY_MIN <= dy <= LIMIT_DY_MAX:
            try:
                return int(match.group(1)), int(match.group(2)), text.strip()
            except ValueError:
                continue
    return None


def _wait_confirm(logger: logging.Logger, hwnd: int, timeout: float, stop_check=None) -> Optional[Dict]:
    """等商品兑换弹窗的确认按钮（「确认兑换/兑换/购买/确认」）。"""
    rect = _window_rect(hwnd)
    if not rect:
        return None
    region = _abs_region(rect, DIALOG_REGION)
    end = time.time() + max(0.5, timeout)
    while time.time() < end:
        if _stop(stop_check):
            return None
        items = _ocr_full(logger, hwnd)
        hit = _pick_confirm(items, region)
        if hit:
            return hit
        time.sleep(0.8)
    return None


def _buy_one(
    logger: logging.Logger,
    hwnd: int,
    name: str,
    stop_check=None,
    grid_frac=EXCHANGE_GRID_REGION,
) -> Dict:
    """买一件商品。返回 {"state", "detail", "before", "after"}。

    state: purchased / missing / sold_out / no_dialog / unconfirmed / failed
    到账证据只认「本月限购 N」递减或购买后出现「空白区域继续」奖励页，二者都没有就
    unconfirmed（不报假成功）。
    """
    out: Dict = {"state": "failed", "detail": "", "before": None, "after": None}
    rect = _window_rect(hwnd)
    if not rect:
        out["detail"] = "窗口区域不可用"
        return out
    _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=2)
    items = _ocr_full(logger, hwnd)
    match = _find_item_name(items, name, rect, grid_frac)
    if not match:
        out.update(state="missing", detail="兑换页商品网格里没找到该商品")
        logger.warning("[无限暖暖·晶石兑换] %s 未在网格中找到（可能不在首屏/需翻页）", name)
        return out
    name_center = (int(match["center"][0]), int(match["center"][1]))
    limit = _read_limit_near(items, name_center, rect)
    before = limit[0] if limit else None
    out["before"] = before
    if before == 0:
        out.update(state="sold_out", detail=f"本月限购已为 0（{limit[2] if limit else ''}）")
        logger.info("[无限暖暖·晶石兑换] %s 本月已换完", name)
        return out
    logger.info(
        "[无限暖暖·晶石兑换] 点击商品 %r（OCR %r conf %.2f）@ %s%s",
        name,
        match.get("text"),
        float(match.get("confidence") or 0),
        name_center,
        f"，本月限购 {before}" if before is not None else "（本月限购未识别）",
    )
    _click(name_center)

    confirm = _wait_confirm(logger, hwnd, timeout=6.0, stop_check=stop_check)
    dismissed = 0
    if confirm:
        center = confirm.get("center")
        if center:
            _click(center)
            logger.info("[无限暖暖·晶石兑换] 点击「%s」@ %s", confirm.get("text"), center)
        time.sleep(1.0)
        dismissed = _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=3)
    else:
        logger.warning("[无限暖暖·晶石兑换] %s 点击后未出现确认弹窗", name)

    time.sleep(0.8)
    fresh = _ocr_full(logger, hwnd)
    after_limit = _read_limit_near(fresh, name_center, rect)
    after = after_limit[0] if after_limit else None
    out["after"] = after
    if before is not None and after is not None and after < before:
        out.update(state="purchased", detail=f"本月限购 {before}→{after}")
        logger.info("[无限暖暖·晶石兑换] %s 购买成功（本月限购 %s→%s）", name, before, after)
        return out
    if dismissed:
        out.update(state="purchased", detail="购买后出现「空白区域继续」奖励页")
        logger.info("[无限暖暖·晶石兑换] %s 购买成功（奖励页已确认）", name)
        return out
    if not confirm:
        out.update(state="no_dialog", detail="点击后未出现确认弹窗，且无到账证据")
        logger.warning("[无限暖暖·晶石兑换] %s %s", name, out["detail"])
        return out
    out.update(state="unconfirmed", detail=f"点了确认但本月限购未变化（{before}→{after}）")
    logger.warning("[无限暖暖·晶石兑换] %s %s", name, out["detail"])
    return out


def _open_calendar(logger: logging.Logger, hwnd: int, stop_check=None) -> bool:
    """打开奇想日历：**就按 L，三次**。L 是顶栏图标的官方快捷键。

    历史（别再往「点图标」方向绕）：
    - 11-01 纯按 L 第 2 下成功（12:26:10，OCR 18→33）并进到奇迹之冠 hub，
      用的还是瞬时 `pyautogui.press('l')`。第 1 下常被窗口激活吞掉，故留 3 次。
    - 11-02 / 11-03 三次 L 全失败；11-04 被顶栏连读误判成「已在日历页」；
      11-05 改成「点右上角图标」也没打开。
    - 11-05 23:28：主世界鼠标被 UE5 锁在屏幕中心、按 Alt 才有光标，
      **点图标本来就不该是入口**；改为每轮按键前由 `_focus_and_press` 抢前台。
    - 11-05 23:50 更正：「11-04 按键发丢了」只是候选解释，**没有直接证据**
      （两次 `SetForegroundWindow 失败` 在启动器阶段、窗口不可用之后已恢复、
      失败快照是干净的游戏画面）。现在每轮记录真实前台窗口，三次失败另落
      全屏快照，明早实跑才能定案。
    - 不按 ESC：主世界 ESC 会开暂停菜单，菜单盖着时 L 完全无效（11-03 实锤）。
    """
    for attempt in range(1, 4):
        if _stop(stop_check):
            return False
        items = _ocr_full(logger, hwnd)
        if _is_calendar_page(items):
            logger.info("[无限暖暖·奇想日历] 已在奇想日历页")
            return True
        _focus_and_press(hwnd, "l")
        logger.info("[无限暖暖·奇想日历] 按 L 打开奇想日历（第 %s/3 次）", attempt)
        items = _wait_for(logger, hwnd, _is_calendar_page, timeout=8.0, stop_check=stop_check)
        if items:
            logger.info("[无限暖暖·奇想日历] 已进入奇想日历")
            return True
        # 打开日历可能先弹「领取奖励/空白区域继续」页：收掉后再复检，
        # 此时日历其实已经在下面，别再按 L 把它关掉。
        _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=2)
        items = _ocr_full(logger, hwnd)
        if _is_calendar_page(items):
            logger.info("[无限暖暖·奇想日历] 收掉奖励页后已进入奇想日历")
            return True
    _record_key_no_response(logger, "l", hwnd)
    return False


def _leave_crown_hub(logger: logging.Logger, hwnd: int, stop_check=None) -> bool:
    """从奇迹之冠 hub 退回上一层（点左上角返回箭头）。

    2026-11-02 实锤：搭配赛页点不进去时流程停在 hub，而 hub 是全屏 overlay，
    ESC 关不掉 →「回主界面」失败 → 共鸣商店被整段跳过，晶石兑换入口也找不到
    （左列菜单在搭配赛页才有）。hub 左上角有返回箭头，点它能退回奇想日历。
    """
    items = _ocr_full(logger, hwnd)
    if not _pick(items, CROWN_HUB_MARKERS, min_conf=0.5):
        return False
    if not _window_rect(hwnd):
        return False
    _click_ratio(logger, hwnd, CROWN_BACK_POINT, "奇迹之冠左上角返回")
    time.sleep(1.2)
    after = _ocr_full(logger, hwnd)
    left = not _pick(after, CROWN_HUB_MARKERS, min_conf=0.5)
    logger.info("[无限暖暖·奇想日历] 退出奇迹之冠 hub：%s", "成功" if left else "未确认")
    return left


def _pair_click_point(pair: Dict, rect) -> Tuple[int, int]:
    """「奇迹之冠搭配赛」的实际可点坐标：文字左上方的多边形碎片内部。

    OCR 的 box 是截图局部坐标（宽高可用），center 是屏幕坐标；
    文字中心点击不生效，向上偏移约 2.2 个文字高度才对上碎片
    （参考图实测文字中心 (531,667) → 碎片重心 (541,569)）。
    """
    cx, cy = pair["center"]
    box = pair.get("box")
    if box:
        try:
            x1, y1, x2, y2 = (int(v) for v in box)
            box_w = max(1, x2 - x1)
            box_h = max(1, y2 - y1)
            return (
                int(cx + box_w * PAIR_CLICK_DX_RATIO),
                int(cy + box_h * PAIR_CLICK_DY_RATIO),
            )
        except (TypeError, ValueError):
            pass
    left, top, width, height = rect
    return (
        int(left + width * PAIR_FALLBACK_POINT[0]),
        int(top + height * PAIR_FALLBACK_POINT[1]),
    )


def _exchange_entry_point(entry: Dict, rect) -> Tuple[int, int]:
    """「晶石兑换」入口真正可点的坐标：文字正上方的圆形图标。

    与 `_pair_click_point` 同理：左列是图标在上、文字在下，点文字中心无反应。
    偏移优先按 OCR 文字框高度（随分辨率缩放），box 取不到时退窗高比例。
    `entry["center"]` 是屏幕绝对坐标，返回值也保持屏幕绝对坐标。
    """
    cx, cy = entry["center"]
    box = entry.get("box")
    if box:
        try:
            _x1, y1, _x2, y2 = (int(v) for v in box)
            box_h = max(1, y2 - y1)
            return (int(cx), int(cy + box_h * EXCHANGE_ICON_DY_BOX_RATIO))
        except (TypeError, ValueError):
            pass
    _left, _top, _width, height = rect
    return (int(cx), int(cy + height * EXCHANGE_ICON_DY_WINDOW_RATIO))


def _resonance_entry_point(entry: Dict, rect) -> Tuple[int, int]:
    """左列「共鸣商店」与「晶石兑换」同构：圆形图标在上、文字在下，点文字无效。

    11-06 22:22 实机：文字中心 (202,915) 点下去页面纹丝不动；上移约 1.1 个
    文字框高到 (202,870) 一次进入共鸣商店页（澎湃/静谧两页签出现）。
    两处共用同一套相对偏移，换分辨率自适应。
    """
    return _exchange_entry_point(entry, rect)


def _claim_pair_rewards(logger: logging.Logger, hwnd: int, stop_check=None) -> Tuple[int, str]:
    """奇想日历 → 奇迹之冠 → 搭配赛 → 有「领取奖励」就点掉并收尾奖励页。"""
    if _stop(stop_check):
        return 0, "用户停止"
    rect = _window_rect(hwnd)
    if not rect:
        return 0, "窗口不可用"
    items = _ocr_full(logger, hwnd)
    crown = _pick(items, (CROWN_ENTRY,), min_conf=0.5, region=_abs_region(rect, CALENDAR_CROWN_REGION))
    if not crown:
        _record_failure(logger, "奇迹之冠-入口", "奇想日历里未找到「奇迹之冠」", hwnd, items)
        return 0, "未找到「奇迹之冠」入口"
    _click(crown["center"])
    logger.info("[无限暖暖·奇想日历] 点击「奇迹之冠」@ %s", crown["center"])
    time.sleep(1.2)

    hub = _wait_for(
        logger,
        hwnd,
        lambda its: bool(_pick(its, CROWN_HUB_MARKERS, min_conf=0.5)),
        timeout=8.0,
        stop_check=stop_check,
    )
    if not hub:
        _record_failure(logger, "奇迹之冠-页面", "点击后未进入奇迹之冠", hwnd)
        return 0, "未进入奇迹之冠"
    # 点「奇迹之冠搭配赛」必须重试：2026-11-02 实锤，点击没生效（失败截图
    # 20261101_122627_NikkiLauncher_晶石兑换-入口.jpg 仍停在奇迹之冠 hub 页），
    # 后面「晶石兑换」左列入口自然找不到（那菜单只在搭配赛页才有）。
    stage = None
    for attempt in range(1, CROWN_PAIR_CLICK_ATTEMPTS + 1):
        if _stop(stop_check):
            return 0, "用户停止"
        hub_items = _ocr_full(logger, hwnd)
        pair = _pick(hub_items, (PAIR_ENTRY,), min_conf=0.5, region=_abs_region(rect, HUB_PAIR_REGION))
        if not pair:
            if attempt == 1:
                _record_failure(logger, "奇迹之冠搭配赛-入口", "奇迹之冠里未找到「奇迹之冠搭配赛」", hwnd, hub_items)
                return 0, "未找到「奇迹之冠搭配赛」"
            break
        target = _pair_click_point(pair, rect)
        _click(target)
        logger.info(
            "[无限暖暖·奇想日历] 点击「奇迹之冠搭配赛」多边形 @ %s"
            "（文字中心 %s，第 %s/%s 次）",
            target, pair["center"], attempt, CROWN_PAIR_CLICK_ATTEMPTS,
        )
        time.sleep(1.5)
        stage = _wait_for(logger, hwnd, _is_stage_page, timeout=12.0, stop_check=stop_check)
        if stage:
            break
        logger.warning(
            "[无限暖暖·奇想日历] 搭配赛页面未就绪，重试点击（第 %s/%s 次）",
            attempt, CROWN_PAIR_CLICK_ATTEMPTS,
        )
    if not stage:
        _record_failure(logger, "奇迹之冠搭配赛-页面", "搭配赛页面未就绪", hwnd)
        # 退不出 hub 就回不去主界面 → 共鸣商店会被整段跳过，先自己退出来
        _leave_crown_hub(logger, hwnd, stop_check=stop_check)
        return 0, "搭配赛页面未就绪"

    claimed = 0
    for _ in range(3):
        if _stop(stop_check):
            break
        items = _ocr_full(logger, hwnd)
        hit = _pick(items, CLAIM_TEXTS, min_conf=0.5)
        if not hit:
            break
        _click(hit["center"])
        claimed += 1
        logger.info("[无限暖暖·奇想日历] 点击「领取奖励」@ %s", hit["center"])
        time.sleep(1.2)
        _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=4)

    leftover = _pick(_ocr_full(logger, hwnd), CLAIM_TEXTS, min_conf=0.5)
    if leftover:
        _record_failure(logger, "奇迹之冠-领取奖励", "点击后「领取奖励」仍存在", hwnd)
        return claimed, (
            f"领奖 {claimed} 次（仍有领取按钮，已记录）" if claimed else "领取奖励未能点掉（已记录）"
        )
    return claimed, (f"领奖 {claimed} 次" if claimed else "无可领奖励")


def _buy_plan_items(
    logger: logging.Logger,
    hwnd: int,
    plan: Dict,
    stop_check,
    grid_frac,
    meta_key: str,
    label: str,
) -> str:
    """在已打开的商店页按清单逐件购买（点商品→确认→空白区域继续），返回摘要。

    到账只认「本月限购 N/M」递减或奖励页出现；部分买到但有硬失败/没买满（非售罄）
    时不写月度标记，留给下次重试；售罄导致的差额视为正常完成。
    """
    purchased = 0
    bought_labels: List[str] = []
    missing: List[str] = []
    failed: List[str] = []
    shortfall: List[str] = []
    stopped = False
    for item in plan["items"]:
        name = item["name"]
        qty = int(item.get("qty") or 1)
        bought = 0
        last: Optional[Dict] = None
        for _ in range(qty):
            if _stop(stop_check):
                stopped = True
                break
            out = _buy_one(logger, hwnd, name, stop_check=stop_check, grid_frac=grid_frac)
            state = out.get("state")
            if state == "purchased":
                bought += 1
                continue
            last = out
            break
        if bought:
            purchased += bought
            bought_labels.append(f"{name}×{bought}" if qty > 1 else name)
            if bought < qty:
                shortfall.append(f"{name} {bought}/{qty}（{(last or {}).get('state') or 'stopped'}）")
        else:
            state = (last or {}).get("state") or "failed"
            if state == "sold_out":
                missing.append(f"{name}(已买完)")
            elif state == "missing":
                missing.append(f"{name}(未找到)")
            else:
                failed.append(f"{name}({state})")
        logger.info("[无限暖暖·%s] %s → 买到 %s/%s", label, name, bought, qty)
        if stopped:
            break

    if stopped:
        logger.info("[无限暖暖·%s] 用户停止（已买 %s 件，不写月度标记）", label, purchased)
        return "用户停止（兑换未完成）"

    detail = ""
    if purchased:
        detail = f"已兑换 {purchased} 件：" + "、".join(bought_labels)
        if shortfall:
            detail += f"；未买满：{'、'.join(shortfall)}"
        if missing:
            detail += f"；未买到：{'、'.join(missing)}"
        if failed:
            detail += f"；失败：{'、'.join(failed)}"
        # 只有「库存不足（sold_out）」可以接受为完成；点了没反应/没确认到账要下轮重试，
        # 否则本月剩下的库存会被月度标记永久锁死。
        retry_needed = bool(failed) or any("sold_out" not in s for s in shortfall)
        if retry_needed:
            detail += "；未写月度标记（下次运行重试）"
            logger.warning("[无限暖暖·%s] %s", label, detail)
        else:
            _mark_monthly_done(logger, meta_key)
            logger.info("[无限暖暖·%s] %s", label, detail)
        return detail
    if missing and not failed and all("已买完" in m for m in missing):
        detail = "清单内商品本月均已换完：" + "、".join(missing)
        _mark_monthly_done(logger, meta_key)
        logger.info("[无限暖暖·%s] %s", label, detail)
        return detail
    detail = "未兑换到任何商品"
    if missing:
        detail += f"；未找到/已买完：{'、'.join(missing)}"
    if failed:
        detail += f"；失败：{'、'.join(failed)}"
    _record_failure(logger, f"{label}-购买", detail, hwnd)
    logger.warning("[无限暖暖·%s] %s", label, detail)
    return detail


def _exchange_crystals(logger: logging.Logger, hwnd: int, stop_check=None) -> str:
    plan = load_crystal_exchange_plan(logger)
    if not plan.get("enabled"):
        return "晶石兑换已关闭"
    if not plan["items"]:
        return "晶石兑换清单为空（待定）"
    if _monthly_done(logger, MONTHLY_META_KEY):
        logger.info("[无限暖暖·晶石兑换] 本月已兑换，跳过")
        return "本月晶石兑换已完成"
    if _stop(stop_check):
        return "用户停止"

    rect = _window_rect(hwnd)
    if not rect:
        return "窗口不可用，跳过晶石兑换"
    _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=2)
    # 左列「晶石兑换」入口要轮询几次：搭配赛页刚切出来时左列菜单还在渲染，
    # 一次 OCR 认不到就当"没有入口"，后面整段兑换都做不了。
    entry = None
    for attempt in range(1, 4):
        items = _ocr_full(logger, hwnd)
        entry = _pick(items, (EXCHANGE_TITLE,), min_conf=0.5, region=_abs_region(rect, STAGE_MENU_REGION))
        if entry:
            break
        if attempt < 3:
            time.sleep(1.2)
    if not entry:
        _record_failure(logger, "晶石兑换-入口", "搭配赛页未找到左侧「晶石兑换」", hwnd, items)
        return "未找到「晶石兑换」入口"
    point = _exchange_entry_point(entry, rect)
    _click(point)
    logger.info(
        "[无限暖暖·晶石兑换] 点击左侧「晶石兑换」圆形图标 @ %s（文字中心 %s 上方）",
        point, entry["center"],
    )
    time.sleep(1.2)
    page = _wait_for(logger, hwnd, _is_exchange_page, timeout=8.0, stop_check=stop_check)
    if not page:
        _record_failure(logger, "晶石兑换-页面", "未进入晶石兑换页", hwnd)
        return "未进入晶石兑换页"
    return _buy_plan_items(
        logger, hwnd, plan, stop_check, EXCHANGE_GRID_REGION, MONTHLY_META_KEY, "晶石兑换"
    )


def _is_shop_page(items: List[Dict]) -> bool:
    # 主界面也有「商城」但没有「购物车」，两个都命中才算进了商城
    return len(_markers_present(items, SHOP_MARKERS, 0.45)) >= 2


def _is_resonance_page(items: List[Dict]) -> bool:
    return len(_markers_present(items, RESONANCE_TABS, 0.45)) >= 2


def _has_serene_markers(items: List[Dict]) -> bool:
    return bool(_markers_present(items, SERENE_MARKERS, 0.5))


def _page_fingerprint(items: List[Dict]):
    """返回 (静谧页独占商品命中集合, 澎湃页独占商品命中集合)。"""
    return (_markers_present(items, SERENE_MARKERS, 0.5),
            _markers_present(items, SURGE_MARKERS, 0.5))


def _serene_page_guard(items: List[Dict]) -> bool:
    """**双向闸**：静谧页独占商品必须出现，澎湃页独占商品必须一个都不出现。

    只看 `_has_serene_markers` 不够——切页动画中间帧可能同时残留两页商品，
    且两页都有同名的「启示水晶/共鸣水晶/无敌碎星闪粉」。保守起见两者都命中时
    判 False（宁可不买，也不能花错货币）。对齐 `monthly_exchange` 的
    `tab_content_markers` + `forbidden_texts` 口径。
    """
    serene, surge = _page_fingerprint(items)
    return bool(serene) and not surge


def _drag_menu_up(logger: logging.Logger, hwnd: int, rect, times: int = 1) -> None:
    """商城左列**从下往上一次大位移拖动**，把底部常规分类推上来。

    滚轮（`pyautogui.scroll`）和拖滚动条对这个 UE5 列表都无效（11-06 实机）。
    `mouseDown` 后只发**一个** `moveTo` 到终点：分 30 小步时游戏只响应第一个
    位移，其余全被吞，效率极低。`finally` 保证异常时也松开左键。
    """
    import pyautogui

    from .foreground_flow_runner import FailSafeAborted

    left, top, width, height = rect
    fx, fy0, fy1 = SHOP_MENU_DRAG
    x = left + int(width * fx)
    y0 = top + int(height * fy0)
    y1 = top + int(height * fy1)
    _focus(hwnd)
    for _ in range(max(1, int(times))):
        try:
            pyautogui.moveTo(x, y0)
            time.sleep(0.2)
            pyautogui.mouseDown()
            try:
                time.sleep(0.15)
                pyautogui.moveTo(x, y1)
                time.sleep(0.3)
            finally:
                pyautogui.mouseUp()
        except pyautogui.FailSafeException as exc:
            raise FailSafeAborted(
                "PyAutoGUI 紧急停止已触发：鼠标位于屏幕左上角 (0,0)。"
            ) from exc
        time.sleep(0.6)
    logger.info("[无限暖暖·共鸣商店] 左列上拖 %s 次 @ x=%s y %s→%s",
                times, x, y0, y1)


def _open_shop(logger: logging.Logger, hwnd: int, stop_check=None) -> bool:
    """主界面 → 商城：**就按 H，三次**。H 是顶栏「商城」图标的官方快捷键。

    11-01～11-05「已进入商城」0 次。11-04 08:52 按 H 三次，OCR 恒定 17 区域、
    失败截图仍是主世界 3D HUD；同一次运行里 08:49 两次 `SetForegroundWindow
    失败`、08:50:40 游戏窗口「已最小化或已关闭，还原失败」——但这两条都在
    按 H 之前且对象/时点错配，**不能据此断定 H 发给了别的窗口**（23:50 更正）。
    23:28 起不再绕鼠标（主世界光标被 UE5 锁在中心），改由 `_focus_and_press`
    每轮先抢前台再发带扫描码的 H；是否真因待明早全屏快照 + 前台窗口记录定案。
    """
    for attempt in range(1, 4):
        if _stop(stop_check):
            return False
        _focus_and_press(hwnd, "h")
        logger.info("[无限暖暖·共鸣商店] 按 H 打开商城（第 %s/3 次）", attempt)
        items = _wait_for(logger, hwnd, _is_shop_page, timeout=6.0, stop_check=stop_check)
        if items:
            logger.info("[无限暖暖·共鸣商店] 已进入商城")
            return True
        _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=2)
    _record_key_no_response(logger, "h", hwnd)
    return False


def _find_resonance_entry(logger: logging.Logger, hwnd: int, stop_check=None) -> Optional[Dict]:
    """左列**从下往上一次大位移拖动**找「共鸣商店」，每拖一次复扫 OCR。

    旧实现用滚轮向下/向上各 10 次（`_scroll_at`），11-06 实机证明对 UE5 这个
    列表完全无效：三轮共 13 次滚轮，左列 OCR 始终是「越冬之约…美鸭梨优选」。
    11-01 留存图 `ref_nikki_shop_menu_scrolled_20261101.png` 显示滚动后左列从
    「美鸭梨优选」开始、共鸣商店在倒数第二项 —— 它就在底下，拖出来即可。
    """
    rect = _window_rect(hwnd)
    if not rect:
        return None
    region = _abs_region(rect, SHOP_MENU_REGION)
    for attempt in range(SHOP_MENU_DRAG_TIMES + 1):
        if _stop(stop_check):
            return None
        items = _ocr_full(logger, hwnd)
        entry = _pick(items, (RESONANCE_ENTRY,), min_conf=0.5, region=region)
        if entry:
            logger.info("[无限暖暖·共鸣商店] 左列找到「共鸣商店」@ %s（上拖 %s 次）",
                        entry["center"], attempt)
            return entry
        if attempt == SHOP_MENU_DRAG_TIMES:
            break
        _drag_menu_up(logger, hwnd, rect)
    return None


def _select_serene_tab(logger: logging.Logger, hwnd: int, stop_check=None) -> bool:
    """必须切到「静谧水滴兑换」：用**双向商品指纹**确认，而不是只看页签名。

    两页都有同名的启示/共鸣水晶与无敌碎星闪粉，只是货币不同；因此除了要求
    静谧页独占商品（万象星实/棱镜/噗灵等）出现，还要求澎湃页独占商品
    （日光果/星光果/花露等）一个都不出现。切页动画的中间帧两页内容可能
    同时残留，此时判 False，宁可重试也不买。
    """
    if _stop(stop_check):
        return False
    first = _ocr_full(logger, hwnd)
    if _serene_page_guard(first):
        logger.info("[无限暖暖·共鸣商店] 已在「静谧水滴兑换」：%s",
                    sorted(_page_fingerprint(first)[0]))
        return True
    surge = _page_fingerprint(first)[1]
    if surge:
        logger.warning("[无限暖暖·共鸣商店] 当前仍是「澎湃浪花兑换」（命中 %s），"
                       "该页水晶单价 20 不是静谧水滴 40，禁止购买", sorted(surge))
    rect = _window_rect(hwnd)
    if not rect:
        return False
    region = _abs_region(rect, RESONANCE_TAB_REGION)
    for _ in range(2):
        if _stop(stop_check):
            return False
        items = _ocr_full(logger, hwnd)
        tab = _pick(items, (RESONANCE_TAB_TARGET,), min_conf=0.5, region=region)
        if not tab:
            _record_failure(logger, "共鸣商店-页签", "未找到「静谧水滴兑换」页签", hwnd, items)
            return False
        _click(tab["center"])
        logger.info("[无限暖暖·共鸣商店] 点击「静谧水滴兑换」@ %s", tab["center"])
        time.sleep(1.2)
        page = _wait_for(logger, hwnd, _serene_page_guard, timeout=5.0, stop_check=stop_check)
        if page:
            logger.info("[无限暖暖·共鸣商店] 已确认静谧水滴页：静谧独占=%s，澎湃独占=无",
                        sorted(_page_fingerprint(page)[0]))
            return True
    last = _ocr_full(logger, hwnd)
    logger.error("[无限暖暖·共鸣商店] 点击后仍未切到静谧水滴页：静谧独占=%s 澎湃独占=%s",
                 sorted(_page_fingerprint(last)[0]), sorted(_page_fingerprint(last)[1]))
    _record_failure(logger, "共鸣商店-页签", "点击后仍未切到「静谧水滴兑换」", hwnd)
    return False


def _run_resonance_shop(logger: logging.Logger, hwnd: int, stop_check=None) -> str:
    """主界面（H 或顶栏商城图标）→ 商城 → 左列滚动找共鸣商店 → 静谧水滴兑换 → 换抽卡道具（每月）。"""
    plan = load_resonance_shop_plan(logger)
    if not plan.get("enabled"):
        return "共鸣商店已关闭"
    if not plan["items"]:
        return "共鸣商店清单为空（待定）"
    if _monthly_done(logger, RESONANCE_META_KEY):
        logger.info("[无限暖暖·共鸣商店] 本月已兑换，跳过")
        return "本月共鸣商店已完成"
    if _stop(stop_check):
        return "用户停止"

    if not _open_shop(logger, hwnd, stop_check=stop_check):
        _record_failure(logger, "共鸣商店-商城", "按 H / 点顶栏商城图标后仍未进入商城", hwnd)
        return "商城未打开（按 H 与顶栏图标均无效，已记录）"
    entry = _find_resonance_entry(logger, hwnd, stop_check=stop_check)
    if not entry:
        _record_failure(logger, "共鸣商店-入口", "左列滚动后未找到「共鸣商店」", hwnd)
        return "未找到「共鸣商店」入口"
    rect = _window_rect(hwnd)
    point = _resonance_entry_point(entry, rect) if rect else tuple(entry["center"])
    _click(point)
    logger.info("[无限暖暖·共鸣商店] 点击「共鸣商店」圆形图标 @ %s（文字中心 %s 上方）",
                point, entry["center"])
    time.sleep(1.2)
    page = _wait_for(logger, hwnd, _is_resonance_page, timeout=8.0, stop_check=stop_check)
    if not page:
        _record_failure(logger, "共鸣商店-页面", "未进入共鸣商店", hwnd)
        return "未进入共鸣商店"
    if not _select_serene_tab(logger, hwnd, stop_check=stop_check):
        return "未确认「静谧水滴兑换」页签，未购买"
    # 二次复核（对齐绝区零/原神 forbidden_texts）：切页成功到点第一件商品之间
    # 可能还有切页动画、浮层或上一屏残留，再取一次 OCR 走同一双向闸。
    recheck = _ocr_full(logger, hwnd)
    serene, surge = _page_fingerprint(recheck)
    if not _serene_page_guard(recheck):
        logger.error("[无限暖暖·共鸣商店] 购买前货币复核未通过：静谧独占=%s 澎湃独占=%s，"
                     "拒绝购买", sorted(serene), sorted(surge))
        _record_failure(logger, "共鸣商店-货币复核",
                        f"购买前页签复核未通过：静谧独占={sorted(serene)} "
                        f"澎湃独占={sorted(surge)}", hwnd, recheck)
        return "购买前货币复核未通过（未购买）"
    logger.info("[无限暖暖·共鸣商店] 购买前货币复核通过：静谧独占=%s",
                sorted(serene))
    return _buy_plan_items(
        logger, hwnd, plan, stop_check, SHOP_GRID_REGION, RESONANCE_META_KEY, "共鸣商店"
    )


def _return_to_main(logger: logging.Logger, hwnd: int, scales, confidences, stop_check=None) -> bool:
    for attempt in range(1, 6):
        if _stop(stop_check):
            return False
        _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=2)
        if _has_main(hwnd, scales, confidences):
            logger.info("[无限暖暖·奇想日历] 已回到主界面")
            return True
        _focus(hwnd)
        _press_esc()
        logger.info("[无限暖暖·奇想日历] 按 ESC 返回主界面（第 %s/5 次）", attempt)
        time.sleep(1.2)
    return _has_main(hwnd, scales, confidences)


def _close_game_for_whimbox_restart(logger: logging.Logger) -> None:
    """交接兜底：关闭游戏，让 whimbox 重新启动并进入主界面。

    用户 2026-11-01 叮嘱：后续接奇想盒时必须保证游戏在主界面；保证不了就关掉游戏，
    让奇想盒按自己的启动流程重新进。只关游戏本体，不动启动器。
    """
    try:
        from .foreground_flow_runner import _find_process_by_exact_name, close_flow_processes
        from .nikki_launcher import GAME_PROCESS

        close_flow_processes(logger, "NikkiLauncher", None, [GAME_PROCESS])
        # 等进程真正退出，避免 whimbox 启动时撞上正在退出的旧窗口
        for _ in range(20):
            if not _find_process_by_exact_name(GAME_PROCESS):
                break
            time.sleep(0.5)
        logger.warning(
            "[无限暖暖·交接] 未能确认回到主界面，已关闭游戏 %s，交给 whimbox 重新进入",
            GAME_PROCESS,
        )
    except Exception as exc:  # noqa: BLE001 - 关游戏失败只能记录，后续仍交给 whimbox
        logger.warning("[无限暖暖·交接] 关闭游戏失败: %s", exc)


def _back_to_main_for_shop(logger, hwnd, scales, confidences, stop_check=None) -> bool:
    """回主界面（共鸣商店要从主界面按 H 进商城）。

    ESC 回不去时，先试着退出奇迹之冠 hub（全屏 overlay，ESC 关不掉）再回一次：
    2026-11-02 就是卡在 hub 上 → 回不去主界面 → 共鸣商店整段被跳过，
    晶石兑换和共鸣商店两个每月清单一件都没买。
    """
    if _return_to_main(logger, hwnd, scales, confidences, stop_check=stop_check):
        return True
    if _leave_crown_hub(logger, hwnd, stop_check=stop_check):
        if _return_to_main(logger, hwnd, scales, confidences, stop_check=stop_check):
            return True
    return False


def _ensure_main_or_close(logger: logging.Logger, hwnd: int, scales, confidences, stop_check=None) -> str:
    """交接前检查：主界面 → main；回不去 → 关游戏 → closed；用户停止 → stopped。"""
    if _stop(stop_check):
        return "stopped"
    if _return_to_main(logger, hwnd, scales, confidences, stop_check=stop_check):
        return "main"
    _record_failure(logger, "奇想日历-交接", "未能确认回到主界面，已关闭游戏交给 whimbox 重新进入", hwnd)
    _close_game_for_whimbox_restart(logger)
    return "closed"


def _run_flow_inner(
    logger: logging.Logger,
    hwnd: int,
    scales,
    confidences,
    stop_check=None,
) -> Tuple[bool, str]:
    if _stop(stop_check):
        return False, "用户请求停止"
    if not hwnd or not _window_rect(hwnd):
        if _stop(stop_check):
            return False, "用户请求停止"
        _close_game_for_whimbox_restart(logger)
        logger.warning("[无限暖暖·奇想日历] 游戏窗口不可用，已关闭游戏交给 whimbox 重新进入")
        return True, "游戏窗口不可用，已关闭游戏交给 whimbox 重新进入"

    _focus(hwnd)
    _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=2)
    if not _has_main(hwnd, scales, confidences):
        time.sleep(1.5)
    if not _has_main(hwnd, scales, confidences):
        message = "未识别到暖暖主界面，跳过奇想日历"
        _record_failure(logger, "奇想日历-入口", message, hwnd)
        if _stop(stop_check):
            return False, "用户请求停止"
        # 交接规则：保证不了在主界面就关游戏，让 whimbox 自己重新进
        _close_game_for_whimbox_restart(logger)
        message += "（已关闭游戏交给 whimbox 重新进入）"
        logger.warning("[无限暖暖·奇想日历] %s", message)
        return True, message

    if not _open_calendar(logger, hwnd, stop_check=stop_check):
        _record_failure(logger, "奇想日历-打开", "按 L 后未出现奇想日历", hwnd)
        _ensure_main_or_close(logger, hwnd, scales, confidences, stop_check=stop_check)
        # 2026-11-05：以前返回 True「已记录，继续后续任务」，日报看起来像前置
        # 流程成功了，实际 _exchange_crystals / _run_resonance_shop 整段没跑，
        # task_meta 里本月两个 nikki 商店标记一条都没有。这里保留「不阻断
        # whimbox 日常」的容错（prelaunch 返回 False 会让整个暖暖任务失败），
        # 但必须把「商店未执行」写进报告，不能冒充成功。
        message = "奇想日历未打开：本月晶石兑换/共鸣商店未执行（不阻断 whimbox 日常）"
        logger.error("[无限暖暖·奇想日历] %s", message)
        return True, message

    notes: List[str] = []
    try:
        _, claim_note = _claim_pair_rewards(logger, hwnd, stop_check=stop_check)
        notes.append(claim_note)
        notes.append(_exchange_crystals(logger, hwnd, stop_check=stop_check))
        if _back_to_main_for_shop(logger, hwnd, scales, confidences, stop_check=stop_check):
            notes.append(_run_resonance_shop(logger, hwnd, stop_check=stop_check))
        else:
            notes.append("未回到主界面，跳过共鸣商店")
    finally:
        handoff = _ensure_main_or_close(logger, hwnd, scales, confidences, stop_check=stop_check)
        if handoff == "closed":
            notes.append("未确认回到主界面，已关闭游戏交给 whimbox 重新进入")
        elif handoff == "stopped":
            notes.append("用户停止（未做交接检查）")
    return True, "；".join(n for n in notes if n)


def run_nikki_calendar_flow(
    logger: logging.Logger,
    hwnd: int,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """主入口（action=nikki_calendar）：返回 (是否继续, 摘要)。

    除用户停止/紧急停止外恒返回 True：领奖或兑换失败只记录，不阻断后续 whimbox。
    """
    from .foreground_flow_runner import FailSafeAborted

    try:
        return _run_flow_inner(logger, hwnd, scales, confidences, stop_check=stop_check)
    except FailSafeAborted:
        raise
    except Exception as exc:  # noqa: BLE001 - 流程异常不能让整个暖暖任务不跑
        logger.error("[无限暖暖·奇想日历] 流程异常（已记录，继续后续任务）: %s", exc, exc_info=True)
        _record_failure(logger, "奇想日历-流程", f"流程异常: {exc}", hwnd)
        try:
            _ensure_main_or_close(logger, hwnd, scales, confidences, stop_check=None)
        except FailSafeAborted:
            raise
        except Exception as back_exc:  # noqa: BLE001 - 收尾失败只记录
            logger.debug("[无限暖暖·奇想日历] 异常后回主界面失败: %s", back_exc)
        return True, f"奇想日历流程异常（已记录，继续后续任务）: {exc}"
