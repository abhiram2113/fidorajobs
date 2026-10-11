$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (!(Test-Path '.env')) { Copy-Item '.env.example' '.env'; Write-Host 'Edit .env and set ADMIN_PASSWORD and APP_SECRET, then run again.' -ForegroundColor Yellow; notepad '.env'; exit }
py app.py
