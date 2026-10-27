import os
import re
import time
import ctypes
import ctypes.wintypes
import logging
import shutil
import sqlite3
from typing import Optional, Tuple, List, Dict
import threading

import pyautogui
import win32gui
from PIL import ImageOps, ImageEnhance, ImageDraw, Image
import numpy as np
from ..settings.paths import OCR_DEBUG_DIR, TEMPLATES_DIR, TESSDATA_DIR, TESSERACT_EXE, WORKFLOW_DB_PATH as PROJECT_WORKFLOW_DB_PATH

# CUDA EP 会把整套 cublas/cuDNN（约 2.2G DLL 映射）载入 GUI 进程，
# 实测占了 3.4G 常驻里的绝大部分。默认关闭，只在 GAMEAUTO_OCR_CUDA=1 时启用。
_USE_CUDA_OCR = os.environ.get("GAMEAUTO_OCR_CUDA", "0") == "1"

# ── 让 onnxruntime-gpu 能加载 CUDA/cuDNN 运行时（来自 nvidia-* pip wheels）──
# CUDA 默认关闭时直接跳过：不污染 PATH/DLL 目录，避免意外载入 2G+ 的 toolkit。
def _ensure_nvidia_cuda_dlls():
    if not _USE_CUDA_OCR:
        return
    """nvidia-cuda-runtime/cublas/cudnn 等 pip 包把 DLL 放在
    site-packages/nvidia/<pkg>/{bin,lib}。onnxruntime-gpu 需要这些 DLL 在进程
    DLL 搜索路径里。

    两层注册都要做，且必须在 import onnxruntime 之前调用：
    1) os.add_dll_directory：对 Python 侧 ctypes / 扩展模块加载有效；
    2) 修改 os.environ['PATH']：onnxruntime 的 CUDAExecutionProvider 通过自身
       LoadLibrary 加载 cublasLt64_*.dll / cudnn64_*.dll，Windows 加载器只按
       PATH（与程序目录、system32）搜索，不会自动查 add_dll_directory 列表。
       实测仅 add_dll_directory 时 CUDA 会静默回退 CPU，必须同时把 nvidia 的
       bin/lib 目录前置进 PATH 才能真正让 CUDA EP 加载成功。
    """
    try:
        sp = os.path.dirname(os.path.dirname(np.__file__))  # .../site-packages
        base = os.path.join(sp, "nvidia")
    except Exception:
        return
    if not os.path.isdir(base):
        return
    extra = []
    for root, _dirs, _files in os.walk(base):
        for sub in ("bin", "lib"):
            p = os.path.join(root, sub)
            if os.path.isdir(p) and any(
                f.lower().endswith(".dll") for f in os.listdir(p)
            ):
                try:
                    os.add_dll_directory(p)
                except Exception:
                    pass
                extra.append(p)
    if extra:
        existing = os.environ.get("PATH", "")
        seen, ordered = set(), []
        for p in extra + existing.split(";"):   # nvidia 目录前置，优先被搜到
            if p and p not in seen:
                seen.add(p)
                ordered.append(p)
        os.environ["PATH"] = ";".join(ordered)
_ensure_nvidia_cuda_dlls()


# ── 截图封装：WGC(Windows Graphics Capture, GPU 直采) 优先，pyautogui(GDI) 回退 ──
_wgc_capture = None
_wgc_thread = None
_wgc_latest = None       # 最新一帧 BGRA numpy（来自 WGC）
_wgc_control = None
_wgc_running = False
_wgc_failed = False      # 首次失败则禁用，避免反复尝试拖慢循环


def _wgc_on_frame(frame, control):
    global _wgc_latest, _wgc_control
    try:
        _wgc_latest = frame.frame_buffer
        _wgc_control = control
    except Exception:
        pass


