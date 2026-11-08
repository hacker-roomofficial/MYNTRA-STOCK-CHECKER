# Run this once in PowerShell from the extracted project folder.
$ErrorActionPreference='Stop'
Set-Location $PSScriptRoot
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies installation failed.' }
python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Playwright Chromium installation failed.' }
Write-Host 'Installation finished. Next: .\run_windows.ps1' -ForegroundColor Green
