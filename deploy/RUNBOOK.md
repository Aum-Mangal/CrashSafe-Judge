# Running judge-prod

The steps that need a person: they use the judge account, a key only you hold, or Windows settings. The scripts do the rest. ARCHITECTURE.md §12 explains the design.

## 1. Make the backup key (once)

Backups are encrypted with [age](https://github.com/FiloSottile/age) to a public key. judge-prod holds only the public key, so it can write backups but never read them (DECISIONS.md, 131).

In the dev distro:

```bash
age-keygen -o judge-backup-key.txt
```

It prints the public key, `age1...`. Then:

1. Put the whole file, the private key, in your password manager, or somewhere off this laptop.
2. Delete `judge-backup-key.txt`. Without the private key, no backup can be restored.
3. Give judge-prod the public key. Signed in as the judge account, in `cmd`:

   ```bat
   wsl -d judge-prod -u root -- sh -c "mkdir -p /etc/judge && echo age1...your-public-key... > /etc/judge/backup-recipient.txt"
   ```

   The key goes inside the quotes. Piping `echo` from `cmd` would add a space and `\r` to the line. `backup.sh` copes with that, but `age` itself refuses such a file.

## 2. Start or update the judge

1. From your account: `powershell -ExecutionPolicy Bypass -File deploy\package-for-prod.ps1 -Bootstrap`
2. Signed in as the judge account: double-click `C:\Users\Public\judge-transfer\run-in-prod.cmd`
3. Back in your account: read the `result-*.txt` it wrote. The last line says `Ready in N s`.

`bootstrap.sh` is safe to run again. It keeps `/etc/judge/judge.env`, which holds the database password and the JWT secret, and the data, then restarts the API and the workers on the new code.

## 3. Keep it running, and its backups off the laptop (once)

1. Signed in as the judge account:

   ```bat
   powershell -ExecutionPolicy Bypass -File C:\Users\Public\judge-transfer\register-tasks.ps1 -Account Judge
   ```

2. In your account:

   ```bat
   powershell -ExecutionPolicy Bypass -File deploy\windows\register-tasks.ps1 -Account Owner
   ```

3. Set sleep to Never while plugged in (Settings > System > Power). Pause Windows Update over contest week.

The judge account must stay signed in, in the background (switch user, don't sign out). WSL only runs for a signed-in account.

## 4. Reboot test

1. Note a job id that is still queued or running, if you have one.
2. Restart Windows. Sign in to the judge account, then switch to yours.
3. Check, as the judge account:

   ```bat
   wsl -l -v
   wsl -d judge-prod -u root -- systemctl is-active judge-api judge-worker@1 judge-worker@2 judge-backup.timer
   wsl -d judge-prod -u root -- curl -s 127.0.0.1:8000/health/ready
   ```

   `judge-prod` should be Running, everything should be `active`, and readiness should be ok. A job from before the restart should finish on its own: its lease runs out and the reaper gives it to a worker.
4. Close every window in both accounts and wait 5 minutes. `wsl -l -v` should still show judge-prod Running.

## 5. Disaster-recovery drill (Tue 24)

1. Copy the newest backup's name from `OneDrive\judge-backups`, and note the time.
2. Delete judge-prod, then create it again with `deploy\create-prod-distro.ps1`, or `import-prod-distro.ps1` as the judge account.
3. Put the backup and the private key into judge-prod through stdin. Use `cmd`, whose `<` keeps the bytes intact, which a PowerShell pipe doesn't:

   ```bat
   wsl -d judge-prod -u root -- sh -c "umask 077; cat > /root/restore.dump.age" < "C:\path\to\judge-YYYYMMDD-HHMMSS.dump.age"
   wsl -d judge-prod -u root -- sh -c "umask 077; cat > /root/backup-key.txt" < "C:\path\to\judge-backup-key.txt"
   ```

   Then, inside judge-prod:

   ```bash
   bash /opt/judge/deploy/bootstrap.sh /root/restore.dump.age /root/backup-key.txt
   ```

4. Delete `/root/backup-key.txt`.
5. Publish two numbers: the minutes from deletion to `Ready`, and the data lost (everything after the backup's time).

## 6. Throughput test (Wed 25)

Run it after step 2, from the same commit, and before the contest: it adds a public practice problem, `loadtest-sum`, 30 accounts nobody can log in to, and a few hundred jobs.

1. From your account: `powershell -ExecutionPolicy Bypass -File deploy\package-for-prod.ps1 -Throughput`. It copies the k6 that the dev distro checked against Grafana's checksum (DECISIONS.md, 137).
2. Signed in as the judge account: double-click `run-in-prod.cmd`. It checks k6's hash inside judge-prod before installing it, then runs about 15 minutes.
3. Back in your account: the `result-*.txt` file is the markdown table. Save it as `loadtest/results/throughput-prod.md`. A number in the "Not AC" column above 0 means the judge gave a wrong verdict under load: that is a bug, not a slow run.
