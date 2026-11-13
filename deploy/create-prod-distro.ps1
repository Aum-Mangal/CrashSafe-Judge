# Creates the judge-prod WSL distro: Docker and gVisor installed, the code copied in,
# then cut off from Windows. Run from Windows PowerShell. Stops if judge-prod already exists.
param([string]$Name = 'judge-prod')

$env:WSL_UTF8 = '1'
$repo = Split-Path -Parent $PSScriptRoot
$repoInWsl = '/mnt/' + $repo.Substring(0, 1).ToLower() + ($repo.Substring(2) -replace '\\', '/')

if ((wsl --list --quiet) -contains $Name) {
    Write-Error "$Name already exists. Remove it first with: wsl --unregister $Name"
    exit 1
}

wsl --install Ubuntu-24.04 --name $Name --no-launch
if ($LASTEXITCODE -ne 0) { Write-Error 'wsl --install failed'; exit 1 }

# While Windows drives are still mounted: install everything and copy the code in
wsl -d $Name -u root -- bash "$repoInWsl/deploy/setup-host.sh"
if ($LASTEXITCODE -ne 0) { Write-Error 'setup-host.sh failed'; exit 1 }
wsl -d $Name -u root -- bash -c "mkdir -p /opt/judge && cp -r '$repoInWsl/.' /opt/judge/ && rm -rf /opt/judge/.git && cp /opt/judge/deploy/wsl-prod.conf /etc/wsl.conf"
if ($LASTEXITCODE -ne 0) { Write-Error 'copying the code failed'; exit 1 }

# Restart so the new wsl.conf applies: no Windows drives, no Windows programs
wsl --terminate $Name
wsl -d $Name -u root -- bash /opt/judge/deploy/check-isolation.sh
