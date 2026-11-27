# Build the client into a single exe (optional).
#
# The exe contains ONLY the shell, not the opencode-pool kernel. The kernel is
# still started by the client via `python -m opencode_pool serve` -- it needs
# `requests` and live-editable config, so bundling it would mean "repackage for
# every one-line config change".
#
# NOTE: this file must stay pure ASCII. Windows PowerShell 5.1 reads .ps1 files
# as ANSI (cp936 on Chinese Windows), so UTF-8 Chinese text breaks the parser.
#
# Usage:
#   pwsh -File build_exe.ps1
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
#   pwsh -File build_exe.ps1 -OneFile        # single file (slower start)

param(
    [switch]$OneFile,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
# Native commands (python/pip/pyinstaller) do not honour $ErrorActionPreference,
# so check their exit codes explicitly.
function Assert-LastExit([string]$What) {
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $here

Write-Host "[1/4] generating icon ..."
& $Python -c "import sys; sys.path.insert(0,'.'); from opencodepool_client import icon; print(icon.build_ico(r'app.ico'))"
Assert-LastExit "icon generation"

Write-Host "[2/4] installing/updating PyInstaller ..."
& $Python -m pip install --quiet --upgrade pyinstaller
Assert-LastExit "PyInstaller install"

Write-Host "[3/4] packaging ..."
$pyArgs = @(
    "--noconfirm", "--clean", "--windowed",
    "--name", "OpenCodePoolClient",
    "--icon", "app.ico",
    "--paths", ".",
    "--collect-submodules", "opencodepool_client"
)
if ($OneFile) { $pyArgs += "--onefile" }
$pyArgs += "run_client.py"
& $Python -m PyInstaller @pyArgs
Assert-LastExit "PyInstaller build"

Write-Host "[4/4] done"
if ($OneFile) {
    Write-Host "  output: dist\OpenCodePoolClient.exe"
} else {
    Write-Host "  output: dist\OpenCodePoolClient\OpenCodePoolClient.exe"
}
Write-Host ""
Write-Host "On first run, check Settings:"
Write-Host "  - project dir = the opencode-pool repo root (the level that contains opencode_pool\)"
Write-Host "  - python      = an interpreter that has requests installed"
Write-Host "                  (the exe cannot host the relay itself; set it explicitly)"
