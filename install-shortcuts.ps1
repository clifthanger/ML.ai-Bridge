# Creates the one-click shortcuts for the ML AI Code console.
#   - Desktop shortcut (always)
#   - Startup shortcut (only with -Startup, so it launches at login)
param([switch]$Startup)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat  = Join-Path $here 'ML AI Code.bat'

$py = @(
  "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
  "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
  "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

function New-Lnk([string]$path) {
  $w = New-Object -ComObject WScript.Shell
  $s = $w.CreateShortcut($path)
  $s.TargetPath       = $bat
  $s.WorkingDirectory = $here
  $s.Description      = 'ML AI Code bridge console'
  # deliberately NOT the python icon — the shortcut is the app, not a script host
  $s.IconLocation     = "$env:SystemRoot\System32\imageres.dll,15"
  $s.Save()
  Write-Host "  created $path"
}

$desktop = [Environment]::GetFolderPath('Desktop')
New-Lnk (Join-Path $desktop 'ML AI Code.lnk')

if ($Startup) {
  $startup = [Environment]::GetFolderPath('Startup')
  New-Lnk (Join-Path $startup 'ML AI Code Console.lnk')
}
