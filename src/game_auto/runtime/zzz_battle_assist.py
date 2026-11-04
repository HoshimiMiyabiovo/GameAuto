"""绝区零自动战斗（移植自 OneDragon，2026-11-01）。

来源与授权：ZenlessZoneZero-OneDragon（GPL-3.0，本地自用移植）。照抄的部分：
- ``src/one_dragon/yolo/yolov8_onnx_cls.py`` + ``onnx_utils.py`` → 闪光分类器推理
  （letterbox 到 640×640、左上补 114、/255、HWC→CHW，输出 3 类概率）
- ``src/zzz_od/auto_battle/auto_battle_dodge_context.py`` → 闪光闪避判定
  （class_idx 1=红闪光→闪避，2=黄闪光→格挡）
- ``assets/game_data/screen_info/battle.yml`` → 下面这些识别区域（1920×1080 基准）
- ``assets/template/battle/btn_*.png`` → 按钮模板（已复制到 ``assets/templates/绝区零-战斗-*.png``）
- ``src/zzz_od/auto_battle/auto_battle_context.py`` → 「普攻按钮在=在战斗」
  「禁用图标=按键被封」这两条判定

**主动砍掉的部分**（用户 2026-11-01 明确给了替代做法）：
- Yolo 距离识别 + 自动转向（``lost_void_det`` 模型）→ 用户：「向前 W 走两秒」
- 音频闪避（soundcard + librosa + scipy + sklearn 四个重依赖）→ 只留视觉闪光
- 24k 行 YAML 配队策略 + 条件状态机引擎 → 换成**针对固定配队手写的循环**
  （见下方「异常队轮转」：三种属性轮流站场叠异常触发紊乱）
- 逐角色 agent_state（浮晖/燔焰/风华 层数识别）→ 先用固定节奏轮转代替，
  层数识别留作后续优化（需要 `assets/template/agent_state/*` 的模板和实机校准）
- 连携技头像模板匹配（OneDragon 给每个代理人存了 avatar_chain_*.png）→ 改用
  连携区域亮度判断：连携时中央会亮起两个大按钮，暗背景上一眼能分开

决策顺序照抄 OneDragon「全配队通用」的主干：闪避最优先（被打断就白打），
其次终结技（窗口错过就没了），最后普攻填充。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Dict, Optional, Tuple

import numpy as np
import pyautogui

from ..settings.paths import BASE_DIR
from . import zzz_agent_state as zas

LOGGER = logging.getLogger("GameAutomation")

LABEL = "自动战斗"

# ---------------------------------------------------------------------------
# 识别区域：抄自 OneDragon assets/game_data/screen_info/battle.yml（1920×1080 基准）
# 括号是 (x1, y1, x2, y2)，按实际画面尺寸等比换算。
# ---------------------------------------------------------------------------
BASE_W, BASE_H = 1920, 1080

AREA_BTN_NORMAL = (1403, 916, 1505, 1017)      # 按键-普通攻击
AREA_BTN_SPECIAL = (1620, 917, 1720, 1017)     # 按键-特殊攻击
AREA_BTN_ULTIMATE = (1728, 778, 1826, 876)     # 按键-终结技
AREA_CHAIN_1 = (479, 828, 597, 946)            # 连携技-1（屏幕中偏左）
AREA_CHAIN_2 = (1322, 828, 1440, 946)          # 连携技-2（屏幕中偏右）

# 模板（相对项目根目录）
TPL_NORMAL_ATTACK = "assets/templates/绝区零-战斗-普通攻击.png"
TPL_BAN_NORMAL = "assets/templates/绝区零-战斗-普攻禁用.png"
TPL_BAN_ULTIMATE = "assets/templates/绝区零-战斗-大招禁用.png"
TPL_SPECIAL_1 = "assets/templates/绝区零-战斗-特殊技1.png"
TPL_SPECIAL_2 = "assets/templates/绝区零-战斗-特殊技2.png"
TPL_ULTIMATE_1 = "assets/templates/绝区零-战斗-大招1.png"
TPL_ULTIMATE_2 = "assets/templates/绝区零-战斗-大招2.png"
TPL_DODGE = "assets/templates/绝区零-战斗-闪避.png"
TPL_INTERACT = "assets/templates/绝区零-战斗-交互.png"

# 闪光分类器（照抄 OneDragon 的模型目录结构；5.7MB 放 data/ 不进版本库）
FLASH_MODEL_DIR = "data/models/flash_classifier/yolov8n-640-flash-20250921"
FLASH_MODEL_NAME = "model.onnx"

MATCH_THRESHOLD = 0.9         # 通用模板阈值（OneDragon 同值）
# 实测（2026-11-01 用户战斗截图 1919×1079）：
#   普攻 0.982 / 0.966，特殊技 0.865 / 0.876，大招可用 0.820、不可用 0.602。
# OneDragon 用 0.9 是因为它自己截 PNG 无损、尺寸精确；这里源图有压缩，
# 所以按实测把三条线分开，大招靠 0.72 卡在"可用 0.82 / 灰 0.60"之间。
ATTACK_THRESHOLD = 0.85       # 普攻按钮（兼作"在战斗画面"判据）
SKILL_THRESHOLD = 0.78        # 特殊技 / 终结技按钮
ULTIMATE_THRESHOLD = 0.72
FLASH_CONF = 0.9              # 闪光分类置信度（OneDragon 默认）
CLASS_NONE, CLASS_RED, CLASS_YELLOW = 0, 1, 2

# 连携技：两个 118×118 区域的平均亮度。暗背景约 20-40，亮起的连携按钮 90+。
CHAIN_BRIGHTNESS = 70.0

# ---------------------------------------------------------------------------
# 异常队（蕾米埃尔 / 菲欧妮 / 维琳娜）专用轮转参数
#
# 这三个在 OneDragon 的角色表里都是 ANOMALY：
#   蕾米埃尔 remielle  LUMIFLUX（流光）  状态：浮晖（层数条）/ 虚曜（连接）
#   菲欧妮   phoenix   FIRE（火）        状态：燔焰（层数条）/ 升变（连接）
#   维琳娜   velina    WIND（风）        状态：风华（层数条）
# 三种不同属性 = 典型紊乱队：A 打出异常后，B 用另一种属性再打出异常 → 紊乱。
# 所以正确打法不是"谁站场谁一直连点"，而是**三人轮流上场各自把 E 打出去**，
# 通用策略恰恰不懂这件事（它连这三个人的状态模板都不引用）。
#
# 这里用固定节奏轮转 + 技能优先来还原这个循环，不去识别异常条/层数：
# 每 ``DWELL_SEC`` 秒换人，站场期间先放 E（强化特殊技，积蓄最快）再普攻填充，
# Q 一有就放（不换人、不等待）。识别异常条触发瞬间切人是下一步的优化点。
# ---------------------------------------------------------------------------
# ZZZ PC 默认键位（与 OneDragon ``_KEY_DEFAULTS`` 一致，2026-11-01 二次核实）：
#   普攻=鼠标左键  闪避=Shift（或鼠标右键）  切换下一位=空格  切换上一位=C
#   特殊技=E       终结技=Q                 1/2/3=连携队友（**不是切人**）
# 之前把空格当闪避、1/2/3 当切人是错的，会把「该闪避」变成「切人」。
DODGE_KEY = "shift"              # 闪避
SWITCH_KEY = "space"             # 切换下一位角色（循环，三人队正好轮转）
TEAM_SIZE = 3                    # 固定三人队（蕾米埃尔/菲欧妮/维琳娜）
DWELL_SEC = 6.0                  # 状态读不到时的兜底站场时长
SWITCH_SETTLE_SEC = 0.6          # 切人动画期间不出招
SWITCH_COOLDOWN = 1.0            # 两次切人最小间隔（防止连按）

# 长按时长 / 重复间隔：全部照抄 OneDragon 的操作模板（按下 → 每 0.5s 重复按下 → 松开）
PHOENIX_BURST_SEC = 3.5          # 菲欧妮「菲欧妮-长按普攻-焚罪」：7×0.5s
VELINA_HOLD_SEC = 3.0            # 维琳娜「维琳娜-长按普攻」：6×0.5s
REMIELLE_HOLD_SEC = 2.5          # 蕾米埃尔「蕾米埃尔-长按强化特殊技」：0.5+0.5+0.5+1
HOLD_REPEAT_SEC = 0.5

# 节奏（秒）
ATTACK_INTERVAL = 0.12        # 普攻连点间隔
DODGE_COOLDOWN = 0.6          # 闪避冷却，避免连按把闪避次数耗光
DODGE_HOLD = 0.06             # 闪避键按下时长
SPECIAL_COOLDOWN = 0.8        # 特殊技冷却
ULTIMATE_COOLDOWN = 1.0       # 终结技冷却
CHAIN_COOLDOWN = 1.5          # 连携冷却（连携动画比输入慢）
FRAME_MIN_INTERVAL = 0.03     # 每帧最短间隔
FOCUS_REFRESH_SEC = 5.0       # 定期重新聚焦（每帧 SetForegroundWindow 太吵）

# 结束原因
END_WIN = "win"
END_FAIL = "fail"
END_STOPPED = "stopped"
END_TIMEOUT = "timeout"


def _abs(path: str) -> str:
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(str(BASE_DIR), path)


def _read_image(path: str):
    """读带中文路径的图片（cv2.imread 在 Windows 下读不了中文路径）。"""
    import cv2

    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


class FlashClassifier:
    """闪光分类器（照抄 OneDragon ``Yolov8Classifier`` 的预处理与后处理）。

    输出 3 类：0=none / 1=red / 2=yellow。红闪光要闪避，黄闪光要格挡。
    """

    def __init__(self, model_path: str, conf: float = FLASH_CONF):
        self.model_path = model_path
        self.conf = conf
        self._session = None
        self._input_name = ""
        self._height = 640
        self._width = 640

    def load(self) -> bool:
        """惰性加载 onnxruntime（只有真打战斗时才付这个代价）。"""
        if self._session is not None:
            return True
        if not os.path.isfile(self.model_path):
            LOGGER.warning(f"[{LABEL}] 闪光模型不存在: {self.model_path}")
            return False
        try:
            import onnxruntime as ort

            session = ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])
            inp = session.get_inputs()[0]
            self._session = session
            self._input_name = inp.name
            shape = inp.shape
            if len(shape) == 4 and isinstance(shape[2], int) and isinstance(shape[3], int):
                self._height, self._width = int(shape[2]), int(shape[3])
            LOGGER.info(
                f"[{LABEL}] 闪光模型已加载 {os.path.basename(self.model_path)} "
                f"输入 {self._width}x{self._height}"
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 模型加载失败按"没得用"处理
            LOGGER.error(f"[{LABEL}] 闪光模型加载失败: {exc}")
            return False

    @staticmethod
    def prepare(image_rgb: np.ndarray, width: int, height: int) -> np.ndarray:
        """照抄 onnx_utils.scale_input_image_u：等比缩放 + 左上补 114 + /255 + CHW。"""
        import cv2

        img_h, img_w = image_rgb.shape[:2]
        min_scale = min(height / img_h, width / img_w)
        scale_h = max(1, int(round(img_h * min_scale)))
        scale_w = max(1, int(round(img_w * min_scale)))
        canvas = np.full((height, width, 3), 114, dtype=np.uint8)
        resized = cv2.resize(image_rgb, (scale_w, scale_h), interpolation=cv2.INTER_LINEAR)
        canvas[0:scale_h, 0:scale_w, :] = resized
        tensor = canvas / 255.0
        tensor = tensor.transpose(2, 0, 1)
        return tensor[np.newaxis, :, :, :].astype(np.float32)

    def run(self, image_rgb: np.ndarray) -> int:
        """返回 class_idx；置信度不足或未加载返回 -1。"""
        if self._session is None and not self.load():
            return -1
        try:
            tensor = self.prepare(image_rgb, self._width, self._height)
            output = self._session.run(None, {self._input_name: tensor})[0]
            scores = np.squeeze(output)
            idx = int(np.argmax(scores))
            return idx if float(scores[idx]) >= self.conf else -1
        except Exception as exc:  # noqa: BLE001 - 单帧推理失败不该中断战斗
            LOGGER.debug(f"[{LABEL}] 闪光推理失败: {exc}")
            return -1


class ZzzBattleAssist:
    """绝区零自动战斗循环：截图 → 识别 → 按优先级出招。"""

    def __init__(
        self,
        hwnd: int,
        *,
        logger: Optional[logging.Logger] = None,
        model_path: Optional[str] = None,
        enable_ultimate: bool = True,
        enable_special: bool = True,
        enable_chain: bool = False,
        chain_brightness: float = CHAIN_BRIGHTNESS,
        dodge_key: str = DODGE_KEY,
        switch_key: str = SWITCH_KEY,
        dwell_sec: float = DWELL_SEC,
        enable_rotation: bool = True,
    ):
        self.hwnd = hwnd
        self.logger = logger or LOGGER
        self.model_path = model_path or _abs(os.path.join(FLASH_MODEL_DIR, FLASH_MODEL_NAME))
        self.enable_ultimate = enable_ultimate
        self.enable_special = enable_special
        # 连携技默认关：OneDragon 靠每个代理人的 avatar_chain_*.png 头像模板判断，
        # 这里换成了区域亮度，但实测战斗画面该区域本来就是亮的（99.8/105.7），
        # 判断不出「连携提示」，开了反而会在战斗结束时乱按空格。
        # 等实机拿到连携提示帧、定出阈值/模板后再打开。
        self.enable_chain = enable_chain
        self.chain_brightness = float(chain_brightness)

        # 异常队轮转（蕾米埃尔/菲欧妮/维琳娜：流光/火/风 三属性轮流叠异常触发紊乱）
        self.dodge_key = str(dodge_key or DODGE_KEY)
        self.switch_key = str(switch_key or SWITCH_KEY)
        self.dwell_sec = max(1.0, float(dwell_sec))
        self.enable_rotation = bool(enable_rotation)
        self._dwell_start = 0.0
        self._special_done = False
        self._last_switch = 0.0
        self._position = 1              # 队伍位置（进战斗默认 1 号位）
        self._last_states: dict = {}    # 最近一次读到的三个资源条，写日志用

        self.flash = FlashClassifier(self.model_path)
        self._tpl_cache: Dict[Tuple[str, float], Optional[np.ndarray]] = {}

        self._last_attack = 0.0
        self._last_dodge = 0.0
        self._last_special = 0.0
        self._last_ultimate = 0.0
        self._last_chain = 0.0
        self._last_focus = 0.0

        self.stats = {
            "frames": 0, "attacks": 0, "dodges": 0,
            "specials": 0, "ultimates": 0, "chains": 0, "switches": 0,
            "bursts": 0, "holds": 0,
        }

    # -- 底层 ---------------------------------------------------------------
    def _window_rect(self) -> Optional[Tuple[int, int, int, int]]:
        from .foreground_flow_runner import _window_rect

        return _window_rect(self.hwnd)

    def grab(self) -> Optional[np.ndarray]:
        """截游戏窗口，返回 RGB ndarray。"""
        rect = self._window_rect()
        if not rect:
            return None
        left, top, width, height = rect
        try:
            shot = pyautogui.screenshot(region=(left, top, width, height))
        except Exception as exc:  # noqa: BLE001 - 截屏失败（锁屏/RDP）按没画面处理
            self.logger.debug(f"[{LABEL}] 截屏失败: {exc}")
            return None
        return np.array(shot.convert("RGB"))

    def _area_px(self, screen: np.ndarray, area: Tuple[int, int, int, int]) -> Optional[np.ndarray]:
        """把 1920×1080 基准的区域按实际画面尺寸换算并裁出。"""
        h, w = screen.shape[:2]
        x1 = int(area[0] * w / BASE_W)
        y1 = int(area[1] * h / BASE_H)
        x2 = int(area[2] * w / BASE_W)
        y2 = int(area[3] * h / BASE_H)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, max(x1 + 1, x2)), min(h, max(y1 + 1, y2))
        return screen[y1:y2, x1:x2]

    def _template(self, path: str, scale: float) -> Optional[np.ndarray]:
        """按窗口缩放读模板：模板是 1920×1080 下裁的，别的分辨率要跟着缩。"""
        import cv2

        key = (path, round(scale, 3))
        if key in self._tpl_cache:
            return self._tpl_cache[key]
        img = _read_image(_abs(path))
        if img is None:
            self._tpl_cache[key] = None
            return None
        if abs(scale - 1.0) > 0.01:
            nw = max(8, int(round(img.shape[1] * scale)))
            nh = max(8, int(round(img.shape[0] * scale)))
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
        out = img.astype(np.float32)
        self._tpl_cache[key] = out
        return out

    def match_area(
        self,
        screen: np.ndarray,
        area: Tuple[int, int, int, int],
        template: str,
        threshold: float = MATCH_THRESHOLD,
    ) -> bool:
        """在指定区域内做模板匹配（区域限定比整窗稳：82px 小图标整窗容易撞）。"""
        import cv2

        part = self._area_px(screen, area)
        if part is None or part.size == 0:
            return False
        scale = screen.shape[1] / BASE_W
        tpl = self._template(template, scale)
        if tpl is None:
            return False
        if tpl.shape[0] > part.shape[0] or tpl.shape[1] > part.shape[1]:
            return False
        try:
            res = cv2.matchTemplate(part.astype(np.float32), tpl, cv2.TM_CCOEFF_NORMED)
        except Exception as exc:  # noqa: BLE001 - 尺寸不合法等，按未命中
            self.logger.debug(f"[{LABEL}] 模板匹配异常 {template}: {exc}")
            return False
        return float(res.max()) >= threshold

    def _brightness(self, screen: np.ndarray, area: Tuple[int, int, int, int]) -> float:
        part = self._area_px(screen, area)
        if part is None or part.size == 0:
            return 0.0
        gray = part.mean(axis=2)
        return float(gray.mean())

    # -- 识别 ---------------------------------------------------------------
    def in_battle(self, screen: np.ndarray) -> bool:
        """普攻按钮在 = 在战斗画面（照抄 is_normal_attack_btn_available）。"""
        return self.match_area(screen, AREA_BTN_NORMAL, TPL_NORMAL_ATTACK, ATTACK_THRESHOLD)

    def ultimate_ready(self, screen: np.ndarray) -> bool:
        """终结技可用：按钮亮（实测 0.82）且不是灰态（0.60）。

        OneDragon 那套「禁用 X 图标」模板在当前 UI 版本上完全匹配不上（得分约 0），
        所以这里以「亮态模板 + 阈值」为主判据，禁用图标仍然试着排除一次。
        """
        if self.match_area(screen, AREA_BTN_ULTIMATE, TPL_BAN_ULTIMATE, 0.8):
            return False
        return self.match_area(screen, AREA_BTN_ULTIMATE, TPL_ULTIMATE_1, ULTIMATE_THRESHOLD)

    def special_ready(self, screen: np.ndarray) -> bool:
        """特殊技可用（EEP 实测 0.865 / 0.876，两帧都亮）。"""
        return self.match_area(screen, AREA_BTN_SPECIAL, TPL_SPECIAL_1, SKILL_THRESHOLD)

    def chain_prompt(self, screen: np.ndarray) -> bool:
        """连携技提示：屏幕中部左右两个区域亮起（暗背景上亮度差很明显）。"""
        return (
            self._brightness(screen, AREA_CHAIN_1) >= self.chain_brightness
            or self._brightness(screen, AREA_CHAIN_2) >= self.chain_brightness
        )

    def dodge_flash(self, screen: np.ndarray) -> int:
        """闪光类别：1=红（闪避）/2=黄（格挡）/0=none/-1=识别失败。"""
        return self.flash.run(screen)

    # -- 动作 ---------------------------------------------------------------
    @staticmethod
    def _press(key: str, hold: float = 0.03) -> None:
        pyautogui.keyDown(key)
        time.sleep(hold)
        pyautogui.keyUp(key)

    @staticmethod
    def _click() -> None:
        # 战斗高频连点也带按住语义，但用更短的 20ms，避免明显拖慢 APM。
        from .foreground_flow_runner import _pyautogui_click_guarded
        x, y = pyautogui.position()
        _pyautogui_click_guarded(x, y, hold_sec=0.02)

    def _focus(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_focus < FOCUS_REFRESH_SEC:
            return
        from .foreground_flow_runner import _focus_window

        _focus_window(self.hwnd)
        self._last_focus = now

    def do_dodge(self) -> None:
        # 闪避默认 Shift（ZZZ PC 默认；鼠标右键也可以，但右键会转视角，不选）
        self._press(self.dodge_key, DODGE_HOLD)
        self.stats["dodges"] += 1
        self._last_dodge = time.time()

    def do_attack(self) -> None:
        self._click()
        self.stats["attacks"] += 1
        self._last_attack = time.time()

    def do_ultimate(self) -> None:
        self._press("q")
        self.stats["ultimates"] += 1
        self._last_ultimate = time.time()

    def do_special(self) -> None:
        self._press("e")
        self.stats["specials"] += 1
        self._last_special = time.time()

    def do_chain(self) -> None:
        # 连携技按位置选：1/2/3 对应三名队友（这是 ZZZ 的连携键，不是切人键）
        self._press("1")
        self.stats["chains"] += 1
        self._last_chain = time.time()

    def do_switch(self) -> None:
        """切到下一位角色（空格）。

        ZZZ PC 默认：空格=切换下一位、C=切换上一位、Shift=闪避。
        三人队循环切换正好满足「三属性轮流站场」的需求，不需要指定位置；
        如果要固定切到某一位，改 ``switch_key`` 或改用方向键方案。
        """
        self._press(self.switch_key)
        self.stats["switches"] += 1
        self._last_switch = time.time()
        self._dwell_start = time.time()
        self._position = zas.next_position(self._position, TEAM_SIZE)

    def do_hold(self, key: str, seconds: float) -> None:
        """长按一个键指定时长（照抄 OneDragon 的「按下 → 每 0.5s 重复按下 → 松开」写法）。

        长按期间会阻塞主循环（3.5s 的焚罪、2.5s 的强化特殊技都在这段时间里打完），
        OneDragon 同样用「无视闪光」状态屏蔽这段时间的闪避——刻意为之，不是漏了。
        """
        is_mouse = str(key).lower().startswith("mouse")
        down = (lambda: pyautogui.mouseDown(button="left")) if is_mouse else (lambda: pyautogui.keyDown(key))
        up = (lambda: pyautogui.mouseUp(button="left")) if is_mouse else (lambda: pyautogui.keyUp(key))
        down()
        end = time.time() + max(0.0, float(seconds))
        while True:
            remain = end - time.time()
            if remain <= 0:
                break
            time.sleep(min(HOLD_REPEAT_SEC, remain))
            down()   # 重复按下，模拟持续按住
        up()

    # -- 决策 ---------------------------------------------------------------
    def _decide(self, screen: np.ndarray) -> str:
        """按站场角色的资源条决定这一拍做什么。

        规则来自 OneDragon 的三人适配（`速切模板-菲欧妮/维琳娜`、`蕾米埃尔-入场`）：
        - 菲欧妮：燔焰 ≥90 → 长按普攻放焚罪；掉到 30 以下 → 这一轮打完，切走
        - 维琳娜：风华 ≥85 → 长按普攻；低于 30 → 切走
        - 蕾米埃尔：浮晖满 100 → 长按强化特殊技（动作自带切下一位）；低于 20 → 切走
        资源条读不到（None）时退回固定 6 秒节奏轮转，保证不会站着不动。
        """
        agent = zas.agent_at_position(self._position)
        value = None
        if agent:
            try:
                value = zas.read_agent_state(screen, agent, self._position)
            except Exception as exc:  # noqa: BLE001 - 状态读失败按读不到处理
                self.logger.debug(f"[{LABEL}] 读取 {agent} 状态失败: {exc}")
                value = None
            self._last_states[agent] = value

        now = time.time()
        if value is not None:
            if agent == "phoenix":
                if value >= zas.PHOENIX_BURST_AT:
                    return "phoenix_burst"
                if value < zas.PHOENIX_DONE_BELOW:
                    return "switch"
            elif agent == "velina":
                if value >= zas.VELINA_HIGH_AT:
                    return "velina_hold"
                if value < zas.VELINA_DONE_BELOW:
                    return "switch"
            elif agent == "remielle":
                if value >= zas.REMIELLE_FULL_AT:
                    return "remielle_hold"
                if value < zas.REMIELLE_LOW_BELOW:
                    return "switch"

        # 兜底：固定节奏轮转（状态读不到时至少还能轮换叠异常）
        if (
            self.enable_rotation
            and now - self._dwell_start >= self.dwell_sec
            and now - self._last_switch >= SWITCH_COOLDOWN
        ):
            return "switch"
        if (
            self.enable_special
            and self.special_ready(screen)
            and now - self._last_special >= SPECIAL_COOLDOWN
        ):
            return "special"
        if now - self._last_attack >= ATTACK_INTERVAL:
            return "attack"
        return "wait"

    def _execute(self, action: str) -> None:
        """执行一拍动作。长按类动作用 OneDragon 的时长（3.5s / 3s / 2.5s）。"""
        if action == "phoenix_burst":
            self.logger.info(f"[{LABEL}] 菲欧妮 燔焰满 → 长按普攻放焚罪（{PHOENIX_BURST_SEC}s）")
            self.do_hold("mouse_left", PHOENIX_BURST_SEC)
            self.stats["bursts"] += 1
        elif action == "velina_hold":
            self.logger.info(f"[{LABEL}] 维琳娜 风华高 → 长按普攻（{VELINA_HOLD_SEC}s）")
            self.do_hold("mouse_left", VELINA_HOLD_SEC)
            self.stats["holds"] += 1
        elif action == "remielle_hold":
            self.logger.info(f"[{LABEL}] 蕾米埃尔 浮晖满 → 长按强化特殊技（{REMIELLE_HOLD_SEC}s）")
            self.do_hold("e", REMIELLE_HOLD_SEC)
            self.stats["holds"] += 1
            self.do_switch()   # 模板结尾自带「切下一个」，这里同步位置
        elif action == "switch":
            self.do_switch()
        elif action == "special":
            self.do_special()
        elif action == "attack":
            self.do_attack()

    # -- 主循环 -------------------------------------------------------------
    def run(
        self,
        *,
        stop_check: Optional[Callable[[], bool]] = None,
        timeout_sec: float = 300.0,
        idle_limit: int = 6,
    ) -> Tuple[str, str]:
        """打一场，直到普攻按钮连续消失 / 超时 / 用户停止。

        返回 (结果, 说明)：END_WIN / END_STOPPED / END_TIMEOUT。
        「打出结算页」的判定留给调用方（结算页要 OCR，放战斗循环里太慢）。
        """
        start = time.time()
        self._focus(force=True)
        self.logger.info(f"[{LABEL}] 开始（超时 {timeout_sec:.0f}s）")

        idle_frames = 0
        while True:
            if stop_check is not None and stop_check():
                return END_STOPPED, f"用户停止（{self._summary()}）"
            if time.time() - start > timeout_sec:
                return END_TIMEOUT, f"战斗超时 {timeout_sec:.0f}s（{self._summary()}）"

            frame_start = time.time()
            screen = self.grab()
            if screen is None:
                time.sleep(0.1)
                continue
            self.stats["frames"] += 1
            self._focus()

            now = time.time()
            if self.in_battle(screen):
                idle_frames = 0
                if self._dwell_start <= 0:
                    # 刚进战斗：默认 1 号位站场（配队页顺序 蕾米埃尔/菲欧妮/维琳娜）
                    self._dwell_start = now

                cls = self.dodge_flash(screen)
                switching = now - self._last_switch < SWITCH_SETTLE_SEC
                if cls in (CLASS_RED, CLASS_YELLOW) and now - self._last_dodge >= DODGE_COOLDOWN:
                    # 红闪光=闪避、黄闪光=格挡，都是 Shift
                    self.do_dodge()
                elif switching:
                    # 切人动画期间不出招，等落地
                    pass
                elif (
                    self.enable_ultimate
                    and self.ultimate_ready(screen)
                    and now - self._last_ultimate >= ULTIMATE_COOLDOWN
                ):
                    self.do_ultimate()
                else:
                    # 其余交给状态驱动：燔焰/风华/浮晖到阈值才放技能，打空了才切人
                    self._execute(self._decide(screen))
            else:
                idle_frames += 1
                if (
                    self.enable_chain
                    and idle_frames <= 3
                    and self.chain_prompt(screen)
                    and now - self._last_chain >= CHAIN_COOLDOWN
                ):
                    self.do_chain()
                elif idle_frames > idle_limit:
                    return END_WIN, f"普攻按钮消失 {idle_frames} 帧，战斗画面结束（{self._summary()}）"

            elapsed = time.time() - frame_start
            if elapsed < FRAME_MIN_INTERVAL:
                time.sleep(FRAME_MIN_INTERVAL - elapsed)

    def _summary(self) -> str:
        s = self.stats
        return (
            f"帧 {s['frames']} 普攻 {s['attacks']} 闪避 {s['dodges']} "
            f"特殊技 {s['specials']} 终结技 {s['ultimates']} 切人 {s['switches']} "
            f"焚罪 {s['bursts']} 长按 {s['holds']}"
        )
