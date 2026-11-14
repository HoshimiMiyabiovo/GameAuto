# -*- coding: utf-8 -*-
"""白荆回廊·场景识别与恢复（2026-11-14 自 baijing_daily.py 拆出）。

职责：把"卡在任意界面"恢复回主界面/任务面板——SCENE_RULES 场景登记表
（特征词 → 专属退出流程 _scene_exit_*）+ recover_scene 主入口 +
_ensure_main_ui（检测→消遮挡→关闭→ESC 循环）。

数据流：上层（tasklist/handlers/echo/daily）通过 recover_scene / _ensure_main_ui
调用；本模块依赖 baijing_common（OCR/点击原语）与 baijing_flow
（detect_baijing_main_ui/_focus_game_window），**禁止**依赖 tasklist/handlers/
echo/daily（会成环——"回任务面板"的 _return_to_task_panel 归 tasklist 就是为此）。

不变量与坑：
- 新场景必须先实机取证再登记（_record_unknown_scene 落 flow_failures）；
  未登记场景 recover 只能报"未匹配"交上层自保，禁盲点。
- 场景特征词与任务 handler 共享的常量（TEA_SUMMARY_MARKERS 等）**以本模块为
  唯一定义点**，handler 从这里 import。
"""
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import pyautogui

from ..settings.paths import TEMPLATES_DIR
from .baijing_common import (
    MAX_CLOSE_ATTEMPTS,
    SCENE_OCR_MIN_CONF,
    TASK_LIST_CORE_MARKERS,
    _abs_region,
    _click_ratio,
    _dismiss_overlay,
    _ensure_window,
    _ocr_best_match,
    _pick_result,
    _record_step_failure,
    _scene_results,
    _stopped,
    _template_center,
)
from .foreground_flow_runner import _pyautogui_click_guarded
from .baijing_flow import _focus_game_window, detect_baijing_main_ui

TEA_SUMMARY_MARKERS = ("茶憩结束", "点击退出")           # 结束摘要

TEA_SUMMARY_CLICK = (0.50, 0.60)         # 摘要区域（点击退出）

# ── 烙痕详情页识别三件套（scene 登记表与 memory handler 共用，唯一定义点在此，
#    handler 从这里 import；2026-11-14 拆分时上收到 scene 避免 handlers↔scene 成环） ──
# 详情页「强化烙痕」按钮模板（底部右侧；注意是"烙痕"不是"熔垣"，
# 2026-10-18 用户指正：低分辨率下图样字容易看错。OCR 对这几个字识别也差，用模板）
MEMORY_ENHANCE_BUTTON_TPL = str(TEMPLATES_DIR / "白荆回廊-强化烙痕按钮.png")

# 详情页「强化烙痕」按钮的 OCR 兜底区域（右下角）。模板失配时用文字确认，
# OCR 常把「烙痕」认成「熔垣」，两个词都收。
MEMORY_ENHANCE_BTN_REGION = (0.55, 0.75, 1.00, 1.00)

MEMORY_ENHANCE_TEXTS = ("强化烙痕", "强化熔垣")



# ── 休息室 / 关注特写：ESC 无效的场景态（2026-10-26 实锤修复） ──────────
# 12:50 那轮茶憩掉进休息室后，代码点了左下角色头像 → 进入「关注」特写（画面只剩
# 莫红袖 + 右上「退出关注」），场景/摘要都等不到；随后 _return_to_task_panel 连按
# 5 次 ESC 也出不来（这两个界面都不吃 ESC），任务列表反复重开失败，整轮日常只剩
# 2/16、活跃度 20/100，俱乐部/零售/叶脉联结全部被拖死。
# 用户 12:53 截图给出正确操作：茶憩入口是右下「午后茶憩」立牌卡，离开要点左上
# 「返回」再在确认框点「确认」。坐标按该截图（960x540）实测换算。
REST_ROOM_MARKERS = ("拜访他人", "布置模式")     # 休息室独有；「午后茶憩」任务行里也有，不能当特征


REST_ROOM_BACK = (0.101, 0.048)                  # 左上「返回」文字中心 (97,26)


REST_ROOM_BACK_REGION = (0.0, 0.0, 0.20, 0.15)   # 「返回」搜索区（左上角，避开其它文字）


REST_ROOM_LEAVE_CONFIRM = (0.623, 0.634)         # 「将离开休息室」确认框「确认」（与茶憩确认框同布局）


FOCUS_MODE_MARKERS = ("退出关注",)               # 角色特写镜头，右上「退出关注」


FOCUS_MODE_REGION = (0.70, 0.0, 1.0, 0.25)       # 「退出关注」在右上角



