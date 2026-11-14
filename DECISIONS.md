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
| 64 | Queue moves are each one statement or one short transaction, in `core/queue.py` | Each move is all-or-nothing, so a crash at any moment leaves the job in a valid state | 12 queue tests |
| 65 | One `_RETIRE` statement handles both a worker's release and the reaper: cancelled, dead with IE, or requeued | A job can never be marked dead without its IE result, and a cancelled job is never re-run | Tests for the crash-during-cancel and three-crash cases |
| 66 | Backoff (10 s × attempts) also applies when the reaper requeues | A job that crashes workers shouldn't be retried instantly | Test for the backoff |
| 67 | `request_cancel` decides "cancel now" or "ask the worker" in one `UPDATE` under the row lock | It can't race with a worker claiming the same job | Cancel tests |
| 68 | Fencing removed on purpose made the frozen-worker test fail (stale heartbeat accepted) | Proves the test checks what it claims | Run on 13 Nov |
| 69 | MLE rule: `RLIMIT_AS` = limit + 48 MB (under the 64 MB cgroup headroom). MLE when peak RSS passes the limit, when a failed program printed `std::bad_alloc` or `MemoryError`, or when the sandbox was OOM-killed | The old "failed at 90% of the limit" rule would call a C++ program allocating in 16 MB blocks a runtime error | `test_judge.py` memory tests, C++ and Python |
| 70 | One Dockerfile: a stage builds `judge-runner`, then both the C++ and Python images copy it in | Every sandbox image carries the same runner, with no host files mounted | `deploy/images/Dockerfile` |
| 71 | Runner passes UTF-8 bytes through instead of escaping each one | Escaping byte by byte garbled g++'s curly quotes into mojibake; the compile-error test caught it | `test_judge.py` |
| 72 | Worker runs on the host for now | Its last-resort `pkill` of a wedged sandbox needs host process IDs. A worker in a container would need `--pid=host`; decide at deployment | Review |
| 73 | Output storage: `run_code` keeps its output, `verify_code` keeps the first failing test's output, submissions keep output only for a failing sample test | Hidden-test output is never stored | `test_judge.py` hidden-test case |
| 74 | **Gate 1 passed in the dev distro.** A worker killed with SIGKILL mid-job: lease expired, job requeued, orphan sandbox removed by its token label, job reclaimed with token 2, one AC result | The core crash-safety claim, measured | `spike/results/2026-11-13-gate1.txt`. Still to repeat in `judge-prod` |
| 75 | Job creation locks the owner's row first, then checks idempotency, rate limit, in-flight cap and queue cap in one transaction | Requests from one owner run one after another, so 20 identical retries at once create exactly one job. A refused request rolls back and isn't counted, unlike the v3 plan | `tests/integration/test_jobs.py` |
| 76 | `NOTIFY job_cancel` triggers an immediate heartbeat; the heartbeat still reads the cancel from the database | The database stays the only source of truth, and a lost notification only delays a cancel to the next heartbeat. Measured 0.26 s from request to sandbox removed, against a 5 s heartbeat | `tests/integration/test_cancel.py` |
| 77 | Adversarial suite: 11 attacks, each ending in an ordinary verdict, never an infrastructure error | Proves the blame rule: no attack makes the judge retry or blame itself | `tests/adversarial/test_attacks.py` |
| 78 | `RLIMIT_AS` is per process, so a Python fork bomb's 64 copies can together outgrow the sandbox; the cgroup then kills it and the verdict is MLE | MLE is the right verdict for memory use by multiplying, and the submission stops at that failure anyway | Adversarial suite |
| 79 | Inside the sandbox, `kill(-1)` fails with "no such process" | The program can signal nothing: the runner is root and Linux spares PID 1 | Adversarial suite |
| 80 | **Fixed 13 Nov.** Files the program wrote in `/work` used to persist into its next test. The runner's new `--keep NAME` option empties `/work` before each run, except the program's own files, acting as the sandbox user | Every test starts clean, at no extra cost: the cleaning happens inside the same `docker exec` | Leak test passes; with cleaning switched off it failed |
| 81 | **Chaos test passed.** 500 jobs, 3 workers, 45 SIGKILLs over 250 s: all 500 completed with exactly one result and the right output; 459 took one attempt, 40 two, 1 three; no sandbox left | The second half of Gate 2, measured | `spike/results/2026-11-13-chaos.txt` |
| 82 | CI workflow written (`.github/workflows/ci.yml`): adds gVisor to GitHub's runner and runs every test except chaos | The first half of Gate 2. Not run yet: it needs a GitHub repo, and whether gVisor works on GitHub's runners is still unproven | Not yet run |
| 83 | The worker does not read the sandbox's cgroup. `worker/cgroup.py` was never written, and workers no longer need `/sys/fs/cgroup` | Probe 5 showed the runner's `wait4` numbers are accurate enough, so the cgroup stays only as a hard limit. Less access for the worker, and less code | Review, 13 Nov |
| 84 | **Done 14 Nov** (migration `0002`). `jobs.container_id` is unused and will be dropped | Reconciliation finds sandboxes by their job and token labels, and a job has two containers anyway | Review, 13 Nov |
| 85 | ARCHITECTURE.md brought in line with the code (v3.2): cancellation as built (67, 76), workers on the host (72), the real file layout, dates removed. A new §16 lists four gaps between the document and the code (two were fixed the same day: 86 to 88) | The design document must describe what exists. A gap that is written down gets fixed; one that is hidden gets published as a false claim | Review, 13 Nov. `/tmp` and `/dev/shm` persistence confirmed by a probe; the other three found by reading |
| 86 | **Fixed 13 Nov.** Sandboxes run with `--ipc none` and a read-only `/tmp`. Before, Docker's `/dev/shm` and a `/tmp` that gVisor adds by itself were both writable, and a file written in one test was readable in the next. `/tmp` also took a 100 MB file | The runner only empties `/work`, so `/work` must be the only writable place. Compilers already use `TMPDIR=/work` | `test_work_is_the_only_writable_place`; it failed before the flags were added |
| 87 | **Fixed 13 Nov.** If `docker inspect` cannot find the sandbox, that is an infrastructure failure. Before, it counted as "sandbox died" and gave RE, or CE during a compile | A program can stop its sandbox but cannot make Docker forget the container. Only something outside can remove it, so the submission must not be blamed | `tests/integration/test_vanished_sandbox.py`; it failed before the fix |
| 88 | **Fixed 13 Nov.** Reconciliation no longer treats an expired lease as proof of an orphan. It removes a sandbox only when the job's status or token has changed | The heartbeat can renew an expired lease the reaper hasn't processed. After a long database outage, another worker could remove a healthy sandbox whose owner then carried on. One component, the reaper, now decides a lease is dead. The cost: an orphan lives up to one reaper interval longer | Same file; it failed before the fix. Gate 1 crash test and the 500-job chaos test both pass again with the change |
| 89 | **Fixed 13 Nov.** Each program runs in its own IPC namespace (`unshare(CLONE_NEWIPC)` in the runner, before it drops to uid 65534). The sandbox gains `SYS_ADMIN` for this; only the root runner holds it, and the program's capabilities measured zero | System V shared memory lives in the kernel, not in a folder, so 86 didn't cover it: a segment from test 1 was readable in test 2. Listing and removing segments failed because gVisor answers `SHM_INFO` and `SHM_STAT` differently from Linux, so a namespace that dies with the program is the robust fix. Trade-off: one more capability for the root runner, inside gVisor | `test_shared_memory_does_not_survive_between_tests` (failed before); namespace probe in this session |
| 90 | **Fixed 13 Nov.** Sandboxes carry a `judge.instance` label, the worker's database name by default (`JUDGE_INSTANCE` overrides it). Reconciliation only lists its own instance | Reconciliation used to treat every labelled sandbox on the Docker host as its own. A worker for another database (here, a concurrent chaos run) removed a test's compile sandbox mid-compile, because that job wasn't in its database | `test_reconciliation_leaves_other_instances_alone` |
| 91 | Chaos re-run on the code with 89 and 90: 500 jobs, 42 SIGKILLs over 230 s. 499 completed with the right output; 1 ended dead with IE after 3 attempts; 465 took one attempt, 33 two, 2 three. Every job had exactly one result, and no sandbox was left | Gate 2's crash half still holds on the current code. The one IE is allowed by design (3 interrupted attempts), but the run didn't record why each attempt ended, so the chaos test now prints `last_error` for dead jobs | `spike/results/2026-11-13-chaos-rerun.txt` |
| 92 | **Fixed 14 Nov.** The worker's last write (finish, cancel or release) goes through `fenced_write`, which retries database errors with backoff (0.2 s doubling to 2 s) until the lease would have run out, then gives up and leaves the job to the reaper. Before, one error there escaped `run_job`, ended the worker process and lost the verdict | Retrying is safe only because every write is fenced and is one transaction. In the ambiguous case, where the commit landed but the reply was lost, the retry finds the job no longer `running` and writes nothing. Retrying past the lease is pointless, because the reaper may have given the job to another worker | `tests/integration/test_final_commit.py`: a retried commit, a lost reply that must not save twice, and a give-up at the lease. All three failed on the old code |
| 93 | **Fixed 14 Nov.** A `listen_loop` task owns the LISTEN connection, named `judge-listen <worker id>`. It reconnects with backoff (1 s doubling to 30 s) when asyncpg's termination listener fires or a `SELECT 1` every 30 s fails. After each connect it wakes the claim loop and the running job's heartbeat, because notifications sent while it was down are lost | The connection was opened once and never checked. After a Postgres restart, jobs still moved through polling, but instant wake-up and fast cancel were gone until the worker restarted. The `SELECT 1` catches a half-open connection, one whose peer vanished without closing it, which never fires the termination listener | `tests/integration/test_listen_reconnect.py`: it failed on the old code given only the connection name. With the fix, a notified job was claimed 0.20 s after a reconnect, with a 60 s poll. A real `docker compose restart postgres` under a worker: listening again 1.6 s after the restart began, job claimed 0.05 s after its NOTIFY, process still alive. `spike/results/2026-11-14-postgres-restart.txt` |
| 94 | **Fixed 14 Nov.** The claim loop clears its wake-up flag before the claim query, not after it | A lost wakeup: a `job_new` arriving while the claim query ran was erased by the later `clear()`, so the worker slept a full poll interval with a job waiting. Polling bounded the damage to 2 s, which is why no test caught it. Found while reading the loop for 93 | By reading; the reconnect test exercises the loop |
| 95 | **Gate 2 passed.** CI run 1 on GitHub (`Aum-Mangal/CrashSafe-Judge`) installed gVisor on GitHub's `ubuntu-24.04` runner and passed every test except chaos in 1 m 41 s | Proves 82's open question: gVisor works on GitHub's runners, so the attack tests run in real sandboxes on a clean machine for every push. With 81 and 91, both halves of Gate 2 hold | CI run 43436499678. Logs need a signed-in viewer, so the test-by-test output was not checked here |
| 57 | Code reaches `judge-prod` as a tar piped from Windows, followed by `chmod` | `judge-prod` can't see `C:\`, and Windows `tar` drops Linux permissions, so every binary first failed with "permission denied" | Same file |

