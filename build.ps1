# Builds dist\ArtSafe\ArtSafe.exe. Run from this folder: powershell -File build.ps1
$ErrorActionPreference = "Stop"
$py = ".venv\Scripts\python.exe"

# Error-report webhook: kept outside the (public) repo, written into the build only.
$hook = Join-Path $env:USERPROFILE ".artsafe\webhook.txt"
if (Test-Path $hook) {
    $url = (Get-Content $hook -Raw).Trim()
    Set-Content artsafe\_secrets.py "DISCORD_WEBHOOK = `"$url`"" -Encoding ascii
    Write-Host "Error reports: on"
} else {
    Remove-Item artsafe\_secrets.py -ErrorAction SilentlyContinue
    Write-Host "Error reports: off (no $hook)"
}

# app icon (.ico) drawn from the same shield used in the app
@'
import sys
from PySide6.QtWidgets import QApplication
from artsafe.gui import make_icon
app = QApplication(sys.argv)
make_icon(256).pixmap(256, 256).save("icon.png")
from PIL import Image
Image.open("icon.png").save("icon.ico", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
'@ | & $py -

& $py -m PyInstaller --noconfirm --clean --windowed --name ArtSafe --icon icon.ico `
    --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets `
    --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick --exclude-module PySide6.QtPdf `
    --exclude-module tkinter --exclude-module pytest --exclude-module pywinauto `
    --hidden-import artsafe._secrets `
    main.py
Remove-Item icon.png
Write-Host "Built: dist\ArtSafe\ArtSafe.exe"
