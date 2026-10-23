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
REM Prefer the workspace venv .venv_wt: the managed python's site-packages
REM gets wiped periodically and then the HUD cannot start (PyQt5 gone).
set "PYW=J:\Quant\.venv_wt\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
"%PYW%" "J:\Quant\wt_hud_launcher.py" --watch
echo.
pause
