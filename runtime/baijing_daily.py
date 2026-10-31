"""白荆回廊视觉版日常流程（2026-10-18 重构）。

替代旧的固定坐标序列回放（click_sequences/白荆回廊-日常.json：100+ 步录制坐标，
录于游戏版本 1.0.2.1082；UI 改版后坐标整体失效，且回放窗口相对坐标时窗口布局
一变就整体偏移，失败只能整段重来）。

新流程（起点：主界面，由 run_baijing_ingame_flow 抵达）：
  1. _ensure_main_ui      确认/恢复到主界面（复用 baijing_flow.detect_baijing_main_ui）
  2. open_daily_task_list 点击右侧「任务」按钮（OCR 优先 + 模板兜底），
                           等待日常任务列表打开（OCR 特征文字 + 快照留证）
  3. process_task_list    列表内操作：一键领取 → 逐个领取；
                           「前往」类未完成任务只记录不点击（跳转子活动需要
                           专门逻辑，见 _handle_goto_tasks 说明）
  4. close_task_list      关闭任务列表（关闭按钮/ESC），确认回到主界面
  4.5. claim_club_daily_reward  活跃度 100 里程碑领完之后顺手领俱乐部每日任务奖励
                           （主界面左上第 4 个图标进俱乐部 → 弹窗点空白继续 →
                           点「俱乐部每日任务」卡片，卡片变「今日任务已完成」）
  5. buy_daily_free_gift_pack  回主界面后顺手领有猫零售的每日免费礼包
                           「每日外勤赠礼」（底栏「有猫零售」→ 精选礼包 →
                           有猫精选 → 卡底「免费」→ 弹窗「购买」）

识别策略：
  - 「任务」按钮用 RapidOCR 文字识别优先。注意主界面中部有「阶段任务154/200」
    也含「任务」二字，全屏搜索会误点，因此全窗识别后按窗口右侧区域
    （TASK_BUTTON_REGION，fx>=0.70）过滤命中，并对命中文本做黑名单排除。
    不要「先裁剪区域再 OCR」——实测裁剪会改变检测结果（右侧裁剪后
    「一任务」直接丢失）。
  - 模板兜底：模板/白荆回廊-任务按钮.png（按 1920x1080 参考分辨率裁剪，
    红色角标已 inpaint 抹除，避免每日数字变化导致失配）。匹配时按窗口宽度
    换算缩放因子，适配不同分辨率。
  - 全程不写死绝对坐标，点击坐标一律来自 OCR/模板匹配结果。

进度记录：结果写 daily_progress（game_key=baijing），与旧序列版一致。
日常任务目录（2026-10-18 实测提取，会随游戏版本漂移）用于滚动扫描匹配
(X/Y) 进度；**活跃度以每日任务面板顶部的「累计活跃度」大数字为准**，
目录分值相加只作读不到面板时的兜底（2026-10-27 实锤：目录 90、面板 105）。
活跃度口径（2026-10-18 用户确认）：里程碑 20/40/60/80/100 五档，**满 100 即
领完全部日常里程碑奖励**；current 按 100 封顶，原始实得（可超 100）留在
extra.earned，目录核算值留在 extra.activity_catalog。日常 tab 处理完毕后还会切到「叶脉联结」tab 做 任务总览 → 全部领取
（该页奖励与日常 tab 不互通）。
"""

import ctypes
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import pyautogui
import win32api
import win32con
import win32gui
from PIL import Image

from ..settings.paths import BAIJING_VERIFY_DIR, TEMPLATES_DIR, WORKFLOW_DB_PATH
from .baijing_flow import (
    BAIJING_GAME_KEY,
    _baijing_today_key,
    _focus_game_window,
    _record_baijing_task,
    _resolve_baijing_window,
    _window_region,
    detect_baijing_main_ui,
)
from .foreground_flow_runner import _pyautogui_click_guarded
from .ocr_click_tester import _normalize_text, grab_screen, ocr_window_content

# ── 「任务」按钮模板（1920x1080 参考分辨率裁剪，红色角标已抹除） ──
TASK_BUTTON_TEMPLATE = str(TEMPLATES_DIR / "白荆回廊-任务按钮.png")
TEMPLATE_REFERENCE_WIDTH = 1920
TEMPLATE_MATCH_THRESHOLD = 0.82

# 「任务」按钮 OCR 搜索区域（窗口相对比例 fx0, fy0, fx1, fy1）：右侧竖排按钮区。
# 必须限定右侧——主界面中部「阶段任务154/200」也含「任务」二字。
TASK_BUTTON_REGION = (0.70, 0.45, 1.00, 0.92)
# OCR 命中文本黑名单：含这些字样的跳过（防误点「阶段任务」）
TASK_TEXT_EXCLUDE = ("阶段",)

# 日常任务列表打开后的特征文字（任一命中即认为列表已打开）。
# **只放面板独有强特征，且任何候选都不能含「任务」二字**——2026-10-21 实锤：
# 候选里放「日常任务/任务列表/距离任务重置」等含"任务"的词会被 _ocr_best_match
# 的反向子串规则击穿：主界面右下角「任务」按钮文字只有 2 字，规则
# "OCR 文本 ∈ 候选 且 ≥2 字符"让它命中任何含"任务"的长候选，
# _wait_task_list 假阳性判"列表已打开"→ 后续找行全部空转（当天 3/15 根因之一）。
# 「一键领取/全部领取/领取奖励」只在列表面板出现，主界面没有，保留安全。
TASK_LIST_MARKERS = (
    "累计活跃度", "主线指引", "一键领取", "全部领取", "领取奖励",
)
# 列表核心特征：只用于"确认已经从子页面回到列表"。不含「一键领取/领取奖励」这类
# 在弹窗或奖励界面也可能出现的词，避免把别的界面当成列表（2026-10-24）。
TASK_LIST_CORE_MARKERS = ("累计活跃度", "主线指引")
# 列表内领取类按钮（先具体后泛化，防「一键领取」被当成单个「领取」）
CLAIM_ALL_TEXTS = ("一键领取", "全部领取", "一键全领")
CLAIM_TEXTS = ("领取",)
# 搜索单个「领取」时要排除的字样（避免点到「一键领取」上重复计数）。
# 「已领取」是行内**状态文字**不是按钮——2026-10-25 实锤：领取循环把 (750,840) 的
# 「已领取」标签当「领取」点了 3+ 轮，画面毫无变化，白烧轮次（一轮≈14s）。
# 真实按钮文字「领取」「领取奖励」都不含"已领取"子串，排除无副作用。
CLAIM_EXCLUDE = ("一键", "全部", "已领取")
# 「前往」类任务：只记录不点击（跳转子活动需专门逻辑，暂不自动执行）
GOTO_TEXTS = ("前往",)

# ── 日常任务静态目录（2026-10-18 从游戏内实测提取；任务项/分值会随版本漂移） ──
# 2026-10-27 实锤：游戏当天是「完成2次战斗」，目录写的是「完成3次战斗」。
# 目录只用于跑流程和兜底核算，**活跃度记录必须以面板大数字为准**（见
# _read_activity_number）；发现对不上时先看 daily_scan_*_日期.jpg 逐屏快照。
# 每项: key=内部标识, name=任务名, activity=活跃度分值, match=OCR 匹配关键词。
# OCR 实测坑：「午后茶歇」会被识别成「午后茶憩」、「记忆烙痕提升」会被识别成
# 「记忆熔痕提开」，关键词一律取稳定片段。
# 研发室任务的徽章是紫角无数字样式，分值 10 由用户 2026-10-18 确认。
DAILY_TASK_CATALOG: List[Dict] = [
    {"key": "login",              "name": "登录游戏",                              "activity": 5,   "match": ["登录游戏"]},
    # 2026-10-25 实跑截图：日常里同时存在「完成3次战斗」(3/3, 10) 和
    # 「完成5次战斗」(4/5, 20)。2026-10-27 用户指出战斗项每天可能是 2/3/4 次
    # 中的一种（不会同时显示）：当天列表实际是「完成2次战斗 (0/2)」，旧目录
    # 只认「完成3次战斗」→ 匹配不到/靠 OCR 误读成 3/3。这里把 2/3/4 归到
    # 同一个 10 分槽位（key 保留 battle_3 兼容历史数据），5 次战斗仍是独立 20 分。
    # 4 次战斗的分值尚未实测，暂按 10；真实活跃度以面板大数字为准。
    {"key": "battle_3",           "name": "完成2/3/4次战斗",                       "activity": 10,  "match": ["完成2次战斗", "完成3次战斗", "完成4次战斗"]},
    {"key": "battle_5",           "name": "完成5次战斗",                           "activity": 20,  "match": ["完成5次战斗"]},
    {"key": "friend_medal",       "name": "赠送1次友情勋章",                       "activity": 10,  "match": ["友情勋章", "勋章"]},
    {"key": "excellent_road",     "name": "完成1次「卓越道路」",                   "activity": 10,  "match": ["卓越道路"]},
    {"key": "kill_60",            "name": "累计击败60名敌方目标",                  "activity": 10,  "match": ["击败60", "击败"]},
    {"key": "lab_harvest",        "name": "在白荆穹顶的初级研发室或高级研发室收获1次产物", "activity": 10,  "match": ["研发室"]},
    {"key": "tea_break",          "name": "邀请任一同调者共享一次「午后茶歇」",      "activity": 15,  "match": ["午后茶"]},
    {"key": "gallery_tour",       "name": "完成1次回廊漫巡",                       "activity": 20,  "match": ["回廊漫巡", "漫巡"]},
    {"key": "imaging_action",     "name": "完成1次任意难度的「显影行动」",          "activity": 5,   "match": ["显影行动"]},
    {"key": "memory_exp",         "name": "为记忆烙痕提升1次经验",                  "activity": 15,  "match": ["经验"]},
    {"key": "skill_upgrade",      "name": "同调者的任意非异核技能升级1次",          "activity": 10,  "match": ["非异核", "技能升级"]},
    {"key": "free_trade",         "name": "完成1次任意难度的「自由贸易」",          "activity": 5,   "match": ["自由贸易"]},
    {"key": "material_guarantee", "name": "完成1次「定向物资保障」",                "activity": 5,   "match": ["物资保障"]},
    {"key": "litho_protocol",     "name": "完成1次「光刻协议」",                    "activity": 5,   "match": ["光刻协议"]},
    {"key": "restroom_mini_game", "name": "游玩一次休息室中任一小游戏",             "activity": 5,   "match": ["小游戏"]},
]
# 每日活跃度理论总分（2026-10-18 实测：研发室徽章无数字但分值 10，用户确认）。
# 实得可超过领满线（用户实测跑完日常到 130）。
DAILY_ACTIVITY_TOTAL = sum(t["activity"] for t in DAILY_TASK_CATALOG if t["activity"])
# 每日活跃度领满线：里程碑奖励为 20/40/60/80/100 五档，满 100 即领完全部
# 日常里程碑奖励（叶脉联结页「日常任务累计活跃度达到100」任务可印证，2026-10-18
# 用户确认）。进度百分比按 领满线/100 计算，实得超过 100 按 100 记。
DAILY_ACTIVITY_TARGET = 100
# task_key → activity 快速查找
DAILY_ACTIVITY_BY_KEY = {t["key"]: t["activity"] for t in DAILY_TASK_CATALOG}
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
# 烙痕列表页特征（前往落地页：顶部工具栏）
MEMORY_LIST_MARKERS = ("只看红点", "排序与筛选", "按等级", "前往技能列表")
# 详情页「强化烙痕」按钮模板（底部右侧；注意是"烙痕"不是"熔垣"，
# 2026-10-18 用户指正：低分辨率下图样字容易看错。OCR 对这几个字识别也差，用模板）
MEMORY_ENHANCE_BUTTON_TPL = str(TEMPLATES_DIR / "白荆回廊-强化烙痕按钮.png")
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
# 详情页「强化烙痕」按钮的 OCR 兜底区域（右下角）。模板失配时用文字确认，
# OCR 常把「烙痕」认成「熔垣」，两个词都收。
MEMORY_ENHANCE_BTN_REGION = (0.55, 0.75, 1.00, 1.00)
# 「指定等级」行：截图上重置中心 (1189,669)/1919x1079 ≈ (0.620,0.620)，
# OCR 置信度长期只有 0.55（阈值 0.55 边缘），限定区域 + 0.45 阈值降低漏检。
MEMORY_RESET_REGION = (0.55, 0.58, 0.68, 0.66)
MEMORY_ENHANCE_TEXTS = ("强化烙痕", "强化熔垣")
# 「+」按钮相对「重置」的水平偏移（占窗口宽度比例；实测 1425-1190=235px @1919 宽）
PLUS_BUTTON_DX_RATIO = 0.1225

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
TEA_SUMMARY_MARKERS = ("茶憩结束", "点击退出")           # 结束摘要
TEA_DRINK_TEXTS = ("喝茶", "再来一杯", "请喝茶", "干杯")  # 喝茶交互文字（场景内动态出现）
# 关键点击位（窗口相对比例，实测 @1919x1079）
TEA_CONFIRM_OK = (0.623, 0.634)          # 前往确认-确定 (1196,684)
TEA_FIRST_CHARACTER = (0.412, 0.417)     # 角色选择页首个角色卡 (790,450)
TEA_FIRST_TOPIC = (0.753, 0.360)         # 话题菜单第一个话题 (1445,388)
TEA_DIALOGUE_CLICK = (0.50, 0.92)        # 对话框区域（点击推进对话）
TEA_SUMMARY_CLICK = (0.50, 0.60)         # 摘要区域（点击退出）
# 喝茶轮数与每杯间隔（用户流程：喝一次 → 等待 → 再来一杯）
TEA_DRINK_ROUNDS = 2
TEA_DRINK_WAIT_SEC = 10.0
TEA_DRINK_POLL_SEC = 20.0
# 茶憩要经过游戏加载：2026-10-20 实测 ASHECHOES 加载条从 47% 到 99% 超过 20s，
# 原先 10s 的等待会把“还在加载”误判成“没进角色选择页”，随后点返回取消进入。
TEA_SELECT_TIMEOUT_SEC = 45.0
TEA_SCENE_TIMEOUT_SEC = 25.0

# ── 休息室 / 关注特写：ESC 无效的场景态（2026-10-26 实锤修复） ──────────
# 12:50 那轮茶憩掉进休息室后，代码点了左下角色头像 → 进入「关注」特写（画面只剩
# 莫红袖 + 右上「退出关注」），场景/摘要都等不到；随后 _return_to_task_panel 连按
# 5 次 ESC 也出不来（这两个界面都不吃 ESC），任务列表反复重开失败，整轮日常只剩
# 2/16、活跃度 20/100，俱乐部/零售/叶脉联结全部被拖死。
# 用户 12:53 截图给出正确操作：茶憩入口是右下「午后茶憩」立牌卡，离开要点左上
# 「返回」再在确认框点「确认」。坐标按该截图（960x540）实测换算。
REST_ROOM_MARKERS = ("拜访他人", "布置模式")     # 休息室独有；「午后茶憩」任务行里也有，不能当特征
REST_ROOM_TEA_ENTRY = (0.883, 0.830)             # 右下「午后茶憩」立牌卡中心 (848,448)
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
EVENT_DIALOG_MARKERS = ("定巡之礼",)
EVENT_DIALOG_CLOSE = (0.913, 0.239)
# 茶憩 3D 房间：任务流程用「茶憩纪念函」当进场特征，但 10-27 10:22 的当帧
# OCR 只认出房间常驻陈设（飞镖/棒球），纪念函没进识别结果——登记表用并集兜底，
# 任意命中即判茶憩房间（话题名"古典余温"每日轮换，不能当特征）。
TEA_ROOM_REGISTRY_MARKERS = ("茶憩纪念函", "飞镖", "棒球")
# 有猫零售页：10-26 20:10 取证，流程漂进零售页后认不出（当帧：返回｜有猫推荐｜
# 精选礼包｜有猫精选｜每日外勤赠礼）。左侧栏目文字是该页独占，主界面没有。
CAT_RETAIL_MARKERS = ("有猫精选", "精选礼包", "有猫推荐")

