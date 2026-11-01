# Installs Claude Duo on Windows from this folder: Python packages, a Start menu shortcut, then starts it.
# Run in PowerShell:   powershell -ExecutionPolicy Bypass -File .\install.ps1
# (Prefer no Python? Download "Claude-Duo-Windows.exe" from the GitHub Releases page instead.)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

# Python 3.12 if the launcher has it (the window library supports up to 3.13), else whatever Python is installed
$python = $null
foreach ($try in @(@("py", "-3.12"), @("py", "-3.13"), @("py", "-3"), @("python"))) {
    try { & $try[0] $try[1..9] -c "import sys; assert (3,10) <= sys.version_info[:2] <= (3,13)" 2>$null; if ($LASTEXITCODE -eq 0) { $python = $try; break } } catch {}
}
if (-not $python) { Write-Host "Install Python 3.12 from https://www.python.org/downloads/windows/ first (tick 'Add python.exe to PATH')."; exit 1 }

Write-Host "Setting up Python packages..."
& $python[0] $python[1..9] -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -r requirements-windows.txt

# an icon for the shortcut, made from the app icon
& .\.venv\Scripts\python.exe -c "from PIL import Image; Image.open('iphone/icon-512.png').save('claude-duo.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"

$lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "Claude Duo.lnk"
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = Join-Path $here ".venv\Scripts\pythonw.exe"
$s.Arguments = "`"$(Join-Path $here 'winapp.py')`""
$s.WorkingDirectory = $here
$s.IconLocation = Join-Path $here "claude-duo.ico"
$s.Description = "Your Claude plan usage in the system tray"
$s.Save()

Write-Host "Installed. Find 'Claude Duo' in the Start menu; starting it now (look for the icon by the clock)."
Start-Process -FilePath (Join-Path $here ".venv\Scripts\pythonw.exe") -ArgumentList "`"$(Join-Path $here 'winapp.py')`"" -WorkingDirectory $here
