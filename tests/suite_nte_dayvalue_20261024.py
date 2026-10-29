"""异环 dayvalue 接入 + 暖暖假进度删除回归测试（2026-10-24）。

用户拍板：
  - 塔吉多 roleHome 的 dayvalue（每日活跃度，上限 100）接入判定：
    dayvalue >= 100 → 当日完成（复核兜底）；体力/城市体力进卡片展示；
  - 社区任务（点赞/浏览/分享）停止查询与落库（getUserTasks 仍供签到内部使用）；
  - 无限暖暖 Pearpal 的 5 项假进度任务（每日心愿/周常奖励/能量体力/派遣/登录天数）
    停止查询与落库，真实进度只来自 whimbox 日志。

断言：
  1. fetch_role_home 解析 roleHome 响应
  2. check_tajiduo_daily：产出 活跃度/体力/城市体力/游戏签到，不再有社区任务
  3. 活跃度满/不满对 daily_completed 的影响；体力不参与完成判定
  4. completion_state：无 日常领取 时活跃度计数兜底；有 日常领取 时状态优先
  5. 事后复核：dayvalue 满 → 不改判（即使 ok-nte 边角失败）；不满 → 保持改判
  6. 暖暖：Pearpal 函数已删除，whimbox 落库仍在

跑法：.venv\\Scripts\\python.exe artifacts\\test_nte_dayvalue_20261024.py
"""

import inspect
import logging
import sys
from unittest.mock import patch

sys.path.insert(0, r"E:\GithubProject")

from game_auto.clients import daily_checker as dc  # noqa: E402
from game_auto.clients import tajiduo_sign as tj  # noqa: E402
from game_auto.runtime import task_batch_runner as tbr  # noqa: E402
from game_auto.services import daily_progress_tracker as dpt  # noqa: E402
from game_auto.services import ok_nte_daily_parser as op  # noqa: E402

FAILURES = []
LOGGER = logging.getLogger("nte_test")

ROLE_HOME = {
    "dayvalue": 100, "staminaValue": 360, "staminaMaxValue": 360,
    "citystaminaValue": 0, "citystaminaMaxValue": 700,
    "roleloginDays": 179, "lev": 56,
}


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} {detail}", flush=True)
        FAILURES.append(name)


def test_fetch_role_home():
    print("[1] fetch_role_home 解析")
    client = tj.TajiduoClient(token="t", deviceid="d", uid="1")
    with patch.object(client, "_h5_request",
                      return_value=(200, {"ok": 1, "code": 0, "data": ROLE_HOME})):
        home = client.fetch_role_home(215051597069)
    check("返回 data 字典", home.get("dayvalue") == 100 and home.get("staminaValue") == 360, home)
    with patch.object(client, "_h5_request",
                      return_value=(200, {"ok": 0, "code": -1, "message": "err"})):
        check("失败返回空 dict", client.fetch_role_home(1) == {})


class _FakeClient:
    role_id = "215051597069"

    def __init__(self, dayvalue=100):
        self.dayvalue = dayvalue

    def fetch_game_cards(self):
        return [{"bindRoleInfo": {"roleId": int(self.role_id), "roleName": "飘飖"}}]

    def fetch_role_home(self, role_id):
        home = dict(ROLE_HOME)
        home["dayvalue"] = self.dayvalue
        return home

    def game_signin_states(self):
        return [{"game": "异环", "signed": True, "role_name": "飘飖"}]

    def get_coin_state(self):
        return {"todayGet": 80}


def _run_check(dayvalue=100):
    with patch.object(tj, "TajiduoClient", lambda **kw: _FakeClient(dayvalue)):
        return dc.check_tajiduo_daily(token="t", deviceid="d", uid="1")


def test_check_tajiduo_daily():
    print("[2] check_tajiduo_daily 新数据结构")
    st = _run_check(100)
    names = {t.name: t for t in st.tasks}
    check("活跃度 100/100 已完成",
          names.get("活跃度") and names["活跃度"].current == 100 and names["活跃度"].is_completed,
          st.tasks)
    check("体力 360/360 进卡片", names.get("体力") and names["体力"].total == 360, st.tasks)
    check("城市体力 0/700 进卡片", names.get("城市体力") and names["城市体力"].current == 0, st.tasks)
    check("游戏签到在列", names.get("异环签到") and names["异环签到"].is_completed, st.tasks)
    check("社区任务已删除",
          not ({"点赞帖子", "浏览帖子", "分享", "社区签到"} & set(names)), list(names))
    check("dayvalue 满 → 每日完成", st.is_daily_completed is True)

    st60 = _run_check(60)
    n60 = {t.name: t for t in st60.tasks}
    check("dayvalue 60 → 活跃度未完成", n60["活跃度"].is_completed is False)
    check("dayvalue 60 → 每日未完成（体力不计入判定）", st60.is_daily_completed is False)