# ── 场景登记表补充（2026-10-27，失败取证驱动）─────────────────────────
# 烙痕详情页：记忆烙痕经验任务的强化画面。10-27 10:18 取证实锤：+1/确认都点中
# 了（画面出现"等级提升"），但页面不在登记表里 → 恢复认不出 → 连环拖死茶憩/
# 叶脉联结。特征取详情页独占的页签文字。
# 2026-10-29 20:03 取证帧（强化烙痕·技能同调页签）没有「特质提升/烙痕等级」，
# 只有页签「基础属性｜漫巡属性｜漫巡技能｜烙痕唤醒｜技能同调」→ 补两个独占页签。
# 「基础属性」「技能同调」比较通用，不用；「漫巡属性」「烙痕唤醒」只在详情页出现。
MEMORY_DETAIL_MARKERS = ("特质提升", "烙痕等级", "漫巡属性", "烙痕唤醒")


# 叶脉联结计划升级的整屏动画：2026-10-29 20:12 取证帧只有「16｜计划等级提升」，
# 点击屏幕任意位置继续，不处理会一直挡着下面的页面。
LEVEL_UP_MARKERS = ("计划等级提升",)


# 烙痕列表页：2026-10-30 10:25 取证（记忆烙痕任务从详情返回后卡在这里）。
# 独占特征取顶部工具栏；左上「返回」回任务面板。
MEMORY_LIST_MARKERS = ("前往技能列表", "只看红点", "排序与筛选")


# 定巡之礼活动弹窗：2026-10-30 10:35 取证（流程漂进活动页认不出）。
# 没有「返回」，右上角有红色 X；坐标按 1919x1079 截图换算。
# 2026-11-06 09:15 再取证（flow_failures #197，恢复途中加载完弹出的同款弹窗）：
# 第 2 天「可领取」@(630,762) conf 0.75，直接 X 等于丢奖——2026-11-07 用户拍板
# 「遇到就给他领了呗」：先领再关。
EVENT_DIALOG_MARKERS = ("定巡之礼",)


EVENT_DIALOG_CLOSE = (0.913, 0.239)


EVENT_DIALOG_CLAIM_MARKERS = ("可领取",)   # 只在规则命中定巡之礼后查找，不会误伤其它页面


# 活动公告横幅（2026-11-04 #163 取证：生死轮回×叶脉联结计划，加载完弹出；
# 用户 2026-11-07 拍板「他只是个活动公告，点一下就好了」）。特征只取公告独有
# 的关闭提示/勾选文案——「叶脉联结计划」绝不能当特征：叶脉联结页面上就有这
# 五个字（10-20 曾实锤误命中），拿它当特征会在领叶脉奖励时误点空白。
# 公告是模态的「点击空白区域关闭」：点左边缘空白（11-04 帧 x<340/1920 无
# 任何 UI），不碰横幅本体（正文/「前往获取」点下去会进活动页）。
EVENT_ANNOUNCE_MARKERS = ("点击空白区域关闭", "本期不再显示")


EVENT_ANNOUNCE_BLANK = (0.10, 0.50)   # 11-04 帧实测：横幅左界 ≈ x0.18，左边缘是空白


# 加载过场（10-31/11-01/11-03/11-04/11-06 五次「未匹配任何已知场景」失败全是它，
# #120/#129/#153/#163/#197）：提示文字每日轮换（刻印属性/漫巡中/真实伤害…）、
# 进度 99%/%86 不稳，都不能当特征。只等待不点击——加载中点哪都无效。
# 2026-11-10 10:00:32 实锤：只用拉丁 logo「ASHECHOES」当特征当天 **0 次命中**。
# 生产 onnxocr v6 把中央 logo 认成「白荆廊」(0.85)/「白荆回廊」(0.71)/「白荆回廓」，
# 拉丁小字整帧被读成「24919904」，同帧只有 47%→98% 和轮换提示。故把中文 logo
# 的三种实测 OCR 变体全部登记，拉丁词保留给能认出来的帧。**别用泛化短词「白荆」**：
# 固定页面上若常驻这俩字，加载规则会一直命中，_scene_exit_loading 白等满 60s；
# panel/main 在 _match_scene_rule 之前先短路，但仍只挑加载页独占的中央大字。
LOADING_SCREEN_MARKERS = ("ASHECHOES", "ASH ECHOES", "白荆廊", "白荆回廊", "白荆回廓")


LOADING_WAIT_TIMEOUT_SEC = 60.0


# 茶憩 3D 房间：任务流程用「茶憩纪念函」当进场特征，但 10-27 10:22 的当帧
# OCR 只认出房间常驻陈设（飞镖/棒球），纪念函没进识别结果——登记表用并集兜底，
# 任意命中即判茶憩房间（话题名"古典余温"每日轮换，不能当特征）。
TEA_ROOM_REGISTRY_MARKERS = ("茶憩纪念函", "飞镖", "棒球")