def _wgc_thread_main():
    global _wgc_running
    try:
        import windows_capture as wc
        cap = wc.WindowsCapture(cursor_capture=False, draw_border=False, monitor_index=1)

        @cap.event
        def on_frame_arrived(frame, control):
            _wgc_on_frame(frame, control)

        @cap.event
        def on_closed():
            pass

        cap.start()   # 阻塞直到 control.stop()
    except Exception as e:
        logging.getLogger("GameAutomation").warning(f"[WGC] 捕获线程异常，回退 pyautogui: {e}")
    finally:
        _wgc_running = False


def _wgc_ensure_started() -> bool:
    """启动持久 WGC 捕获线程；无显示器/不可用返回 False（调用方走 pyautogui 回退）。"""
    global _wgc_thread, _wgc_running, _wgc_failed
    if _wgc_failed:
        return False
    if _wgc_running and _wgc_latest is not None:
        return True
    try:
        import windows_capture  # noqa: F401  未安装则直接回退
    except ImportError:
        _wgc_failed = True
        return False
    t = threading.Thread(target=_wgc_thread_main, daemon=True)
    t.start()
    for _ in range(50):            # 等待首帧（最多 ~2.5s）
        if _wgc_latest is not None:
            break
        time.sleep(0.05)
    if _wgc_latest is None:
        _wgc_failed = True
        _wgc_running = False
        return False
    _wgc_running = True
    _wgc_thread = t
    return True


def grab_screen(region=None):
    """统一截图：优先 WGC（GPU 直采，高帧率、不卡游戏）；失败/无显示器回退 pyautogui(GDI)。

    region: (left, top, width, height) 或 None 全屏（与 pyautogui 约定一致）。
    返回 PIL.Image(RGB)，与 pyautogui.screenshot() 完全兼容。
    """
    if _wgc_ensure_started() and _wgc_latest is not None:
        try:
            import cv2
            buf = _wgc_latest
            if buf.ndim == 3 and buf.shape[2] == 4:
                rgb = cv2.cvtColor(buf[:, :, :3], cv2.COLOR_BGR2RGB)
            elif buf.ndim == 3 and buf.shape[2] == 3:
                rgb = cv2.cvtColor(buf, cv2.COLOR_BGR2RGB)
            else:
                rgb = buf
            img = Image.fromarray(rgb)
            if region is not None:
                left, top, width, height = region
                img = img.crop((left, top, left + width, top + height))
            return img
        except Exception as e:
            logging.getLogger("GameAutomation").warning(f"[WGC] 帧转换失败，回退 pyautogui: {e}")
    if region is not None:
        return pyautogui.screenshot(region=region)
    return pyautogui.screenshot()


