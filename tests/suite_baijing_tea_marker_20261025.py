"""白荆茶憩角色选择页特征回归（2026-10-25）。

背景：17:51 那轮点「前往茶憩」→「确定」后，加载到 100% 但 45s 内没命中
`TEA_SELECT_MARKERS`（今日可邀份额/位阶/默契值）→ 茶憩失败。
19:08 复跑时角色页稳定命中页底帮助文字
「同调者位阶达到S2后，可以开始提升技能等级。」(conf 0.93)。
修复：把「同调者位阶」加入特征（长词，比 2 字「位阶」抗 OCR 抖动），
并保留超时分支的截图 + OCR 落库。

跑法：.venv\\Scripts\\python.exe artifacts\\test_baijing_tea_marker_20261025.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

import game_auto.runtime.baijing_daily as bd  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def main():
    print("[1] 角色选择页特征包含稳定长词")
    markers = bd.TEA_SELECT_MARKERS
    check("含「同调者位阶」", "同调者位阶" in markers, markers)
    check("仍保留「今日可邀份额」", "今日可邀份额" in markers, markers)
    check("仍保留「默契值」", "默契值" in markers, markers)

    print("[2] 19:08 实跑捕获的真实 OCR 文本能被特征命中")
    observed = "同调者位阶达到S2后，可以开始提升技能等级。"
    hit = [m for m in markers if m in observed]
    check("长词命中真实文本", "同调者位阶" in hit, hit)

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
