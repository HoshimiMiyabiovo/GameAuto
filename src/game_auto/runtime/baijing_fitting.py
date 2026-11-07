"""白荆回廊「拟合回归」每周活动（2026-11-07 提权实机探索后落地）。

调用方：`baijing_flow._maybe_run_fitting_weekly`（白荆一条龙已进主界面后）。
**不另建 flow_configs 独立流程**：白荆是 `post_launch_ingame_flow=baijing` 的内置模板
流程，启动器/加载过场/进主界面都有专门逻辑（`baijing_flow`），独立 `run_flow` 要重做
这一整套且会多启动一次客户端。挂在一条龙里用**独立周门控**，俱乐部商店失败也
不影响本任务（两者各写各的 meta）。

实机链路（1920x1080，2026-11-07 20:59~21:20，全程提权点击）：
  主界面「外勤」(1735,700) → 外勤页「拟合回归」(1096,919) → 活动页「拟合开始」
  (1606,964) → 编队页「作战开始」(1579,1001) → 布阵页「开始战斗」(1746,980) →
  战斗自动挂机（当前波次 1/25 → 25/25，约 100 秒）→ 结算页「作战完成」点空白
  → 回活动页 → 点左下角波次里程碑 (150,960) **一次领全部 5 档** →
  「新物品已存入 S.E.E.D. 仓库」点「点击屏幕继续」→ 活动页「返回」。

  本周累积波次上限 50，单场最多 25 → 每周打 2 场；流程自己读「本周累积波次
  N/50」算还差几场，用户手动打过就自动少打，已 50/50 则只补领奖。11-07 实测
  两场后 50/50、已领 60/60 与 25000/25000，左下 10/15/20/23/25 全部变对勾。

坐标口径：全部存窗口比例（1080p 基准 /1920 /1080），点击走
`baijing_daily._click_ratio`（`_window_rect` 换算），换分辨率 / DSR 不受影响；
**不要把探针里的绝对像素搬进来**。

页面判定遵守 docs/conventions/04-game-flows.md「页面判定铁律」：「拟合回归」同时
出现在外勤页与活动页，**不能**当任何一页的单独判据；每页用 ≥2 个专属标记或
标题+内容双证据，登记表见 tests/suite_page_judgement_policy.py，截图矩阵见
tests/suite_baijing_fitting_20261107.py。

坑（实机踩到）：
- 低权限 shell 的合成点击会被 UIPI 静默拦截（点「外勤」两次画面纹丝不动），
  正式 GUI 必须用 00_START_HERE_GUI_ADMIN.bat 提权启动，否则整段看着"成功"
  其实一步没点动。
- 「作战开始」→ 布阵页加载较慢（第二轮连续点击间隔 7s 时第三下点空），
  每步都必须等页面特征出现再点下一步，绝不盲点一串。
- 结算页没有任何按钮，点空白 (0.5,0.5) 才回活动页，中间有加载过场。
- 活动页「返回」是**正常收尾动作**（用户 11-07 21:59 更正：实机最后游戏进程
  消失是用户主动关闭，不是切场景崩溃）。代码在 finally 里点一次返回把游戏退出
  拟合回归页；返回后落在外勤页还是主界面本次没拍到，因此不校验后续页面，
  收尾也不参与本轮成败判定（打完并领完奖即算完成）。
"""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .ocr_click_tester import ocr_window_content

LOGGER = logging.getLogger("GameAutomation")
LOG_TAG = "[白荆回廊·拟合回归]"

# ── 页面判定证据（2026-11-07 实机 OCR 帧）────────────────────────────────
# 每页至少两个专属标记或「标题 + 内容」双证据，禁止单关键词。
MAIN_MARKERS = ("功能菜单", "有猫零售", "同调者", "记忆烙痕", "战术刻印", "精神深潜", "活动中心")
FIELD_MARKERS = ("物资筹备", "漫巡回忆", "队长养成", "思维演绎", "联合训练场", "天隙勘测", "收起")
TEAM_MARKERS = ("自动编队", "载入队列", "队伍", "选择参战同调者", "详细属性")
DEPLOY_MARKERS = ("刻印提供的激励", "深度等级", "刻印评级", "刻印")
BATTLE_MARKERS = ("战术终端", "后备队", "暂停")
MIN_CONF = 0.5

