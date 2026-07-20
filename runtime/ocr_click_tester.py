import os
import re
import time
import ctypes
import logging
import shutil
import sqlite3
from typing import Optional, Tuple, List, Dict

import pyautogui
import win32gui
from PIL import ImageOps, ImageEnhance, ImageDraw, Image
from ..settings.paths import OCR_DEBUG_DIR, TEMPLATES_DIR, TESSDATA_DIR, TESSERACT_EXE, WORKFLOW_DB_PATH as PROJECT_WORKFLOW_DB_PATH

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    from rapidocr_onnxruntime import RapidOCR
    _rapidocr_engine = None
    _tesseract_error_logged = False  # 避免重复记录Tesseract错误

    def _get_rapidocr_engine():
        global _rapidocr_engine
        if _rapidocr_engine is None:
            try:
                _rapidocr_engine = RapidOCR()
            except Exception:
                return None
        return _rapidocr_engine
except ImportError:
    _get_rapidocr_engine = None
    _tesseract_error_logged = False

LOCAL_TESSERACT_EXE = str(TESSERACT_EXE)
PROJECT_OCR_DEBUG_DIR = str(OCR_DEBUG_DIR)
ENTER_OFFLINE_TEMPLATE = str(TEMPLATES_DIR / "进入脱机.png")
LOGIN_TEMPLATE = str(TEMPLATES_DIR / "XM登录.png")
START_TEMPLATE = str(TEMPLATES_DIR / "XM开始.png")
RUNNING_TEMPLATE = str(TEMPLATES_DIR / "XM运行中.png")
WORKFLOW_DB_PATH = str(PROJECT_WORKFLOW_DB_PATH)
XM_STARTUP_WAIT_SEC = 12


def _ensure_tesseract_cmd(logger: logging.Logger) -> bool:
    if pytesseract is None:
        return False
    if os.path.exists(LOCAL_TESSERACT_EXE):
        pytesseract.pytesseract.tesseract_cmd = LOCAL_TESSERACT_EXE
        os.environ["TESSDATA_PREFIX"] = str(TESSDATA_DIR)
        return True
    t_bin = shutil.which("tesseract")
    if t_bin:
        pytesseract.pytesseract.tesseract_cmd = t_bin
        tessdata_dir = os.path.join(os.path.dirname(t_bin), "tessdata")
        os.environ["TESSDATA_PREFIX"] = tessdata_dir
        return True
    logger.error("未检测到 tesseract.exe，请安装 Tesseract OCR 并加入 PATH")
    return False


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _center_from_box(left: int, top: int, width: int, height: int) -> Tuple[int, int]:
    return left + width // 2, top + height // 2


def _find_by_template(
    logger: logging.Logger,
    template_path: str,
    region: Optional[Tuple[int, int, int, int]] = None,
) -> Optional[Tuple[int, int]]:
    if not template_path or not os.path.exists(template_path):
        logger.warning(f"模板文件不存在: {template_path}")
        return None
    template_img = None
    try:
        # 用 PIL 先加载模板，规避 OpenCV 在 Windows 上读取中文路径失败的问题
        template_img = Image.open(template_path)
    except Exception as e:
        logger.warning(f"模板加载失败: {template_path}, error: {str(e)}")
        return None
    # 先高置信匹配，失败再降低阈值；confidence 依赖 opencv，异常时回退无 confidence
    for conf in (0.9, 0.85, 0.8):
        try:
            pt = pyautogui.locateCenterOnScreen(template_img, region=region, confidence=conf)
            if pt:
                logger.info(f"模板匹配成功(conf={conf}): {template_path}")
                return (pt.x, pt.y)
        except Exception:
            try:
                pt = pyautogui.locateCenterOnScreen(template_img, region=region)
                if pt:
                    logger.info(f"模板匹配成功(无confidence): {template_path}")
                    return (pt.x, pt.y)
            except Exception:
                pass
    return None


