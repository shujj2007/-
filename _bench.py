# -*- coding: utf-8 -*-
"""大规模照片压力测试：创建 N 个照片文件（硬链接，速度快），测量扫描/计划/重命名耗时。

用法:
    python _bench.py scan 10000 [--jobs N] [--fmt 格式]
    python _bench.py apply 2000 [--jobs N] [--fmt 格式]
"""
import argparse
import os
import shutil
import time

from PIL import Image
import piexif

import photo_renamer as core

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(HERE, ".bench")


def make_bench(n):
    if os.path.isdir(BENCH):
        shutil.rmtree(BENCH)
    os.makedirs(BENCH)
    master = os.path.join(BENCH, "_master.jpg")
    exif = piexif.dump({"0th": {}, "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2020:01:01 00:00:00"},
                        "1st": {}, "GPS": {}, "Interop": {}})
    Image.new("RGB", (4, 4), (1, 2, 3)).save(master, exif=exif)
    t0 = time.perf_counter()
    for i in range(n):
        shutil.copy2(master, os.path.join(BENCH, f"p{i:05d}.jpg"))
    print(f"创建 {n} 个文件(复制): {time.perf_counter() - t0:.1f}s")
    return BENCH


def cmd_scan(args):
    make_bench(args.n)
    t0 = time.perf_counter()
    files = core.collect_files(BENCH, False, set(core.DEFAULT_EXTS))
    t1 = time.perf_counter()
    print(f"collect_files: {t1 - t0:.1f}s（{len(files)} 个文件）")
    plan, skipped = core.build_plan(files, args.fmt, fallback=True, jobs=args.jobs)
    t2 = time.perf_counter()
    print(f"build_plan(jobs={args.jobs}): {t2 - t1:.1f}s  计划 {len(plan)} 个，跳过 {len(skipped)} 个")
    print(f"扫描+读取EXIF+计划 总耗时: {t2 - t0:.1f}s")


def cmd_apply(args):
    make_bench(args.n)
    files = core.collect_files(BENCH, False, set(core.DEFAULT_EXTS))
    plan, skipped = core.build_plan(files, args.fmt, fallback=True, jobs=args.jobs)
    print(f"计划 {len(plan)} 个文件（格式: {args.fmt}）")
    t0 = time.perf_counter()
    ok, fail = core.apply_plan(plan, "rename", False)
    t1 = time.perf_counter()
    print(f"apply_plan 重命名: {t1 - t0:.1f}s  ok={ok} fail={fail}")
    shutil.rmtree(BENCH, ignore_errors=True)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("scan", cmd_scan), ("apply", cmd_apply)):
        sp = sub.add_parser(name)
        sp.add_argument("n", type=int)
        sp.add_argument("--jobs", type=int, default=1)
        sp.add_argument("--fmt", default="%Y%m%d_%H%M%S")
        sp.set_defaults(fn=fn)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
