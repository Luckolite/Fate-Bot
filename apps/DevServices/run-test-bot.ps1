param(
    [string]$TokenPath
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if ($TokenPath) {
    $env:FATE_TOKEN_PATH = $TokenPath
} elseif (-not $env:FATE_TOKEN_PATH) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $env:FATE_TOKEN_PATH = Join-Path $desktop "token.txt"
}

if (-not (Test-Path -LiteralPath $env:FATE_TOKEN_PATH -PathType Leaf)) {
    throw "Discord token file not found: $env:FATE_TOKEN_PATH"
}
$env:FATE_TOKEN_PATH = (Resolve-Path -LiteralPath $env:FATE_TOKEN_PATH).Path

Set-Location -LiteralPath $root

& (Join-Path $PSScriptRoot "start-databases.ps1")

$env:FATE_CONFIG_PATH = Join-Path $root "data\config.test.json"
$env:FATE_AUTH_PATH = Join-Path $root "data\auth.test.json"

Write-Host "Starting Fate with the external token file and isolated test databases..."
& (Join-Path $root "venv\Scripts\python.exe") (Join-Path $root "fate.py")
