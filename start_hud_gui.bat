@echo off
title WT HUD Launcher
echo ========================================
echo   War Thunder HUD Launcher
echo ========================================
echo.
echo   Starting launcher window...
echo.

REM 用 pythonw.exe：不弹黑色控制台窗口（守护进程和 HUD 也都是无窗口的）
"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe" -u "J:\Quant\wt_hud_launcher_gui.py"

if errorlevel 1 (
  echo.
  echo Launcher exited with an error.
  pause
)
