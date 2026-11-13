# Architecture: Crash-Safe Code Execution & Verification Engine

Status: v3.1 (3 Nov 2026), hosted on the laptop. Build window: 2 to 30 Nov 2026.
Cost target: ₹0 recurring. Availability: best-effort, no uptime guarantee.
Why anything changed from v2: see [DECISIONS.md](DECISIONS.md). Build order: see [ROADMAP.md](ROADMAP.md).

Values marked **[S]** are starting guesses. The Day 1 spike (§14) replaces them with measured values.

---

## 0. What this is

A crash-safe engine that runs untrusted code in sandboxes and returns judged results, with two interfaces:

1. **Online judge (humans):** submit a solution, get AC/WA/TLE/MLE/OLE/RE/CE. Used to host a real college contest.
2. **MCP server (AI agents):** agents call `run_code`, `verify_code`, or `submit_solution`. Long runs return a durable task handle through the MCP Tasks extension (`io.modelcontextprotocol/tasks`).

**Execution semantics:** at-least-once execution, at-most-one persisted final result.

**What this adds to prior art (Judge0, Piston, DOMjudge, E2B):** test-proven crash safety, published adversarial, chaos and recovery results, and durable MCP Tasks. It is built to learn and measure, not to compete.

---

## 1. System overview

```
Humans (browser, REST, polling)      AI agents (MCP Streamable HTTP, stateless)
              \                          /
        Cloudflare Tunnel (cloudflared, outbound only, TLS at Cloudflare)
                         |
               API (FastAPI, 1 process)            no Docker socket
                         |
               PostgreSQL 16                       single source of truth
                 jobs (= queue), results, limits,
                 users, clients, problems, contests
                 LISTEN/NOTIFY = wake-up only
                         ^
          claim / heartbeat / finish (fenced)
                         |
             Worker 1        Worker 2              Docker socket + /sys/fs/cgroup (ro)
                 |               |
         compile container  ->  run container      gVisor, one pair per job
         no network, read-only rootfs, cgroup limits
                                 |
                          judge-runner             enforces limits per test, reports result
```

Principles:
- PostgreSQL is the single source of truth. Notifications only make things faster, and the system still works if every one is lost.
- The API never executes code and never touches Docker.
- Workers never talk to clients.

---

## 2. Zero-cost stack

The judge runs on the laptop. Oracle was dropped on 3 Nov because it only accepts credit cards for verification.

| Need | Choice | Notes |
|---|---|---|
| Host | The laptop: Windows 11 + WSL2 | 8 CPUs, 16 GB RAM, of which WSL2 gets about 7.8 GB |
| Dev distro | `Ubuntu-24.04` | Windows drives mounted, for editing code |
| Production distro | `judge-prod`, under a separate standard Windows account | Windows drives and programs switched off, your files hidden by Windows permissions (§10) |
| Runtime | Docker Engine, gVisor (runsc), PostgreSQL 16, FastAPI | Open source |
| Language | Python 3.12 (API, worker), C (judge-runner) | asyncio for all concurrency |
| Public access | Cloudflare Quick Tunnel (`cloudflared`) | No account, domain or open ports. Random URL that changes on restart |
| Stable URL (optional) | Decided by 21 Nov (§12) | Only needed for the uptime check and the MCP Registry |
| CI | GitHub Actions, public repo, `ubuntu-24.04` | Same architecture (amd64) as production |
| Backups | Encrypted nightly `pg_dump` copied into the OneDrive folder | OneDrive syncs it off the laptop. Keep at most 14 |
| Tooling | pytest, k6, MCP Inspector, OBS | Free |

**Cost rules:** no paid tiers anywhere. Public GitHub repo and standard runners only. No card entered anywhere.

---

## 3. Resource budget (WSL2: 8 CPUs, ~7.8 GB)

| Component | Memory | CPU |
|---|---|---|
| PostgreSQL | ~1 GB (shared_buffers 256 MB) | shared |
| API | ~0.3 GB | shared |
| cloudflared | ~0.05 GB | shared |
| 2 workers | ~0.3 GB | shared |
| 2 active sandboxes | ≤ 2 × 1 GB | `--cpus 1` each |
| WSL + headroom | ~4 GB | |

