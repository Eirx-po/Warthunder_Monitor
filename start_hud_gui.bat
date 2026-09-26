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
"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe" -u "J:\Quant\wt_hud_launcher_gui.py"

if errorlevel 1 (
  echo.
  echo Launcher exited with an error.
  pause
)