# ── 1080p 基准坐标 → 窗口比例 ────────────────────────────────────────────
P_FIELD_ENTRY = (1735 / 1920, 700 / 1080)
P_FITTING_ENTRY = (1096 / 1920, 919 / 1080)
P_FITTING_START = (1606 / 1920, 964 / 1080)
P_TEAM_START = (1579 / 1920, 1001 / 1080)
P_BATTLE_START = (1746 / 1920, 980 / 1080)
P_RESULT_CONTINUE = (0.5, 0.5)
P_WAVE_REWARD = (150 / 1920, 960 / 1080)
P_REWARD_CONTINUE = (0.5, 1001 / 1080)
P_BACK = (193 / 1920, 50 / 1080)

WAVES_PER_RUN = 25
WEEKLY_WAVE_CAP = 50
MAX_RUNS = 2
BATTLE_TIMEOUT_SEC = 420          # 实机单场约 100s，留 4 倍余量（慢机器/卡顿）
BATTLE_HEARTBEAT_SEC = 20
POLL_INTERVAL_SEC = 1.0

# 每步等页面特征的超时（秒）；布阵页加载最慢，给足
WAIT_FIELD = 8.0
WAIT_ACTIVITY = 12.0
WAIT_TEAM = 15.0
WAIT_DEPLOY = 20.0
WAIT_BATTLE = 25.0
WAIT_BACK_ACTIVITY = 25.0
WAIT_REWARD = 8.0
WAIT_REWARD_CLOSED = 12.0


def _texts(items: Sequence[Dict]) -> List[str]:
    return [
        str(it.get("text") or "")
        for it in (items or [])
        if float(it.get("confidence") or 0) >= MIN_CONF
    ]


def _has(texts: Sequence[str], word: str) -> bool:
    return any(word in t for t in texts)


def _count(texts: Sequence[str], markers: Sequence[str]) -> int:
    return sum(1 for m in markers if _has(texts, m))


def _is_main_page(items) -> bool:
    """主界面：底栏专属标记 ≥3 AND 右侧「外勤」入口。"""
    texts = _texts(items)
    return _count(texts, MAIN_MARKERS) >= 3 and _has(texts, "外勤")


def _is_field_page(items) -> bool:
    """外勤页：「外勤作战中心」专属标题 AND 左列/节点链 ≥2 个标记。"""
    texts = _texts(items)
    return _has(texts, "外勤作战中心") and _count(texts, FIELD_MARKERS) >= 2


def _is_activity_page(items) -> bool:
    """拟合回归活动页：「拟合开始」按钮 AND「本周累积波次」正文（双证据）。"""
    texts = _texts(items)
    return _has(texts, "拟合开始") and _has(texts, "本周累积波次")


def _is_team_page(items) -> bool:
    """编队页：「作战开始」按钮 AND 编队专属标记 ≥2。"""
    texts = _texts(items)
    return _has(texts, "作战开始") and _count(texts, TEAM_MARKERS) >= 2


def _is_deploy_page(items) -> bool:
    """布阵页：「开始战斗」按钮 AND 刻印/深度等级说明 ≥1。"""
    texts = _texts(items)
    return _has(texts, "开始战斗") and _count(texts, DEPLOY_MARKERS) >= 1


def _is_battle_page(items) -> bool:
    """战斗中：「当前波次」 AND 战术终端/后备队/暂停 ≥1。"""
    texts = _texts(items)
    return _has(texts, "当前波次") and _count(texts, BATTLE_MARKERS) >= 1


def _is_result_page(items) -> bool:
    """结算页：「作战完成」标题 AND「达成波次」。"""
    texts = _texts(items)
    return _has(texts, "作战完成") and _has(texts, "达成波次")


def _is_reward_page(items) -> bool:
    """领奖弹窗：「新物品已存入」 AND「点击屏幕继续」（后者单独会在别的过场出现）。"""
    texts = _texts(items)
    return _has(texts, "新物品已存入") and _has(texts, "点击屏幕继续")


PAGE_CHECKS = (
    ("reward", _is_reward_page),
    ("result", _is_result_page),
    ("battle", _is_battle_page),
    ("deploy", _is_deploy_page),
    ("team", _is_team_page),
    ("activity", _is_activity_page),
    ("field", _is_field_page),
    ("main", _is_main_page),
)


