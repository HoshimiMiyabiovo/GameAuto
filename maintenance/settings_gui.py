"""
settings_gui.py - 配置管理界面

功能：
  - 查看/添加/删除/排序 脚本条目（APPS）
  - 修改每个脚本的启动路径、启动参数、游戏进程名等
  - 支持多选项脚本（launcher_options）的编辑
  - 修改全局选项（qBittorrent 路径、日志目录、超时参数）
  - 所有修改实时保存到 app_config.json
"""
import copy
import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Optional

import game_auto.settings.manager as cm

FONT_TITLE  = ("微软雅黑", 13, "bold")
FONT_LABEL  = ("微软雅黑", 10)
FONT_SMALL  = ("微软雅黑", 9)
PAD         = {"padx": 8, "pady": 4}


# ──────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────

def _browse_file(entry_widget: tk.Entry) -> None:
    """弹出文件选择对话框，将选中路径填入 Entry。"""
    path = filedialog.askopenfilename(
        title="选择可执行文件",
        filetypes=[("可执行文件", "*.exe *.bat *.cmd"), ("所有文件", "*.*")],
    )
    if path:
        entry_widget.delete(0, tk.END)
        entry_widget.insert(0, path.replace("/", "\\"))


def _browse_dir(entry_widget: tk.Entry) -> None:
    """弹出目录选择对话框，将选中目录填入 Entry。"""
    path = filedialog.askdirectory(title="选择目录")
    if path:
        entry_widget.delete(0, tk.END)
        entry_widget.insert(0, path.replace("/", "\\"))


def _labeled_entry(parent, label: str, value: str = "", width: int = 45,
                   browse_file: bool = False, browse_dir: bool = False) -> tk.Entry:
    """创建带标签的输入行，可选文件/目录浏览按钮，返回 Entry。"""
    row = ttk.Frame(parent)
    row.pack(fill="x", **PAD)
    ttk.Label(row, text=label, width=18, anchor="w", font=FONT_LABEL).pack(side="left")
    entry = ttk.Entry(row, width=width, font=FONT_SMALL)
    entry.insert(0, value)
    entry.pack(side="left", padx=(0, 4))
    if browse_file:
        ttk.Button(row, text="浏览...", width=7,
                   command=lambda: _browse_file(entry)).pack(side="left")
    if browse_dir:
        ttk.Button(row, text="浏览...", width=7,
                   command=lambda: _browse_dir(entry)).pack(side="left")
    return entry


# ──────────────────────────────────────────────────────────────
# 启动参数编辑弹窗
# ──────────────────────────────────────────────────────────────

class _ParamsEditor(tk.Toplevel):
    """弹出窗口：逐行编辑启动参数列表。"""

    def __init__(self, parent, params: list, title: str = "编辑启动参数"):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.grab_set()
        self.result: Optional[list] = None

        ttk.Label(self, text="每行一个参数：", font=FONT_LABEL).pack(padx=12, pady=(10, 2), anchor="w")
        self._text = tk.Text(self, width=42, height=8, font=FONT_SMALL)
        self._text.insert("1.0", "\n".join(params))
        self._text.pack(padx=12, pady=4)

        btn_row = ttk.Frame(self)
        btn_row.pack(pady=(4, 10))
        ttk.Button(btn_row, text="确定", width=10, command=self._ok).pack(side="left", padx=6)
        ttk.Button(btn_row, text="取消", width=10, command=self.destroy).pack(side="left", padx=6)

    def _ok(self):
        raw = self._text.get("1.0", tk.END).strip()
        self.result = [line for line in raw.splitlines() if line.strip()]
        self.destroy()


# ──────────────────────────────────────────────────────────────
# 单脚本编辑对话框（单 launcher）
# ──────────────────────────────────────────────────────────────