# ── 显影行动任务（2026-10-18 用户截图实测） ──
# 流程：任务行「前往」→ 关卡选择页（E-1~E-10）→ 点右下「快速战斗」→
#       「快速战斗设置」弹窗（指定次数默认 1）→ 再点弹窗内「快速战斗」→
#       战斗结算页随意点击 → ESC 逐层返回。
IMAGING_STAGE_MARKERS = ("进入关卡", "行动力")              # 关卡选择页
IMAGING_DIALOG_MARKERS = ("快速战斗设置", "指定次数")        # 快速战斗设置弹窗
IMAGING_SETTLEMENT_MARKERS = ("任务完成", "战斗结算", "结算", "挑战成功", "获得道具")
# 关键点击位（窗口相对比例，实测 @1919x1079）
IMAGING_QUICK_BATTLE_BTN = (0.784, 0.893)   # 关卡页「快速战斗」(1505,963)
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
# ── 有猫零售「每日外勤赠礼」免费礼包（2026-10-18 用户截图实测） ──
# 流程：主界面底栏「有猫零售」→ 左侧「精选礼包」→ 顶部 tab「有猫精选」→
#       首张卡「每日外勤赠礼」点底部「免费」→ 购买弹窗点「购买」→ 关奖励弹窗 → 返回主界面。
# 下方比例/区域全部来自 1919x1079 实测截图 + RapidOCR 探针复核（字串与坐标都对得上）：
#   主界面底栏 有猫零售 (0.712,0.935)｜零售页 返回 (0.102,0.049)｜左列 精选礼包 (0.105,0.217)
#   顶部 tab 有猫精选 (0.670,0.203)（同排还有 活动限时 0.274 / 启航钜惠 0.406 / 里程碑 0.538）
#   首张卡 每日外勤赠礼 (0.269,0.734) / 剩余1件 (0.268,0.769) / 免费 (0.267,0.833)
#   购买弹窗 合计 (0.436,0.626) / 免费 (0.597,0.625) / 购买 (0.606,0.714)
# 注意「免费」在同页有两个：卡片顶部角标 (0.267,0.287，conf 仅 0.54) 与卡底价格按钮
# (0.267,0.833)。只认卡底那个 → 搜索区域限定 fy 0.70~0.90（角标与弹窗里的
# 「免费」(0.597,0.625) 都被排除在外）。
SHOP_ENTRY_TEXTS = ("有猫零售",)
# 主界面底栏区域：夹在「精神深潜」(0.595) 与「活动中心」(0.828) 之间
SHOP_ENTRY_REGION = (0.62, 0.88, 0.80, 1.00)
SHOP_ENTRY_FALLBACK = (0.712, 0.935)
# 零售页特征文字（不用「客服」：主界面左下角有「Q客服」，子串匹配会假命中）
SHOP_PAGE_MARKERS = ("有猫推荐", "精选礼包", "已购礼包自动收起", "X晶体验值")
SHOP_SIDE_TEXTS = ("精选礼包",)
SHOP_SIDE_REGION = (0.0, 0.10, 0.20, 0.95)   # 左侧功能列
SHOP_SIDE_FALLBACK = (0.105, 0.217)
SHOP_TAB_TEXTS = ("有猫精选",)
SHOP_TAB_REGION = (0.20, 0.15, 0.80, 0.26)   # 顶部 tab 行
SHOP_TAB_FALLBACK = (0.670, 0.203)
# 「有猫精选」页首张卡的名字（用它确认 tab 切对了：停在「活动限时」时首卡是「集结共鸣助力」）
SHOP_FREE_PACK_TEXTS = ("每日外勤赠礼",)
# 首张卡区域 + 「免费」按钮搜索区域（含礼包名/剩余/底部价格按钮，不含顶部角标）。
# 整排搜索而非只搜首列：礼包顺序若变化仍能点到卡底价格按钮，fy 与弹窗里的「免费」错开。
SHOP_FREE_BTN_REGION = (0.18, 0.70, 1.00, 0.90)
SHOP_FREE_BTN_TEXTS = ("免费",)
# 已买完时的字样（找不到「免费」时用来区分"已领取"与"没找到"）
SHOP_SOLD_OUT_TEXTS = ("剩余0件", "已售罄", "已购买", "已领完")
# 购买确认弹窗特征（点卡底「免费」后出现）。
# **不要把「购买」当弹窗特征**：买完后卡片按钮变「已购买」，子串匹配会永远命中，
# 弹窗判定永远为"仍在"→ 每次都判失败重试。用弹窗独有的「内容:」「合计」。
SHOP_DIALOG_MARKERS = ("内容", "合计")
SHOP_DIALOG_REGION = (0.15, 0.20, 0.92, 0.80)   # 弹窗覆盖范围
SHOP_DIALOG_BUY_TEXTS = ("购买",)
SHOP_DIALOG_BUY_REGION = (0.35, 0.55, 0.95, 0.85)
SHOP_BUY_FALLBACK = (0.606, 0.707)   # (1162,763)@1919x1079（弹窗截图底部少 11px，按窗口高折算）
# 「最大」购买数量按钮：弹窗内 + 号右侧的蓝色双箭头图标（无文字可 OCR）。
# 2026-10-20 用户反馈——不点它每次都只买 1 件。实测中心 (1368,566)@1919x1079。
# 仅俱乐部商店（baijing_shop）使用；每日免费礼包无数量选择，不要套用。
SHOP_MAX_QTY_FALLBACK = (0.713, 0.524)
# 零售页左上「返回」
SHOP_BACK_REGION = (0.0, 0.0, 0.20, 0.10)
# 打开零售页的最大尝试次数
SHOP_MAX_ATTEMPTS = 2

# ── 俱乐部每日任务（2026-10-18 用户截图实测） ──
# 流程：主界面左上角第 4 个图标（俱乐部）→ 弹「新物品已存入S.E.E.D.仓库」点空白继续 →
#       俱乐部页面点「俱乐部每日任务」卡片领取 → 卡片变「今日任务已完成」→ 返回主界面。
# 下面比例全部来自 1919x1079 实测截图 + RapidOCR 探针复核：
#   入口图标 (0.183,0.047)（图标无文字，靠模板定位，比例兜底；红点已 inpaint 抹除，
#   整图只有一个峰 conf=0.924，不会误点旁边的邮件/好友/铃铛图标）
#   俱乐部页 返回 (0.102,0.049)｜俱乐部每日任务 卡片标题 (0.555,0.294)｜卡片中心 (0.573,0.381)
#   领完后卡片显示「今日任务已完成」(0.573,0.381)
#   页面特征：俱乐部等级 0.103,0.372｜俱乐部首席 0.102,0.578｜共响金 0.087,0.510｜
#             俱乐部活动 0.720,0.292｜俱乐部商店 0.898,0.931｜俱乐部成员 0.563,0.932
CLUB_ENTRY_TEMPLATE = str(TEMPLATES_DIR / "白荆回廊-俱乐部入口.png")
CLUB_ENTRY_REGION = (0.13, 0.00, 0.24, 0.10)   # 左上角图标区（只放第 4 个图标，排掉邻居）
CLUB_ENTRY_FALLBACK = (0.183, 0.047)
# 页面特征：**不要用底部「俱乐部成员/俱乐部商店/俱乐部支援」**——弹「新物品已存入
# 仓库」提示时这三行照样能被 OCR 读到，会把弹窗状态误判成已进页面，后面点卡片会被
# 弹窗挡住。用「弹窗期间认不出的那几个字」当特征（实测 210011 弹窗帧全都没认出来）。
CLUB_PAGE_MARKERS = ("俱乐部每日任务", "俱乐部活动", "俱乐部等级", "俱乐部首席", "共响金")
CLUB_DAILY_TASK_TEXTS = ("俱乐部每日任务",)
CLUB_DONE_TEXTS = ("今日任务已完成",)
CLUB_CLAIM_TEXTS = ("领取",)
CLUB_CARD_REGION = (0.45, 0.25, 0.75, 0.52)   # 每日任务卡片
CLUB_CARD_FALLBACK = (0.573, 0.381)
# 弹窗/加载中的空白推进点（顶部居中空白，避开返回按钮与货币栏）
CLUB_BLANK_CLICK = (0.50, 0.13)
CLUB_BACK_REGION = (0.0, 0.0, 0.20, 0.10)
CLUB_SNAPSHOT_PREFIX = "club_daily"

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

# 任务行区域（窗口相对比例）：右侧列表区，避开左侧 tab/活跃度面板和底部固定栏
TASK_ROW_REGION = (0.42, 0.12, 1.00, 0.80)
# 「累计活跃度」大数字区域（窗口相对比例）：每日任务面板顶部的圆形数字。
# 2026-10-27 实测 480x270 验证图中心 (148,66) → (0.308,0.244)，960x540 列表
# 快照同位置；区域内只有大数字，不会碰到左侧 tab / 下方里程碑奖励卡。
ACTIVITY_NUMBER_REGION = (0.22, 0.15, 0.42, 0.34)
# 面板特征：只有这些字样在屏上才认为大数字来自每日任务面板。
# 防止面板被关掉后，把主界面/其它页面同区域里的数字误当活跃度。
ACTIVITY_PANEL_MARKERS = ("累计活跃度", "主线指引", "距离任务重置", "一键领取")
# 活跃度合理上限：当前版本理论总分 160，留余量；防止把道具图标上的 1000
# 或其它界面数字当成活跃度。
ACTIVITY_NUMBER_MAX = 300
# 面板数字有入场动画（刚打开时会从 0 涨到真实值），多次读取取非零最大值。
ACTIVITY_NUMBER_ATTEMPTS = 3
ACTIVITY_NUMBER_INTERVAL = 0.8
# 左侧 tab 列区域（用于确保停在「日常」页）
TAB_COLUMN_REGION = (0.0, 0.15, 0.28, 0.85)
# 滚动扫描：鼠标悬停位置取列表区中心，每次滚动量与最大滚动次数
# TASK_SCROLL_STEPS 语义 = 滚轮格数（负数向下），经 _wheel_real 逐格发送 ±120
TASK_SCROLL_STEPS = -3
TASK_SCROLL_MAX = 5

# ── 滚轮发送参数（2026-10-19 修复滚动失效） ──
# 根因：pyautogui.scroll(-3) 在 Windows 上等价于
# mouse_event(MOUSEEVENTF_WHEEL, dwData=-3)，而一格滚轮是 WHEEL_DELTA=120——
# 不足 1/40 格，Unreal/Slate 列表积累不到阈值直接不动（2026-10-18/19 连续两天
# 6 屏 OCR 完全相同的实锤）。ok-ef/ok-ww 的可靠做法（参考实现）：
#   1. 发送前激活游戏窗口（try_activate / active_and_send_mouse_delta）
#   2. 真实光标 SetCursorPos 落位并驻留（run_at_window_pos 驻留 0.5s）
#   3. 逐格发 ±WHEEL_DELTA（mouse_event / mouse.wheel）
#   4. PostMessage WM_MOUSEWHEEL 到窗口兜底（wParam=MAKELONG(0, 120*n),
#      lParam=MAKELONG(客户区x, 客户区y)）
#   5. 调用方用 OCR 验证列表内容真的变了，没变就升级/止损，不烧无效屏
WHEEL_NOTCH = win32con.WHEEL_DELTA           # 120，一格滚轮
SCROLL_CURSOR_DWELL = 0.3                    # 光标落位后驻留，等游戏注册 hover
SCROLL_NOTCH_PAUSE = 0.06                    # 逐格滚轮之间的间隔
SCROLL_POST_DELAY = 1.2                      # 滚轮发完等列表滚动动画稳定

# 列表内操作最大轮数（每轮最多执行一个动作）
MAX_LIST_ROUNDS = 12
# 打开列表的最大尝试次数（点击「任务」→等列表出现）
MAX_OPEN_ATTEMPTS = 3
# 关闭列表回到主界面的最大尝试次数
MAX_CLOSE_ATTEMPTS = 3

# 任务列表快照（压缩 JPEG，按日期覆盖，便于日常核对流程是否导航正确）
DAILY_LIST_SNAPSHOT_MAX_WIDTH = 960
DAILY_LIST_SNAPSHOT_QUALITY = 72


def _record_step_failure(logger: logging.Logger, hwnd, step: str, detail: str) -> None:
    """白荆各步骤失败时补一条结构化失败记录（截图 + 当帧文字），供事后聚合分析。"""
    try:
        from .flow_diagnostics import record_failure

        record_failure(
            logger,
            flow_key="BaiJingDaily",
            step=step,
            error=detail,
            hwnd=hwnd,
            game_key=BAIJING_GAME_KEY,
            save_image=bool(hwnd),
            extra={"kind": "baijing_daily_step"},
        )
    except Exception as e:
        logger.debug(f"[白荆回廊·日常] 失败记录写入失败（已忽略）: {e}")


def _daily_gift_meta_key() -> str:
    return f"baijing_daily_gift:{_baijing_today_key()}"


def _daily_gift_marked_done() -> bool:
    """今日是否已经成功领过免费礼包（task_meta 标记，一天一次，后写不覆盖）。"""
    try:
        from ..storage.workflow_db import WorkflowDB

        return WorkflowDB(str(WORKFLOW_DB_PATH)).get_task_meta(_daily_gift_meta_key()) == "ok"
    except Exception:
        return False


def _mark_daily_gift_done(logger: logging.Logger) -> None:
    try:
        from ..storage.workflow_db import WorkflowDB

        WorkflowDB(str(WORKFLOW_DB_PATH)).set_task_meta(_daily_gift_meta_key(), "ok")
    except Exception as e:
        logger.debug(f"[白荆回廊·日常] 写入每日礼包 meta 失败（已忽略）: {e}")


def _baijing_task_status_today(task_key: str) -> Optional[str]:
    """读今日 daily_progress 里白荆某任务的最终状态（读不到/异常返回 None）。

    用于"一天只能做一次"的动作（如每日免费礼包）在 force_rerun/重复运行时
    避免把"已领过导致入口消失"误报成失败（2026-10-25 17:49 实锤）。
    """
    try:
        from ..services.daily_progress_tracker import get_tracker

        snap = get_tracker(str(WORKFLOW_DB_PATH)).load_today()
        rec = snap.games.get(BAIJING_GAME_KEY)
        if not rec:
            return None
        for task in rec.tasks:
            if task.task_key == task_key:
                return task.status
    except Exception:
        return None
    return None


def _stopped(stop_check) -> bool:
    try:
        return bool(stop_check and stop_check())
    except Exception:
        return False


def _abs_region(hwnd, frac: Tuple[float, float, float, float]) -> Optional[Tuple[int, int, int, int]]:
    """窗口相对比例区域 → 屏幕绝对区域 (left, top, width, height)。窗口失效返回 None。"""
    region = _window_region(hwnd)
    if not region:
        return None
    left, top, width, height = region
    fx0, fy0, fx1, fy1 = frac
    ax0, ay0 = int(left + fx0 * width), int(top + fy0 * height)
    ax1, ay1 = int(left + fx1 * width), int(top + fy1 * height)
    return (ax0, ay0, max(0, ax1 - ax0), max(0, ay1 - ay0))


def _window_valid(hwnd) -> bool:
    try:
        import win32gui
        return bool(hwnd) and win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)
    except Exception:
        return False


def _ensure_window(logger: logging.Logger, hwnd):
    """校验窗口句柄，失效则重找；返回有效句柄或 None。"""
    if _window_valid(hwnd):
        return hwnd
    fresh = _resolve_baijing_window(logger)
    if fresh:
        logger.info(f"[白荆回廊·日常] 窗口句柄已刷新 {hwnd} -> {fresh}")
    return fresh


