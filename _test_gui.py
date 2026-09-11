# -*- coding: utf-8 -*-
"""GUI 自动化冒烟测试：不显示窗口，直接驱动 GUI 内部方法验证预览/执行流程。

用法: python _test_gui.py
"""
import datetime
import os
import shutil
import sys
import tempfile

import tkinter as tk
from PIL import Image
import piexif

import photo_renamer_gui as gui_mod

# 兼容 GBK 等旧控制台编码，避免打印特殊符号时崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


def make(base):
    os.makedirs(base, exist_ok=True)
    exif = piexif.dump({"0th": {}, "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2022:03:04 05:06:07"},
                        "1st": {}, "GPS": {}, "Interop": {}})
    Image.new("RGB", (16, 16), (10, 20, 30)).save(os.path.join(base, "exif_photo.jpg"), exif=exif)
    for name, t in (("plain1.jpg", datetime.datetime(2021, 3, 4, 5, 6, 7)),
                    ("plain2.png", datetime.datetime(2021, 3, 4, 5, 6, 8))):
        Image.new("RGB", (16, 16), (40, 50, 60)).save(os.path.join(base, name))
        ts = t.timestamp()
        os.utime(os.path.join(base, name), (ts, ts))
    with open(os.path.join(base, "notes.txt"), "w", encoding="utf-8") as f:
        f.write("not a photo")
    os.makedirs(os.path.join(base, "sub"))
    p3 = os.path.join(base, "sub", "plain3.jpg")
    Image.new("RGB", (16, 16), (70, 80, 90)).save(p3)
    ts = datetime.datetime(2021, 3, 4, 5, 6, 10).timestamp()
    os.utime(p3, (ts, ts))
    return base


