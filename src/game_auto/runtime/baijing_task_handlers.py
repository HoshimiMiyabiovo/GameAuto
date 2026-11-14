# -*- coding: utf-8 -*-
"""白荆回廊·专项任务 handler（2026-11-14 自 baijing_daily.py 拆出）。

职责：需要跳转专门界面的日常任务——研发室收取（lab）、烙痕记忆强化/突破
（memory）、午后茶憩（tea）、叶脉联结领取（leaf）、阶段战斗/快速战斗
（stage battle/imaging）、好友赠礼与勋章（friend）。

数据流：daily 的 process_task_list 按目录 key 调度 *_handle_*_task；handler
内部复用 tasklist 的找行/回面板原语（_find_row_button/_return_to_task_panel）、
scene 的场景常量（TEA_SUMMARY_MARKERS 等，唯一定义点在 baijing_scene）、
common 的 OCR/点击原语。依赖方向 handlers → tasklist/scene/common，禁反向。

不变量与坑：
- 茶憩是全流程最长的专项（邀请→选人→喝茶×2→话题→摘要退出），每步都有
  假阳性实锤（见各常量注释），改判定前先读注释里的日期实锤。
- 战斗页是 OCR 盲页（#283），「开始战斗」点完按"布阵页已离开"判进战斗。
"""
import logging
import time
from typing import Dict, List, Optional, Tuple

import pyautogui

from ..settings.paths import BAIJING_VERIFY_DIR, TEMPLATES_DIR
from .baijing_common import (
    TASK_LIST_CORE_MARKERS,
    TASK_ROW_REGION,
    _abs_region,
    _capture_daily_list_snapshot,
    _click_ratio,
    _dismiss_overlay,
    _ensure_window,
    _ocr_best_match,
    _pick_result,
    _pixel_is_blue,
    _ratio_point,
    _record_step_failure,
    _same_click_target,
    _scene_results,
    _stopped,
    _template_center,
    _wait_page_markers,
)
from .baijing_scene import (
    FOCUS_MODE_MARKERS,
    FOCUS_MODE_REGION,
    LEVEL_UP_MARKERS,
    MEMORY_DETAIL_MARKERS,
    MEMORY_ENHANCE_BTN_REGION,
    MEMORY_ENHANCE_BUTTON_TPL,
    MEMORY_ENHANCE_TEXTS,
    MEMORY_LIST_MARKERS,
    REST_ROOM_MARKERS,
    TEA_SUMMARY_CLICK,
    TEA_SUMMARY_MARKERS,
    _exit_via_top_left_back,
    _in_rest_room,
    _on_memory_detail_page,
)
from .baijing_tasklist import (
    _ensure_task_list_open,
    _find_row_button,
    _return_to_task_panel,
    _wait_task_list,
    close_task_list,
)
from .baijing_echo import CLUB_BLANK_CLICK
from .baijing_flow import _baijing_today_key, _focus_game_window, _window_region
from .foreground_flow_runner import _pyautogui_click_guarded
from .ocr_click_tester import _normalize_text, grab_screen, ocr_window_content

# 「白荆穹顶」设施弹窗特征文字（点研发室「前往」后出现，2026-10-18 实测截图）
# 不能用「白荆穹顶」当弹窗特征：任务行描述「在白荆穹顶的初级研发室…」和收取
# 成功提示「资源已存入白荆穹顶仓库」都含这四个字，弹窗关掉后仍会被判成"还开着"，
# 从而多按 ESC 把任务列表一起关掉（2026-10-24 实锤）。
LAB_DIALOG_MARKERS = ("当前设施生产情况", "一键收取")


# 弹窗关闭提示文字（X 按钮紧贴其右侧）
LAB_DIALOG_HINT = "点击按钮或空白区域关闭"



# ── 记忆烙痕经验任务（2026-10-18 用户截图实测） ──
# 强化弹窗特征（等级提升/特质提升 tab、指定等级、强化费用）
MEMORY_DIALOG_MARKERS = ("等级提升", "特质提升", "指定等级", "强化费用")


# 详情页「强化烙痕」按钮模板/OCR 兜底区域/文字候选已上收 baijing_scene
# （scene 登记表判定详情页也要用，2026-11-14 拆分；本模块从 scene import）。


# 烙痕列表页候选点击位（窗口相对比例）：2026-10-18 用户截图实测校准。
# 首卡「第三杯咖啡」(SSR 01级) 名称在 (402,442) @1919x1079 → (0.21, 0.41)。
# 网格列距约 0.15、行距约 0.38；首卡点不到时依次试后面的卡片。
MEMORY_LIST_CLICK_CANDIDATES = (
    (0.21, 0.41),   # 首卡（左上，实测 01 级肯定不会满级）
    (0.36, 0.41),   # 第二列
    (0.52, 0.41),   # 第三列
    (0.21, 0.79),   # 第二行首卡
    (0.36, 0.79),
)


# 「指定等级」行：截图上重置中心 (1189,669)/1919x1079 ≈ (0.620,0.620)，
# OCR 置信度长期只有 0.55（阈值 0.55 边缘），限定区域 + 0.45 阈值降低漏检。
MEMORY_RESET_REGION = (0.55, 0.58, 0.68, 0.66)


# 「+」按钮相对「重置」的水平偏移（占窗口宽度比例；实测 1425-1190=235px @1919 宽）
PLUS_BUTTON_DX_RATIO = 0.1225


# 突破弹窗（2026-11-08 12:41 首跑实锤 #221）：烙痕升到整十级（10/20/30…）时点
# 「强化烙痕」弹的不是强化窗而是「等级提升」突破确认——顶部页签就叫「等级提升」，
# 会被 MEMORY_DIALOG_MARKERS 误认成强化窗，随后 重置/+ 全部落空。用户口径：
# 能突破（确认按钮蓝色可点）就确认后继续强化；材料不够不能突破就取消换一张。
# 判定标记只在突破窗出现；「等级提升」两个窗都有，绝不能算突破证据。
MEMORY_BREAKTHROUGH_MARKERS = ("突破效果变化", "烙痕等级上限", "消耗以下材料")


# 弹窗按钮（窗口比例，2026-11-08 取证帧 @1919x1079）：确认中心 (1173,860) →
# (0.611,0.796)；取色点在确认文字左侧的按钮底色空白 (1056,860)→(0.55,0.796)；
# 取消中心 (741,860)→(0.386,0.796)。按钮文字「确 认/取 消」带空格，OCR 匹配
# 走 _ocr_best_match 的 _normalize_text（去空白），上午 XM「Uni ty」同款坑。
MEMORY_BREAKTHROUGH_CONFIRM = (0.611, 0.796)


MEMORY_BREAKTHROUGH_CONFIRM_SAMPLE = (0.55, 0.796)


MEMORY_BREAKTHROUGH_CANCEL = (0.386, 0.796)


# 强化弹窗的两个页签（2026-11-11 10:15 取证帧 #271 @1919x1079）：
# 「等级提升」@(447,206)、「特质提升」@(758,205)。弹窗**默认停在「特质提升」**，
# 此时画面只有「特质变动/特质升级/消耗材料/当前选择」，没有「指定等级/重置/+」，
# 直接按 MEMORY_RESET_REGION 找「重置」→ 推算「+」必然落空（用户：切到等级提升页就好）。
# 「重置/[+]/最大」只存在于「等级提升」页签。区域按 (x0,y0,x1,y1) 窗口比例
# （_abs_region 的语义，与 MEMORY_RESET_REGION 一致），只圈顶部页签行，
# 避免正文里的同名词被误点。
MEMORY_LEVEL_TAB_REGION = (0.15, 0.13, 0.45, 0.26)


MEMORY_LEVEL_TAB_TEXT = ("等级提升",)


MEMORY_LEVEL_TAB_FALLBACK = (0.233, 0.191)


MEMORY_LEVEL_TAB_MARKERS = ("指定等级", "重置", "最大")



# ── 午后茶憩任务（2026-10-18 用户截图实测） ──
# 流程：任务行「前往」→「是否前往茶憩?」确定 → 角色选择页点首个角色 →
#       茶憩场景喝茶 → 等待后再来一杯 → 茶憩结束摘要随意点击 → 休息室 → 返回 → 确认 → 主界面
TEA_CONFIRM_MARKERS = ("是否前往茶憩",)                  # 前往确认弹窗


# 角色选择页特征。2026-10-25 19:08 实跑稳定命中页底帮助文字
# 「同调者位阶达到S2后，可以开始提升技能等级。」（conf 0.93），
# 「位阶」这类短词 OCR 有抖动（同日 17:51 那轮 45s 没命中），补长词兜底。
TEA_SELECT_MARKERS = ("今日可邀份额", "位阶", "默契值", "同调者位阶")


# 2026-10-25 19:50 失败截图：点「确定」后有时直接进休息室 3D 场景（不是角色
# 选择页）。2026-10-26 12:50 进一步实锤：休息室里要点右下「午后茶憩」立牌卡，
# 点左下角色头像只会进入「关注」特写镜头，茶憩场景永远等不到（休息室判定与
# 入口坐标见下方「REST_ROOM_*」）。
TEA_SCENE_MARKERS = ("茶憩纪念函",)                      # 茶憩场景


TEA_TOPIC_MARKERS = ("选择话题", "结束茶憩")             # 话题菜单（场景中弹出）


TEA_DRINK_TEXTS = ("喝茶", "再来一杯", "请喝茶", "干杯")  # 喝茶交互文字（场景内动态出现）


# 2026-11-03 实锤：点首个角色卡后画面停在「20 个文字区域」的页面 23 秒（不是茶憩
# 场景），之后按老逻辑点底部对话框 (0.50,0.92)，反而把页面退回休息室。这里只认
# 语义明确的词，**不碰「确认」**——同页有「将离开休息室」的确认框，误点会直接
# 退回上一层。
# 2026-11-07 13:20 当帧（20261107_132050）修正认识：点角色卡后页面根本不跳页——
# 当帧就是角色选择页本身（首位角色卡已打勾、今日可邀份额 3/3、底部说明文字
# 「每名同调者一天仅能邀请一次…」就在这页上），之前认定的"角色详情页"多半就是它。
# 真正的前进按钮是右下「喝茶」圆钮，把它加进候选（OCR 哪天认出竖排字就直接点）。
TEA_INVITE_TEXTS = ("喝茶", "邀请", "开始茶憩", "一同品茶", "邀约")


