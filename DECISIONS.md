# Decisions

Each entry says what we chose and why. This file is also interview preparation: if you can explain each row, you can defend the project.

Evidence labels: **Spec** = primary source. **Report** = third-party report, not yet reproduced by us. **Review** = found by reading the design.

## v2 to v3 changes (2 Nov 2026)

### Sandbox

| # | Change | Why | Evidence |
|---|---|---|---|
| 1 | Added `judge-runner` inside the sandbox | Killing `docker exec` leaves the program running, so wall and output limits must be enforced from inside | Report |
| 2 | Runner and program use different uids | With the same uid, a program could reach the runner's file descriptors and forge its verdict | Review |
| 3 | Two containers per job | One container has one memory limit, but compile and run need different limits | Review |
| 4 | `--oom-score-adj -1000` | The host OOM killer can kill gVisor's own process, which destroys the sandbox and its cgroup | Report, spike #1 |
| 5 | `--pids-limit` raised to a backstop, guest `nproc` added | Under runsc, the pids cgroup counts gVisor's threads, and hitting it can kill the sandbox instead of returning `EAGAIN` | Report and open gVisor issue #2490, spike #2 and #3 |
| 6 | Runner kills leftovers after each test | Background processes would otherwise survive into the next test and pollute its CPU time | Review |
| 7 | Added blame rule | Without it, a fork bomb looks like an infrastructure failure and gets retried three times | Review |
| 8 | Added OLE and a separate 16 MB output limit | v2's 64 KB cap would have killed valid large answers | Review |
| 9 | Peak memory is now per test | The runner can report it, so MLE is easier to explain to contestants | Review |
| 10 | Precompiled header for C++ | g++ spawns several processes, and process creation is gVisor's main cost | Report, spike #9 |
| 11 | `/work` tmpfs owned by uid 65534, `TMPDIR=/work`, source through stdin | v2's flags would stop the compiler writing its files | Review |

### Queue

| # | Change | Why | Evidence |
|---|---|---|---|
| 12 | Reaper checks `cancel_requested` | A crash during cancel would otherwise re-run a cancelled job | Review |
| 13 | 2 s claim poll, reaper sends NOTIFY | A requeued job or missed NOTIFY would otherwise sit until the next job arrived | Review |
| 14 | Fencing token added to the container label | Job ID alone can't tell an orphan from the retry of the same job | Review |
| 15 | Added `release` path with `not_before` backoff | v2 had no way to return a job after a Docker failure except waiting for lease expiry | Review |
| 16 | Added `priority`, with contest jobs first | Agent traffic must not delay a live contest | Review |
| 17 | Conditional cancel for queued jobs | Avoids a race with a worker claiming the same job | Review |
| 18 | Finish also checks `status='running'` | Makes the fenced update complete | Review |
| 19 | `request_hash` includes kind and contest | The same source in another context is a different request | Review |
| 20 | Reaper waits one heartbeat after connecting | Stops a Postgres restart from burning retry attempts on healthy jobs | Review |
| 21 | Job budgets per tool | Count-based rate limits don't stop a few very heavy jobs filling both workers | Review |

### Contest and data

| # | Change | Why | Evidence |
|---|---|---|---|
| 22 | Hidden tests store no program output | A program can echo hidden input to stderr | Review |
| 23 | Problem visibility rule | v2 would show contest problems before the start | Review |
| 24 | Admin CLI with `rejudge` | One bad test case in a live contest needs a rejudge | Review |
| 25 | Invite code to register | Open sign-up gives strangers free compute | Review |
| 26 | Expiry deletes source, stdin and tests | v2 only removed results | Review |
| 27 | Backups encrypted with `age` | They contain emails and password hashes | Review |
| 28 | `submit_solution` over MCP is limited to public problems | An agent should not be able to probe a live contest's hidden tests | Review |

### MCP

| # | Change | Why | Evidence |
|---|---|---|---|
| 29 | `tasks/update` acknowledges known IDs | The spec asks for an acknowledgement and an error only for unknown IDs | Spec |
| 30 | `tasks/cancel` always returns an empty acknowledgement | Required by the spec; v2 returned the completed result | Spec |
| 31 | Dead job maps to `failed` with a JSON-RPC `error` | `failed` is only for JSON-RPC errors, and a tool-level failure counts as `completed` | Spec |
| 32 | Optional `idempotency_key` on tools | The spec doesn't address `tools/call` retries | Spec |
| 33 | No-Tasks path is treated as the main path | Client support is uneven and the Python SDK has no Tasks support yet | Spec, SDK release notes |

### Infrastructure and plan

