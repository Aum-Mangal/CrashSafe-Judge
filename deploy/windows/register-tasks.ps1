# Registers the scheduled tasks that keep judge-prod running and its backups off the laptop
# (ARCHITECTURE.md §12). Run it once in each account; running it again replaces the tasks.
#
#   Signed in as the judge account:
#     powershell -ExecutionPolicy Bypass -File C:\Users\Public\judge-transfer\register-tasks.ps1 -Account Judge
#       judge-keepalive      at logon: keeps a wsl.exe process open, since WSL stops a distro
#                            nothing on Windows is using, and restarts it if it ends
#       judge-copy-backups   daily 03:00: new backups from judge-prod into
#                            C:\Users\Public\judge-transfer\backups
#   In your own account, from the repository:
#     powershell -ExecutionPolicy Bypass -File deploy\windows\register-tasks.ps1 -Account Owner
#       judge-backups-to-onedrive  daily 03:30: from that folder into OneDrive\judge-backups
#
# Nothing runs with administrator rights. conhost --headless keeps the console windows hidden.
param(
    [Parameter(Mandatory)][ValidateSet('Judge', 'Owner')][string]$Account,
    [string]$Distro = 'judge-prod',
    [string]$OwnerProfile = 'Saran'
)

$ErrorActionPreference = 'Stop'
$me = "$env:USERDOMAIN\$env:USERNAME"
$isOwner = (Split-Path $env:USERPROFILE -Leaf) -eq $OwnerProfile
if ($Account -eq 'Judge' -and $isOwner) { throw "Run -Account Judge signed in as the judge account, not as $OwnerProfile." }
if ($Account -eq 'Owner' -and -not $isOwner) { throw "Run -Account Owner in your own account ($OwnerProfile)." }

$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew

function Hidden([string]$Command) {
    New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless $Command"
}

function Register([string]$Name, $Action, $Trigger, [string]$Description) {
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $settings `
        -Principal $principal -Description $Description -Force | Out-Null
    "registered $Name"
}

if ($Account -eq 'Judge') {
    # The judge account runs its own copy of the script, which other accounts can't change
    $taskDir = Join-Path $env:LOCALAPPDATA 'judge'
    New-Item -ItemType Directory -Force $taskDir | Out-Null
    Copy-Item (Join-Path $PSScriptRoot 'copy-backups.ps1') $taskDir -Force
    $copy = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$taskDir\copy-backups.ps1`" -Step FromDistro -Distro $Distro"

    Register 'judge-keepalive' (Hidden "wsl.exe -d $Distro -u root -- sleep infinity") `
        (New-ScheduledTaskTrigger -AtLogOn -User $me) "Keeps $Distro running while this account is signed in"
    Register 'judge-copy-backups' (Hidden $copy) (New-ScheduledTaskTrigger -Daily -At '03:00') `
        "Copies new encrypted backups out of $Distro"
    Start-ScheduledTask -TaskName 'judge-keepalive'
    "started judge-keepalive"
} else {
    $copy = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\copy-backups.ps1`" -Step ToOneDrive"
    Register 'judge-backups-to-onedrive' (Hidden $copy) (New-ScheduledTaskTrigger -Daily -At '03:30') `
        'Copies the judge''s encrypted backups into OneDrive'
}
