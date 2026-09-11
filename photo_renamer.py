#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""照片按拍摄时间自动重命名工具 (Photo Renamer by EXIF datetime).

从照片 EXIF 中读取拍摄时间（优先 DateTimeOriginal），按用户自定义格式重命名，
例如:

    python photo_renamer.py D:\\Photos -f "IMG_%Y%m%d_%H%M%S"

依赖（按需安装）:
    pip install Pillow        # 读取常见格式(JPEG/PNG/WebP/TIFF 等)的 EXIF
    pip install exifread      # 可选: 读取 RAW(CR2/NEF/ARW/DNG 等)的 EXIF
    pip install pillow-heif   # 可选: 支持 HEIC/HEIF

格式占位符（strftime）:
    %Y 四位年份  %y 两位年份  %m 月份(01-12)  %d 日(01-31)
    %H 小时(24小时制,00-23)  %I 小时(12小时制,统一两位00-12;凌晨0点为00)
    %M 分钟(00-59)  %S 秒(00-59)  %f 微秒(6 位)
    %p 中文时段词(凌晨0-5/上午6-10/中午11-12/下午13-18/晚上19-23,位置自定,如 %p%I 为"下午03")
    %j 年内第几天  %U 年内第几周  %a 星期简写  %A 星期全写
    %b 月份简写    %B 月份全写    %% 字面 %
自定义占位符:
    {orig} 原文件名（不含扩展名）   {ext} 原扩展名（不含点）
    {week} 中文星期简写(周一~周日)  {weekfull} 中文星期全写(星期一~星期日)

格式中包含 / 或 \\ 时会自动创建子目录，例如 -f "%Y\\%m\\%d_%H%M%S" 按日期归档。

详细说明见 README.md。
"""

__version__ = "1.4.5"

import argparse
import datetime
import logging
import os
import re
import shutil
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

try:
    import exifread
    HAVE_EXIFREAD = True
except Exception:
    HAVE_EXIFREAD = False

if HAVE_EXIFREAD:
    # exifread 对无 EXIF 的 PNG/WebP 等会打印日志到 stderr，属正常情况，全局静音
    _exif_logger = logging.getLogger("exifread")
    _exif_logger.setLevel(logging.CRITICAL)
    _exif_logger.propagate = False

# 常见照片/RAW 扩展名（不区分大小写）
DEFAULT_EXTS = {
    "jpg", "jpeg", "jpe", "tif", "tiff", "png", "webp", "heic", "heif", "avif",
    "cr2", "cr3", "nef", "nrw", "arw", "srw", "orf", "raf", "rw2", "dng",
    "raw", "pef", "x3f", "mrw", "3fr", "erf", "iiq", "kdc", "rwl", "srf",
}

# EXIF 标签 ID: DateTimeOriginal / DateTimeDigitized / DateTime
TAG_DT_ORIGINAL = 36867
TAG_DT_DIGITIZED = 36868
TAG_DT = 306

# 中文星期（索引对应 datetime.weekday(): 0=周一 ... 6=周日）
WEEK_CN_SHORT = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
WEEK_CN_FULL = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# %p / %I 的中文替换哨兵（strftime 前替换，strftime 后替换为中文时段词/小时）
_AMPM_SENTINEL = "{__时段__}"
_H12_SENTINEL = "{__12时__}"


def period_cn(hour):
    """按小时返回中文时段词（用户指定划分）：
    凌晨 0-5 / 上午 6-10 / 中午 11-12 / 下午 13-18 / 晚上 19-23。"""
    if hour <= 5:
        return "凌晨"
    if hour <= 10:
        return "上午"
    if hour <= 12:
        return "中午"
    if hour <= 18:
        return "下午"
    return "晚上"


def hour12_cn(hour):
    """12 小时制小时，统一两位：00-12（凌晨0点显示 00，避免与中午 12 混淆）。"""
    if hour == 0:
        return "00"
    h = hour % 12
    return f"{h if h else 12:02d}"

# 终端彩色输出（仅 stdout 为终端时启用）
_COLOR = False

# ANSI 颜色码：34 蓝 / 32 绿 / 31 红 / 90 灰
_COLOR_PLAN = "34"     # 已识别时间、需要改名
_COLOR_SAME = "32"     # 新名称与原名称相同
_COLOR_NOEXIF = "31"   # 无 EXIF 拍摄时间
_COLOR_OTHER = "90"


def _enable_vt():
    """Windows 10+ 控制台启用 ANSI 转义序列。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetConsoleMode(k32.GetStdHandle(-11), 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def _c(text, code):
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def _print_progress(done, total):
    """在终端用 \r 覆盖刷新识别进度（输出到 stderr，不影响 stdout 的结果）。"""
    pct = done * 100.0 / total if total else 100.0
    sys.stderr.write(f"\r识别进度: {pct:5.1f}%  ({done}/{total})")
    sys.stderr.flush()


def _clear_progress_line():
    sys.stderr.write("\r" + " " * 70 + "\r")
    sys.stderr.flush()


def _skip_color(reason):
    if reason.startswith("新名称与原名称相同"):
        return _COLOR_SAME
    if "无 EXIF" in reason:
        return _COLOR_NOEXIF
    return _COLOR_OTHER

# 文件名非法字符（Windows 与常见系统）
INVALID_CHARS = set('<>:"|?*')


def parse_exif_str(text):
    """解析 EXIF 时间字符串（'YYYY:MM:DD HH:MM:SS' 或带 '-' 的分隔形式）。"""
    text = text.strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y:%m:%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def get_photo_datetime(path):
    """读取照片 EXIF 拍摄时间；读不到返回 None。"""
    if HAVE_PIL:
        try:
            with warnings.catch_warnings():
                # 无 EXIF 的 PNG 等格式会触发 "does not have exif data" 警告，属正常情况
                warnings.simplefilter("ignore", UserWarning)
                with Image.open(path) as img:
                    exif = img.getexif()
                    if exif:
                        for tag in (TAG_DT_ORIGINAL, TAG_DT_DIGITIZED, TAG_DT):
                            raw = exif.get(tag)
                            if raw:
                                dt = parse_exif_str(str(raw))
                                if dt:
                                    return dt
        except Exception:
            pass
    if HAVE_EXIFREAD:
        try:
            with open(path, "rb") as fh:
                tags = exifread.process_file(fh, details=False)
            for key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized",
                        "Image DateTime"):
                tag = tags.get(key)
                if tag:
                    dt = parse_exif_str(str(tag))
                    if dt:
                        return dt
        except Exception:
            pass
    return None


