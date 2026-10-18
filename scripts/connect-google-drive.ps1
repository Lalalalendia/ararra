# RE-09A: owner-only local Google Drive consent (Windows PowerShell).
# Default: authorize and check folder permissions only.
# -RunSmoke explicitly creates ONLY synthetic TXT in the two chosen roots,
# edits the newly created files, and proves retry never overwrites edits.
# Never prints tokens or touches real books.
param(
    [Parameter(Mandatory = $true)]
    [string] $ClientJson,

    [switch] $RunSmoke,

    [switch] $UseExistingToken
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
$alreadyAuthorized = Test-Path -LiteralPath $tokenPath -PathType Leaf
if ($alreadyAuthorized -and -not $UseExistingToken) {
    throw "Existing private Drive token detected. Use -UseExistingToken to verify access without reauthorization. No file will be overwritten."
}
if (-not $alreadyAuthorized -and $UseExistingToken) {
    throw "No existing Drive token. Run without -UseExistingToken to authorize first."
}
$env:CHAPTERA_DRIVE_SOURCE_FOLDER_ID = $Sources
$env:CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID = $Translations

if (-not $alreadyAuthorized) {
    Write-Host "ReadEra: owner Google Drive consent"
    Write-Host "Select the Google account that owns or can EDIT both approved folders."
    Write-Host "If either folder is absent, stop. Do not create replacements."
    Write-Host "Opening narrow drive.file OAuth / Google Picker..."
    & $tool.Source "drive-authorize" "--client-json" $ClientJson "--token-file" $tokenPath
    if ($LASTEXITCODE -ne 0) {
        throw "Drive consent for both folders not confirmed. No files uploaded."
    }
    if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) {
        throw "OAuth finished without a protected credential file."
    }
}
Write-Host "Verifying both existing Drive root folders with ReadEra's OWN OAuth..."
& $tool.Source "drive-smoke" "--client-json" $ClientJson "--token-file" $tokenPath
if ($LASTEXITCODE -ne 0) {
    throw "ReadEra OAuth cannot list both approved folders. No synthetic upload attempted."
}
if ($RunSmoke) {
    Write-Host "Running explicit synthetic export acceptance: two new TXT, in-place edits, same-ID retry."
    Write-Host "No real books, Gemini, or production SQLite are used."
    & $tool.Source "drive-smoke" "--client-json" $ClientJson "--token-file" $tokenPath "--live-smoke"
    if ($LASTEXITCODE -ne 0) {
        throw "Synthetic Drive export acceptance failed. Inspect synthetic Drive files; do not enable automatic export."
    }
    Write-Host "Synthetic source and translation export verified, including unchanged IDs and preserved edits."
} else {
    Write-Host "Read-only folder preflight completed; use -RunSmoke for a synthetic LIVE export."
}
Write-Host "Credential saved on this computer only. Provision it securely to the ReadEra server."
Write-Host "Never paste refresh tokens into chat, GitHub, Notion or logs."

