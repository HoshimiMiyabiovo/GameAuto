"""UIPI 护栏单测（2026-10-20）。

打桩 _process_is_elevated 验证 _uipi_block_reason 的四种权限组合，
并验证护栏只拦「本程序普通 + 目标管理员」这一种真会被拦的组合。
"""
import sys

sys.path.insert(0, "E:/GithubProject")

from game_auto.runtime import foreground_flow_runner as ffr


def main():
    ok = True

    def check(label, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), label)
        ok = ok and cond

    orig = ffr._process_is_elevated
    try:
        # 本进程权限固定 monkeypatch，目标进程权限逐案设置
        def make_case(self_elev):
            state = {"target": None}

            def fake(pid):
                return self_elev if pid == 0 else state["target"]

            ffr._process_is_elevated = fake
            ffr.os.getpid = lambda: 0
            return state

        # 1) 普通 → 管理员：必须拦
        st = make_case(False)
        st["target"] = True
        r = ffr._uipi_block_reason(1234)
        check("普通→管理员 被拦", bool(r) and "UIPI" in r and "管理员" in r)

        # 2) 管理员 → 管理员：放行
        st = make_case(True)
        st["target"] = True
        check("管理员→管理员 放行", ffr._uipi_block_reason(1234) == "")

        # 3) 普通 → 普通：放行
        st = make_case(False)
        st["target"] = False
        check("普通→普通 放行", ffr._uipi_block_reason(1234) == "")

        # 4) 管理员 → 普通：放行（高完整性可向低完整性发输入）
        st = make_case(True)
        st["target"] = False
        check("管理员→普通 放行", ffr._uipi_block_reason(1234) == "")

        # 5) 读不到目标权限（None）：放行不误伤
        st = make_case(False)
        st["target"] = None
        check("目标权限未知 放行", ffr._uipi_block_reason(1234) == "")

        # 6) 坏 PID：不炸
        check("pid=0 不炸", ffr._uipi_block_reason(0) == "")
        check("pid=-1 不炸", ffr._uipi_block_reason(-1) == "")

        # 7) 真实自检：当前进程（非提权）对自己不应被拦
        ffr._process_is_elevated = orig
        check("真实调用不抛异常", isinstance(ffr._uipi_block_reason(0), str))
    finally:
        ffr._process_is_elevated = orig

    print("\nALL PASS" if ok else "\nHAS FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