def sanitize_name(name):
    """把文件名中的非法字符替换为下划线；/ 与 \\ 视为路径分隔符（用于按日期归档）。

    返回的字符串仍可能包含子目录层级，由调用方确保目标目录存在。
    """
    parts = [p for p in re.split(r"[\\/]+", name) if p]
    cleaned = [re.sub(r'[<>:"|?*]', "_", p).strip(" .") for p in parts]
    return os.sep.join(cleaned)


def render_filename(fmt, dt, orig_stem, ext):
    """按格式串生成新的文件名主体（可含子目录，不含扩展名）。

    %p 固定渲染为中文时段词（凌晨/上午/中午/下午/晚上，按小时自动选择），
    %I 统一两位（00-12，凌晨 0 点为 00）；均先用哨兵替换，
    strftime 后再替换，不依赖系统区域设置，在打包的 exe 中行为一致。
    """
    body = (fmt.replace("{orig}", orig_stem)
               .replace("{ext}", ext)
               .replace("{week}", WEEK_CN_SHORT[dt.weekday()])
               .replace("{weekfull}", WEEK_CN_FULL[dt.weekday()]))
    body = body.replace("%I", _H12_SENTINEL).replace("%p", _AMPM_SENTINEL)
    try:
        body = dt.strftime(body)
    except ValueError as exc:
        raise ValueError(f"命名格式无效: {exc}") from exc
    body = body.replace(_AMPM_SENTINEL, period_cn(dt.hour))
    body = body.replace(_H12_SENTINEL, hour12_cn(dt.hour))
    body = sanitize_name(body)
    if not body:
        raise ValueError("命名格式生成的文件名为空")
    return body


def collect_files(root, recursive, exts):
    """收集符合条件的照片文件路径（排序保证确定性）。"""
    files = []
    if recursive:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                files.append(os.path.join(dirpath, name))
    else:
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if os.path.isfile(full):
                files.append(full)
    matched = [p for p in files
               if os.path.splitext(p)[1].lstrip(".").lower() in exts]
    return sorted(matched)


