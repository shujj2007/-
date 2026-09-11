#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""照片按拍摄时间重命名工具 —— 图形界面版 (Tkinter)。

运行方式:
    python photo_renamer_gui.py

功能:
    - 选择照片目录（可递归处理子目录）
    - 自定义命名格式，内置常用预设
    - 支持按日期自动归档（格式中含 \\ 或 / 时自动创建子目录）
    - 预览改名结果，确认后再执行
    - 复制模式（保留原文件）/ 重命名模式
    - 无 EXIF 的照片可跳过，或回退到文件修改时间

依赖: 与命令行版相同，pip install Pillow exifread
"""

__version__ = "1.4.5"

import os
import queue
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import photo_renamer as core

# 常用格式预设：(格式串, 下拉框显示文字)
PRESETS = [
    ("%Y%m%d_%H%M%S", "20240101_123456"),
    ("%Y-%m-%d_%H%M%S", "2024-01-01_123456"),
    ("IMG_%Y%m%d_%H%M%S", "IMG_20240101_123456"),
    ("%Y%m%d_%H%M%S_{orig}", "20240101_123456_原名"),
    ("%Y\\%m\\%d_%H%M%S", "2024\\01\\01_123456  → 按日期归档"),
    ("%Y-%m-%d", "2024-01-01  → 仅日期"),
    ("%Y年%m月%d日{weekfull}-%p%I点%M分%S秒", "2020年07月25日星期六-晚上09点41分47秒"),
]

# 星期显示选项：(显示文字, 对应的格式占位符)
WEEK_CHOICES = [
    ("不显示", ""),
    ("周一（{week}）", "{week}"),
    ("星期一（{weekfull}）", "{weekfull}"),
    ("Mon（%a）", "%a"),
    ("Monday（%A）", "%A"),
]

# 小时制选项：(显示文字, 目标格式说明)
HOUR_CHOICES = [
    "24 小时制（%H）",
    "12 小时制（%I）",
    "12 小时制 + 时段词（%p%I）",
]


class PhotoRenamerGUI:
    """照片重命名工具主窗口。"""

    def __init__(self, root):
        self.root = root
        self.plan = []        # [(源文件, 目标文件), ...]
        self.skipped = []     # [(源文件, 原因), ...]
        self.files = []
        self._busy = False
        self._thread = None
        self._folder = None
        self.q = queue.Queue()
        self._setup_fonts()
        self._build_ui()
        self.root.after(80, self._poll_queue)
        lib = []
        if core.HAVE_PIL:
            lib.append("Pillow")
        if core.HAVE_EXIFREAD:
            lib.append("exifread")
        if lib:
            self.status_var.set(f"就绪（EXIF 解析库: {'、'.join(lib)}）。请选择照片目录并点击「① 预览」")
        else:
            self.status_var.set("警告: 未检测到 Pillow/exifread，将全部回退到文件修改时间。可执行: pip install Pillow exifread")

    # ------------------------------------------------------------------ UI

    def _setup_fonts(self):
        try:
            from tkinter import font as tkfont
            for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
                f = tkfont.nametofont(name)
                f.configure(family="Microsoft YaHei UI", size=10)
        except Exception:
            pass
        try:
            style = ttk.Style(self.root)
            style.configure("Treeview", font=("Microsoft YaHei UI", 9), rowheight=24)
            style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        except Exception:
            pass

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ---- 目录选择 ----
        f_top = ttk.LabelFrame(self.root, text="照片目录")
        f_top.pack(fill="x", **pad)
        self.folder_var = tk.StringVar()
        ttk.Entry(f_top, textvariable=self.folder_var).pack(
            side="left", fill="x", expand=True, padx=(8, 4), pady=8)
        ttk.Button(f_top, text="浏览…", command=self._browse).pack(side="left", padx=(0, 8))

        # ---- 命名格式 ----
        f_fmt = ttk.LabelFrame(self.root, text="命名格式（%Y年 %m月 %d日 %H/%I时 %M分 %S秒，{orig}=原名，{ext}=扩展名，{week}=星期）")
        f_fmt.pack(fill="x", **pad)
        self.fmt_var = tk.StringVar(value="%Y%m%d_%H%M%S")
        ttk.Entry(f_fmt, textvariable=self.fmt_var).pack(
            side="left", fill="x", expand=True, padx=(8, 4), pady=8)
        ttk.Label(f_fmt, text="预设:").pack(side="left", padx=(4, 0))
        self.preset_var = tk.StringVar()
        cb = ttk.Combobox(f_fmt, textvariable=self.preset_var, state="readonly", width=30)
        cb["values"] = [d for _, d in PRESETS]
        cb.pack(side="left", padx=(0, 8))
        cb.bind("<<ComboboxSelected>>", self._on_preset)

        # 星期 / 小时制快捷选择（自动改写格式串）
        f_fmt2 = ttk.Frame(f_fmt)
        f_fmt2.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(f_fmt2, text="星期显示:").pack(side="left")
        self.week_var = tk.StringVar(value=WEEK_CHOICES[0][0])
        wcb = ttk.Combobox(f_fmt2, textvariable=self.week_var, state="readonly", width=20)
        wcb["values"] = [d for d, _ in WEEK_CHOICES]
        wcb.pack(side="left", padx=(4, 16))
        wcb.bind("<<ComboboxSelected>>", self._on_week)
        ttk.Label(f_fmt2, text="小时制:").pack(side="left")
        self.hour_var = tk.StringVar(value=HOUR_CHOICES[0])
        hcb = ttk.Combobox(f_fmt2, textvariable=self.hour_var, state="readonly", width=28)
        hcb["values"] = HOUR_CHOICES
        hcb.pack(side="left", padx=(4, 8))
        hcb.bind("<<ComboboxSelected>>", self._on_hour)

        # ---- 选项 ----
        f_opt = ttk.LabelFrame(self.root, text="选项")
        f_opt.pack(fill="x", **pad)
        self.recursive_var = tk.BooleanVar(value=True)
        self.nofb_var = tk.BooleanVar(value=False)
        self.copy_var = tk.BooleanVar(value=False)
        self.name_fb_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f_opt, text="递归处理子目录",
                        variable=self.recursive_var).pack(side="left", padx=8, pady=6)
        ttk.Checkbutton(f_opt, text="无 EXIF 时跳过（否则用文件修改时间）",
                        variable=self.nofb_var).pack(side="left", padx=8)
        ttk.Checkbutton(f_opt, text="复制模式（保留原文件）",
                        variable=self.copy_var,
                        command=self._update_action_label).pack(side="left", padx=8)
        ttk.Checkbutton(f_opt, text="无 EXIF 时从文件名识别时间",
                        variable=self.name_fb_var).pack(side="left", padx=8)

        # 第二行：扩展名 / 并行线程
        f_opt2 = ttk.Frame(f_opt)
        f_opt2.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(f_opt2, text="扩展名:").pack(side="left")
        self.ext_var = tk.StringVar()
        ttk.Entry(f_opt2, textvariable=self.ext_var, width=32).pack(
            side="left", padx=(4, 8))
        ttk.Label(f_opt2, text="并行线程:").pack(side="left", padx=(12, 0))
        self.jobs_var = tk.StringVar(value="1")
        ttk.Spinbox(f_opt2, from_=1, to=16, textvariable=self.jobs_var,
                    width=3).pack(side="left", padx=(4, 8))
        ttk.Label(f_opt2, text="(网络盘/机械硬盘可调大)", foreground="#777777"
                  ).pack(side="left", padx=(0, 8))

        # ---- 操作按钮 ----
        f_btn = ttk.Frame(self.root)
        f_btn.pack(fill="x", **pad)
        self.btn_preview = ttk.Button(f_btn, text="① 预览", command=self.preview)
        self.btn_preview.pack(side="left", padx=(8, 8))
        self.btn_apply = ttk.Button(f_btn, text="② 执行重命名",
                                    command=self.apply, state="disabled")
        self.btn_apply.pack(side="left", padx=(0, 8))
        ttk.Button(f_btn, text="清空列表", command=self.clear_list).pack(side="left", padx=(0, 8))
        ttk.Button(f_btn, text="打开目录", command=self._open_folder).pack(side="right", padx=8)

        # ---- 结果列表 ----
        f_list = ttk.LabelFrame(
            self.root, text="预览结果（蓝=识别到时间待改名，绿=名称相同跳过，红=未识别到时间；双击行打开所在文件夹）")
        f_list.pack(fill="both", expand=True, **pad)
        cols = ("orig", "new", "status")
        self.tree = ttk.Treeview(f_list, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("orig", text="原文件")
        self.tree.heading("new", text="新名称")
        self.tree.heading("status", text="状态")
        self.tree.column("orig", width=330, anchor="w")
        self.tree.column("new", width=330, anchor="w")
        self.tree.column("status", width=200, anchor="w")
        vsb = ttk.Scrollbar(f_list, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("plan", foreground="#1565C0")        # 蓝：已识别时间、待改名
        self.tree.tag_configure("skip_same", foreground="#2E7D32")   # 绿：新名称与原名称相同
        self.tree.tag_configure("skip_noexif", foreground="#C62828") # 红：无 EXIF 拍摄时间
        self.tree.tag_configure("skip_other", foreground="#8a8a8a")  # 灰：其他原因
        self.tree.tag_configure("ok", foreground="#2E7D32")          # 绿：已完成
        self.tree.tag_configure("fail", foreground="#C62828")        # 红：失败
        self.tree.bind("<Double-1>", self._on_double_click)

        # ---- 状态栏 ----
        f_status = ttk.Frame(self.root)
        f_status.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="就绪。")
        ttk.Label(f_status, textvariable=self.status_var).pack(side="left", padx=(8, 8))
        self.pct_var = tk.StringVar(value="")
        ttk.Label(f_status, textvariable=self.pct_var, width=5, anchor="e",
                  font=("Microsoft YaHei UI", 9, "bold")).pack(side="right", padx=(0, 2))
        self.progress = ttk.Progressbar(f_status, mode="determinate", length=180)
        self.progress.pack(side="right", padx=8)

    # ------------------------------------------------------------- 回调

    def _browse(self):
        d = filedialog.askdirectory(title="选择照片目录")
        if d:
            self.folder_var.set(d)

    def _on_preset(self, _event=None):
        desc = self.preset_var.get()
        for fmt, d in PRESETS:
            if d == desc:
                self.fmt_var.set(fmt)
                break

    # 星期/小时制下拉框：把选择翻译成格式串中的占位符
    _WEEK_TOKENS = ("{week}", "{weekfull}", "%a", "%A")

    def _on_week(self, _event=None):
        desc = self.week_var.get()
        token = next((t for d, t in WEEK_CHOICES if d == desc), "")
        fmt = self.fmt_var.get()
        if token:
            present = [o for o in self._WEEK_TOKENS if o in fmt]
            if present:
                new = fmt.replace(present[0], token)
            elif token in fmt:
                new = fmt
            else:
                new = fmt.rstrip("_") + "_" + token
        else:  # 不显示：移除所有星期占位符
            new = fmt
            for o in self._WEEK_TOKENS:
                new = new.replace(o, "")
            new = new.rstrip("_")
        self.fmt_var.set(new)

    def _on_hour(self, _event=None):
        desc = self.hour_var.get()
        fmt = self.fmt_var.get()
        if desc.startswith("24"):
            new = fmt.replace("%I", "%H").replace("%p", "")
        elif desc.startswith("12 小时制 +"):  # 12 小时制 + 时段词（凌晨/上午/中午/下午/晚上，在小时前）
            new = fmt.replace("%H", "%I")
            if "%p" not in new:
                new = new.replace("%I", "%p%I")
        else:  # 12 小时制（不带上午/下午）
            new = fmt.replace("%H", "%I").replace("%p", "")
        self.fmt_var.set(new)

    def _update_action_label(self):
        if not self._busy:
            self.btn_apply.config(
                text="② 执行复制" if self.copy_var.get() else "② 执行重命名")

    def _open_folder(self):
        folder = self.folder_var.get().strip()
        if folder and os.path.isdir(folder):
            try:
                os.startfile(folder)  # Windows
                return
            except Exception:
                pass
        messagebox.showinfo("提示", "请先选择有效的照片目录")

    def _on_double_click(self, _event):
        item = self.tree.focus()
        if not item:
            return
        path = None
        if item.startswith("p") and item[1:].isdigit():
            _, dst = self.plan[int(item[1:])]
            path = os.path.dirname(dst)
        elif item.startswith("s") and item[1:].isdigit():
            src, _ = self.skipped[int(item[1:])]
            path = os.path.dirname(src)
        if path and os.path.isdir(path):
            try:
                os.startfile(path)
            except Exception:
                pass

    # ------------------------------------------------------- 工具方法

    def _exts(self):
        raw = self.ext_var.get().strip()
        if not raw:
            return set(core.DEFAULT_EXTS)
        return {e.lower().lstrip(".") for e in raw.split()}

    def _set_busy(self, busy):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_preview.config(state=state)
        self.btn_apply.config(state=state)
        self.btn_apply.config(text="② 执行复制" if self.copy_var.get() else "② 执行重命名")

    def clear_list(self):
        self.tree.delete(*self.tree.get_children())
        self.plan, self.skipped, self.files = [], [], []
        self.progress["value"] = 0
        self.pct_var.set("")
        self.status_var.set("列表已清空。")

    def _rel(self, path):
        try:
            return os.path.relpath(path, self._folder or os.getcwd())
        except ValueError:
            return path

    def _populate(self, plan, skipped, extracted=frozenset()):
        self.plan, self.skipped = plan, skipped
        self.tree.delete(*self.tree.get_children())
        act = "复制" if self.copy_var.get() else "重命名"
        for i, (src, dst) in enumerate(plan):
            status = f"待{act}（名称识别）" if src in extracted else f"待{act}"
            self.tree.insert("", "end", iid=f"p{i}",
                             values=(self._rel(src), self._rel(dst), status),
                             tags=("plan",))
        for i, (src, reason) in enumerate(skipped):
            if reason.startswith("新名称与原名称相同"):
                tag = "skip_same"
            elif "无 EXIF" in reason:
                tag = "skip_noexif"
            else:
                tag = "skip_other"
            self.tree.insert("", "end", iid=f"s{i}",
                             values=(self._rel(src), "", f"跳过: {reason}"),
                             tags=(tag,))

    # ------------------------------------------------------- 预览

    def preview(self):
        if self._busy:
            return
        folder = self.folder_var.get().strip()
        if not folder:
            messagebox.showwarning("提示", "请先选择照片目录")
            return
        if not os.path.isdir(folder):
            messagebox.showwarning("提示", f"目录不存在:\n{folder}")
            return
        fmt = self.fmt_var.get().strip()
        if not fmt:
            messagebox.showwarning("提示", "命名格式不能为空")
            return
        # 主线程捕获全部参数后传给工作线程，避免跨线程访问 Tk 变量
        self._folder = folder
        rec = self.recursive_var.get()
        nofb = self.nofb_var.get()
        name_fb = self.name_fb_var.get()
        exts = self._exts()
        try:
            jobs = max(1, int(self.jobs_var.get()))
        except ValueError:
            jobs = 1
        self._p_fmt = fmt
        self._p_nofb = nofb
        self._set_busy(True)
        self.status_var.set("正在扫描并读取拍摄时间…")
        self.progress["mode"] = "indeterminate"
        self.progress.start(12)
        self._thread = threading.Thread(
            target=self._preview_worker,
            args=(folder, rec, exts, jobs, name_fb), daemon=True)
        self._thread.start()

    def _preview_worker(self, folder, rec, exts, jobs, name_fb):
        try:
            files = core.collect_files(folder, rec, exts)
            total = len(files)
            self.q.put(("preview_begin", total))

            def cb(done, total_):
                self.q.put(("preview_progress", done, total_))

            times = core.read_times(files, jobs=jobs, progress=cb)
            self.q.put(("times_ready", files, times, name_fb))
        except Exception as exc:  # noqa: BLE001 - 界面层统一兜底
            self.q.put(("error", str(exc)))

    def _ask_name_times(self, ordered):
        """弹出分组确认对话框，返回 {掩码: 确认时间}（可能为空）。"""
        if not ordered:
            return {}
        dlg = NameTimeDialog(self.root, ordered)
        self.root.wait_window(dlg)
        return dlg.confirmed

    # ------------------------------------------------------- 执行

    def apply(self):
        if self._busy:
            return
        if not self.plan:
            messagebox.showinfo("提示", "请先点击「① 预览」生成改名计划")
            return
        act = "复制" if self.copy_var.get() else "重命名"
        if not messagebox.askyesno("确认执行", f"将对 {len(self.plan)} 个文件执行{act}，\n是否继续？"):
            return
        copy_mode = self.copy_var.get()
        self._set_busy(True)
        self.progress["mode"] = "determinate"
        self.progress["maximum"] = len(self.plan)
        self.progress["value"] = 0
        self.pct_var.set("0%")
        self.status_var.set(f"正在{act}…")
        self._thread = threading.Thread(target=self._apply_worker, args=(copy_mode,), daemon=True)
        self._thread.start()

    def _apply_worker(self, copy_mode):
        ok = fail = 0
        n = len(self.plan)
        namer = core.UniqueNamer()
        for idx, (src, dst) in enumerate(self.plan):
            final = namer.unique(dst)
            try:
                d = os.path.dirname(final)
                if d:
                    os.makedirs(d, exist_ok=True)
                if copy_mode:
                    shutil.copy2(src, final)
                else:
                    os.rename(src, final)
                ok += 1
                self.q.put(("row_ok", idx, self._rel(final)))
            except OSError as exc:
                fail += 1
                self.q.put(("row_fail", idx, str(exc)))
            self.q.put(("progress", idx + 1, n))
        self.q.put(("apply_done", ok, fail))

    # ------------------------------------------------------- 消息循环

    def _poll_queue(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "preview_begin":
            _, total = msg
            self.progress.stop()
            self.progress["mode"] = "determinate"
            self.progress["maximum"] = max(total, 1)
            self.progress["value"] = 0
            self.pct_var.set("0%")
            self.status_var.set(f"正在识别拍摄时间…（共 {total} 个文件）")
        elif kind == "preview_progress":
            _, done, total = msg
            self.progress["maximum"] = max(total, 1)
            self.progress["value"] = done
            self.pct_var.set(f"{done * 100.0 / total:.0f}%" if total else "100%")
        elif kind == "times_ready":
            _, files, times, name_fb = msg
            self.files = files
            self.progress.stop()
            extracted = set()
            rules_txt = ""
            if name_fb:
                noexif = [f for f in files if times.get(f) is None]
                if noexif:
                    ordered = core.organize_groups(core.group_by_pattern(noexif))
                    confirmed = self._ask_name_times(ordered)
                    if confirmed:
                        apply_groups = {m: fs for m, fs in ordered if m != core.OTHER_MASK}
                        result, failed, rules = core.apply_name_groups(apply_groups, confirmed)
                        for f, t in result.items():
                            times[f] = t
                            extracted.add(f)
                        if rules:
                            rules_txt = "识别规则: " + "；".join(rules.values())
                        if failed:
                            rules_txt += (("；" if rules_txt else "") +
                                          f"有 {len(failed)} 个文件无法从名称提取时间")
            plan, skipped = core.build_plan(files, self._p_fmt,
                                            fallback=not self._p_nofb, times=times)
            self._populate(plan, skipped, extracted)
            self._set_busy(False)
            self.pct_var.set("100%")
            if not plan:  # 全部跳过时禁用执行按钮
                self.btn_apply.config(state="disabled")
            act = "复制" if self.copy_var.get() else "重命名"
            self.status_var.set(
                f"发现 {len(files)} 个照片文件，计划{act} {len(plan)} 个，跳过 {len(skipped)} 个"
                + ("（重名时自动追加 _1/_2）" if plan else "")
                + (("  |  " + rules_txt) if rules_txt else ""))
        elif kind == "row_ok":
            _, idx, final = msg
            src, _dst = self.plan[idx]
            if self.tree.exists(f"p{idx}"):
                self.tree.item(f"p{idx}", values=(self._rel(src), final, "✓ 完成"),
                               tags=("ok",))
        elif kind == "row_fail":
            _, idx, err = msg
            src, _dst = self.plan[idx]
            if self.tree.exists(f"p{idx}"):
                self.tree.item(f"p{idx}", values=(self._rel(src), "", f"✗ {err}"),
                               tags=("fail",))
        elif kind == "progress":
            _, done, total = msg
            self.progress["maximum"] = total
            self.progress["value"] = done
            self.pct_var.set(f"{done * 100.0 / total:.0f}%" if total else "100%")
        elif kind == "apply_done":
            _, ok, fail = msg
            self._set_busy(False)
            self.progress["value"] = 0
            self.pct_var.set("100%")
            self.status_var.set(f"完成: 成功 {ok} 个，失败 {fail} 个。")
            if fail:
                messagebox.showwarning("完成", f"成功 {ok} 个，失败 {fail} 个。\n失败项见列表中红色行。")
        elif kind == "error":
            _, err = msg
            self.progress.stop()
            self._set_busy(False)
            self.pct_var.set("")
            self.status_var.set("出错。")
            messagebox.showerror("错误", err)


class NameTimeDialog(tk.Toplevel):
    """无 EXIF 照片按名称类型分组，确认/修正每组拍摄时间。

    完成后 self.confirmed = {掩码: datetime}；空字典表示全部跳过。
    """

    def __init__(self, parent, ordered):
        """ordered: organize_groups 返回的有序 [(掩码, [文件路径]), ...]。"""
        super().__init__(parent)
        self.title("从文件名识别拍摄时间")
        self.geometry("820x500")
        self.minsize(720, 420)
        self.transient(parent)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.confirmed = {}              # 掩码 -> datetime
        self._groups = {m: fs for m, fs in ordered}
        self._order = [m for m, _ in ordered]
        self._guesses = {}
        for mask, fs in self._groups.items():
            if mask == core.OTHER_MASK:
                self._guesses[mask] = None
            else:
                stem = os.path.splitext(os.path.basename(fs[0]))[0]
                self._guesses[mask] = core.heuristic_guess_time(stem)
        self._build()
        self._reload()

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        f_top = ttk.Frame(self)
        f_top.pack(fill="x", **pad)
        ttk.Label(f_top, text="以下照片没有 EXIF，但文件名可能包含拍摄时间。"
                             "请核对每组时间（可修改后确定），确定后同类型照片将自动套用识别规则。"
                             "分组按照片数量从多到少排列；单张且未识别出时间的照片已并入「其它」。\n"
                             "支持多选（Ctrl/Shift+点击）：选中多组后点「确定所选组」可一次采用各组猜测。"
                  ).pack(anchor="w")
        f_list = ttk.Frame(self)
        f_list.pack(fill="both", expand=True, **pad)
        cols = ("mask", "sample", "count", "state")
        self.tree = ttk.Treeview(f_list, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("mask", text="名称类型")
        self.tree.heading("sample", text="示例文件名")
        self.tree.heading("count", text="数量")
        self.tree.heading("state", text="识别时间")
        self.tree.column("mask", width=120, anchor="w")
        self.tree.column("sample", width=280, anchor="w")
        self.tree.column("count", width=60, anchor="center")
        self.tree.column("state", width=190, anchor="w")
        vsb = ttk.Scrollbar(f_list, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        f_edit = ttk.Frame(self)
        f_edit.pack(fill="x", **pad)
        ttk.Label(f_edit, text="时间 (YYYY-MM-DD HH:MM:SS):").pack(side="left")
        self.time_var = tk.StringVar()
        self.time_entry = ttk.Entry(f_edit, textvariable=self.time_var, width=24)
        self.time_entry.pack(side="left", padx=(6, 6))
        ttk.Button(f_edit, text="确定该组", command=self._set_group).pack(side="left", padx=(0, 6))
        ttk.Button(f_edit, text="跳过该组", command=self._skip_group).pack(side="left", padx=(0, 6))
        f_bottom = ttk.Frame(self)
        f_bottom.pack(fill="x", **pad)
        ttk.Button(f_bottom, text="全选", command=self._select_all).pack(side="left", padx=(0, 6))
        ttk.Button(f_bottom, text="确定所选组（采用各组猜测）",
                   command=self._confirm_selected).pack(side="left", padx=(0, 8))
        ttk.Button(f_bottom, text="全部跳过", command=self._skip_all).pack(side="left", padx=(0, 8))
        self.count_var = tk.StringVar(value="")
        ttk.Label(f_bottom, textvariable=self.count_var, foreground="#777777"
                  ).pack(side="left")
        ttk.Button(f_bottom, text="完成", command=self._finish).pack(side="right")

    def _reload(self):
        self.tree.delete(*self.tree.get_children())
        for i, mask in enumerate(self._order):
            fs = self._groups[mask]
            guess = self._guesses[mask]
            disp = "其它（单张未识别）" if mask == core.OTHER_MASK else mask
            if mask == core.OTHER_MASK:
                state = "—（此类照片请手动处理）"
            elif mask in self.confirmed:
                state = f"已确认: {self.confirmed[mask]:%Y-%m-%d %H:%M:%S}"
            elif guess:
                state = f"猜测: {guess:%Y-%m-%d %H:%M:%S}"
            else:
                state = "未识别出时间"
            self.tree.insert("", "end", iid=str(i),
                             values=(disp, os.path.basename(fs[0]), len(fs), state))
        self._update_count()
        if self._order:
            self.tree.selection_set("0")
            self._on_select()

    def _update_count(self):
        total = sum(1 for m in self._order if m != core.OTHER_MASK)
        self.count_var.set(f"已确认 {len(self.confirmed)} / {total} 组" if self.confirmed
                           else f"共 {total} 组可确认")

    def _selected_masks(self):
        sel = self.tree.selection()
        return [self._order[int(i)] for i in sel if i.isdigit() and 0 <= int(i) < len(self._order)]

    def _select_all(self):
        self.tree.selection_set(*self.tree.get_children())
        self._on_select()

    def _confirm_selected(self):
        """批量确定选中组：采用各组自己的自动猜测（已确认的保留）。"""
        for mask in self._selected_masks():
            if mask == core.OTHER_MASK:
                continue
            dt = self.confirmed.get(mask) or self._guesses.get(mask)
            if dt:
                self.confirmed[mask] = dt
        self._reload()

    def _current_mask(self):
        sel = self.tree.selection()
        if not sel:
            return None
        i = int(sel[0])
        return self._order[i] if 0 <= i < len(self._order) else None

    def _on_select(self, _event=None):
        mask = self._current_mask()
        if mask is None:
            return
        if mask == core.OTHER_MASK:  # 其它分组不可确认
            self.time_var.set("")
            self.time_entry.state(["disabled"])
            return
        self.time_entry.state(["!disabled"])
        dt = self.confirmed.get(mask) or self._guesses.get(mask)
        self.time_var.set(f"{dt:%Y-%m-%d %H:%M:%S}" if dt else "")

    def _set_group(self):
        mask = self._current_mask()
        if mask is None:
            return
        if mask == core.OTHER_MASK:
            messagebox.showinfo("提示", "「其它」分组是单张且未识别出时间的照片，\n请手动改名或直接跳过。",
                                parent=self)
            return
        s = self.time_var.get().strip()
        dt = core.parse_datetime_flexible(s) if s else self._guesses.get(mask)
        if dt is None:
            messagebox.showwarning("提示", "时间格式无效，请使用 YYYY-MM-DD HH:MM:SS", parent=self)
            return
        self.confirmed[mask] = dt
        self._reload()

    def _skip_group(self):
        mask = self._current_mask()
        if mask is not None:
            self.confirmed.pop(mask, None)
        self._reload()

    def _skip_all(self):
        self.confirmed.clear()
        self._finish()

    def _finish(self):
        self.destroy()


def main():
    try:  # Windows 高分屏清晰显示
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.title(f"照片按拍摄时间重命名工具 v{__version__}")
    root.geometry("960x640")
    root.minsize(820, 540)
    PhotoRenamerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