try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    from rapidocr_onnxruntime import RapidOCR
    import onnxruntime as _ort
    _rapidocr_engine = None
    _tesseract_error_logged = False  # 避免重复记录Tesseract错误

    def _patch_rapidocr_gpu_providers():
        """运行时给 RapidOCR 的 ONNX session 注入 EP 列表。

        默认 DirectML → CPU（不碰 CUDA，省 2G+ 内存映射）。
        仅 GAMEAUTO_OCR_CUDA=1 时才把 CUDA 放首位。
        RapidOCR 原生的 ``OrtInferSession`` 只认 CUDA/CPU，根本不注册
        DirectML（DmlExecutionProvider）。本函数 patch 其 EP 列表，使装有
        onnxruntime-directml 的环境也能走 GPU。仅在首次构造引擎时 patch 一次。
        """
        from rapidocr_onnxruntime.utils import (
            OrtInferSession, SessionOptions, GraphOptimizationLevel,
            get_available_providers, InferenceSession,
        )
        if getattr(OrtInferSession, "_gpu_provider_patched", False):
            return

        def _gpu_init(self, config):
            sess_opt = SessionOptions()
            sess_opt.log_severity_level = 4
            sess_opt.enable_cpu_mem_arena = False
            sess_opt.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL
            cpu_ep = "CPUExecutionProvider"
            cpu_opts = {"arena_extend_strategy": "kSameAsRequested"}
            cuda_ep = "CUDAExecutionProvider"
            cuda_opts = {
                "device_id": 0,
                "arena_extend_strategy": "kSameAsRequested",
                "cudnn_conv_algo_search": "HEURISTIC",
                "do_copy_in_default_stream": True,
            }
            dml_ep = "DmlExecutionProvider"
            avail = get_available_providers()
            # 默认跳过 CUDA（省内存）；显式开启才放首位
            ep_list = []
            if _USE_CUDA_OCR and cuda_ep in avail:
                ep_list.append((cuda_ep, cuda_opts))
            if dml_ep in avail:
                ep_list.append((dml_ep, {}))
            ep_list.append((cpu_ep, cpu_opts))
            self._verify_model(config["model_path"])
            self.session = InferenceSession(
                config["model_path"], sess_options=sess_opt, providers=ep_list)

        OrtInferSession.__init__ = _gpu_init
        OrtInferSession._gpu_provider_patched = True

    def _get_rapidocr_engine(logger: logging.Logger = None) -> object:
        global _rapidocr_engine
        if _rapidocr_engine is None:
            try:
                _patch_rapidocr_gpu_providers()
                eng = RapidOCR()
                _rapidocr_engine = eng
                lg = logger or logging.getLogger("GameAutomation")
                avail = _ort.get_available_providers()
                lg.info(f"[OCR] RapidOCR 已加载，可用 EP={avail}（CUDA={'开' if _USE_CUDA_OCR else '关(GAMEAUTO_OCR_CUDA=1 开启)'}，按 DirectML→CPU 选择）")
                # 打印实际生效 EP（取识别模型 session）
                try:
                    lg.info(f"[OCR] 生效 EP={eng.text_recognizer.session.session.get_providers()}")
                except Exception:
                    pass
            except Exception as e:
                logging.getLogger("GameAutomation").warning(f"RapidOCR 初始化失败: {e}")
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


# ── OCR 防护：日志特征文本识别 ────────────────────────────────────────────────
# 本项目的日志窗口 / 控制台窗口经常和游戏窗口同时存在于屏幕上。OCR 一旦把
# 日志文本（如 "[OCR匹配] 未找到: ['今日已签到']"、"测试识别点击流程完成"）当成
# 游戏界面文字，就会出现"自己干扰自己"的假匹配。
# 2026-10-17 起升级为「探测 + 告警 + 硬拦截」：命中特征文本的条目一律剔除，
# 禁止参与匹配（详见 _check_log_contamination 的教训记录）。
_LOG_SIGNATURE_PATTERNS = (
    "gameautomation:",
    "[rapidocr]",
    "[tesseract]",
    "[ocr匹配]",
    "[联合识别]",
    "[wgc]",
    "[bridge]",
    "traceback (most recent call last)",
    "auto_runner.log",
    "# 操作日志",
    "重试当前步骤",
    "点击签到入口",
    "未找到:",
    "未找到：",
    "扫描了",
    "个区域",
    "置信度:",
    "置信度：",
    "步骤失败",
    "已取消当前任务",
    "用户请求停止任务",
    "================================",
    "--------------------------------",
)
_LOG_ALERT_INTERVAL_SEC = 30.0
_last_log_alert_ts = 0.0


def _is_log_signature_text(text: str) -> bool:
    """单条 OCR 文本是否命中日志特征模式。"""
    lowered = str(text or "").lower()
    return any(pat in lowered for pat in _LOG_SIGNATURE_PATTERNS)


def _detect_log_signature(results: List[Dict]) -> List[str]:
    """在 OCR 结果里找"日志特征文本"。"""
    hits = []
    for r in results or []:
        text = str(r.get("text") or "")
        if _is_log_signature_text(text):
            hits.append(text)
    return hits