class _SimpleLauncherEditor(tk.Toplevel):
    """
    编辑只有一个 launcher 字段的单脚本 App。
    """

    def __init__(self, parent, app_key: str, app_data: dict):
        super().__init__(parent)
        self.title(f"编辑脚本 — {app_data.get('display_name', app_key)}")
        self.resizable(True, False)
        self.grab_set()
        self.result: Optional[dict] = None

        self._data = copy.deepcopy(app_data)
        self._params: list = list(self._data.get("launcher_params", []))
        self._build_ui()

    def _build_ui(self):
        d = self._data

        ttk.Label(self, text="基本信息", font=FONT_TITLE).pack(anchor="w", padx=12, pady=(10, 2))
        sep = ttk.Separator(self, orient="horizontal")
        sep.pack(fill="x", padx=8)

        self._e_display   = _labeled_entry(self, "显示名称", d.get("display_name", ""), 38)
        self._e_script    = _labeled_entry(self, "脚本标识", d.get("script_name", ""), 38)
        self._e_launcher  = _labeled_entry(self, "启动程序路径", d.get("launcher", ""), 38, browse_file=True)
        self._e_process   = _labeled_entry(self, "游戏进程名", d.get("game", {}).get("process_name", ""), 38)

        # 启动参数行
        params_row = ttk.Frame(self)
        params_row.pack(fill="x", **PAD)
        ttk.Label(params_row, text="启动参数", width=18, anchor="w", font=FONT_LABEL).pack(side="left")
        self._params_label = ttk.Label(params_row, font=FONT_SMALL,
                                       text=self._fmt_params(), foreground="#555", width=30)
        self._params_label.pack(side="left", padx=(0, 4))
        ttk.Button(params_row, text="编辑...", width=7, command=self._edit_params).pack(side="left")

        ttk.Label(self, text="超时设置（秒）", font=FONT_TITLE).pack(anchor="w", padx=12, pady=(10, 2))
        sep2 = ttk.Separator(self, orient="horizontal")
        sep2.pack(fill="x", padx=8)

        timeouts = d.get("timeouts", {})
        self._e_t_launch  = _labeled_entry(self, "启动超时", str(timeouts.get("launch", 300)), 10)
        self._e_t_monitor = _labeled_entry(self, "监控超时", str(timeouts.get("monitor", 3600)), 10)
        self._e_init_wait = _labeled_entry(self, "初始等待", str(d.get("initial_wait", 20)), 10)

        btn_row = ttk.Frame(self)
        btn_row.pack(pady=(10, 12))
        ttk.Button(btn_row, text="保存", width=10, command=self._save).pack(side="left", padx=6)
        ttk.Button(btn_row, text="取消", width=10, command=self.destroy).pack(side="left", padx=6)

    def _fmt_params(self):
        return " ".join(self._params) if self._params else "（无）"

    def _edit_params(self):
        w = _ParamsEditor(self, self._params)
        self.wait_window(w)
        if w.result is not None:
            self._params = w.result
            self._params_label.config(text=self._fmt_params())

    def _save(self):
        d = self._data
        d["display_name"]    = self._e_display.get().strip()
        d["script_name"]     = self._e_script.get().strip()
        d["launcher"]        = self._e_launcher.get().strip()
        d["launcher_params"] = self._params
        d.setdefault("game", {})["process_name"] = self._e_process.get().strip()
        try:
            d.setdefault("timeouts", {})["launch"]  = int(self._e_t_launch.get())
            d["timeouts"]["monitor"] = int(self._e_t_monitor.get())
            d["initial_wait"]        = int(self._e_init_wait.get())
        except ValueError:
            messagebox.showerror("输入错误", "超时/等待时间必须是整数", parent=self)
            return
        self.result = d
        self.destroy()


# ──────────────────────────────────────────────────────────────
# 多选项脚本编辑对话框（launcher_options）
# ──────────────────────────────────────────────────────────────