def _ocr_best_match(
    logger: logging.Logger,
    texts,
    hwnd,
    frac_region: Optional[Tuple[float, float, float, float]] = None,
    min_conf: float = 0.5,
    exclude=(),
) -> Optional[Dict]:
    """全窗 OCR 一次，按候选文字 + 相对区域 + 黑名单过滤，返回置信度最高的命中（dict）。

    匹配规则：先精确匹配（OCR 文本 == 候选文字），无精确命中再子串匹配
    （候选文字 ∈ OCR 文本，或 OCR 文本 ∈ 候选文字且至少 2 字符）。
    精确优先很重要：实测「确认」按钮会被对话框正文「将离开休息室，是否确认退出?」
    以更高置信度截胡，点到正文中间等于没点按钮。
    exclude 中的字样出现在 OCR 文本里则跳过该命中。

    注意：不做「先裁剪区域再 OCR」——实测裁剪会改变检测模型的结果
    （右侧区域裁剪后「一任务」直接丢失、中部「阶段任务」只剩「4/200」），
    全窗识别 + 对结果按区域过滤才稳（与 ocr_find_any_text 单次多候选同策略）。
    """
    results = ocr_window_content(logger, hwnd=hwnd)
    if not results:
        return None
    abs_region = _abs_region(hwnd, frac_region) if frac_region else None
    targets = [_normalize_text(t) for t in texts if _normalize_text(t)]
    best_exact = None
    best_exact_conf = -1.0
    best_sub = None
    best_sub_conf = -1.0
    for r in results:
        try:
            conf = float(r.get("confidence") or 0.0)
        except Exception:
            conf = 0.0
        if conf < min_conf:
            continue
        center = r.get("center")
        if abs_region and center:
            ax0, ay0, aw, ah = abs_region
            if not (ax0 <= center[0] <= ax0 + aw and ay0 <= center[1] <= ay0 + ah):
                continue
        tnorm = _normalize_text(str(r.get("text") or ""))
        if not tnorm:
            continue
        if any(x and _normalize_text(x) in tnorm for x in exclude):
            continue
        for target in targets:
            if tnorm == target:
                if conf > best_exact_conf:
                    best_exact, best_exact_conf = r, conf
                break
            if target in tnorm or (tnorm in target and len(tnorm) >= 2):
                if conf > best_sub_conf:
                    best_sub, best_sub_conf = r, conf
                break
    best = best_exact if best_exact is not None else best_sub
    if best is not None:
        logger.info(
            f"[白荆回廊·日常] OCR 命中 {best['text']!r} "
            f"({float(best.get('confidence') or 0):.2f}) @ {best['center']}"
        )
    return best


def _template_center(
    logger: logging.Logger,
    hwnd,
    template_path: str,
    frac_region: Optional[Tuple[float, float, float, float]] = None,
    threshold: float = TEMPLATE_MATCH_THRESHOLD,
) -> Optional[Tuple[int, int]]:
    """在窗口（可限定相对区域）内做多尺度模板匹配，返回屏幕绝对中心坐标。"""
    if not template_path or not os.path.exists(template_path):
        logger.warning(f"[白荆回廊·日常] 模板文件不存在: {template_path}")
        return None
    region = _window_region(hwnd)
    if not region:
        return None
    try:
        shot = grab_screen(region=region)
    except Exception as e:
        logger.debug(f"[白荆回廊·日常] 模板匹配截图失败: {e}")
        return None
    scr = cv2.cvtColor(np.asarray(shot), cv2.COLOR_RGB2GRAY)
    if frac_region:
        fx0, fy0, fx1, fy1 = frac_region
        h, w = scr.shape[:2]
        x0, y0 = int(fx0 * w), int(fy0 * h)
        x1, y1 = int(fx1 * w), int(fy1 * h)
        sub = scr[y0:y1, x0:x1]
    else:
        sub, x0, y0 = scr, 0, 0
    if sub.size == 0:
        return None
    try:
        tpl = cv2.cvtColor(
            np.asarray(Image.open(template_path).convert("RGB")), cv2.COLOR_RGB2GRAY
        )
    except Exception as e:
        logger.warning(f"[白荆回廊·日常] 模板加载失败: {template_path} ({e})")
        return None
    # 模板按 1920 宽参考分辨率裁剪；按当前窗口宽度换算缩放，±8% 容错
    base_scale = region[2] / float(TEMPLATE_REFERENCE_WIDTH)
    for scale in (base_scale, base_scale * 1.08, base_scale * 0.92):
        t = tpl
        if abs(scale - 1.0) > 1e-3:
            interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            t = cv2.resize(tpl, None, fx=scale, fy=scale, interpolation=interp)
        if t.shape[0] > sub.shape[0] or t.shape[1] > sub.shape[1]:
            continue
        result = cv2.matchTemplate(sub, t, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val >= threshold:
            th, tw = t.shape[:2]
            cx = region[0] + x0 + int(max_loc[0]) + tw // 2
            cy = region[1] + y0 + int(max_loc[1]) + th // 2
            logger.info(
                f"[白荆回廊·日常] 模板命中 {os.path.basename(template_path)} "
                f"scale={scale:.3f} conf={max_val:.3f} @ ({cx},{cy})"
            )
            return (cx, cy)
    return None


def _capture_daily_list_snapshot(
    logger: logging.Logger, hwnd, prefix: str = "daily_list"
) -> Optional[str]:
    """任务列表/商店页打开后抓一张压缩快照落盘（按日期覆盖），便于核对导航是否正确。"""
    try:
        region = _window_region(hwnd)
        img = grab_screen(region=region)
        w, h = img.size
        if w > DAILY_LIST_SNAPSHOT_MAX_WIDTH:
            img = img.resize(
                (DAILY_LIST_SNAPSHOT_MAX_WIDTH, max(1, int(h * DAILY_LIST_SNAPSHOT_MAX_WIDTH / w))),
                Image.LANCZOS,
            )
        if img.mode != "RGB":
            img = img.convert("RGB")
        BAIJING_VERIFY_DIR.mkdir(parents=True, exist_ok=True)
        out_path = BAIJING_VERIFY_DIR / f"{prefix}_{_baijing_today_key()}.jpg"
        img.save(str(out_path), "JPEG", quality=DAILY_LIST_SNAPSHOT_QUALITY, optimize=True)
        logger.info(f"[白荆回廊·日常] 快照已保存: {out_path.name}")
        return str(out_path)
    except Exception as e:
        logger.debug(f"[白荆回廊·日常] 任务列表快照保存失败: {e}")
        return None


def _read_activity_number(logger: logging.Logger, hwnd) -> Optional[int]:
    """读取每日任务面板顶部的「累计活跃度」大数字（游戏真实值）。

    数据流：全窗 OCR（遵守 ocr_click_tester「不裁剪识别」的约定）→ 按
    ACTIVITY_NUMBER_REGION 过滤 center → 只接受纯数字 token（被拆成多个
    数字块时按 x 拼接）→ 多次读取取非零最大值。返回 None 表示读不到，
    调用方退回静态目录核算，绝不写假值。

    2026-10-27 背景：静态目录把当天已完成任务算成 90，游戏面板实际 105
    （五档里程碑全部已领取）。目录会随版本漂移（当天游戏是「完成2次战斗」，
    目录写的是「完成3次战斗」），面板数字才是权威口径；480x270 的压缩
    验证图上 RapidOCR 已能读出 105（conf≈0.71），实时窗口更大更稳。
    """
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        return None
    abs_region = _abs_region(hwnd, ACTIVITY_NUMBER_REGION)
    if not abs_region:
        return None
    ax0, ay0, aw, ah = abs_region
    readings: List[int] = []
    for attempt in range(ACTIVITY_NUMBER_ATTEMPTS):
        if attempt:
            time.sleep(ACTIVITY_NUMBER_INTERVAL)
        try:
            results = ocr_window_content(logger, hwnd=hwnd)
        except Exception as exc:
            logger.debug(f"[白荆回廊·日常] 面板活跃度 OCR 失败: {exc}")
            continue
        # 先确认当前屏确实是每日任务面板，避免面板被关掉后误读其它数字
        texts = [_normalize_text(str(r.get("text") or "")) for r in results]
        if not any(
            any(marker in text for marker in ACTIVITY_PANEL_MARKERS)
            for text in texts
        ):
            logger.debug("[白荆回廊·日常] 未识别到每日任务面板特征，跳过活跃度读取")
            continue
        tokens: List[Tuple[float, str, float]] = []
        for r in results:
            center = r.get("center")
            if not center:
                continue
            if not (ax0 <= center[0] <= ax0 + aw and ay0 <= center[1] <= ay0 + ah):
                continue
            text = _normalize_text(str(r.get("text") or ""))
            if re.fullmatch(r"\d{1,3}", text):
                tokens.append((center[0], text, float(r.get("confidence") or 0)))
        if not tokens:
            continue
        # 优先用字符数最多的数字 token（105 优先于被截断的 10/05），同长度看置信度；
        # 没有多位 token 时兼容被拆成 1/0/5 三块的情况。
        multi = [t for t in tokens if len(t[1]) >= 2]
        if multi:
            multi.sort(key=lambda t: (-len(t[1]), -t[2], -int(t[1])))
            readings.append(int(multi[0][1]))
            continue
        joined = "".join(t[1] for t in sorted(tokens, key=lambda t: t[0]))
        if 1 <= len(joined) <= 3:
            value = int(joined)
            if 0 <= value <= ACTIVITY_NUMBER_MAX:
                readings.append(value)
    positives = [v for v in readings if v > 0]
    if not positives:
        if readings:
            logger.warning(
                f"[白荆回廊·日常] 面板活跃度只读到 0（可能仍在入场动画）{readings}，"
                f"退回目录核算"
            )
        return None
    chosen = max(positives)
    logger.info(f"[白荆回廊·日常] 面板累计活跃度读取: {readings} → 采用 {chosen}")
    return chosen


def _capture_stage_battle_dialog(logger: logging.Logger, hwnd) -> Optional[str]:
    """快速战斗设置弹窗首次出现时抓图并打印 OCR 文字（定位“指定次数”控件）。

    只按天抓一次；文件存在就跳过，避免四个关卡战斗重复写盘/刷日志。
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


def _find_task_button(logger: logging.Logger, hwnd, stop_check=None):
    """定位「任务」按钮：OCR 优先（结果限定右侧区域），模板兜底。返回 ((x, y), 方式) 或 None。"""
    hit = _ocr_best_match(
        logger, ("任务",), hwnd, frac_region=TASK_BUTTON_REGION, min_conf=0.55, exclude=TASK_TEXT_EXCLUDE
    )
    if hit:
        center = hit.get("center")
        if center:
            return (int(center[0]), int(center[1])), "ocr"
    center = _template_center(logger, hwnd, TASK_BUTTON_TEMPLATE, frac_region=TASK_BUTTON_REGION)
    if center:
        return (int(center[0]), int(center[1])), "template"
    return None


def _wait_task_list(
    logger: logging.Logger,
    hwnd,
    timeout: float = 8.0,
    stop_check=None,
    snapshot: bool = True,
) -> bool:
    """等待日常任务列表打开（OCR 特征文字）。

    snapshot=False 时跳过快照落盘（process_task_list 的每任务快检用，
    避免每轮覆盖 daily_list 快照）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        hit = _ocr_best_match(logger, TASK_LIST_MARKERS, hwnd, min_conf=0.55)
        if hit:
            logger.info(
                f"[白荆回廊·日常] 任务列表已打开（特征文字 {hit['text']!r} @ {hit['center']}）"
            )
            if snapshot:
                _capture_daily_list_snapshot(logger, hwnd)
            return True
        time.sleep(1.0)
    return False


def _ensure_task_list_open(
    logger: logging.Logger,
    hwnd,
    stop_check=None,
    timeout: float = 3.0,
) -> bool:
    """确认任务列表在屏，不在则重开（2026-10-22，治 10-21 空转根因）。

    10-21 实锤：研发室收取后面板被关回主界面，但 _wait_task_list 曾被主界面
    「任务」按钮文字骗过（假阳性），后续每个任务的找行都在错误界面上滚动
    6 次 ×3.5s 空转。现在每个任务处理前都强特征快检，丢了就重开列表。
    """
    if _wait_task_list(logger, hwnd, timeout=timeout, stop_check=stop_check, snapshot=False):
        return True
    logger.warning("[白荆回廊·日常] 任务列表不在屏，尝试重新打开")
    if open_daily_task_list(logger, hwnd, stop_check=stop_check):
        return _wait_task_list(logger, hwnd, timeout=timeout, stop_check=stop_check, snapshot=False)
    # 重开失败通常不是"列表按钮找不到"，而是界面卡在某个子页面（2026-10-24 烙痕详情页）：
    # 先 ESC/关闭恢复到主界面，再从主界面重开，别让后续任务一路空转跳过。
    logger.warning("[白荆回廊·日常] 重开任务列表失败，先恢复主界面再重试")
    # 茶憩残留的休息室/关注特写里根本没有「任务」按钮，先按场景登记表离开再重开
    # （2026-10-26 12:51 实锤：不处理则每个任务都"重开失败→跳过"，整轮只剩 2/16）
    state, acted = recover_scene(logger, hwnd, stop_check=stop_check, context="重开任务列表")
    if acted or state in ("main", "panel"):
        if open_daily_task_list(logger, hwnd, stop_check=stop_check):
            return _wait_task_list(logger, hwnd, timeout=timeout, stop_check=stop_check, snapshot=False)
    if _ensure_main_ui(logger, hwnd, stop_check=stop_check):
        if open_daily_task_list(logger, hwnd, stop_check=stop_check):
            return _wait_task_list(logger, hwnd, timeout=timeout, stop_check=stop_check, snapshot=False)
    return False


def _parse_task_rows(logger: logging.Logger, results: List[Dict]) -> List[Dict]:
    """    把全窗 OCR 结果按行聚类成任务行（行间距约 108px，±40px 聚类安全）。
    区域过滤由调用方（scan_daily_task_states）做；行内文本按 x 排序拼接——
    实测「完成1次「卓越道路」」的进度「(1/1)」常被 OCR 拆成独立文本框。
    """
    rows: List[Dict] = []
    for r in results:
        try:
            cx, cy = float(r["center"][0]), float(r["center"][1])
        except Exception:
            continue
        placed = False
        for row in rows:
            if abs(row["y"] - cy) <= 40:
                row["items"].append(r)
                row["n"] += 1
                row["y"] = (row["y"] * (row["n"] - 1) + cy) / row["n"]
                placed = True
                break
        if not placed:
            rows.append({"y": cy, "n": 1, "items": [r]})
    parsed = []
    for row in rows:
        items = sorted(row["items"], key=lambda i: i["center"][0])
        parsed.append({
            "y": row["y"],
            "text": "".join(str(i.get("text") or "") for i in items),
            "items": items,
        })
    return parsed


def _match_rows_to_catalog(logger: logging.Logger, rows: List[Dict]) -> Dict[str, Dict]:
    """把任务行匹配到静态目录，解析 (X/Y) 进度与按钮状态。

    done 判定：进度 X>=Y，或行内有「领取奖励」按钮（完成待领）。
    """
    states: Dict[str, Dict] = {}
    for row in rows:
        tnorm = _normalize_text(row["text"])
        if not tnorm:
            continue
        for task in DAILY_TASK_CATALOG:
            if not any(_normalize_text(k) in tnorm for k in task["match"]):
                continue
            m = re.search(r"(\d+)\s*/\s*(\d+)", tnorm)
            cur, need = (int(m.group(1)), int(m.group(2))) if m else (0, 1)
            has_claim = any(
                "领取" in _normalize_text(str(i.get("text") or ""))
                and "一键" not in _normalize_text(str(i.get("text") or ""))
                for i in row["items"]
            )
            states[task["key"]] = {
                "done": bool(cur >= need or has_claim),
                "progress": f"{cur}/{need}",
                "claimable": has_claim,
                "activity": task["activity"],
            }
            break
    return states


def _cursor_pos() -> Tuple[int, int]:
    """当前鼠标屏幕坐标。"""
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return (int(pt.x), int(pt.y))


def _set_cursor_pos(x: int, y: int) -> None:
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def _wheel_real(logger: logging.Logger, hwnd, x: int, y: int, notches: int) -> bool:
    """真实光标落位后逐格发滚轮（mouse_event ±WHEEL_DELTA）。

    notches 为正向上、负向下；按格数循环发送，每格间隔 SCROLL_NOTCH_PAUSE。
    发送前聚焦游戏窗口，光标落位后驻留 SCROLL_CURSOR_DWELL 让游戏注册 hover，
    结束后恢复原光标位置（不把鼠标留在游戏画面中央）。
    """
    if notches == 0:
        return True
    try:
        _focus_game_window(logger, hwnd)
        original = _cursor_pos()
        try:
            _set_cursor_pos(x, y)
            time.sleep(SCROLL_CURSOR_DWELL)
            sign = 1 if notches > 0 else -1
            for _ in range(abs(int(notches))):
                ctypes.windll.user32.mouse_event(
                    win32con.MOUSEEVENTF_WHEEL, 0, 0, sign * WHEEL_NOTCH, 0
                )
                time.sleep(SCROLL_NOTCH_PAUSE)
        finally:
            _set_cursor_pos(*original)
        return True
    except Exception as exc:
        logger.debug(f"[白荆回廊·日常] 真实滚轮发送失败 @ ({x},{y}) {notches} 格: {exc}")
        return False


def _wheel_post(logger: logging.Logger, hwnd, x: int, y: int, notches: int) -> bool:
    """PostMessage WM_MOUSEWHEEL 兜底（ok-ef/ok-ww post_message 后端同款）。

    lParam 用窗口客户区坐标；ok-ef 实测 PostMessage 鼠标消息在窗口未真实
    激活时可能被游戏丢弃，因此发送前同样先聚焦。
    """
    if not hwnd or notches == 0:
        return False
    try:
        _focus_game_window(logger, hwnd)
        client_x, client_y = win32gui.ScreenToClient(hwnd, (int(x), int(y)))
        wparam = win32api.MAKELONG(0, int(notches) * WHEEL_NOTCH)
        lparam = win32api.MAKELONG(client_x, client_y)
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wparam, lparam)
        logger.debug(
            f"[白荆回廊·日常] PostMessage 滚轮 hwnd={hwnd} "
            f"客户区坐标=({client_x},{client_y}) 格数={notches}"
        )
        return True
    except Exception as exc:
        logger.debug(f"[白荆回廊·日常] PostMessage 滚轮失败: {exc}")
        return False


