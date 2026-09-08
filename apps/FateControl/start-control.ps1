param(
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$repository = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$bundledPython = Join-Path $repository "venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $bundledPython) {
    $bundledPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}
$configuredControl = $null
$configPath = Join-Path $repository "data\config.json"
if (Test-Path -LiteralPath $configPath) {
    try {
        $configuredControl = (Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json).control_panel
    }
    catch {
        Write-Warning "Could not read control_panel settings from config.json; using defaults."
    }
}
$controlPort = if ($env:FATE_CONTROL_PORT) {
    [int]$env:FATE_CONTROL_PORT
} elseif ($configuredControl -and $configuredControl.port) {
    [int]$configuredControl.port
} else {
    16421
}
$controlHost = if ($env:FATE_CONTROL_HOST) {
    $env:FATE_CONTROL_HOST
} elseif ($configuredControl -and $configuredControl.host) {
    [string]$configuredControl.host
} else {
    "0.0.0.0"
}
$healthHost = if ($controlHost -in @("0.0.0.0", "::", "[::]")) {
    "127.0.0.1"
} else {
    $controlHost
}

function Test-FateControlRunning {
    for ($attempt = 0; $attempt -lt 2; $attempt++) {
        try {
            $status = Invoke-RestMethod `
                -Uri "http://${healthHost}:$controlPort/" `
                -Method Get `
                -TimeoutSec 3 `
                -ErrorAction Stop
            return $status.service -eq "fate-control" -and $status.controller_online -eq $true
        }
        catch {
            if ($attempt -eq 0) {
                Start-Sleep -Milliseconds 250
            }
        }
    }
    return $false
}

function Test-ControlPortListening {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connected = $client.ConnectAsync("127.0.0.1", $controlPort).Wait(500)
        return $connected -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

# Task Scheduler's MultipleInstances setting does not cover someone launching
# this wrapper manually. The mutex prevents two updated wrappers from racing,
# while the health check also recognizes a controller started by an older
# wrapper or directly with Python.
$mutex = [System.Threading.Mutex]::new($false, "Local\FateControlService")
$ownsMutex = $false
try {
    try {
        $ownsMutex = $mutex.WaitOne(0, $false)
    }
    catch [System.Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }

    if (-not $ownsMutex) {
        Write-Output "FateControl supervisor is already running."
        exit 0
    }
    if (Test-FateControlRunning) {
        if ($Once) {
            Write-Output "FateControl is already listening on port $controlPort."
            exit 0
        }
        Write-Output (
            "FateControl is already listening on port $controlPort; " +
            "the scheduled supervisor will take over if that instance exits."
        )
        do {
            Start-Sleep -Seconds 5
        } while (Test-FateControlRunning)
    }
    if (Test-ControlPortListening) {
        [Console]::Error.WriteLine(
            "Port $controlPort is occupied by another service; FateControl was not started."
        )
        exit 1
    }

    Set-Location -LiteralPath $repository
    do {
        & $python -m apps.FateControl.server
        $exitCode = $LASTEXITCODE
        if ($Once -or $exitCode -eq 0) {
            exit $exitCode
        }
        if (Test-FateControlRunning) {
            Write-Output "FateControl is already listening on port $controlPort."
            exit 0
        }
        if (Test-ControlPortListening) {
            [Console]::Error.WriteLine(
                "Port $controlPort is occupied by another service; FateControl will not restart."
            )
            exit $exitCode
        }
        Write-Warning "FateControl exited with code $exitCode. Restarting in 5 seconds."
        Start-Sleep -Seconds 5
    } while ($true)
}
finally {
    if ($ownsMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