The laptop has more CPU than the free VM would have had, so a third worker may fit. The load test decides that. Windows apps share the same CPUs, so nothing else heavy runs during the contest.

Swap must be off inside WSL, because reports show runsc memory limits can leak into swap. `swapoff -a` only lasts until WSL restarts, so `swap=0` goes in `.wslconfig`.

---

## 4. Data model (PostgreSQL)

```
users            id, email UNIQUE, password_hash, role ('participant'|'admin'), created_at
api_clients      id, name, key_hash, max_inflight (default 5), created_at
problems         id, slug UNIQUE, title, statement_md, time_limit_ms, memory_limit_mb,
                 output_limit_kb (default 16384), checker ('exact'|'token'),
                 is_public BOOL
testcases        id, problem_id, ordinal, input_text, expected_text, is_sample
                 UNIQUE(problem_id, ordinal)
contests         id, title, invite_code, starts_at, ends_at
contest_problems contest_id, problem_id, label

rate_limits      owner_type, owner_id, window_start, count
                 PK(owner_type, owner_id, window_start)

jobs             id UUID default gen_random_uuid()      -- also the MCP taskId
                 kind ('submission'|'run'|'verify')
                 owner_type ('user'|'client'), owner_id
                 idempotency_key NULL, request_hash
                   -- sha256 of kind, language, source, stdin, tests, problem, contest
                 language ('cpp'|'python'), source, stdin, tests JSONB,
                 problem_id NULL, contest_id NULL
                 priority SMALLINT                      -- 0 contest, 1 practice, 2 agent
                 status ('queued'|'running'|'completed'|'cancelled'|'dead')
                 attempts, max_attempts (3), not_before
                 worker_id, lease_until, fencing_token BIGINT, container_id
                 cancel_requested BOOL, last_error
                 submitted_at, created_at, started_at, finished_at, expires_at
                 rejudge_of UUID NULL, superseded BOOL DEFAULT false

                 UNIQUE(owner_type, owner_id, idempotency_key)
                   WHERE idempotency_key IS NOT NULL
                 index (priority, created_at) WHERE status = 'queued'
                 index (status, lease_until)

results          job_id PK REFERENCES jobs ON DELETE CASCADE
                 verdict ('AC'|'WA'|'TLE'|'MLE'|'OLE'|'RE'|'CE'|'OK'|'IE')
                 per_test JSONB   -- list of {verdict, cpu_ms, wall_ms, peak_kb}
                 peak_memory_kb
                 failed_test INT NULL
                 stdout_trunc, stderr_trunc, compile_log   -- 64 KB caps
                 created_at
```

Rules:
- `results.job_id` as primary key is what makes "at most one result" a database guarantee.
- **Hidden tests store no program output.** `stdout_trunc` and `stderr_trunc` stay NULL for hidden tests. Data that is never stored cannot leak through an API bug. A program could otherwise echo hidden input to stderr.
- `IE` is written only for jobs that went dead.

**Leaderboard:** ICPC ranking from `jobs` joined with `results`, using `submitted_at`, excluding `superseded` jobs, cached 5 s. CE and IE add no penalty.

---

## 5. Job lifecycle

```
create -> queued --claim--> running --fenced commit--> completed
           |  ^                |
    cancel |  +--- requeue ----+  lease expired or infra failure, attempts < max
           v                   |
       cancelled <-------------+  cancel requested (worker commit, or reaper after crash)
                               |
                               +-> dead + IE result    lease expired, attempts >= max
```

Guarantees, stated exactly in the README:
- **At-least-once execution.** A worker that crashes before commit causes re-execution elsewhere. This is safe because the sandbox has no network and cannot cause outside effects.
- **At-most-one persisted result.** Enforced by the primary key and the fenced update.
- **Cancel vs complete.** First fenced commit wins. A cancelled job is never re-executed, even if its worker crashed.
- **Clock safety.** Lease timestamps come only from Postgres.

---

## 6. Queue logic

**Create** (one transaction)
1. Lock the owner's row.
2. If the idempotency key exists for this owner: same `request_hash` returns the existing job, different hash returns 409.
3. Rate-limit upsert. Over the limit: reject with retry-after, keeping the counter.
4. Owner's queued + running ≥ `max_inflight`: reject.
5. Global queued count over the cap: reject.
6. Insert as queued. NOTIFY `job_new`.