class _OptionEditor(tk.Toplevel):
    """编辑 launcher_options 列表中的单个选项。"""

    def __init__(self, parent, opt: dict):
        super().__init__(parent)
        self.title("编辑脚本选项")
        self.resizable(True, False)
        self.grab_set()
        self.result: Optional[dict] = None
        self._data = copy.deepcopy(opt)
        self._params: list = list(opt.get("launcher_params", []))
        self._build_ui()

    def _build_ui(self):
        d = self._data
        self._e_name     = _labeled_entry(self, "选项 Key",  d.get("name", ""), 38)
        self._e_display  = _labeled_entry(self, "显示名称",  d.get("display_name", ""), 38)
        self._e_launcher = _labeled_entry(self, "启动程序路径", d.get("launcher", ""), 38, browse_file=True)

        params_row = ttk.Frame(self)
        params_row.pack(fill="x", **PAD)
        ttk.Label(params_row, text="启动参数", width=18, anchor="w", font=FONT_LABEL).pack(side="left")
        self._params_label = ttk.Label(params_row, font=FONT_SMALL,
                                       text=self._fmt_params(), foreground="#555", width=30)
        self._params_label.pack(side="left", padx=(0, 4))
        ttk.Button(params_row, text="编辑...", width=7, command=self._edit_params).pack(side="left")

        btn_row = ttk.Frame(self)
        btn_row.pack(pady=(10, 12))
        ttk.Button(btn_row, text="确定", width=10, command=self._ok).pack(side="left", padx=6)
        ttk.Button(btn_row, text="取消", width=10, command=self.destroy).pack(side="left", padx=6)

    def _fmt_params(self):
        return " ".join(self._params) if self._params else "（无）"

    def _edit_params(self):
        w = _ParamsEditor(self, self._params)
        self.wait_window(w)
        if w.result is not None:
            self._params = w.result
            self._params_label.config(text=self._fmt_params())

    def _ok(self):
        d = self._data
        d["name"]            = self._e_name.get().strip()
        d["display_name"]    = self._e_display.get().strip()
        d["launcher"]        = self._e_launcher.get().strip()
        d["launcher_params"] = self._params
        self.result = d
        self.destroy()


class _MultiLauncherEditor(tk.Toplevel):
    """
    编辑含 launcher_options 列表的多脚本 App。
    """

    def __init__(self, parent, app_key: str, app_data: dict):
        super().__init__(parent)
        self.title(f"编辑脚本 — {app_data.get('display_name', app_key)}")
        self.resizable(True, True)
        self.grab_set()
        self.result: Optional[dict] = None

        self._data = copy.deepcopy(app_data)
        self._options: list = list(self._data.get("launcher_options", []))
        self._build_ui()

    def _build_ui(self):
        d = self._data

        ttk.Label(self, text="基本信息", font=FONT_TITLE).pack(anchor="w", padx=12, pady=(10, 2))
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8)

        self._e_display = _labeled_entry(self, "显示名称",  d.get("display_name", ""), 38)
        self._e_process = _labeled_entry(self, "游戏进程名", d.get("game", {}).get("process_name", ""), 38)

        ttk.Label(self, text="脚本选项列表", font=FONT_TITLE).pack(anchor="w", padx=12, pady=(12, 2))
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8)

        list_frame = ttk.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=12, pady=6)

        scroll = ttk.Scrollbar(list_frame, orient="vertical")
        self._lb = tk.Listbox(list_frame, yscrollcommand=scroll.set,
                              height=6, font=FONT_SMALL, selectmode="single")
        scroll.config(command=self._lb.yview)
        self._lb.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

        self._refresh_listbox()

        op_frame = ttk.Frame(self)
        op_frame.pack(fill="x", padx=12, pady=4)
        ttk.Button(op_frame, text="添加选项", command=self._add_option).pack(side="left", padx=4)
        ttk.Button(op_frame, text="编辑选项", command=self._edit_option).pack(side="left", padx=4)
        ttk.Button(op_frame, text="删除选项", command=self._del_option).pack(side="left", padx=4)
        ttk.Button(op_frame, text="↑ 上移",   command=lambda: self._move(-1)).pack(side="left", padx=4)
        ttk.Button(op_frame, text="↓ 下移",   command=lambda: self._move(1)).pack(side="left", padx=4)

        ttk.Label(self, text="超时设置（秒）", font=FONT_TITLE).pack(anchor="w", padx=12, pady=(10, 2))
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8)

        timeouts = d.get("timeouts", {})
        self._e_t_launch  = _labeled_entry(self, "启动超时", str(timeouts.get("launch", 300)), 10)
        self._e_t_monitor = _labeled_entry(self, "监控超时", str(timeouts.get("monitor", 3600)), 10)
        self._e_init_wait = _labeled_entry(self, "初始等待", str(d.get("initial_wait", 20)), 10)

        btn_row = ttk.Frame(self)
        btn_row.pack(pady=(10, 12))
        ttk.Button(btn_row, text="保存", width=10, command=self._save).pack(side="left", padx=6)
        ttk.Button(btn_row, text="取消", width=10, command=self.destroy).pack(side="left", padx=6)

    def _refresh_listbox(self):
        self._lb.delete(0, tk.END)
        for opt in self._options:
            self._lb.insert(tk.END, f"  {opt.get('display_name', opt.get('name', '?'))}  [ {opt.get('launcher', '')} ]")

    def _selected_idx(self) -> Optional[int]:
        sel = self._lb.curselection()
        return sel[0] if sel else None

    def _add_option(self):
        w = _OptionEditor(self, {"name": "", "display_name": "", "type": "normal",
                                  "launcher": "", "launcher_params": []})
        self.wait_window(w)
        if w.result:
            self._options.append(w.result)
            self._refresh_listbox()

    def _edit_option(self):
        idx = self._selected_idx()
        if idx is None:
            messagebox.showinfo("提示", "请先选中一个选项", parent=self)
            return
        w = _OptionEditor(self, self._options[idx])
        self.wait_window(w)
        if w.result:
            self._options[idx] = w.result
            self._refresh_listbox()

    def _del_option(self):
        idx = self._selected_idx()
        if idx is None:
            messagebox.showinfo("提示", "请先选中一个选项", parent=self)
            return
        name = self._options[idx].get("display_name", "?")
        if messagebox.askyesno("确认删除", f"删除选项「{name}」？", parent=self):
            self._options.pop(idx)
            self._refresh_listbox()

    def _move(self, delta: int):
        idx = self._selected_idx()
        if idx is None:
            return
        new_idx = idx + delta
        if 0 <= new_idx < len(self._options):
            self._options[idx], self._options[new_idx] = self._options[new_idx], self._options[idx]
            self._refresh_listbox()
            self._lb.selection_set(new_idx)

    def _save(self):
        d = self._data
        d["display_name"]      = self._e_display.get().strip()
        d.setdefault("game", {})["process_name"] = self._e_process.get().strip()
        d["launcher_options"]  = self._options
        try:
            d.setdefault("timeouts", {})["launch"]  = int(self._e_t_launch.get())
            d["timeouts"]["monitor"] = int(self._e_t_monitor.get())
            d["initial_wait"]        = int(self._e_init_wait.get())
        except ValueError:
            messagebox.showerror("输入错误", "超时/等待时间必须是整数", parent=self)
            return
        self.result = d
        self.destroy()


