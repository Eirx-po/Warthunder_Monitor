@echo off
REM Console-free launcher: hand off to the VBS wrapper and exit immediately.
REM For ZERO console flash, double-click start_hud_gui.vbs directly.
REM The VBS runs pythonw hidden and reports tracebacks from wt_gui_error.log.
start "" wscript.exe "%~dp0start_hud_gui.vbs"
exit /b
