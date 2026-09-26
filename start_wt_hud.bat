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
REM Run with pythonw.exe: daemon mode stays in background, no console window.
REM Runtime status is written to wt_hud_*.log
REM NOTE: this file must stay pure ASCII -- cmd.exe reads .bat with the
REM system ANSI codepage, so UTF-8 Chinese text gets parsed as commands.
"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe" "J:\Quant\wt_hud_launcher.py" --watch
echo.
pause
