# -*- coding: utf-8 -*-
"""onnxocr v6 单档基准（2026-11-07 起 RapidOCR/v3/v4/v5 全部下线，只剩 v6）。

回归资产：data/artifacts/ref_xm_window_20261107.png（XM 10px 日志面板，
窗口 508,240,901,610；面板紧裁剪 1149,547,1378,620）。
指标：命中「加载完」、目标行字符相似度（去时间戳+标点归一）、预热后 3 轮最优延迟。

跑法：.venv\\Scripts\\python.exe tools\\bench_ocr_engines.py
结果同时写 data/artifacts/ocr_bench_20261107.json。
"""

import difflib
import io
import json
import logging
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

WINDOW = (508, 240, 901, 610)
PANEL_BOX = (1149, 547, 1378, 620)
REF = ROOT / "data" / "artifacts" / "ref_xm_window_20261107.png"
TARGET = "加载完"
TRUTH = "游戏数据加载完毕！"
_PUNCT = str.maketrans({"：": ":", "（": "(", "）": ")", "！": "!", ",": ",", "，": ",", " ": ""})


def _norm(text: str) -> str:
    s = text.translate(_PUNCT)
    if s.startswith("[") and "]" in s:
        s = s.split("]", 1)[1].lstrip(":")
    return s


def _score(rows: list) -> tuple:
    hit_rows = [t for t in rows if TARGET in t]
    if not hit_rows:
        return False, 0.0, ""
    best = max(hit_rows, key=lambda t: difflib.SequenceMatcher(None, _norm(t), _norm(TRUTH)).ratio())
    return True, difflib.SequenceMatcher(None, _norm(best), _norm(TRUTH)).ratio(), best


def _engine_ep(engine) -> str:
    if hasattr(engine, "providers"):
        try:
            p = engine.providers()
            return f"det={p.get('det')} rec={p.get('rec')}"
        except Exception as exc:
            return f"<{exc}>"
    try:
        return str(engine.text_recognizer.session.session.get_providers())
    except Exception as exc:
        return f"<{exc}>"


def build_engines(logger):
    from game_auto.runtime import ocr_click_tester as oct

    # 2026-11-07 起只保留 onnxocr v6 单档（v3/v4/v5 与 RapidOCR 已删），
    # 本工具改为 v6 性能回归基准。
    eng = oct._get_onnxocr_engine(logger)
    return ([("v6 onnxocr", eng)] if eng is not None else [])


def bench(engine, img, rounds: int = 3):
    engine(img)
    best, out = 1e9, None
    for _ in range(rounds):
        t0 = time.perf_counter()
        out = engine(img)
        best = min(best, time.perf_counter() - t0)
    raw = out[0] if isinstance(out, tuple) else out
    rows = [str(item[1]) for item in (raw or []) if len(item) >= 2]
    return best * 1000, rows


def main() -> int:
    logger = logging.getLogger("bench_ocr")
    logger.setLevel(logging.ERROR)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    if not REF.is_file():
        print(f"SKIP 缺回归资产: {REF}")
        return 1
    full = np.asarray(Image.open(str(REF)).convert("RGB"))
    l, t, w, h = WINDOW
    crops = {
        "window 901x610": full[t:t + h, l:l + w],
        "panel 229x73": full[PANEL_BOX[1]:PANEL_BOX[3], PANEL_BOX[0]:PANEL_BOX[2]],
    }

    engines = build_engines(logger)
    print(f"引擎 {len(engines)} 个：", [name for name, _ in engines])
    for name, eng in engines:
        print(f"  EP {name}: {_engine_ep(eng)}")

    records = []
    print()
    header = f"{'engine':<14}{'crop':<16}{'latency':>9}{'regions':>9}{'hit':>5}{'char_sim':>9}  best_row"
    print(header)
    print("-" * len(header))
    for crop_name, img in crops.items():
        for name, eng in engines:
            ms, rows = bench(eng, img)
            hit, sim, best = _score(rows)
            print(f"{name:<14}{crop_name:<16}{ms:>7.0f}ms{len(rows):>9}{'Y' if hit else 'N':>5}{sim:>9.2f}  {best}")
            records.append({
                "engine": name, "crop": crop_name, "latency_ms": round(ms, 1),
                "regions": len(rows), "hit": hit, "char_sim": round(sim, 3), "best_row": best,
            })

    out = ROOT / "data" / "artifacts" / "ocr_bench_20261107.json"
    out.write_text(json.dumps({
        "asset": str(REF), "window": WINDOW, "panel_box": PANEL_BOX,
        "metric": {"target": TARGET, "truth": TRUTH, "rounds": 3},
        "results": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