def _save_ocr_overlay(
    screenshot,
    data: dict,
    debug_dir: str,
    tag: str,
):
    try:
        os.makedirs(debug_dir, exist_ok=True)
        overlay = screenshot.copy()
        draw = ImageDraw.Draw(overlay)
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            left = int(data["left"][i])
            top = int(data["top"][i])
            width = int(data["width"][i])
            height = int(data["height"][i])
            draw.rectangle((left, top, left + width, top + height), outline="red", width=2)
            # 避免中文字体依赖，直接写原始文本（可能有方框，但仍能定位）
            draw.text((left, max(0, top - 12)), text, fill="yellow")
        out = os.path.join(debug_dir, f"ocr_overlay_{tag}_{int(time.time())}.png")
        overlay.save(out)
    except Exception:
        pass


def _find_window_by_keywords(keywords) -> Optional[int]:
    keywords = [k.lower() for k in keywords if k]
    if not keywords:
        return None
    matches = []

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = (win32gui.GetWindowText(hwnd) or "").strip().lower()
        if not title:
            return
        if any(k in title for k in keywords):
            matches.append(hwnd)

    win32gui.EnumWindows(callback, None)
    return matches[0] if matches else None


def _activate_window(hwnd: int, logger: logging.Logger):
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        logger.info("测试窗口已激活")
    except Exception as e:
        logger.warning(f"激活窗口失败: {str(e)}")