def classify_page(items) -> str:
    """互斥分类：越具体的页面越先判，返回 unknown 时绝不盲点。"""
    for name, fn in PAGE_CHECKS:
        if fn(items):
            return name
    return "unknown"


def _ocr(logger: logging.Logger, hwnd) -> List[Dict]:
    try:
        return ocr_window_content(logger, hwnd=hwnd) or []
    except Exception as exc:  # noqa: BLE001 - OCR 失败按未知页处理，下一轮重试
        logger.debug(f"{LOG_TAG} OCR 失败: {exc}")
        return []


def _stopped(stop_check: Optional[Callable[[], bool]]) -> bool:
    try:
        return bool(stop_check and stop_check())
    except Exception:
        return False


def _page_now(logger: logging.Logger, hwnd) -> Tuple[str, List[Dict]]:
    items = _ocr(logger, hwnd)
    return classify_page(items), items


def _wait_page(
    logger: logging.Logger,
    hwnd,
    want: str,
    timeout: float,
    stop_check: Optional[Callable[[], bool]],
    also_ok: Sequence[str] = (),
) -> Tuple[bool, str, List[Dict]]:
    """轮询等到目标页面；stop_check 触发或超时返回 (False, 当前页, 当帧 OCR)。"""
    deadline = time.time() + max(1.0, float(timeout))
    last_page, last_items = "unknown", []
    while time.time() < deadline:
        if _stopped(stop_check):
            return False, "stopped", last_items
        page, items = _page_now(logger, hwnd)
        last_page, last_items = page, items
        if page == want or page in also_ok:
            return True, page, items
        time.sleep(POLL_INTERVAL_SEC)
    logger.warning(f"{LOG_TAG} 等「{want}」超时 {timeout:.0f}s，停在「{last_page}」")
    return False, last_page, last_items


def _click(logger: logging.Logger, hwnd, point: Tuple[float, float], label: str) -> bool:
    """统一走 baijing_daily._click_ratio（窗口比例 → 屏幕坐标，含 fail-safe）。"""
    from .baijing_daily import _click_ratio

    return bool(_click_ratio(logger, hwnd, point, label))


def _record_failure(logger: logging.Logger, hwnd, step: str, detail: str, items) -> None:
    """失败取证：截图 + 当帧 OCR（与白荆日常同一套 flow_diagnostics）。"""
    try:
        from .flow_diagnostics import record_failure

        record_failure(
            logger,
            flow_key="BaiJingFitting",
            step=step,
            error=detail,
            hwnd=hwnd,
            game_key="baijing",
            save_image=bool(hwnd),
            ocr_results=items,
            extra={"kind": "baijing_fitting_weekly"},
        )
    except Exception as exc:  # noqa: BLE001 - 取证失败不能拖垮主流程
        logger.debug(f"{LOG_TAG} 失败取证写入失败（已忽略）: {exc}")


def _fail(logger, hwnd, step, detail, items, runs, claimed):
    logger.error(f"{LOG_TAG} {step}: {detail}")
    _record_failure(logger, hwnd, step, detail, items)
    return {"ok": False, "detail": f"{step}: {detail}", "runs": runs,
            "claimed": claimed, "step": step}


def parse_weekly_wave(items) -> Optional[Tuple[int, int]]:
    """从 OCR 读「本周累积波次 N/50」，读不到返回 None（调用方按 2 场保守补打）。"""
    for text in _texts(items):
        m = re.search(r"本周累积波次\s*(\d+)\s*/\s*(\d+)", text)
        if m:
            return int(m.group(1)), int(m.group(2))
    # OCR 偶尔把标签与数字拆成两条：标签命中后只认「整条就是 N/M」的独立文本，
    # 避免把「已领 30/60」「1/2 下一条」这类同页比分误当成本周波次。
    if any("本周累积波次" in t for t in _texts(items)):
        for text in _texts(items):
            m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", text)
            if m:
                return int(m.group(1)), int(m.group(2))
    return None


def runs_needed(wave: Optional[Tuple[int, int]]) -> int:
    """还差几场（每场 25 波，本周上限 50）；读不到按 2 场，绝不超过 MAX_RUNS。"""
    if wave is None:
        return MAX_RUNS
    cur, total = wave
    cap = total if total > 0 else WEEKLY_WAVE_CAP
    return min(MAX_RUNS, max(0, math.ceil((cap - cur) / WAVES_PER_RUN)))


