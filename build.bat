@echo off
chcp 65001 >nul
echo ============================================
echo  PSD Viewer - 打包为 exe
echo ============================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

REM 安装依赖
echo [1/3] 安装依赖包...
pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 安装依赖失败
    pause
    exit /b 1
)

REM 运行 PyInstaller 打包
echo.
echo [2/3] 开始打包...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "PSD_Viewer" ^
    --icon "assets\icon.ico" ^
    --add-data "src;src" ^
    --hidden-import psd_tools ^
    --hidden-import psd_tools.composite ^
    --hidden-import PIL ^
    --hidden-import PIL.ImageQt ^
    --collect-all psd_tools ^
    src\main.py

if errorlevel 1 (
    echo [错误] 打包失败，请检查上方日志
    pause
    exit /b 1
)

echo.
echo [3/3] 打包完成！
echo 可执行文件位于: dist\PSD_Viewer.exe
echo.
pause
