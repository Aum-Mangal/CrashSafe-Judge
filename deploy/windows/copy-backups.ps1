# Copies new encrypted backups one step towards OneDrive, keeping the newest 14 at the end.
# Two scheduled tasks use it (register-tasks.ps1):
#   judge account:  judge-prod's /var/backups/judge  ->  C:\Users\Public\judge-transfer\backups
#   your account:   C:\Users\Public\judge-transfer\backups  ->  OneDrive\judge-backups
# judge-prod can't see C: (ARCHITECTURE.md §10), so the first step reads the files out through wsl.exe.
# The judge account never touches your files, and only age-encrypted files pass through Public.
param(
    [ValidateSet('FromDistro', 'ToOneDrive')][string]$Step = 'FromDistro',
    [string]$Distro = 'judge-prod',
    [string]$Transfer = 'C:\Users\Public\judge-transfer\backups',
    [string]$OneDrive = "$env:OneDrive\judge-backups",
    [int]$Keep = 14
)

$ErrorActionPreference = 'Stop'
$env:WSL_UTF8 = '1'
$pattern = '^judge-\d{8}-\d{6}\.dump\.age$'  # also stops a strange name from reaching a command line

function Keep-Newest([string]$Folder) {
    # Names hold the time, so sorting by name sorts by age
    Get-ChildItem $Folder -File | Where-Object Name -match $pattern | Sort-Object Name -Descending |
        Select-Object -Skip $Keep | ForEach-Object { Remove-Item $_.FullName; "removed $($_.Name)" }
}

if ($Step -eq 'FromDistro') {
    New-Item -ItemType Directory -Force $Transfer | Out-Null
    # No folder yet (no backup has run) means nothing to copy; a missing distro still fails
    $names = wsl -d $Distro -u root -- sh -c 'ls -1 /var/backups/judge 2> /dev/null || true'
    if ($LASTEXITCODE -ne 0) { throw "can't list the backups in $Distro" }
    # Only the newest $Keep: an older one would be copied, then removed again on every run
    foreach ($name in $names | Where-Object { $_ -match $pattern } | Sort-Object -Descending | Select-Object -First $Keep) {
        $target = Join-Path $Transfer $name
        if (Test-Path $target) { continue }
        # cmd's > keeps the bytes intact, which a PowerShell pipe wouldn't. Copy to .part, then
        # rename, so the next step never picks up half a file.
        cmd /c "wsl -d $Distro -u root -- cat /var/backups/judge/$name > `"$target.part`""
        if ($LASTEXITCODE -ne 0) { Remove-Item "$target.part" -ErrorAction SilentlyContinue; throw "copying $name failed" }
        Move-Item "$target.part" $target
        "copied $name"
    }
    Keep-Newest $Transfer
} else {
    New-Item -ItemType Directory -Force $OneDrive | Out-Null
    foreach ($file in Get-ChildItem $Transfer -File | Where-Object Name -match $pattern) {
        $target = Join-Path $OneDrive $file.Name
        if (Test-Path $target) { continue }
        Copy-Item $file.FullName "$target.part"
        Move-Item "$target.part" $target
        "copied $($file.Name)"
    }
    Keep-Newest $OneDrive
}
