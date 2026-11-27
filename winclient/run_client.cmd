@echo off
rem 双击启动 opencode-pool 的 Windows 客户端（无控制台窗口）。
rem 优先 pythonw；没有就用 py -3w；再没有就退回 python（会带一个黑框）。
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "run_client.py" %*
    exit /b 0
)

where py >nul 2>nul
if %errorlevel%==0 (
    start "" py -3w "run_client.py" %*
    exit /b 0
)

echo 没找到 pythonw / py，改用 python（会保留一个控制台窗口）。
python "run_client.py" %*