**Claim** (one transaction)
```sql
SELECT id FROM jobs
WHERE status = 'queued' AND not_before <= now()
ORDER BY priority, created_at
LIMIT 1 FOR UPDATE SKIP LOCKED;
```
Then set `running`, `worker_id`, `lease_until = now() + 30s`, `attempts + 1`, `fencing_token + 1`, `started_at`.

**Worker wake-up:** claim on `job_new`, **and** poll every 2 s. Polling is the guarantee, NOTIFY is the optimisation. After finishing a job, claim again immediately.

**Heartbeat** (every 5 s, own task)
```sql
UPDATE jobs SET lease_until = now() + interval '30 seconds'
WHERE id = $1 AND worker_id = $2 AND fencing_token = $3 AND status = 'running'
RETURNING cancel_requested;
```
Zero rows means the lease is lost: kill the containers and abandon the work.

**Finish** (one transaction): set `completed` with the same fenced `WHERE`. Zero rows: rollback. Otherwise insert the result, commit, NOTIFY `job_done`.

**Release** (infra failure): fenced update back to `queued` with `not_before = now() + 10s × attempts`, then NOTIFY `job_new`. Failures caused by the submission never use this path (§7).

**Reaper** (every 15 s, in every worker, using `FOR UPDATE SKIP LOCKED`). Each worker waits one heartbeat after connecting before it runs, so live jobs can renew leases after a Postgres restart. For expired running jobs:
1. `cancel_requested`: set `cancelled`.
2. `attempts < max_attempts`: requeue and NOTIFY `job_new`.
3. Otherwise: set `dead` and insert an IE result in the same transaction.

It also deletes jobs past `expires_at`. That removes source, stdin, tests and, by cascade, the result. Contest jobs have no expiry.

**Reconciliation** (worker startup + every 60 s): list containers labelled `judge.job`. Keep one only if its job is `running`, its `judge.token` label equals the current `fencing_token`, and the lease is live. Kill and remove the rest. The token check catches an old orphan while a retry of the same job runs.

---

## 7. Sandbox and judging

**Runtime:** Docker with `--runtime=runsc` on sandbox containers only. runsc is not the daemon default.

**Why two containers per job:** a container has one memory limit, and compile (512 MB) needs a different limit from the run (problem limit). The worker pipes the binary from one to the other, capped at 32 MB.

**Container flags**
```
--runtime=runsc --network none --read-only
--tmpfs /work:rw,exec,size=64m,uid=65534,gid=65534,mode=0700
--memory X --memory-swap X              (X = limit + 64 MB; gVisor itself used about 13 MB)
--pids-limit 512                        (host backstop only; hitting it kills the sandbox. 64 guest processes used 162)
--ulimit nproc=64                       (guest limit; gVisor returns EAGAIN, proven in probe 2)
--cpus 1
--cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add KILL
--security-opt no-new-privileges
--user 65534
--label judge.job=<id> --label judge.token=<fencing_token>
```
No host mounts. No secrets in the environment. The container's main process is `sleep`.

**judge-runner:** a small static C program, baked into the read-only image. Every compile and test runs through it, started with `docker exec -i --user 0`. Killing `docker exec` does not stop the process inside, so limits must be enforced from inside the sandbox.

For each execution it:
1. Forks the program with its own session, rlimits (CPU, address space, processes, file size, open files, no core dumps), a fixed environment and uid 65534.
2. Enforces the wall limit (3 × time limit, or the compile limit) with SIGKILL.
3. Forwards stdout up to the output limit, then kills the program.
4. Collects the exit status and `wait4` usage (CPU time, peak RSS).
5. Kills and reaps every remaining uid 65534 process, so nothing survives into the next test. The runner is a child subreaper, so orphans are handed to it rather than to PID 1, and no zombies are left.
6. Writes one JSON report line to its own stderr.

It runs as a different uid from the program. If they shared one, the submission could read the runner's file descriptors and forge its own verdict.