def rows_of(app):
    return [app.tree.item(r, "values") for r in app.tree.get_children()]


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    base1 = os.path.join(here, ".gui_test_a")
    base2 = os.path.join(here, ".gui_test_b")
    for b in (base1, base2):
        shutil.rmtree(b, ignore_errors=True)
    try:
        make(base1)
        make(base2)

        root = tk.Tk()
        root.withdraw()
        app = gui_mod.PhotoRenamerGUI(root)
        app.folder_var.set(base1)
        app.recursive_var.set(True)
        app.name_fb_var.set(False)  # 既有场景关闭"从文件名识别时间"，避免弹窗

        # ---- 场景1: 预览（回退模式）----
        app.preview()
        app._thread.join()
        app._poll_queue()
        v = rows_of(app)
        print("预览行数:", len(v))
        for x in v:
            print("   ", x)
        assert len(v) == 4, f"应计划 4 个文件，实际 {len(v)}"
        by_src = {os.path.normpath(x[0]): os.path.normpath(x[1]) for x in v}
        assert by_src["plain1.jpg"] == "20210304_050607.jpg", by_src
        assert by_src["plain2.png"] == "20210304_050608.png", by_src
        assert by_src[os.path.join("sub", "plain3.jpg")] == os.path.join("sub", "20210304_050610.jpg"), by_src
        # EXIF 优先于 mtime（mtime 是"现在"，若回退则不会是 20220304）
        assert by_src["exif_photo.jpg"] == "20220304_050607.jpg", by_src
        assert "notes.txt" not in by_src
        assert not app.btn_apply.instate(["disabled"])
        # 识别进度：预览完成后进度条到 100%，百分比标签显示 100%
        assert app.pct_var.get() == "100%", app.pct_var.get()
        assert str(app.progress["maximum"]) == "4", app.progress["maximum"]
        print("场景1 预览+EXIF优先+进度显示: OK")

        # ---- 场景2: 执行重命名 ----
        app._apply_worker(False)
        app._thread.join()
        app._poll_queue()
        renamed = set(os.listdir(base1))
        assert "20210304_050607.jpg" in renamed and "plain1.jpg" not in renamed, renamed
        assert "20220304_050607.jpg" in renamed and "exif_photo.jpg" not in renamed, renamed
        assert "notes.txt" in renamed, renamed
        assert os.path.exists(os.path.join(base1, "sub", "20210304_050610.jpg"))
        st = [x for x in rows_of(app) if x[2].startswith("✓")]
        assert len(st) == 4, rows_of(app)
        print("场景2 执行重命名: OK")

        # ---- 场景3: 无 EXIF 跳过 + 复制模式 ----
        app.folder_var.set(base2)
        app.nofb_var.set(True)
        app.preview()
        app._thread.join()
        app._poll_queue()
        v2 = rows_of(app)
        planned = [x for x in v2 if x[2].startswith("待")]
        skipped = [x for x in v2 if x[2].startswith("跳过")]
        assert len(planned) == 1 and planned[0][0] == "exif_photo.jpg", v2
        assert len(skipped) == 3, v2
        assert all("无 EXIF" in x[2] for x in skipped), v2
        # 颜色标签：待改名=蓝(plan)，无EXIF跳过=红(skip_noexif)
        assert app.tree.item("p0", "tags") == ("plan",), app.tree.item("p0", "tags")
        assert all(app.tree.item(f"s{i}", "tags") == ("skip_noexif",) for i in range(3)), \
            [app.tree.item(f"s{i}", "tags") for i in range(3)]
        app.copy_var.set(True)
        app._apply_worker(True)
        app._thread.join()
        app._poll_queue()
        after = set(os.listdir(base2))
        assert "20220304_050607.jpg" in after and "exif_photo.jpg" in after, after
        print("场景3 no-fallback 跳过 + 复制模式 + 颜色标签: OK")

        # ---- 场景4: 预设格式映射 ----
        app.preset_var.set("2024\\01\\01_123456  → 按日期归档")
        app._on_preset()
        assert app.fmt_var.get() == "%Y\\%m\\%d_%H%M%S", app.fmt_var.get()
        app.preset_var.set("2020年07月25日星期六-晚上09点41分47秒")
        app._on_preset()
        assert app.fmt_var.get() == "%Y年%m月%d日{weekfull}-%p%I点%M分%S秒", app.fmt_var.get()
        print("场景4 预设格式映射: OK")

        # ---- 场景5: 星期显示 / 小时制 ----
        import photo_renamer as core
        monday = datetime.datetime(2024, 1, 1, 15, 30, 0)  # 2024-01-01 是周一
        assert core.render_filename("%Y%m%d_{week}", monday, "a", "jpg") == "20240101_周一"
        assert core.render_filename("%Y%m%d_{weekfull}", monday, "a", "jpg") == "20240101_星期一"
        assert core.render_filename("%Y%m%d_%H%M%S", monday, "a", "jpg") == "20240101_153000"
        assert core.render_filename("%Y%m%d_%I%M%S%p", monday, "a", "jpg") == "20240101_033000下午"
        assert core.render_filename("%Y%m%d_%p%I%M%S", monday, "a", "jpg") == "20240101_下午033000"
        morning = datetime.datetime(2024, 1, 1, 9, 5, 0)
        assert core.render_filename("%Y%m%d_%p%I%M%S", morning, "a", "jpg") == "20240101_上午090500"
        # 时段词边界：凌晨0-5 / 上午6-10 / 中午11-12 / 下午13-18 / 晚上19-23；小时统一两位
        assert core.render_filename("%Y%m%d_%p%I点%M分", datetime.datetime(2024, 1, 1, 0, 30, 0),
                                    "a", "jpg") == "20240101_凌晨00点30分"
        assert core.render_filename("%Y%m%d_%p%I点%M分", datetime.datetime(2024, 1, 1, 5, 59, 0),
                                    "a", "jpg") == "20240101_凌晨05点59分"
        assert core.render_filename("%Y%m%d_%p%I点%M分", datetime.datetime(2024, 1, 1, 6, 0, 0),
                                    "a", "jpg") == "20240101_上午06点00分"
        assert core.render_filename("%Y%m%d_%p%I点%M分", datetime.datetime(2024, 1, 1, 11, 30, 0),
                                    "a", "jpg") == "20240101_中午11点30分"
        assert core.render_filename("%Y%m%d_%p%I点%M分", datetime.datetime(2024, 1, 1, 12, 30, 0),
                                    "a", "jpg") == "20240101_中午12点30分"
        assert core.render_filename("%Y%m%d_%p%I点%M分", datetime.datetime(2024, 1, 1, 13, 30, 0),
                                    "a", "jpg") == "20240101_下午01点30分"
        assert core.render_filename("%Y%m%d_%p%I点%M分", datetime.datetime(2024, 1, 1, 19, 0, 0),
                                    "a", "jpg") == "20240101_晚上07点00分"
        # 中文长格式（GUI 预设）：2020-07-25 是星期六，21:41:47 → 晚上09点41分47秒
        user_dt = datetime.datetime(2020, 7, 25, 21, 41, 47)
        assert core.render_filename("%Y年%m月%d日{weekfull}-%p%I点%M分%S秒",
                                    user_dt, "a", "jpg") == "2020年07月25日星期六-晚上09点41分47秒"
        app.fmt_var.set("%Y%m%d_%H%M%S")
        app.week_var.set("周一（{week}）")
        app._on_week()
        assert app.fmt_var.get() == "%Y%m%d_%H%M%S_{week}", app.fmt_var.get()
        app.week_var.set("不显示")
        app._on_week()
        assert app.fmt_var.get() == "%Y%m%d_%H%M%S", app.fmt_var.get()
        app.hour_var.set("12 小时制（%I）")
        app._on_hour()
        assert app.fmt_var.get() == "%Y%m%d_%I%M%S", app.fmt_var.get()
        app.hour_var.set("12 小时制 + 时段词（%p%I）")
        app._on_hour()
        assert app.fmt_var.get() == "%Y%m%d_%p%I%M%S", app.fmt_var.get()
        app.hour_var.set("24 小时制（%H）")
        app._on_hour()
        assert app.fmt_var.get() == "%Y%m%d_%H%M%S", app.fmt_var.get()
        print("场景5 星期/小时制: OK")

        # ---- 场景6: 名称相同直接跳过（绿色标签）----
        app.folder_var.set(base1)      # base1 已在场景2 改过名
        app.nofb_var.set(False)
        app.copy_var.set(False)
        app.fmt_var.set("%Y%m%d_%H%M%S")
        app.preview()
        app._thread.join()
        app._poll_queue()
        v6 = rows_of(app)
        assert len(v6) == 4, v6
        assert all(x[2].startswith("跳过: 新名称与原名称相同") for x in v6), v6
        assert all(app.tree.item(f"s{i}", "tags") == ("skip_same",) for i in range(4)), \
            [app.tree.item(f"s{i}", "tags") for i in range(4)]
        assert app.btn_apply.instate(["disabled"]), "计划为空时应禁用执行按钮"
        print("场景6 名称相同跳过 + 绿色标签: OK")

        # ---- 场景7: 识别进度回调 ----
        files = core.collect_files(base1, True, set(core.DEFAULT_EXTS))
        calls = []
        core.build_plan(files, "%Y%m%d_%H%M%S", fallback=True, jobs=1,
                        progress=lambda d, t: calls.append((d, t)))
        assert calls[-1] == (len(files), len(files)), calls[-1]
        assert [d for d, _ in calls] == list(range(1, len(files) + 1)), calls
        print("场景7 识别进度回调: OK")

        # ---- 场景8: 从文件名识别拍摄时间（无 EXIF 照片）----
        nbase = os.path.join(here, ".gui_test_n")
        shutil.rmtree(nbase, ignore_errors=True)
        os.makedirs(nbase)
        for n in ("IMG_20230101_123456.jpg", "IMG_20230203_091011.jpg",
                  "IMG_20231399_999999.jpg", "DSC_0001.jpg"):
            Image.new("RGB", (8, 8), (1, 2, 3)).save(os.path.join(nbase, n))
        nfiles = core.collect_files(nbase, False, set(core.DEFAULT_EXTS))
        times = core.read_times(nfiles, jobs=1)
        assert all(v is None for v in times.values())
        noexif = [f for f in nfiles if times[f] is None]
        groups = core.group_by_pattern(noexif)
        assert "IMG_<8>_<6>" in groups and "DSC_<4>" in groups, list(groups)
        assert len(groups["IMG_<8>_<6>"]) == 3
        # 猜测
        assert core.heuristic_guess_time("IMG_20230101_123456") == datetime.datetime(2023, 1, 1, 12, 34, 56)
        assert core.heuristic_guess_time("2023-01-01_08-15-30") == datetime.datetime(2023, 1, 1, 8, 15, 30)
        assert core.heuristic_guess_time("230101_123456") == datetime.datetime(2023, 1, 1, 12, 34, 56)
        assert core.heuristic_guess_time("DSC_0001") is None
        # 学习映射并套用到同类型
        confirmed = {"IMG_<8>_<6>": datetime.datetime(2023, 1, 1, 12, 34, 56)}
        result, failed, rules = core.apply_name_groups(groups, confirmed)
        feb = [f for f in nfiles if f.endswith("IMG_20230203_091011.jpg")][0]
        assert result[feb] == datetime.datetime(2023, 2, 3, 9, 10, 11), result
        assert len(failed) == 1 and failed[0].endswith("IMG_20231399_999999.jpg"), failed
        assert "月2位" in rules["IMG_<8>_<6>"], rules
        # 两位年份映射
        m2 = core.learn_time_mapping("230101_123456", datetime.datetime(2023, 1, 1, 12, 34, 56))
        assert core.apply_time_mapping("240201_091011", m2) == datetime.datetime(2024, 2, 1, 9, 10, 11), m2
        # 名称中夹杂无关数字段（序号/版本号）也能学习并套用
        m3 = core.learn_time_mapping("IMG_20230101_123456_01", datetime.datetime(2023, 1, 1, 12, 34, 56))
        assert m3, m3
        assert core.apply_time_mapping("IMG_20240203_091011_99", m3) == datetime.datetime(2024, 2, 3, 9, 10, 11), m3
        m4 = core.learn_time_mapping("20230101123456_01", datetime.datetime(2023, 1, 1, 12, 34, 56))
        assert m4 and core.apply_time_mapping("20240203091011_02", m4) == datetime.datetime(2024, 2, 3, 9, 10, 11), m4
        # 仅日期（部分组件）：成员时间为 00:00:00
        m5 = core.learn_time_mapping("IMG_20230101", datetime.datetime(2023, 1, 1, 12, 34, 56))
        assert m5 and core.apply_time_mapping("IMG_20240203", m5) == datetime.datetime(2024, 2, 3), m5
        # 时间在日期前（倒序匹配）
        m6 = core.learn_time_mapping("123456_20230101", datetime.datetime(2023, 1, 1, 12, 34, 56))
        assert m6 and core.apply_time_mapping("091011_20240203", m6) == datetime.datetime(2024, 2, 3, 9, 10, 11), m6
        # 确认时间与名称不完全一致（如日期差一天）：结构兜底仍套用规则
        g2 = core.group_by_pattern([os.path.join(nbase, "IMG_20230101_123456.jpg"),
                                    os.path.join(nbase, "IMG_20230203_091011.jpg")])
        res2, fail2, rules2 = core.apply_name_groups(
            g2, {"IMG_<8>_<6>": datetime.datetime(2023, 1, 2, 12, 34, 56)})
        feb2 = [f for f in res2 if f.endswith("IMG_20230203_091011.jpg")]
        assert feb2 and res2[feb2[0]] == datetime.datetime(2023, 2, 3, 9, 10, 11), res2
        assert "按名称结构推断" in rules2["IMG_<8>_<6>"], rules2
        # 时间戳命名（13 位毫秒，微信 mmexport 风格）
        ts = int(datetime.datetime(2023, 7, 22, 9, 46, 40).timestamp()) * 1000
        mts = core.learn_time_mapping(f"mmexport{ts}", datetime.datetime.fromtimestamp(ts / 1000))
        assert mts and list(mts.values()) == [(0, "EPOCH", 0)], mts
        assert core.apply_time_mapping(f"mmexport{ts}", mts) == datetime.datetime.fromtimestamp(ts / 1000)
        assert core.heuristic_guess_time(f"mmexport{ts}") == datetime.datetime.fromtimestamp(ts / 1000)
        # retouch 场景：16位连续数字 = 14位时间 + 2位序号粘合（用户实测）
        m7 = core.learn_time_mapping("retouch_2023072117520501", datetime.datetime(2023, 7, 21, 17, 52, 5))
        assert m7, m7
        assert core.apply_time_mapping("retouch_2023072118461014", m7) == datetime.datetime(2023, 7, 21, 18, 46, 10), m7
        assert core.heuristic_guess_time("retouch_2023072117520501") == datetime.datetime(2023, 7, 21, 17, 52, 5)
        # 猜测：带序号也能猜出
        assert core.heuristic_guess_time("20230101123456_01") == datetime.datetime(2023, 1, 1, 12, 34, 56)
        assert core.heuristic_guess_time("IMG_20230101_123456_01") == datetime.datetime(2023, 1, 1, 12, 34, 56)
        assert core.heuristic_guess_time("2023-01-01_123456") == datetime.datetime(2023, 1, 1, 12, 34, 56)
        # 灵活时间解析
        assert core.parse_datetime_flexible("2023-01-01 12:34:56") == datetime.datetime(2023, 1, 1, 12, 34, 56)
        assert core.parse_datetime_flexible("20230101123456") == datetime.datetime(2023, 1, 1, 12, 34, 56)
        assert core.parse_datetime_flexible("垃圾") is None
        # 对话框（无主循环）：确定该组
        dlg = gui_mod.NameTimeDialog(root, [("IMG_<8>_<6>", [os.path.join(nbase, "IMG_20230101_123456.jpg")])])
        dlg.time_var.set("2023-05-06 07:08:09")
        dlg._set_group()
        assert dlg.confirmed == {"IMG_<8>_<6>": datetime.datetime(2023, 5, 6, 7, 8, 9)}
        dlg._finish()
        root.update_idletasks()

        # 分组整理：单张且无日期 → 并入"其它"；按数量降序；其它恒在最后
        for n in ("abc_123.jpg", "xyz_456.png"):
            Image.new("RGB", (8, 8), (1, 2, 3)).save(os.path.join(nbase, n))
        nfiles2 = core.collect_files(nbase, False, set(core.DEFAULT_EXTS))
        ordered3 = core.organize_groups(core.group_by_pattern(nfiles2))
        assert ordered3[-1][0] == core.OTHER_MASK, [m for m, _ in ordered3]
        assert len(ordered3[-1][1]) == 3, len(ordered3[-1][1])  # DSC_0001 + abc_123 + xyz_456
        assert ordered3[0][0] == "IMG_<8>_<6>" and len(ordered3[0][1]) == 3, ordered3
        counts = [len(fs) for _, fs in ordered3[:-1]]
        assert counts == sorted(counts, reverse=True), counts
        # 对话框：其它行不可确认（输入框禁用）
        dlg2 = gui_mod.NameTimeDialog(root, ordered3)
        dlg2.tree.selection_set(str(len(ordered3) - 1))
        dlg2._on_select()
        assert dlg2.time_var.get() == ""
        assert "disabled" in dlg2.time_entry.state()
        gui_mod.messagebox.showinfo = lambda *a, **k: None  # 测试环境不弹窗
        old = dict(dlg2.confirmed)
        dlg2.time_var.set("2023-01-01 12:34:56")
        dlg2._set_group()
        assert dlg2.confirmed == old, dlg2.confirmed  # 其它不可确认
        dlg2._finish()
        root.update_idletasks()

        # 多选批量确认：全选 → 确定所选组（采用各组猜测，无猜测/其它跳过）
        dlg3 = gui_mod.NameTimeDialog(root, [
            ("IMG_<8>_<6>", [os.path.join(nbase, "IMG_20230101_123456.jpg")]),
            ("retouch_<16>", [os.path.join(nbase, "retouch_2023072117520501.jpg")]),
            ("abc_<3>", [os.path.join(nbase, "abc_123.jpg")]),
            (core.OTHER_MASK, [os.path.join(nbase, "xyz_456.png")]),
        ])
        dlg3._select_all()
        assert len(dlg3.tree.selection()) == 4
        dlg3._confirm_selected()
        assert set(dlg3.confirmed) == {"IMG_<8>_<6>", "retouch_<16>"}, dlg3.confirmed
        assert dlg3.confirmed["IMG_<8>_<6>"] == datetime.datetime(2023, 1, 1, 12, 34, 56)
        assert dlg3.confirmed["retouch_<16>"] == datetime.datetime(2023, 7, 21, 17, 52, 5)
        assert "已确认 2 / 3" in dlg3.count_var.get(), dlg3.count_var.get()
        dlg3._finish()
        root.update_idletasks()
        shutil.rmtree(nbase, ignore_errors=True)
        print("场景8 从文件名识别时间+分组整理+多选确认: OK")

        print("\nGUI 冒烟测试全部通过 ✔")
        root.destroy()
    finally:
        shutil.rmtree(base1, ignore_errors=True)
        shutil.rmtree(base2, ignore_errors=True)


if __name__ == "__main__":
    main()
