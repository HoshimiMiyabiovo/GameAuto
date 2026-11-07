"""2026-11-07 README 与仓库状态不再脱节的回归。

事故：README 写「Tesseract OCR 5.x（项目已内置在 tools/tesseract/）」，但
.gitignore 排除了 tools/tesseract/(251MB)、tools/flet/(216MB)、
tesseract-installer.exe，注释写 "bundled separately"；git log --all --
tools/tesseract 为空，二进制从未入库。文档按「作者本机目录」写，不是按
「clone 后会发生什么」写，导致新人第一步不知道该信哪句、也没有一条不提权
就能验证环境的命令。data/artifacts/ 参考图同样 0 个入库，文档里的
「见 ref_xxx.png」全是死链。

本套件锁住：文档说实话、安装脚本给出提示、自检步骤存在、以及
「缺 Tesseract 不阻断启动」这条兜底不被误改成硬失败。

2026-11-07 二轮复核（用户问「我们的文字 OCR 不是 Rapid 吗」）：主引擎确实是
RapidOCR，但代码里三处把它写反了 ——
  1. `foreground_flow_runner.HAS_OCR = bool(pytesseract)` 卡住整个文字定位函数，
     可下游 `ocr_find_any_text` → `_ocr_scan` 走的是 RapidOCR，等于没装这个
     Python 包就把主 OCR 一起关掉；
  2. `dependency_manager` 缺 exe 打 WARNING「OCR识别点击功能可能不可用」，吓人且是错的；
  3. `_ensure_tesseract_cmd` 把可选依赖打成 ERROR，且每次兜底都会刷屏。
另有一处概念混淆：`pytesseract` **Python 包必装**（在 requirements / 依赖检查里），
可选的只是 `tesseract.exe` + `chi_sim` 中文包。本套件 [5][6] 锁住这些口径。

跑法：.venv\\Scripts\\python.exe tests\\suite_readme_bootstrap_20261107.py
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def text(rel):
    p = PROJECT_ROOT / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_readme_honest():
    print("[1] README：不再宣称内置二进制，说清 clone 后缺什么")
    r = text("README.md")
    check("README 存在", bool(r))
    check("不再写「项目已内置在 tools/tesseract/」",
          "项目已内置在 `tools/tesseract/`" not in r)
    check("目录树不再把 tools 说成内置二进制",
          "内置工具（Tesseract OCR" not in r)
    check("写明主 OCR 是 onnxocr v6、Tesseract 只在它失败时兜底",
          "主 OCR：**onnxocr PP-OCRv6**" in r and "仅当 onnxocr v6 返回空或异常时才尝试" in r)
    check("区分「Python 包必装」与「exe 可选」",
          "自动安装（必装）" in r and "中文包**可选**" in r)
    check("点明唯一硬依赖 exe 的入口",
          "XM Seer 测试" in r and "唯一硬依赖" in r)
    check("有「克隆后仓库里没有什么」对照表",
          "克隆后仓库里没有什么" in r and "tools/tesseract/" in r
          and "tools/flet/" in r and "app_config.json" in r)
    check("说明参考图不入库 / 坐标按比例",
          "仓库未收录" in r and "窗口比例" in r)
    check("澄清 1919x1079 是定标来源而非运行时要求",
          "定标来源标注" in r and "不是运行时分辨率要求" in r)
    check("首次运行从 example 自动复制",
          "首次运行自动从 `.example.json` 复制" in r)


def test_first_step():
    print("[2] 快速开始：有一条不提权、不碰游戏的第一步")
    r = text("README.md")
    check("第 3 步是自检 pytest",
          re.search(r"# 3\. 先自检.*\n.*pytest tests -q", r) is not None)
    check("自检步骤标明不提权/不启动游戏/不写个人配置",
          "不提权、不启动游戏、不写个人配置" in r)
    check("启动挪到第 4 步", "# 4. 启动" in r)
    check("说明所有入口需管理员 + 会弹 UAC",
          "*_ADMIN.bat" in r and "弹 UAC" in r and "UIPI" in r)
    check("自检用的解释器路径真实存在",
          (PROJECT_ROOT / ".venv" / "Scripts" / "python.exe").exists())


def test_setup_deps():
    print("[3] setup_deps.bat：装完提示可选 Tesseract")
    b = text("setup_deps.bat")
    for i in range(1, 6):
        check(f"步骤编号 {i}/5 存在", f"[{i}/5]" in b)
    check("不再残留 /4] 旧编号", "/4]" not in b)
    check("检查 tools/tesseract/tesseract.exe", "tools\\tesseract\\tesseract.exe" in b)
    check("检查 PATH 中的 tesseract", "where tesseract" in b)
    check("明确主 OCR 不受影响", "onnxocr v6" in b and "不受影响" in b)
    check("安装结束提示先跑自检", "先跑自检" in b and "pytest tests -q" in b)
    check("括号配对", b.count("(") == b.count(")"), (b.count("("), b.count(")")))


def test_repo_contract():
    print("[4] 仓库契约：排除项与随仓库资产")
    g = text(".gitignore")
    for item in ("tools/tesseract/", "tools/flet/", "tools/tesseract-installer.exe", "/data/"):
        check(f".gitignore 仍排除 {item}", item in g)
    for item in ("flow_configs.json", "app_config.json"):
        check(f".gitignore 仍排除本地 {item}", item in g)
    check("assets/templates 有入库模板",
          len(list((PROJECT_ROOT / "assets" / "templates").glob("*.png"))) > 50)
    check("requirements.lock 入库", (PROJECT_ROOT / "requirements.lock").exists())
    check("flow_configs.example.json 入库", (PROJECT_ROOT / "flow_configs.example.json").exists())
    check("app_config.example.json 入库", (PROJECT_ROOT / "app_config.example.json").exists())


def test_missing_tesseract_is_soft():
    print("[5] 缺 Tesseract 不阻断启动，且 onnxocr v6 是主引擎")
    ocr = text("src/game_auto/runtime/ocr_click_tester.py")
    dm = text("src/game_auto/services/dependency_manager.py")
    rr = text("src/game_auto/runtime/foreground_flow_runner.py")
    check("_ensure_tesseract_cmd 有 PATH 兜底",
          'shutil.which("tesseract")' in ocr)
    check("OCR 兜底入口先判可用性", "if not _ensure_tesseract_cmd(logger):" in ocr)
    check("主 OCR 是 onnxocr v6", "onnxocr v6" in ocr or "onnxocr_v6" in ocr)
    seg = ocr[ocr.find("def _ocr_scan"):ocr.find("def ocr_window_content")]
    check("_ocr_scan 里 onnxocr v6 成功即返回，Tesseract 只在其后兜底",
          seg.find('"source": "onnxocr_v6"') < seg.find("备选：使用 Tesseract")
          and "return _check_log_contamination(logger, results)" in seg)
    check("缺 exe 记 info 而非误导性 warning",
          'report.add_info("未检测到 tesseract.exe：可选 OCR 兜底不可用' in dm
          and "OCR识别点击功能可能不可用" not in dm)
    check("缺中文包不再打「准确率会很低」告警", "中文识别准确率会很低" not in dm)
    check("可选依赖不打 ERROR",
          "未检测到 tesseract.exe，请安装 Tesseract OCR 并加入 PATH" not in ocr)
    check("唯一硬依赖 exe 的入口单独报错",
          "[XM Seer 测试] 该独立测试工具需要 tesseract.exe" in ocr)
    check("文字定位不再按 pytesseract 包是否存在做门控",
          "if not HAS_OCR:" not in rr and "    import pytesseract" not in rr)
    check("门控移除后函数体仍有 try/except 兜底",
          'logger.debug(f"OCR定位失败' in rr)


def test_engine_source_of_truth():
    print("[6] 引擎口径：Python 包必装 / exe 可选 / 下游走 onnxocr v6")
    req = text("requirements.txt")
    r = text("README.md")
    check("pytesseract 在 requirements 里（必装）",
          re.search(r"^pytesseract", req, re.M) is not None)
    check("onnxocr 主引擎依赖在 requirements 里",
          "onnxruntime-gpu" in req and "rapidocr-onnxruntime" not in req)
    check("README 明确 exe 可选、仓库不含二进制", "仓库不含二进制" in r)
    ocr = text("src/game_auto/runtime/ocr_click_tester.py")
    check("ocr_find_any_text 直接调 _ocr_scan 并透传 engine（onnxocr v6 主链路）",
          "results, screenshot, _scan_region = _ocr_scan(" in ocr
          and "logger, hwnd=hwnd, region=region, engine=engine" in ocr)
    check("setup_deps 说明 Python 包已随依赖装好",
          "Python 包 pytesseract 已随依赖装好" in text("setup_deps.bat"))


def main():
    test_readme_honest()
    test_first_step()
    test_setup_deps()
    test_repo_contract()
    test_missing_tesseract_is_soft()
    test_engine_source_of_truth()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