# 2026-11-04 实锤：角色详情页的说明文字
# 「每名同调者一天仅能邀请一次，重复邀请请使用通用邀请函」也含「邀请」，
# 旧逻辑按子串命中后点了这行正文，页面原地不动 → 茶憩永远进不去。
# 邀请入口只认短按钮名；含这些字样的一律不是按钮。
TEA_INVITE_EXCLUDE_TEXTS = ("一天仅能邀请一次", "重复邀请", "邀请函", "邀约函")


# 「邀请函」兜住一族道具名：通用邀请函/定向邀请函……11-06 09:13 实锤 legacy 子串
# 匹配把详情页道具「定向邀请函」(0.82) 当邀请按钮点了（5 个字，≤12 过滤拦不住），
# 等于没点，之后喝茶轮询在错误页面上盲点漫游。真正的按钮是「邀请/开始茶憩」。
TEA_INVITE_MAX_CHARS = 12


# 关键点击位（窗口相对比例，实测 @1919x1079）
TEA_CONFIRM_OK = (0.623, 0.634)          # 前往确认-确定 (1196,684)


TEA_FIRST_CHARACTER = (0.412, 0.417)     # 角色选择页首个角色卡 (790,450)


# 2026-11-07 13:20 当帧（960x540）实测：角色选择页右下「喝茶」圆钮中心。竖排
# 艺术字，当帧 OCR 15 个文字区域里完全没有它——OCR 候选命中不了，只能按比例兜底。
TEA_SELECT_DRINK_RATIO = (0.827, 0.800)


TEA_FIRST_TOPIC = (0.753, 0.360)         # 话题菜单第一个话题 (1445,388)


TEA_DIALOGUE_CLICK = (0.50, 0.92)        # 对话框区域（点击推进对话）

# TEA_SUMMARY_CLICK 已上收 baijing_scene（scene 的茶憩摘要退出也要用，拆分）。


# 喝茶轮数与每杯间隔（用户流程：喝一次 → 等待 → 再来一杯）
TEA_DRINK_ROUNDS = 2


TEA_DRINK_WAIT_SEC = 10.0


TEA_DRINK_POLL_SEC = 20.0


# 茶憩要经过游戏加载：2026-10-20 实测 ASHECHOES 加载条从 47% 到 99% 超过 20s，
# 原先 10s 的等待会把“还在加载”误判成“没进角色选择页”，随后点返回取消进入。
TEA_SELECT_TIMEOUT_SEC = 45.0


TEA_SCENE_TIMEOUT_SEC = 25.0


REST_ROOM_TEA_ENTRY = (0.883, 0.830)             # 右下「午后茶憩」立牌卡中心 (848,448)



# ── 显影行动任务（2026-10-18 用户截图实测） ──
# 流程：任务行「前往」→ 关卡选择页（E-1~E-10）→ 点右下「快速战斗」→
#       「快速战斗设置」弹窗（指定次数默认 1）→ 再点弹窗内「快速战斗」→
#       战斗结算页随意点击 → ESC 逐层返回。
IMAGING_STAGE_MARKERS = ("进入关卡", "行动力")              # 关卡选择页


IMAGING_DIALOG_MARKERS = ("快速战斗设置", "指定次数")        # 快速战斗设置弹窗


IMAGING_SETTLEMENT_MARKERS = ("任务完成", "战斗结算", "结算", "挑战成功", "获得道具")


# 关键点击位（窗口相对比例，实测 @1919x1079）
IMAGING_QUICK_BATTLE_BTN = (0.784, 0.893)   # 关卡页「快速战斗」(1505,963)


# 2026-11-03 实锤：物资保障点上面这个固定比例，弹出的是「进入关卡/行动力」确认框，
# 不是「快速战斗设置」——同一天的显影/自由贸易/光刻都正常，说明不同关卡页布局不一样，
# 比例落点会压到邻居按钮。有文字就先点 OCR 命中的 center，比例只做兜底。
IMAGING_QUICK_BATTLE_TEXTS = ("快速战斗",)


IMAGING_QUICK_BATTLE_REGION = (0.55, 0.72, 1.0, 1.0)   # 关卡页右下按钮区


IMAGING_DIALOG_OK_BTN = (0.617, 0.799)      # 弹窗内「快速战斗」确认 (1185,862)


# 2026-10-25 用户弹窗截图（1707x960）：指定次数 1 右侧第一个蓝色圆钮是「+」，
# 再右边是「最大」。点一次 + 即 1→2；四个关卡里只给第一场加一次，凑满 5 次战斗。
IMAGING_DIALOG_PLUS_BTN = (0.580, 0.570)


# 只用于 OCR 复核「指定次数」的值，避免窗口其他数字（行动力/49 等）干扰
IMAGING_DIALOG_COUNT_REGION = (0.43, 0.50, 0.57, 0.63)


IMAGING_ANYWHERE_CLICK = (0.50, 0.50)       # 结算页随意点击（屏幕中心）


# 关卡战斗类任务清单（操作相同，2026-10-18 用户确认）：
# (task_key, 行内关键词, 展示名)
STAGE_BATTLE_TASKS = (
    ("imaging_action", "显影行动", "显影行动"),
    ("free_trade", "自由贸易", "自由贸易"),
    ("material_guarantee", "物资保障", "定向物资保障"),
    ("litho_protocol", "光刻协议", "光刻协议"),
)



# ── 叶脉联结领取（2026-10-18 用户截图实测） ──
# 日常完毕后：点左侧「叶脉联结」tab →「任务总览」→「全部领取」。
# 叶脉联结页含本日/本周/本期任务（如「日常任务累计活跃度达到100」），
# 奖励与日常 tab 不互通，必须单独领。
LEAF_TAB_TEXTS = ("叶脉联结", "叶脉连结")              # 左侧 tab（游戏内是"联结"）


LEAF_PAGE_MARKERS = ("本日任务", "联结计划", "任务总览", "当前经验")  # 叶脉联结页特征


LEAF_OVERVIEW_BTN = (0.384, 0.793)   # 任务总览 (736,856)


LEAF_CLAIM_ALL_BTN = (0.709, 0.796)  # 全部领取 (1360,859)


# 「叶脉联结」tab 搜索区域：只取左侧 tab 列，排除底部 VEINOS 图标
# （2026-10-19 曾把 (188,848) 的 logo 误认成「叶脉联结计划」；该点在 fy≈0.79）
LEAF_TAB_REGION = (0.0, 0.20, 0.16, 0.72)


LEAF_TAB_FALLBACK = (0.10, 0.55)     # 实测 tab 中心 (208,596)@1919x1079 → (0.108,0.553)



# ── 友情勋章（2026-10-20 用户实测截图） ──
# 主界面左上角功能图标行：设置(0.052) / 邮件(0.095) / 好友(0.138) / 俱乐部(0.183)。
# 好友图标无文字，按实测比例点击；好友页顶部右侧「一键领取/赠送」用 OCR 定位。
# 好友页特征：左侧「好友列表」+ 右上「一键领取/赠送」。
FRIEND_ENTRY_FALLBACK = (0.138, 0.047)


FRIEND_PAGE_MARKERS = ("好友列表", "一键领取/赠送")


FRIEND_GIFT_TEXTS = ("一键领取/赠送", "一键领取赠送", "一键领取", "一键赠送")


# 点击「一键领取/赠送」后的结果弹窗按钮（按优先级给 OCR 匹配）
FRIEND_POPUP_TEXTS = ("确定", "确认", "知道了", "关闭", "点击屏幕继续", "收下")


# 弹窗按钮搜索区（放宽到接近全屏中部，兼容底部确认按钮）
FRIEND_POPUP_REGION = (0.15, 0.12, 0.85, 0.92)


# 失败提示只在更小的中部区域判断，且只用赠送独有词组，避免好友签名误伤
FRIEND_POPUP_TEXT_REGION = (0.20, 0.18, 0.80, 0.82)


FRIEND_POPUP_FAILURE_TEXTS = (
    "赠送失败", "领取失败", "赠送次数", "已达上限", "已用完", "没有好友", "无法赠送",
)




def _capture_stage_battle_dialog(logger: logging.Logger, hwnd) -> Optional[str]:
    """快速战斗设置弹窗首次出现时抓图并打印 OCR 文字（定位“指定次数”控件）。

    只按天抓一次；文件存在就跳过，避免四个关卡战斗重复写盘/刷日志。

    注意：这是「日常留档」（baijing_verify，一天一张），不是失败证据。
    异常/失败取证走 `_record_step_failure` → `flow_diagnostics.record_failure`
    （flow_failures 表 + flow_failures 目录 + OCR 标注图）。
    """
    try:
        out_path = BAIJING_VERIFY_DIR / f"battle_dialog_{_baijing_today_key()}.jpg"
        if out_path.is_file():
            return str(out_path)
        img = grab_screen(region=_window_region(hwnd))
        if img.mode != "RGB":
            img = img.convert("RGB")
        BAIJING_VERIFY_DIR.mkdir(parents=True, exist_ok=True)
        img.save(str(out_path), "JPEG", quality=85, optimize=True)
        logger.info(f"[白荆回廊·日常] 快速战斗设置弹窗快照: {out_path.name}")
        results = ocr_window_content(logger, hwnd=hwnd)
        for r in results[:30]:
            logger.info(
                f"  - {r.get('text')!r} ({float(r.get('confidence') or 0):.2f}) @ {r.get('center')}"
            )
        return str(out_path)
    except Exception as e:
        logger.debug(f"[白荆回廊·日常] 快速战斗弹窗快照失败: {e}")
        return None