def _row_region_signature(logger: logging.Logger, hwnd) -> str:
    """任务行区域 OCR 文本签名，用于判断滚动是否真的改变了列表内容。"""
    try:
        results = ocr_window_content(logger, hwnd=hwnd)
        region = _abs_region(hwnd, TASK_ROW_REGION)
        if region:
            ax0, ay0, aw, ah = region
            results = [
                r for r in results
                if r.get("center")
                and ax0 <= r["center"][0] <= ax0 + aw
                and ay0 <= r["center"][1] <= ay0 + ah
            ]
        texts = sorted(
            _normalize_text(str(r.get("text") or ""))
            for r in results
            if _normalize_text(str(r.get("text") or ""))
        )
        return "|".join(texts)
    except Exception:
        return ""


def _scroll_list_at(logger: logging.Logger, hwnd, x: int, y: int, notches: int) -> bool:
    """在 (x, y) 滚动任务列表，真实滚轮优先、PostMessage 兜底。

    返回 True = 至少一种方式发送成功（不保证列表真的动了，调用方需要
    用 OCR 验证；见 _scroll_list_with_verify）。
    """
    if _wheel_real(logger, hwnd, x, y, notches):
        return True
    logger.info("[白荆回廊·日常] 真实滚轮发送失败，改用 PostMessage 兜底")
    return _wheel_post(logger, hwnd, x, y, notches)


def _scroll_list_with_verify(logger: logging.Logger, hwnd, notches: int) -> bool:
    """滚动任务列表并用 OCR 验证内容真的变了。

    返回 True = 列表内容已变化（滚动生效）；
    返回 False = 真实滚轮 + PostMessage 都发过，内容仍无变化
    （已滚到列表底部，或滚动未生效——调用方按需止损）。
    """
    region = _abs_region(hwnd, TASK_ROW_REGION)
    if not region:
        return False
    sx, sy = region[0] + region[2] // 2, region[1] + region[3] // 2
    before = _row_region_signature(logger, hwnd)
    if _wheel_real(logger, hwnd, sx, sy, notches):
        time.sleep(SCROLL_POST_DELAY)
        if _row_region_signature(logger, hwnd) != before:
            return True
        logger.info("[白荆回廊·日常] 真实滚轮后列表内容无变化，尝试 PostMessage 兜底")
        if _wheel_post(logger, hwnd, sx, sy, notches):
            time.sleep(SCROLL_POST_DELAY)
            if _row_region_signature(logger, hwnd) != before:
                logger.info("[白荆回廊·日常] PostMessage 滚轮生效")
                return True
    return False


