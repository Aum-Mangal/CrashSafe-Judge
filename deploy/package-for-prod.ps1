# Packages committed code for judge-prod and writes the .cmd file the judge account runs.
#
# 1. From your own account:  powershell -ExecutionPolicy Bypass -File deploy\package-for-prod.ps1
# 2. Signed in as judgehost:  double-click C:\Users\Public\judge-transfer\run-in-prod.cmd
# 3. Back in your account:    read the result-*.txt file it wrote next to it
#
# judge-prod can't see C:, so the code goes in as a tar through stdin (DECISIONS.md, 57).
param(
    [string]$Ref = 'HEAD',
    [string]$Transfer = 'C:\Users\Public\judge-transfer',
    [string]$Name = 'judge-prod'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$commit = (git -C $repo rev-parse --short $Ref).Trim()
if ($Ref -eq 'HEAD' -and (git -C $repo status --porcelain)) {
    Write-Warning 'There are uncommitted changes. They are not included: only the commit is packaged.'
}

New-Item -ItemType Directory -Force $Transfer | Out-Null
# git archive takes committed files only, so nothing untracked or ignored can leak in
git -C $repo archive --format=tar "--add-virtual-file=COMMIT:$commit" -o "$Transfer\judge-src.tar" $Ref
if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }

# A new result file each run: a file judgehost wrote can't be overwritten from your account, or the reverse
$result = "result-$commit-$(Get-Date -Format 'yyyyMMdd-HHmmss').txt"
@"
@echo off
rem Written by deploy\package-for-prod.ps1 for commit $commit. Run it signed in as judgehost.
rem cmd's < passes the tar's bytes unchanged, which a PowerShell pipe would not.
echo Copying commit $commit into $Name...
wsl -d $Name -u root -- bash -c "rm -rf /opt/judge.old; if [ -d /opt/judge ]; then mv /opt/judge /opt/judge.old; fi; mkdir /opt/judge && tar -x -C /opt/judge" < "%~dp0judge-src.tar"
if errorlevel 1 (
    echo Copying failed.
    pause
    exit /b 1
)
echo Running the tests. The first run installs packages and builds images, so it takes several minutes...
wsl -d $Name -u root -- bash /opt/judge/deploy/prod-test.sh > "%~dp0$result" 2>&1
echo Finished. The output is in %~dp0$result
pause
"@ | Set-Content -Encoding ascii "$Transfer\run-in-prod.cmd"

Write-Host "Packaged commit $commit into $Transfer"
Write-Host "Now sign in as judgehost and double-click $Transfer\run-in-prod.cmd"
Write-Host "It will write $Transfer\$result"