**Per job**
1. Create the compile container. Store its ID on the job.
2. Send source through `docker exec -i` (`docker cp` cannot reach a tmpfs).
3. Compile: `g++ -std=c++20 -O2 -pipe` with a precompiled `bits/stdc++.h`, 10 s wall, 512 MB. In the spike, one compile took about 0.9 s and peaked at 347 MB. Failure gives CE. Python uses `py_compile` with the same CE path.
4. Stream the binary out, remove the compile container, create the run container, stream the binary in.
5. For each test, pipe input to the runner and read the output and report.
6. Contest submissions stop at the first failure. `verify_code` runs all tests.
7. Remove the container.

**Verdict order for each test**

| Check | Verdict |
|---|---|
| Runner killed the program at the output limit | OLE |
| CPU over limit, or wall limit hit | TLE |
| Peak RSS over the limit, or the program failed with `std::bad_alloc` or `MemoryError` in stderr | MLE |
| Sandbox died with Docker `OOMKilled` true (poll for up to 3 s) | MLE |
| Sandbox died otherwise (fork bomb hitting the pids backstop) | RE |
| Non-zero exit or signal | RE |
| Checker mismatch | WA |
| Otherwise | AC |

**Blame rule:** if the submission caused it, it gets a final verdict and is never retried. Otherwise a malicious program could burn all three attempts and an IE. Only Docker errors and timeouts count as infrastructure failures.

If the sandbox dies during `verify_code`, the current test gets its verdict and the rest are marked `skipped`.

**Docker itself can hang.** In the spike, one wedged sandbox blocked every Docker command for about 10 minutes. Every Docker call has a timeout. If `docker rm -f` times out, the worker kills that container's runsc processes directly, by container ID.

**Never use `--oom-score-adj -1000`** on sandboxes. It caused that wedge (DECISIONS.md, probe 1).

**Measurement**
- CPU and peak memory are per test, from the runner. The container cgroup is a cross-check and a hard limit.
- The output limit (default 16 MB) is for judging. The 64 KB cap is only for what gets stored.
- Limits are calibrated with runsc in `judge-prod`. Overhead against plain runc is published.

**Job budgets**

| Tool | Limits |
|---|---|
| `run_code` | 10 s CPU, 256 MB |
| `verify_code` | 20 tests, 2 s each, 30 s total CPU |
| Contest submission | Problem limits, stop at first failure |

**Worker concurrency:** all Docker calls are async subprocesses with timeouts. Heartbeat and the cancel listener are separate asyncio tasks and never wait behind a Docker call.

---

## 8. Cancellation

1. Cancel accepted by REST `DELETE`, `tasks/cancel`, or `cancel_run`.
2. `UPDATE ... SET status='cancelled' WHERE id=$1 AND status='queued'`. One row means done.
3. Zero rows: `SET cancel_requested = true WHERE status='running'`, then NOTIFY `job_cancel`.
4. **Fast path:** the owning worker hears NOTIFY, runs `docker kill`, commits `cancelled` with the fenced update.
5. **Fallback:** the next heartbeat returns `cancel_requested`.
6. **Crash path:** the reaper sets `cancelled` when the lease expires.

Published: fast-path p50/p95/p99, and the fallback bound of heartbeat interval + kill time.

---

## 9. Interfaces

### REST
- `POST /auth/register` (requires a contest invite code), `POST /auth/login`
- `GET /problems`, `GET /problems/{slug}`
- `POST /submissions` (header `Idempotency-Key`), `GET /submissions/{id}`, `DELETE /submissions/{id}`
- `GET /contests/{id}/leaderboard`
- `GET /health/live`, `GET /health/ready`
- `/metrics` on an internal port only

**Problem visibility:** a problem is visible only if `is_public`, or if it belongs to a contest that has started. Otherwise return 404. This applies to REST and MCP, and has a CI test.

**Contest window:** a submission is accepted only between `starts_at` and `ends_at`, by database time.

**Hidden-test rule:** results expose verdict, failing test index, time and memory only. Samples may show diffs.

### Admin CLI (no web UI)
`python -m judge.admin` runs inside `judge-prod`:
- `import-problem <folder>`
- `create-contest`, `create-client`
- `rejudge --problem <slug> --contest <id>`: creates new jobs with `rejudge_of` set, the original `submitted_at`, and marks the old ones `superseded`. Old results stay for audit.

