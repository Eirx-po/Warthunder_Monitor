@echo off
title WT HUD
echo ========================================
echo   War Thunder HUD v2
echo ========================================
echo.
echo   1. Game must be windowed or borderless
echo   2. HUD is transparent / topmost / click-through
echo   3. Press Ctrl+C to exit
echo.
echo Starting...
echo.
REM Prefer the workspace venv .venv_wt: the managed python's site-packages
REM gets wiped periodically and then the HUD cannot start (PyQt5 gone).
REM NOTE: this file must stay pure ASCII -- cmd.exe reads .bat with the
REM system ANSI codepage, so UTF-8 Chinese text gets parsed as commands.
set "PY=J:\Quant\.venv_wt\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
"%PY%" -u "J:\Quant\wt_hud_v2.py"
echo.
echo HUD exited.
pause