def _wait_battle_result(
    logger: logging.Logger,
    hwnd,
    stop_check: Optional[Callable[[], bool]],
    timeout: float = BATTLE_TIMEOUT_SEC,
) -> Tuple[bool, str, List[Dict]]:
    """挂机等到结算页；每 20s 打一条波次心跳，全程可中断。"""
    deadline = time.time() + timeout
    last_heartbeat = 0.0
    last_page, last_items = "unknown", []
    while time.time() < deadline:
        if _stopped(stop_check):
            return False, "stopped", last_items
        page, items = _page_now(logger, hwnd)
        last_page, last_items = page, items
        if page == "result":
            logger.info(f"{LOG_TAG} 作战完成（{int(time.time() - (deadline - timeout))}s）")
            return True, page, items
        now = time.time()
        if now - last_heartbeat >= BATTLE_HEARTBEAT_SEC:
            wave_text = next((t for t in _texts(items) if "当前波次" in t), "")
            logger.info(f"{LOG_TAG} 挂机中… 页面={page} {wave_text}")
            last_heartbeat = now
        time.sleep(POLL_INTERVAL_SEC)
    return False, last_page, last_items


def _claim_wave_rewards(
    logger: logging.Logger, hwnd, stop_check: Optional[Callable[[], bool]]
) -> Tuple[bool, str, List[Dict]]:
    """点左下角波次里程碑（一次领全部 5 档），返回 (是否处理完, 状态, 当帧 OCR)。"""
    if not _click(logger, hwnd, P_WAVE_REWARD, "左下角波次奖励"):
        return False, "click_failed", []
    ok, page, items = _wait_page(logger, hwnd, "reward", WAIT_REWARD, stop_check,
                                 also_ok=("activity",))
    if not ok:
        return False, page, items
    if page != "reward":
        # 图标已全是对勾时点击不会弹窗，属于「本周已领」，不是失败
        logger.info(f"{LOG_TAG} 左下角奖励已领过（未弹奖励页）")
        return True, "already", items
    if not _click(logger, hwnd, P_REWARD_CONTINUE, "点击屏幕继续"):
        return False, "reward_continue_click_failed", items
    ok2, page2, items2 = _wait_page(logger, hwnd, "activity", WAIT_REWARD_CLOSED, stop_check)
    if not ok2:
        return False, page2, items2
    logger.info(f"{LOG_TAG} 已领取左下角全部波次奖励")
    return True, "claimed", items2


def _leave_to_hub(logger: logging.Logger, hwnd) -> None:
    """正常收尾：点活动页「返回」，把游戏退出拟合回归页交回白荆一条龙。

    不校验返回后落在外勤页还是主界面（本次实机没拍到），也不把收尾计入本轮
    成败：打完并领完奖之后，后续月度等任务由 `baijing_flow` 自己确认/恢复
    主界面。窗口已失效时 `_click_ratio` 会安全返回，异常只记 debug 日志。
    """
    try:
        _click(logger, hwnd, P_BACK, "活动页返回")
        logger.info(f"{LOG_TAG} 已点「返回」，交回白荆一条龙（后续步骤自行确认主界面）")
    except Exception as exc:  # noqa: BLE001 - 收尾异常不改变本轮结论
        logger.debug(f"{LOG_TAG} 收尾返回异常（已忽略）: {exc}")


