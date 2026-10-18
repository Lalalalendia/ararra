# RE-09A: owner-only local Google Drive consent (Windows PowerShell).
# Does NOT create folders, upload books, print secrets or modify Google Drive files.
param(
    [Parameter(Mandatory = $true)]
    [string] $ClientJson
)

$ErrorActionPreference = "Stop"
$Sources = "1daqCzEVgPdX639YV5otTLxgVTPbfeBEp"
$Translations = "1X2ZP8Fzbnq5MEAWJyGSErVGGFkfGBrR9"

if (-not (Test-Path -LiteralPath $ClientJson -PathType Leaf)) {
    throw "Google Desktop OAuth client JSON not found. Download it from Google Cloud Console."
}
$tool = Get-Command "chaptera-server" -ErrorAction SilentlyContinue
if (-not $tool) {
    throw "chaptera-server is not installed. In the repo root, run: py -m pip install -e ."
}
if (-not $env:LOCALAPPDATA) {
    throw "LocalAppData folder unavailable on this Windows account."
}
$privateDir = Join-Path $env:LOCALAPPDATA "ReadEra"
New-Item -ItemType Directory -Path $privateDir -Force | Out-Null
$tokenPath = Join-Path $privateDir "drive-owner-refresh-token.txt"
if (Test-Path -LiteralPath $tokenPath) {
    throw "Refusing to overwrite an existing Drive credential: $tokenPath"
}
$env:CHAPTERA_DRIVE_SOURCE_FOLDER_ID = $Sources
$env:CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID = $Translations

Write-Host "ReadEra: Google Drive owner consent"
Write-Host "Required: exactly the two original folders named Sources and Translations."
Write-Host "Google will ask you to choose the account that OWNS or can edit BOTH folders."
Write-Host "If the folders are absent, stop. Do not choose replacements."
Write-Host "Opening OAuth with narrow drive.file permissions..."

& $tool.Source "drive-authorize" "--client-json" $ClientJson "--token-file" $tokenPath
if ($LASTEXITCODE -ne 0) {
    throw "Drive consent was not confirmed for both folders. No upload was attempted."
}
if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) {
    throw "Google Drive consent returned without creating the expected private token file."
}
Write-Host "OAuth confirmed for both folders; credential saved only on this computer."
Write-Host "This is NOT the first real book upload."
Write-Host "Next: transfer credential securely to the ReadEra server using protected secrets."
Write-Host "Never paste the refresh token into chat, GitHub, Notion, or logs."
