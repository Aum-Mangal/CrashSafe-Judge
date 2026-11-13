# Imports judge-prod for the Windows account running this script, then checks its isolation.
# Run it signed in as the dedicated judge account, not your own:
#   powershell -ExecutionPolicy Bypass -File C:\Users\Public\judge-transfer\import-prod-distro.ps1
param(
    [string]$Tarball = "$PSScriptRoot\judge-prod.tar",
    [string]$Name = 'judge-prod',
    [string]$ProtectedUser = 'Saran'
)

$env:WSL_UTF8 = '1'

# Compare profile folders, not account names: the account Aum has the profile folder Saran
if ((Split-Path $env:USERPROFILE -Leaf) -eq $ProtectedUser) {
    Write-Error "Run this as the judge account, not as $ProtectedUser."
    exit 1
}
if ((wsl --list --quiet) -contains $Name) {
    Write-Error "$Name already exists for $env:USERNAME."
    exit 1
}

$target = Join-Path $env:LOCALAPPDATA "wsl\$Name"
New-Item -ItemType Directory -Force $target | Out-Null
Write-Host "Importing $Name for $env:USERNAME. This takes a few minutes."
wsl --import $Name $target $Tarball
if ($LASTEXITCODE -ne 0) { Write-Error 'wsl --import failed'; exit 1 }

Write-Host ''
Write-Host "Isolation check, running as Windows user $env:USERNAME"
# Feed in the check from this folder, because the tarball may hold an older copy.
# cmd's < keeps the bytes intact, which PowerShell's pipe doesn't.
cmd /c "wsl -d $Name -u root -- env PROTECTED_USER=$ProtectedUser bash -s < `"$PSScriptRoot\check-isolation.sh`""
