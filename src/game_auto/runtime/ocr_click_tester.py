import os
import re
import time
import math
import ctypes
import ctypes.wintypes
import logging
import shutil
import sqlite3
from typing import Any, Optional, Tuple, List, Dict
import threading

import pyautogui
import win32gui
from PIL import ImageOps, ImageEnhance, ImageDraw, Image, ImageFont
import numpy as np
from ..settings.paths import OCR_DEBUG_DIR, TEMPLATES_DIR, TESSDATA_DIR, TESSERACT_EXE, WORKFLOW_DB_PATH as PROJECT_WORKFLOW_DB_PATH

_LOGGER = logging.getLogger("GameAutomation")

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
                except Exception as exc:
                    _LOGGER.debug("[OCR] 注册 CUDA DLL 目录失败 %s: %s", p, exc)
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
        # 帧回调失败只丢失当前帧，下一次回调会继续，不能刷日志
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
                except Exception as exc:
                    lg.debug("[OCR] 读取生效 EP 失败: %s", exc)
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


# ── OCR 结构化匹配（2026-10-31）──────────────────────────────────────────────
# 旧实现是「目标按配置顺序、结果按 OCR 顺序，先命中先返回」的子串匹配：
# MiniMax「签到得」会先撞上卡片宣传语「连续签到得更多积分」，点进文案中间，
# 真正的按钮从未被点到。堆排除词治标不治本，这里把「选哪个框」拆成三层：
#   匹配等级（精确 > 前缀 > 子串 > OCR 漏字反向）→ 锚点几何 → 置信度。
# exclude_texts 仍保留，但只作为最后兜底，不再承担主要识别逻辑。
_MATCH_TIER_EXACT = 4
_MATCH_TIER_PREFIX = 3
_MATCH_TIER_SUBSTRING = 2
_MATCH_TIER_REVERSE = 1
_MATCH_TIER_LABELS = {
    _MATCH_TIER_EXACT: "exact",
    _MATCH_TIER_PREFIX: "prefix",
    _MATCH_TIER_SUBSTRING: "substring",
    _MATCH_TIER_REVERSE: "reverse",
}
_MATCH_MODES = ("legacy", "smart", "exact", "prefix", "substring")
_OCR_DEBUG_ENV = "GAMEAUTO_OCR_DEBUG"
_DEBUG_SAVE_INTERVAL_SEC = 5.0
_last_debug_save: Dict[str, float] = {}
_near_without_anchor_warned = set()


def _normalize_match_mode(mode: Any) -> str:
    value = str(mode or "").strip().lower()
    return value if value in _MATCH_MODES else "legacy"


