@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  照片重命名工具 - 一键打包
echo  版本号取自源码中的 __version__
echo ============================================
python build_exe.py
if errorlevel 1 (
    echo.
    echo 打包失败！请确认已安装: pip install pyinstaller
) else (
    echo.
    echo 打包成功，exe 已生成到 dist 文件夹。
)
pause
