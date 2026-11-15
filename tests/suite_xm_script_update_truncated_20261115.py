"""XM/CJ 换血抢跑截断回归（2026-11-15）。

背景：XM 脚本把新版 exe 下载到同目录（同名基础名 + 更高版本号），下载还没写完
文件名就已可见。换血逻辑（_find_newer_script_exe）只比版本号就认定"新版已落地"，
立刻杀掉正在下载的旧进程 → 下载中断 → 新版 exe 被截断（实测 PE 声明 23,666,688
字节、实际 16,660,480，缺 7.0MB）→ 启动报 WinError 193「不是有效的 Win32 应用程序」，
整轮失败（#357，08:08）。

断言：
  1. _pe_file_complete 认得真 PE（sys.executable）与自造完整 PE
  2. _pe_file_complete 拒绝：节数据越界（截断）/ 非 PE / 空文件 / 头被砍
  3. _find_newer_script_exe 跳过未下载完成的候选（版本比较与 mtime 兜底两条路径）
  4. 截断候选存在时返回空串，不把半成品当新版本

跑法：.venv\\Scripts\\python.exe -m pytest tests/suite_xm_script_update_truncated_20261115.py -q
"""

import os
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from game_auto.runtime import foreground_flow_runner as ffr  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} {detail}", flush=True)
        FAILURES.append(name)


def write_minimal_pe(path, size=0x400, raw_size=0x200, raw_ptr=0x200):
    """写一个最小自洽 PE（单节，节表 RawPtr+RawSize 不越界），用作换血测试夹具。"""
    buf = bytearray(size)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x80)          # e_lfanew
    buf[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", buf, 0x84, 0x014C, 1, 0, 0, 0, 224, 0x0102)
    struct.pack_into("<H", buf, 0x98, 0x010B)        # PE32
    struct.pack_into("<I", buf, 0x98 + 56, 0x2000)   # SizeOfImage
    sec = 0x98 + 224
    buf[sec:sec + 8] = b".text\x00\x00\x00"
    struct.pack_into("<IIII", buf, sec + 8, 0x1000, 0x1000, raw_size, raw_ptr)
    Path(path).write_bytes(bytes(buf))


def test_pe_file_complete():
    print("[1] _pe_file_complete：真 PE 通过，残缺/非 PE 拒绝")
    check("真 PE（sys.executable）完整", ffr._pe_file_complete(sys.executable))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ok = root / "ok.exe"
        write_minimal_pe(ok)
        check("自造完整 PE → True", ffr._pe_file_complete(str(ok)))

        truncated = root / "truncated.exe"
        write_minimal_pe(truncated, size=0x300)       # 节数据声明到 0x400
        check("节数据越界（下载截断）→ False", not ffr._pe_file_complete(str(truncated)),
              "PE 声明 0x400，文件只有 0x300")

        notpe = root / "notpe.exe"
        notpe.write_bytes(b"XM Seer\xa0\xd9" * 64)
        check("非 PE 内容 → False", not ffr._pe_file_complete(str(notpe)))

        empty = root / "empty.exe"
        empty.write_bytes(b"")
        check("空文件 → False", not ffr._pe_file_complete(str(empty)))

        short = root / "short.exe"
        short.write_bytes(b"MZ" + b"\x00" * 20)       # 连 e_lfanew 都读不全
        check("头被砍（<0x40）→ False", not ffr._pe_file_complete(str(short)))

        badptr = root / "badptr.exe"
        write_minimal_pe(badptr)
        data = bytearray(badptr.read_bytes())
        struct.pack_into("<I", data, 0x18C, 0x100000)  # PointerToRawData 指向文件外
        badptr.write_bytes(bytes(data))
        check("PointerToRawData 越界 → False", not ffr._pe_file_complete(str(badptr)))

        missing = root / "missing.exe"
        check("文件不存在 → False", not ffr._pe_file_complete(str(missing)))


def test_find_newer_skips_incomplete():
    print("[2] _find_newer_script_exe：半成品不得被当成新版本")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        now = time.time()
        old_exe = root / "XM Seer脱机日常 ver 2.6.6.exe"
        new_bad = root / "XM Seer脱机日常 ver 2.6.61.exe"   # 版本更高但没下完
        other = root / "HoYo.Gacha.exe"
        write_minimal_pe(old_exe)
        write_minimal_pe(new_bad, size=0x300)
        other.write_bytes(b"")
        os.utime(old_exe, (now - 86400, now - 86400))
        os.utime(new_bad, (now - 5, now - 5))

        check("高版本但截断 → 返回空串（不抢跑）",
              ffr._find_newer_script_exe(str(old_exe)) == "",
              ffr._find_newer_script_exe(str(old_exe)))

        # 下载完成后（补全同一文件）应当立刻可认
        write_minimal_pe(new_bad)
        check("同一文件下载补全后 → 认得新版本",
              ffr._find_newer_script_exe(str(old_exe)).endswith("ver 2.6.61.exe"),
              ffr._find_newer_script_exe(str(old_exe)))

        # mtime 兜底路径（exe 名无版本）同样要过 PE 校验
        anon_old = root / "SomeTool.exe"
        anon_bad = root / "SomeTool ver 9.exe"
        write_minimal_pe(anon_old)
        write_minimal_pe(anon_bad, size=0x300)
        os.utime(anon_old, (now - 86400, now - 86400))
        os.utime(anon_bad, (now - 5, now - 5))
        check("mtime 兜底 + 截断候选 → 空串",
              ffr._find_newer_script_exe(str(anon_old), since_ts=now - 60) == "",
              ffr._find_newer_script_exe(str(anon_old), since_ts=now - 60))
        write_minimal_pe(anon_bad)
        check("mtime 兜底 + 补全 → 找到",
              ffr._find_newer_script_exe(str(anon_old), since_ts=now - 60).endswith("SomeTool ver 9.exe"))


def test_real_truncated_file():
    print("[3] 实测残留：XM 2.6.61 截断文件必须判为不完整")
    real = Path(r"D:\Program Files\新建文件夹\XM Seer脱机日常 ver 2.6.61.exe")
    if not real.exists():
        print("  SKIP 本机已无该残留文件")
        return
    check("实测截断文件 → False", not ffr._pe_file_complete(str(real)))
    check("同目录旧版 2.6.6 → True", ffr._pe_file_complete(
        str(real.with_name("XM Seer脱机日常 ver 2.6.6.exe"))))


def main():
    test_pe_file_complete()
    test_find_newer_skips_incomplete()
    test_real_truncated_file()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
