$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$serviceRoot = Join-Path $root ".fate-test-services"
$downloadRoot = Join-Path $serviceRoot "downloads"

$packages = @(
    @{
        Name = "MySQL"
        Url = "https://cdn.mysql.com/Downloads/MySQL-8.4/mysql-8.4.9-winx64.zip"
        Archive = "mysql-8.4.9-winx64.zip"
        Destination = "mysql-8.4.9-winx64"
        Sha256 = "5795BA250E89290F7507ED3BCC6A655BE373616ABB58B877ACDEA71E1B8F4E8C"
    },
    @{
        Name = "MongoDB"
        Url = "https://fastdl.mongodb.org/windows/mongodb-windows-x86_64-8.0.16.zip"
        Archive = "mongodb-windows-x86_64-8.0.16.zip"
        Destination = "mongodb-windows-x86_64-8.0.16"
        Sha256 = "34EB6795860E29BDF8E5CC9CB4CBBA2C73295A25C60204667029D5F3614FE807"
    }
)

New-Item -ItemType Directory -Force -Path $downloadRoot | Out-Null

foreach ($package in $packages) {
    $archivePath = Join-Path $downloadRoot $package.Archive
    $destinationPath = Join-Path $serviceRoot $package.Destination

    if (-not (Test-Path -LiteralPath $archivePath)) {
        Write-Host "Downloading $($package.Name)..."
        curl.exe --fail --location --output $archivePath $package.Url
        if ($LASTEXITCODE -ne 0) {
            throw "$($package.Name) download failed."
        }
    }

    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
    if ($actualHash -ne $package.Sha256) {
        throw "$($package.Name) archive hash mismatch: $actualHash"
    }

    if (-not (Test-Path -LiteralPath $destinationPath)) {
        Write-Host "Extracting $($package.Name)..."
        Expand-Archive -LiteralPath $archivePath -DestinationPath $destinationPath
    }
}

Write-Host "Portable MySQL and MongoDB binaries are installed under $serviceRoot"