def _check_log_contamination(logger: logging.Logger, results: List[Dict]) -> List[Dict]:
    """统一入口：识别结果中若混入日志特征文本，节流告警 + 硬拦截剔除，返回干净结果。

    2026-10-17 实例（XM 流程整体失败）：窗口不可用回退全屏 → 记事本里开着的
    auto_runner.log 被拍进截图 → OCR 把日志文字当成「进入脱机」假命中（0.87）
    → 点到记事本上 → 确认失败 → 步骤重试到耗尽。当时只有告警、没有拦截。
    从此命中特征文本的条目一律剔除，禁止参与匹配。
    阈值策略：>=2 条才节流告警（防噪音）；任何 1 条命中即剔除（防漏网）。
    目标文字都是界面标签（进入脱机/登录/开始），不含日志特征模式，剔除不会误杀目标。
    """
    if not results:
        return results
    hits = _detect_log_signature(results)
    if not hits:
        return results
    if len(hits) >= 2:
        _alert_log_contamination(logger, hits)
    kept = [r for r in results if not _is_log_signature_text(str(r.get("text") or ""))]
    dropped = len(results) - len(kept)
    logger.info(
        f"[OCR防护] 🚫 硬拦截日志污染文本 {dropped} 条，不参与匹配（保留 {len(kept)} 条）"
    )
    return kept


def _alert_log_contamination(logger: logging.Logger, hits: List[str]) -> None:
    """节流告警：日志窗口正在被 OCR 读到，会污染识别结果。"""
    global _last_log_alert_ts
    now = time.time()
    if now - _last_log_alert_ts < _LOG_ALERT_INTERVAL_SEC:
        return
    _last_log_alert_ts = now
    preview = " | ".join(h[:40] for h in hits[:5])
    logger.warning(
        f"[OCR防护] ⚠️ 识别区域疑似包含日志/控制台窗口（命中 {len(hits)} 条特征文本）: {preview}"
    )
    logger.warning(
        "[OCR防护] 该文本会被当成界面文字参与匹配，可能导致假命中。"
        "建议：1) 关闭/最小化 auto_runner.log 记事本与控制台窗口；"
        "2) 为步骤配置 local_region 限制识别区域；3) 用 search_scope=window 只识别目标窗口。"
    )


# ── OCR 防护：目标窗口被遮挡检测 ──────────────────────────────────────────────
# WGC/pyautogui 抓的是「屏幕合成结果」，被别的窗口盖住的区域照样会被抓进来。
# 用 WindowFromPoint 在窗口内做网格采样：采样点归属的窗口既不是自己、也不是
# 自己的子窗口，就算被遮挡。轻量、不引入新依赖。
_OBSCURE_CHECK_INTERVAL_SEC = 10.0
_last_obscure_check_ts = 0.0
# 遮挡比例达到这个值就视为"目标窗口基本不可见"：抓屏等于在识别遮挡窗口的内容，
# 识别结果必假（2026-10-26 实例：MiniMax Code 被聊天窗口完全盖住，OCR 把聊天里
# 贴的截图当成应用界面，定位到假的「签到得」按钮）。
_OCCLUSION_REFUSE_RATIO = 0.9


def _try_raise_target(logger: logging.Logger, hwnd: int) -> bool:
    """尝试把被遮挡的目标窗口提到前台；返回 True 表示遮挡已解除。

    置顶失败不算错——上层会按"未匹配"重试，重试路径里会再次 focus。
    """
    try:
        from .foreground_flow_runner import _focus_window

        _focus_window(hwnd)
    except Exception:
        logger.debug("[OCR防护] 置顶目标窗口失败", exc_info=True)
        return False
    time.sleep(0.4)
    new_ratio = _window_obscured_ratio(hwnd)
    return new_ratio is not None and new_ratio < _OCCLUSION_REFUSE_RATIO


