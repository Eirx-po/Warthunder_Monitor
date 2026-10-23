@echo off
title WT Power Curve
echo ========================================
echo   War Thunder Power Curve Recorder
echo ========================================
echo.
echo   Recording thrust/power vs altitude.
echo   Enter a battle and fly (full throttle
echo   climb gives the max-thrust curve).
echo.

REM Prefer the workspace venv .venv_wt: the managed python's site-packages
REM gets wiped periodically and then this tool cannot start (PyQt5 gone).
REM NOTE: this file must stay pure ASCII -- cmd.exe reads .bat with the
REM system ANSI codepage, so UTF-8 Chinese text gets parsed as commands.
set "PY=J:\Quant\.venv_wt\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
"%PY%" -u "J:\Quant\wt_power_curve.py"

if errorlevel 1 (
  echo.
  echo Exited with an error.
  pause
)
