#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键打包工具：把命令行版与图形界面版打包成独立的 exe。

用法:
    python build_exe.py

产物（在 dist 目录下）:
    photo_renamer_v<版本>.exe       命令行版（带控制台窗口）
    photo_renamer_gui_v<版本>.exe   图形界面版（无控制台窗口）

版本号自动从 photo_renamer.py / photo_renamer_gui.py 中的 __version__ 读取，
每次发布新版本只需修改源码中的版本号，再运行本脚本即可。

依赖: pip install pyinstaller
"""

import os
import re
import subprocess
import sys

# 控制台可能是 GBK 等旧编码，特殊符号(✔ 等)打印会崩溃；统一容错输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
WORK = os.path.join(HERE, "build")
ICON = os.path.join(HERE, "assets", "icon.ico")


def read_version(path):
    """从源码中读取 __version__ 常量。"""
    with open(path, encoding="utf-8") as f:
        m = re.search(r'__version__\s*=\s*"([^"]+)"', f.read())
    return m.group(1) if m else "0.0.0"


def generate_icon():
    """用 Pillow 生成一个相机造型的程序图标。"""
    from PIL import Image, ImageDraw
    os.makedirs(os.path.dirname(ICON), exist_ok=True)
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([24, 72, 232, 224], radius=28, fill=(32, 96, 168))   # 机身
    d.rounded_rectangle([72, 44, 184, 96], radius=18, fill=(32, 96, 168))    # 顶部凸起
    d.ellipse([150, 54, 190, 94], fill=(222, 62, 54))                        # 快门键
    d.ellipse([72, 104, 184, 216], fill=(238, 243, 248))                     # 镜头外圈
    d.ellipse([98, 130, 158, 190], fill=(36, 58, 92))                        # 镜片
    d.ellipse([114, 146, 142, 174], fill=(118, 160, 210))                    # 镜片高光
    d.rectangle([40, 190, 216, 208], fill=(32, 96, 168))                     # 底部挡板
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                          (64, 64), (128, 128), (256, 256)])
    print("已生成图标:", ICON)


def build(target, windowed):
    """打包单个入口脚本。返回输出的 exe 路径。"""
    version = read_version(os.path.join(HERE, target))
    name = target.replace(".py", "")
    exe_name = f"{name}_v{version}"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", exe_name,
        "--distpath", DIST,
        "--workpath", WORK,
        "--specpath", WORK,
        "--icon", ICON,
    ]
    if windowed:
        cmd.append("--windowed")  # GUI 版不弹控制台窗口
    cmd.append(os.path.join(HERE, target))
    print(">>>", " ".join(cmd))
    subprocess.run(cmd, check=True)
    out = os.path.join(DIST, f"{exe_name}.exe")
    print(f"[OK] 打包完成: {out}\n")
    return out


def main():
    if not os.path.exists(ICON):
        generate_icon()
    os.makedirs(DIST, exist_ok=True)
    outs = [
        build("photo_renamer.py", windowed=False),      # 命令行版
        build("photo_renamer_gui.py", windowed=True),   # 图形界面版
    ]
    print("=" * 60)
    print("全部打包完成！产物如下：")
    for o in outs:
        print("  ", o)


if __name__ == "__main__":
    main()
