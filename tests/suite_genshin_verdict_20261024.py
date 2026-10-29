"""原神判定口径改为 BetterGI 日志回归测试（2026-10-24）。

用户拍板：原神最终判定以 BetterGI 日志（委托奖励 + 探索派遣两项标记）为准，
米游社便笺不再作为判定依据；超时但日志已确认全部完成 → 判成功且不重试。

断言：
  1. 监控行解析：真实 BetterGI 日志行能置起 委托/派遣 完成标记
  2. all_completed = commission AND expedition
  3. _apply_bettergi_verdict 六种组合（停止/监控不可用/未完成/超时+已全完成/
     成功+已全完成/原样通过）
  4. 旧行为保持：监控不可用时成功也改判 fail-closed
  5. 调用点接线：GenshinImpact 分支走 _apply_bettergi_verdict

跑法：.venv\\Scripts\\python.exe artifacts\\test_genshin_verdict_20261024.py
"""

import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, r"E:\GithubProject")

from game_auto.runtime import bettergi_log_monitor as bm  # noqa: E402
from game_auto.runtime import task_batch_runner as tbr  # noqa: E402

FAILURES = []
LOGGER = logging.getLogger("gi_verdict_test")


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} {detail}", flush=True)
        FAILURES.append(name)


def _monitor_with_lines(lines):
    m = bm.BetterGILogMonitor(LOGGER, log_dir=r"NUL:\nonexistent", file_pattern="x*.log")
    for line in lines:
        m._process_line(line.strip())
    # 桩数据没有真实日志文件，手工标记"今日日志已找到"，
    # 否则 failure_message 会先被"日志缺失"分支吃掉（2026-10-26 踩过）
    m.log_found = True
    m.source_files = ["better-genshin-impact20261026.log"]
    return m.build_summary()


def test_line_parsing():
    print("[1] 日志标记解析（真实行样本）")
    s = _monitor_with_lines([
        '▶ "领取『每日委托』奖励" 未完成或者已领取',
        '▶ "领取『每日委托』奖励！"',
        '▶ "探索派遣" 未探索完成或已领取',
        '"探索派遣" 完成',
    ])
    check("委托奖励完成标记", s.commission_completed is True)
    check("探索派遣完成标记", s.expedition_completed is True)
    check("all_completed", s.all_completed is True)

    s2 = _monitor_with_lines([
        '▶ "领取『每日委托』奖励！"',
        '▶ "探索派遣" 未探索完成或已领取',
    ])
    check("仅委托完成时 all_completed=False", s2.all_completed is False)
    check("缺失项报告探索派遣", "探索派遣" in s2.failure_message(), s2.failure_message())


def test_verdict():
    print("[2] 判定组合")
    ok_summary = _monitor_with_lines(['▶ "领取『每日委托』奖励！"', '"探索派遣" 完成'])
    partial = _monitor_with_lines(['▶ "领取『每日委托』奖励！"'])

    # a) 用户停止 → 保持原判，不覆盖
    s, m = tbr._apply_bettergi_verdict(False, "超时", ok_summary, True)
    check("停止时不覆盖", (s, m) == (False, "超时"), (s, m))

    # b) 监控不可用 + 原本成功 → fail-closed（旧行为保持）
    s, m = tbr._apply_bettergi_verdict(True, "", None, False)
    check("监控不可用 fail-closed", (s, m) == (False, "BetterGI 日志监控不可用"), (s, m))

    # c) 监控不可用 + 原本失败 → 原判
    s, m = tbr._apply_bettergi_verdict(False, "超时", None, False)
    check("监控不可用+失败原判", (s, m) == (False, "BetterGI 日志监控不可用"), (s, m))

    # d) 未全部完成 → 失败（只降不升）
    s, m = tbr._apply_bettergi_verdict(True, "", partial, False)
    check("部分完成改判失败", s is False and "探索派遣" in m, (s, m))

    # e) 超时 + 日志确认全部完成 → 判成功（本次需求核心）
    s, m = tbr._apply_bettergi_verdict(False, "超时", ok_summary, False)
    check("超时+已全完成 → 成功", s is True, (s, m))
    check("成功消息可读", "委托奖励" in m and "探索派遣" in m, m)

    # f) 成功 + 已全完成 → 原样通过
    s, m = tbr._apply_bettergi_verdict(True, "任务完成", ok_summary, False)
    check("成功+已全完成原样", (s, m) == (True, "任务完成"), (s, m))


def test_monitor_survives_stop():
    print("[3] stop() 后 summary 仍可读（超时路径先 stop 再 build_summary）")
    m = bm.BetterGILogMonitor(LOGGER, log_dir=r"NUL:\nonexistent", file_pattern="x*.log")
    m._process_line('▶ "领取『每日委托』奖励！"')
    m._process_line('"探索派遣" 完成')
    m.stop(timeout=0.1)
    s = m.build_summary()
    check("stop 后标记保留", s.all_completed is True, s)


