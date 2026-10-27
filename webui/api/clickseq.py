from __future__ import annotations

from fastapi import APIRouter
from game_auto.settings.paths import PROTECTED_CLICK_SEQUENCES
from game_auto.webui.state import logger

router = APIRouter(prefix="/api/click-sequences", tags=["clickseq"])

@router.get("")
def list_seq():
    from game_auto.runtime.click_sequence import ClickSequencePlayer
    player = ClickSequencePlayer(logger=logger)
    items=[]
    for n in player.list_sequences():
        try:
            seq = player.load(n)
            items.append({"name": n, "steps": len(seq.events), "created_at": seq.created_at or "", "note": seq.note or "", "window": seq.window_title_keyword or "", "protected": n in PROTECTED_CLICK_SEQUENCES})
        except Exception:
            items.append({"name": n, "steps": 0, "created_at": "", "note": "", "window": "", "protected": n in PROTECTED_CLICK_SEQUENCES})
    # 补充缺失的受保护占位
    existing = {it["name"] for it in items}
    for name in PROTECTED_CLICK_SEQUENCES:
        if name not in existing:
            items.append({"name": name, "steps": 0, "created_at": "", "note": "内置序列缺失，可恢复", "window": "", "protected": True, "missing": True})
    items.sort(key=lambda it: (not it.get("protected", False), it.get("name","")))
    return {"sequences": items}

@router.post("/{name}/play")
def play(name: str, payload: dict = None):
    payload = payload or {}
    loop = int(payload.get("loop", 1))
    dry = bool(payload.get("dry", False))
    from game_auto.runtime.click_sequence import ClickSequencePlayer
    player = ClickSequencePlayer(logger=logger)
    try:
        seq = player.load(name)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    # 同步回放会阻塞请求，改 daemon 线程
    import threading
    def _bg():
        try:
            player.play(seq, loop=loop, dry_run=dry)
        except Exception as exc:
            logger.exception("[WebUI] 回放失败 %s", name)
    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "started": name, "loop": loop, "dry": dry}

@router.delete("/{name}")
def delete_seq(name: str):
    if name in PROTECTED_CLICK_SEQUENCES:
        return {"ok": False, "error": f"「{name}」为一条龙内置序列，已做防误删保护，禁止删除"}
    import os
    from game_auto.settings.paths import CLICK_SEQUENCES_DIR
    p = CLICK_SEQUENCES_DIR / f"{name}.json"
    if not p.exists():
        return {"ok": False, "error": "序列不存在"}
    try:
        p.unlink()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

@router.post("/restore-protected")
def restore():
    import shutil
    from game_auto.settings.paths import CLICK_SEQUENCES_DIR, PROTECTED_BACKUP_DIR
    restored=[]
    missing=[]
    for name in PROTECTED_CLICK_SEQUENCES:
        src = PROTECTED_BACKUP_DIR / f"{name}.json"
        dst = CLICK_SEQUENCES_DIR / f"{name}.json"
        if dst.exists():
            continue
        if src.exists():
            try:
                shutil.copy2(str(src), str(dst))
                restored.append(name)
            except Exception as e:
                missing.append(name)
        else:
            missing.append(name)
    return {"ok": True, "restored": restored, "missing": missing}
