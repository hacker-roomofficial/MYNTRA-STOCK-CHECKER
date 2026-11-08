# Local development launcher — no credentials stored on disk.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not $env:STOCKWATCH_DATA_DIR) { $env:STOCKWATCH_DATA_DIR = Join-Path $PSScriptRoot 'data' }
$env:PYTHONUTF8 = '1'
if (-not (Test-Path (Join-Path $env:STOCKWATCH_DATA_DIR 'platform.db'))) {
  Write-Host 'First-time administrator setup:' -ForegroundColor Cyan
  $env:PLATFORM_ADMIN_USER = Read-Host 'Choose admin username'
  $secure = Read-Host 'Choose a strong admin password (12+ characters)' -AsSecureString
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try { $env:PLATFORM_ADMIN_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
  if ($env:PLATFORM_ADMIN_PASSWORD.Length -lt 12) { throw 'Password must have at least 12 characters.' }
}
Write-Host 'Open http://127.0.0.1:8080/login in your browser.' -ForegroundColor Green
python main.py dashboard --host 127.0.0.1 --port 8080