# 有猫零售页：10-26 20:10 取证，流程漂进零售页后认不出（当帧：返回｜有猫推荐｜
# 精选礼包｜有猫精选｜每日外勤赠礼）。左侧栏目文字是该页独占，主界面没有。
CAT_RETAIL_MARKERS = ("有猫精选", "精选礼包", "有猫推荐")




def _on_memory_detail_page(logger: logging.Logger, hwnd) -> bool:
    """是否处于烙痕详情页：右下「强化烙痕」模板优先，OCR 文字兜底。"""
    if (
        _template_center(logger, hwnd, MEMORY_ENHANCE_BUTTON_TPL, threshold=0.85)
        is not None
    ):
        return True
    hit = _ocr_best_match(
        logger,
        MEMORY_ENHANCE_TEXTS,
        hwnd,
        frac_region=MEMORY_ENHANCE_BTN_REGION,
        min_conf=0.55,
    )
    if hit:
        logger.info(
            f"[白荆回廊·日常] 烙痕详情页 OCR 命中 {hit['text']!r} @ {hit['center']}"
        )
        return True
    return False




def _in_rest_room(logger: logging.Logger, hwnd) -> bool:
    """是否停在休息室（茶憩所在场景）。

    特征只用「拜访他人 / 布置模式」——这两个词是休息室独有；「午后茶憩」在
    日常任务行里也出现，拿它判定会把任务列表误认成休息室。
    界面恢复统一走下方 `recover_scene`（场景登记表），这里只给茶憩流程做即时判定。
    """
    return _ocr_best_match(logger, REST_ROOM_MARKERS, hwnd, min_conf=0.55) is not None




# ── 场景退出登记表（2026-10-26）──────────────────────────────────────
# 为什么要登记表：以前"认不出页面"只能盲按 ESC / 点关闭，遇到 ESC 无效的界面
# （休息室、关注特写）就是今天 12:50 那出——连按 5 次 ESC 空转，整轮日常只剩 2/16。
# 现在每轮只取一次 OCR 结果，按登记表匹配：认出是哪个界面就跑它的专属退出；
# 一个都匹配不上（或反复退不出去）就把当帧截图 + 文字落进 flow_failures，
# 下次照证据加一行登记，而不是靠猜。
SCENE_RECOVERY_ROUNDS = 4          # 一次恢复最多处理几轮


SCENE_UNKNOWN_ROUNDS = 2           # 连续几轮匹配不上 → 落证据并交回兜底


_SCENE_LOGGED: Dict[str, float] = {}        # 证据节流：context → 上次记录时间


_SCENE_LOG_INTERVAL_SEC = 600.0




