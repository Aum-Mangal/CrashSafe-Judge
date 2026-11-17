# Contest checklist

Commands marked **judge-prod** run as root inside it. From Windows, signed in as the judge account:
`wsl -d judge-prod -u root -- bash -c "cd /opt/judge && set -a && . /etc/judge/judge.env && set +a && PATH=/opt/judge-venv/bin:$PATH && <command>"`,
so `python` there is the judge's own environment.
Setting up and recovering the judge itself is in [RUNBOOK.md](RUNBOOK.md).

## The week before

- [ ] Gate 3 has passed: a friend finished a mock contest in a browser, and a real MCP client got a result after a reconnect.
- [ ] **Public URL decided.** Not built yet: it waits on the Gate 3 go/no-go (ARCHITECTURE.md §12). This checklist gets its steps when it is.
- [ ] Every problem checked. A reference solution must get AC using at most half the limit, and a wrong or slow one must not:

  ```bash
  python -m admin check-problem problems/<slug> solutions/<slug>.cpp
  python -m admin check-problem problems/<slug> solutions/<slug>-slow.py --expect TLE
  ```

  Run these in `judge-prod` too, once the problems are copied in: limits come from there (DECISIONS.md, 136, 140).
- [ ] Problems copied into `judge-prod`. The hidden tests are kept out of git, so `package-for-prod` can't carry them. From your account, in the repo:

  ```powershell
  tar -cf C:\Users\Public\judge-transfer\problems.tar problems solutions
  ```

  Then as the judge account, in `cmd` (its `<` keeps the bytes):

  ```bat
  wsl -d judge-prod -u root -- bash -c "rm -rf /root/contest && mkdir -m 700 /root/contest && tar -x -C /root/contest" < C:\Users\Public\judge-transfer\problems.tar
  ```

  Delete `problems.tar` from the shared folder afterwards: other accounts on the laptop can read it.
- [ ] Problems imported and the contest created (**judge-prod**). `--start` is local time:

  ```bash
  python -m admin import-problem /root/contest/problems/<slug>
  python -m admin create-contest --title "..." --start "2026-11-28 10:00" --minutes 120 --problems a-slug,b-slug
  ```

  Keep the contest id and the invite code it prints. Send the code only to the people invited.
- [ ] Throughput measured in `judge-prod` (RUNBOOK.md §6), and the number of workers chosen from it. At most 4 on this laptop (DECISIONS.md, 140).
- [ ] The reboot test and the disaster-recovery drill done (RUNBOOK.md §4, §5).
- [ ] Backups reaching `OneDrive\judge-backups` every night.
- [ ] Windows Update paused. Sleep set to Never while plugged in.
- [ ] **Decide now**, and tell the contestants, what happens if the judge is down during the contest: for example, "the end moves by the minutes lost, rounded up to 5". `extend-contest` (below) does it.
- [ ] Code frozen. Note the commit: `git log -1 --format=%h`.

## The day before

- [ ] The frozen commit is what runs: `deploy\package-for-prod.ps1 -Bootstrap`, then `run-in-prod.cmd` as the judge account. Its result file ends with `Ready in N s`.
- [ ] `deploy/status.sh` (**judge-prod**) prints no warnings and exits 0.
- [ ] The contest's problems are still hidden. Each must answer 404 until the start (**judge-prod**):

  ```bash
  curl -s -o /dev/null -w "%{http_code}\n" 127.0.0.1:${API_PORT}/problems/<slug>
  ```

- [ ] A backup taken by hand (`systemctl start judge-backup.service`), and copied to OneDrive: run `judge-copy-backups` in the judge account's Task Scheduler, then `judge-backups-to-onedrive` in yours.

## One hour before

- [ ] The laptop is plugged in. The judge account is signed in, in the background (switch user, don't sign out).
- [ ] `deploy/status.sh` is clean.
- [ ] The public URL works from a phone on mobile data, not the laptop's Wi-Fi.
- [ ] A test submission to a public practice problem gets its verdict.

## During the contest

Run `deploy/status.sh` every 10 minutes, or keep it on screen with `watch -n 30 bash /opt/judge/deploy/status.sh`. It exits 1 and prints `WARNING:` lines when something needs you.

| What you see | What it means | What to do |
|---|---|---|
| `judge-worker@N is activating` or `restarted N times` going up | A worker crashed and systemd is restarting it | Nothing at first: its job's lease runs out and another worker takes it. If it keeps restarting, read `journalctl -u judge-worker@N -n 50` |
| `a job has waited N s` | Jobs are queued but no worker is taking them | Check the worker lines above it. If all are `active`, read their logs: a Docker call may be hanging until its timeout |
| `lease ran out ... and the job wasn't requeued` | No worker is running its reaper | `systemctl restart judge-worker@1` |
| `IE verdicts` or `dead jobs` | The judge failed three times on a job. Never the contestant's fault | Read the workers' logs. Fix, then rejudge the problem (below) |
| `judge-postgres isn't running` or `the API isn't ready` | The database is down. Submissions get 503, nothing is lost | `docker start judge-postgres`. Workers reconnect on their own |
| A wrong `.out` file reported by contestants | The test data is wrong | Fix the folder, `check-problem` it, `import-problem` it, then `python -m admin rejudge --contest <id> --problem <slug>`. Announce it |
| `near the cap of 500` | The queue is full and new submissions get 429 | The judge is overloaded. Contestants' retries will get in as it drains. Don't add workers past 4 |
| The public URL stops answering | The tunnel restarted, or the internet is down | (Filled in when the public URL is built.) |
| The laptop restarted or lost power | Everything stopped | Plug in, sign in to the judge account, wait for `judge-keepalive`, then `status.sh`. Jobs from before finish on their own. Note how many minutes were lost, then move the end (below) |
| Minutes lost to an outage | Contestants couldn't submit | `python -m admin extend-contest --contest <id> --minutes <n>`, and announce the new end it prints. If the contest's end already passed during the outage, add `--reopen`. It only moves the end later, by 1 to 240 minutes, and applies at once |
| `the disk is N% full` | Old sandboxes or images, or logs | `docker system df`, then `journalctl --vacuum-size=200M` |

## After the contest

- [ ] Wait for the queue to empty: `status.sh` shows `0 waiting, 0 running`.
- [ ] Save the numbers (**judge-prod**), then read them before publishing. They are counts and times only, never emails, names or code, but check anyway:

  ```bash
  python -m admin contest-report --contest <id> > /root/contest-report.md
  ```

  Copy it out the way backups go (cmd's `>` keeps the bytes) and commit it as `loadtest/results/contest-<date>.md`.
- [ ] Save the final leaderboard: `/c/<id>/leaderboard`.
- [ ] Take a backup by hand and copy it to OneDrive.
- [ ] Unpause Windows Update.
- [ ] Write up the numbers in the README (ROADMAP.md, Sun 29).