def read_times(files, jobs=1, progress=None):
    """读取所有文件的拍摄时间；返回 {路径: datetime 或 None}。

    progress: 可选回调 progress(已完成数, 总数)。
    """
    n = len(files)

    def read_dt(src):
        return src, get_photo_datetime(src)

    if jobs <= 0:
        jobs = os.cpu_count() or 1
    result = {}
    if jobs > 1 and n > 1:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            for idx, (src, dt) in enumerate(ex.map(read_dt, files), 1):
                result[src] = dt
                if progress:
                    progress(idx, n)
    else:
        for idx, f in enumerate(files, 1):
            _, dt = read_dt(f)
            result[f] = dt
            if progress:
                progress(idx, n)
    return result


def build_plan(files, fmt, fallback, jobs=1, progress=None, times=None):
    """生成 (源文件, 目标文件) 计划与跳过清单。

    jobs: 并行读取 EXIF 的线程数（0=按 CPU 核数自动，1=单线程顺序处理）。
    progress: 可选回调 progress(已完成数, 总数)，在识别过程中逐步调用。
    times: 可选 {路径: datetime 或 None}，提供后不再重新读取 EXIF
           （用于"从文件名识别时间"等前置阶段）。
    """
    plan, skipped = [], []
    n = len(files)

    def handle(src, dt):
        if dt is None:
            if not fallback:
                skipped.append((src, "无 EXIF 拍摄时间（--no-fallback）"))
                return
            dt = datetime.datetime.fromtimestamp(os.path.getmtime(src))
        d = os.path.dirname(src) or "."
        stem = os.path.splitext(os.path.basename(src))[0]
        ext = os.path.splitext(src)[1].lstrip(".").lower()
        try:
            body = render_filename(fmt, dt, stem, ext)
        except ValueError as exc:
            skipped.append((src, str(exc)))
            return
        dst = os.path.join(d, f"{body}.{ext}")
        if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(src)):
            skipped.append((src, "新名称与原名称相同"))
            return
        plan.append((src, dst))

    if times is not None:
        results = [(f, times.get(f)) for f in files]
    elif jobs <= 0:
        jobs = os.cpu_count() or 1
        results = [(f, get_photo_datetime(f)) for f in files]
    elif jobs > 1 and n > 1:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            results = list(ex.map(lambda f: (f, get_photo_datetime(f)), files))
    else:
        results = [(f, get_photo_datetime(f)) for f in files]

    for idx, (src, dt) in enumerate(results, 1):
        handle(src, dt)
        if progress and times is None:
            progress(idx, n)
    return plan, skipped


class UniqueNamer:
    """基于目录快照 + 序号续接的重名检测器。

    每个目录只做一次 os.listdir 快照；同一前缀的重名探测从上次用到的
    序号继续（而不是每次都从 _1 开始），整体 O(n)，上万张同秒照片
    也能在几秒内完成，且绝不覆盖已有文件。
    """

    def __init__(self):
        self._dirs = {}   # normcase(绝对目录) -> {normcase(名字), ...}
        self._next = {}   # (normcase(目录), normcase(前缀)) -> 下一个尝试序号

    def _names(self, d):
        key = os.path.normcase(d)
        names = self._dirs.get(key)
        if names is None:
            try:
                names = {os.path.normcase(n) for n in os.listdir(d)} if os.path.isdir(d) else set()
            except OSError:
                names = set()
            self._dirs[key] = names
        return names

    def unique(self, dst):
        """返回不冲突的目标路径；与已存在（或本次已占用）的名字冲突时追加 _1/_2。"""
        d = os.path.dirname(dst) or "."
        names = self._names(d)
        base = os.path.basename(dst)
        key_base = os.path.normcase(base)
        if key_base not in names:
            names.add(key_base)
            return dst
        stem, ext = os.path.splitext(base)
        key = (os.path.normcase(d), os.path.normcase(stem))
        i = self._next.get(key, 1)
        while True:
            cand = f"{stem}_{i}{ext}"
            if os.path.normcase(cand) not in names:
                names.add(os.path.normcase(cand))
                self._next[key] = i + 1
                return os.path.join(d, cand)
            i += 1


