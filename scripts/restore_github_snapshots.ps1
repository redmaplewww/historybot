param(
    [string]$Repository = "redmaplewww/historybot",
    [string]$Release = "data-snapshot-20260922",
    [string]$Directory = "data"
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $Directory | Out-Null
gh release download $Release --repo $Repository --pattern "*.db" --dir $Directory
Write-Host "Database snapshots restored to $Directory"
