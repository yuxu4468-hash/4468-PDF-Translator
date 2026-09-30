@echo off
chcp 65001 >nul
title PDF 文档翻译器
cd /d "%~dp0"

echo ============================================
echo   PDF 文档翻译器 —— 基于 AI 大模型的 PDF 翻译
echo ============================================
echo.

REM 把临时目录指向项目内的 temp，避免系统临时目录权限问题
if not exist "%~dp0temp" mkdir "%~dp0temp"
set "TMP=%~dp0temp"
set "TEMP=%~dp0temp"
set "TMPDIR=%~dp0temp"

REM 控制台按 UTF-8 输出，避免中文乱码
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

REM 优先使用项目内的虚拟环境（如果存在）
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON=%~dp0.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

REM 检查 Python 是否可用
"%PYTHON%" --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 没有找到 Python。
    echo.
    echo 请先安装 Python 3.9 或更高版本： https://www.python.org/downloads/
    echo 安装时请勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

REM 检查依赖是否齐全，缺失时自动安装
"%PYTHON%" -c "import fitz, flask, requests" >nul 2>&1
if errorlevel 1 (
    echo [提示] 检测到缺少运行依赖，正在自动安装……
    echo.
    "%PYTHON%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败。请手动执行：
        echo     pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo.
)

REM OCR（扫描版 PDF）依赖：可选，缺失时补装，不影响普通 PDF 翻译
"%PYTHON%" -c "import onnxruntime, numpy" >nul 2>&1
if errorlevel 1 (
    echo [提示] 未检测到 OCR 依赖，正在安装 onnxruntime 与 numpy……
    echo         （用于识别扫描版 PDF；不装的话扫描件无法翻译）
    echo.
    "%PYTHON%" -m pip install onnxruntime numpy
    if errorlevel 1 (
        echo.
        echo [警告] OCR 依赖安装失败，扫描版 PDF 将无法翻译。
        echo         普通（有文本层的）PDF 不受影响，可以继续使用。
        echo.
    )
    echo.
)

REM 提示 OCR 模型状态（首次使用需要联网下载，约 21 MB）
"%PYTHON%" -m src.main --ocr-status 2>nul | findstr /C:"就绪    : 否" >nul 2>&1
if not errorlevel 1 (
    echo [提示] 还没有下载 OCR 模型，扫描版 PDF 暂时无法翻译。
    echo         需要时执行：  python -m src.main --download-ocr ch
    echo.
)

echo 正在启动服务，浏览器会自动打开界面……
echo 若未自动打开，请手动访问：  http://127.0.0.1:8765/
echo.
echo 关闭本窗口即可停止服务。
echo.

"%PYTHON%" -m src.main %*

echo.
echo 服务已停止。
pause