def _scene_exit_overlay(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """过场/结算遮罩：先点掉「点击屏幕继续」，否则底下的按钮全都点不到。"""
    hit = _pick_result(logger, hwnd, results, ("点击屏幕继续",), min_conf=0.5)
    if not hit:
        return False
    _pyautogui_click_guarded(*hit["center"])
    logger.info(f"[白荆回廊·日常] 场景退出·过场遮罩：点「点击屏幕继续」@ {hit['center']}")
    time.sleep(1.2)
    return True




def _scene_exit_level_up(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """计划等级提升整屏动画：点屏幕中心继续（多级连升时恢复循环会再点一次）。"""
    hit = _pick_result(logger, hwnd, results, LEVEL_UP_MARKERS, min_conf=0.55)
    if not hit:
        return False
    _click_ratio(logger, hwnd, (0.5, 0.5), "计划等级提升（点击继续）")
    logger.info(f"[白荆回廊·日常] 场景退出·计划等级提升：点击继续（命中 {hit['text']!r}）")
    time.sleep(1.2)
    return True




def _scene_exit_tea_summary(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """茶憩结束摘要：随意点关掉它（挂着会挡住休息室的返回按钮）。"""
    hit = _pick_result(logger, hwnd, results, TEA_SUMMARY_MARKERS, min_conf=0.55)
    if not hit:
        return False
    _click_ratio(logger, hwnd, TEA_SUMMARY_CLICK, "茶憩摘要（点击退出）")
    logger.info(f"[白荆回廊·日常] 场景退出·茶憩摘要：关闭（命中 {hit['text']!r} @ {hit['center']}）")
    time.sleep(1.5)
    return True




def _scene_exit_focus(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """关注特写：点右上「退出关注」回休息室全景（特写镜头下没有左上「返回」）。"""
    hit = _pick_result(
        logger, hwnd, results, FOCUS_MODE_MARKERS,
        frac_region=FOCUS_MODE_REGION, min_conf=0.55,
    )
    if not hit:
        return False
    _pyautogui_click_guarded(*hit["center"])
    logger.info(f"[白荆回廊·日常] 场景退出·关注特写：点「退出关注」@ {hit['center']}")
    time.sleep(1.2)
    return True




def _scene_exit_rest_room(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """休息室：左上「返回」→ 退出确认框「确认」。这个界面吃不到 ESC，只能点按钮。"""
    back = _pick_result(
        logger, hwnd, results, ("返回",), frac_region=REST_ROOM_BACK_REGION, min_conf=0.5
    )
    if back:
        _pyautogui_click_guarded(*back["center"])
        logger.info(f"[白荆回廊·日常] 场景退出·休息室：点左上「返回」@ {back['center']}")
    else:
        _click_ratio(logger, hwnd, REST_ROOM_BACK, "休息室左上「返回」（比例兜底）")
    time.sleep(1.5)
    # 确认框是点了返回之后才弹出来的，必须重新识别一轮
    fresh = _scene_results(logger, hwnd)
    confirm = _pick_result(logger, hwnd, fresh, ("确认", "确定"), min_conf=0.6)
    if confirm:
        _pyautogui_click_guarded(*confirm["center"])
        logger.info(
            f"[白荆回廊·日常] 场景退出·休息室：确认框「{confirm['text']}」@ {confirm['center']}"
        )
    else:
        _click_ratio(logger, hwnd, REST_ROOM_LEAVE_CONFIRM, "休息室退出确认（比例兜底）")
    time.sleep(1.5)
    return True




def _exit_via_top_left_back(logger: logging.Logger, hwnd, results, scene_name: str) -> bool:
    """通用「左上返回」退出：OCR 找返回按钮优先，找不到按实测比例点兜底。

    适用于烙痕详情 / 有猫零售这类"返回键就在左上角、无确认框"的普通页面。
    """
    back = _pick_result(
        logger, hwnd, results, ("返回",), frac_region=REST_ROOM_BACK_REGION, min_conf=0.5
    )
    if back:
        _pyautogui_click_guarded(*back["center"])
        logger.info(f"[白荆回廊·日常] 场景退出·{scene_name}：点左上「返回」@ {back['center']}")
    else:
        _click_ratio(logger, hwnd, REST_ROOM_BACK, f"{scene_name}左上「返回」（比例兜底）")
    time.sleep(1.5)
    return True




def _scene_exit_memory_detail(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """烙痕详情页：点「返回」回烙痕列表；列表页也有登记，下一轮会继续退回任务面板。"""
    return _exit_via_top_left_back(logger, hwnd, results, "烙痕详情")




def _scene_exit_memory_list(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """烙痕列表页：点左上「返回」回任务面板。"""
    return _exit_via_top_left_back(logger, hwnd, results, "烙痕列表")




def _scene_exit_loading(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """加载过场：只等待不点击（点了也无效），轮询到特征消失（加载完）为止。

    以前加载页会烧光恢复轮次被判 unknown（10-31～11-06 五次失败实锤），加载
    完才弹出的真页面（如定巡之礼弹窗）从来没轮到被处理。等待有 60s 上限，
    游戏真卡死时返回 False 交回兜底落证据，不会无限等。
    """
    deadline = time.time() + LOADING_WAIT_TIMEOUT_SEC
    while time.time() < deadline:
        if _stopped(stop_check):
            return False
        time.sleep(1.0)
        if _pick_result(
            logger, hwnd, _scene_results(logger, hwnd),
            LOADING_SCREEN_MARKERS, min_conf=0.5,
        ) is None:
            logger.info("[白荆回廊·日常] 场景退出·加载过场：加载完成，交回下一轮识别")
            return True
    logger.warning(
        f"[白荆回廊·日常] 场景退出·加载过场：等待 {LOADING_WAIT_TIMEOUT_SEC:.0f}s 仍在加载，交回兜底"
    )
    return False




def _scene_exit_event_dialog(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """定巡之礼等活动弹窗：先领「可领取」的签到奖励，再点右上角 X 关闭。

    2026-11-06 09:15 实拍帧第 2 天「可领取」，直接 X 等于丢奖（2026-11-07
    用户拍板「遇到就给他领了呗」）。领取后弹窗状态会变（可能出获得奖励遮罩）
    ——当帧里弹窗标记已不在时不盲点 X，交回下一轮恢复识别（遮罩/弹窗各有
    登记规则接力）。
    """
    hit = _pick_result(logger, hwnd, results, EVENT_DIALOG_MARKERS, min_conf=0.55)
    if not hit:
        return False
    claimed = 0
    for _ in range(3):
        claim = _pick_result(logger, hwnd, results, EVENT_DIALOG_CLAIM_MARKERS, min_conf=0.5)
        if not claim:
            break
        _pyautogui_click_guarded(*claim["center"])
        claimed += 1
        logger.info(
            f"[白荆回廊·日常] 场景退出·定巡之礼：领取「{claim.get('text')}」@ {claim['center']}"
        )
        time.sleep(1.5)
        results = _scene_results(logger, hwnd)  # 领取后弹窗状态会变，必须重识别
    if _pick_result(logger, hwnd, results, EVENT_DIALOG_MARKERS, min_conf=0.55):
        _click_ratio(logger, hwnd, EVENT_DIALOG_CLOSE, "定巡之礼右上关闭")
        logger.info(
            f"[白荆回廊·日常] 场景退出·定巡之礼：关闭弹窗（命中 {hit['text']!r}，领取 {claimed} 项）"
        )
    else:
        logger.info(
            f"[白荆回廊·日常] 场景退出·定巡之礼：领取 {claimed} 项后弹窗已不在，交回下一轮识别"
        )
    time.sleep(1.2)
    return True




def _scene_exit_event_announce(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """活动公告横幅：点空白区域关闭（公告自己的提示就是这么写的）。

    2026-11-07 用户拍板「他只是个活动公告，点一下就好了」。特征用公告独有的
    「点击空白区域关闭/本期不再显示」，绝不用「叶脉联结计划」（叶脉页同名，
    见 EVENT_ANNOUNCE_MARKERS 注释）。空白点取左边缘 (0.10,0.50)，避开横幅
    本体（点正文/「前往获取」会进活动页）和主界面顶栏/底栏。
    """
    hit = _pick_result(logger, hwnd, results, EVENT_ANNOUNCE_MARKERS, min_conf=0.55)
    if not hit:
        return False
    _click_ratio(logger, hwnd, EVENT_ANNOUNCE_BLANK, "活动公告点空白关闭")
    logger.info(f"[白荆回廊·日常] 场景退出·活动公告：点空白关闭（命中 {hit['text']!r}）")
    time.sleep(1.2)
    return True




def _scene_exit_tea_room(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """茶憩 3D 房间：左上「返回」→ 退出确认框「确认」（与休息室同一套离开交互）。

    退出后回到休息室——它也在登记表里，下一轮恢复会接着把休息室退掉。
    """
    back = _pick_result(
        logger, hwnd, results, ("返回",), frac_region=REST_ROOM_BACK_REGION, min_conf=0.5
    )
    if back:
        _pyautogui_click_guarded(*back["center"])
        logger.info(f"[白荆回廊·日常] 场景退出·茶憩房间：点左上「返回」@ {back['center']}")
    else:
        _click_ratio(logger, hwnd, REST_ROOM_BACK, "茶憩房间左上「返回」（比例兜底）")
    time.sleep(1.5)
    # 确认框是点了返回之后才弹出来的，必须重新识别一轮（同休息室）
    fresh = _scene_results(logger, hwnd)
    confirm = _pick_result(logger, hwnd, fresh, ("确认", "确定"), min_conf=0.6)
    if confirm:
        _pyautogui_click_guarded(*confirm["center"])
        logger.info(
            f"[白荆回廊·日常] 场景退出·茶憩房间：确认框「{confirm['text']}」@ {confirm['center']}"
        )
    else:
        _click_ratio(logger, hwnd, REST_ROOM_LEAVE_CONFIRM, "茶憩房间退出确认（比例兜底）")
    time.sleep(1.5)
    return True




def _scene_exit_cat_retail(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """有猫零售页：点左上「返回」回主界面（零售入口只在主界面底栏，返回即到家）。"""
    return _exit_via_top_left_back(logger, hwnd, results, "有猫零售")




# 好友页：2026-11-13 10:18 取证（#282，友情勋章任务领完留在好友页，恢复认不出
# 白等 90s）。OCR 实测「一键领取/赠送」的「一」常被截成「键领取/赠送」，两个
# 变体都登记；判定走 detector ≥2 独有词（铁律），「好友列表」单词太泛不上表。
FRIEND_SCENE_MARKERS = ("好友列表", "列表内查找好友", "今日赠礼", "键领取/赠送", "一键领取/赠送")




def _friend_page_hit(logger: logging.Logger, hwnd, results: List[Dict]) -> Optional[Dict]:
    """好友页 = 独有词 ≥2（「好友列表」单独出现不算——别的页面可能同名词）。"""
    texts = [str(r.get("text") or "") for r in (results or [])]
    hits = [m for m in FRIEND_SCENE_MARKERS if any(m in t for t in texts)]
    if len(hits) < 2:
        return None
    return {"text": "好友页:" + "+".join(hits), "confidence": 1.0, "center": (0, 0)}




def _scene_exit_friend(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """好友页：点左上「返回」回主界面（好友入口在主界面左上图标行）。"""
    return _exit_via_top_left_back(logger, hwnd, results, "好友页")




def _loading_screen_hit(logger, hwnd, results: List[Dict]) -> Optional[Dict]:
    """加载过场 = 中央 logo + 进度百分比，两样都在才算。

    只看 logo 会把任何含「白荆回廊」字样的固定页面误判成加载（`_pick_result`
    的子串匹配），随后 `_scene_exit_loading` 白等满 60s。进度数字本身每帧不稳
    （11-10 实测 47%→98%），但「这一帧有百分比」是加载页的稳定事实，正好当
    第二道闸；其它规则仍走普通 markers 匹配，不受影响。
    """
    hit = _pick_result(logger, hwnd, results, LOADING_SCREEN_MARKERS, min_conf=0.5)
    if not hit:
        return None
    has_percent = any(
        re.search(r"\d{1,3}\s*%", str(r.get("text") or ""))
        for r in (results or [])
    )
    return hit if has_percent else None




# 登记表顺序 = 匹配优先级：遮罩类先清，再清会挡住返回按钮的摘要，最后才是界面本身。
# 新增界面只要加一行（特征必须挑该界面独占文字，原因见 REST_ROOM_MARKERS 注释），
# 并在 artifacts/test_baijing_scene_registry_20261026.py 补一条用例锁住顺序。
SCENE_RULES = (
    # 加载过场排最前：加载中点哪都无效，先等它过去，别让真页面在 unknown 里被放弃
    {"name": "加载过场", "markers": LOADING_SCREEN_MARKERS, "min_conf": 0.5,
     "detector": _loading_screen_hit, "exit": _scene_exit_loading},
    # 活动公告横幅在遮罩前：加载完它经常第一个弹出来挡路（11-04/11-06 两次实录）
    {"name": "活动公告", "markers": EVENT_ANNOUNCE_MARKERS, "min_conf": 0.55, "exit": _scene_exit_event_announce},
    {"name": "过场遮罩", "markers": ("点击屏幕继续",), "min_conf": 0.5, "exit": _scene_exit_overlay},
    {"name": "计划等级提升", "markers": LEVEL_UP_MARKERS, "min_conf": 0.55,
     "exact_only": True, "exit": _scene_exit_level_up},
    {"name": "定巡之礼", "markers": EVENT_DIALOG_MARKERS, "min_conf": 0.55, "exit": _scene_exit_event_dialog},
    {"name": "茶憩摘要", "markers": TEA_SUMMARY_MARKERS, "min_conf": 0.55, "exit": _scene_exit_tea_summary},
    {"name": "关注特写", "markers": FOCUS_MODE_MARKERS, "min_conf": 0.55, "exit": _scene_exit_focus},
    {"name": "休息室", "markers": REST_ROOM_MARKERS, "min_conf": 0.55, "exit": _scene_exit_rest_room},
    {"name": "烙痕详情", "markers": MEMORY_DETAIL_MARKERS, "min_conf": 0.55, "exit": _scene_exit_memory_detail},
    {"name": "烙痕列表", "markers": MEMORY_LIST_MARKERS, "min_conf": 0.55, "exit": _scene_exit_memory_list},
    {"name": "茶憩房间", "markers": TEA_ROOM_REGISTRY_MARKERS, "min_conf": 0.55, "exit": _scene_exit_tea_room},
    {"name": "有猫零售", "markers": CAT_RETAIL_MARKERS, "min_conf": 0.55, "exit": _scene_exit_cat_retail},
    # 好友页排最后：独有词多页少见，detector ≥2 闸在（2026-11-13 #282 取证驱动）
    {"name": "好友页", "markers": FRIEND_SCENE_MARKERS, "min_conf": 0.55,
     "detector": _friend_page_hit, "exit": _scene_exit_friend},
)




def _match_scene_rule(logger: logging.Logger, hwnd, results: List[Dict]):
    """按登记表顺序找第一个命中的场景，返回 (规则, 命中项)；都没有返回 (None, None)。"""
    for rule in SCENE_RULES:
        detector = rule.get("detector")
        if detector is not None:
            # 组合判据（如加载页 = logo + 百分比）自己决定要不要命中
            hit = detector(logger, hwnd, results)
            if hit:
                return rule, hit
            continue
        hit = _pick_result(
            logger, hwnd, results, rule["markers"],
            min_conf=rule.get("min_conf", SCENE_OCR_MIN_CONF),
            exact_only=bool(rule.get("exact_only", False)),
        )
        if hit:
            return rule, hit
    return None, None




def _record_unknown_scene(logger: logging.Logger, hwnd, context: str, detail: str) -> None:
    """认不出界面 / 反复退不出去时落证据（截图 + 当帧文字），同一上下文 10 分钟一次。

    这是"下次遇到新页面只要加一行登记"的前提：没有证据就只能靠猜。
    """
    key = context or "default"
    now = time.time()
    # 用 None 而不是 0.0 当"从未记录"哨兵：测试里 time.time 被换成从 0 起步的
    # 假时钟，`now - 0.0 < 600` 会把第一次记录也吞掉（真机上不会，但断言全瞎）。
    last = _SCENE_LOGGED.get(key)
    if last is not None and now - last < _SCENE_LOG_INTERVAL_SEC:
        return
    _SCENE_LOGGED[key] = now
    _record_step_failure(logger, hwnd, f"场景恢复:{key}", detail)




def _scene_failure_detail(
    last_scene: str, last_unmatched_texts: str, last_acted: bool = False
) -> str:
    """恢复收尾的取证文案：不让上一轮的场景名冒充当帧结论。

    抽成纯函数便于用真实当帧 OCR 文本回归（2026-11-10 10:00:32 事故）。
    `last_acted` 区分「加载过场」的两种结局：等待超时返回 False（文案说
    仍在加载）和加载完成后轮次恰好耗尽——后者不该说成 60s 超时
    （2026-11-12 10:14 #279 实锤：实际 3s 就加载完了）。
    """
    if last_unmatched_texts:
        return (
            f"最后识别到「{last_scene}」，收尾时画面已变成未登记界面；"
            f"当帧文字: {last_unmatched_texts[:200]}"
        )
    if last_scene == "加载过场" and not last_acted:
        return f"加载过场等待 {LOADING_WAIT_TIMEOUT_SEC:.0f}s 后仍在加载，未强行点击"
    return f"场景「{last_scene}」反复退出失败，仍无法回到任务面板/主界面"




def recover_scene(
    logger: logging.Logger,
    hwnd,
    stop_check=None,
    context: str = "",
    rounds: int = SCENE_RECOVERY_ROUNDS,
    results: Optional[List[Dict]] = None,
) -> Tuple[str, bool]:
    """匹配当前场景并执行它的专属退出流程。

    返回 (状态, 是否做过动作)：
    - "panel"：日常任务列表在屏；
    - "main"：主界面；
    - "scene:<名称>"：命中过已知场景（可能仍没退出去，调用方按 acted 决定是否复检）；
    - "unknown"：连续几轮认不出任何已知场景（已落证据，调用方走自己的兜底）。

    `results` 让调用方把已经取到的一轮 OCR 传进来复用，避免重复截屏。
    """
    acted_any = False
    last_scene = ""
    last_acted = False
    unknown_streak = 0
    last_unmatched_texts = ""
    cached = results
    for _ in range(max(1, int(rounds))):
        if _stopped(stop_check):
            break
        current = cached if cached is not None else _scene_results(logger, hwnd)
        cached = None  # 只有调用方传进来的第一轮能复用，之后必须重新截屏
        if _pick_result(logger, hwnd, current, TASK_LIST_CORE_MARKERS, min_conf=0.55):
            return ("panel", acted_any)
        is_main, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=1.5)
        if is_main:
            return ("main", acted_any)
        rule, _hit = _match_scene_rule(logger, hwnd, current)
        if rule is None:
            unknown_streak += 1
            last_unmatched_texts = "｜".join(
                str(r.get("text") or "") for r in (current or [])[:12]
            )
            if unknown_streak >= SCENE_UNKNOWN_ROUNDS:
                texts = last_unmatched_texts
                _record_unknown_scene(
                    logger, hwnd, context or "未知场景",
                    f"未匹配任何已知场景，需要补登记；当帧文字: {texts[:200]}",
                )
                return ("unknown", acted_any)
            continue
        last_unmatched_texts = ""
        last_scene = str(rule["name"])
        logger.info(f"[白荆回廊·日常] 识别到场景「{last_scene}」，执行专属退出")
        try:
            acted = bool(rule["exit"](logger, hwnd, current, stop_check=stop_check))
        except Exception as exc:
            logger.warning(f"[白荆回廊·日常] 场景「{last_scene}」退出流程异常: {exc}")
            acted = False
        acted_any = acted_any or acted
        last_acted = acted
        if not acted:
            unknown_streak += 1
            if unknown_streak >= SCENE_UNKNOWN_ROUNDS:
                break
        time.sleep(0.4)
    if last_scene:
        # 轮次耗尽 ≠ 失败：收尾当帧若已漂到面板/主界面/另一张登记表内场景，
        # 说明每轮退出都在推进、只是预算烧完，落失败证据只会制造假案
        # （2026-11-12 10:14 #279 实锤：茶憩房间→关注特写→休息室→加载过场
        # 四轮全部成功，烧光 rounds 后恰逢定巡之礼弹出，被落成「仍在加载」
        # 假失败，3 秒后调用方重试就把它领掉了）。只有仍卡在 last_scene 同名
        # 规则上（真·退不出去）或画面未登记时才落证据。
        fresh = _scene_results(logger, hwnd)
        if _pick_result(logger, hwnd, fresh, TASK_LIST_CORE_MARKERS, min_conf=0.55):
            return ("panel", acted_any)
        if detect_baijing_main_ui(logger, hwnd, budget_sec=1.5)[0]:
            return ("main", acted_any)
        rule_now, _hit_now = _match_scene_rule(logger, hwnd, fresh)
        if rule_now is not None and rule_now["name"] != last_scene:
            logger.info(
                f"[白荆回廊·日常] 恢复轮次耗尽，当帧已是登记表内场景「{rule_now['name']}」，"
                "交回调用方继续下一轮恢复"
            )
            return (f"scene:{last_scene}", acted_any)
        # 轮次耗尽时画面常已不是 last_scene（2026-11-10 10:00:32：最后识别到
        # 「休息室」，点确认后游戏切进加载过场，取证截图也是 98% 加载页），
        # 拿旧场景名配当帧截图会报出「对着加载页说休息室反复退出失败」。
        detail = _scene_failure_detail(
            last_scene, last_unmatched_texts, last_acted=last_acted
        )
        _record_unknown_scene(logger, hwnd, context or f"场景{last_scene}", detail)
        return (f"scene:{last_scene}", acted_any)
    return ("unknown", acted_any)




def _ensure_main_ui(
    logger: logging.Logger,
    hwnd,
    stop_check=None,
    attempts: Optional[int] = None,
) -> bool:
    """确认/恢复到主界面（文件头规划的步骤 1，2026-10-22 补实现）。

    循环：检测主界面 → 不在则先 _dismiss_overlay 消遮挡（「点击屏幕继续」类，
    否则关闭按钮被盖住点不到）→ 点「关闭」（OCR 优先 + 模板兜底）→ ESC → 再检测。
    成功即 True；attempts 轮（默认 MAX_CLOSE_ATTEMPTS）后仍不在返回 False。

    OCR「关闭」必须排除引导气泡「点击按钮或空白区域关闭」——2026-10-21 实锤：
    该气泡 conf 0.89 截胡真关闭按钮，点它无效，白烧 3 轮 attempt 后
    close_task_list 判失败，连带俱乐部/零售免费礼包被整体跳过。
    """
    max_attempts = attempts if attempts is not None else MAX_CLOSE_ATTEMPTS
    for attempt in range(1, max_attempts + 1):
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        ok, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=2.0)
        if ok:
            logger.info("[白荆回廊·日常] 已回到主界面")
            return True
        # 场景登记表：ESC 无效的界面（休息室/关注特写）必须走各自的退出流程
        state, acted = recover_scene(logger, hwnd, stop_check=stop_check, context="恢复主界面")
        if state == "main":
            logger.info("[白荆回廊·日常] 已回到主界面")
            return True
        if acted:
            continue
        _dismiss_overlay(logger, hwnd)
        # 关闭按钮（OCR 带引导文字黑名单 → 模板兜底）
        hit = _ocr_best_match(
            logger, ("关闭",), hwnd, min_conf=0.6, exclude=("点击按钮", "空白区域")
        )
        if hit:
            _pyautogui_click_guarded(*hit["center"])
            logger.info(f"[白荆回廊·日常] 点击「关闭」@ {hit['center']}")
            time.sleep(1.2)
            continue
        center = _template_center(logger, hwnd, str(TEMPLATES_DIR / "白荆回廊-关闭.png"), threshold=0.85)
        if center:
            _pyautogui_click_guarded(center[0], center[1])
            logger.info(f"[白荆回廊·日常] 点击「关闭」按钮 @ {center}")
            time.sleep(1.2)
            continue
        # ESC 收尾
        _focus_game_window(logger, hwnd)
        pyautogui.press("escape")
        logger.info(f"[白荆回廊·日常] 按 ESC 关闭当前界面 (#{attempt}/{max_attempts})")
        time.sleep(1.5)
    ok, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=2.0)
    if ok:
        logger.info("[白荆回廊·日常] 已回到主界面")
    else:
        logger.warning("[白荆回廊·日常] 多次尝试后仍未确认回到主界面")
    return ok