def _get_window_region(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = max(0, right - left)
        height = max(0, bottom - top)
        if width == 0 or height == 0:
            return None
        return left, top, width, height
    except Exception:
        return None


def _resolve_step_template(step_text: str) -> Optional[str]:
    # DB key 规范：xm.enter_offline / xm.login / xm.start
    key_mapping = {
        "进入脱机": "xm.enter_offline",
        "登录": "xm.login",
        "开始": "xm.start",
        "运行中": "xm.running",
    }
    key = key_mapping.get(step_text)
    path_from_db = _get_template_path_from_db(key) if key else None
    if path_from_db:
        return path_from_db

    # 兜底：保持兼容原硬编码
    fallback = {
        "进入脱机": ENTER_OFFLINE_TEMPLATE,
        "登录": LOGIN_TEMPLATE,
        "开始": START_TEMPLATE,
        "运行中": RUNNING_TEMPLATE,
    }
    return fallback.get(step_text)


def _get_template_path_from_db(template_key: str) -> Optional[str]:
    if not template_key or not os.path.exists(WORKFLOW_DB_PATH):
        return None
    try:
        conn = sqlite3.connect(WORKFLOW_DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT image_path FROM image_templates WHERE template_key = ? LIMIT 1",
            (template_key,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        image_path = row["image_path"]
        return image_path if image_path and os.path.exists(image_path) else None
    except Exception:
        return None


def _find_text_center(target_text: str, region: Optional[Tuple[int, int, int, int]] = None) -> Optional[Tuple[int, int]]:
    screenshot = pyautogui.screenshot(region=region) if region else pyautogui.screenshot()
    target = _normalize_text(target_text)

    # 优先使用 RapidOCR（中文识别准确率更高）
    if _get_rapidocr_engine:
        try:
            ocr_engine = _get_rapidocr_engine()
            if ocr_engine:
                result, elapse = ocr_engine(screenshot)
                if result:
                    for box, text, confidence in result:
                        text_normalized = _normalize_text(text)
                        if confidence > 0.6 and (target in text_normalized or text_normalized in target):
                            # box 格式: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
                            x1 = min(p[0] for p in box)
                            y1 = min(p[1] for p in box)
                            x2 = max(p[0] for p in box)
                            y2 = max(p[1] for p in box)
                            center_x = (x1 + x2) // 2
                            center_y = (y1 + y2) // 2
                            if region:
                                center_x += region[0]
                                center_y += region[1]
                            return center_x, center_y
        except Exception as e:
            logging.getLogger("GameAutomation").debug(f"RapidOCR识别失败，回退到Tesseract: {str(e)}")

    # 备选：使用 Tesseract OCR
    if pytesseract is None:
        return None

    gray = ImageOps.grayscale(screenshot)
    high_contrast = ImageEnhance.Contrast(gray).enhance(2.2)
    binary = high_contrast.point(lambda x: 255 if x > 150 else 0)
    candidates = [
        screenshot,
        ImageOps.autocontrast(gray),
        high_contrast,
        binary,
        gray.resize((gray.width * 2, gray.height * 2)),
        binary.resize((binary.width * 2, binary.height * 2)),
    ]
    tess_cfgs = ["--oem 3 --psm 6", "--oem 3 --psm 11", "--oem 3 --psm 7", "--oem 3 --psm 13"]

    for img in candidates:
        for cfg in tess_cfgs:
            try:
                data = pytesseract.image_to_data(
                    img,
                    lang="chi_sim+eng",
                    config=cfg,
                    output_type=pytesseract.Output.DICT,
                )
            except getattr(pytesseract, "TesseractNotFoundError", Exception):
                return None
            except Exception:
                continue

            n = len(data.get("text", []))
            entries = []
            for i in range(n):
                text = _normalize_text(data["text"][i])
                if not text:
                    continue
                try:
                    left = int(data["left"][i])
                    top = int(data["top"][i])
                    width = int(data["width"][i])
                    height = int(data["height"][i])
                except Exception:
                    continue
                entries.append(
                    {
                        "text": text,
                        "left": left,
                        "top": top,
                        "width": width,
                        "height": height,
                        "line_num": data.get("line_num", [0] * n)[i] if data.get("line_num") else 0,
                        "block_num": data.get("block_num", [0] * n)[i] if data.get("block_num") else 0,
                    }
                )

            # 1) 直接词级匹配
            for e in entries:
                text = e["text"]
                if target in text or text in target:
                    scale = 2 if img.width == gray.width * 2 else 1
                    x, y = _center_from_box(e["left"], e["top"], e["width"], e["height"])
                    x //= scale
                    y //= scale
                    if region:
                        x += region[0]
                        y += region[1]
                    return x, y

            # 2) 行级拼接匹配（处理中文被拆字）
            grouped = {}
            for e in entries:
                key = (e["block_num"], e["line_num"])
                grouped.setdefault(key, []).append(e)

            for _, arr in grouped.items():
                arr.sort(key=lambda x: x["left"])
                line_text = "".join(x["text"] for x in arr)
                if not line_text:
                    continue
                if target in line_text:
                    start_idx = line_text.find(target)
                    end_idx = start_idx + len(target)
                    cursor = 0
                    selected = []
                    for token in arr:
                        t = token["text"]
                        t_start = cursor
                        t_end = cursor + len(t)
                        cursor = t_end
                        if t_end <= start_idx or t_start >= end_idx:
                            continue
                        selected.append(token)
                    if selected:
                        l = min(x["left"] for x in selected)
                        t = min(x["top"] for x in selected)
                        r = max(x["left"] + x["width"] for x in selected)
                        b = max(x["top"] + x["height"] for x in selected)
                        scale = 2 if img.width == gray.width * 2 else 1
                        x, y = _center_from_box(l, t, r - l, b - t)
                        x //= scale
                        y //= scale
                        if region:
                            x += region[0]
                            y += region[1]
                        return x, y
    return None


def _click_text_with_retry(
    logger: logging.Logger,
    text: str,
    timeout_sec: int = 30,
    region: Optional[Tuple[int, int, int, int]] = None,
    hwnd: Optional[int] = None,
    debug_dir: Optional[str] = None,
) -> bool:
    end_time = time.time() + timeout_sec
    template_path = _resolve_step_template(text)
    while time.time() < end_time:
        # 实时刷新窗口区域，适配窗口拖动/大小变化
        current_region = _get_window_region(hwnd) if hwnd else region
        if current_region:
            region = current_region

        # 三个关键步骤都优先走模板匹配
        if template_path:
            center = _find_by_template(logger, template_path, region=region)
            if not center:
                center = _find_by_template(logger, template_path, region=None)
            if center:
                logger.info(f"通过模板识别到 [{text}]，点击坐标: {center}")
                pyautogui.click(center[0], center[1], button="left")
                time.sleep(1.0)
                return True

        # 优先窗口区域，失败后回退全屏 OCR（更泛化）
        center = _find_text_center(text, region=region)
        if not center:
            center = _find_text_center(text, region=None)
        if center:
            x, y = int(center[0]), int(center[1])
            logger.info(f"识别到文本 [{text}]，点击坐标: ({x}, {y})")
            pyautogui.click(x, y, button="left")
            time.sleep(1.0)
            return True
        time.sleep(0.8)
    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)
        debug_file = os.path.join(debug_dir, f"ocr_fail_{_normalize_text(text)}_{int(time.time())}.png")
        shot = pyautogui.screenshot(region=region) if region else pyautogui.screenshot()
        shot.save(debug_file)
        logger.warning(f"已保存失败截图: {debug_file}")
        # 额外输出 OCR 框选叠加图，便于定位识别偏差
        try:
            # 优先输出 RapidOCR 的调试信息
            if _get_rapidocr_engine:
                ocr_engine = _get_rapidocr_engine()
                if ocr_engine:
                    rapid_result, _ = ocr_engine(shot)
                    if rapid_result:
                        overlay = shot.copy()
                        draw = ImageDraw.Draw(overlay)
                        for box, text, confidence in rapid_result:
                            x1 = min(p[0] for p in box)
                            y1 = min(p[1] for p in box)
                            x2 = max(p[0] for p in box)
                            y2 = max(p[1] for p in box)
                            draw.rectangle((x1, y1, x2, y2), outline="blue", width=2)
                            draw.text((x1, max(0, y1 - 12)), f"{text}({confidence:.2f})", fill="cyan")
                        overlay_path = os.path.join(debug_dir, f"rapidocr_overlay_{_normalize_text(text)}_{int(time.time())}.png")
                        overlay.save(overlay_path)

            # 同时输出 Tesseract 的调试信息作为对比
            data = pytesseract.image_to_data(
                shot,
                lang="chi_sim+eng",
                config="--oem 3 --psm 6",
                output_type=pytesseract.Output.DICT,
            )
            _save_ocr_overlay(shot, data, debug_dir, _normalize_text(text))
        except Exception:
            pass
    logger.error(f"超时未识别到文本: {text}")
    return False


def _wait_running_with_retry_start(
    logger: logging.Logger,
    hwnd: Optional[int],
    timeout_sec: int = 90,
    retry_click_interval: int = 8,
    confirm_hits_required: int = 3,
    confirm_interval_sec: float = 0.8,
) -> bool:
    """
    点击开始后，等待“运行中”模板出现。
    若未出现，按间隔重试点击“开始”，避免登录尚未完成时提前点击导致失败。
    """
    end_time = time.time() + timeout_sec
    running_tpl = _resolve_step_template("运行中")
    start_tpl = _resolve_step_template("开始")
    last_retry_click = 0.0
    stable_running_hits = 0

    while time.time() < end_time:
        current_region = _get_window_region(hwnd) if hwnd else None
        # 先判定是否检测到运行中
        running_center = None
        if running_tpl:
            running_center = _find_by_template(logger, running_tpl, region=current_region) or _find_by_template(
                logger, running_tpl, region=None
            )
        start_center = None
        if start_tpl:
            start_center = _find_by_template(logger, start_tpl, region=current_region) or _find_by_template(
                logger, start_tpl, region=None
            )

        # 二次确认：运行中连续命中，且开始按钮已消失，才判定成功
        if running_center and not start_center:
            stable_running_hits += 1
            logger.info(f"运行中确认中: {stable_running_hits}/{confirm_hits_required}")
            if stable_running_hits >= confirm_hits_required:
                logger.info("检测到[运行中]稳定状态，流程确认成功")
                return True
        else:
            stable_running_hits = 0

        # 未运行中则定时重试点击“开始”
        if start_tpl and (time.time() - last_retry_click >= retry_click_interval):
            if start_center:
                sx, sy = int(start_center[0]), int(start_center[1])
                logger.info(f"未检测到稳定运行中，重试点击[开始]: ({sx}, {sy})")
                pyautogui.click(sx, sy, button="left")
                last_retry_click = time.time()
                time.sleep(1.0)

        time.sleep(confirm_interval_sec)

    logger.error("等待运行中状态超时，可能登录流程未完成或页面状态异常")
    return False


def run_xm_seer_test(logger: logging.Logger, exe_path: str) -> bool:
    if not os.path.exists(exe_path):
        logger.error(f"测试EXE不存在: {exe_path}")
        return False
    if pytesseract is None:
        logger.error("缺少 pytesseract，请先安装: pip install pytesseract")
        return False
    if not _ensure_tesseract_cmd(logger):
        return False

    logger.info(f"以管理员模式启动测试程序: {exe_path}")
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe_path, None, os.path.dirname(exe_path), 1)
    if result <= 32:
        logger.error(f"管理员启动失败，错误码: {result}")
        return False

    logger.info("等待程序界面加载...")
    time.sleep(XM_STARTUP_WAIT_SEC)
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1

    hwnd = _find_window_by_keywords(["xm seer", "脱机", "seer"])
    region = None
    if hwnd:
        _activate_window(hwnd, logger)
        time.sleep(1)
        region = _get_window_region(hwnd)
        if region:
            logger.info(f"将在窗口区域内识别: {region}")

    debug_dir = PROJECT_OCR_DEBUG_DIR
    for text in ["进入脱机", "登录", "开始"]:
        if not _click_text_with_retry(logger, text, timeout_sec=40, region=region, hwnd=hwnd, debug_dir=debug_dir):
            return False
        # 登录后给页面状态切换一点缓冲，减少“开始”过早点击
        if text == "登录":
            time.sleep(3.0)

    if not _wait_running_with_retry_start(logger, hwnd=hwnd, timeout_sec=90, retry_click_interval=8):
        return False
    logger.info("测试识别点击流程完成")
    return True


def ocr_window_content(
    logger: logging.Logger,
    hwnd: Optional[int] = None,
    region: Optional[Tuple[int, int, int, int]] = None,
) -> List[Dict]:
    """
    对窗口或区域进行OCR识别，返回详细的识别结果列表。
    每个结果包含：text（文字）、confidence（置信度）、box（坐标框）、center（中心点）
    """
    global _tesseract_error_logged
    results = []
    current_region = _get_window_region(hwnd) if hwnd else region
    screenshot = pyautogui.screenshot(region=current_region) if current_region else pyautogui.screenshot()

    # 使用 RapidOCR
    if _get_rapidocr_engine:
        try:
            ocr_engine = _get_rapidocr_engine()
            if ocr_engine:
                result, elapse = ocr_engine(screenshot)
                if result:
                    for box, text, confidence in result:
                        x1 = min(p[0] for p in box)
                        y1 = min(p[1] for p in box)
                        x2 = max(p[0] for p in box)
                        y2 = max(p[1] for p in box)
                        center_x = (x1 + x2) // 2
                        center_y = (y1 + y2) // 2
                        if current_region:
                            center_x += current_region[0]
                            center_y += current_region[1]
                        results.append({
                            "text": text,
                            "confidence": confidence,
                            "box": (x1, y1, x2, y2),
                            "center": (center_x, center_y),
                            "source": "rapidocr"
                        })
                    logger.debug(f"[RapidOCR] 识别到 {len(results)} 个文本区域，耗时 {elapse*1000:.1f}ms")
                    if len(results) <= 5:  # 只在数量少时输出详细信息
                        for i, r in enumerate(results):
                            logger.debug(f"  [{i+1}] \"{r['text']}\" ({r['confidence']:.2f}) @ {r['center']}")
                    return results
        except Exception as e:
            logger.debug(f"RapidOCR识别失败: {str(e)}")

    # 备选：使用 Tesseract
    if pytesseract and _ensure_tesseract_cmd(logger):
        try:
            data = pytesseract.image_to_data(
                screenshot,
                lang="chi_sim+eng",
                config="--oem 3 --psm 6",
                output_type=pytesseract.Output.DICT,
            )
            n = len(data.get("text", []))
            for i in range(n):
                text = (data["text"][i] or "").strip()
                if not text:
                    continue
                try:
                    left = int(data["left"][i])
                    top = int(data["top"][i])
                    width = int(data["width"][i])
                    height = int(data["height"][i])
                    conf = float(data.get("conf", [0])[i]) if data.get("conf") else 0
                    center_x = left + width // 2
                    center_y = top + height // 2
                    if current_region:
                        center_x += current_region[0]
                        center_y += current_region[1]
                    results.append({
                        "text": text,
                        "confidence": conf / 100.0,
                        "box": (left, top, left + width, top + height),
                        "center": (center_x, center_y),
                        "source": "tesseract"
                    })
                except Exception:
                    continue
            if results:
                logger.debug(f"[Tesseract] 识别到 {len(results)} 个文本区域")
        except Exception as e:
            if not _tesseract_error_logged:  # 只记录一次错误
                logger.warning(f"Tesseract初始化失败: {str(e)}")
                logger.warning("后续将仅使用 RapidOCR 进行识别")
                _tesseract_error_logged = True

    return results


def ocr_find_text(
    target_text: str,
    logger: logging.Logger,
    hwnd: Optional[int] = None,
    region: Optional[Tuple[int, int, int, int]] = None,
    min_confidence: float = 0.5,
) -> Optional[Dict]:
    """
    在窗口中查找目标文字，返回第一个匹配的结果（包含坐标信息）。
    支持模糊匹配：target_text 在识别结果中 或 识别结果在 target_text 中
    """
    target_normalized = _normalize_text(target_text)
    results = ocr_window_content(logger, hwnd=hwnd, region=region)

    for r in results:
        text_normalized = _normalize_text(r["text"])
        if r["confidence"] >= min_confidence:
            # 正向匹配：OCR文本包含目标文本（如 target="登录" 在 OCR="登录账号" 中）
            if target_normalized in text_normalized:
                logger.info(f"[OCR匹配] ✅ 找到 \"{target_text}\" -> \"{r['text']}\" ({r['confidence']:.2f}) @ {r['center']}")
                return r
            # 反向匹配：目标文本包含OCR文本（如 OCR="脱机" 漏字后仍在 target="进入脱机" 中）
            # 要求OCR文本至少2字符且达到目标长度一半，避免单字误匹配（如"脱"匹配"进入脱机"命中窗口标题栏）
            if (
                text_normalized in target_normalized
                and len(text_normalized) >= max(2, len(target_normalized) // 2)
            ):
                logger.info(f"[OCR匹配] ✅ 找到 \"{target_text}\" -> \"{r['text']}\" ({r['confidence']:.2f}) @ {r['center']}")
                return r

    # 未找到时只输出简要信息（debug级别）
    logger.debug(f"[OCR匹配] 未找到: \"{target_text}\" (扫描了 {len(results)} 个区域)")
    return None


def ocr_and_template_match(
    target_text: str,
    template_path: str,
    logger: logging.Logger,
    hwnd: Optional[int] = None,
    region: Optional[Tuple[int, int, int, int]] = None,
    scales: List[float] = None,
    confidences: List[float] = None,
) -> Tuple[Optional[Tuple[int, int]], str]:
    """
    OCR与模板匹配联合识别策略：
    1. 优先尝试模板匹配（快速、准确）
    2. 模板匹配失败时，使用OCR识别作为备选
    返回：(坐标点, 匹配方式："template" | "ocr" | None)
    """
    if scales is None:
        scales = [1.0, 1.25, 0.8, 1.5, 0.67]
    if confidences is None:
        confidences = [0.92, 0.88, 0.84, 0.8]

    # 策略1：模板匹配
    if template_path and os.path.exists(template_path):
        template_center = _find_by_template(logger, template_path, region=region)
        if not template_center and hwnd:
            window_region = _get_window_region(hwnd)
            template_center = _find_by_template(logger, template_path, region=window_region)
        if template_center:
            logger.info(f"[联合识别] 模板匹配成功: \"{target_text}\" @ {template_center}")
            return template_center, "template"

    # 策略2：OCR识别
    ocr_result = ocr_find_text(target_text, logger, hwnd=hwnd, region=region)
    if ocr_result:
        center = ocr_result["center"]
        logger.info(f"[联合识别] OCR匹配成功: \"{target_text}\" -> \"{ocr_result['text']}\" @ {center} (置信度: {ocr_result['confidence']:.2f})")
        return center, "ocr"

    logger.warning(f"[联合识别] 未找到: \"{target_text}\" (模板匹配+OCR均失败)")
    return None, None


def monitor_window_for_text(
    target_texts: List[str],
    logger: logging.Logger,
    hwnd: int,
    check_interval_sec: float = 10.0,
    timeout_sec: int = 3600,
    on_detected_callback=None,
) -> bool:
    """
    定期监控窗口OCR状态，检测目标文字是否出现。

    参数：
        target_texts: 要检测的目标文字列表（任一出现即触发）
        hwnd: 窗口句柄
        check_interval_sec: 检查间隔（默认10秒）
        timeout_sec: 超时时间（默认1小时）
        on_detected_callback: 检测到时的回调函数(callback(text, result))

    返回：
        bool: 是否检测到目标文字
    """
    end_time = time.time() + timeout_sec
    logger.info(f"[定期监控] 开始监控窗口，目标文字: {target_texts}, 间隔: {check_interval_sec}秒, 超时: {timeout_sec}秒")

    while time.time() < end_time:
        # 执行OCR识别
        results = ocr_window_content(logger, hwnd=hwnd)
        if results:
            # 检查是否匹配任意目标文字
            for target in target_texts:
                target_normalized = _normalize_text(target)
                for r in results:
                    text_normalized = _normalize_text(r["text"])
                    if target_normalized in text_normalized or text_normalized in target_normalized:
                        logger.info(f"[定期监控] ✅ 检测到目标文字: \"{target}\" (实际: \"{r['text']}\", 置信度: {r['confidence']:.2f})")
                        if on_detected_callback:
                            on_detected_callback(target, r)
                        return True

            # 输出当前屏幕文字快照（每分钟输出一次详细日志）
            current_time = time.time()
            if int(current_time) % 60 < check_interval_sec:  # 大约每分钟输出一次
                texts_found = [r["text"] for r in results[:15]]
                logger.debug(f"[定期监控] 屏幕文字快照: {texts_found}")

        time.sleep(check_interval_sec)

    logger.warning(f"[定期监控] ⏰ 监控超时，未检测到目标文字: {target_texts}")
    return False


def run_xm_enhanced_flow(
    logger: logging.Logger,
    exe_path: str,
    enable_ocr_fallback: bool = True,
    enable_monitoring: bool = True,
) -> bool:
    """
    增强版XM流程执行器，支持OCR+模板匹配联合识别和定期监控。

    流程步骤：
    1. 启动程序
    2. 识别并点击"进入脱机"
    3. 识别并点击"登录"（登录后变为"退出"）
    4. 识别并点击"开始"（开始后变为"停止"/"运行中"）
    5. 定期监控检测"执行完毕"

    参数：
        enable_ocr_fallback: 是否启用OCR备选识别（默认True）
        enable_monitoring: 是否启用定期监控（默认True）
    """
    if not os.path.exists(exe_path):
        logger.error(f"EXE不存在: {exe_path}")
        return False

    logger.info("=" * 60)
    logger.info("[增强版XM流程] 开始执行")
    logger.info(f"程序路径: {exe_path}")
    logger.info(f"OCR备选: {'启用' if enable_ocr_fallback else '禁用'}")
    logger.info(f"定期监控: {'启用' if enable_monitoring else '禁用'}")
    logger.info("=" * 60)

    # 步骤配置：(目标文字, 模板路径, 描述)
    steps_config = [
        ("进入脱机", ENTER_OFFLINE_TEMPLATE, "点击进入脱机按钮"),
        ("登录", LOGIN_TEMPLATE, "点击登录按钮"),
        ("开始", START_TEMPLATE, "点击开始按钮"),
    ]

    # 启动程序
    logger.info(f"[启动] 以管理员模式启动: {exe_path}")
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe_path, None, os.path.dirname(exe_path), 1)
    if result <= 32:
        logger.error(f"[启动] 管理员启动失败，错误码: {result}")
        return False

    logger.info("[启动] 等待程序界面加载...")
    time.sleep(XM_STARTUP_WAIT_SEC)

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1

    # 查找窗口
    hwnd = _find_window_by_keywords(["xm seer", "脱机", "seer"])
    if not hwnd:
        logger.error("[窗口] 未找到XM Seer窗口")
        return False

    _activate_window(hwnd, logger)
    time.sleep(1)
    region = _get_window_region(hwnd)
    if region:
        logger.info(f"[窗口] 窗口区域: {region}")

    # 执行各步骤
    for idx, (target_text, template_path, description) in enumerate(steps_config):
        step_num = idx + 1
        logger.info("-" * 50)
        logger.info(f"[步骤{step_num}] {description}: \"{target_text}\"")

        if enable_ocr_fallback:
            # 使用OCR+模板匹配联合识别
            center, match_method = ocr_and_template_match(
                target_text=target_text,
                template_path=template_path,
                logger=logger,
                hwnd=hwnd,
                region=region,
            )

            if center:
                x, y = int(center[0]), int(center[1])
                logger.info(f"[步骤{step_num}] 点击 ({x}, {y}) [匹配方式: {match_method}]")
                pyautogui.click(x, y, button="left")
                time.sleep(1.5 if target_text == "登录" else 1.0)  # 登录后多等一会
            else:
                logger.error(f"[步骤{step_num}] ❌ 未识别到 \"{target_text}\"")
                return False
        else:
            # 仅使用原始的点击重试逻辑
            if not _click_text_with_retry(logger, target_text, timeout_sec=40, region=region, hwnd=hwnd):
                logger.error(f"[步骤{step_num}] ❌ 点击失败: \"{target_text}\"")
                return False
            if target_text == "登录":
                time.sleep(3.0)

    # 等待运行中状态确认
    logger.info("-" * 50)
    logger.info("[确认] 等待运行中状态...")
    if not _wait_running_with_retry_start(logger, hwnd=hwnd, timeout_sec=90, retry_click_interval=8):
        logger.error("[确认] ❌ 未检测到运行中状态")
        return False

    logger.info("[确认] ✅ 已进入运行中状态")

    # 定期监控执行完毕状态
    if enable_monitoring:
        logger.info("-" * 50)
        logger.info("[监控] 开始定期监控执行完毕状态...")

        def on_finish_detected(text, result):
            logger.info(f"[监控] ✅✅✅ 检测到流程完成信号: \"{text}\"")

        finish_detected = monitor_window_for_text(
            target_texts=["选择任务已执行完毕", "执行完毕", "完毕"],
            logger=logger,
            hwnd=hwnd,
            check_interval_sec=10.0,
            timeout_sec=3600,
            on_detected_callback=on_finish_detected,
        )

        if finish_detected:
            logger.info("[监控] 流程执行完毕，准备关闭进程")
            try:
                import psutil
                for proc in psutil.process_iter(["pid", "name"]):
                    if proc.info["name"] and "xm seer" in proc.info["name"].lower():
                        proc.terminate()
                        logger.info(f"[关闭] 已终止进程 PID={proc.info['pid']}")
                        break
            except Exception as e:
                logger.warning(f"[关闭] 终止进程失败: {str(e)}")

    logger.info("=" * 60)
    logger.info("[增强版XM流程] ✅ 流程执行完成")
    logger.info("=" * 60)
    return True