def test_completion_state():
    print("[3] completion_state 口径")
    def mk(renwu=None, activity=None):
        tasks = []
        if renwu:
            tasks.append(DailyTaskRecord := dpt.DailyTaskRecord(
                task_key="日常领取", task_name="日常领取", source="script", status=renwu))
        if activity:
            cur, total = activity
            tasks.append(dpt.DailyTaskRecord(
                task_key="活跃度", task_name="活跃度", source="community",
                status="pending", extra={"current": cur, "total": total}))
        return dpt.GameDailyRecord(game_key="nte", game_name="异环", tasks=tasks)

    check("无日常领取+活跃度100 → completed", mk(None, (100, 100)).completion_state() == "completed")
    check("无日常领取+活跃度60 → pending", mk(None, (60, 100)).completion_state() == "pending")
    check("日常领取成功 → completed（状态优先）", mk("success", (60, 100)).completion_state() == "completed")
    check("日常领取失败+活跃度100 → failed（状态优先，复核侧由 dayvalue 守护）",
          mk("failed", (100, 100)).completion_state() == "failed")


def test_recheck():
    print("[4] 事后复核 dayvalue 兜底")
    def nte_data(renwu_status="failed"):
        return {"game_key": "nte", "game_name": "异环",
                "is_all_success": renwu_status != "failed", "has_failed": renwu_status == "failed",
                "progress_percent": 50,
                "tasks": [{"task_key": "日常领取", "task_name": "日常领取",
                           "status": renwu_status, "message": "执行失败"}],
                "errors": ["日常领取: 执行失败"], "report_time": ""}

    merged_full = [{"task_key": "活跃度", "task_name": "活跃度", "status": "pending",
                    "message": "100/100", "extra": {"current": 100, "total": 100}}]
    merged_60 = [{"task_key": "活跃度", "task_name": "活跃度", "status": "pending",
                  "message": "60/100", "extra": {"current": 60, "total": 100}}]

    with patch.object(op, "parse_latest_ok_nte_log", lambda: nte_data("failed")), \
         patch.object(tbr, "_load_api_key_progress_tasks", lambda g, l: merged_full):
        r = tbr._verify_script_log_result("NevernessToEverness", None, "异环", LOGGER)
    check("dayvalue 满 → 不改判（边角失败不烧重试）", r is None, r)

    with patch.object(op, "parse_latest_ok_nte_log", lambda: nte_data("failed")), \
         patch.object(tbr, "_load_api_key_progress_tasks", lambda g, l: merged_60):
        r = tbr._verify_script_log_result("NevernessToEverness", None, "异环", LOGGER)
    check("dayvalue 不满 → 保持改判", r is not None and "日常领取" in r, r)

    with patch.object(op, "parse_latest_ok_nte_log", lambda: nte_data("failed")), \
         patch.object(tbr, "_load_api_key_progress_tasks", lambda g, l: []):
        r = tbr._verify_script_log_result("NevernessToEverness", None, "异环", LOGGER)
    check("无 dayvalue 数据 → 旧口径改判", r is not None, r)

    with patch.object(op, "parse_latest_ok_nte_log", lambda: None):
        r = tbr._verify_script_log_result("NevernessToEverness", None, "异环", LOGGER)
    check("当天无日志 → None", r is None, r)


def test_nikki_removal():
    print("[5] 暖暖 Pearpal 假进度删除")
    src = inspect.getsource(dc)
    check("check_nikki_daily 已删除", "def check_nikki_daily" not in src)
    check("NIKKI_EXPECTED_TASKS 已删除", "NIKKI_EXPECTED_TASKS" not in src)
    check("_record_nikki_to_tracker 已删除", "_record_nikki_to_tracker" not in src)
    check("pearpal_notebook 落库已删除", 'source="pearpal_notebook"' not in src)
    check("Pearpal notebook 导入已删除", "fetch_pearpal_notebook_payload" not in src)
    check("whimbox 落库仍在", "_record_whimbox_script_tasks" in src)
    check("check_games_progress 走 whimbox 落库",
          "whimbox 脚本任务落库跳过" in inspect.getsource(dc.check_games_progress))


def main():
    test_fetch_role_home()
    test_check_tajiduo_daily()
    test_completion_state()
    test_recheck()
    test_nikki_removal()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