def unique_dst(dst):
    """目标已存在时追加 _1/_2 后缀，避免覆盖已有文件。"""
    if not os.path.exists(dst):
        return dst
    stem, ext = os.path.splitext(dst)
    i = 1
    while True:
        cand = f"{stem}_{i}{ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def apply_plan(plan, action, verbose):
    ok = fail = 0
    namer = UniqueNamer()
    ensured = set()
    for src, dst in plan:
        final = namer.unique(dst)
        if os.path.normcase(final) == os.path.normcase(src):
            continue  # 唯一化后仍指向自身（理论上不会发生）
        try:
            target_dir = os.path.dirname(final)
            if target_dir and target_dir not in ensured:
                os.makedirs(target_dir, exist_ok=True)
                ensured.add(target_dir)
            if action == "copy":
                shutil.copy2(src, final)
            else:
                os.rename(src, final)
            ok += 1
            if verbose:
                print(_c(f"  OK  {src}  ->  {final}", _COLOR_SAME))
        except OSError as exc:
            fail += 1
            print(_c(f"  失败  {src}: {exc}", _COLOR_NOEXIF), file=sys.stderr)
    return ok, fail


# ---------------------------------------------------------------------------
# 从文件名识别拍摄时间（针对无 EXIF 的照片）
# ---------------------------------------------------------------------------

def digit_runs(stem):
    """返回文件名主体中的连续数字段列表：[(起始, 结束, 数字串), ...]。"""
    runs = []
    i, n = 0, len(stem)
    while i < n:
        if stem[i].isdigit():
            j = i
            while j < n and stem[j].isdigit():
                j += 1
            runs.append((i, j, stem[i:j]))
            i = j
        else:
            i += 1
    return runs


def name_mask(stem):
    """按数字段长度生成名称类型掩码：'IMG_20230101_123456' -> 'IMG_<8>_<6>'。"""
    out = []
    i, n = 0, len(stem)
    while i < n:
        if stem[i].isdigit():
            j = i
            while j < n and stem[j].isdigit():
                j += 1
            out.append(f"<{j - i}>")
            i = j
        else:
            out.append(stem[i])
            i += 1
    return "".join(out)


def group_by_pattern(files):
    """按文件名类型分组（忽略扩展名）：{掩码: [文件路径, ...]}。"""
    groups = {}
    for p in files:
        stem = os.path.splitext(os.path.basename(p))[0]
        groups.setdefault(name_mask(stem), []).append(p)
    return groups


# 无法识别的单张照片合并分组使用的掩码
OTHER_MASK = "__其它__"


def organize_groups(groups):
    """整理分组（用于确认对话框展示）：

    - 只有 1 张且未识别出日期的组，全部并入"其它"（OTHER_MASK）
    - 其余按组内照片数量降序排列（多的在前）
    - "其它"分组无论数量多少，始终排在最后
    返回有序列表 [(掩码, [文件路径]), ...]
    """
    main, other = {}, []
    for mask, files in groups.items():
        if len(files) == 1 and heuristic_guess_time(_stem(files[0])) is None:
            other.extend(files)
        else:
            main[mask] = files
    ordered = sorted(main.items(), key=lambda kv: len(kv[1]), reverse=True)
    if other:
        ordered.append((OTHER_MASK, other))
    return ordered


def _stem(path):
    return os.path.splitext(os.path.basename(path))[0]


def _year2_to_full(yy):
    """两位年份转四位：70-99 → 19xx，00-69 → 20xx。"""
    return 1900 + yy if yy >= 70 else 2000 + yy


