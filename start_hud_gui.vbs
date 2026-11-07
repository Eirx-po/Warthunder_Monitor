' War Thunder HUD Launcher - console-free starter.
' Double-click this file: no cmd window, no taskbar button.
' The GUI writes its own traceback to wt_gui_error.log (pythonw has no console).
Option Explicit

Dim sh, fso, here, pyw, scriptPath, logPath, q, cmdLine, rc
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
q = Chr(34)

' Prefer the persistent workspace venv (managed python's site-packages
' gets wiped periodically, which silently kills PyQt5).
pyw = here & "\.venv_wt\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then
    pyw = "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
End If
scriptPath = here & "\wt_hud_launcher_gui.py"
logPath = here & "\wt_gui_error.log"

' 0 = hidden window; True = wait so we can show the log if the GUI crashes.
cmdLine = q & pyw & q & " -u " & q & scriptPath & q
rc = sh.Run(cmdLine, 0, True)

If rc <> 0 Then
    MsgBox "Launcher exited with an error (code " & rc & ")." & vbCrLf & vbCrLf _
           & ReadTail(logPath), vbCritical, "WT HUD Launcher"
End If

Function ReadTail(path)
    Dim f, txt
    ReadTail = "(no log file)"
    On Error Resume Next
    If Not fso.FileExists(path) Then Exit Function
    Set f = fso.OpenTextFile(path, 1, False)
    If Err.Number <> 0 Then Exit Function
    txt = f.ReadAll
    f.Close
    If Len(txt) > 2000 Then txt = Right(txt, 2000)
    ReadTail = txt
End Function
