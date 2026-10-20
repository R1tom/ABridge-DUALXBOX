# ABridge DUALXBOX Configuration installer: checks the drivers, creates a private Python environment next
# to this file, installs the dependencies, adds Desktop / Start menu shortcuts and opens the app.
# Run it through install.bat, or:  powershell -ExecutionPolicy Bypass -File install.ps1
param([switch]$NoShortcuts)

$name = "ABridge DUALXBOX Configuration"
$here = $PSScriptRoot
$venvPy = Join-Path $here "venv\Scripts\python.exe"
$pyw = Join-Path $here "venv\Scripts\pythonw.exe"

# drivers: ViGEmBus is required (virtual Xbox pads), HidHide is strongly recommended (no double input)
$ok = $true
if (-not (Get-Service ViGEmBus -ErrorAction SilentlyContinue)) {
    Write-Host "ViGEmBus is NOT installed (required)." -ForegroundColor Red
    Write-Host "  Install it from https://github.com/nefarius/ViGEmBus/releases, then run this again."
    $ok = $false
} else { Write-Host "ViGEmBus: installed" -ForegroundColor Green }
if (-not (Test-Path "C:\Program Files\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe")) {
    Write-Host "HidHide is not installed (recommended): games will see the real controller AND the virtual one." -ForegroundColor Yellow
    Write-Host "  Install it from https://github.com/nefarius/HidHide/releases and restart the PC."
} else { Write-Host "HidHide: installed" -ForegroundColor Green }
if (-not $ok) { exit 1 }

if (-not (Test-Path $venvPy)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $py = "py"; $pyArgs = @("-3.12") }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $py = "python"; $pyArgs = @() }
    else {
        Write-Host "Python 3.12 was not found." -ForegroundColor Red
        Write-Host "Install it from https://www.python.org/downloads/ (64-bit, tick 'Add python.exe to PATH'), then run this again."
        exit 1
    }
    Write-Host "Creating the Python environment..."
    & $py @pyArgs -m venv (Join-Path $here "venv")
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) {
        Write-Host "Could not create the environment. Python 3.12 (64-bit) is needed: https://www.python.org/downloads/" -ForegroundColor Red
        exit 1
    }
}

Write-Host "Installing dependencies..."
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $here "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Host "Dependency install failed (see above)." -ForegroundColor Red; exit 1 }

if (-not $NoShortcuts) {
    $ws = New-Object -ComObject WScript.Shell
    foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
        $s = $ws.CreateShortcut((Join-Path $dir "$name.lnk"))
        $s.TargetPath = $pyw
        $s.Arguments = "`"$(Join-Path $here 'ps5toxbox.pyw')`""
        $s.WorkingDirectory = $here
        $s.Save()
    }
}

Write-Host "Installed. Opening $name..." -ForegroundColor Green
Write-Host "Turn on 'Start with Windows' in the app's Settings tab if you want it running at logon."
Start-Process $pyw "`"$(Join-Path $here 'ps5toxbox.pyw')`"" -WorkingDirectory $here