def heuristic_guess_time(stem):
    """从文件名猜测拍摄时间（尽力而为，自动扫描任意位置的数字段），识别不出返回 None。"""
    runs = [r[2] for r in digit_runs(stem)]

    def ok(y, mo, d, h=0, mi=0, s=0):
        try:
            return datetime.datetime(y, mo, d, h, mi, s)
        except ValueError:
            return None

    # 任意 ≥14 位数字：前 14 位为 YYYYMMDDHHMMSS（尾部可能粘合序号等多余数字）
    for r in runs:
        if len(r) >= 14:
            dt = ok(int(r[0:4]), int(r[4:6]), int(r[6:8]),
                    int(r[8:10]), int(r[10:12]), int(r[12:14]))
            if dt:
                return dt
    # 任意 8 位日期，优先配任意 6 位时间
    for i, r in enumerate(runs):
        if len(r) == 8:
            d8 = ok(int(r[0:4]), int(r[4:6]), int(r[6:8]))
            if d8:
                for j, t in enumerate(runs):
                    if i != j and len(t) == 6:
                        dt = ok(int(r[0:4]), int(r[4:6]), int(r[6:8]),
                                int(t[0:2]), int(t[2:4]), int(t[4:6]))
                        if dt:
                            return dt
                return d8
    # 连续 4,2,2 日期（带分隔符），后接 2,2,2 或单个 6 位时间
    for i in range(len(runs) - 2):
        if (len(runs[i]), len(runs[i + 1]), len(runs[i + 2])) == (4, 2, 2):
            d = ok(int(runs[i]), int(runs[i + 1]), int(runs[i + 2]))
            if d:
                if (i + 5 < len(runs) and
                        (len(runs[i + 3]), len(runs[i + 4]), len(runs[i + 5])) == (2, 2, 2)):
                    t = ok(int(runs[i]), int(runs[i + 1]), int(runs[i + 2]),
                           int(runs[i + 3]), int(runs[i + 4]), int(runs[i + 5]))
                    if t:
                        return t
                for j, t in enumerate(runs):
                    if j > i + 2 and len(t) == 6:
                        dt = ok(int(runs[i]), int(runs[i + 1]), int(runs[i + 2]),
                                int(t[0:2]), int(t[2:4]), int(t[4:6]))
                        if dt:
                            return dt
                return d
    # 两个 6 位：YYMMDD + HHMMSS
    for i, r in enumerate(runs):
        if len(r) == 6:
            for j, t in enumerate(runs):
                if i != j and len(t) == 6:
                    dt = ok(_year2_to_full(int(r[0:2])), int(r[2:4]), int(r[4:6]),
                            int(t[0:2]), int(t[2:4]), int(t[4:6]))
                    if dt:
                        return dt
    # 任意 6 位：YYMMDD
    for r in runs:
        if len(r) == 6:
            dt = ok(_year2_to_full(int(r[0:2])), int(r[2:4]), int(r[4:6]))
            if dt:
                return dt
    # 时间戳：10 位秒 / 13 位毫秒（微信 mmexport 等）
    for r in runs:
        cand = _epoch_candidate(r)
        if cand:
            return cand
    return None


def _comp_widths(compstr, yw):
    """按组件串与年份位数返回每个组件的位数（m/d/H/M/S 固定 2 位）。"""
    return [yw if c == "Y" else 2 for c in compstr]


def _block_len(compstr, yw):
    return yw + 2 * (len(compstr) - 1) if "Y" in compstr else 2 * len(compstr)


def _structural_mappings(runs):
    """仅按数字段位数枚举可能的 组件→数字段 分解（不依赖具体数值）。

    返回 [(映射, 覆盖组件数), ...]；映射为 {数字段下标: (偏移, 组件串, 年份位数)}。
    支持数字段内定位：段首/段尾粘合的多余数字（如 14位时间+2位序号）会被忽略。
    """
    orders = (["Y", "m", "d", "H", "M", "S"],      # 常规：日期在前
              ["H", "M", "S", "Y", "m", "d"])      # 少见：时间块在前
    results = []
    for order in orders:
        for yw in (4, 2):
            for start in range(len(runs)):

                def assign(i_run, i_comp, found):
                    """枚举该起点下所有可行分解（不短路，供评分选择）。"""
                    if i_comp == len(order):
                        return [dict(found)]  # 全部组件已消费（剩余数字段可忽略）
                    if i_run >= len(runs):
                        return [dict(found)] if i_comp >= 1 else []
                    r = runs[i_run]
                    out = []
                    for k in range(1, len(order) - i_comp + 1):
                        block = "".join(order[i_comp + j] for j in range(k))
                        blen = _block_len(block, yw if "Y" in block else 0)
                        if blen > len(r):
                            continue
                        # 组件块可放在段内任意偏移（忽略段首/段尾多余数字）
                        for off in range(len(r) - blen + 1):
                            found[i_run] = (off, block, yw if "Y" in block else 0)
                            out.extend(assign(i_run + 1, i_comp + k, found))
                            del found[i_run]
                    return out

                for m in assign(start, 0, {}):
                    results.append((m, sum(len(c) for _, c, _ in m.values())))
    seen, uniq = set(), []
    for m, comps in results:
        key = tuple(sorted(m.items()))
        if key not in seen:
            seen.add(key)
            uniq.append((m, comps))
    return uniq


def _epoch_candidate(run_digits):
    """若数字段是 10 位秒 / 13 位毫秒时间戳，返回其对应时间，否则 None。"""
    if len(run_digits) not in (10, 13) or not run_digits.isdigit():
        return None
    try:
        v = int(run_digits)
        if len(run_digits) >= 13:
            v = v / 1000.0
        return datetime.datetime.fromtimestamp(v)
    except (ValueError, OSError, OverflowError):
        return None