def test_wiring():
    print("[4] 调用点接线")
    import inspect
    src = inspect.getsource(tbr._run_single_task)
    check("GenshinImpact 分支调用 _apply_bettergi_verdict",
          "_apply_bettergi_verdict" in src and "bettergi_monitor.build_summary()" in src)
    check("旧的内联降级逻辑已移除", "not bettergi_summary.all_completed" not in src)


def test_failure_reason():
    """2026-10-26 真实场景：用户没做每日委托，BetterGI 直接跳过领取。

    失败文案必须说清"委托没做完"，不能只写"未确认"（会被当成监控误报）。
    """
    print("[5] 失败原因可读（2026-10-26 真实日志行）")
    s = _monitor_with_lines([
        '▶ "领取『每日委托』奖励" 未完成或者已领取',
        '▶ "探索派遣" 未探索完成或已领取',
        '对话选项："探索派遣"',
        '探索派遣："完成"',
    ])
    msg = s.failure_message()
    check("判失败", s.all_completed is False, msg)
    check("只缺委托奖励", s.missing_items() == ["委托奖励"], s.missing_items())
    check("文案点明委托未完成被跳过", "BetterGI 判定「未完成」并跳过领取" in msg, msg)
    check("文案说明一条龙不做委托", "没有做每日委托的任务" in msg, msg)

    ok = _monitor_with_lines(['▶ "领取『每日委托』奖励！"', '"探索派遣" 完成'])
    check("全完成时 failure_message=成功", ok.failure_message() == "成功", ok.failure_message())

    no_record = _monitor_with_lines(['随便一行无关日志'])
    check("无标记时只说没有完成记录", "没有完成记录" in no_record.failure_message(), no_record.failure_message())

    lines = bm.format_bettergi_daily_reward_summary(s)
    check("日报里带委托未完成原因", any("需要先做完 4 个委托" in l for l in lines), lines)


def test_bettergi_own_errors():
    """2026-10-26 用户指出"今天是 BetterGI 自己出问题"：报错要进判定文案。"""
    print("[6] BetterGI 自身报错计数与文案")
    s = _monitor_with_lines([
        '[09:09:59.776] [ERR] [Primary:S1:P3480] BetterGenshinImpact.Core.Script.Dependence.Log',
        '未识别到地脉花文本，尝试重新导航',
        '[09:20:00.238] [ERR] [Primary:S1:P3480] BetterGenshinImpact.GameTask.Common.TaskControl',
        '执行地图追踪时候发生错误: "未能返回主界面"',
        '[09:21:20.786] [ERR] [Primary:S1:P3480] BetterGenshinImpact.Service.ScriptService',
        '执行脚本时发生异常: "Error: 传送失败"',
        '▶ "领取『每日委托』奖励" 未完成或者已领取',
    ])
    check("ERR 计数=3", s.error_count == 3, s.error_count)
    check("样本取自 ERR 的下一行正文", "地脉花" in " ".join(s.error_samples), s.error_samples)
    check("样本最多 3 条", len(s.error_samples) <= 3, s.error_samples)
    msg = s.failure_message()
    check("失败文案带报错数", "BetterGI 自身报错 3 条" in msg, msg)
    check("失败文案带示例", "传送失败" in msg or "未能返回主界面" in msg, msg)
    check("一条龙任务事实写进文案", "没有做每日委托的任务" in msg, msg)
    lines = bm.format_bettergi_daily_reward_summary(s)
    check("日报里有报错行", any("BetterGI 自身报错 3 条" in l for l in lines), lines)

    # ERR 头后面紧跟另一行日志头（没有正文行）时不能把日志头当样本
    s2 = _monitor_with_lines([
        '[09:20:00.238] [ERR] [Primary:S1] SomeClass',
        '[09:20:01.000] [INF] [Primary:S1] OtherClass',
    ])
    check("无正文的 ERR 不污染样本", s2.error_samples == [] and s2.error_count == 1, s2.error_samples)


def test_real_log_today():
    """直接读今天的真实 BetterGI 日志：确认报错计数与委托跳过都被抓到。"""
    print("[7] 真实日志回放（better-genshin-impact20261026.log）")
    path = Path(r"D:/Program Files/genshin ai/BetterGI/BetterGI/log/better-genshin-impact20261026.log")
    if not path.exists():
        print("  SKIP 找不到今天的 BetterGI 日志")
        return
    m = bm.BetterGILogMonitor(LOGGER, log_dir=str(path.parent), file_pattern=path.name)
    m._parse_file_from_offset(path, 0)
    s = m.build_summary()
    check("读到日志", s.log_found is True or bool(m.error_count), s)
    check("抓到 BetterGI 报错", s.error_count > 0, s.error_count)
    check("抓到委托跳过标记", s.commission_incomplete_seen is True, s.commission_incomplete_seen)
    check("委托未被误判为完成", s.commission_completed is False, s.commission_completed)
    print(f"  · 报错 {s.error_count} 条，样本：{s.error_samples[:3]}")


def main():
    test_line_parsing()
    test_verdict()
    test_monitor_survives_stop()
    test_wiring()
    test_failure_reason()
    test_bettergi_own_errors()
    test_real_log_today()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
