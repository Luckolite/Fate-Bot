$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$serviceRoot = Join-Path $root ".fate-test-services"
$runRoot = Join-Path $serviceRoot "run"

foreach ($service in @("mysql", "mongo")) {
    $pidFile = Join-Path $runRoot "$service.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) {
        continue
    }

    $savedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $process = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
    if ($process -and $process.ProcessName -in @("mysqld", "mongod")) {
        Stop-Process -Id $savedPid
        [void]$process.WaitForExit(10000)
        Write-Host "Stopped $service (PID $savedPid)"
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}