def learn_time_mapping(stem, dt):
    """从（文件名主体, 确认时间）反推数字段与年月日时分秒的对应关系。

    自动在名称中寻找能匹配 年月日时分秒 的数字段（支持段内偏移，忽略
    段首/段尾粘合的多余数字，如 retouch_2023072117520501 尾部序号）。
    返回 {数字段下标: (偏移, 组件串, 年份位数)} 或 None。
    """
    runs = [r[2] for r in digit_runs(stem)]
    values = {"m": f"{dt.month:02d}", "d": f"{dt.day:02d}",
              "H": f"{dt.hour:02d}", "M": f"{dt.minute:02d}", "S": f"{dt.second:02d}"}
    year_opts = [str(dt.year)]
    y2 = f"{dt.year % 100:02d}"
    if y2 != str(dt.year):
        year_opts.append(y2)
    orders = (["Y", "m", "d", "H", "M", "S"],      # 常规：日期在前
              ["H", "M", "S", "Y", "m", "d"])      # 少见：时间块在日期块前

    def search(order, require_full):
        best = None
        for yval in year_opts:
            yw = len(yval)
            values["Y"] = yval
            for start in range(len(runs)):
                found = {}

                def assign(i_run, i_comp):
                    if i_comp == len(order):
                        return True  # 全部组件已消费（剩余数字段可忽略）
                    if i_run >= len(runs):
                        return (not require_full) and i_comp >= 1  # 名称到末尾，允许部分
                    r = runs[i_run]
                    for k in range(len(order) - i_comp, 0, -1):
                        seg = "".join(values[order[i_comp + j]] for j in range(k))
                        pos = r.find(seg)  # 段内定位：允许前后粘合多余数字
                        if pos < 0:
                            continue
                        compstr = "".join(order[i_comp + j] for j in range(k))
                        found[i_run] = (pos, compstr, yw if "Y" in compstr else 0)
                        if assign(i_run + 1, i_comp + k):
                            return True
                        del found[i_run]
                    return False

                if assign(start, 0):
                    # 数值匹配必须与确认时间完全一致，否则拒绝
                    # （防止 2 位年份在错误位置撞上巧合数字，如 "123456" 里的 "23"）
                    if apply_time_mapping(stem, found) != dt:
                        continue
                    comps = sum(len(c) for _, c, _ in found.values())
                    score = (comps, -start)
                    if best is None or score > best[0]:
                        best = (score, dict(found))
        return best[1] if best else None

    m = search(orders[0], require_full=True)
    if m is None:
        m = search(orders[1], require_full=True)
    if m is None:
        m = search(orders[0], require_full=False)
    if m is None:
        m = search(orders[1], require_full=False)
    if m is None:
        # 时间戳命名（微信 mmexport 等 10/13 位）：与确认时间吻合则视为时间戳
        for idx, r in enumerate(runs):
            cand = _epoch_candidate(r)
            if cand and abs((cand - dt).total_seconds()) <= 7200:
                return {idx: (0, "EPOCH", 0)}
        # 结构兜底：数值匹配不上时，只要位数布局符合常见格式就按结构推断
        best = None
        for cand, comps in _structural_mappings(runs):
            t = apply_time_mapping(stem, cand)
            if t is None:
                continue
            date_ok = (t.year, t.month, t.day) == (dt.year, dt.month, dt.day)
            time_ok = (t.hour, t.minute, t.second) == (dt.hour, dt.minute, dt.second)
            score = (date_ok, time_ok, comps)
            if best is None or score > best[0]:
                best = (score, cand)
        if best:
            m = best[1]
    return m


def apply_time_mapping(stem, mapping):
    """按已学到的映射从文件名提取拍摄时间；非法（如月=13）返回 None。"""
    runs = [r[2] for r in digit_runs(stem)]
    if any(idx >= len(runs) for idx in mapping):
        return None
    vals = {}
    for idx, (off, compstr, yw) in mapping.items():
        r = runs[idx]
        if compstr == "EPOCH":  # 时间戳：10 位秒 / 13 位毫秒
            cand = _epoch_candidate(r)
            if cand is None:
                return None
            return cand
        widths = _comp_widths(compstr, yw)
        if off + sum(widths) > len(r):
            return None
        pos = off
        for c, w in zip(compstr, widths):
            vals[c] = int(r[pos:pos + w])
            pos += w
    y = vals.get("Y")
    if y is None:
        return None
    if y < 100:
        y = _year2_to_full(y)
    try:
        return datetime.datetime(y, vals.get("m", 1), vals.get("d", 1),
                                 vals.get("H", 0), vals.get("M", 0), vals.get("S", 0))
    except ValueError:
        return None