| # | Change | Why | Evidence |
|---|---|---|---|
| 34 | CI uses `ubuntu-24.04-arm` | Free for public repos and matches production | Spec |
| 35 | Deploy to the VM in week 1, not week 4 | The riskiest unknowns only appear on the real host | Review |
| 36 | Nightly chaos runs from VM cron | Avoids keeping an SSH key in GitHub | Review |
| 37 | Dev in WSL2 Ubuntu with Docker Engine | Docker Desktop here has no runsc and uses cgroupfs, unlike production | Checked on this laptop |
| 38 | Stay at 2 OCPU all month | Avoids a resize failing before the contest | Review |
| 39 | Keep Python; it left the cut list | It has no compile step and is what agents will mostly use | Review |
| 40 | Uptime check uses `/health/ready` | v2 called `/health`, which doesn't exist | Review |

## v3.1 changes (3 Nov 2026): host on the laptop

Oracle only accepts credit cards for verification, so it was dropped. This replaces decisions 34, 35, 36 and 38.

| # | Change | Why | Evidence |
|---|---|---|---|
| 41 | Production runs on the laptop in WSL2 | No card needed, and the laptop has more CPU than the free VM | Review |
| 42 | Separate `judge-prod` distro with Windows drives and Windows programs switched off | Untrusted code now runs on a personal laptop, so a sandbox escape must not reach personal files | Report, Review |
| 43 | Cloudflare Quick Tunnel for public access | No account, domain or open ports. Its 200-request and no-SSE limits fit a polling design | Spec |
| 44 | Stable URL decided by 21 Nov | A Quick Tunnel's URL changes on restart. Only the uptime check and MCP Registry need a stable one | Spec |
| 45 | Caddy removed | Cloudflare handles TLS, so Caddy had nothing left to do | Review |
| 46 | Encrypted backups copied into OneDrive | Off the laptop at no cost | Review |
| 47 | Keep-alive task at Windows logon | WSL stops idle distros, and systemd doesn't prevent it | Report |
| 48 | CI on `ubuntu-24.04` (amd64) | Matches production again | Review |
| 49 | Swap off permanently with `.wslconfig` | `swapoff -a` only lasts until WSL restarts | Review |

## Environment

Dev (2 Nov 2026): Ubuntu 24.04.5 on WSL2, kernel 6.18, amd64, 8 CPUs, 7.8 GB. Docker Engine 29.9.2 with the systemd cgroup driver on cgroup v2. gVisor release-20261026.0. Default runtime stays `runc`. `deploy/setup-host.sh` builds this.

Swap is turned off by `setup-host.sh` with `swapoff -a`, which lasts until WSL restarts, so the script must be re-run or swap disabled before memory tests.

## Spike results

To be filled in during the spike. Each ARCHITECTURE.md value marked [S] gets its measured value here.

| # | Question | Result | Decision |
|---|---|---|---|
| 1 | OOM survival | Default: the host OOM killer killed `gvisor_sentry`, the sandbox died, Docker showed `OOMKilled=true` and exit 137, and the cgroup was removed. With `--oom-score-adj -1000`: the kernel killed small runsc helpers instead, the sandbox wedged, and `docker rm -f` blocked the whole Docker daemon for about 10 minutes | Never use `-1000`. MLE = sandbox died with `OOMKilled=true`, or runner peak RSS over the limit. Probe 1b tests catching it earlier |
| 2 | `nproc` in runsc | `--ulimit nproc=64`: fork 63 returned `EAGAIN` and the sandbox stayed up (64 = 62 children + forker + `sleep`) | Main fork-bomb defence |
| 3 | Lowest stable `--pids-limit` | Idle sandbox uses 31 host pids. Limit 32: exec failed with a misleading "out of memory". Limits 64, 128, 256 with 200 forks: sandbox exited with code 2, not OOM | `--pids-limit` only as a high backstop. Never classify by runsc error text. Probe 3b sets the value |
| 4 | `kill(-1)` cleanup | A background `sleep` from test 1 was still running in test 2. `kill(-1)` from uid 65534 killed it and left PID 1 alive, but it stayed a zombie because `sleep infinity` doesn't reap | Runner cleanup works. Zombies count toward `nproc`, so add `--init` (probe 4b) |
| 5 | `wait4` accuracy | Burn test: runsc `wait4` 530–610 ms vs cgroup 563–661 ms. runc 466–570 vs 490–599. 128 MB touched: `maxrss` 133,156 KB under runsc, 131,656 KB under runc. runsc CPU times come in 10 ms steps | Runner `wait4` is the timing and memory source. Report CPU in 10 ms steps. Runs vary by about 20% on the laptop, so limits need margin and calibration |
| 6 | Cgroup path | `/sys/fs/cgroup/system.slice/docker-<id>.scope`. Docker passes `--systemd-cgroup=true` to runsc. Another container with `/sys/fs/cgroup` mounted read-only could read its `cpu.stat` | Workers find a sandbox's cgroup from its container ID |
| 7 | gVisor memory overhead | Idle: 11 MB under runsc vs 0.7 MB under runc. With a 128 MB program: 13 MB beyond the program vs 1.7 MB | Sandbox memory = problem limit + 64 MB headroom |
| 8 | Capabilities with `no-new-privileges` | With only `SETUID`, `SETGID` and `KILL`, the runner dropped to uid 65534. The program could not open the runner's stderr, kill the runner, or become root | Runner design confirmed |
| 9 | Compile time | Precompiled header: about 860 ms under runsc vs 2.2 s without (warm runs; the first is slower). One compile under a 512 MB limit peaked at 347 MB | Keep the precompiled header, the 512 MB compile limit and the 10 s compile wall limit |
| 10 | Container and exec cost | Start: 223 ms under runsc vs 224 ms under runc. One `docker exec`: 77 ms vs 111 ms | Two containers per job are affordable. Per-test cost is mostly the exec |
| 1b | Memory limit inside the sandbox | Runner with a 192 MB `RLIMIT_AS`: `malloc` failed at 192 MB, peak 182 MB, the sandbox stayed up | The runner sets `RLIMIT_AS` to the problem limit, so most memory bombs no longer kill the sandbox. Python is still untested |
| 3b | Host pids for 64 guest processes | `pids.peak` 162 | `--pids-limit 512` |
| 4b | Zombies | With the runner as a child subreaper, a leftover background process was killed and reaped, with no zombie left | Drop `--init` |