### MCP `/mcp` (Streamable HTTP, stateless)
- **Auth:** Bearer API key resolves to an `api_clients` row. Keys are issued by hand with the admin CLI.
- **Tools:**
  - `run_code(language, source, stdin?, idempotency_key?)`
  - `verify_code(language, source, tests[], idempotency_key?)`
  - `submit_solution(problem_slug, language, source, idempotency_key?)` (public practice problems only)
  - `list_problems()`, `get_problem(slug)`
  - Fallback: `get_result(job_id)`, `cancel_run(job_id)`

The spec does not cover `tools/call` retries, so the optional `idempotency_key` argument is how an agent retries safely.

**Tasks extension (matches the 2026-07-28 spec)**
- Advertise it in `server/discover`.
- Return a task only if this request's `_meta` declared `io.modelcontextprotocol/tasks`.
- `CreateTaskResult`: `resultType: "task"`, `taskId`, `status: "working"`, `createdAt`, `lastUpdatedAt`, `ttlMs: 86400000`, `pollIntervalMs: 1000`. Commit the job first.
- `tasks/get` reads the jobs table on every call, with an ownership check.
- `tasks/update`: a known ID gets an empty acknowledgement, since no input is ever requested. Unknown ID gets `-32602`.
- `tasks/cancel`: always an empty acknowledgement for a known ID. Cancellation is eventually consistent.
- Unknown or expired task: `-32602`.

| Job status | Task status | Payload |
|---|---|---|
| queued, running | `working` | None |
| completed (any verdict, including WA) | `completed` | Tool result |
| cancelled | `cancelled` | None |
| dead | `failed` | JSON-RPC `error` (`-32603`) |

`input_required` is never used. A wrong answer is a successful judging, so it is `completed`.

**Clients without Tasks:** wait up to 20 s for `job_done`, otherwise return `job_id` and tell the agent to call `get_result`. Treat this as the main path, because client support is uneven.

**Implementation:** use the official Python SDK v2 for the base protocol and hand-write the Tasks layer. Week 3 starts by checking that the SDK lets a `tools/call` return a task result. If not, `/mcp` becomes a small hand-written JSON-RPC route.

---

## 10. Security

**Defended:** resource exhaustion, network egress, filesystem escape and tampering, most kernel exploits (gVisor intercepts syscalls), tool injection (no tool accepts paths, URLs or shell), cross-client task access, hidden-test leakage, verdict forgery, early problem leakage.

**Privilege split:** only workers hold the Docker socket and the cgroup filesystem. The API has neither. Workers expose no ports.