def _confirm_action_power_use(logger: logging.Logger, hwnd, stop_check=None, max_clicks: int = 2) -> bool:
    """点掉「体力/行动力不足 → 确认使用补充道具」弹窗（最多点 max_clicks 次）。

    2026-11-03 用户确认：游戏推荐的是限时行动力药，可以直接用，不必等过期。
    只匹配「确认使用」这条文案，不碰泛化的「确认」——同一套页面里还有
    「将离开休息室」之类的退出确认，误点会把流程退回上一层。
    注意：这里默认「确认使用」消耗的是限时/免费补充道具；若哪天游戏改成
    默认消耗付费道具，需要在此处加文案白名单（例如必须同时出现「限时」），
    日志里已记录命中的原文，便于事后核对。
    返回是否至少点过一次。
    """
    clicked = False
    for _ in range(max_clicks):
        if _stopped(stop_check):
            break
        hit = _ocr_best_match(logger, ("确认使用",), hwnd, min_conf=0.5)
        if not hit:
            break
        _pyautogui_click_guarded(*hit["center"])
        logger.info(
            f"[白荆回廊·日常] 行动力/体力不足，已确认使用补充道具 {hit['text']!r} @ {hit['center']}"
        )
        clicked = True
        time.sleep(1.5)
    return clicked