Raw output: `spike/results/2026-11-04-probes-6-10.txt` and `spike/results/2026-11-04-runner.txt`.

## Runner decisions (4 Nov 2026)

| # | Change | Why | Evidence |
|---|---|---|---|
| 50 | Runner reports facts; the worker decides the verdict | Keeps the code running next to untrusted programs small and easy to check | Review |
| 51 | Runner is a child subreaper | Orphans come back to the runner, which kills and reaps all of them, so no `--init` is needed | Follow-up 4b |
| 52 | CPU time comes from `RUSAGE_CHILDREN` after reaping | Counts every process the program started, not just the first one | Review |
| 53 | Fixed four-variable environment | Nothing from `docker exec` leaks in, and every run sees the same environment | Runner test |
| 54 | Setup pipe closed on exec | Separates "couldn't start" (a setup error) from "ran and exited 127" | Runner test |

## judge-prod (4 Nov 2026)

| # | Finding or change | Why it matters | Evidence |
|---|---|---|---|
| 55 | **Fix chosen: 62.** Automount off is not a boundary: root in `judge-prod` mounted `C:\` by hand with `mount -t drvfs`. Windows programs stayed blocked, even after root re-registered the handler | Root there could read your files or plant a Windows startup script. Options: run `judge-prod` under a separate standard Windows account (ACLs then hide your profile), use a VirtualBox VM, or accept the documented risk | `spike/results/2026-11-04-judge-prod.txt` |
| 56 | Spike repeated in `judge-prod`: same results as the dev distro | The sandbox behaves the same where it will actually run | Same file |
| 58 | Plain numbered SQL migrations with a 40-line runner, not an ORM tool | The queue's correctness lives in SQL, so the SQL should be visible and reviewed as written. Each file applies in one transaction, under an advisory lock | `tests/integration/test_schema.py` |
| 59 | `asyncpg` as the Postgres driver | Async, fast, and supports the LISTEN/NOTIFY wake-ups the queue needs | Review |
| 60 | Schema guarantees tested directly: one result per job, idempotency keys, lease required while running, `SKIP LOCKED` claims, priority order | These are the promises the README makes, so each has a test | 6 tests passing |
| 62 | `judge-prod` runs under a separate standard Windows account | WSL reads `C:\` as the Windows account that started it, so Windows permissions hide your profile even through a hand-mounted drive. The account should also get its own WSL VM and kernel. You create the account by hand | **Verified 13 Nov.** As `judgehost`, root still mounted `C:\`, but reading `C:\Users\Saran` was blocked, and the kernel boot id differed from the dev distro's. As `Aum`, both were reachable and shared. `spike/results/2026-11-13-judgehost-isolation.txt` |
| 63 | The import script feeds the check in with `bash -s` instead of copying it into the distro first | The copy step failed silently, so the first `judgehost` run used the old script and tested nothing | Same file |
| 61 | Dev database on `127.0.0.1:55432` | Docker Desktop's `documind` database already answers on 5432 | Found when the password was rejected |
| 57 | Code reaches `judge-prod` as a tar piped from Windows, followed by `chmod` | `judge-prod` can't see `C:\`, and Windows `tar` drops Linux permissions, so every binary first failed with "permission denied" | Same file |
