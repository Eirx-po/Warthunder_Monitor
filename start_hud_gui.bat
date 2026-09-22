@echo off
title WT HUD Launcher
echo ========================================
echo   War Thunder HUD Launcher
echo ========================================
echo.
echo   Starting launcher window...
echo.

"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" -u "J:\Quant\wt_hud_launcher_gui.py"

if errorlevel 1 (
  echo.
  echo Launcher exited with an error.
  pause
)
