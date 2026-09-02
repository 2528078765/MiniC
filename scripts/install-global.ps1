param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvScripts = Join-Path $ProjectRoot ".venv\Scripts"
$Python = Join-Path $VenvScripts "python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "Error: python not found at $Python" -ForegroundColor Red
    exit 1
}

$CommandArgs = @("-m", "minic.cli.global_install", "--venv-scripts", $VenvScripts)
if ($Uninstall) {
    $CommandArgs += "--uninstall"
}

& $Python @CommandArgs
exit $LASTEXITCODE
