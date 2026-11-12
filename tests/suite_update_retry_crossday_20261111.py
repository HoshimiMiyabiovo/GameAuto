"""更新补跑「跨日条目」回归（2026-11-11）。

事故：公告开服在次日时，队列条目的 date 存的是创建当天，而所有判断都拿
date == today 过滤 → 创建当天被当成"还有补跑没执行"，把日报邮件/自动关机
无限推迟（11-10、11-11 连续两天）；到真正该执行的那天又永不触发。

修复：统一按 due_at 的日期判断「该在哪天执行」，date 字段迁移成应执行日。
跑法：.venv\\Scripts\\python.exe tests\\suite_update_retry_crossday_20261111.py
"""

import json
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_auto.ui.bridge_views import scheduler as sched  # noqa: E402

FAILURES = []
# 日期动态生成（2026-11-12 补，同 11-09 suite_baijing_fitting 周键动态化先例）：
# 创建日/应执行日随**真实当天**推导——[1] 里 `_has_unfired_scheduled_today()` 走真时钟
# 不注入 now，日期写死 11-11 的版本到 11-12 就必挂（due_at==真今天 恰好是"今天有
# 未触发补跑"，is False 断言翻车）。动态推导后语义不变：创建日=今天、应执行日=明天。
from datetime import datetime as _dt, timedelta as _td  # noqa: E402

DAY1 = _dt.now().astimezone().strftime("%Y-%m-%d")                              # 创建日
DAY2 = (_dt.now().astimezone() + _td(days=1)).strftime("%Y-%m-%d")              # 应执行日
DAY3 = (_dt.now().astimezone() + _td(days=2)).strftime("%Y-%m-%d")              # 过期清理日
OLD_ENTRY = {
    "app_key": "WutheringWaves",
    "script_choice": None,
    "date": DAY1,
    "attempt": 0,
    "max_attempts": 3,
    "created_at": f"{DAY1}T09:20:51+08:00",
    "due_at": f"{DAY2}T11:05:00+08:00",
    "open_at": f"{DAY2}T11:00:00+08:00",
    "version": "3.8",
    "title": "《鸣潮》3.8版本更新维护预告",
}


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _probe(tmp: Path, entries):
    # 队列文件是模块级常量，每个用例指向自己的临时目录，互不串数据
    sched.UPDATE_RETRY_FILE = tmp / "update_retries.json"
    sched.UPDATE_RETRY_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    p = object.__new__(sched.SchedulerView)
    p._update_retry_lock = threading.Lock()
    p._is_running = False
    p._auto_run_paused = False
    p.started = []
    p.startAutomation = lambda *a, **k: p.started.append((a, k))
    p.loadPostRunPrefs = lambda: "{}"
    p._unfired_scheduled_today = lambda: []
    return p


def _read(tmp: Path):
    raw = (tmp / "update_retries.json").read_text(encoding="utf-8")
    return json.loads(raw)


