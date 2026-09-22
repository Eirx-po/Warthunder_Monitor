@echo off
title WT HUD Launcher
echo ========================================
echo   War Thunder HUD One-Click Launcher
echo ========================================
echo.
echo   Auto-detect game mode and start HUD
echo   Ctrl+C to exit
echo.
echo Starting...
echo.
REM 用 pythonw.exe：守护模式后台静默运行，不弹控制台窗口。
REM 运行状态写在 wt_hud_*.log 里。
"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe" "J:\Quant\wt_hud_launcher.py" --watch
echo.
pause