def _window_obscured_ratio(hwnd: int, samples: int = 5) -> Optional[float]:
    """返回窗口被遮挡的采样点比例（0.0~1.0）；无法判定返回 None。"""
    try:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        if win32gui.IsIconic(hwnd):
            return 1.0
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return None
        try:
            root_owner = ctypes.windll.user32.GetAncestor(hwnd, 2)  # GA_ROOTOWNER
        except Exception:
            root_owner = hwnd
        try:
            # Win8+ 需进程声明 DPI 感知，否则 WindowFromPoint 返回的是缩放后坐标
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

        covered = 0
        total = 0
        for ix in range(1, samples + 1):
            for iy in range(1, samples + 1):
                x = left + int(width * ix / (samples + 1))
                y = top + int(height * iy / (samples + 1))
                try:
                    pt = ctypes.wintypes.POINT(int(x), int(y))
                    hit = ctypes.windll.user32.WindowFromPoint(pt)
                except Exception:
                    continue
                if not hit:
                    continue
                total += 1
                if hit == hwnd or hit == root_owner:
                    continue
                # 子窗口（控件）不算遮挡
                try:
                    if ctypes.windll.user32.GetAncestor(hit, 2) == root_owner:
                        continue
                except Exception:
                    pass
                covered += 1
        if total == 0:
            return None
        return covered / float(total)
    except Exception:
        return None


def _alert_window_obscured(logger: logging.Logger, hwnd: int, ratio: float) -> None:
    """节流告警：目标窗口被遮挡，抓屏会拍到遮挡窗口的内容。"""
    global _last_obscure_check_ts
    now = time.time()
    if now - _last_obscure_check_ts < _OBSCURE_CHECK_INTERVAL_SEC:
        return
    _last_obscure_check_ts = now
    title = ""
    try:
        title = win32gui.GetWindowText(hwnd) or ""
    except Exception:
        pass
    logger.warning(
        f"[OCR防护] ⚠️ 目标窗口被遮挡约 {ratio * 100:.0f}%（HWND={hwnd} 标题={title!r}）"
    )
    logger.warning(
        "[OCR防护] 截图是屏幕合成结果，被遮挡区域会把遮挡窗口的文字一起识别进来。"
        "请将目标窗口置顶或移开遮挡窗口。"
    )


_FULLSCREEN_ALERT_INTERVAL_SEC = 60.0
_last_fullscreen_alert_ts = 0.0


def _alert_fullscreen_scope(logger: logging.Logger) -> None:
    """节流告警：本次识别没有任何区域约束，拿到的是整屏内容。"""
    global _last_fullscreen_alert_ts
    now = time.time()
    if now - _last_fullscreen_alert_ts < _FULLSCREEN_ALERT_INTERVAL_SEC:
        return
    _last_fullscreen_alert_ts = now
    logger.warning(
        "[OCR防护] ⚠️ 本次 OCR 未限定识别区域，将扫描整个屏幕"
        "（日志窗口、控制台、浏览器内容都会被一并识别，可能造成假匹配）"
    )
    logger.warning(
        "[OCR防护] 如需缩小范围：1) 调用时传 hwnd 或 region；"
        "2) 在 flow_configs.json 对应步骤配置 local_region / 保持 search_scope=\"window\"。"
    )


_DEAD_WINDOW_ALERT_INTERVAL_SEC = 30.0
_last_dead_window_alert = {"ts": 0.0, "hwnd": None}


def _restore_window_if_minimized(hwnd: int) -> bool:
    """窗口被最小化时尝试还原（SW_RESTORE）。成功返回 True；非最小化/已死/失败返回 False。"""
    try:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        if not win32gui.IsIconic(hwnd):
            return False
        win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.2)
        return not win32gui.IsIconic(hwnd)
    except Exception:
        return False


