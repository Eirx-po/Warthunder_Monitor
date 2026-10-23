$title = "War Thunder HUD v2"
Write-Host "========================================"
Write-Host "  $title"
Write-Host "========================================"
Write-Host ""
Write-Host "  1. Game must be windowed or borderless"
Write-Host "  2. HUD transparent / topmost / click-through"
Write-Host "  3. Ctrl+C to exit"
Write-Host ""
Write-Host "Starting..." -ForegroundColor Green
Write-Host ""
# Prefer the workspace venv: the managed python's site-packages gets wiped
# periodically, and then the HUD cannot start (PyQt5 missing).
$py = "J:\Quant\.venv_wt\Scripts\python.exe"
if (-not (Test-Path $py)) {
  $py = "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
}
& $py -u "J:\Quant\wt_hud_v2.py"