**Hosting on a personal laptop raises the stakes.** A full escape to Windows needs two independent bugs: one in gVisor, then one in the Linux kernel or WSL. Mitigations:
- Production runs in its own distro, `judge-prod`, with `[automount] enabled=false` and `[interop] enabled=false` in `/etc/wsl.conf`. Tested on 4 Nov: root there cannot start Windows programs, even after re-registering the handler. **Root can still mount `C:\` by hand** (`mount -t drvfs`), so automount off removes casual access but is not a boundary against root (DECISIONS.md, 55). Becoming root in `judge-prod` takes a gVisor escape followed by a kernel exploit from inside gVisor's own seccomp jail, or a compromise of the worker.
- **Fix chosen on 4 Nov (DECISIONS.md, 62):** `judge-prod` is registered under a separate standard Windows account. A hand-mounted `C:\` only shows what that account may read, and Windows permissions keep your profile out of reach. It also gets its own WSL virtual machine and kernel, away from the dev distro. Both were verified on 13 Nov with `deploy/check-isolation.sh`.
- `judge-prod` holds no personal files, browser data or keys other than the project's own.
- The tunnel exposes only the API. The laptop opens no inbound ports.

**Not defended, stated honestly:**
- gVisor bugs, or a kernel exploit that bypasses gVisor.
- All WSL2 distros of one Windows account share one Linux kernel. Under its own account, `judge-prod` runs in a separate virtual machine from the dev distro (verified 13 Nov: different kernel boot ids).
- The judge account can still read and write whatever any standard user can, such as `C:\Users\Public` and parts of `C:\ProgramData`.
- Worker compromise equals compromise of `judge-prod`. The Docker socket is root-equivalent.
- Timing side channels. Failing test index and timing are visible by design, as on all judges. Rate limits reduce this but do not remove it.

**Data:** source and output are never logged. Agent jobs and results expire after 24 h. API keys are hashed and compared in constant time. Backups contain emails and password hashes, so they are encrypted with `age` before being copied to OneDrive.

---

## 11. Failure modes

| Failure | Handling |
|---|---|
| Worker crash | Lease expires, reaper requeues, fencing blocks stale writes, reconciliation removes the orphan |
| Job fails 3× | `dead` + IE result |
| Crash during cancel | Reaper sets `cancelled`, never requeues |
| Missed NOTIFY | 2 s claim poll, heartbeat covers cancel |
| Postgres restart | API 503 via readiness, workers reconnect and re-LISTEN, reaper waits one heartbeat |
| API restart | Clients retry with the same idempotency key |
| Docker hang | Timeouts on every call, then release with backoff |
| Program OOM | Program killed, MLE, sandbox survives |
| Sandbox death | Classified by the blame rule, never retried |
| Leaked container | Token-aware sweep every 60 s and at startup |
| Duplicate submit | Same hash returns the job, different hash returns 409 |
| Request flood | Rate limit, then per-owner cap, then global cap |
| Laptop reboot or WSL shutdown | Keep-alive task restarts `judge-prod` at logon, containers restart, jobs recover through leases |
| Tunnel restart | New random URL, announced to users (a stable URL fixes this, §12) |
| Power or internet cut | Contest pauses. Jobs recover on return. Covered in the contest checklist |
| Distro lost or corrupted | `bootstrap.sh` + latest backup from OneDrive |

---

## 12. Deployment and availability

**Compose services:** `cloudflared`, `api`, `postgres`, `worker-1`, `worker-2`, all with `restart: unless-stopped`. Workers mount `/var/run/docker.sock` and `/sys/fs/cgroup:ro`. Sandbox images `judge-cpp` and `judge-python` are amd64.

**Production distro:** `deploy/bootstrap.sh` creates `judge-prod` from a clean Ubuntu 24.04 image and writes its `wsl.conf` (systemd on, automount off, interop off). It then runs `setup-host.sh`, clones the repo from GitHub, restores the latest backup and starts Compose. Code reaches `judge-prod` only through `git pull`, because it cannot see `C:\`.

**Keeping it running.** WSL stops a distro when nothing on the Windows side is using it, and systemd does not prevent that. A Task Scheduler task at logon keeps a long-lived `wsl.exe -d judge-prod` process running. WSL only runs while its Windows account is signed in, so the judge account stays signed in in the background (fast user switching) or a scheduled task runs as it. Week 4 verifies that:
- the distro survives closing every terminal
- everything comes back after a reboot and login
- sleep is off while plugged in, and Windows Update is paused over contest week

**Public URL.** A Cloudflare Quick Tunnel needs no account or domain. Its limits are a random URL that changes when `cloudflared` restarts, 200 in-flight requests, no Server-Sent Events and no uptime guarantee. The design already polls and uses Streamable HTTP, so these are acceptable for the contest.

By 21 Nov, decide whether a stable URL is worth it. Only the hourly uptime check and the MCP Registry need one. Options to check then: Tailscale Funnel (free personal plan, `*.ts.net` address), or a free student domain with a named Cloudflare Tunnel. If neither works, cut those two features.

**Availability is best-effort.**
1. `bootstrap.sh` rebuilds `judge-prod` in under 30 minutes.
2. One disaster-recovery drill: delete `judge-prod`, rebuild, publish the time and data loss.
3. Encrypted nightly backups copied into OneDrive.
4. README results and a demo video carry the project even when the laptop is off.

**CI**
- Every PR: lint, unit tests, integration tests against Postgres, adversarial suite under runsc on `ubuntu-24.04`, short crash test, leakage and visibility tests.
- Nightly: full chaos suite and benchmarks, from cron in `judge-prod`.

---

## 13. Test matrix (published evidence)

| Claim | Test |
|---|---|
| ≤ 1 result, no lost jobs | 500 jobs with repeated random `kill -9` of workers |
| Survives DB restart | Restart Postgres under load; all jobs reach a terminal state |
| Idempotency | 20 concurrent same-key creates give 1 job; different payload gives 409 |
| Isolation | Fork bomb, memory bomb, infinite loop, sleep, network, fs write, `/etc/passwd`, 1 GB output, compile bomb, under runsc |
| Blame rule | Each attack gets a final verdict with `attempts = 1` and never IE |
| Verdict integrity | Program tries to forge or tamper with the runner's report |
| Test isolation | Background process from test 1 is gone before test 2 |
| Hidden tests stay hidden | No API path returns hidden data; stderr echo stores nothing |
| Problem visibility | Contest problem returns 404 before the start, on REST and MCP |
| Cancellation | Fast-path percentiles, fallback bound, crash-during-cancel never re-executes, 0 leaked containers |
| Orphan cleanup | Kill a worker mid-run; orphan is removed even while the retry runs |
| Lost NOTIFY | With NOTIFY disabled, every job still completes |
| Fair timing | Same program × 50: CPU vs wall variance, runsc vs runc overhead |
| Large output | A valid 1 MB answer gets AC; an output flood gets OLE |
| MCP correctness | Opt-in gets a handle; no opt-in gets a result or job_id; reconnect retrieves the result; cross-client access denied |
| Throughput | k6 at 1, 2 and 3 workers in `judge-prod` |
| Disaster recovery | `judge-prod` deleted, rebuilt from script and backup in N minutes |
| Windows isolation | `deploy/check-isolation.sh` as `judgehost`: Windows programs unreachable, your files unreadable even through a hand-mounted `C:\`, separate kernel from the dev distro |

---

## 14. Day 1 spike: what must be proven

No judging code is written until these have measured answers.

| # | Question | Decides |
|---|---|---|
| 1 | Does `--oom-score-adj -1000` keep the sandbox alive when a program is OOM-killed? | MLE detection |
| 2 | Does `--ulimit nproc` stop a fork bomb inside runsc? | pids strategy |
| 3 | What is the lowest stable `--pids-limit` for g++ and a normal program? | Backstop value |
| 4 | Does `kill(-1)` from the runner clean up background processes under gVisor? | Test isolation |
| 5 | Does `wait4` usage track real CPU time and peak memory under gVisor? | Timing source |
| 6 | Where is the cgroup for a runsc container with Docker's systemd driver? | Worker cgroup reads |
| 7 | How much memory does gVisor add per sandbox? | The `X` in `--memory` |
| 8 | Do `SETUID`, `SETGID` and `KILL` work with `no-new-privileges` under runsc? | Runner design |
| 9 | Compile time with and without the precompiled header? | Compile limit, capacity |
| 10 | Cost of container start and of `docker exec`? | Throughput estimate |

Follow-ups added after probes 1 to 5:
- **1b:** does an in-guest memory limit (`RLIMIT_AS`, set by the runner) stop a program before the cgroup kills the whole sandbox?
- **3b:** how many host pids does a sandbox use with 64 guest processes, so the backstop can be set safely?
- **4b:** does `--init` work under runsc and reap killed processes?

All were answered on 3 and 4 Nov in the dev distro (DECISIONS.md).

The runner sets `RLIMIT_AS` to the problem limit plus 48 MB, below the cgroup's 64 MB headroom. A program can therefore pass its limit, and be judged MLE on its real peak RSS, before the cgroup could kill the sandbox. An allocation that fails at the cap surfaces as `std::bad_alloc` or `MemoryError`, which also counts as MLE (DECISIONS.md, 69). Python under this cap was tested on 13 Nov.

---

## 15. Repository layout

```
judge/
  api/         app.py, auth.py, routes.py, ratelimit.py, pages/
  mcp/         server.py, tools.py, tasks.py, schemas.py
  core/        jobs.py, queue.py, reaper.py, reconcile.py
  worker/      main.py, sandbox.py, cgroup.py, checker.py, verdict.py
  runner/      judge_runner.c, Makefile
  admin/       __main__.py
  db/          models.py, migrations/
  deploy/      docker-compose.yml, setup-host.sh, bootstrap.sh, backup.sh, keepalive/, images/Dockerfile
  spike/       probes and results
  loadtest/    k6/
  agent_eval/  optional agent benchmark
  problems/    contest problem folders (hidden tests stay out of the public repo)
  .github/workflows/  ci.yml, healthcheck.yml
  tests/       unit/ integration/ adversarial/ chaos/ mcp/ leakage/
  README.md  ARCHITECTURE.md  DECISIONS.md  ROADMAP.md  THREAT_MODEL.md
```