def scan_daily_task_states(
    logger: logging.Logger,
    hwnd,
    stop_check=None,
    max_scrolls: int = TASK_SCROLL_MAX,
    capture_prefix: Optional[str] = None,
) -> Dict[str, Dict]:
    """滚动任务列表并逐屏 OCR，聚合日常任务的状态。

    每屏识别后与静态目录匹配，直到找齐或滚动耗尽；每次滚动后用 OCR 签名
    验证列表内容真的变了，无效即止损（见 _scroll_list_with_verify）。
    capture_prefix 非空时每屏额外存一张快照（daily_scan_1_日期.jpg …），
    用于事后核对目录与游戏实际任务/分值的差异（2026-10-27 本地 90 vs 游戏 105）。
    返回 {task_key: {"done", "progress", "claimable", "activity"}}。
    """
    states: Dict[str, Dict] = {}
    for i in range(max_scrolls + 1):
        if _stopped(stop_check):
            break
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            break
        results_all = ocr_window_content(logger, hwnd=hwnd)
        results = results_all
        region = _abs_region(hwnd, TASK_ROW_REGION)
        if region:
            ax0, ay0, aw, ah = region
            results = [
                r for r in results
                if r.get("center")
                and ax0 <= r["center"][0] <= ax0 + aw
                and ay0 <= r["center"][1] <= ay0 + ah
            ]
        rows = _parse_task_rows(logger, results)
        found = _match_rows_to_catalog(logger, rows)
        new_keys = [k for k in found if k not in states]
        states.update(found)
        if capture_prefix:
            _capture_daily_list_snapshot(
                logger, hwnd, prefix=f"{capture_prefix}_{i + 1}"
            )
        logger.info(
            f"[白荆回廊·日常] 任务扫描第 {i + 1} 屏: 识别 {len(found)} 行, "
            f"新增 {len(new_keys)} 个任务, 累计 {len(states)}/{len(DAILY_TASK_CATALOG)}"
        )
        # 首屏零行 + 全窗 OCR 连列表强特征都没有 = 扫的根本不是任务列表
        # （2026-10-25 事故：面板被误关后重扫对着非列表页滚了 6 屏全 0 行、
        # ~25s 纯浪费）。复用本屏已有的全窗 OCR 结果判断，零额外识别开销；
        # 立即止损并落 flow_failures，别再往下滚。
        if i == 0 and not found:
            all_texts = [str(r.get("text") or "") for r in results_all]
            if not any(
                any(m in t for m in TASK_LIST_CORE_MARKERS) for t in all_texts
            ):
                logger.error(
                    "[白荆回廊·日常] 首屏 0 行且全窗无列表强特征（累计活跃度/主线指引），"
                    "当前界面不是任务列表，中止扫描"
                )
                _record_step_failure(
                    logger, hwnd, "日常任务扫描",
                    "首屏 0 行且无列表强特征：扫描时界面不是任务列表",
                )
                break
        if len(states) >= len(DAILY_TASK_CATALOG):
            break
        if i >= max_scrolls:
            break
        # 向下滚动一屏并验证内容真的变了（悬停列表区中心，每轮现算）。
        # 滚动无效时不再烧后续屏——2026-10-18/19 的教训：pyautogui.scroll(-3)
        # 的 dwData 不足一格滚轮，6 屏 OCR 完全相同纯属浪费。
        region = _abs_region(hwnd, TASK_ROW_REGION)
        if region:
            scroll_pos = (region[0] + region[2] // 2, region[1] + region[3] // 2)
        else:
            scroll_pos = (960, 540)
        logger.info(
            f"[白荆回廊·日常] 列表下滚 {abs(TASK_SCROLL_STEPS)} 格 @ {scroll_pos}"
        )
        if not _scroll_list_with_verify(logger, hwnd, TASK_SCROLL_STEPS):
            logger.warning(
                "[白荆回廊·日常] 滚动后列表内容无变化（真实滚轮 + PostMessage 均试过），"
                "已到底部或滚动未生效，停止继续扫描"
            )
            break
    if len(states) < len(DAILY_TASK_CATALOG):
        missing = [t["name"] for t in DAILY_TASK_CATALOG if t["key"] not in states]
        logger.warning(
            f"[白荆回廊·日常] 扫描覆盖不全 ({len(states)}/{len(DAILY_TASK_CATALOG)}): "
            f"未识别 {missing}（列表滚动可能无效）"
        )
    return states


def _ensure_daily_tab(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """确保任务面板停在「日常」页：看「累计活跃度」在不在，不在则点左侧「日常」tab。"""
    if _stopped(stop_check):
        return False
    if _ocr_best_match(logger, ("累计活跃度",), hwnd, min_conf=0.55):
        return True
    tab = _ocr_best_match(
        logger, ("日常",), hwnd, frac_region=TAB_COLUMN_REGION, min_conf=0.5, exclude=("周常",)
    )
    if not tab:
        logger.warning("[白荆回廊·日常] 未找到「日常」tab，可能面板未打开")
        return False
    _pyautogui_click_guarded(*tab["center"])
    logger.info(f"[白荆回廊·日常] 点击左侧「日常」tab @ {tab['center']}")
    # tab 切换有 1-2s 过渡；原实现固定等 1.2s 只查一次，2026-10-20 实操中
    # 任务面板默认停在「叶脉联结」，点日常后检查失败但页面随后已切到日常。
    for _ in range(3):
        time.sleep(1.2)
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        if _ocr_best_match(logger, ("累计活跃度",), hwnd, min_conf=0.55):
            return True
    return False


def _find_row_button_once(
    logger: logging.Logger, hwnd, row_keyword: str, button_text: str
) -> Optional[Tuple[int, int]]:
    """当前屏内定位任务行按钮：先找含 row_keyword 的行，再取同行(±45px)的 button_text。"""
    results = ocr_window_content(logger, hwnd=hwnd)
    region = _abs_region(hwnd, TASK_ROW_REGION)
    if region:
        ax0, ay0, aw, ah = region
        results = [
            r for r in results
            if r.get("center")
            and ax0 <= r["center"][0] <= ax0 + aw
            and ay0 <= r["center"][1] <= ay0 + ah
        ]
    row_y = None
    for r in results:
        if _normalize_text(row_keyword) in _normalize_text(str(r.get("text") or "")):
            row_y = r["center"][1]
            break
    if row_y is None:
        return None
    btn = _normalize_text(button_text)
    for r in results:
        text = _normalize_text(str(r.get("text") or ""))
        if btn in text and abs(r["center"][1] - row_y) <= 45:
            logger.info(
                f"[白荆回廊·日常] 定位到 {row_keyword!r} 行的 {button_text!r} 按钮 @ {r['center']}"
            )
            return (int(r["center"][0]), int(r["center"][1]))
    return None


def _scroll_row_list_to_top(logger: logging.Logger, hwnd) -> bool:
    """把任务列表滚到顶部（发送足够多的向上滚轮，顶部会 clamp）。"""
    region = _abs_region(hwnd, TASK_ROW_REGION)
    if not region:
        return False
    sx, sy = region[0] + region[2] // 2, region[1] + region[3] // 2
    # 扫描最多下滚 TASK_SCROLL_MAX 次；回顶按“满量 + 2 屏”发，确保从底部也能 clamp 到顶。
    notches = abs(TASK_SCROLL_STEPS) * (TASK_SCROLL_MAX + 2)
    logger.info(f"[白荆回廊·日常] 回滚任务列表到顶部（向上 {notches} 格）")
    ok = _wheel_real(logger, hwnd, sx, sy, notches)
    if not ok:
        ok = _wheel_post(logger, hwnd, sx, sy, notches)
    if ok:
        time.sleep(SCROLL_POST_DELAY)
    return ok


def _find_row_button(
    logger: logging.Logger, hwnd, row_keyword: str, button_text: str, stop_check=None
) -> Optional[Tuple[int, int]]:
    """定位任务行内的按钮：先看当前屏，否则回顶部并逐屏校验扫描整个列表。

    扫描阶段会把列表滚到底部，而每个「前往」处理器返回面板时的滚动位置不确定。
    2026-10-20 的教训：回顶部只发 6 格（扫描累计下滚了 12 格），第 3 行的
    研发室永远不在视野里；下层战斗行也只试 3 屏 + 不校验的 PostMessage。
    现在改为：足量回顶 → 逐屏下滚并用 OCR 校验内容真的变化 → 到底后再回顶重扫一次。
    """
    if _stopped(stop_check):
        return None
    pos = _find_row_button_once(logger, hwnd, row_keyword, button_text)
    if pos:
        return pos
    if not _abs_region(hwnd, TASK_ROW_REGION):
        logger.warning(f"[白荆回廊·日常] 未找到含 {row_keyword!r} 的任务行")
        return None
    # 1) 足量回顶（顶部 clamp，滚过头无害）
    if not _scroll_row_list_to_top(logger, hwnd):
        logger.warning(f"[白荆回廊·日常] 无法回滚任务列表顶部，放弃定位 {row_keyword!r}")
        return None
    pos = _find_row_button_once(logger, hwnd, row_keyword, button_text)
    if pos:
        return pos
    # 2) 从顶部逐屏下滚；每屏用 OCR 校验列表真的移动，滚到底再回顶重扫一次
    retried_from_top = False
    moved_screens = 0
    for attempt in range(1, TASK_SCROLL_MAX + 2):
        if _stopped(stop_check):
            return None
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return None
        moved = _scroll_list_with_verify(logger, hwnd, TASK_SCROLL_STEPS)
        if not moved:
            # 已到底（或滚动未生效）。若还没重扫过，回顶再完整扫一轮。
            if retried_from_top or moved_screens >= TASK_SCROLL_MAX:
                break
            retried_from_top = True
            logger.info(
                f"[白荆回廊·日常] 已扫到列表底部仍未找到 {row_keyword!r}，回顶重扫一轮"
            )
            if not _scroll_row_list_to_top(logger, hwnd):
                break
            pos = _find_row_button_once(logger, hwnd, row_keyword, button_text)
            if pos:
                return pos
            continue
        moved_screens += 1
        logger.info(
            f"[白荆回廊·日常] 未找到 {row_keyword!r} 行，列表下滚重试 ({attempt}/{TASK_SCROLL_MAX + 1})"
        )
        pos = _find_row_button_once(logger, hwnd, row_keyword, button_text)
        if pos:
            return pos
    logger.warning(f"[白荆回廊·日常] 滚动后仍未找到 {row_keyword!r} 行的 {button_text!r} 按钮")
    return None


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


def _wait_page_markers(
    logger: logging.Logger,
    hwnd,
    markers,
    timeout: float = 6.0,
    stop_check=None,
    frac_region: Optional[Tuple[float, float, float, float]] = None,
) -> bool:
    """轮询等待页面特征文字出现（任一命中）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        if _ocr_best_match(logger, markers, hwnd, min_conf=0.55, frac_region=frac_region):
            return True
        time.sleep(1.0)
    return False


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


def _same_click_target(a, b, tol: int = 25) -> bool:
    """两个 OCR 命中是否在同一位置（用于判断上一次点击是否让界面发生了变化）。"""
    if not a or not b:
        return False
    return abs(int(a[0]) - int(b[0])) <= tol and abs(int(a[1]) - int(b[1])) <= tol


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
SCENE_OCR_MIN_CONF = 0.5
_SCENE_LOGGED: Dict[str, float] = {}        # 证据节流：context → 上次记录时间
_SCENE_LOG_INTERVAL_SEC = 600.0


def _scene_results(logger: logging.Logger, hwnd) -> List[Dict]:
    """一轮恢复只截一次屏：所有场景判定共用这份 OCR 结果。

    旧实现里 `_in_rest_room` / `_in_focus_mode` / `_ocr_best_match` 每个判定都
    各自截屏 + OCR，恢复一轮要跑 4-6 次识别（十几秒）。
    """
    try:
        return ocr_window_content(logger, hwnd=hwnd) or []
    except Exception as exc:
        logger.debug(f"[白荆回廊·日常] 场景识别 OCR 失败: {exc}")
        return []


def _pick_result(
    logger: logging.Logger,
    hwnd,
    results: List[Dict],
    markers,
    frac_region: Optional[Tuple[float, float, float, float]] = None,
    min_conf: float = SCENE_OCR_MIN_CONF,
    exclude=(),
    exact_only: bool = False,
) -> Optional[Dict]:
    """在已取到的 OCR 结果里挑一条命中（规则同 `_ocr_best_match`，但不再截屏）。

    精确匹配优先于子串匹配：实测「确认」按钮会被对话框正文以更高置信度截胡。
    区域过滤用屏幕绝对坐标（`ocr_window_content` 返回的 center 已加过窗口原点），
    所以 frac_region 要先经 `_abs_region` 换算。
    """
    if not results:
        return None
    abs_region = _abs_region(hwnd, frac_region) if frac_region else None
    targets = [_normalize_text(t) for t in markers if _normalize_text(t)]
    best_exact = None
    best_exact_conf = -1.0
    best_sub = None
    best_sub_conf = -1.0
    for r in results:
        try:
            conf = float(r.get("confidence") or 0.0)
        except Exception:
            conf = 0.0
        if conf < min_conf:
            continue
        center = r.get("center")
        if abs_region and center:
            ax0, ay0, aw, ah = abs_region
            if not (ax0 <= center[0] <= ax0 + aw and ay0 <= center[1] <= ay0 + ah):
                continue
        tnorm = _normalize_text(str(r.get("text") or ""))
        if not tnorm:
            continue
        if any(x and _normalize_text(x) in tnorm for x in exclude):
            continue
        for target in targets:
            if tnorm == target:
                if conf > best_exact_conf:
                    best_exact, best_exact_conf = r, conf
                break
            # exact_only：只认「候选文字 ⊆ OCR 文本」，禁止反向包含。
            # 用于「计划等级提升」这类长特征——普通子串匹配会让旧页面的
            # 「等级提升」短文字误命中，进而点错屏幕。
            if target in tnorm or (not exact_only and tnorm in target and len(tnorm) >= 2):
                if conf > best_sub_conf:
                    best_sub, best_sub_conf = r, conf
                break
    return best_exact if best_exact is not None else best_sub


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


def _scene_exit_event_dialog(logger: logging.Logger, hwnd, results, stop_check=None) -> bool:
    """定巡之礼等活动弹窗：点右上角 X 关闭（没有「返回」按钮）。"""
    hit = _pick_result(logger, hwnd, results, EVENT_DIALOG_MARKERS, min_conf=0.55)
    if not hit:
        return False
    _click_ratio(logger, hwnd, EVENT_DIALOG_CLOSE, "定巡之礼右上关闭")
    logger.info(f"[白荆回廊·日常] 场景退出·定巡之礼：关闭弹窗（命中 {hit['text']!r}）")
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


# 登记表顺序 = 匹配优先级：遮罩类先清，再清会挡住返回按钮的摘要，最后才是界面本身。
# 新增界面只要加一行（特征必须挑该界面独占文字，原因见 REST_ROOM_MARKERS 注释），
# 并在 artifacts/test_baijing_scene_registry_20261026.py 补一条用例锁住顺序。
SCENE_RULES = (
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
)


def _match_scene_rule(logger: logging.Logger, hwnd, results: List[Dict]):
    """按登记表顺序找第一个命中的场景，返回 (规则, 命中项)；都没有返回 (None, None)。"""
    for rule in SCENE_RULES:
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
    unknown_streak = 0
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
            if unknown_streak >= SCENE_UNKNOWN_ROUNDS:
                texts = "｜".join(str(r.get("text") or "") for r in (current or [])[:12])
                _record_unknown_scene(
                    logger, hwnd, context or "未知场景",
                    f"未匹配任何已知场景，需要补登记；当帧文字: {texts[:200]}",
                )
                return ("unknown", acted_any)
            continue
        last_scene = str(rule["name"])
        logger.info(f"[白荆回廊·日常] 识别到场景「{last_scene}」，执行专属退出")
        try:
            acted = bool(rule["exit"](logger, hwnd, current, stop_check=stop_check))
        except Exception as exc:
            logger.warning(f"[白荆回廊·日常] 场景「{last_scene}」退出流程异常: {exc}")
            acted = False
        acted_any = acted_any or acted
        if not acted:
            unknown_streak += 1
            if unknown_streak >= SCENE_UNKNOWN_ROUNDS:
                break
        time.sleep(0.4)
    if last_scene:
        _record_unknown_scene(
            logger, hwnd, context or f"场景{last_scene}",
            f"场景「{last_scene}」反复退出失败，仍无法回到任务面板/主界面",
        )
        return (f"scene:{last_scene}", acted_any)
    return ("unknown", acted_any)


def _return_to_task_panel(logger: logging.Logger, hwnd, stop_check=None, max_attempts: int = 5) -> bool:
    """从子页面逐层返回日常任务面板：已在面板 → 主界面则重开「任务」→ 否则 ESC 退一层。

    「确认」必须防原地死循环：2026-10-24 记忆烙痕强化后返回时，页面上有个不生效的
    「确认」文字，原实现每轮都点它、从不按 ESC，界面卡在烙痕页 2 分钟，后续任务全跳过。
    同一坐标连续命中说明上次点击没改变界面，本轮改走 ESC。
    """
    last_confirm: Optional[Tuple[int, int]] = None
    last_back: Optional[Tuple[int, int]] = None
    last_cont: Optional[Tuple[int, int]] = None
    for attempt in range(1, max_attempts + 1):
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        # 1) 已在任务面板？
        if _ocr_best_match(logger, TASK_LIST_CORE_MARKERS, hwnd, min_conf=0.55):
            return _ensure_daily_tab(logger, hwnd, stop_check=stop_check)
        # 2) 在主界面 → 重开任务面板
        is_main, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=1.5)
        if is_main:
            _focus_game_window(logger, hwnd)
            found = _find_task_button(logger, hwnd, stop_check)
            if found:
                (x, y), method = found
                _pyautogui_click_guarded(x, y)
                logger.info(f"[白荆回廊·日常] 重新点击「任务」按钮 [{method}] @ ({x},{y})")
                time.sleep(2.0)
                if _ocr_best_match(logger, TASK_LIST_CORE_MARKERS, hwnd, min_conf=0.55):
                    return _ensure_daily_tab(logger, hwnd, stop_check=stop_check)
                continue
        # 2.4) 场景登记表：认出是哪个界面就跑它的专属退出流程
        # （休息室/关注特写这类界面吃不到 ESC，2026-10-26 实锤：连按 5 次空转）
        state, acted = recover_scene(logger, hwnd, stop_check=stop_check, context="返回任务面板")
        if state == "panel":
            return _ensure_daily_tab(logger, hwnd, stop_check=stop_check)
        if state == "main" or acted:
            continue
        # 3) 过场动画遮罩：先点掉「点击屏幕继续」，否则底部按钮都点不到
        cont = _ocr_best_match(logger, ("点击屏幕继续",), hwnd, min_conf=0.5)
        if cont:
            pos = (int(cont["center"][0]), int(cont["center"][1]))
            if not _same_click_target(pos, last_cont):
                last_cont = pos
                _pyautogui_click_guarded(*pos)
                logger.info(f"[白荆回廊·日常] 返回途中点击「点击屏幕继续」@ {pos}")
                time.sleep(1.5)
                continue
            # 同一坐标反复出现 = 点不动的静态文字，落到下面走 ESC
        # 4) 退出确认类弹窗（如「将离开休息室，是否确认退出?」）→ 点「确认」
        confirm = _ocr_best_match(logger, ("确认",), hwnd, min_conf=0.6)
        if confirm:
            pos = (int(confirm["center"][0]), int(confirm["center"][1]))
            if _same_click_target(pos, last_confirm):
                logger.warning(
                    "[白荆回廊·日常] 「确认」点击后界面未变化（疑似静态文字），本轮改按 ESC"
                )
            else:
                last_confirm = pos
                _pyautogui_click_guarded(*pos)
                logger.info(f"[白荆回廊·日常] 点击退出确认框「确认」@ {pos}")
                time.sleep(1.5)
                if _ocr_best_match(logger, TASK_LIST_CORE_MARKERS, hwnd, min_conf=0.55):
                    return _ensure_daily_tab(logger, hwnd, stop_check=stop_check)
                continue
        # 5) 页面左上角「返回」按钮（烙痕列表页/休息室等 ESC 不灵时用）
        back = _ocr_best_match(
            logger, ("返回",), hwnd, frac_region=(0.0, 0.0, 0.20, 0.15), min_conf=0.55
        )
        if back:
            pos = (int(back["center"][0]), int(back["center"][1]))
            if _same_click_target(pos, last_back):
                logger.warning("[白荆回廊·日常] 「返回」点击后界面未变化，本轮改按 ESC")
            else:
                last_back = pos
                _pyautogui_click_guarded(*pos)
                logger.info(f"[白荆回廊·日常] 点击页面左上角「返回」@ {pos}")
                time.sleep(1.5)
                if _ocr_best_match(logger, TASK_LIST_CORE_MARKERS, hwnd, min_conf=0.55):
                    return _ensure_daily_tab(logger, hwnd, stop_check=stop_check)
                continue
        # 6) ESC 退一层（不清 last_confirm：同一坐标的「确认」本次返回不再重复点）
        _focus_game_window(logger, hwnd)
        pyautogui.press("escape")
        logger.info(f"[白荆回廊·日常] 按 ESC 返回上一层 (#{attempt}/{max_attempts})")
        time.sleep(1.5)
    return _ocr_best_match(logger, TASK_LIST_CORE_MARKERS, hwnd, min_conf=0.55) is not None


def _handle_memory_exp_task(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """记忆烙痕经验任务：前往 → 任选烙痕 → 强化烙痕 → 重置 → +1 → 确认 → 动画 → 回面板。

    2026-10-18 用户截图实测流程：点行内「前往」进入记忆烙痕列表页（3列卡片网格，
    左上角首个卡片实测坐标 (402,442)@1919x1079），点首个卡片进详情页，点右下
    「强化烙痕」打开等级提升弹窗，「重置」后点一次「+」，「确认」后播等级提升
    动画，点击屏幕继续，然后逐层返回日常任务面板（该任务随后出现「领取奖励」，
    由常规领取循环处理）。
    """
    goto_pos = _find_row_button(logger, hwnd, "经验", "前往", stop_check=stop_check)
    if not goto_pos:
        return False
    _pyautogui_click_guarded(goto_pos[0], goto_pos[1])
    logger.info(f"[白荆回廊·日常] 点击记忆烙痕任务「前往」@ {goto_pos}")
    # 等落地页：烙痕列表页（顶部工具栏）或直接就是详情页
    _wait_page_markers(logger, hwnd, MEMORY_LIST_MARKERS, timeout=8.0, stop_check=stop_check)
    time.sleep(1.5)
    # 1) 若落地不是详情页，点首个卡片进去（实测坐标校准）
    if not _on_memory_detail_page(logger, hwnd):
        opened = False
        for fx, fy in MEMORY_LIST_CLICK_CANDIDATES:
            if _stopped(stop_check):
                return False
            hwnd = _ensure_window(logger, hwnd)
            if not hwnd:
                return False
            region = _window_region(hwnd)
            if not region:
                return False
            cx = int(region[0] + fx * region[2])
            cy = int(region[1] + fy * region[3])
            _pyautogui_click_guarded(cx, cy)
            logger.info(f"[白荆回廊·日常] 尝试点击烙痕卡片 @ ({cx},{cy}) [{fx:.2f},{fy:.2f}]")
            time.sleep(2.0)
            if _on_memory_detail_page(logger, hwnd):
                opened = True
                break
        if not opened:
            logger.warning("[白荆回廊·日常] 未能进入烙痕详情页（候选点击位全部落空）")
            _return_to_task_panel(logger, hwnd, stop_check=stop_check)
            return False
    logger.info("[白荆回廊·日常] 已进入烙痕详情页")
    # 2) 点「强化烙痕」开弹窗
    btn = _template_center(logger, hwnd, MEMORY_ENHANCE_BUTTON_TPL, threshold=0.85)
    if not btn:
        ocr_btn = _ocr_best_match(
            logger,
            MEMORY_ENHANCE_TEXTS,
            hwnd,
            frac_region=MEMORY_ENHANCE_BTN_REGION,
            min_conf=0.55,
        )
        if ocr_btn:
            btn = (int(ocr_btn["center"][0]), int(ocr_btn["center"][1]))
            logger.info(f"[白荆回廊·日常] 用 OCR 兜底定位「强化烙痕」@ {btn}")
    if not btn:
        logger.warning("[白荆回廊·日常] 详情页未找到「强化烙痕」按钮")
        _return_to_task_panel(logger, hwnd, stop_check=stop_check)
        return False
    _pyautogui_click_guarded(btn[0], btn[1])
    logger.info(f"[白荆回廊·日常] 点击「强化烙痕」@ {btn}")
    if not _wait_page_markers(logger, hwnd, MEMORY_DIALOG_MARKERS, timeout=6.0, stop_check=stop_check):
        logger.warning("[白荆回廊·日常] 强化弹窗未打开")
        _return_to_task_panel(logger, hwnd, stop_check=stop_check)
        return False
    # 3) 重置 → +1 → 确认
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
    # 4) 等级提升动画 → 点击屏幕继续
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
    # 5) 返回日常任务面板
    return _return_to_task_panel(logger, hwnd, stop_check=stop_check)


def _click_ratio(logger: logging.Logger, hwnd, ratio: Tuple[float, float], label: str = ""):
    """按窗口相对比例点击。"""
    region = _window_region(hwnd)
    if not region:
        return None
    cx = int(region[0] + ratio[0] * region[2])
    cy = int(region[1] + ratio[1] * region[3])
    _pyautogui_click_guarded(cx, cy)
    logger.info(f"[白荆回廊·日常] 点击{label or '位置'} @ ({cx},{cy}) [{ratio[0]:.2f},{ratio[1]:.2f}]")
    return (cx, cy)


def _handle_tea_break_task(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """午后茶憩任务：前往→确定→任选角色→喝茶→再来一杯→摘要→返回→确定→主界面。

    2026-10-18 用户截图实测流程：点任务行「前往」后弹「是否前往茶憩?」确认框，
    确定后进入角色选择页（今日可邀份额），点任意角色进入茶憩场景；场景内喝茶，
    等待一段时间后再来一杯；茶憩结束出摘要，随意点击关闭；回到休息室后点左上
    「返回」，在「将离开休息室」确认框点「确认」，回到主界面。
    喝茶/再来一杯是场景内动态出现的交互（截图未捕捉到按钮形态），用 OCR 轮询
    文字点击；若轮询不到只记录警告，不盲点 3D 场景。
    """
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
    scene_seen = _wait_page_markers(
        logger, hwnd, TEA_SCENE_MARKERS, timeout=TEA_SCENE_TIMEOUT_SEC, stop_check=stop_check
    )
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


def _open_retail_shop(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """主界面底栏点「有猫零售」进入零售页（OCR 优先，比例兜底）。"""
    _focus_game_window(logger, hwnd)
    hit = _ocr_best_match(
        logger, SHOP_ENTRY_TEXTS, hwnd, frac_region=SHOP_ENTRY_REGION, min_conf=0.55
    )
    if hit:
        _pyautogui_click_guarded(*hit["center"])
        logger.info(f"[白荆回廊·日常] 点击「有猫零售」@ {hit['center']}")
    else:
        _click_ratio(logger, hwnd, SHOP_ENTRY_FALLBACK, "「有猫零售」（比例兜底）")
    return _wait_page_markers(logger, hwnd, SHOP_PAGE_MARKERS, timeout=8.0, stop_check=stop_check)


def _select_featured_gift_tab(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """左侧「精选礼包」→ 顶部「有猫精选」tab，确认首张卡是「每日外勤赠礼」。"""
    side = _ocr_best_match(
        logger, SHOP_SIDE_TEXTS, hwnd, frac_region=SHOP_SIDE_REGION, min_conf=0.55
    )
    if side:
        _pyautogui_click_guarded(*side["center"])
        logger.info(f"[白荆回廊·日常] 点击左侧「精选礼包」@ {side['center']}")
    else:
        _click_ratio(logger, hwnd, SHOP_SIDE_FALLBACK, "左侧「精选礼包」（比例兜底）")
    time.sleep(1.5)
    tab = _ocr_best_match(
        logger, SHOP_TAB_TEXTS, hwnd, frac_region=SHOP_TAB_REGION, min_conf=0.55
    )
    if tab:
        _pyautogui_click_guarded(*tab["center"])
        logger.info(f"[白荆回廊·日常] 点击顶部「有猫精选」tab @ {tab['center']}")
    else:
        _click_ratio(logger, hwnd, SHOP_TAB_FALLBACK, "「有猫精选」tab（比例兜底）")
    time.sleep(1.2)
    # 确认切到了含「每日外勤赠礼」的那一页（停在「活动限时」时首卡是「集结共鸣助力」）
    if not _wait_page_markers(logger, hwnd, SHOP_FREE_PACK_TEXTS, timeout=6.0, stop_check=stop_check):
        logger.warning("[白荆回廊·日常] 有猫精选页未出现「每日外勤赠礼」")
        return False
    return True


def _buy_in_dialog(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """购买确认弹窗点「购买」（OCR 优先，比例兜底）。"""
    hit = _ocr_best_match(
        logger, SHOP_DIALOG_BUY_TEXTS, hwnd, frac_region=SHOP_DIALOG_BUY_REGION, min_conf=0.55
    )
    if hit:
        _pyautogui_click_guarded(*hit["center"])
        logger.info(f"[白荆回廊·日常] 点击「购买」@ {hit['center']}")
    else:
        _click_ratio(logger, hwnd, SHOP_BUY_FALLBACK, "「购买」（比例兜底）")
    return True


def _wait_purchase_settled(logger: logging.Logger, hwnd, timeout: float = 12.0, stop_check=None) -> str:
    """点完「购买」后确认到账。

    先等购买弹窗消失（期间不做任何点击，避免误关弹窗把购买打断），
    弹窗消失后再关奖励弹窗并查卡片是否显示已买完。
    返回 "purchased" / "unknown"。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _stopped(stop_check):
            return "unknown"
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return "unknown"
        if _ocr_best_match(
            logger, SHOP_DIALOG_MARKERS, hwnd, min_conf=0.55, frac_region=SHOP_DIALOG_REGION
        ):
            time.sleep(1.0)
            continue
        # 弹窗已消失 → 关掉可能弹出的奖励弹窗，再看卡片状态
        _dismiss_overlay(logger, hwnd)
        sold = _ocr_best_match(logger, SHOP_SOLD_OUT_TEXTS, hwnd, min_conf=0.5)
        if sold:
            logger.info(f"[白荆回廊·日常] 卡片显示 {sold['text']!r}，确认购买成功")
        else:
            logger.info("[白荆回廊·日常] 购买弹窗已关闭，确认购买完成")
        return "purchased"
    logger.warning("[白荆回廊·日常] 点「购买」后弹窗仍在，未确认到账")
    return "unknown"


def _leave_retail_shop(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """零售页返回主界面（左上「返回」→ ESC），返回是否已回主界面。"""
    for attempt in range(1, MAX_CLOSE_ATTEMPTS + 1):
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        # 购买成功可能弹出奖励弹窗，先清一次再判断是否已回主界面
        _dismiss_overlay(logger, hwnd)
        ok, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=2.0)
        if ok:
            logger.info("[白荆回廊·日常] 已从零售页回到主界面")
            return True
        back = _ocr_best_match(logger, ("返回",), hwnd, frac_region=SHOP_BACK_REGION, min_conf=0.55)
        if back:
            _pyautogui_click_guarded(*back["center"])
            logger.info(f"[白荆回廊·日常] 点击零售页「返回」@ {back['center']}")
        else:
            _focus_game_window(logger, hwnd)
            pyautogui.press("escape")
            logger.info(f"[白荆回廊·日常] 按 ESC 退出零售页 (#{attempt}/{MAX_CLOSE_ATTEMPTS})")
        time.sleep(1.5)
    ok, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=2.0)
    if not ok:
        logger.warning("[白荆回廊·日常] 多次尝试后仍未从零售页回到主界面")
    return ok


def buy_daily_free_gift_pack(logger: logging.Logger, hwnd, stop_check=None) -> Dict:
    """有猫零售领取每日免费礼包「每日外勤赠礼」（2026-10-18 用户实测流程）。

    主界面 → 底栏「有猫零售」→ 左侧「精选礼包」→ 顶部「有猫精选」→ 首张卡
    「每日外勤赠礼」卡底「免费」→ 购买弹窗「购买」→ 关奖励弹窗 → 返回主界面。

    返回 {"status", "purchased", "detail"}：
      purchased=True  当日买到（免费礼包每天刷新，错过要等 8 小时刷新）
      status="already" 今日已领（卡片已无「免费」按钮，显示已售罄/剩余0件）
      status="failed"  / "skipped" 见 detail
    """
    result = {"status": "skipped", "purchased": False, "detail": "", "attempts": 0}
    if _stopped(stop_check):
        result["detail"] = "用户停止"
        return result
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        result.update(status="failed", detail="未找到游戏窗口")
        return result
    # 每日礼包一天只能领一次：强制重跑/同一天第二次执行时，商品已从货架消失，
    # 继续导航只会得到"未找到免费按钮"并误报失败（2026-10-25 17:49 实锤）。
    # 以今日 daily_progress 的成功记录为准，直接记 already。
    prev = _baijing_task_status_today("daily_gift")
    if prev in ("success", "already") or _daily_gift_marked_done():
        result.update(status="already", detail="今日已领取（进度记录/meta）")
        logger.info(f"[白荆回廊·日常] {result['detail']}，跳过零售页")
        return result
    # 2026-10-22 修复：不再依赖主流程 close_task_list 的 back_ok 门禁
    # （10-21 实锤：close_task_list 被引导气泡带偏判失败，零售/俱乐部被整体跳过，
    # 连记录都没落）。这里自己确保回到主界面，失败才 skipped 且 detail 留痕。
    if not _ensure_main_ui(logger, hwnd, stop_check=stop_check):
        result.update(status="skipped", detail="多次尝试仍未回到主界面，跳过零售")
        logger.info(f"[白荆回廊·日常] {result['detail']}")
        return result

    for attempt in range(1, SHOP_MAX_ATTEMPTS + 1):
        if _stopped(stop_check):
            result["detail"] = "用户停止"
            return result
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            result.update(status="failed", detail="游戏窗口丢失")
            return result
        result["attempts"] = attempt
        # 每轮开始都确保在主界面（上一轮 _leave_retail_shop 可能没退干净）
        if attempt > 1 and not _ensure_main_ui(logger, hwnd, stop_check=stop_check):
            result.update(status="skipped", detail="多次尝试仍未回到主界面，跳过零售")
            return result
        # 1) 进零售页
        if not _open_retail_shop(logger, hwnd, stop_check=stop_check):
            logger.warning(f"[白荆回廊·日常] 第 {attempt}/{SHOP_MAX_ATTEMPTS} 次：未打开零售页")
            time.sleep(1.5)
            continue
        # 2) 精选礼包 → 有猫精选 tab
        if not _select_featured_gift_tab(logger, hwnd, stop_check=stop_check):
            logger.warning(f"[白荆回廊·日常] 第 {attempt}/{SHOP_MAX_ATTEMPTS} 次：未切到有猫精选页")
            _leave_retail_shop(logger, hwnd, stop_check=stop_check)
            time.sleep(1.5)
            continue
        # 3) 点卡底「免费」（区域限定价格行，排掉卡片顶部角标与弹窗里的同名文字）
        btn = _ocr_best_match(
            logger, SHOP_FREE_BTN_TEXTS, hwnd, frac_region=SHOP_FREE_BTN_REGION, min_conf=0.5
        )
        if not btn:
            sold = _ocr_best_match(logger, SHOP_SOLD_OUT_TEXTS, hwnd, min_conf=0.5)
            if sold:
                result.update(status="already", detail=f"今日已领取（{sold['text']}）")
                _mark_daily_gift_done(logger)
            else:
                result.update(status="failed", detail="未找到「免费」按钮且无已领标记")
            logger.info(f"[白荆回廊·日常] 跳过购买：{result['detail']}")
            _leave_retail_shop(logger, hwnd, stop_check=stop_check)
            return result
        _pyautogui_click_guarded(*btn["center"])
        logger.info(f"[白荆回廊·日常] 点击「每日外勤赠礼」卡底「免费」@ {btn['center']}")
        # 4) 购买弹窗 → 点「购买」
        if not _wait_page_markers(
            logger,
            hwnd,
            SHOP_DIALOG_MARKERS,
            timeout=6.0,
            stop_check=stop_check,
            frac_region=SHOP_DIALOG_REGION,
        ):
            logger.warning("[白荆回廊·日常] 点「免费」后未出现购买弹窗")
            _dismiss_overlay(logger, hwnd)
            _leave_retail_shop(logger, hwnd, stop_check=stop_check)
            time.sleep(1.5)
            continue
        _buy_in_dialog(logger, hwnd, stop_check=stop_check)
        state = _wait_purchase_settled(logger, hwnd, timeout=12.0, stop_check=stop_check)
        _dismiss_overlay(logger, hwnd)
        if state == "purchased":
            result.update(
                status="success", purchased=True, detail="已领取「每日外勤赠礼」免费礼包"
            )
            _mark_daily_gift_done(logger)
            logger.info(f"[白荆回廊·日常] {result['detail']}")
            _leave_retail_shop(logger, hwnd, stop_check=stop_check)
            return result
        logger.warning(f"[白荆回廊·日常] 第 {attempt}/{SHOP_MAX_ATTEMPTS} 次购买未确认到账，重试")
        _dismiss_overlay(logger, hwnd)
        _leave_retail_shop(logger, hwnd, stop_check=stop_check)
        time.sleep(1.5)
    result.update(status="failed", detail="多次尝试后仍未完成购买")
    return result


def _enter_club_page(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """主界面左上第 4 个图标进俱乐部；中途弹「新物品已存入仓库」点空白继续。"""
    if _stopped(stop_check):
        return False
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        return False
    _focus_game_window(logger, hwnd)
    _dismiss_overlay(logger, hwnd)
    center = _template_center(logger, hwnd, CLUB_ENTRY_TEMPLATE, frac_region=CLUB_ENTRY_REGION, threshold=0.85)
    if center:
        _pyautogui_click_guarded(*center)
        logger.info(f"[白荆回廊·俱乐部] 点击左上角俱乐部入口图标 @ {center}")
    else:
        _click_ratio(logger, hwnd, CLUB_ENTRY_FALLBACK, "「俱乐部」入口图标（比例兜底）")
    time.sleep(2.0)
    # 直接进页面就最好；没进去通常是弹了「新物品已存入S.E.E.D.仓库」这类提示
    if _wait_page_markers(logger, hwnd, CLUB_PAGE_MARKERS, timeout=6.0, stop_check=stop_check):
        logger.info("[白荆回廊·俱乐部] 已进入俱乐部页面")
        return True
    logger.info("[白荆回廊·俱乐部] 未见俱乐部页面特征，点击空白区域继续")
    _dismiss_overlay(logger, hwnd)
    _click_ratio(logger, hwnd, CLUB_BLANK_CLICK, "空白区域（继续进入）")
    time.sleep(1.5)
    if _wait_page_markers(logger, hwnd, CLUB_PAGE_MARKERS, timeout=8.0, stop_check=stop_check):
        logger.info("[白荆回廊·俱乐部] 点击空白区域后已进入俱乐部页面")
        return True
    # 2026-10-22：两次都没进页面，重试一次入口点击（弹窗/加载挡掉首次点击时），
    # 再次以页面特征文字确认——不凭"点了就算"
    logger.info("[白荆回廊·俱乐部] 仍未进页面，重试点击俱乐部入口")
    _dismiss_overlay(logger, hwnd)
    center = _template_center(logger, hwnd, CLUB_ENTRY_TEMPLATE, frac_region=CLUB_ENTRY_REGION, threshold=0.85)
    if center:
        _pyautogui_click_guarded(center[0], center[1])
        logger.info(f"[白荆回廊·俱乐部] 重试点击俱乐部入口图标 @ {center}")
    else:
        _click_ratio(logger, hwnd, CLUB_ENTRY_FALLBACK, "「俱乐部」入口图标（比例兜底）")
    time.sleep(2.0)
    if not _wait_page_markers(logger, hwnd, CLUB_PAGE_MARKERS, timeout=8.0, stop_check=stop_check):
        logger.warning("[白荆回廊·俱乐部] 未进入俱乐部页面")
        return False
    logger.info("[白荆回廊·俱乐部] 重试后已进入俱乐部页面")
    return True


def _claim_club_daily_task(logger: logging.Logger, hwnd, stop_check=None) -> str:
    """点「俱乐部每日任务」卡片领取，返回 purchased / already / no_claim / failed。

    领完后卡片会变成「今日任务已完成」（实测 2026-10-18 截图），这是唯一的成功凭据：
    页面顶栏那条「今日俱乐部奖励已领取」在进页面前就已存在，不能当领取结果。
    """
    if _ocr_best_match(logger, CLUB_DONE_TEXTS, hwnd, frac_region=CLUB_CARD_REGION, min_conf=0.55):
        logger.info("[白荆回廊·俱乐部] 卡片已是「今日任务已完成」，无需重复领取")
        return "already"
    # 先点卡片标题；有些版本标题即领取热区
    card = _ocr_best_match(
        logger, CLUB_DAILY_TASK_TEXTS, hwnd, frac_region=CLUB_CARD_REGION, min_conf=0.55
    )
    if card:
        _pyautogui_click_guarded(*card["center"])
        logger.info(f"[白荆回廊·俱乐部] 点击「俱乐部每日任务」卡片 @ {card['center']}")
    else:
        _click_ratio(logger, hwnd, CLUB_CARD_FALLBACK, "「俱乐部每日任务」卡片（比例兜底）")
    time.sleep(1.5)
    # 领完后可能弹奖励提示，先清一次
    _dismiss_overlay(logger, hwnd)
    if _ocr_best_match(logger, CLUB_DONE_TEXTS, hwnd, frac_region=CLUB_CARD_REGION, min_conf=0.55):
        logger.info("[白荆回廊·俱乐部] 卡片已变为「今日任务已完成」")
        return "purchased"
    # 若弹出独立「领取」按钮则点掉（卡片点击不一定直接领）
    claim_clicked = False
    claim = _ocr_best_match(logger, CLUB_CLAIM_TEXTS, hwnd, frac_region=CLUB_CARD_REGION, min_conf=0.6)
    if claim:
        claim_clicked = True
        _pyautogui_click_guarded(*claim["center"])
        logger.info(f"[白荆回廊·俱乐部] 点击「领取」@ {claim['center']}")
        time.sleep(1.5)
        _dismiss_overlay(logger, hwnd)
        if _ocr_best_match(logger, CLUB_DONE_TEXTS, hwnd, frac_region=CLUB_CARD_REGION, min_conf=0.55):
            logger.info("[白荆回廊·俱乐部] 点击「领取」后卡片显示「今日任务已完成」")
            return "purchased"
    # 兜底：点卡片中心（标题不是热区时）
    _click_ratio(logger, hwnd, CLUB_CARD_FALLBACK, "卡片中心（领取热区兜底）")
    time.sleep(1.5)
    _dismiss_overlay(logger, hwnd)
    if _ocr_best_match(logger, CLUB_DONE_TEXTS, hwnd, frac_region=CLUB_CARD_REGION, min_conf=0.55):
        logger.info("[白荆回廊·俱乐部] 点击卡片中心后显示「今日任务已完成」")
        return "purchased"
    # 没有可点的「领取」按钮：卡片点击本身没生效更可能是“今日无奖励可领”
    # （任务未完成或奖励已领），不要记成点击失败。只有确实点过「领取」仍未变化才算 failed。
    return "failed" if claim_clicked else "no_claim"


def _leave_club_page(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """俱乐部页返回主界面（左上「返回」→ ESC）。"""
    for attempt in range(1, MAX_CLOSE_ATTEMPTS + 1):
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        _dismiss_overlay(logger, hwnd)
        ok, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=2.0)
        if ok:
            logger.info("[白荆回廊·俱乐部] 已回到主界面")
            return True
        back = _ocr_best_match(logger, ("返回",), hwnd, frac_region=CLUB_BACK_REGION, min_conf=0.55)
        if back:
            _pyautogui_click_guarded(*back["center"])
            logger.info(f"[白荆回廊·俱乐部] 点击「返回」@ {back['center']}")
        else:
            _focus_game_window(logger, hwnd)
            pyautogui.press("escape")
            logger.info(f"[白荆回廊·俱乐部] 按 ESC 返回主界面 (#{attempt}/{MAX_CLOSE_ATTEMPTS})")
        time.sleep(1.5)
    ok, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=2.0)
    if not ok:
        logger.warning("[白荆回廊·俱乐部] 多次尝试后仍未回到主界面")
    return ok


def claim_club_daily_reward(logger: logging.Logger, hwnd, stop_check=None) -> Dict:
    """领俱乐部每日任务奖励（每日刷新，2026-10-18 用户截图实测）。

    返回 {"status", "detail"}：status = success / already / failed / skipped。
    """
    result = {"status": "skipped", "detail": ""}
    if _stopped(stop_check):
        result["detail"] = "用户停止"
        return result
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        result.update(status="failed", detail="未找到游戏窗口")
        return result
    # 2026-10-22 修复：同零售礼包，自己确保回到主界面，不再被 back_ok 连坐跳过
    if not _ensure_main_ui(logger, hwnd, stop_check=stop_check):
        result.update(status="skipped", detail="多次尝试仍未回到主界面，跳过俱乐部")
        logger.info(f"[白荆回廊·俱乐部] {result['detail']}")
        return result
    if not _enter_club_page(logger, hwnd, stop_check=stop_check):
        result.update(status="failed", detail="未进入俱乐部页面")
        _leave_club_page(logger, hwnd, stop_check=stop_check)
        return result
    _capture_daily_list_snapshot(logger, hwnd, prefix=CLUB_SNAPSHOT_PREFIX)
    state = _claim_club_daily_task(logger, hwnd, stop_check=stop_check)
    if state == "purchased":
        result.update(status="success", detail="已领取俱乐部每日任务奖励")
    elif state == "already":
        result.update(status="already", detail="俱乐部每日任务今日已完成")
    elif state == "no_claim":
        result.update(
            status="skipped",
            detail="卡片无「领取」/「今日任务已完成」，今日可能无奖励可领（任务未完成或已领取）",
        )
    else:
        result.update(status="failed", detail="点击「领取」后卡片未变为「今日任务已完成」")
    back_ok = _leave_club_page(logger, hwnd, stop_check=stop_check)
    result["returned_main"] = back_ok
    logger.info(f"[白荆回廊·俱乐部] 结果 {result['status']}：{result['detail']}，回主界面={back_ok}")
    return result


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
    # 3) 第一次点「快速战斗」→ 设置弹窗
    _click_ratio(logger, hwnd, IMAGING_QUICK_BATTLE_BTN, "「快速战斗」（关卡页）")
    # 4) 等弹窗 → 第二次点「快速战斗」确认（精确匹配优先，不会误点弹窗标题）
    if _wait_page_markers(logger, hwnd, IMAGING_DIALOG_MARKERS, timeout=6.0, stop_check=stop_check):
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
    else:
        logger.warning("[白荆回廊·日常] 未见快速战斗设置弹窗，按比例兜底再点一次")
        _click_ratio(logger, hwnd, IMAGING_DIALOG_OK_BTN, "「快速战斗」确认（兜底）")
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


def open_daily_task_list(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """从主界面点击「任务」并确认日常任务列表打开（含切到日常页）。"""
    for attempt in range(1, MAX_OPEN_ATTEMPTS + 1):
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            logger.error("[白荆回廊·日常] 游戏窗口丢失，无法打开任务列表")
            return False
        _focus_game_window(logger, hwnd)
        found = _find_task_button(logger, hwnd, stop_check)
        if not found:
            logger.warning(f"[白荆回廊·日常] 第 {attempt}/{MAX_OPEN_ATTEMPTS} 次：未找到「任务」按钮")
            time.sleep(1.5)
            continue
        (x, y), method = found
        _pyautogui_click_guarded(x, y)
        logger.info(f"[白荆回廊·日常] 已点击「任务」按钮 [{method}] @ ({x},{y})")
        if _wait_task_list(logger, hwnd, timeout=8.0, stop_check=stop_check):
            # 面板可能停在其它 tab，确保切到「日常」
            if _ensure_daily_tab(logger, hwnd, stop_check=stop_check):
                _capture_daily_list_snapshot(logger, hwnd)
                return True
            logger.warning("[白荆回廊·日常] 任务面板已打开但未能切到日常页")
            return False
        logger.warning(
            f"[白荆回廊·日常] 第 {attempt}/{MAX_OPEN_ATTEMPTS} 次点击后未确认任务列表打开，重试"
        )
        time.sleep(1.5)
    return False


def _claim_rewards_midway(logger: logging.Logger, hwnd, stop_check=None) -> int:
    """中途领取：点固定底栏的「一键领取」并处理领取后的奖励弹窗。

    用户 2026-10-18 建议：每个「前往」类任务完成后顺手领一次，避免已完成奖励
    堆积把后面的任务行挤到可视区外。底栏「一键领取」是固定件（任何滚动位置都
    可见），所以中途领取不需要滚动页面；弹窗用模板关 + 屏幕中心随意点双保险
    （无弹窗时中心点落在任务行文字上，无副作用）。
    返回点击次数（0=当前无可领）。
    """
    if _stopped(stop_check):
        return 0
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        return 0
    hit = _ocr_best_match(logger, CLAIM_ALL_TEXTS, hwnd, min_conf=0.6)
    if not hit:
        return 0
    _pyautogui_click_guarded(*hit["center"])
    logger.info(f"[白荆回廊·日常] 中途「一键领取」@ {hit['center']}")
    time.sleep(1.5)
    # 奖励弹窗处理：先试模板（点击屏幕继续/关闭），再屏幕中心随意点
    _dismiss_overlay(logger, hwnd)
    time.sleep(0.5)
    region = _window_region(hwnd)
    if region:
        cx, cy = region[0] + region[2] // 2, region[1] + region[3] // 2
        _pyautogui_click_guarded(cx, cy)
        logger.info(f"[白荆回廊·日常] 领取后屏幕中心随意点（关可能存在的奖励弹窗）@ ({cx},{cy})")
        time.sleep(0.8)
    return 1


def _dismiss_overlay(logger: logging.Logger, hwnd) -> bool:
    """关闭可能遮挡任务列表的弹窗（「点击屏幕继续」/「关闭」模板）。

    「关闭」模板有一个致命撞车（2026-10-25 实锤）：任务列表面板/俱乐部页/
    零售页这些**宿主面板自己的 X** 也在窗口右上角（(1847,159)@1919x1079，
    conf 0.968 稳定命中），纹样与弹窗 X 同款。没有弹窗时 dismiss 一样会命中
    它、一点就把宿主面板整个关掉——领取循环因此每轮"误关面板→重开"空转
    （12 轮烧完），重扫前奏也因此在"列表已确认在屏"的 1 秒后把面板点没了，
    末轮重扫 6 屏 0 行。弹窗的 X 都在弹窗框内（窗口中央区），永远不会跑到
    窗口极右上角，所以这里把右上角宿主关闭区排除掉，命中即跳过不点。
    """
    for name, tpl in (
        ("点击屏幕继续", str(TEMPLATES_DIR / "白荆回廊-点击屏幕继续.png")),
        ("关闭", str(TEMPLATES_DIR / "白荆回廊-关闭.png")),
    ):
        center = _template_center(logger, hwnd, tpl, threshold=0.85)
        if center:
            if name == "关闭" and _in_host_panel_close_zone(hwnd, center):
                logger.info(
                    f"[白荆回廊·日常] 「关闭」命中宿主面板右上角 X @ {center}，跳过"
                    f"（那是列表面板自己的关闭钮，点了会把面板关掉）"
                )
                continue
            _pyautogui_click_guarded(center[0], center[1])
            logger.info(f"[白荆回廊·日常] 关闭遮挡弹窗（{name}）@ {center}")
            time.sleep(0.8)
            return True
    return False


def _in_host_panel_close_zone(hwnd, center) -> bool:
    """命中点是否落在宿主面板自己的右上角关闭区（窗口相对 fx≥0.88, fy≤0.25）。

    标定：(1847,159)@1919x1079 → fx=0.962/fy=0.147。中央弹窗的 X 最右也就到
    fx≈0.81（1200px 宽弹窗贴右边），与 0.88 之间留了安全带；俱乐部入口图标
    (351,51) fx=0.18 不受影响。
    """
    region = _window_region(hwnd)
    if not region:
        return False
    left, top, width, height = region
    cx, cy = center[0], center[1]
    fx = (cx - left) / width if width else 0.0
    fy = (cy - top) / height if height else 0.0
    return fx >= 0.88 and fy <= 0.25


def process_task_list(
    logger: logging.Logger, hwnd, stop_check=None, max_rounds: int = MAX_LIST_ROUNDS
) -> Dict:
    """任务列表内操作：先滚动扫描全表拿任务状态，多个「前往」类任务自动执行，再一键/逐个领取。

    已实测的「前往」流程（2026-10-18 用户截图）：
    - 研发室：点行内「前往」→「白荆穹顶」弹窗 →「一键收取」→ 点 X/空白关闭回列表；
    - 记忆烙痕经验：点行内「前往」→ 烙痕列表页点首个卡片 → 详情页「强化烙痕」→
      重置 → +1 → 确认 → 动画点击屏幕 → 逐层返回任务面板；
    - 午后茶憩：点行内「前往」→「是否前往茶憩?」确定 → 角色选择页点首个角色 →
      场景内喝茶 → 等待后再来一杯 → 摘要随意点击 → 休息室返回 → 确认 → 主界面；
    - 关卡战斗类（显影行动/自由贸易/定向物资保障/光刻协议，操作相同）：
      点行内「前往」→ 关卡页「快速战斗」→ 设置弹窗再点「快速战斗」→ 结算页随意
      点击 → ESC 逐层返回。
    以上任务完成后对应「领取奖励」均由下方领取循环处理；每个任务完成后还会先做
    一次中途「一键领取」（固定底栏、无需滚动，用户 2026-10-18 建议：避免已完成
    奖励堆积把后面的任务行挤到可视区外）。
    其余「前往」类任务（回廊漫巡/战斗/友情勋章等）会跳转到不同玩法，流程未实测，
    v1 只记录不点击，待按活动类型分别实测后实现。
    """
    # 1) 滚动扫描全表（15 个任务的状态 + 活跃度核算基础）
    states = scan_daily_task_states(
        logger, hwnd, stop_check=stop_check, capture_prefix="daily_scan"
    )
    summary = {
        "claim_all": 0,
        "claim": 0,
        "goto_seen": 0,
        "rounds": 0,
        "scanned": len(states),
        "tasks_total": len(DAILY_TASK_CATALOG),
    }
    # 2) 研发室「前往」任务：流程已知（白荆穹顶弹窗一键收取），自动执行；
    #    收取后该任务应出现「领取奖励」，交给下面的领取循环
    lab_state = states.get("lab_harvest")
    if lab_state and not lab_state["done"] and not _stopped(stop_check):
        if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
            logger.warning("[白荆回廊·日常] 任务列表丢失且重开失败，跳过研发室任务")
        elif _handle_lab_harvest_task(logger, hwnd, stop_check=stop_check):
            lab_state["done"] = True
            lab_state["progress"] = "1/1"
            lab_state["claimable"] = True
            logger.info("[白荆回廊·日常] 研发室任务已收取，等待领取奖励")
            summary["claim_all"] += _claim_rewards_midway(logger, hwnd, stop_check=stop_check)
    # 3) 记忆烙痕经验「前往」任务：流程已知（任选烙痕→强化烙痕→重置→+1→确认），
    #    强化后该任务应出现「领取奖励」，交给下面的领取循环
    mem_state = states.get("memory_exp")
    if mem_state and not mem_state["done"] and not _stopped(stop_check):
        if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
            logger.warning("[白荆回廊·日常] 任务列表丢失且重开失败，跳过记忆烙痕任务")
        elif _handle_memory_exp_task(logger, hwnd, stop_check=stop_check):
            mem_state["done"] = True
            mem_state["progress"] = "1/1"
            mem_state["claimable"] = True
            logger.info("[白荆回廊·日常] 记忆烙痕经验任务已强化，等待领取奖励")
            summary["claim_all"] += _claim_rewards_midway(logger, hwnd, stop_check=stop_check)
    # 4) 午后茶憩「前往」任务：流程已知（选角色→喝茶→再来一杯→摘要→返回确定），
    #    茶憩后该任务应出现「领取奖励」，交给下面的领取循环
    tea_state = states.get("tea_break")
    if tea_state and not tea_state["done"] and not _stopped(stop_check):
        if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
            logger.warning("[白荆回廊·日常] 任务列表丢失且重开失败，跳过午后茶憩任务")
        elif _handle_tea_break_task(logger, hwnd, stop_check=stop_check):
            # 不乐观写 done：茶憩返回 True 只代表流程走完（能回到面板），不代表
            # 游戏内任务真的 1/1（2026-10-25 19:06 假成功实锤：本地记成功、
            # 游戏内仍 0/1）。任务状态交给下面的最终重扫，重扫失败就保守判未完成。
            logger.info("[白荆回廊·日常] 午后茶憩流程已执行，任务状态以重扫为准")
            summary["claim_all"] += _claim_rewards_midway(logger, hwnd, stop_check=stop_check)
    # 5) 友情勋章「前往」任务：优先点行内「前往」，未进好友页则回主界面点左上角
    #    第 2 个功能图标（好友）→「一键领取/赠送」→ 处理弹窗 → 重开任务列表
    friend_state = states.get("friend_medal")
    if friend_state and not friend_state["done"] and not _stopped(stop_check):
        if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
            logger.warning("[白荆回廊·日常] 任务列表丢失且重开失败，跳过友情勋章任务")
        elif _handle_friend_medal_task(logger, hwnd, stop_check=stop_check):
            # 友情勋章的“成功”只能靠任务行 1/1 或「领取奖励」确认，弹窗可能是错误提示；
            # 这里不手动改 done，交给处理完的重扫判定，避免把失败提示当成功。
            logger.info("[白荆回廊·日常] 友情勋章流程已执行，任务状态以重扫为准")
            summary["claim_all"] += _claim_rewards_midway(logger, hwnd, stop_check=stop_check)
    # 6) 关卡战斗类「前往」任务（显影行动/自由贸易/定向物资保障/光刻协议，
    #    操作相同：快速战斗×2 → 结算随意点 → ESC 返回），逐个处理；
    #    战斗后各任务应出现「领取奖励」，交给下面的领取循环
    # 「完成5次战斗」需要 5 场：四个关卡任务各 1 场只有 4 场，给本批第一个
    # 实际执行的关卡加一次指定次数（2），正好凑满 5（2026-10-25 用户指定）。
    double_battle_pending = True
    for task_key, row_keyword, task_label in STAGE_BATTLE_TASKS:
        if _stopped(stop_check):
            break
        st = states.get(task_key)
        if not st or st["done"]:
            continue
        if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
            logger.warning(f"[白荆回廊·日常] 任务列表丢失且重开失败，跳过{task_label}任务")
            break
        battle_count = 2 if double_battle_pending else 1
        if battle_count == 2:
            logger.info(f"[白荆回廊·日常] {task_label}使用指定次数=2（凑满完成5次战斗）")
        if _handle_stage_battle_task(
            logger, hwnd, row_keyword, task_label,
            battle_count=battle_count, stop_check=stop_check,
        ):
            double_battle_pending = False
            st["done"] = True
            st["progress"] = "1/1"
            st["claimable"] = True
            logger.info(f"[白荆回廊·日常] {task_label}任务已完成，等待领取奖励")
            summary["claim_all"] += _claim_rewards_midway(logger, hwnd, stop_check=stop_check)
    # 7) 领取（一键领取在固定底栏，一次点完所有已完成任务）
    for rnd in range(1, max_rounds + 1):
        if _stopped(stop_check):
            break
        summary["rounds"] = rnd
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            logger.error("[白荆回廊·日常] 游戏窗口丢失，中断列表操作")
            break
        # 0) 先关掉可能遮挡列表的弹窗（领取奖励后的弹窗等）
        _dismiss_overlay(logger, hwnd)
        # 0.5) 确认列表还在屏（不在则重开；10-21 空转根因修复）
        if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
            logger.warning("[白荆回廊·日常] 领取阶段任务列表丢失且重开失败，结束列表操作")
            break
        # 1) 一键领取
        hit = _ocr_best_match(logger, CLAIM_ALL_TEXTS, hwnd, min_conf=0.6)
        if hit:
            _pyautogui_click_guarded(*hit["center"])
            summary["claim_all"] += 1
            logger.info(f"[白荆回廊·日常] 点击「一键领取」@ {hit['center']}")
            time.sleep(1.5)
            continue
        # 2) 单个「领取」（排除「一键/全部」字样，避免重复计数）
        hit = _ocr_best_match(logger, CLAIM_TEXTS, hwnd, min_conf=0.6, exclude=CLAIM_EXCLUDE)
        if hit:
            _pyautogui_click_guarded(*hit["center"])
            summary["claim"] += 1
            logger.info(f"[白荆回廊·日常] 点击「领取」@ {hit['center']}")
            time.sleep(1.2)
            continue
        # 3) 「前往」类未完成任务：只记录不点击（研发室/记忆烙痕/午后茶憩/显影行动已单独处理，
        #    其余前往流程未实测）
        goto_hit = _ocr_best_match(logger, GOTO_TEXTS, hwnd, min_conf=0.6)
        if goto_hit:
            summary["goto_seen"] += 1
            logger.info(
                f"[白荆回廊·日常] 检测到「前往」类未完成任务 @ {goto_hit['center']}，"
                f"暂不自动跳转（需按活动类型分别实现）"
            )
        break
    # 8) 处理完成后重新扫描一次，拿到战斗/茶憩等顺带推进的真实状态。
    #    原实现只用手动标记的 states 算活跃度，「完成2次战斗」「击败60」这类
    #    被战斗顺带推进的任务永远不会计入。重扫失败只告警，不影响主流程。
    initial_done = sum(1 for s in states.values() if s.get("done"))
    if not _stopped(stop_check) and initial_done < len(DAILY_TASK_CATALOG):
        hwnd = _ensure_window(logger, hwnd)
        if hwnd:
            # 顺序必须是 先关弹窗 → 再确认列表在屏：2026-10-25 实锤原顺序
            # （先 ensure 后 dismiss）下 dismiss 一旦动到面板，紧接着的重扫就
            # 对着非列表界面扫 6 屏全 0 行。先 dismiss 再 ensure 可以自愈。
            _dismiss_overlay(logger, hwnd)
            if _ensure_task_list_open(logger, hwnd, stop_check=stop_check) and _scroll_row_list_to_top(logger, hwnd):
                logger.info("[白荆回廊·日常] 处理完成后重新扫描任务状态…")
                try:
                    final_states = scan_daily_task_states(
                        logger,
                        hwnd,
                        stop_check=stop_check,
                        capture_prefix="daily_scan_final",
                    )
                except Exception as e:
                    final_states = {}
                    logger.warning(f"[白荆回廊·日常] 重新扫描任务状态失败（已忽略）: {e}")
                merged = 0
                for key, st in final_states.items():
                    old = states.get(key)
                    if old is None:
                        states[key] = st
                        merged += 1
                        continue
                    # 任一来源判定完成都保留，避免 OCR 波动抹掉处理器已确认的结果
                    old["done"] = bool(old.get("done") or st.get("done"))
                    old["claimable"] = bool(old.get("claimable") or st.get("claimable"))
                    if st.get("progress") and st["progress"] != "0/1":
                        old["progress"] = st["progress"]
                logger.info(
                    f"[白荆回廊·日常] 重新扫描完成: {len(final_states)}/{len(DAILY_TASK_CATALOG)} 项，"
                    f"补入新状态 {merged} 项"
                )
    # 9) 活跃度核算：优先读游戏面板顶部的「累计活跃度」大数字（权威值），
    #    读不到才退回静态目录核算。2026-10-27 用户实锤：目录算出 90，游戏面板
    #    105 且五档奖励全领；目录会随版本漂移（当天任务是「完成2次战斗」，
    #    目录写「完成3次战斗」），不能再用目录和当唯一口径。
    #    activity_earned=原始实得（可超 100）；activity_total=领满线 100；
    #    activity_max=理论总分（面板值更高时跟随面板）。
    done_keys = [k for k, s in states.items() if s.get("done")]
    catalog_earned = sum(
        DAILY_ACTIVITY_BY_KEY[k] for k in done_keys if DAILY_ACTIVITY_BY_KEY.get(k)
    )
    game_activity = _read_activity_number(logger, hwnd)
    if game_activity is not None:
        earned = game_activity
        summary["activity_source"] = "game_panel"
        logger.info(
            f"[白荆回廊·日常] 活跃度口径: 面板 {game_activity}，"
            f"目录核算 {catalog_earned} → 以面板为准"
        )
    else:
        earned = catalog_earned
        summary["activity_source"] = "catalog"
        logger.warning(
            f"[白荆回廊·日常] 面板活跃度读取失败，退回目录核算 {catalog_earned}"
        )
    summary["activity_catalog"] = catalog_earned
    summary["activity_earned"] = earned
    summary["activity_total"] = DAILY_ACTIVITY_TARGET
    summary["activity_max"] = max(DAILY_ACTIVITY_TOTAL, earned)
    summary["scanned"] = len(states)
    summary["done"] = len(done_keys)
    # states 一并带回调用方：入口要做活跃度记录（历史上入口直接引用 states
    # 会 NameError，把日常记录整段炸掉）
    summary["states"] = states
    logger.info(
        f"[白荆回廊·日常] 列表操作结束: 一键领取x{summary['claim_all']} "
        f"逐个领取x{summary['claim']} 前往类x{summary['goto_seen']} "
        f"任务完成 {summary['done']}/{summary['tasks_total']} "
        f"活跃度 {earned}/{summary['activity_max']}"
        f"（来源 {summary['activity_source']}，目录核算 {summary['activity_catalog']}，"
        f"领满线 {DAILY_ACTIVITY_TARGET}） (共 {summary['rounds']} 轮)"
    )
    return summary


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


def close_task_list(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """关闭任务列表并确认回到主界面。

    先走 _ensure_main_ui（检测→关闭→ESC）；3 轮没回主界面的中间态，
    2026-10-22 起再试一次「重开任务列表 → 关闭」——列表面板有可靠的关闭
    按钮模板，重开刷新状态比继续盲按 ESC 更有效（10-21 实锤 ESC/点关闭
    对中间态无效，close 判失败连带跳过俱乐部/礼包）。
    """
    if _ensure_main_ui(logger, hwnd, stop_check=stop_check):
        return True
    if _stopped(stop_check):
        return False
    logger.info("[白荆回廊·日常] 直接关闭失败，尝试重开任务列表再关闭")
    if open_daily_task_list(logger, hwnd, stop_check=stop_check):
        return _ensure_main_ui(logger, hwnd, stop_check=stop_check, attempts=2)
    return False


def run_baijing_daily_vision_flow(
    logger: logging.Logger, hwnd=None, stop_check=None
) -> bool:
    """白荆回廊视觉版日常流程入口（由 run_baijing_full_flow 调用）。

    主界面 → 点击「任务」→ 日常任务列表 → 领取 → 关闭列表回主界面 →
    有猫零售领「每日外勤赠礼」免费礼包。
    返回 True 表示成功打开并处理了任务列表（领没领到奖励都算成功，
    具体数量见 daily_progress extra.summary，零售结果见 extra.free_gift_pack）。
    """
    logger.info(
        "=" * 50 + "\n[白荆回廊·日常] 视觉版日常流程开始（主界面 → 任务 → 日常任务列表）\n" + "=" * 50
    )

    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        logger.error("[白荆回廊·日常] 未找到游戏窗口")
        _record_baijing_task(logger, "daily", "日常任务", "failed", "未找到游戏窗口")
        return False

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1
    _focus_game_window(logger, hwnd)
    time.sleep(0.8)

    # 1) 确认主界面（未识别到则 ESC 恢复一次再判）
    is_main, evidence = detect_baijing_main_ui(logger, hwnd, budget_sec=4.0)
    if not is_main and not _stopped(stop_check):
        logger.info("[白荆回廊·日常] 未识别到主界面，尝试 ESC 恢复后重判")
        pyautogui.press("escape")
        time.sleep(1.5)
        is_main, evidence = detect_baijing_main_ui(logger, hwnd, budget_sec=4.0)
    if not is_main:
        logger.error(f"[白荆回廊·日常] 未处于主界面，放弃本次日常（证据: {evidence}）")
        _record_baijing_task(logger, "daily", "日常任务", "failed", "未处于主界面")
        return False
    logger.info("[白荆回廊·日常] 已确认处于主界面")

    # 2) 打开日常任务列表
    if not open_daily_task_list(logger, hwnd, stop_check=stop_check):
        if _stopped(stop_check):
            _record_baijing_task(logger, "daily", "日常任务", "skipped", "用户停止")
            return False
        _record_baijing_task(logger, "daily", "日常任务", "failed", "未能打开日常任务列表")
        return False

    # 3) 列表内领取（日常 tab：扫描 + 前往类任务 + 一键/逐个领取）
    summary = process_task_list(logger, hwnd, stop_check=stop_check)

    # 3.5) 叶脉联结领取（日常完毕后：任务总览 → 全部领取；奖励与日常 tab 不互通）
    leaf_ok = False
    if not _stopped(stop_check):
        leaf_ok = _claim_leaf_vein_rewards(logger, hwnd, stop_check=stop_check)
        if not leaf_ok:
            logger.warning("[白荆回廊·日常] 叶脉联结领取未完成（不影响日常结果记录）")
            # 2026-10-25 用户拍板：叶脉失败此前只打 warning，flow_failures 查不到，
            # 漏领无迹可寻。补结构化埋点（截图+当帧文字），与俱乐部/礼包同口径。
            _record_step_failure(logger, hwnd, "叶脉联结领取", "未进入叶脉联结页或领取失败")

    # 4) 关闭列表，回主界面。back_ok 仅作记录，不再门禁后续步骤：
    #    2026-10-22 起俱乐部/零售各自 _ensure_main_ui 自保，close 失败不再连坐跳过
    #    （10-21 实锤：close 被引导气泡带偏 → 俱乐部+免费礼包整体跳过且无记录）。
    back_ok = close_task_list(logger, hwnd, stop_check=stop_check)
    if not back_ok and not _stopped(stop_check):
        logger.warning("[白荆回廊·日常] 关闭列表后未确认回主界面，后续步骤将自行恢复")

    # 4.5) 俱乐部每日任务奖励（活跃度 100 里程碑领完之后顺手领：主界面左上第 4 个
    #      图标进俱乐部 → 空白继续 → 点「俱乐部每日任务」卡片领取）
    club = {"status": "skipped", "detail": "未执行"}
    if not _stopped(stop_check):
        club = claim_club_daily_reward(logger, hwnd, stop_check=stop_check)
    else:
        club["detail"] = "用户停止"
    _record_baijing_task(
        logger,
        "club_daily",
        "俱乐部每日任务",
        club["status"],
        club["detail"],
        extra=dict(club),
    )
    if club["status"] == "failed":
        _record_step_failure(logger, hwnd, "俱乐部每日任务", club["detail"])

    # 5) 有猫零售每日免费礼包（主界面底栏「有猫零售」→ 精选礼包 → 有猫精选 →
    #    「每日外勤赠礼」卡底「免费」→ 购买弹窗「购买」）。免费礼包每日刷新、
    #    8 小时重置，错过要等刷新，所以放在日常尾部独立记录一项；
    #    skipped 也记录（10-21 跳过无记录，用户看不到漏买）。
    gift = {"status": "skipped", "purchased": False, "detail": "未执行", "attempts": 0}
    if not _stopped(stop_check):
        gift = buy_daily_free_gift_pack(logger, hwnd, stop_check=stop_check)
    else:
        gift["detail"] = "用户停止"
    if gift["status"] == "failed":
        _record_step_failure(logger, hwnd, "每日外勤赠礼", gift["detail"])
    _record_baijing_task(
        logger,
        "daily_gift",
        "每日外勤赠礼",
        gift["status"],
        gift["detail"],
        extra=gift,
    )

    if _stopped(stop_check):
        _record_baijing_task(logger, "daily", "日常任务", "skipped", "用户停止")
        return False

    states = summary.get("states") or {}
    # states 提到 extra 顶层，summary 里剔掉，避免同一份任务状态在 JSON 里存两遍
    summary_view = {k: v for k, v in summary.items() if k != "states"}
    extra = {
        "summary": summary_view,
        "states": states,
        "returned_main": back_ok,
        "leaf_vein_claimed": leaf_ok,
        "club_daily": club,
        "free_gift_pack": gift,
        "engine": "vision",
    }
    msg = (
        f"一键领取x{summary['claim_all']} 逐个领取x{summary['claim']} "
        f"前往类x{summary['goto_seen']}"
    )
    done = summary["done"]
    capped = min(summary["activity_earned"], DAILY_ACTIVITY_TARGET)
    # 状态仍保持 success（避免触发整条任务重跑），但消息必须如实反映完成度：
    # 以前只按“有没有点过领取”写“已完成”，出现 2/15、10/100 也报成功的情况。
    if done >= summary["tasks_total"] or capped >= DAILY_ACTIVITY_TARGET:
        status, message = "success", (
            f"已完成（活跃度 {capped}/{DAILY_ACTIVITY_TARGET}，"
            f"任务 {done}/{summary['tasks_total']}；{msg}）"
        )
    elif done > 0 or summary["activity_earned"] > 0:
        status, message = "success", (
            f"部分完成（活跃度 {capped}/{DAILY_ACTIVITY_TARGET}，"
            f"任务 {done}/{summary['tasks_total']}；{msg}）"
        )
    else:
        status, message = "success", f"未完成任何日常任务（活跃度 0/{DAILY_ACTIVITY_TARGET}；{msg}）"
    _record_baijing_task(logger, "daily", "日常任务", status, message, extra=extra)
    # 活跃度计数任务（与 wuwa「今日活跃」同口径：extra.current/total 供
    # daily_progress_tracker.real_progress_percent 计算真实进度百分比）。
    # current 按领满线 100 封顶（实得可超 100，原始值保留在 extra.earned）；
    # 满 100 即日常里程碑奖励全领完（用户 2026-10-18 确认）。
    if states:
        earned = summary["activity_earned"]
        capped = min(earned, DAILY_ACTIVITY_TARGET)
        _record_baijing_task(
            logger,
            "activity",
            "活跃度",
            "success",
            f"活跃度 {capped}/{DAILY_ACTIVITY_TARGET}"
            f"（实得 {earned}/{summary['activity_max']}），"
            f"任务完成 {summary['done']}/{summary['tasks_total']}",
            extra={
                "current": capped,
                "total": DAILY_ACTIVITY_TARGET,
                "earned": earned,
                "max": summary["activity_max"],
                "done": summary["done"],
                "tasks_total": summary["tasks_total"],
            },
        )
    logger.info(
        f"[白荆回廊·日常] 视觉版日常流程完成: {message} "
        f"活跃度 {summary['activity_earned']}/{summary['activity_max']}"
        f"（领满线 {DAILY_ACTIVITY_TARGET}） "
        f"叶脉联结领取={leaf_ok} 免费礼包={gift['status']}({gift['detail']}) "
        f"回主界面={back_ok}"
    )
    return True