# ──────────────────────────────────────────────────────────────
# 全局设置面板（qBittorrent / 日志 / 通用参数）
# ──────────────────────────────────────────────────────────────

class _GlobalSettingsPanel(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._build()

    def _build(self):
        qbt = cm.get_qbittorrent()
        log = cm.get_log_config()
        glb = cm.get_global()

        ttk.Label(self, text="qBittorrent", font=FONT_TITLE).pack(anchor="w", padx=12, pady=(12, 2))
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8)
        self._e_qbt_path    = _labeled_entry(self, "程序路径", qbt.get("path", ""), 42, browse_file=True)
        self._e_qbt_proc    = _labeled_entry(self, "进程名",   qbt.get("process_name", ""), 28)

        ttk.Label(self, text="日志设置", font=FONT_TITLE).pack(anchor="w", padx=12, pady=(14, 2))
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8)
        self._e_log_dir     = _labeled_entry(self, "日志目录", log.get("dir", ""), 42, browse_dir=True)
        self._e_log_file    = _labeled_entry(self, "日志文件名", log.get("file", ""), 28)
        self._e_log_backup  = _labeled_entry(self, "备份数量",  str(log.get("backup_count", 5)), 8)

        ttk.Label(self, text="运行参数", font=FONT_TITLE).pack(anchor="w", padx=12, pady=(14, 2))
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8)
        self._e_min_run     = _labeled_entry(self, "最短运行时间(秒)", str(glb.get("min_run_time", 300)), 10)
        self._e_interval    = _labeled_entry(self, "检测间隔(秒)",    str(glb.get("check_interval", 10)), 10)

        debug_row = ttk.Frame(self)
        debug_row.pack(fill="x", **PAD)
        ttk.Label(debug_row, text="调试模式", width=18, anchor="w", font=FONT_LABEL).pack(side="left")
        self._debug_var = tk.BooleanVar(value=bool(glb.get("debug_mode", False)))
        ttk.Checkbutton(debug_row, variable=self._debug_var, text="启用").pack(side="left")

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8, pady=10)
        ttk.Button(self, text="保存全局设置", command=self._save).pack(pady=4)

    def _save(self):
        try:
            cm.update_qbittorrent({
                "path": self._e_qbt_path.get().strip(),
                "process_name": self._e_qbt_proc.get().strip(),
            })
            cm.update_log_config({
                "dir":          self._e_log_dir.get().strip(),
                "file":         self._e_log_file.get().strip(),
                "backup_count": int(self._e_log_backup.get()),
            })
            cm.update_global({
                "min_run_time":   int(self._e_min_run.get()),
                "check_interval": int(self._e_interval.get()),
                "debug_mode":     self._debug_var.get(),
            })
            messagebox.showinfo("成功", "全局设置已保存", parent=self)
        except ValueError:
            messagebox.showerror("输入错误", "数字字段请填入整数", parent=self)
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=self)


