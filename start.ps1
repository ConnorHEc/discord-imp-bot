param([switch]$Check)
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot -ErrorAction Stop
$taskPythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPythonPath)) {
    $taskPyLauncher = Get-Command py -ErrorAction SilentlyContinue
    $taskPython = Get-Command python -ErrorAction SilentlyContinue
    $taskPythonFound = $false
    if ($taskPyLauncher) {
        foreach ($taskVersion in @('-3.14', '-3.13', '-3.12', '-3.11', '-3.10')) {
            & $taskPyLauncher.Source $taskVersion -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' *> $null
            if ($LASTEXITCODE -eq 0) {
                & $taskPyLauncher.Source $taskVersion -m venv .venv
                if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python virtual environment.' }
                $taskPythonFound = $true
                break
            }
        }
    }
    if (-not $taskPythonFound -and $taskPython) {
        & $taskPython.Source -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' *> $null
        if ($LASTEXITCODE -eq 0) {
            & $taskPython.Source -m venv .venv
            if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python virtual environment.' }
            $taskPythonFound = $true
        }
    }
    if (-not $taskPythonFound) { throw 'Please install Python 3.10 or newer, then run this script again.' }
}
& $taskPythonPath -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if ($Check) {
    & $taskPythonPath bot.py --check
    exit $LASTEXITCODE
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.env'))) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '.env.example') -Destination (Join-Path $PSScriptRoot '.env') -ErrorAction Stop
    Write-Host 'Created .env. Fill DISCORD_BOT_TOKEN, STRATZ_API_TOKEN and DISCORD_GUILD_ID locally, then run again.'
    exit 1
}
& $taskPythonPath bot.py
exit $LASTEXITCODE
