[CmdletBinding()]
param(
    [string]$Destination = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Fate Control.exe')
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectFile = Join-Path $projectRoot 'Windows\FateControl.Desktop.csproj'
$publishRoot = Join-Path $projectRoot 'Windows'
$publishDirectory = Join-Path $publishRoot ("publish-staging-$PID-" + [Guid]::NewGuid().ToString('N'))

try {
    dotnet publish $projectFile `
        --configuration Release `
        --runtime win-x64 `
        --self-contained false `
        --output $publishDirectory `
        -p:PublishSingleFile=true `
        -p:DebugType=None `
        -p:DebugSymbols=false

    if ($LASTEXITCODE -ne 0) {
        throw "Fate Control desktop publishing failed with exit code $LASTEXITCODE."
    }

    $publishedExecutable = Join-Path $publishDirectory 'FateControl.exe'
    if (-not (Test-Path -LiteralPath $publishedExecutable)) {
        throw "The published FateControl.exe was not produced."
    }

    $destinationDirectory = Split-Path -Parent $Destination
    if (-not (Test-Path -LiteralPath $destinationDirectory)) {
        New-Item -ItemType Directory -Path $destinationDirectory | Out-Null
    }

    Copy-Item -LiteralPath $publishedExecutable -Destination $Destination -Force
    $sourceHash = (Get-FileHash -LiteralPath $publishedExecutable -Algorithm SHA256).Hash
    $destinationHash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash
    if ($sourceHash -ne $destinationHash) {
        throw "The Desktop copy did not match the verified published executable."
    }

    $publishedResult = Get-Item -LiteralPath $Destination | Select-Object FullName, Length, LastWriteTime
}
finally {
    if (Test-Path -LiteralPath $publishDirectory) {
        $resolvedRoot = [IO.Path]::GetFullPath($publishRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        $resolvedStaging = [IO.Path]::GetFullPath($publishDirectory)
        if (-not $resolvedStaging.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase) -or
            -not ([IO.Path]::GetFileName($resolvedStaging)).StartsWith('publish-staging-', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean an unexpected desktop publish staging path: $resolvedStaging"
        }
        Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
    }
}

$publishedResult
Write-Output "SHA256 $destinationHash"
