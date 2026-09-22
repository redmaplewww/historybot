param(
    [string]$Repository = "redmaplewww/historybot",
    [string]$Release = "data-snapshot-20260922",
    [string]$Directory = "data"
)

$ErrorActionPreference = "Stop"
$archiveName = "historybot_large_db_snapshots_20260922.zip"
$checksumPath = Join-Path (Split-Path -Parent $PSScriptRoot) "RELEASE_DATA_SHA256SUMS.txt"

function Assert-Hash {
    param(
        [string]$Path,
        [string]$RelativePath
    )

    $expected = $null
    foreach ($line in Get-Content -LiteralPath $checksumPath) {
        if ($line -match '^([0-9a-fA-F]{64})\s{2}(.+)$' -and $Matches[2] -eq $RelativePath) {
            $expected = $Matches[1].ToLowerInvariant()
            break
        }
    }
    if (-not $expected) {
        throw "No checksum found for $RelativePath"
    }

    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "SHA-256 mismatch for $RelativePath"
    }
}

New-Item -ItemType Directory -Force -Path $Directory | Out-Null

gh release download $Release --repo $Repository --pattern "*.db" --dir $Directory --clobber
if ($LASTEXITCODE -ne 0) {
    throw "Failed to download direct database assets"
}

gh release download $Release --repo $Repository --pattern "$archiveName.part-*" --dir $Directory --clobber
if ($LASTEXITCODE -ne 0) {
    throw "Failed to download large database archive parts"
}

$parts = Get-ChildItem -LiteralPath $Directory -Filter "$archiveName.part-*" | Sort-Object Name
if ($parts.Count -eq 0) {
    throw "No large database archive parts were downloaded"
}

foreach ($part in $parts) {
    Assert-Hash -Path $part.FullName -RelativePath "data/$($part.Name)"
}

$archivePath = Join-Path $Directory $archiveName
$archive = [System.IO.File]::Open($archivePath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
try {
    foreach ($part in $parts) {
        $input = [System.IO.File]::OpenRead($part.FullName)
        try {
            $input.CopyTo($archive)
        } finally {
            $input.Dispose()
        }
    }
} finally {
    $archive.Dispose()
}

Assert-Hash -Path $archivePath -RelativePath "data/$archiveName"
Expand-Archive -LiteralPath $archivePath -DestinationPath $Directory -Force

foreach ($line in Get-Content -LiteralPath $checksumPath) {
    if ($line -match '^([0-9a-fA-F]{64})\s{2}data/(.+\.db)$') {
        $databasePath = Join-Path $Directory $Matches[2]
        if (-not (Test-Path -LiteralPath $databasePath)) {
            throw "Missing restored database: $databasePath"
        }
        Assert-Hash -Path $databasePath -RelativePath "data/$($Matches[2])"
    }
}

Write-Host "Database snapshots restored and verified in $Directory"