def _ocr_match_tier(text_norm: str, target_norm: str, mode: str = "smart") -> int:
    """返回文本与目标文字的匹配等级；0 表示不匹配。等级越大越可信。"""
    if not text_norm or not target_norm:
        return 0
    if text_norm == target_norm:
        return _MATCH_TIER_EXACT
    if mode == "exact":
        return 0
    if text_norm.startswith(target_norm):
        return _MATCH_TIER_PREFIX
    if mode == "prefix":
        return 0
    if target_norm in text_norm:
        return _MATCH_TIER_SUBSTRING
    if mode == "substring":
        return 0
    # 反向匹配：OCR 漏字时（target=进入脱机, OCR=脱机）仍可命中。
    # 放在最低等级，避免它抢走真正包含目标词的候选。
    if text_norm in target_norm and len(text_norm) >= max(2, len(target_norm) // 2):
        return _MATCH_TIER_REVERSE
    return 0


def _normalize_anchor(anchor: Any) -> Tuple[List[str], bool]:
    """解析 ocr_anchor：支持字符串 / 列表 / {"text(s)": [...], "required": bool}。"""
    required = False
    raw = anchor
    if isinstance(anchor, dict):
        required = bool(anchor.get("required", False))
        raw = anchor.get("texts")
        if not raw:
            raw = anchor.get("text")
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    texts: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        texts.append(text)
    return texts, required


def _normalize_near(near: Any) -> Dict[str, Any]:
    """解析 ocr_near 几何约束：direction / max_distance / max_dx / min_dy。"""
    if not isinstance(near, dict):
        return {}
    direction = str(near.get("direction") or "near").strip().lower()
    if direction not in ("near", "below", "above"):
        direction = "near"
    config: Dict[str, Any] = {"direction": direction}
    for key in ("max_distance", "max_dx", "min_dy"):
        value = near.get(key)
        if isinstance(value, (int, float)) and float(value) > 0:
            config[key] = float(value)
    return config


def _result_center(result: Dict) -> Optional[Tuple[float, float]]:
    center = result.get("center")
    if center is not None:
        try:
            return float(center[0]), float(center[1])
        except (TypeError, ValueError, IndexError):
            pass
    box = result.get("box")
    if box is not None and len(box) >= 4:
        try:
            return (float(box[0]) + float(box[2])) / 2.0, (float(box[1]) + float(box[3])) / 2.0
        except (TypeError, ValueError, IndexError):
            return None
    return None


def _center_distance(a: Dict, b: Dict) -> Optional[float]:
    ca, cb = _result_center(a), _result_center(b)
    if ca is None or cb is None:
        return None
    return math.hypot(ca[0] - cb[0], ca[1] - cb[1])


def _pick_anchor_item(items: List[Dict], anchor_norms: List[str]) -> Optional[Dict]:
    """在可用结果里选锚点：匹配等级优先，其次置信度，再次锚点配置顺序。"""
    best = None
    best_key = None
    for item in items:
        # 锚点可以同时是「被排除的候选」：MiniMax 的锚点「连续签到」正好出现在
        # 宣传语「连续签到得更多积分」里，排除词要挡住它被点击，但不能挡住它
        # 当几何参考，否则锚点永远找不到、几何过滤形同虚设。
        if item.get("status") not in ("filtered", "excluded"):
            continue
        for order, anchor_norm in enumerate(anchor_norms):
            tier = _ocr_match_tier(item["text_norm"], anchor_norm, "smart")
            if tier <= 0:
                continue
            key = (tier, item["confidence"], -order)
            if best_key is None or key > best_key:
                best_key = key
                best = item
    return best


def _near_violation(result: Dict, anchor_result: Dict, near: Dict[str, Any], distance: Optional[float]) -> str:
    """检查候选相对锚点是否满足几何约束；返回空串表示通过。"""
    if distance is None:
        return "缺少坐标"
    ca, cb = _result_center(result), _result_center(anchor_result)
    if ca is None or cb is None:
        return "缺少坐标"
    dx = ca[0] - cb[0]
    dy = ca[1] - cb[1]
    min_dy = float(near.get("min_dy") or 4.0)
    direction = near.get("direction") or "near"
    if direction == "below" and dy < min_dy:
        return f"不在锚点下方(dy={dy:.0f})"
    if direction == "above" and -dy < min_dy:
        return f"不在锚点上方(dy={dy:.0f})"
    max_dx = float(near.get("max_dx") or 0.0)
    if max_dx > 0 and abs(dx) > max_dx:
        return f"水平超出锚点(dx={dx:.0f}>{max_dx:.0f})"
    max_distance = float(near.get("max_distance") or 0.0)
    if max_distance > 0 and distance > max_distance:
        return f"超出锚点距离({distance:.0f}>{max_distance:.0f})"
    return ""


def _rank_ocr_candidates(
    results: List[Dict],
    candidates: List[str],
    mode: str,
    min_confidence: float,
    exclude_texts: Optional[List[str]] = None,
    anchor_texts: Optional[List[str]] = None,
    anchor_required: bool = False,
    near: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict], Dict[str, Any]]:
    """对全量 OCR 结果做结构化排序。

    返回 (最优候选 item, 元信息)。item 形如：
        {"result": OCR结果, "text", "confidence", "tier", "target_idx",
         "score", "distance", "status", "reason"}
    status: filtered/matched/excluded/low_conf/near_rejected/anchor_missing/
            not_target/duplicate_target/empty。
    """
    normalized_targets = [
        (idx, target, _normalize_text(target))
        for idx, target in enumerate(candidates or [])
        if _normalize_text(target)
    ]
    excluded = [_normalize_text(t) for t in (exclude_texts or []) if _normalize_text(t)]
    anchor_norms = [_normalize_text(t) for t in (anchor_texts or []) if _normalize_text(t)]
    near_cfg = near or {}

    items: List[Dict] = []
    for result in results or []:
        try:
            confidence = float(result.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        text = str(result.get("text") or "")
        text_norm = _normalize_text(text)
        item = {
            "result": result,
            "text": text,
            "text_norm": text_norm,
            "confidence": confidence,
            "tier": 0,
            "target_idx": -1,
            "score": 0.0,
            "distance": None,
            "status": "filtered",
            "reason": "",
        }
        if not text_norm:
            item["status"] = "empty"
        elif confidence < min_confidence:
            item["status"] = "low_conf"
        elif any(term in text_norm for term in excluded):
            item["status"] = "excluded"
        items.append(item)

    anchor_item = _pick_anchor_item(items, anchor_norms) if anchor_norms else None

    for item in items:
        if item["status"] != "filtered":
            continue
        if anchor_norms and anchor_item is None and anchor_required:
            item["status"] = "anchor_missing"
            item["reason"] = "锚点文字未找到"
            continue
        if anchor_item is not None:
            item["distance"] = _center_distance(item["result"], anchor_item["result"])
            if near_cfg:
                reason = _near_violation(item["result"], anchor_item["result"], near_cfg, item["distance"])
                if reason:
                    item["status"] = "near_rejected"
                    item["reason"] = reason
                    continue
        best_tier = 0
        best_idx = -1
        for idx, _target, target_norm in normalized_targets:
            tier = _ocr_match_tier(item["text_norm"], target_norm, mode)
            if tier > best_tier:
                best_tier = tier
                best_idx = idx
        if best_tier <= 0:
            item["status"] = "not_target"
            continue
        item["tier"] = best_tier
        item["target_idx"] = best_idx
        # 等级压倒一切（tier*100000），同等级内锚点距离优先、置信度其次。
        # 目标顺序只做极小权重，避免多个同分候选时随机漂移。
        score = best_tier * 100000.0 + item["confidence"] * 100.0 - best_idx * 5.0
        if anchor_item is not None and item["distance"] is not None:
            max_distance = float(near_cfg.get("max_distance") or 0.0)
            if max_distance > 0:
                score += max(0.0, max_distance - item["distance"]) / max_distance * 3000.0
            else:
                score += max(0.0, 3000.0 - item["distance"])
        item["score"] = score
        item["status"] = "matched"

    # 同一个 OCR 文本框可能同时命中多个目标文案，只保留分数最高的一条，
    # 否则歧义检测会把同一个框误报成两个候选。
    best_by_key: Dict[Tuple[str, int, int], Dict] = {}
    for item in items:
        if item["status"] != "matched":
            continue
        center = _result_center(item["result"])
        if center is None:
            key = (item["text_norm"], 0, 0)
        else:
            key = (item["text_norm"], int(center[0] // 8), int(center[1] // 8))
        previous = best_by_key.get(key)
        if previous is None or item["score"] > previous["score"]:
            if previous is not None:
                previous["status"] = "duplicate_target"
                previous["reason"] = "同一文本框命中多个候选"
            best_by_key[key] = item
        else:
            item["status"] = "duplicate_target"
            item["reason"] = "同一文本框命中多个候选"

    scored = sorted(
        best_by_key.values(),
        key=lambda item: (-item["score"], -item["confidence"], item["target_idx"]),
    )

    ambiguous: List[Dict] = []
    if scored:
        top = scored[0]
        for other in scored[1:]:
            if other["tier"] != top["tier"]:
                break
            distance = _center_distance(other["result"], top["result"])
            if distance is not None and distance > 40 and abs(other["score"] - top["score"]) <= 200:
                ambiguous.append(other)

    meta = {
        "items": items,
        "scored": scored,
        "ambiguous": ambiguous,
        "anchor_item": anchor_item,
        "mode": mode,
        "anchor_texts": anchor_texts or [],
        "near": near_cfg,
        "exclude_texts": exclude_texts or [],
    }
    return (scored[0] if scored else None), meta


def _log_candidate_ranking(
    logger: logging.Logger,
    candidates: List[str],
    meta: Dict[str, Any],
    chosen: Optional[Dict],
) -> None:
    """输出候选排序。命中时 INFO 打前 5 条；未命中时 DEBUG，避免轮询刷屏。"""
    anchor_item = meta.get("anchor_item")
    anchor_desc = "未找到"
    if anchor_item is not None:
        anchor_desc = f"\"{anchor_item['text']}\"@{anchor_item['result'].get('center')}"
    scored = meta.get("scored") or []
    level = logger.info if chosen is not None else logger.debug
    level(
        "[OCR匹配] 候选排序（模式=%s 锚点=%s 目标=%s）",
        meta.get("mode"),
        anchor_desc,
        " / ".join(candidates),
    )
    for pos, item in enumerate(scored[:5], 1):
        distance = f"{item['distance']:.0f}" if item["distance"] is not None else "-"
        level(
            "  #%d \"%s\" conf=%.2f tier=%s dist=%s score=%.0f%s",
            pos,
            item["text"],
            item["confidence"],
            _MATCH_TIER_LABELS.get(item["tier"], item["tier"]),
            distance,
            item["score"],
            "  <== 选中" if item is chosen else "",
        )
    # 第 6 条起只在 debug 里输出，保证「全量候选」可回查又不刷 INFO 日志。
    for pos, item in enumerate(scored[5:], 6):
        distance = f"{item['distance']:.0f}" if item["distance"] is not None else "-"
        logger.debug(
            "  #%d \"%s\" conf=%.2f tier=%s dist=%s score=%.0f",
            pos,
            item["text"],
            item["confidence"],
            _MATCH_TIER_LABELS.get(item["tier"], item["tier"]),
            distance,
            item["score"],
        )
    for item in meta.get("items") or []:
        if item["status"] in ("matched", "filtered"):
            continue
        distance = f"{item['distance']:.0f}" if item.get("distance") is not None else "-"
        logger.debug(
            "  [%s] \"%s\" conf=%.2f dist=%s%s",
            item["status"],
            item["text"],
            item["confidence"],
            distance,
            f" ({item['reason']})" if item.get("reason") else "",
        )
    if chosen is None:
        status_counts: Dict[str, int] = {}
        for item in meta.get("items") or []:
            status = item["status"]
            if status == "matched":
                continue
            status_counts[status] = status_counts.get(status, 0) + 1
        logger.debug("[OCR匹配] 未选中候选状态统计: %s", status_counts)


def _debug_overlay_enabled() -> bool:
    return os.environ.get(_OCR_DEBUG_ENV, "0") == "1"


def _load_debug_font(size: int):
    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _save_match_debug(
    screenshot,
    meta: Dict[str, Any],
    candidates: List[str],
    tag: Optional[str],
    force: bool = False,
    chosen_override: Optional[Dict] = None,
) -> Optional[str]:
    """把当帧 OCR 框、状态、分数画在截图上，供调参定位（失败不抛异常）。"""
    if screenshot is None:
        return None
    key = str(tag or "ocr_match")
    now = time.time()
    if not force and now - _last_debug_save.get(key, 0.0) < _DEBUG_SAVE_INTERVAL_SEC:
        return None
    _last_debug_save[key] = now
    try:
        os.makedirs(PROJECT_OCR_DEBUG_DIR, exist_ok=True)
        base = screenshot.convert("RGBA")
        header_h = 42
        band = Image.new("RGBA", base.size, (0, 0, 0, 0))
        band_draw = ImageDraw.Draw(band)
        band_draw.rectangle((0, 0, base.width, header_h), fill=(0, 0, 0, 185))
        base = Image.alpha_composite(base, band)
        draw = ImageDraw.Draw(base)
        font = _load_debug_font(14)
        small = _load_debug_font(12)
        anchor_text = "/".join(meta.get("anchor_texts") or []) or "-"
        line1 = f"{key}  mode={meta.get('mode')}  anchor={anchor_text}"
        line2 = "target=" + " / ".join(candidates)
        draw.text((6, 2), line1[:120], fill=(255, 255, 255), font=font)
        draw.text((6, 21), line2[:120], fill=(170, 215, 255), font=small)

        chosen = chosen_override or (meta.get("scored")[0] if meta.get("scored") else None)
        anchor_item = meta.get("anchor_item")
        colors = {
            "matched": (0, 170, 255),
            "excluded": (255, 70, 70),
            "low_conf": (150, 150, 150),
            "near_rejected": (255, 0, 255),
            "anchor_missing": (255, 70, 70),
            "duplicate_target": (130, 130, 130),
            "not_target": (90, 90, 90),
            "empty": (90, 90, 90),
            "filtered": (120, 200, 120),
        }
        for item in meta.get("items") or []:
            box = item["result"].get("box")
            if box is None or len(box) < 4:
                continue
            try:
                x1, y1, x2, y2 = [int(float(v)) for v in box[:4]]
            except (TypeError, ValueError):
                continue
            color = colors.get(item["status"], (120, 120, 120))
            width = 2
            label = f"{item['status']} \"{item['text']}\" ({item['confidence']:.2f})"
            if item is chosen:
                color, width = (0, 255, 0), 4
                label = (
                    f"CHOSEN \"{item['text']}\" ({item['confidence']:.2f}) "
                    f"{_MATCH_TIER_LABELS.get(item['tier'], item['tier'])} s={item['score']:.0f}"
                )
            elif item is anchor_item:
                color, width = (255, 165, 0), 3
                label = f"ANCHOR \"{item['text']}\" ({item['confidence']:.2f})"
            if item.get("distance") is not None:
                label += f" d={item['distance']:.0f}"
            if item.get("reason"):
                label += f" {item['reason']}"
            draw.rectangle((x1, y1, x2, y2), outline=color, width=width)
            draw.text((x1, max(header_h, y1 - 16)), label[:90], fill=color, font=small)
            if item is chosen:
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                draw.line((cx - 14, cy, cx + 14, cy), fill=(0, 255, 0), width=3)
                draw.line((cx, cy - 14, cx, cy + 14), fill=(0, 255, 0), width=3)

        safe_tag = re.sub(r'[\\/:*?"<>|\s]+', "_", key)[:80] or "ocr"
        out = os.path.join(PROJECT_OCR_DEBUG_DIR, f"ocr_match_{safe_tag}_{int(now * 1000)}.jpg")
        base.convert("RGB").save(out, quality=82)
        _LOGGER.info(f"[OCR标注] 候选标注图已保存: {out}")
        return out
    except Exception as exc:
        _LOGGER.debug("[OCR标注] 保存候选标注图失败: %s", exc)
        return None


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
            except Exception as exc:
                logger.debug("[OCR] 模板定位失败 %s: %s", template_path, exc)
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
    except Exception as exc:
        _LOGGER.debug("[OCR] 保存调试覆盖图失败: %s", exc)


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
            # 旧系统没有 shcore 或已设置过 DPI 感知，忽略
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
                    # 单点查询失败按遮挡处理，继续采样
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
    except Exception as exc:
        logger.debug("[OCR防护] 读取窗口标题失败: %s", exc)
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
    except Exception as exc:
        _LOGGER.debug("[OCR] 判断窗口最小化失败: %s", exc)
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
        except Exception as exc:
            logger.debug("[OCR] 保存点击调试图失败: %s", exc)
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


def _ocr_scan(
    logger: logging.Logger,
    hwnd: Optional[int] = None,
    region: Optional[Tuple[int, int, int, int]] = None,
) -> Tuple[List[Dict], Optional[Image.Image], Optional[Tuple[int, int, int, int]]]:
    """执行一次 OCR，返回 (识别结果, 实际截图, 实际识别区域)。

    box 是截图局部坐标，center 已换算成屏幕绝对坐标；截图供结构化匹配的
    标注图使用。其余防护逻辑与旧 ocr_window_content 完全一致：

    区域优先级（重要）：
        region（调用方显式指定的局部区域）> hwnd 对应窗口区域 > 全屏
    以前写成 `_get_window_region(hwnd) if hwnd else region`，只要传了 hwnd，
    调用方精调的局部搜索区域就会被整窗覆盖，`_calc_local_search_region` 形同虚设。

    窗口不可用时的行为（2026-10-17 起，重要）：
        传了 hwnd 但窗口最小化/已关闭 → 先尝试 SW_RESTORE 还原；还原失败 →
        打 ERROR 并返回空结果，**不再回退全屏识别**。
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
            return [], None, None
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
                    return [], None, None

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
    except Exception as exc:
        logger.debug("[OCR防护] 坐标系自检失败: %s", exc)

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
                    return _check_log_contamination(logger, results), screenshot, current_region
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

    return _check_log_contamination(logger, results), screenshot, current_region


def ocr_window_content(
    logger: logging.Logger,
    hwnd: Optional[int] = None,
    region: Optional[Tuple[int, int, int, int]] = None,
) -> List[Dict]:
    """对窗口/区域做一次 OCR，只返回识别结果（防护逻辑见 _ocr_scan）。"""
    results, _screenshot, _region = _ocr_scan(logger, hwnd=hwnd, region=region)
    return results


def ocr_find_any_text(
    target_texts: List[str],
    logger: logging.Logger,
    hwnd: Optional[int] = None,
    region: Optional[Tuple[int, int, int, int]] = None,
    min_confidence: float = 0.5,
    exclude_texts: Optional[List[str]] = None,
    match_mode: str = "legacy",
    anchor: Any = None,
    near: Optional[Dict[str, Any]] = None,
    ambiguous_action: str = "warn",
    debug_tag: Optional[str] = None,
    save_debug: bool = False,
) -> Optional[Dict]:
    """抄 ok-script match=[...]：单次 OCR 统一过滤多候选，避免每候选截一次屏。

    match_mode:
        legacy（默认）：目标按配置顺序、结果按 OCR 顺序，先命中先返回
            （兼容旧流程，存在「签到得」撞宣传语的问题）。
        smart：精确 > 前缀 > 子串 > OCR 漏字反向，再按锚点距离/置信度排序。
        exact / prefix / substring：只允许对应的匹配等级，更严格。
    anchor / near:
        anchor 先找锚点文字（字符串、列表或 {"texts": [...], "required": bool}），
        near 支持 {"direction": "below|above|near", "max_distance", "max_dx", "min_dy"}。
        配了锚点时默认只考虑满足几何约束的候选；锚点找不到时默认回退全量
        （required=true 则直接判未匹配）。
    exclude_texts:
        命中这些子串的结果先剔除，仅作最后兜底。
    debug_tag / save_debug:
        保存一张候选标注图（所有 OCR 框 + 状态 + 分数 + 选中框）到 data/artifacts/ocr_debug。
        GAMEAUTO_OCR_DEBUG=1 可全局开启；歧义时无论开关都会保存一张。
    """
    candidates = [str(t or "").strip() for t in (target_texts or []) if str(t or "").strip()]
    if not candidates:
        return None
    mode = _normalize_match_mode(match_mode)
    anchor_texts, anchor_required = _normalize_anchor(anchor)
    near_cfg = _normalize_near(near)
    if near_cfg and not anchor_texts:
        # 同一份配置在步骤搜索循环里会反复进来，只告警一次，避免每秒刷日志
        warn_key = repr(near)
        if warn_key not in _near_without_anchor_warned:
            _near_without_anchor_warned.add(warn_key)
            logger.warning("[OCR匹配] ocr_near 已配置但没有 ocr_anchor，几何约束无法生效（已忽略）")
        near_cfg = {}
    if (anchor_texts or near_cfg) and mode == "legacy":
        # 配了锚点/几何约束却仍按「先命中先返回」等于没配，自动升级为结构化排序
        mode = "smart"

    results, screenshot, _scan_region = _ocr_scan(logger, hwnd=hwnd, region=region)

    if mode == "legacy":
        normalized = [_normalize_text(t) for t in candidates]
        excluded = [_normalize_text(t) for t in (exclude_texts or []) if str(t or "").strip()]
        if excluded:
            filtered = []
            for r in results:
                text_normalized = _normalize_text(r["text"])
                if any(term and term in text_normalized for term in excluded):
                    logger.debug(f"[OCR匹配] 跳过排除词命中: \"{r['text']}\"")
                    continue
                filtered.append(r)
            results = filtered
        found = None
        matched_target = None
        for idx, target_normalized in enumerate(normalized):
            for r in results:
                text_normalized = _normalize_text(r["text"])
                if r["confidence"] < min_confidence:
                    continue
                if target_normalized in text_normalized:
                    found, matched_target = r, candidates[idx]
                    break
                if (
                    text_normalized in target_normalized
                    and len(text_normalized) >= max(2, len(target_normalized) // 2)
                ):
                    found, matched_target = r, candidates[idx]
                    break
            if found is not None:
                break
        if found is None:
            logger.debug(f"[OCR匹配] 未找到: {candidates} (扫描了 {len(results)} 个区域)")
        # legacy 也支持候选标注：用 smart 视角把全量候选和分数画出来，
        # 但选中框仍然是 legacy 实际命中的那个（避免调试图与实际行为不一致）。
        debug_image = None
        if save_debug or _debug_overlay_enabled():
            debug_meta = _rank_ocr_candidates(
                results,
                candidates,
                "smart",
                min_confidence,
                exclude_texts=exclude_texts,
            )[1]
            chosen_item = None
            if found is not None:
                chosen_item = next(
                    (item for item in debug_meta["items"] if item["result"] is found),
                    None,
                )
                if chosen_item is None:
                    chosen_item = {
                        "result": found,
                        "text": str(found.get("text") or ""),
                        "confidence": float(found.get("confidence") or 0.0),
                        "tier": 0,
                        "target_idx": -1,
                        "score": 0.0,
                        "distance": None,
                        "status": "matched",
                        "reason": "legacy",
                    }
                    debug_meta["items"].append(chosen_item)
            debug_image = _save_match_debug(
                screenshot,
                debug_meta,
                candidates,
                debug_tag,
                chosen_override=chosen_item,
            )
        if found is not None:
            logger.info(
                f"[OCR匹配] ✅ 找到 \"{matched_target}\" -> \"{found['text']}\" "
                f"({found['confidence']:.2f}) @ {found['center']} [模式=legacy]"
            )
            return {**found, "matched_target": matched_target, "debug_image": debug_image}
        return None

    chosen, meta = _rank_ocr_candidates(
        results,
        candidates,
        mode=mode,
        min_confidence=min_confidence,
        exclude_texts=exclude_texts,
        anchor_texts=anchor_texts,
        anchor_required=anchor_required,
        near=near_cfg,
    )
    _log_candidate_ranking(logger, candidates, meta, chosen)

    should_save_debug = bool(save_debug or _debug_overlay_enabled() or meta["ambiguous"])
    debug_image = None
    if should_save_debug:
        debug_image = _save_match_debug(
            screenshot,
            meta,
            candidates,
            debug_tag,
            force=bool(meta["ambiguous"]),
        )

    if meta["ambiguous"] and chosen is not None:
        others = " / ".join(
            f"\"{item['text']}\"@{item['result'].get('center')}" for item in meta["ambiguous"]
        )
        logger.warning(
            f"[OCR匹配] ⚠️ 候选歧义：\"{chosen['text']}\"@{chosen['result'].get('center')} "
            f"与 {others} 等级/分数接近，已按分数选择前者"
            + (f"（标注图: {debug_image}）" if debug_image else "")
        )
        if str(ambiguous_action or "").strip().lower() == "fail":
            logger.warning("[OCR匹配] 候选歧义且配置为 fail，放弃本次匹配（不瞎点）")
            return None

    if chosen is None:
        logger.debug(
            f"[OCR匹配] 未找到: {candidates} (扫描了 {len(results)} 个区域，模式={mode})"
        )
        return None

    matched = {
        **chosen["result"],
        "matched_target": candidates[chosen["target_idx"]],
        "match_mode": mode,
        "match_tier": _MATCH_TIER_LABELS.get(chosen["tier"], str(chosen["tier"])),
        "score": chosen["score"],
        "ambiguous": bool(meta["ambiguous"]),
        "debug_image": debug_image,
    }
    anchor_item = meta.get("anchor_item")
    if anchor_item is not None:
        matched["anchor_center"] = anchor_item["result"].get("center")
    logger.info(
        f"[OCR匹配] ✅ 找到 \"{candidates[chosen['target_idx']]}\" -> \"{chosen['text']}\" "
        f"({chosen['confidence']:.2f}) @ {chosen['result'].get('center')} "
        f"[模式={mode} 等级={matched['match_tier']}]"
    )
    return matched


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

