$ErrorActionPreference = "Stop"

function Wait-ForPort {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutSeconds = 45
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $task = $client.ConnectAsync($HostName, $Port)
            if ($task.Wait(500) -and $client.Connected) {
                return
            }
        } catch {
            # The server may still be starting.
        } finally {
            $client.Dispose()
        }
        Start-Sleep -Milliseconds 300
    }
    throw "Timed out waiting for ${HostName}:$Port"
}

function Test-ProcessFile {
    param([string]$PidFile)
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return $false
    }
    $savedPid = [int](Get-Content -LiteralPath $PidFile -Raw)
    return $null -ne (Get-Process -Id $savedPid -ErrorAction SilentlyContinue)
}

function Start-BackgroundService {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$PidFile
    )

    $launcherArguments = @(
        (Join-Path $PSScriptRoot "launch_background.py"),
        "--cwd", $root,
        "--pid-file", $PidFile,
        "--", $FilePath
    ) + $ArgumentList
    & $python @launcherArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to launch background service: $FilePath"
    }
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root "venv\Scripts\python.exe"
$serviceRoot = Join-Path $root ".fate-test-services"
$mysqlRoot = Join-Path $serviceRoot "mysql-8.4.9-winx64\mysql-8.4.9-winx64"
$mongoRoot = Join-Path $serviceRoot "mongodb-windows-x86_64-8.0.16\mongodb-win32-x86_64-windows-8.0.16"
$mysqlExe = Join-Path $mysqlRoot "bin\mysqld.exe"
$mongoExe = Join-Path $mongoRoot "bin\mongod.exe"

if (-not (Test-Path -LiteralPath $mysqlExe) -or -not (Test-Path -LiteralPath $mongoExe)) {
    & (Join-Path $PSScriptRoot "install-databases.ps1")
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Fate's virtual-environment Python was not found: $python"
}

$mysqlData = Join-Path $serviceRoot "data\mysql"
$mongoData = Join-Path $serviceRoot "data\mongo"
$logRoot = Join-Path $serviceRoot "logs"
$runRoot = Join-Path $serviceRoot "run"
New-Item -ItemType Directory -Force -Path $mysqlData, $mongoData, $logRoot, $runRoot | Out-Null

$mysqlConfig = Join-Path $serviceRoot "mysql.ini"
$mysqlDataIni = $mysqlData.Replace("\", "/")
$mysqlBaseIni = $mysqlRoot.Replace("\", "/")
@"
[mysqld]
basedir=$mysqlBaseIni
datadir=$mysqlDataIni
port=3307
bind-address=127.0.0.1
mysqlx=0
character-set-server=utf8mb4
collation-server=utf8mb4_unicode_ci
log-error=$($logRoot.Replace("\", "/"))/mysql.log

[client]
host=127.0.0.1
port=3307
protocol=tcp
"@ | Set-Content -LiteralPath $mysqlConfig -Encoding ASCII

$mysqlSystemTable = Join-Path $mysqlData "mysql"
if (-not (Test-Path -LiteralPath $mysqlSystemTable)) {
    Write-Host "Initializing MySQL data directory..."
    & $mysqlExe "--defaults-file=$mysqlConfig" --initialize-insecure
    if ($LASTEXITCODE -ne 0) {
        throw "MySQL initialization failed. See $logRoot\mysql.log"
    }
}

$mysqlPidFile = Join-Path $runRoot "mysql.pid"
if (-not (Test-ProcessFile $mysqlPidFile)) {
    Start-BackgroundService `
        -FilePath $mysqlExe `
        -ArgumentList @("--defaults-file=$mysqlConfig") `
        -PidFile $mysqlPidFile
}
Wait-ForPort -HostName "127.0.0.1" -Port 3307

$mongoPidFile = Join-Path $runRoot "mongo.pid"
if (-not (Test-ProcessFile $mongoPidFile)) {
    $mongoLog = Join-Path $logRoot "mongo.log"
    Start-BackgroundService `
        -FilePath $mongoExe `
        -ArgumentList @(
            "--dbpath", $mongoData,
            "--port", "27018",
            "--bind_ip", "127.0.0.1",
            "--logpath", $mongoLog,
            "--logappend"
        ) `
        -PidFile $mongoPidFile
}
Wait-ForPort -HostName "127.0.0.1" -Port 27018

& (Join-Path $root "venv\Scripts\python.exe") (Join-Path $PSScriptRoot "setup-test-config.py")
if ($LASTEXITCODE -ne 0) {
    throw "Test database bootstrap failed."
}

Write-Host "MySQL is ready on 127.0.0.1:3307"
Write-Host "MongoDB is ready on 127.0.0.1:27018"