def _ensure_window_region(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """取窗口的屏幕绝对坐标；窗口最小化返回 None。"""
    try:
        if win32gui.IsIconic(hwnd):
            return None
    except Exception:
        pass
    return _get_window_region(hwnd)


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
    screenshot = grab_screen(region=region) if region else grab_screen()
    target = _normalize_text(target_text)

    # 优先使用 RapidOCR（中文识别准确率更高）
    if _get_rapidocr_engine:
        try:
            ocr_engine = _get_rapidocr_engine()
            if ocr_engine:
                result, elapse = ocr_engine(np.asarray(screenshot))
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
        shot = grab_screen(region=region) if region else grab_screen()
        shot.save(debug_file)
        logger.warning(f"已保存失败截图: {debug_file}")
        # 额外输出 OCR 框选叠加图，便于定位识别偏差
        try:
            # 优先输出 RapidOCR 的调试信息
            if _get_rapidocr_engine:
                ocr_engine = _get_rapidocr_engine()
                if ocr_engine:
                    rapid_result, _ = ocr_engine(np.asarray(shot))
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

    区域优先级（重要）：
        region（调用方显式指定的局部区域）> hwnd 对应窗口区域 > 全屏
    以前写成 `_get_window_region(hwnd) if hwnd else region`，只要传了 hwnd，
    调用方精调的局部搜索区域就会被整窗覆盖，`_calc_local_search_region` 形同虚设。

    窗口不可用时的行为（2026-10-17 起，重要）：
        传了 hwnd 但窗口最小化/已关闭 → 先尝试 SW_RESTORE 还原；还原失败 →
        打 ERROR 并返回空列表，**不再回退全屏识别**。
        旧行为（安静回退全屏）闯过大祸：全屏把日志窗口文字卷进来 → 假命中 →
        "自己干扰自己"。返回空让上层按"未匹配"处理，步骤重试时
        run_flow 的 _refresh_flow_window 会重新枚举窗口，比全屏瞎认更正确。
    """
    global _tesseract_error_logged
    results = []
    if region:
        current_region = region
    elif hwnd:
        current_region = _ensure_window_region(hwnd)
        if current_region is None and _restore_window_if_minimized(hwnd):
            current_region = _ensure_window_region(hwnd)
        if current_region is None:
            now = time.time()
            if (
                now - _last_dead_window_alert["ts"] >= _DEAD_WINDOW_ALERT_INTERVAL_SEC
                or _last_dead_window_alert["hwnd"] != hwnd
            ):
                _last_dead_window_alert["ts"] = now
                _last_dead_window_alert["hwnd"] = hwnd
                logger.error(
                    f"[OCR防护] ❌ 窗口不可用（已最小化或已关闭）HWND={hwnd}，"
                    f"还原失败，拒绝回退全屏识别，本次返回空结果"
                    f"（上层将按未匹配重试，并借 _refresh_flow_window 重新枚举窗口）"
                )
            return []
    else:
        current_region = None

    # 防护1：目标窗口被遮挡告警（只在指定了 hwnd 且未显式限定 region 时检查）
    if hwnd and not region:
        ratio = _window_obscured_ratio(hwnd)
        if ratio is not None and ratio >= 0.3:
            _alert_window_obscured(logger, hwnd, ratio)
            if ratio >= _OCCLUSION_REFUSE_RATIO:
                # 几乎完全被挡住：截图内容基本是遮挡窗口的，识别结果必假。
                # 先尝试把目标提到前台；仍不可见就拒绝识别（返回空结果），
                # 让上层按"未匹配"重试，而不是拿着假坐标去点。
                if not _try_raise_target(logger, hwnd):
                    logger.error(
                        f"[OCR防护] ❌ 目标窗口几乎完全被遮挡（{ratio * 100:.0f}%），"
                        f"置顶后仍不可见，本次拒绝识别（返回空结果），"
                        f"避免把遮挡窗口的文字当成目标"
                    )
                    return []

    # 防护3：无任何区域约束时说明本次是全屏识别，日志/其他窗口的内容会全部进来。
    # 这是历史上"自己干扰自己"的直接成因，必须显式留痕（节流）。
    if current_region is None and hwnd is None and not region:
        _alert_fullscreen_scope(logger)

    screenshot = grab_screen(region=current_region) if current_region else grab_screen()

    # 防护2：坐标系自检。WGC 抓的是 monitor_index=1 那一块屏，若窗口区域
    # 超出该屏范围，裁剪出来的坐标会整体错位，必须先发现再告警（不静默出错）。
    try:
        sw, sh = screenshot.size
        if current_region:
            rw, rh = current_region[2], current_region[3]
            if rw > sw or rh > sh:
                logger.warning(
                    f"[OCR防护] ⚠️ 识别区域({rw}x{rh})超出截图({sw}x{sh})，"
                    f"可能因显示器缩放/多屏导致坐标错位 region={current_region} hwnd={hwnd}"
                )
    except Exception:
        pass

    # 使用 RapidOCR
    if _get_rapidocr_engine:
        try:
            ocr_engine = _get_rapidocr_engine()
            if ocr_engine:
                result, elapse = ocr_engine(np.asarray(screenshot))
                if result:
                    for box, text, confidence in result:
                        # rapidocr_onnxruntime 1.2.x 的 score 是字符串，必须转 float，
                        # 否则下游 `confidence >= min_confidence` 会 str>=float 崩溃
                        try:
                            confidence = float(confidence)
                        except (TypeError, ValueError):
                            confidence = 0.0
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
                    # 1.2.x 的 elapse 是 [det, cls, rec] 列表，不能直接 :.1f 格式化
                    if isinstance(elapse, (list, tuple)):
                        elapse_ms = sum(float(e) for e in elapse) * 1000
                    else:
                        elapse_ms = float(elapse or 0) * 1000
                    logger.debug(f"[RapidOCR] 识别到 {len(results)} 个文本区域，耗时 {elapse_ms:.1f}ms")
                    if len(results) <= 5:  # 只在数量少时输出详细信息
                        for i, r in enumerate(results):
                            logger.debug(f"  [{i+1}] \"{r['text']}\" ({r['confidence']:.2f}) @ {r['center']}")
                    return _check_log_contamination(logger, results)
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

    return _check_log_contamination(logger, results)


def ocr_find_any_text(
    target_texts: List[str],
    logger: logging.Logger,
    hwnd: Optional[int] = None,
    region: Optional[Tuple[int, int, int, int]] = None,
    min_confidence: float = 0.5,
) -> Optional[Dict]:
    """抄 ok-script match=[...]：单次 OCR 统一过滤多候选，避免每候选截一次屏。

    返回首个命中的结果 dict（含 matched_target），未命中返回 None。
    """
    candidates = [str(t or "").strip() for t in (target_texts or []) if str(t or "").strip()]
    if not candidates:
        return None
    normalized = [_normalize_text(t) for t in candidates]
    results = ocr_window_content(logger, hwnd=hwnd, region=region)
    for idx, target_normalized in enumerate(normalized):
        for r in results:
            text_normalized = _normalize_text(r["text"])
            if r["confidence"] < min_confidence:
                continue
            if target_normalized in text_normalized:
                logger.info(f"[OCR匹配] ✅ 找到 \"{candidates[idx]}\" -> \"{r['text']}\" ({r['confidence']:.2f}) @ {r['center']}")
                return {**r, "matched_target": candidates[idx]}
            if (
                text_normalized in target_normalized
                and len(text_normalized) >= max(2, len(target_normalized) // 2)
            ):
                logger.info(f"[OCR匹配] ✅ 找到 \"{candidates[idx]}\" -> \"{r['text']}\" ({r['confidence']:.2f}) @ {r['center']}")
                return {**r, "matched_target": candidates[idx]}
    logger.debug(f"[OCR匹配] 未找到: {candidates} (扫描了 {len(results)} 个区域)")
    return None


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

