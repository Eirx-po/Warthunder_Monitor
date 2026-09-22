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

"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" -u "J:\Quant\wt_power_curve.py"

if errorlevel 1 (
  echo.
  echo Exited with an error.
  pause
)
