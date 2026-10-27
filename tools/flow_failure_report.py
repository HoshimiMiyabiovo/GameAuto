"""失败记录分析报告（配套 runtime/flow_diagnostics.py）。

失败发生时 runtime 会把结构化记录写进 workflow.sqlite3 的 flow_failures 表，
截图落在 artifacts/flow_failures/。这个脚本把两者汇总成一份可直接看的报告：
哪条流程、哪一步最常失败、最近都发生了什么、当帧屏幕上有什么、证据截图在哪。

用法：
    .venv\\Scripts\\python.exe tools\\flow_failure_report.py            # 最近 7 天汇总
    .venv\\Scripts\\python.exe tools\\flow_failure_report.py --days 30 --top 30
    .venv\\Scripts\\python.exe tools\\flow_failure_report.py --json     # 机器可读
    .venv\\Scripts\\python.exe tools\\flow_failure_report.py --flow BaiJing --verbose
    .venv\\Scripts\\python.exe tools\\flow_failure_report.py --purge 90   # 顺手清 90 天前记录
"""

import argparse
import io
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# runtime/ 内部用相对导入（from ..settings.paths import ...），必须以包的形式导入，
# 因此 sys.path 要指到仓库的父目录（game_auto 本身没有 __init__.py，走命名空间包）
sys.path.insert(0, os.path.dirname(BASE_DIR))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description="自动化失败记录分析")
    ap.add_argument("--days", type=int, default=7, help="统计最近多少天（<=0 表示不限）")
    ap.add_argument("--top", type=int, default=20, help="聚合分组数上限")
    ap.add_argument("--limit", type=int, default=100, help="明细条数上限")
    ap.add_argument("--flow", default="", help="只看某个流程 flow_key")
    ap.add_argument("--game", default="", help="只看某个游戏 game_key")
    ap.add_argument("--verbose", action="store_true", help="明细里带当帧 OCR 文字")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--purge", type=int, default=0, help="清理 N 天前的失败记录（0=不清理）")
    ap.add_argument("--output", default="", help="报告写入文件（默认打印到控制台）")
    args = ap.parse_args()

    from game_auto.runtime.flow_diagnostics import dumps, format_failure_report, purge_old_records
    from game_auto.settings.paths import WORKFLOW_DB_PATH
    from game_auto.storage.workflow_db import WorkflowDB

    if args.purge:
        purge_old_records(None, keep_days=args.purge)

    db = WorkflowDB(str(WORKFLOW_DB_PATH))
    summary = db.summarize_flow_failures(days=args.days, group_limit=args.top)
    recent = db.query_flow_failures(
        days=args.days, limit=args.limit, flow_key=args.flow, game_key=args.game
    )

    if args.json:
        text = dumps({"days": args.days, "summary": summary, "recent": recent})
    else:
        text = format_failure_report(summary, recent if args.verbose else recent[:15], days=args.days)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"报告已写入: {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())