def _at(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+08:00")


def test_pending_semantics():
    print("[1] 跨日条目：创建日不挡收尾，应执行日才算待执行")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        p = _probe(tmp, [dict(OLD_ENTRY)])
        check("11-11 19:55 无 pending", p._pending_update_retries(now=_at(DAY1, "19:55")) == [])
        check(f"{DAY1}（创建日）不阻塞邮件/关机", p._has_unfired_scheduled_today() is False)
        pending = p._pending_update_retries(now=_at(DAY2, "11:06"))
        check(f"{DAY2} 能查到该补跑", len(pending) == 1 and pending[0]["app_key"] == "WutheringWaves")
        check(f"{DAY2} 09:00（未到点）也算待执行",
              len(p._pending_update_retries(now=_at(DAY2, "09:00"))) == 1)
        check("旧格式 date 字段仍兼容",
              p._retry_scheduled_date({"date": DAY2}) == DAY2)
        check("attempt 用尽不再 pending",
              p._pending_update_retries(now=_at(DAY2, "11:06")) != [] and
              _probe(tmp, [{**OLD_ENTRY, "attempt": 3}])._pending_update_retries(
                  now=_at(DAY2, "11:06")) == [])


def test_trigger_and_cleanup():
    print("[2] 到点触发 / 创建日不误触发 / 过期残留清理")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        p = _probe(tmp, [dict(OLD_ENTRY)])
        p._check_update_retries(now=_at(DAY1, "19:55"))
        check("创建日不会提前触发", p.started == [], p.started)
        check("创建日不清掉跨日条目", len(_read(tmp)) == 1)

        p2 = _probe(tmp, [dict(OLD_ENTRY)])
        p2._check_update_retries(now=_at(DAY2, "11:06"))
        check("应执行日到点会触发", len(p2.started) == 1, p2.started)
        args = p2.started[0][0]
        check("触发的是 WutheringWaves", "WutheringWaves" in str(args[0]), args)
        items = _read(tmp)
        check("触发后 attempt+1", items[0]["attempt"] == 1, items[0])

        p3 = _probe(tmp, [dict(OLD_ENTRY)])
        p3._check_update_retries(now=_at(DAY3, "10:00"))
        check("隔天过期条目被清理", _read(tmp) == [], _read(tmp))
        check("过期条目不触发", p3.started == [], p3.started)


def test_schedule_writes_due_day():
    print("[3] 排队写入 date=应执行日、复用旧条目不重复")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        p = _probe(tmp, [dict(OLD_ENTRY)])
        info = {"open_at_dt": _at(DAY2, "11:00"), "version": "3.8", "title": "维护"}
        p._schedule_update_retry("WutheringWaves", None, info, source="test", now=_at(DAY1, "12:00"))
        items = _read(tmp)
        check("复用了旧条目、没新增", len(items) == 1, items)
        check("date 迁移成应执行日", items[0]["date"] == DAY2, items[0]["date"])
        check("due_at 仍是开服 +5min", items[0]["due_at"].endswith("11:05:00+08:00"), items[0]["due_at"])
        p._schedule_update_retry("WutheringWaves", None, info, source="test2", now=_at(DAY1, "13:00"))
        check("同日再排队仍只有一条", len(_read(tmp)) == 1, _read(tmp))

        p2 = _probe(tmp, [])
        p2._schedule_update_retry("GenshinImpact", None, info, source="test", now=_at(DAY1, "12:00"))
        fresh = _read(tmp)
        check("新条目 date=应执行日", len(fresh) == 1 and fresh[0]["date"] == DAY2, fresh)
        check("新条目 attempt=0", fresh[0]["attempt"] == 0, fresh[0])


def test_remove_and_result():
    print("[4] 补跑成功能删掉跨日条目（含旧格式）")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        p = _probe(tmp, [dict(OLD_ENTRY)])

        class Result:
            app_key = "WutheringWaves"
            state = "success"
            message = ""
            success = True
            script_choice = None

        p._handle_update_retry_result(Result(), now=_at(DAY2, "11:20"))
        check("成功结果清空队列", _read(tmp) == [], _read(tmp))
        check(f"{DAY2} 之后不再阻塞", p._pending_update_retries(now=_at(DAY2, "12:00")) == [])

        p2 = _probe(tmp, [dict(OLD_ENTRY), {**OLD_ENTRY, "app_key": "Other"}])
        p2._remove_update_retry("WutheringWaves", "测试")
        rest = _read(tmp)
        check("只删目标 app", len(rest) == 1 and rest[0]["app_key"] == "Other", rest)


def test_static():
    print("[5] 静态：所有日期判断都走应执行日")
    src = (PROJECT_ROOT / "src/game_auto/ui/bridge_views/scheduler.py").read_text(encoding="utf-8")
    check("helper 已定义", "def _retry_scheduled_date" in src)
    check("不再有 date==today 的裸判断", 'str(e.get("date") or "") == today' not in src)
    check("不再有 date>=today 的裸过滤", 'str(e.get("date") or "") >= today' not in src)
    check("_check_update_retries 可注入 now", "def _check_update_retries(self, now:" in src)


def main():
    original = sched.UPDATE_RETRY_FILE
    try:
        test_pending_semantics()
        test_trigger_and_cleanup()
        test_schedule_writes_due_day()
        test_remove_and_result()
    finally:
        sched.UPDATE_RETRY_FILE = original
    test_static()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
