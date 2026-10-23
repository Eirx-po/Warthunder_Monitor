@echo off
title WT HUD Launcher
echo ========================================
echo   War Thunder HUD Launcher
echo ========================================
echo.
echo   Starting launcher window...
echo.

REM Use pythonw.exe: no black console window.
REM The daemon and the HUD sub-processes are windowless too.
REM NOTE: this file must stay pure ASCII -- cmd.exe reads .bat with the
REM system ANSI codepage, so UTF-8 Chinese text gets parsed as commands.
REM Prefer the workspace venv .venv_wt: the managed python's site-packages
REM gets wiped periodically, which silently kills the GUI (PyQt5 gone).
set "PYW=J:\Quant\.venv_wt\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
REM pythonw has no console, so a traceback would be swallowed silently.
REM Redirect to a log file and dump it on failure so the real error is visible.
set "GUILOG=J:\Quant\wt_gui_error.log"
"%PYW%" -u "J:\Quant\wt_hud_launcher_gui.py" 1>"%GUILOG%" 2>&1

if errorlevel 1 (
  echo.
  echo Launcher exited with an error. Last lines of %GUILOG%:
  echo ----------------------------------------
  type "%GUILOG%"
  echo ----------------------------------------
  pause
)