def _close_lab_dialog(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """关闭「白荆穹顶」弹窗：X 按钮（提示文字右侧）→ 空白处 → ESC。"""
    for attempt in range(3):
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        # 已经回到任务列表 → 弹窗肯定关了。必须先判列表再看弹窗特征，
        # 否则列表行里的“白荆穹顶”等文字会让关闭循环继续按 ESC 把列表也关掉。
        if _ocr_best_match(logger, TASK_LIST_CORE_MARKERS, hwnd, min_conf=0.55):
            logger.info("[白荆回廊·日常] 任务列表已在屏，判定白荆穹顶弹窗已关闭")
            return True
        # 弹窗已关？
        if not _ocr_best_match(logger, LAB_DIALOG_MARKERS, hwnd, min_conf=0.55):
            return True
        # 1) X 按钮：OCR 到关闭提示文字，点其右侧
        hint = _ocr_best_match(logger, (LAB_DIALOG_HINT,), hwnd, min_conf=0.5)
        if hint:
            box = hint.get("box")
            cx = int(box[2]) + 25 if box else int(hint["center"][0]) + 150
            cy = int(hint["center"][1])
            _pyautogui_click_guarded(cx, cy)
            logger.info(f"[白荆回廊·日常] 点击白荆穹顶弹窗 X 按钮 @ ({cx},{cy})")
            time.sleep(1.2)
            if not _ocr_best_match(logger, LAB_DIALOG_MARKERS, hwnd, min_conf=0.55):
                return True
        # 2) 空白处：弹窗下沿下方（避开行内按钮与底部固定栏）
        region = _abs_region(hwnd, TASK_ROW_REGION)
        if region:
            bx, by = region[0] + region[2] // 3, region[1] + region[3] - 30
            _pyautogui_click_guarded(bx, by)
            logger.info(f"[白荆回廊·日常] 点击空白区域关闭弹窗 @ ({bx},{by})")
            time.sleep(1.2)
            if not _ocr_best_match(logger, LAB_DIALOG_MARKERS, hwnd, min_conf=0.55):
                return True
        # 3) ESC 收尾
        _focus_game_window(logger, hwnd)
        pyautogui.press("escape")
        logger.info("[白荆回廊·日常] 按 ESC 关闭白荆穹顶弹窗")
        time.sleep(1.2)
    return not _ocr_best_match(logger, LAB_DIALOG_MARKERS, hwnd, min_conf=0.55)




def _handle_lab_harvest_task(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """研发室收获任务：点行内「前往」→ 白荆穹顶弹窗 → 一键收取 → 关闭回列表。

    2026-10-18 用户实测截图：点「前往」后弹「白荆穹顶」设施面板（当前设施生产
    情况 + 前往穹顶 + 一键收取）。注意是「一键收取」不是任务奖励的「一键领取」。
    收取后点 X/空白关闭弹窗即回到日常任务列表，该任务随后可出现「领取奖励」。
    """
    goto_pos = _find_row_button(logger, hwnd, "研发室", "前往", stop_check=stop_check)
    if not goto_pos:
        return False
    _pyautogui_click_guarded(goto_pos[0], goto_pos[1])
    logger.info(f"[白荆回廊·日常] 点击研发室任务「前往」@ {goto_pos}")
    # 等弹窗出现
    deadline = time.time() + 8.0
    dialog_ok = False
    while time.time() < deadline:
        if _stopped(stop_check):
            return False
        if _ocr_best_match(logger, LAB_DIALOG_MARKERS, hwnd, min_conf=0.55):
            dialog_ok = True
            break
        time.sleep(1.0)
    if not dialog_ok:
        logger.warning("[白荆回廊·日常] 点击「前往」后未出现白荆穹顶弹窗")
        return False
    logger.info("[白荆回廊·日常] 白荆穹顶弹窗已打开")
    # 一键收取（注意排除「前往穹顶」按钮）
    hit = _ocr_best_match(logger, ("一键收取",), hwnd, min_conf=0.6)
    if not hit:
        logger.warning("[白荆回廊·日常] 弹窗内未找到「一键收取」")
        _close_lab_dialog(logger, hwnd, stop_check=stop_check)
        return False
    _pyautogui_click_guarded(*hit["center"])
    logger.info(f"[白荆回廊·日常] 点击「一键收取」@ {hit['center']}")
    time.sleep(1.5)
    # 关闭弹窗回列表
    if not _close_lab_dialog(logger, hwnd, stop_check=stop_check):
        logger.warning("[白荆回廊·日常] 白荆穹顶弹窗未能关闭")
        return False
    # 确认回到日常任务列表
    if _wait_task_list(logger, hwnd, timeout=5.0, stop_check=stop_check):
        logger.info("[白荆回廊·日常] 已回到日常任务列表，研发室收取完成")
        return True
    logger.warning("[白荆回廊·日常] 关闭弹窗后未确认回到日常任务列表")
    return False




def _find_plus_button(
    logger: logging.Logger, hwnd, anchor: Optional[Tuple[int, int]] = None
) -> Optional[Tuple[int, int]]:
    """定位强化弹窗里的「+」按钮：由「重置」位置 + 固定水平偏移推算。

    2026-10-30 用户截图实测定标（`docs/refs/baijing_memory_enhance_20261030.png`，
    1919x1079）：「指定等级」行布局 [重置][Lv][+][最大]，重置中心 (1189,669)、
    最大中心 (1529,670)、+ 中心约 (1424,669)——+ 与重置同行、右移 0.1225 倍窗宽。
    点完重置后第二次 OCR 可能认不出「重置」（当日 0.56 低置信度），因此调用方
    传入刚点过的重置坐标（anchor）直接推算，不再依赖第二次 OCR；无 anchor 时才
    重新 OCR。
    """
    if anchor:
        region = _window_region(hwnd)
        if not region:
            return None
        cx = int(anchor[0] + PLUS_BUTTON_DX_RATIO * region[2])
        cy = int(anchor[1])
        logger.info(f"[白荆回廊·日常] 按「重置」锚点推算「+」按钮 @ ({cx},{cy})")
        return (cx, cy)
    hit = _ocr_best_match(
        logger, ("重置",), hwnd, frac_region=MEMORY_RESET_REGION, min_conf=0.45
    )
    if not hit:
        return None
    region = _window_region(hwnd)
    if not region:
        return None
    cx = int(hit["center"][0] + PLUS_BUTTON_DX_RATIO * region[2])
    cy = int(hit["center"][1])
    logger.info(f"[白荆回廊·日常] 推算「+」按钮位置 @ ({cx},{cy})")
    return (cx, cy)




def _ensure_memory_level_tab(
    logger: logging.Logger, hwnd, stop_check=None
) -> bool:
    """确保强化弹窗停在「等级提升」页签；「重置 / +」只在这一页。

    2026-11-11 10:15 取证帧（#271）：点「强化烙痕」后弹窗默认选中「特质提升」，
    ``MEMORY_DIALOG_MARKERS`` 里的「等级提升」只是**另一个页签的标题**，旧流程
    误以为强化窗已就绪，接着找「重置」→ 推算「+」→ 报「未找到 + 按钮」。
    用户口径：切到「等级提升」页即可。切完必须复验该页独占内容，否则绝不盲点。
    """
    # 已在目标页（如弹窗记住了上次页签）：不点击，直接放行
    if _ocr_best_match(logger, MEMORY_LEVEL_TAB_MARKERS, hwnd, min_conf=0.45):
        return True
    tab = _ocr_best_match(
        logger, MEMORY_LEVEL_TAB_TEXT, hwnd,
        frac_region=MEMORY_LEVEL_TAB_REGION, min_conf=0.5,
    )
    if tab:
        _pyautogui_click_guarded(*tab["center"])
        logger.info(f"[白荆回廊·日常] 强化弹窗切到「等级提升」页签 @ {tab['center']}")
    else:
        pos = _ratio_point(logger, hwnd, MEMORY_LEVEL_TAB_FALLBACK)
        if not pos:
            return False
        _pyautogui_click_guarded(*pos)
        logger.info(f"[白荆回廊·日常] 「等级提升」页签 OCR 未命中，比例兜底 @ {pos}")
    # 切页后短轮询复验；看不到「指定等级/重置/最大」就不允许继续找「+」
    for _ in range(3):
        if _stopped(stop_check):
            return False
        if _ocr_best_match(logger, MEMORY_LEVEL_TAB_MARKERS, hwnd, min_conf=0.45):
            return True
        time.sleep(0.5)
    return False




# _same_click_target 已上收 baijing_common（tasklist 找行防重复点击也要用，拆分）。


def _is_breakthrough_dialog(logger: logging.Logger, hwnd) -> bool:
    """当前弹窗是否「突破」确认窗（烙痕整十级）：专属标记 ≥2。

    「等级提升」是突破弹窗的页签、也被 MEMORY_DIALOG_MARKERS 收录，两个窗都有，
    绝不能当突破证据；「突破效果变化/烙痕等级上限/消耗以下材料」只在突破窗出现
    （2026-11-08 12:41 取证帧三词全在，conf ≥0.9）。
    """
    results = ocr_window_content(logger, hwnd=hwnd)
    if not results:
        return False
    texts = [_normalize_text(str(r.get("text") or "")) for r in results]
    texts = [t for t in texts if t]
    hits = sum(1 for m in MEMORY_BREAKTHROUGH_MARKERS if any(m in t for t in texts))
    return hits >= 2




def _find_memory_enhance_button(logger: logging.Logger, hwnd) -> Optional[Tuple[int, int]]:
    """烙痕详情页「强化烙痕」按钮：模板优先，OCR 兜底（烙痕/熔垣两词都收）。"""
    btn = _template_center(logger, hwnd, MEMORY_ENHANCE_BUTTON_TPL, threshold=0.85)
    if btn:
        return btn
    ocr_btn = _ocr_best_match(
        logger,
        MEMORY_ENHANCE_TEXTS,
        hwnd,
        frac_region=MEMORY_ENHANCE_BTN_REGION,
        min_conf=0.55,
    )
    if ocr_btn:
        pos = (int(ocr_btn["center"][0]), int(ocr_btn["center"][1]))
        logger.info(f"[白荆回廊·日常] 用 OCR 兜底定位「强化烙痕」@ {pos}")
        return pos
    return None




def _back_to_memory_list(logger: logging.Logger, hwnd) -> bool:
    """烙痕详情页点左上「返回」回列表页（换下一张烙痕用），**必须验证落点**。

    2026-11-08 用户追问「防止一直在同一张上循环」：返回后若不验证列表特征，
    停在详情页时下一轮点卡片坐标全是无效点击、详情页复核又把"没换成功"当
    成功——剩余候选全耗在同一张烙痕上。所以点返回（最多重试 2 次）后必须
    OCR 到列表页特征才算换卡成功；到不了返回 False，调用方立即终止落证据。
    """
    for _ in range(2):
        try:
            _exit_via_top_left_back(logger, hwnd, _scene_results(logger, hwnd), "烙痕详情")
        except Exception as exc:  # noqa: BLE001 - 返回异常按未离开详情页处理
            logger.debug(f"[白荆回廊·日常] 详情页返回异常: {exc}")
        if _wait_page_markers(logger, hwnd, MEMORY_LIST_MARKERS, timeout=5.0):
            logger.info("[白荆回廊·日常] 已返回烙痕列表页，换下一张")
            return True
    logger.warning("[白荆回廊·日常] 点返回后未回到烙痕列表页（防同卡循环，终止换卡）")
    return False




def _handle_memory_exp_task(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """记忆烙痕经验任务：前往 → 逐张卡尝试强化 → 重置 → +1 → 确认 → 动画 → 回面板。

    2026-10-18 用户截图实测流程：点行内「前往」进入记忆烙痕列表页（3列卡片网格，
    左上角首个卡片实测坐标 (402,442)@1919x1079），点卡片进详情页，点右下
    「强化烙痕」打开等级提升弹窗，「重置」后点一次「+」，「确认」后播等级提升
    动画，点击屏幕继续，然后逐层返回日常任务面板（该任务随后出现「领取奖励」，
    由常规领取循环处理）。

    2026-11-08 起逐卡循环（用户口径）：烙痕到整十数等级点「强化烙痕」弹的是
    「突破」确认窗——能突破就确认后继续强化，材料不够不能突破就取消换一张；
    全部候选卡试完仍没强化成功才判失败（#221 首跑实锤：首卡已 20/20 满级）。
    """
    goto_pos = _find_row_button(logger, hwnd, "经验", "前往", stop_check=stop_check)
    if not goto_pos:
        return False
    _pyautogui_click_guarded(goto_pos[0], goto_pos[1])
    logger.info(f"[白荆回廊·日常] 点击记忆烙痕任务「前往」@ {goto_pos}")
    # 等落地页：烙痕列表页（顶部工具栏）或直接就是详情页
    _wait_page_markers(logger, hwnd, MEMORY_LIST_MARKERS, timeout=8.0, stop_check=stop_check)
    time.sleep(1.5)
    # 2) 逐张卡尝试强化（2026-11-08 用户口径）：整十级烙痕弹突破窗——能突破就
    #    确认后继续强化，不能就取消换下一张；全部候选试完仍没强化成才判失败。
    on_detail = _on_memory_detail_page(logger, hwnd)
    if on_detail:
        logger.info("[白荆回廊·日常] 落地即烙痕详情页，直接强化")
    last_fail = ""
    for attempt, (card_fx, card_fy) in enumerate(MEMORY_LIST_CLICK_CANDIDATES):
        if _stopped(stop_check):
            return False
        if not on_detail:
            hwnd = _ensure_window(logger, hwnd)
            if not hwnd:
                return False
            region = _window_region(hwnd)
            if not region:
                return False
            cx = int(region[0] + card_fx * region[2])
            cy = int(region[1] + card_fy * region[3])
            _pyautogui_click_guarded(cx, cy)
            logger.info(
                f"[白荆回廊·日常] 尝试点击烙痕卡片 #{attempt + 1} @ ({cx},{cy}) "
                f"[{card_fx:.2f},{card_fy:.2f}]"
            )
            time.sleep(2.0)
            if not _on_memory_detail_page(logger, hwnd):
                last_fail = f"点卡片 #{attempt + 1} 未进入烙痕详情页"
                logger.warning(f"[白荆回廊·日常] {last_fail}")
                continue
            on_detail = True
        # 3) 点「强化烙痕」开弹窗（模板 + OCR 兜底）
        btn = _find_memory_enhance_button(logger, hwnd)
        if not btn:
            last_fail = "详情页未找到「强化烙痕」按钮"
            logger.warning(f"[白荆回廊·日常] {last_fail}（换下一张）")
            if not _back_to_memory_list(logger, hwnd):
                last_fail = "换卡失败：详情页返回后未回到列表页（防同卡循环，终止）"
                _record_step_failure(logger, hwnd, "记忆烙痕强化", last_fail)
                _return_to_task_panel(logger, hwnd, stop_check=stop_check)
                return False
            on_detail = False
            continue
        _pyautogui_click_guarded(btn[0], btn[1])
        logger.info(f"[白荆回廊·日常] 点击「强化烙痕」@ {btn}")
        if not _wait_page_markers(logger, hwnd, MEMORY_DIALOG_MARKERS, timeout=6.0, stop_check=stop_check):
            last_fail = "强化弹窗未打开"
            logger.warning(f"[白荆回廊·日常] {last_fail}（换下一张）")
            on_detail = _back_to_memory_list(logger, hwnd)
            continue
        # 4) 突破拦截：整十级烙痕弹「等级提升」突破确认而非强化窗（#221 实锤）
        if _is_breakthrough_dialog(logger, hwnd):
            can_break = _pixel_is_blue(logger, hwnd, *MEMORY_BREAKTHROUGH_CONFIRM_SAMPLE)
            logger.info(f"[白荆回廊·日常] 检测到突破弹窗（确认按钮可点={can_break}）")
            if can_break:
                confirm = _ocr_best_match(logger, ("确认",), hwnd, min_conf=0.5)
                confirm_pos = confirm["center"] if confirm else _ratio_point(
                    logger, hwnd, MEMORY_BREAKTHROUGH_CONFIRM
                )
                if not confirm_pos:
                    last_fail = "突破弹窗未找到「确认」按钮"
                    logger.warning(f"[白荆回廊·日常] {last_fail}（换下一张）")
                    _click_ratio(logger, hwnd, MEMORY_BREAKTHROUGH_CANCEL, "突破弹窗取消")
                    time.sleep(1.5)
                    on_detail = _back_to_memory_list(logger, hwnd)
                    continue
                _pyautogui_click_guarded(*confirm_pos)
                logger.info(f"[白荆回廊·日常] 突破确认 @ {confirm_pos}")
                # 突破可能有「点击屏幕继续」过场；等突破弹窗特征消失再回详情页
                if _wait_page_markers(logger, hwnd, ("点击屏幕继续",), timeout=6.0, stop_check=stop_check):
                    hit = _ocr_best_match(logger, ("点击屏幕继续",), hwnd, min_conf=0.5)
                    if hit:
                        _pyautogui_click_guarded(*hit["center"])
                        logger.info(f"[白荆回廊·日常] 突破过场点击屏幕继续 @ {hit['center']}")
                    else:
                        _click_ratio(logger, hwnd, (0.5, 0.5), "突破过场点屏幕中心")
                    time.sleep(1.0)
                # 不能用 MEMORY_DIALOG_MARKERS 判"弹窗已关"——详情页的「特质提升」
                # 会被误判成弹窗还在；只认突破专属标记是否消失。
                deadline = time.time() + 10.0
                while time.time() < deadline and _is_breakthrough_dialog(logger, hwnd):
                    time.sleep(0.5)
                if _is_breakthrough_dialog(logger, hwnd):
                    last_fail = "突破确认后弹窗未关闭"
                    _record_step_failure(logger, hwnd, "记忆烙痕强化", last_fail)
                    _return_to_task_panel(logger, hwnd, stop_check=stop_check)
                    return False
                # 突破后回详情页：重新点「强化烙痕」做本次经验提升
                btn2 = _find_memory_enhance_button(logger, hwnd)
                if not btn2:
                    last_fail = "突破后未找到「强化烙痕」按钮"
                    logger.warning(f"[白荆回廊·日常] {last_fail}（换下一张）")
                    if not _back_to_memory_list(logger, hwnd):
                        last_fail = "换卡失败：详情页返回后未回到列表页（防同卡循环，终止）"
                        _record_step_failure(logger, hwnd, "记忆烙痕强化", last_fail)
                        _return_to_task_panel(logger, hwnd, stop_check=stop_check)
                        return False
                    on_detail = False
                    continue
                _pyautogui_click_guarded(btn2[0], btn2[1])
                logger.info(f"[白荆回廊·日常] 突破后点击「强化烙痕」@ {btn2}")
                if not _wait_page_markers(logger, hwnd, MEMORY_DIALOG_MARKERS, timeout=6.0, stop_check=stop_check):
                    last_fail = "突破后强化弹窗未打开"
                    logger.warning(f"[白荆回廊·日常] {last_fail}（换下一张）")
                    if not _back_to_memory_list(logger, hwnd):
                        last_fail = "换卡失败：详情页返回后未回到列表页（防同卡循环，终止）"
                        _record_step_failure(logger, hwnd, "记忆烙痕强化", last_fail)
                        _return_to_task_panel(logger, hwnd, stop_check=stop_check)
                        return False
                    on_detail = False
                    continue
            else:
                # 材料不够不能突破（确认按钮置灰/取色读不出）：取消换一张。
                # 误判方向安全：把能突破误判成不能，换一张卡照样完成任务；
                # 反过来在置灰按钮上白点会把流程卡死。
                cancel = _ocr_best_match(logger, ("取消",), hwnd, min_conf=0.5)
                cancel_pos = cancel["center"] if cancel else _ratio_point(
                    logger, hwnd, MEMORY_BREAKTHROUGH_CANCEL
                )
                if cancel_pos:
                    _pyautogui_click_guarded(*cancel_pos)
                    logger.info(f"[白荆回廊·日常] 突破弹窗取消 @ {cancel_pos}（换下一张烙痕）")
                    time.sleep(1.5)
                else:
                    last_fail = "突破弹窗未找到「取消」按钮"
                    logger.warning(f"[白荆回廊·日常] {last_fail}")
                    _click_ratio(logger, hwnd, MEMORY_BREAKTHROUGH_CANCEL, "突破弹窗取消（比例兜底）")
                if not _back_to_memory_list(logger, hwnd):
                    last_fail = "换卡失败：详情页返回后未回到列表页（防同卡循环，终止）"
                    _record_step_failure(logger, hwnd, "记忆烙痕强化", last_fail)
                    _return_to_task_panel(logger, hwnd, stop_check=stop_check)
                    return False
                on_detail = False
                continue
        # 5) 正常强化：先确认停在「等级提升」页签 → 重置 → +1 → 确认
        #    2026-11-11 10:15 实锤（#271）：弹窗默认停在「特质提升」页，那一页
        #    只有「特质变动/消耗材料/当前选择」，没有「指定等级/重置/+」，
        #    直接按重置偏移找「+」必然报「未找到 + 按钮」。
        if not _ensure_memory_level_tab(logger, hwnd, stop_check=stop_check):
            last_fail = "强化弹窗未切到「等级提升」页签（找不到 指定等级/重置/最大）"
            logger.warning(f"[白荆回廊·日常] {last_fail}")
            _record_step_failure(logger, hwnd, "记忆烙痕强化", last_fail)
            _return_to_task_panel(logger, hwnd, stop_check=stop_check)
            return False
        reset = _ocr_best_match(
            logger, ("重置",), hwnd, frac_region=MEMORY_RESET_REGION, min_conf=0.45
        )
        reset_anchor = None
        if reset:
            reset_anchor = (int(reset["center"][0]), int(reset["center"][1]))
            _pyautogui_click_guarded(*reset["center"])
            logger.info(f"[白荆回廊·日常] 点击「重置」@ {reset['center']}")
            time.sleep(0.8)
            # 2026-10-30 10:25 实测：点重置后强化弹窗消失（OCR 区域 34→30，
            # 随后露出烙痕详情）。弹窗不在时绝不能按偏移盲点 +，否则点到详情页。
            if not _wait_page_markers(
                logger, hwnd, MEMORY_DIALOG_MARKERS, timeout=2.0, stop_check=stop_check
            ):
                logger.warning("[白荆回廊·日常] 重置后强化弹窗消失，停止（不盲点 +）")
                _record_step_failure(
                    logger, hwnd, "记忆烙痕强化", "重置后强化弹窗消失（重置=%s）" % (reset_anchor,)
                )
                _return_to_task_panel(logger, hwnd, stop_check=stop_check)
                return False
        plus_pos = _find_plus_button(logger, hwnd, anchor=reset_anchor)
        if not plus_pos:
            logger.warning("[白荆回廊·日常] 未找到「+」按钮")
            _record_step_failure(logger, hwnd, "记忆烙痕强化", "未找到「+」按钮（重置=%s）" % (reset_anchor,))
            _return_to_task_panel(logger, hwnd, stop_check=stop_check)
            return False
        _pyautogui_click_guarded(plus_pos[0], plus_pos[1])
        logger.info(f"[白荆回廊·日常] 点击「+」@ {plus_pos}")
        time.sleep(0.8)
        confirm = _ocr_best_match(logger, ("确认",), hwnd, min_conf=0.55)
        if not confirm:
            logger.warning("[白荆回廊·日常] 未找到「确认」按钮")
            _return_to_task_panel(logger, hwnd, stop_check=stop_check)
            return False
        _pyautogui_click_guarded(*confirm["center"])
        logger.info(f"[白荆回廊·日常] 点击「确认」@ {confirm['center']}")
        # 6) 等级提升动画 → 点击屏幕继续
        if _wait_page_markers(logger, hwnd, ("点击屏幕继续",), timeout=8.0, stop_check=stop_check):
            hit = _ocr_best_match(logger, ("点击屏幕继续",), hwnd, min_conf=0.5)
            if hit:
                _pyautogui_click_guarded(*hit["center"])
                logger.info(f"[白荆回廊·日常] 点击屏幕继续 @ {hit['center']}")
            else:
                region = _window_region(hwnd)
                if region:
                    cx, cy = region[0] + region[2] // 2, region[1] + region[3] // 2
                    _pyautogui_click_guarded(cx, cy)
                    logger.info(f"[白荆回廊·日常] 动画点击屏幕中心 @ ({cx},{cy})")
            time.sleep(1.5)
        else:
            logger.warning("[白荆回廊·日常] 未检测到等级提升动画（可能已达上限或确认未生效）")
        # 7) 返回日常任务面板
        return _return_to_task_panel(logger, hwnd, stop_check=stop_check)
    # 全部候选卡都没强化成（含逐张突破取消）：落证据交人工
    _record_step_failure(logger, hwnd, "记忆烙痕强化", last_fail or "全部候选卡未完成强化")
    _return_to_task_panel(logger, hwnd, stop_check=stop_check)
    return False




def _invite_hit_usable(hit: Optional[Dict]) -> bool:
    """角色详情页的「邀请」候选是否像按钮。

    2026-11-04：说明文字「每名同调者一天仅能邀请一次，重复邀请请使用通用邀请函」
    含「邀请」且置信度 0.92，被当成按钮点了一次。按钮名是短词，正文一律排除。
    """
    if not hit:
        return False
    text = "".join(str(hit.get("text") or "").split())
    if not text or len(text) > TEA_INVITE_MAX_CHARS:
        return False
    return not any(marker in text for marker in TEA_INVITE_EXCLUDE_TEXTS)




def _nudge_into_tea_scene(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """点完角色卡后补一脚：先快检茶憩场景，没进就找「邀请/喝茶」类推进按钮。

    2026-11-03 实锤：11:34:24 点了首个角色卡，之后 23 秒画面稳定在 20 个文字
    区域（不是茶憩场景），接着老逻辑点底部对话框 (0.50,0.92) 把页面退回了休息室。

    2026-11-04 实锤：这页的说明文字「每名同调者一天仅能邀请一次，重复邀请请
    使用通用邀请函」含「邀请」，旧逻辑把正文当按钮点，页面原地不动。现在只认
    短按钮名并排除这些说明文字。

    2026-11-07 13:20 当帧再修正：点角色卡后页面根本不跳页——当帧就是角色选择页
    本身（首位角色卡已打勾），真正的前进按钮是右下「喝茶」圆钮。它是竖排艺术字，
    当帧 OCR 15 个文字区域里完全没有它，OCR 候选基本命不中；所以在确认仍停留
    角色选择页（TEA_SELECT_MARKERS 在场）时按 TEA_SELECT_DRINK_RATIO 兜底点
    一下，与休息室「午后茶憩」入口的 OCR 优先/比例兜底同模式。快检不过就什么都不
    点交给调用方按失败处理，避免在未知页面盲点把页面退掉。

    返回是否已确认进入场景或已点了推进入口（邀请按钮 / 选择页「喝茶」）。
    """
    # 点角色卡可能直接把镜头切进「关注特写」（2026-10-26 12:50 同款），先退掉再找入口
    _exit_focus_if_present(logger, hwnd, stop_check=stop_check)
    if _wait_page_markers(logger, hwnd, TEA_SCENE_MARKERS, timeout=8.0, stop_check=stop_check):
        return True
    if _stopped(stop_check):
        return False
    hit = _ocr_best_match(
        logger,
        TEA_INVITE_TEXTS,
        hwnd,
        min_conf=0.55,
        exclude=TEA_INVITE_EXCLUDE_TEXTS,
    )
    if hit and not _invite_hit_usable(hit):
        logger.warning(
            f"[白荆回廊·日常] 拒绝把说明文字当邀请按钮: {hit.get('text')!r} @ {hit.get('center')}"
        )
        hit = None
    if hit:
        _pyautogui_click_guarded(*hit["center"])
        logger.info(f"[白荆回廊·日常] 角色详情页点击 {hit['text']!r} @ {hit['center']}")
        time.sleep(1.5)
        return True
    if _wait_page_markers(
        logger, hwnd, TEA_SELECT_MARKERS, timeout=2.0, stop_check=stop_check
    ):
        # 仍停在角色选择页：点卡只是打勾选中，差的是右下「喝茶」确认（13:20 实锤）
        _click_ratio(logger, hwnd, TEA_SELECT_DRINK_RATIO, "角色选择页右下「喝茶」")
        time.sleep(1.5)
        return True
    logger.warning(
        "[白荆回廊·日常] 点角色卡后未见茶憩场景，也没找到「邀请/开始茶憩/喝茶」类按钮"
    )
    return False




def _exit_focus_if_present(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """链路撞见「关注特写」就点右上「退出关注」关掉（2026-11-07 用户指路）。

    茶憩处理器自己的等待/推进循环不查场景登记表；11-06 09:15 实锤：链路停在
    特写镜头里干等茶憩纪念函直到失败——特写帧上「退出关注」(0.80) 明晃晃挂着
    没人点。判定口径与登记表「关注特写」规则完全一致（FOCUS_MODE_REGION 限定
    右上角，防止把别处的同名词当特写）。
    """
    hit = _pick_result(
        logger, hwnd, _scene_results(logger, hwnd),
        FOCUS_MODE_MARKERS, frac_region=FOCUS_MODE_REGION, min_conf=0.55,
    )
    if not hit:
        return False
    _pyautogui_click_guarded(*hit["center"])
    logger.info(f"[白荆回廊·日常] 点「退出关注」退出特写 @ {hit['center']}")
    time.sleep(1.5)
    return True




def _retry_tea_entry_after_focus(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """退出关注特写后按休息室路径重找一次茶憩入口（退出特写只会回到休息室全景）。

    2026-11-06 09:15 链路实锤：休息室 →「午后茶憩」→ 角色卡之后画面进了特写，
    场景等待/两杯喝茶全部空转。只退特写不重找入口的话，后面照样等不到场景。
    """
    entry_hit = _ocr_best_match(logger, ("午后茶憩",), hwnd, min_conf=0.55)
    if entry_hit:
        _pyautogui_click_guarded(*entry_hit["center"])
        logger.info(f"[白荆回廊·日常] 点击休息室「午后茶憩」文字 @ {entry_hit['center']}")
    else:
        _click_ratio(logger, hwnd, REST_ROOM_TEA_ENTRY, "休息室「午后茶憩」立牌卡")
    time.sleep(2.0)
    if _wait_page_markers(
        logger, hwnd, TEA_SELECT_MARKERS, timeout=TEA_SELECT_TIMEOUT_SEC, stop_check=stop_check
    ):
        _click_ratio(logger, hwnd, TEA_FIRST_CHARACTER, "首个角色卡")
        if _nudge_into_tea_scene(logger, hwnd, stop_check=stop_check):
            return True
    # 点「午后茶憩」后可能直接进场景（无角色选择页）
    return _wait_page_markers(
        logger, hwnd, TEA_SCENE_MARKERS, timeout=TEA_SCENE_TIMEOUT_SEC, stop_check=stop_check
    )




def _handle_tea_break_task(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """午后茶憩任务：前往→确定→任选角色→喝茶→再来一杯→摘要→返回→确定→主界面。

    2026-10-18 用户截图实测流程：点任务行「前往」后弹「是否前往茶憩?」确认框，
    确定后进入角色选择页（今日可邀份额），点任意角色进入茶憩场景；场景内喝茶，
    等待一段时间后再来一杯；茶憩结束出摘要，随意点击关闭；回到休息室后点左上
    「返回」，在「将离开休息室」确认框点「确认」，回到主界面。
    喝茶/再来一杯是场景内动态出现的交互（截图未捕捉到按钮形态），用 OCR 轮询
    文字点击；若轮询不到只记录警告，不盲点 3D 场景。
    """
    # 点角色卡后的补脚结果：True=已进场景/已点邀请，False=两样都没有，None=没走角色卡分支
    nudge_ok = None
    # 1) 任务行内「前往」（任务名含「午后茶」，OCR 常把"歇"认成"憩"，取稳定前缀）
    goto_pos = _find_row_button(logger, hwnd, "午后茶", "前往", stop_check=stop_check)
    if not goto_pos:
        return False
    _pyautogui_click_guarded(goto_pos[0], goto_pos[1])
    logger.info(f"[白荆回廊·日常] 点击午后茶憩任务「前往」@ {goto_pos}")
    # 2) 「是否前往茶憩?」→ 确定
    if _wait_page_markers(logger, hwnd, TEA_CONFIRM_MARKERS, timeout=6.0, stop_check=stop_check):
        _click_ratio(logger, hwnd, TEA_CONFIRM_OK, "「确定」（前往茶憩）")
        time.sleep(1.5)
    else:
        logger.warning("[白荆回廊·日常] 未见「是否前往茶憩」确认框（可能已直接进入）")
    # 3) 角色选择页 → 点首个角色
    # 加载可能很慢（2026-10-20 实测加载条 47%→99% 超过 20s），等待期间不要
    # 调 _return_to_task_panel，否则会把“还在加载”当成“没进去”并点返回取消。
    logger.info(
        f"[白荆回廊·日常] 等待茶憩角色选择页（最长 {TEA_SELECT_TIMEOUT_SEC:.0f}s，含游戏加载）"
    )
    role_page = _wait_page_markers(
        logger, hwnd, TEA_SELECT_MARKERS, timeout=TEA_SELECT_TIMEOUT_SEC, stop_check=stop_check
    )
    if role_page:
        _click_ratio(logger, hwnd, TEA_FIRST_CHARACTER, "首个角色卡")
        nudge_ok = _nudge_into_tea_scene(logger, hwnd, stop_check=stop_check)
    elif _in_rest_room(logger, hwnd):
        # 2026-10-25 19:50 实锤：点「确定」后有时直接进休息室 3D 场景。
        # 2026-10-26 12:50 进一步实锤：休息室里要点右下「午后茶憩」立牌卡；
        # 旧实现点左下第一个角色头像，进入的是「关注」特写镜头，茶憩场景永远等不到。
        logger.info("[白荆回廊·日常] 检测到休息室，点击右下「午后茶憩」入口")
        # OCR 优先点卡片上的「午后茶憩」文字：10-29 20:04 固定比例点击后没进茶憩，
        # 房间布局随角色/镜头变化时比例会偏；识别不到文字再退回固定比例。
        entry_hit = _ocr_best_match(logger, ("午后茶憩",), hwnd, min_conf=0.55)
        if entry_hit:
            _pyautogui_click_guarded(*entry_hit["center"])
            logger.info(f"[白荆回廊·日常] 点击休息室「午后茶憩」文字 @ {entry_hit['center']}")
        else:
            _click_ratio(logger, hwnd, REST_ROOM_TEA_ENTRY, "休息室「午后茶憩」立牌卡")
        time.sleep(2.0)
        role_page = _wait_page_markers(
            logger, hwnd, TEA_SELECT_MARKERS, timeout=TEA_SELECT_TIMEOUT_SEC, stop_check=stop_check
        )
        if role_page:
            _click_ratio(logger, hwnd, TEA_FIRST_CHARACTER, "首个角色卡")
            nudge_ok = _nudge_into_tea_scene(logger, hwnd, stop_check=stop_check)
        else:
            logger.warning("[白荆回廊·日常] 休息室点「午后茶憩」后未见角色选择页（可能直接进场景）")
    else:
        logger.warning(
            f"[白荆回廊·日常] {TEA_SELECT_TIMEOUT_SEC:.0f}s 内未进入角色选择页/休息室"
        )
        # 失败时落截图 + 当帧 OCR 文字，下次从 flow_failures 直接看真实页面。
        _record_step_failure(
            logger, hwnd, "午后茶憩",
            f"{TEA_SELECT_TIMEOUT_SEC:.0f}s 内未进入角色选择页/休息室",
        )
        _return_to_task_panel(logger, hwnd, stop_check=stop_check)
        return False
    # 4) 茶憩场景 → 喝茶循环
    if nudge_ok is False:
        # 点了角色卡，但既没进场景、也没找到「邀请」入口、也没能在选择页点「喝茶」：
        # 页面停在登记表之外的地方。2026-11-03 实锤——这时按老逻辑点底部
        # (0.50,0.92) 会把页面退回休息室，然后两轮「喝茶」轮询全空。这里再给 10s
        # 兜底，还进不去就落证据收工。
        # 2026-11-07：特写在场时等不到场景，先退特写并按休息室路径重找一次入口
        # （2026-11-06 09:15 实锤：特写帧右上「退出关注」(0.80) 没人点，链路干等
        # 到失败——用户指路「点那个 X 就关掉了」）。
        if not _wait_page_markers(
            logger, hwnd, TEA_SCENE_MARKERS, timeout=10.0, stop_check=stop_check
        ):
            if not (
                _exit_focus_if_present(logger, hwnd, stop_check=stop_check)
                and _retry_tea_entry_after_focus(logger, hwnd, stop_check=stop_check)
            ):
                logger.warning("[白荆回廊·日常] 角色卡点击后仍未进入茶憩场景，跳过盲点推进")
                _record_step_failure(
                    logger, hwnd, "午后茶憩",
                    "点角色卡后停在未知页，未找到「邀请/开始茶憩/喝茶」推进入口",
                )
                _return_to_task_panel(logger, hwnd, stop_check=stop_check)
                return False
    scene_seen = _wait_page_markers(
        logger, hwnd, TEA_SCENE_MARKERS, timeout=TEA_SCENE_TIMEOUT_SEC, stop_check=stop_check
    )
    if not scene_seen and _exit_focus_if_present(logger, hwnd, stop_check=stop_check):
        # 场景等待期间画面在「关注特写」里（11-06 09:15 同款）：退掉特写回休息室
        # 全景后，重走一遍「午后茶憩 → 角色卡」入口，进不去按原口径失败
        scene_seen = _retry_tea_entry_after_focus(logger, hwnd, stop_check=stop_check)
    if not scene_seen:
        logger.warning("[白荆回廊·日常] 未检测到茶憩场景（茶憩纪念函）")
    topic_clicked = False
    for rnd in range(1, TEA_DRINK_ROUNDS + 1):
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        # 4a) 话题菜单兜底：出现则点第一个话题（每次茶憩仅能选一项，只点一次）
        if not topic_clicked and _ocr_best_match(logger, TEA_TOPIC_MARKERS, hwnd, min_conf=0.6):
            _click_ratio(logger, hwnd, TEA_FIRST_TOPIC, "第一个话题")
            topic_clicked = True
            time.sleep(1.0)
        # 4b) 推进对话（点对话框区域），同时轮询喝茶按钮
        drink_hit = None
        deadline = time.time() + TEA_DRINK_POLL_SEC
        while time.time() < deadline:
            if _stopped(stop_check):
                return False
            hwnd = _ensure_window(logger, hwnd)
            if not hwnd:
                return False
            drink_hit = _ocr_best_match(logger, TEA_DRINK_TEXTS, hwnd, min_conf=0.55)
            if drink_hit:
                break
            # 摘要已出现 → 不用再喝
            if _ocr_best_match(logger, TEA_SUMMARY_MARKERS, hwnd, min_conf=0.55):
                break
            if _exit_focus_if_present(logger, hwnd, stop_check=stop_check):
                continue    # 特写镜头里不盲点对话框（点了只会乱聚焦/进出特写）
            # 盲点推进只允许发生在茶憩场景里（2026-11-07 根因收口）：
            # 场景没确认就只轮询不点——11-03 实锤盲点把角色详情页点回休息室，
            # 11-06 实锤之后 100 秒在错误页面每 2.5s 盲点一次，12 次失败 10 次
            # 死在休息室、2 次点中场景里的角色/家具停在「关注特写」
            in_scene = scene_seen or bool(
                _ocr_best_match(logger, TEA_SCENE_MARKERS, hwnd, min_conf=0.55)
            )
            if not in_scene:
                time.sleep(2.5)
                continue
            _click_ratio(logger, hwnd, TEA_DIALOGUE_CLICK, "对话框（推进对话）")
            time.sleep(2.5)
        if drink_hit:
            _pyautogui_click_guarded(*drink_hit["center"])
            logger.info(f"[白荆回廊·日常] 第 {rnd} 杯：点击 {drink_hit['text']!r} @ {drink_hit['center']}")
            time.sleep(2.0)
        else:
            logger.warning(f"[白荆回廊·日常] 第 {rnd} 杯：未轮询到喝茶按钮（可能按钮形态与文字不符）")
        # 4c) 每杯之间等待一段时间（用户：等待一段时间后再来一杯）
        if rnd < TEA_DRINK_ROUNDS:
            time.sleep(TEA_DRINK_WAIT_SEC)
    # 5) 等茶憩结束摘要 → 随意点击关闭
    summary_seen = _wait_page_markers(
        logger, hwnd, TEA_SUMMARY_MARKERS, timeout=45.0, stop_check=stop_check
    )
    if not summary_seen:
        # 未自动结束则尝试点「结束茶憩」
        end_hit = _ocr_best_match(logger, ("结束茶憩",), hwnd, min_conf=0.55)
        if end_hit:
            _pyautogui_click_guarded(*end_hit["center"])
            logger.info(f"[白荆回廊·日常] 点击「结束茶憩」@ {end_hit['center']}")
            if _wait_page_markers(
                logger, hwnd, TEA_SUMMARY_MARKERS, timeout=15.0, stop_check=stop_check
            ):
                summary_seen = True
    if _ocr_best_match(logger, TEA_SUMMARY_MARKERS, hwnd, min_conf=0.55):
        summary_seen = True
        _click_ratio(logger, hwnd, TEA_SUMMARY_CLICK, "摘要区域（点击退出）")
        time.sleep(2.0)
    # 6) 回休息室 → 返回 → 确认 → 主界面
    # 2026-10-25 假成功修复：返回任务面板成功 ≠ 茶憩完成。19:06 那轮「确认」后
    # 既没见过场景也没见过摘要，仅因 _return_to_task_panel 成功就被上层记成功，
    # 而游戏内实际仍是 0/1。这里必须用场景/摘要作为证据，失败就返回 False；
    # 上层也不再乐观写 done，最终以任务列表重扫为准。
    has_evidence = scene_seen or summary_seen
    if not has_evidence:
        logger.warning("[白荆回廊·日常] 茶憩未出现场景/结束摘要，任务状态未确认")
        _record_step_failure(
            logger, hwnd, "午后茶憩",
            "未检测到茶憩场景/结束摘要，任务状态未确认",
        )
    returned = _return_to_task_panel(logger, hwnd, stop_check=stop_check)
    return bool(returned and has_evidence)




def _claim_leaf_vein_rewards(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """叶脉联结页领取：点左侧 tab → 任务总览 → 全部领取 → 关奖励弹窗。

    2026-10-18 用户截图实测：日常 tab 处理完毕后，任务面板左侧还有「叶脉联结」
    tab（本日/本周/本期任务，如「日常任务累计活跃度达到100」也有奖励），
    底栏有「任务总览」和「全部领取」。按用户流程：切到叶脉联结 → 任务总览 →
    全部领取。奖励与日常 tab 不互通，漏领会丢每日联结经验。
    """
    if _stopped(stop_check):
        return False
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        return False
    # 0) 自愈入口（2026-10-25 实锤）：此前默认 process_task_list 结束时面板还开着，
    #    但领取循环/重扫的任何闪失都可能把面板关掉——叶脉在错误界面上找 tab 永远
    #    找不到，比例兜底一点反而回主界面，联结奖励直接漏领。先确认面板在屏
    #    （在屏是零成本快检，不在屏则从主界面重开）。
    if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
        logger.warning("[白荆回廊·日常] 叶脉联结前无法确认任务面板在屏，跳过联结领取")
        return False
    # 1) 点左侧「叶脉联结」tab（限定 tab 列，排除底部 VEINOS 图标）
    tab = _ocr_best_match(
        logger, LEAF_TAB_TEXTS, hwnd, frac_region=LEAF_TAB_REGION, min_conf=0.45
    )
    entered = False
    if tab:
        _pyautogui_click_guarded(*tab["center"])
        logger.info(f"[白荆回廊·日常] 点击「叶脉联结」tab @ {tab['center']}")
        entered = _wait_page_markers(
            logger, hwnd, LEAF_PAGE_MARKERS, timeout=8.0, stop_check=stop_check
        )
    else:
        logger.warning("[白荆回廊·日常] OCR 未找到「叶脉联结」tab，尝试比例兜底")
    # 2) OCR 未命中或点击未生效 → 按实测 tab 位置比例兜底
    if not entered:
        _click_ratio(logger, hwnd, LEAF_TAB_FALLBACK, "「叶脉联结」tab（比例兜底）")
        entered = _wait_page_markers(
            logger, hwnd, LEAF_PAGE_MARKERS, timeout=8.0, stop_check=stop_check
        )
    if not entered:
        logger.warning("[白荆回廊·日常] 未进入叶脉联结页，跳过联结领取")
        return False
    time.sleep(1.0)
    # 3) 任务总览（OCR 优先，比例兜底）
    hit = _ocr_best_match(logger, ("任务总览",), hwnd, min_conf=0.55)
    if hit:
        _pyautogui_click_guarded(*hit["center"])
        logger.info(f"[白荆回廊·日常] 点击「任务总览」@ {hit['center']}")
    else:
        _click_ratio(logger, hwnd, LEAF_OVERVIEW_BTN, "「任务总览」（比例兜底）")
    time.sleep(1.5)
    # 4) 全部领取（OCR 优先，比例兜底）
    hit = _ocr_best_match(logger, ("全部领取",), hwnd, min_conf=0.55)
    if hit:
        _pyautogui_click_guarded(*hit["center"])
        logger.info(f"[白荆回廊·日常] 点击「全部领取」@ {hit['center']}")
    else:
        _click_ratio(logger, hwnd, LEAF_CLAIM_ALL_BTN, "「全部领取」（比例兜底）")
    time.sleep(2.0)
    # 5) 领取后可能出奖励弹窗，关掉（最多两轮）
    for _ in range(2):
        if _stopped(stop_check):
            break
        if not _dismiss_overlay(logger, hwnd):
            break
        time.sleep(0.8)
    logger.info("[白荆回廊·日常] 叶脉联结领取步骤完成")
    return True




def _click_quick_battle_entry(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """关卡页点「快速战斗」：优先点 OCR 命中的按钮文字，找不到才退回固定比例。

    2026-11-03 实锤（定向物资保障）：固定比例 (0.784,0.893) 点下去弹的是
    「进入关卡/行动力」确认框，不是快速战斗设置窗 —— 同一天另外三个关卡用同一
    比例都正常，说明各关卡页布局不一致，比例会压到邻居按钮上。OCR 既然认得出
    「快速战斗」文字，就直接点它的 center（注意只会命中关卡页上的那个按钮，
    设置弹窗此时还没出现）。
    返回 True 表示按 OCR 坐标点的，False 表示走了比例兜底。
    """
    if _stopped(stop_check):
        return False
    hit = _ocr_best_match(
        logger,
        IMAGING_QUICK_BATTLE_TEXTS,
        hwnd,
        frac_region=IMAGING_QUICK_BATTLE_REGION,
        min_conf=0.55,
    )
    if hit:
        _pyautogui_click_guarded(*hit["center"])
        logger.info(f"[白荆回廊·日常] 点击关卡页「快速战斗」[ocr] @ {hit['center']}")
        return True
    _click_ratio(logger, hwnd, IMAGING_QUICK_BATTLE_BTN, "「快速战斗」（关卡页，比例兜底）")
    return False




def _handle_stage_battle_task(
    logger: logging.Logger,
    hwnd,
    row_keyword: str,
    task_label: str,
    battle_count: int = 1,
    stop_check=None,
) -> bool:
    """关卡战斗类任务通用流程：前往 → 关卡页快速战斗 → 弹窗再点快速战斗 → 结算随意点 → ESC 返回。

    2026-10-18 用户截图实测（显影行动）：点任务行「前往」进入关卡选择页，
    点右下「快速战斗」弹出「快速战斗设置」窗（指定次数默认 1），再点窗内蓝色
    「快速战斗」开始战斗；出任务完成结算页后随意点击关闭，然后 ESC 逐层返回。
    用户确认自由贸易/定向物资保障/光刻协议操作与此相同，故按行关键词参数化复用。
    用户叮嘱：任务完成后若不知道页面到哪了，就 ESC 返回（_return_to_task_panel
    的 ESC 兜底即为此设计）。
    """
    # 1) 任务行内「前往」（找不到会自动滚回顶部再逐屏下滚重试）
    goto_pos = _find_row_button(logger, hwnd, row_keyword, "前往", stop_check=stop_check)
    if not goto_pos:
        return False
    _pyautogui_click_guarded(goto_pos[0], goto_pos[1])
    logger.info(f"[白荆回廊·日常] 点击{task_label}任务「前往」@ {goto_pos}")
    # 2) 等关卡选择页
    if not _wait_page_markers(logger, hwnd, IMAGING_STAGE_MARKERS, timeout=10.0, stop_check=stop_check):
        logger.warning(f"[白荆回廊·日常] 未进入{task_label}关卡页")
        return _return_to_task_panel(logger, hwnd, stop_check=stop_check)
    time.sleep(1.0)
    # 3) 第一次点「快速战斗」→ 设置弹窗（OCR 定位优先，比例兜底）
    _click_quick_battle_entry(logger, hwnd, stop_check=stop_check)
    # 4) 等弹窗 → 第二次点「快速战斗」确认（精确匹配优先，不会误点弹窗标题）
    dialog_seen = _wait_page_markers(
        logger, hwnd, IMAGING_DIALOG_MARKERS, timeout=6.0, stop_check=stop_check
    )
    if not dialog_seen:
        # 2026-11-03 实锤（物资保障）：点「快速战斗」后没弹设置窗，弹的是带
        # 「确认使用」的行动力/体力补充确认框（用户判断：体力不足时游戏自动
        # 推荐限时行动力药，限时的可以直接用）。旧逻辑在这里按比例盲点
        # (0.617,0.799)，再把当前页当结算页点掉 → 这一场根本没开打，
        # 连带「定向物资保障」0/1（-5）和「完成5次战斗」停在 4/5（-20）。
        # 新逻辑：先确认使用补充道具，再重新等设置窗；仍然等不到就回任务面板，
        # 按失败取证（flow_failures + OCR 标注图）留档，绝不盲点、也不谎报完成。
        if _confirm_action_power_use(logger, hwnd, stop_check=stop_check):
            dialog_seen = _wait_page_markers(
                logger, hwnd, IMAGING_DIALOG_MARKERS, timeout=5.0, stop_check=stop_check
            )
            if not dialog_seen:
                # 补完行动力一般退回关卡页，需要再点一次「快速战斗」
                _click_quick_battle_entry(logger, hwnd, stop_check=stop_check)
                dialog_seen = _wait_page_markers(
                    logger, hwnd, IMAGING_DIALOG_MARKERS, timeout=6.0, stop_check=stop_check
                )
        if not dialog_seen:
            logger.warning(
                "[白荆回廊·日常] 仍未出现快速战斗设置窗，不再盲点，回任务面板复核"
            )
            # 走统一失败取证：截图 + 当帧 OCR 文字写进 flow_failures，
            # 截图会附一份 OCR 标注版（框 + 文字 + 置信度），供事后定位。
            _record_step_failure(
                logger, hwnd, f"{task_label}·快速战斗",
                "未见「快速战斗设置」弹窗（已尝试确认使用行动力/道具），未开打，回任务面板复核",
            )
            _return_to_task_panel(logger, hwnd, stop_check=stop_check)
            # 关键：没有开打的证据就不能报成功，否则上层会写「任务已完成」，
            # 任务行仍停在 0/1，下一轮又被漏掉（今天物资保障就是这样丢的 5 分）。
            return False
    _capture_stage_battle_dialog(logger, hwnd)
    if battle_count > 1:
        # 「指定次数」默认 1，点右侧 + 递增；每轮最多 5 次，最多点 4 下
        for _ in range(min(int(battle_count) - 1, 4)):
            _click_ratio(logger, hwnd, IMAGING_DIALOG_PLUS_BTN, "指定次数 +1")
            time.sleep(0.35)
        confirmed = _ocr_best_match(
            logger,
            (str(int(battle_count)),),
            hwnd,
            frac_region=IMAGING_DIALOG_COUNT_REGION,
            min_conf=0.5,
        )
        if confirmed:
            logger.info(f"[白荆回廊·日常] 指定次数已设置为 {int(battle_count)}")
        else:
            logger.warning(
                f"[白荆回廊·日常] 未确认到指定次数={int(battle_count)}，按当前设置继续"
            )
    hit = _ocr_best_match(logger, ("快速战斗",), hwnd, min_conf=0.6)
    if hit:
        _pyautogui_click_guarded(*hit["center"])
        logger.info(f"[白荆回廊·日常] 点击弹窗内「快速战斗」确认 @ {hit['center']}")
    else:
        _click_ratio(logger, hwnd, IMAGING_DIALOG_OK_BTN, "「快速战斗」确认（弹窗，比例兜底）")
    # 5) 等战斗结算页 → 随意点击（战斗播放 + 结算加载预留时间）
    time.sleep(6.0)
    if _wait_page_markers(logger, hwnd, IMAGING_SETTLEMENT_MARKERS, timeout=15.0, stop_check=stop_check):
        logger.info("[白荆回廊·日常] 检测到战斗结算页")
    else:
        logger.info("[白荆回廊·日常] 未匹配到结算页特征文字，按用户流程仍随意点击关闭")
    _click_ratio(logger, hwnd, IMAGING_ANYWHERE_CLICK, "结算页任意位置（随意点击）")
    time.sleep(2.0)
    # 6) ESC 逐层返回任务面板
    return _return_to_task_panel(logger, hwnd, stop_check=stop_check)




def _enter_friend_page_from_main(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """主界面左上角好友图标 → 好友页（图标无文字，按实测比例点击）。"""
    _click_ratio(logger, hwnd, FRIEND_ENTRY_FALLBACK, "好友入口图标（左上第 2 个功能图标）")
    time.sleep(2.0)
    if _wait_page_markers(logger, hwnd, FRIEND_PAGE_MARKERS, timeout=6.0, stop_check=stop_check):
        return True
    _dismiss_overlay(logger, hwnd)
    _click_ratio(logger, hwnd, CLUB_BLANK_CLICK, "好友入口弹窗空白继续")
    time.sleep(1.5)
    return _wait_page_markers(logger, hwnd, FRIEND_PAGE_MARKERS, timeout=6.0, stop_check=stop_check)




def _handle_friend_gift_popup(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """关闭「一键领取/赠送」后的弹窗；返回是否看到成功弹窗（无失败字样）。"""
    popup_seen = False
    failed = False
    for _ in range(3):
        if _stopped(stop_check):
            break
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            break
        # 只在弹窗区域找失败提示，避免好友签名里的“失败/不足”误伤
        try:
            results = ocr_window_content(logger, hwnd=hwnd)
            region = _abs_region(hwnd, FRIEND_POPUP_TEXT_REGION)
            if region:
                ax0, ay0, aw, ah = region
                results = [
                    r for r in results
                    if r.get("center")
                    and ax0 <= r["center"][0] <= ax0 + aw
                    and ay0 <= r["center"][1] <= ay0 + ah
                ]
            joined = "".join(_normalize_text(str(r.get("text") or "")) for r in results)
            if any(_normalize_text(k) in joined for k in FRIEND_POPUP_FAILURE_TEXTS):
                failed = True
                logger.warning("[白荆回廊·日常] 好友赠礼弹窗出现失败提示，赠送可能未成功")
        except Exception as exc:
            logger.debug("[白荆回廊·日常] 好友赠礼弹窗识别失败: %s", exc)
        if _dismiss_overlay(logger, hwnd):
            popup_seen = True
            time.sleep(0.8)
            continue
        hit = _ocr_best_match(
            logger, FRIEND_POPUP_TEXTS, hwnd,
            frac_region=FRIEND_POPUP_REGION, min_conf=0.55,
        )
        if hit:
            popup_seen = True
            _pyautogui_click_guarded(*hit["center"])
            logger.info(f"[白荆回廊·日常] 好友赠礼弹窗点击 {hit['text']!r} @ {hit['center']}")
            time.sleep(1.0)
            continue
        if _ocr_best_match(logger, FRIEND_PAGE_MARKERS, hwnd, min_conf=0.55):
            break
        # 识别不到弹窗按钮，按 ESC 收尾（会顺带退出好友页）
        _focus_game_window(logger, hwnd)
        pyautogui.press("escape")
        popup_seen = True
        logger.info("[白荆回廊·日常] 好友赠礼弹窗按 ESC 关闭")
        time.sleep(1.0)
    return popup_seen and not failed




def _do_friend_medal_on_page(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """在好友页点「一键领取/赠送」→ 处理弹窗 → 回日常任务面板。"""
    btn = _ocr_best_match(logger, FRIEND_GIFT_TEXTS, hwnd, min_conf=0.55)
    if not btn:
        logger.warning("[白荆回廊·日常] 好友界面未找到「一键领取/赠送」")
        _return_to_task_panel(logger, hwnd, stop_check=stop_check)
        return False
    _pyautogui_click_guarded(*btn["center"])
    logger.info(f"[白荆回廊·日常] 点击「一键领取/赠送」@ {btn['center']}")
    time.sleep(2.0)
    gift_ok = _handle_friend_gift_popup(logger, hwnd, stop_check=stop_check)
    if not gift_ok:
        logger.warning("[白荆回廊·日常] 未确认到好友赠礼成功弹窗（任务状态以重扫为准）")
    return _return_to_task_panel(logger, hwnd, stop_check=stop_check) and gift_ok




def _handle_friend_medal_task(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """友情勋章任务：任务行「前往」优先，失败再从主界面左上角好友入口进入。"""
    goto_pos = _find_row_button(logger, hwnd, "友情勋章", "前往", stop_check=stop_check)
    if goto_pos:
        _pyautogui_click_guarded(goto_pos[0], goto_pos[1])
        logger.info(f"[白荆回廊·日常] 点击友情勋章任务「前往」@ {goto_pos}")
        if _wait_page_markers(
            logger, hwnd, FRIEND_PAGE_MARKERS, timeout=8.0, stop_check=stop_check
        ):
            return _do_friend_medal_on_page(logger, hwnd, stop_check=stop_check)
        logger.warning(
            "[白荆回廊·日常] 友情勋章「前往」未进入好友界面，改用主界面左上角好友入口"
        )
        _return_to_task_panel(logger, hwnd, stop_check=stop_check)
    # 主界面入口：先关任务列表 → 点左上角好友图标 → 完成后 _return_to_task_panel 重开列表
    if not close_task_list(logger, hwnd, stop_check=stop_check):
        logger.warning("[白荆回廊·日常] 无法关闭任务列表进入主界面，友情勋章跳过")
        return False
    if not _enter_friend_page_from_main(logger, hwnd, stop_check=stop_check):
        logger.warning("[白荆回廊·日常] 未进入好友界面")
        _return_to_task_panel(logger, hwnd, stop_check=stop_check)
        return False
    return _do_friend_medal_on_page(logger, hwnd, stop_check=stop_check)