def run_fitting_regression(
    logger: logging.Logger,
    hwnd,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Dict:
    """从主界面跑完本周「拟合回归」：按需打 1~2 场 → 领左下角奖励 → 返回。

    返回 {"ok", "detail", "runs", "claimed", "step"}；ok=False 时调用方**不写**
    周标记，下次批次按「本周累积波次」自动补打剩余场次（幂等）。
    """
    started = time.time()
    runs = 0
    claimed = False
    try:
        ok, page, items = _wait_page(logger, hwnd, "main", WAIT_FIELD, stop_check)
        if not ok:
            return _fail(logger, hwnd, "起点确认", f"不在主界面（{page}）", items, runs, claimed)

        if not _click(logger, hwnd, P_FIELD_ENTRY, "外勤"):
            return _fail(logger, hwnd, "打开外勤", "点击失败", [], runs, claimed)
        ok, page, items = _wait_page(logger, hwnd, "field", WAIT_FIELD, stop_check)
        if not ok:
            return _fail(logger, hwnd, "打开外勤", f"未进入外勤页（{page}）", items, runs, claimed)

        if not _click(logger, hwnd, P_FITTING_ENTRY, "拟合回归"):
            return _fail(logger, hwnd, "进入拟合回归", "点击失败", [], runs, claimed)
        ok, page, items = _wait_page(logger, hwnd, "activity", WAIT_ACTIVITY, stop_check)
        if not ok:
            return _fail(logger, hwnd, "进入拟合回归", f"未进入活动页（{page}）", items, runs, claimed)

        wave = parse_weekly_wave(items)
        need = runs_needed(wave)
        logger.info(
            f"{LOG_TAG} 本周累积波次 {f'{wave[0]}/{wave[1]}' if wave else '读不到'}，"
            f"本轮需打 {need} 场"
        )

        for seq in range(need):
            step = f"第{seq + 1}场"
            # 1) 活动页 → 编队页
            if not _click(logger, hwnd, P_FITTING_START, "拟合开始"):
                return _fail(logger, hwnd, step, "拟合开始点击失败", [], runs, claimed)
            ok, page, items = _wait_page(logger, hwnd, "team", WAIT_TEAM, stop_check)
            if not ok:
                return _fail(logger, hwnd, step, f"未进入编队页（{page}）", items, runs, claimed)
            # 2) 编队页 → 布阵页（加载慢，必须等特征，不能连点）
            if not _click(logger, hwnd, P_TEAM_START, "作战开始"):
                return _fail(logger, hwnd, step, "作战开始点击失败", [], runs, claimed)
            ok, page, items = _wait_page(logger, hwnd, "deploy", WAIT_DEPLOY, stop_check)
            if not ok:
                return _fail(logger, hwnd, step, f"未进入布阵页（{page}）", items, runs, claimed)
            # 3) 布阵页 → 开打
            if not _click(logger, hwnd, P_BATTLE_START, "开始战斗"):
                return _fail(logger, hwnd, step, "开始战斗点击失败", [], runs, claimed)
            ok, page, items = _wait_page(logger, hwnd, "battle", WAIT_BATTLE, stop_check,
                                         also_ok=("result",))
            if not ok:
                return _fail(logger, hwnd, step, f"战斗未开始（{page}）", items, runs, claimed)
            # 4) 挂机直到结算（自动战斗默认开启，全程不点技能）
            if page != "result":
                ok, page, items = _wait_battle_result(logger, hwnd, stop_check)
                if not ok:
                    return _fail(logger, hwnd, step, f"挂机未结算（{page}）", items, runs, claimed)
            runs += 1
            logger.info(f"{LOG_TAG} {step} 完成（{runs}/{need}）")
            # 5) 结算页点空白 → 回活动页（中间有加载过场）
            if not _click(logger, hwnd, P_RESULT_CONTINUE, "结算页点空白"):
                return _fail(logger, hwnd, step, "结算页点击失败", [], runs, claimed)
            ok, page, items = _wait_page(logger, hwnd, "activity", WAIT_BACK_ACTIVITY, stop_check)
            if not ok:
                return _fail(logger, hwnd, step, f"结算后未回到活动页（{page}）", items, runs, claimed)

        # 领左下角波次里程碑（一次全领）；本周已打满时也照样补领一次
        done, state, items = _claim_wave_rewards(logger, hwnd, stop_check)
        if not done:
            return _fail(logger, hwnd, "领取左下角奖励", f"未完成（{state}）", items, runs, claimed)
        claimed = state == "claimed"

        detail = (
            f"本周拟合回归完成：打了 {runs} 场，"
            f"左下角奖励{'已领取' if claimed else '本周已领过'}"
            f"（{int(time.time() - started)}s）"
        )
        logger.info(f"{LOG_TAG} {detail}")
        return {"ok": True, "detail": detail, "runs": runs, "claimed": claimed, "step": "done"}
    finally:
        # finally 收尾：无论成功/失败/用户停止，都点一次「返回」把游戏从子页面
        # 退回，不让下周的一条龙停在战斗/结算页。窗口已失效时安全返回。
        try:
            _leave_to_hub(logger, hwnd)
        except Exception as exc:  # noqa: BLE001 - 收尾异常不改变本轮结论
            logger.debug(f"{LOG_TAG} finally 收尾异常（已忽略）: {exc}")