## API (14 Nov 2026)

| # | Change | Why | Evidence |
|---|---|---|---|
| 96 | Passwords are hashed with argon2id at OWASP's minimum settings (19 MiB, 2 passes, 1 lane), in a thread | The library default (64 MiB, 3 passes) would let four logins at once use most of the API's 0.3 GB (§3). Hashing in the event loop would stall every other request while it runs | Dev distro, median of 20 checks: 20.8 ms with these settings, 52.9 ms with the default. `test_api.py` checks only an `$argon2id$` hash is stored |
| 97 | A login returns an HS256 JWT holding only the user's id, valid 12 h. Each request reloads the user from the database. The decoder accepts only HS256, and `exp` is required. The API refuses to start without a `JWT_SECRET` of 32 characters or more | Reloading the user means deleting an account or changing a role works at once, at the cost of one primary-key lookup. Pinning the algorithm refuses unsigned (`alg: none`) tokens. A short secret can be guessed offline from any one token | `test_api.py`: garbage, wrong-secret, expired, unsigned and never-expiring tokens get 401, and so does a deleted user's token |
| 98 | Registration needs the invite code of a contest that hasn't ended. It doesn't tie the user to that contest, since the schema has no participants table. Login gives the same answer for an unknown email and a wrong password, and checks a dummy hash for an unknown email so both take as long | Keeps sign-ups to people we invited without adding a table. Equal answers and timing stop login from revealing who has an account. Registering a taken email still answers 409, which only invite holders can see | `test_api.py` |
| 99 | The visibility rule is one SQL condition in `core/problems.py`, used by REST and later by MCP. A problem that isn't visible answers exactly like a missing one: 404, same body | One copy of the rule can't disagree with another. A 403 would tell people a contest problem exists before the start | Removing `starts_at <= now()` from the rule made 2 tests fail: the problem was listed early, and a submission to it was accepted |
| 100 | A submission with `contest_id` needs the problem in that contest and the database time in `[starts_at, ends_at)`, and gets priority 0. Without it, only a public problem is accepted, at priority 1. The check runs in the same transaction as the insert. A retry with the same `Idempotency-Key` gets its job even after the contest ends | `now()` is fixed for a whole transaction, so the time checked is exactly the `submitted_at` stored: nothing can slip in after the end. A request accepted at the last moment whose reply was lost must not turn into a 403 on retry | `test_api.py`: a submission after the end gets 403, and a retry after the end gets the original job with 200 |
| 101 | Open: login and registration have no rate limit yet | argon2 makes each password guess cost about 21 ms, but nothing stops a client from making many. To fix before the contest | ARCHITECTURE.md §16 |
