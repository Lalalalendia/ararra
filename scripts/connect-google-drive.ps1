# RE-09A: owner-only local Google Drive consent (Windows PowerShell).
# Default: authorize and check folder permissions only.
# -RunSmoke explicitly creates ONLY synthetic TXT in the two chosen roots,
# edits the newly created files, and proves retry never overwrites edits.
# Never prints tokens or touches real books.
param(
    [Parameter(Mandatory = $true)]
    [string] $ClientJson,

    [switch] $RunSmoke,

    [switch] $UseExistingToken,

    [switch] $IncludeBackupFolder,

    [switch] $UseOriginalToken
)

$ErrorActionPreference = "Stop"
$Sources = "1daqCzEVgPdX639YV5otTLxgVTPbfeBEp"
$Translations = "1X2ZP8Fzbnq5MEAWJyGSErVGGFkfGBrR9"
$BackupFolder = "1ll0rsERY7kpdMm8rQQVDAFcdx7_gihtl"
if ($IncludeBackupFolder -and $RunSmoke) {
    throw "Backup authorization is read-only. Do not combine -IncludeBackupFolder with -RunSmoke."
}
if ($UseOriginalToken -and (-not $IncludeBackupFolder -or -not $UseExistingToken)) {
    throw "-UseOriginalToken requires -IncludeBackupFolder -UseExistingToken; it cannot reauthorize the original file."
}

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
# A new, separate candidate MUST be explicitly accepted by the operator.
# The old working refresh token is NEVER overwritten or deleted by this flow.
$originalToken = Join-Path $privateDir "drive-owner-refresh-token.txt"
$candidateToken = Join-Path $privateDir "drive-owner-refresh-token-backup-candidate.txt"
$tokenPath = if ($IncludeBackupFolder -and -not $UseOriginalToken) { $candidateToken } else { $originalToken }
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
    Write-Host "ReadEra: owner Google Drive consent (drive.file only)"
    if ($IncludeBackupFolder) {
        Write-Host "Select ALL THREE folders: the two existing book folders and Erida / Backups."
        Write-Host "Creating a NEW candidate token. The original credential is untouched."
        & $tool.Source "drive-authorize" "--client-json" $ClientJson "--token-file" $tokenPath "--backup-folder-id" $BackupFolder
    } else {
        Write-Host "Select the Google account that can EDIT both original book folders."
        & $tool.Source "drive-authorize" "--client-json" $ClientJson "--token-file" $tokenPath
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Owner consent was not confirmed. No files uploaded."
    }
    if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) {
        throw "OAuth finished without a protected credential file."
    }
}
Write-Host "Verifying existing Drive book folders with ReadEra's OWN OAuth..."
if ($IncludeBackupFolder) {
    Write-Host "Also testing backup folder access with this SAME token, read-only."
    & $tool.Source "drive-smoke" "--client-json" $ClientJson "--token-file" $tokenPath "--backup-folder-id" $BackupFolder
} else {
    & $tool.Source "drive-smoke" "--client-json" $ClientJson "--token-file" $tokenPath
}
if ($LASTEXITCODE -ne 0) {
    throw "One or more approved Drive folders could not be verified. No synthetic uploads were attempted."
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
if ($IncludeBackupFolder) {
    Write-Host "Backup folder and both book folders passed ReadEra read-only checks."
    if ($UseOriginalToken) {
        Write-Host "Existing original token verified; no new token generated."
    } else {
        Write-Host "Backup CANDIDATE token verified; original credential remains unchanged."
        Write-Host "Provision only after a separately reviewed VPS migration and rollback check."
    }
} else {
    Write-Host "Credential saved on this computer only. Provision it securely to the ReadEra server."
}
Write-Host "Never paste refresh tokens into chat, GitHub, Notion or logs."