def describe_mapping(mapping, run_lens):
    """把映射转成人类可读的规则说明。"""
    names = {"Y": "年", "m": "月", "d": "日", "H": "时", "M": "分", "S": "秒"}
    parts = []
    for idx in sorted(mapping):
        off, compstr, yw = mapping[idx]
        if compstr == "EPOCH":
            parts.append(f"第{idx + 1}段({run_lens[idx]}位时间戳)")
            continue
        widths = _comp_widths(compstr, yw)
        cparts = [f"{names[c]}{w}位" for c, w in zip(compstr, widths)]
        pos_txt = f"第{off + 1}位起，" if off else ""
        parts.append(f"第{idx + 1}段({pos_txt}{', '.join(cparts)})")
    return "，".join(parts)


def parse_datetime_flexible(s):
    """解析用户输入的时间串（宽松格式），失败返回 None。"""
    s = s.strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M", "%Y%m%d_%H%M%S", "%Y%m%d %H%M%S", "%Y%m%d",
                "%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日 %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    if len(s) == 14 and s.isdigit():  # YYYYMMDDHHMMSS
        try:
            return datetime.datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                                     int(s[8:10]), int(s[10:12]), int(s[12:14]))
        except ValueError:
            return None
    return None


def apply_name_groups(groups, confirmed):
    """按用户确认的 {掩码: 时间} 批量从名称提取时间。

    返回 (result: {文件: datetime}, failed: [文件], rules: {掩码: 规则说明})。
    无法反推规则的组：仅示例照片使用确认时间，其余成员进入 failed。
    """
    result, failed, rules = {}, [], {}
    for mask, files in groups.items():
        dt = confirmed.get(mask)
        if dt is None:
            continue
        sample = files[0]
        stem = _stem(sample)
        run_lens = [len(r[2]) for r in digit_runs(stem)]
        mapping = learn_time_mapping(stem, dt)
        if not mapping:
            rules[mask] = "无法从名称反推数字含义，仅示例照片使用确认时间"
            result[sample] = dt
            failed.extend(f for f in files[1:])
            continue
        sample_t = apply_time_mapping(stem, mapping)
        note = "" if sample_t == dt else "（确认时间与名称数字不完全一致，按名称结构推断位置）"
        rules[mask] = describe_mapping(mapping, run_lens) + note
        for f in files:
            t = apply_time_mapping(_stem(f), mapping)
            if t is None:
                failed.append(f)
            else:
                result[f] = t
        result[sample] = dt  # 示例始终使用确认时间
    return result, failed, rules


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="photo_renamer",
        description="按 EXIF 拍摄时间批量重命名照片",
        epilog=("示例:\n"
                "  python photo_renamer.py D:\\Photos\n"
                "  python photo_renamer.py . -f \"IMG_%Y%m%d_%H%M%S\" -r -n\n"
                "  python photo_renamer.py . -f \"%Y\\\\%m\\\\%d_%H%M%S\" --action copy\n"
                "  python photo_renamer.py . -f \"%Y%m%d_%H%M%S_{orig}\""),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("dir", nargs="?", default=".",
                        help="要处理的目录（默认: 当前目录）")
    parser.add_argument("-f", "--format", default="%Y%m%d_%H%M%S",
                        help="命名格式（strftime 占位符 + {orig}/{ext}），默认: %(default)s")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="递归处理子目录")
    parser.add_argument("-e", "--ext", nargs="+", metavar="EXT",
                        help="要处理的扩展名（不含点，不区分大小写）；默认处理常见照片格式")
    parser.add_argument("--action", choices=("rename", "copy"), default="rename",
                        help="rename=直接重命名（默认）；copy=复制出新名称并保留原文件")
    parser.add_argument("--no-fallback", action="store_true",
                        help="无 EXIF 时不用文件修改时间代替，直接跳过")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="仅预览，不实际执行")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="跳过执行前的确认询问")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="输出更多信息")
    parser.add_argument("-j", "--jobs", type=int, default=1,
                        help="并行读取 EXIF 的线程数（0=按 CPU 核数自动，1=单线程；网络盘/机械硬盘可调大），默认: %(default)s")
    parser.add_argument("--no-color", action="store_true",
                        help="关闭彩色输出（默认仅终端显示时自动彩色）")
    parser.add_argument("--no-progress", action="store_true",
                        help="关闭识别进度百分比显示（默认仅终端显示时自动显示）")
    parser.add_argument("--name-fallback", action="store_true",
                        help="无 EXIF 的照片尝试从文件名识别拍摄时间（按名称类型分组确认；-y 时自动采用猜测值）")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    global _COLOR
    _COLOR = (not args.no_color) and sys.stdout.isatty()
    if _COLOR:
        _enable_vt()

    if not os.path.isdir(args.dir):
        print(f"错误: 目录不存在: {args.dir}", file=sys.stderr)
        return 1

    exts = {e.lower().lstrip(".") for e in args.ext} if args.ext else DEFAULT_EXTS

    if not (HAVE_PIL or HAVE_EXIFREAD):
        print("警告: 未安装 Pillow/exifread，无法读取 EXIF，将全部回退到文件修改时间。"
              "可执行: pip install Pillow exifread", file=sys.stderr)

    files = collect_files(args.dir, args.recursive, exts)
    print(f"发现 {len(files)} 个照片文件")
    if not files:
        return 0

    show_progress = (not args.no_progress) and sys.stderr.isatty() and len(files) >= 20

    def progress_cb(done, total):
        if show_progress:
            _print_progress(done, total)

    times = read_times(files, jobs=args.jobs,
                       progress=progress_cb if show_progress else None)
    if show_progress:
        _clear_progress_line()

    if args.name_fallback:
        noexif = [f for f in files if times.get(f) is None]
        if noexif:
            ordered = organize_groups(group_by_pattern(noexif))
            confirmed = {}
            for mask, fs in ordered:
                if mask == OTHER_MASK:
                    print(f"\n[其它] {len(fs)} 张单张照片未识别出时间，已并入「其它」分组，跳过")
                    continue
                sample = fs[0]
                guess = heuristic_guess_time(_stem(sample))
                if args.yes:  # 自动模式：直接采用猜测值
                    if guess:
                        confirmed[mask] = guess
                    continue
                hint = f"（自动猜测: {guess:%Y-%m-%d %H:%M:%S}）" if guess else "（未能自动猜测）"
                print(f"\n[{len(fs)} 张] 无 EXIF，示例: {os.path.basename(sample)}  类型: {mask}")
                ans = input(f"  输入时间 {hint}\n  回车=接受猜测，s=跳过该组，q=结束: ").strip()
                if ans.lower() == "q":
                    break
                if ans.lower() == "s":
                    continue
                if ans:
                    t = parse_datetime_flexible(ans)
                    if t is None:
                        print("  时间格式无效，该组跳过")
                        continue
                    confirmed[mask] = t
                elif guess:
                    confirmed[mask] = guess
            if confirmed:
                apply_groups = {m: fs for m, fs in ordered if m != OTHER_MASK}
                result, failed, rules = apply_name_groups(apply_groups, confirmed)
                for f, t in result.items():
                    times[f] = t
                for mask, rule in rules.items():
                    print(f"  [识别规则] {mask}: {rule}")
                if failed:
                    print(f"  [提示] {len(failed)} 个文件无法从名称提取时间")

    plan, skipped = build_plan(files, args.format, fallback=not args.no_fallback,
                               jobs=args.jobs, times=times)

    if args.verbose:
        print(f"EXIF 解析库: Pillow={'有' if HAVE_PIL else '无'}, "
              f"exifread={'有' if HAVE_EXIFREAD else '无'}")
        for src, reason in skipped:
            print(_c(f"  跳过  {src}: {reason}", _skip_color(reason)))

    verb = "预览" if args.dry_run else ("复制" if args.action == "copy" else "重命名")
    print(f"计划{verb} {len(plan)} 个文件，跳过 {len(skipped)} 个"
          + ("（重名时自动追加 _1/_2 后缀）" if plan else ""))
    for src, dst in plan:
        print(_c(f"  {os.path.basename(src)}  ->  {os.path.basename(dst)}", _COLOR_PLAN))

    if args.dry_run:
        print("（--dry-run 预览模式，未做任何改动）")
        return 0

    if not args.yes:
        try:
            answer = input("确认执行？[y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("已取消。")
            return 0

    ok, fail = apply_plan(plan, args.action, args.verbose)
    print(f"完成: 成功 {ok} 个，失败 {fail} 个。")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