# ──────────────────────────────────────────────────────────────
# 脚本列表面板
# ──────────────────────────────────────────────────────────────

class _AppsListPanel(ttk.Frame):
    """脚本列表主面板，含增删改排序。"""

    def __init__(self, parent):
        super().__init__(parent)
        self._build()

    def _build(self):
        # 工具栏
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=8, pady=(10, 4))
        ttk.Button(toolbar, text="+ 新增脚本", command=self._add_app).pack(side="left", padx=4)
        ttk.Button(toolbar, text="✎ 编辑选中", command=self._edit_app).pack(side="left", padx=4)
        ttk.Button(toolbar, text="✕ 删除选中", command=self._del_app).pack(side="left", padx=4)
        ttk.Button(toolbar, text="↑ 上移",     command=lambda: self._move(-1)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="↓ 下移",     command=lambda: self._move(1)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="↺ 刷新",     command=self._refresh).pack(side="right", padx=4)

        # Treeview 列表
        columns = ("key", "display", "launcher_type", "launcher", "process")
        self._tree = ttk.Treeview(self, columns=columns, show="headings", height=14, selectmode="browse")
        for col, text, width in [
            ("key",          "Key",      120),
            ("display",      "显示名称", 120),
            ("launcher_type","类型",      60),
            ("launcher",     "启动路径", 320),
            ("process",      "进程名",   130),
        ]:
            self._tree.heading(col, text=text)
            self._tree.column(col, width=width, minwidth=40, anchor="w")

        sb_y = ttk.Scrollbar(self, orient="vertical",   command=self._tree.yview)
        sb_x = ttk.Scrollbar(self, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        self._tree.pack(side="top", fill="both", expand=True, padx=8)
        sb_y.pack(side="right", fill="y")
        sb_x.pack(side="bottom", fill="x")
        self._tree.bind("<Double-1>", lambda e: self._edit_app())

        info = ttk.Label(self, text="双击表格行可快速编辑", font=FONT_SMALL, foreground="#888")
        info.pack(anchor="e", padx=12, pady=2)

        self._refresh()

    def _refresh(self):
        for row in self._tree.get_children():
            self._tree.delete(row)
        order = cm.get_app_order()
        apps  = cm.get_apps()
        # 先显示 order 里的，再显示 order 里没有的
        shown = set()
        for key in order:
            if key in apps:
                self._insert_row(key, apps[key])
                shown.add(key)
        for key, data in apps.items():
            if key not in shown:
                self._insert_row(key, data)

    def _insert_row(self, key: str, data: dict):
        if "launcher_options" in data:
            launcher_type = "多选项"
            launcher_text = f"({len(data['launcher_options'])} 个选项)"
        else:
            launcher_type = "单路径"
            launcher_text = data.get("launcher", "")
        process = data.get("game", {}).get("process_name", "—")
        display = data.get("display_name", key)
        self._tree.insert("", tk.END, iid=key,
                          values=(key, display, launcher_type, launcher_text, process))

    def _selected_key(self) -> Optional[str]:
        sel = self._tree.selection()
        return sel[0] if sel else None

    def _current_order(self) -> list:
        return list(self._tree.get_children())

    def _add_app(self):
        key = simpledialog.askstring("新增脚本", "请输入脚本 Key（英文，不含空格）：\n例如：MyNewGame", parent=self)
        if not key:
            return
        key = key.strip().replace(" ", "_")
        if key in cm.get_apps():
            messagebox.showerror("错误", f"Key「{key}」已存在", parent=self)
            return
        new_data = {
            "display_name":    key,
            "script_name":     key,
            "type":            "script",
            "launcher":        "",
            "launcher_params": [],
            "game":            {"process_name": ""},
            "timeouts":        {"launch": 300, "monitor": 3600},
            "initial_wait":    20,
        }
        # 直接弹出编辑对话框
        w = _SimpleLauncherEditor(self, key, new_data)
        self.wait_window(w)
        if w.result:
            cm.upsert_app(key, w.result)
            order = cm.get_app_order()
            if key not in order:
                order.append(key)
                cm.update_app_order(order)
            self._refresh()

    def _edit_app(self):
        key = self._selected_key()
        if not key:
            messagebox.showinfo("提示", "请先选中一行", parent=self)
            return
        data = cm.get_app(key)
        if data is None:
            return

        if "launcher_options" in data:
            w = _MultiLauncherEditor(self, key, data)
        else:
            w = _SimpleLauncherEditor(self, key, data)

        self.wait_window(w)
        if w.result:
            cm.upsert_app(key, w.result)
            self._refresh()
            messagebox.showinfo("成功", f"「{data.get('display_name', key)}」已保存", parent=self)

    def _del_app(self):
        key = self._selected_key()
        if not key:
            messagebox.showinfo("提示", "请先选中一行", parent=self)
            return
        display = cm.get_app(key).get("display_name", key)
        if messagebox.askyesno("确认删除", f"删除脚本「{display}」（{key}）？\n此操作不可撤销。", parent=self):
            cm.remove_app(key)
            self._refresh()

    def _move(self, delta: int):
        key = self._selected_key()
        if not key:
            return
        order = self._current_order()
        idx = order.index(key)
        new_idx = idx + delta
        if 0 <= new_idx < len(order):
            order[idx], order[new_idx] = order[new_idx], order[idx]
            cm.update_app_order(order)
            self._refresh()
            self._tree.selection_set(key)
            self._tree.focus(key)


# ──────────────────────────────────────────────────────────────
# 主窗口
# ──────────────────────────────────────────────────────────────

class SettingsWindow(tk.Toplevel):
    """
    配置管理主窗口（Toplevel，可内嵌到 gui_launcher 中）。
    也可独立运行。
    """

    def __init__(self, parent=None):
        if parent:
            super().__init__(parent)
        else:
            # 独立模式：创建根窗口
            self._root = tk.Tk()
            super().__init__(self._root)

        self.title("设置 — 游戏自动化助手")
        self.geometry("900x620")
        self.resizable(True, True)
        self._build_ui()

    # 独立运行时的 mainloop
    def run(self):
        if hasattr(self, "_root"):
            self._root.withdraw()
            self.mainloop()

    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        # Tab 1：脚本管理
        apps_frame = _AppsListPanel(nb)
        nb.add(apps_frame, text="  脚本管理  ")

        # Tab 2：全局设置
        global_canvas = tk.Canvas(nb, highlightthickness=0)
        global_scroll = ttk.Scrollbar(nb, orient="vertical", command=global_canvas.yview)
        global_frame = ttk.Frame(global_canvas)
        global_frame.bind("<Configure>",
                          lambda e: global_canvas.configure(scrollregion=global_canvas.bbox("all")))
        global_canvas.create_window((0, 0), window=global_frame, anchor="nw")
        global_canvas.configure(yscrollcommand=global_scroll.set)

        panel_wrapper = ttk.Frame(nb)
        global_canvas.pack(in_=panel_wrapper, side="left", fill="both", expand=True)
        global_scroll.pack(in_=panel_wrapper, side="right", fill="y")

        _GlobalSettingsPanel(global_frame).pack(fill="both", expand=True)
        nb.add(panel_wrapper, text="  全局设置  ")

        # 底栏
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(bottom, text="所有修改自动保存到 app_config.json", font=FONT_SMALL,
                  foreground="#666").pack(side="left")
        ttk.Button(bottom, text="关闭", command=self.destroy).pack(side="right")


# ──────────────────────────────────────────────────────────────
# 独立入口
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()
    win = SettingsWindow(root)
    win.protocol("WM_DELETE_WINDOW", root.quit)
    root.mainloop()
