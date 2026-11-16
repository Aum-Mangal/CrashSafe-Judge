# Roadmap

**Dates dropped on 13 Nov.** We fell behind, so the work now follows this order and its gates, with no deadlines. The day labels below only show the order. The contest date is set once Gate 3 passes.

Each week ends with a **gate**: a demo that must work before the next week starts. If a gate fails, we fix it or use the cut list. We don't move on with it broken.

## Week 0 and 1: Prove the risky parts, then build the core loop (2 to 8 Nov)

**Goal:** one submission goes through queue, sandbox and verdict in `judge-prod`, and survives a worker being killed.

| Day | Work | What you'll learn |
|---|---|---|
| Mon 2 | Set up WSL2 Ubuntu, Docker Engine, runsc. `git init` inside `judge/`. (Oracle dropped on 3 Nov.) | Containers, runtimes, what gVisor is |
| Tue 3 | Spike probes 1 to 5 on OOM, fork bombs, cleanup, timing. | cgroups, syscalls, how sandboxes fail |
| Wed 4 | Spike probes 6 to 10. Build `judge-runner`. Fill in DECISIONS.md. | fork/exec, uids, rlimits, signals |
| Thu 5 | Create `judge-prod` with Windows access switched off. Repeat the spike there. Postgres schema and migrations. | WSL isolation, schema design, transactions |
| Fri 6 | Create, claim and finish, with concurrency tests. | Row locks, `SKIP LOCKED`, race conditions |
| Sat 7 | Worker loop, sandbox wrapper, verdicts, heartbeat. | asyncio, leases, tasks |
| Sun 8 | Reaper and fencing. Catch-up. | Fencing tokens, at-least-once delivery |

**Gate 1:** in `judge-prod`, submit a C++ solution, `kill -9` the worker mid-run, and see the job recover to one correct verdict.

## Week 2: Make it hard to break (9 to 15 Nov)

**Goal:** every attack gets a correct final verdict, and every crash recovers.

| Day | Work | What you'll learn |
|---|---|---|
| Mon 9 | Python language, checkers, compile limits. | Language runtimes, calibration |
| Tue 10 | Adversarial suite. | Thinking like an attacker |
| Wed 11 | GitHub repo, CI with runsc. | CI/CD |
| Thu 12 | Idempotency, rate limits, inflight and global caps. | Idempotency, backpressure |
| Fri 13 | Cancellation on all three paths. | LISTEN/NOTIFY, async races |
| Sat 14 | Reconciliation and the 500-job crash test. | Failure testing, orphan handling |
| Sun 15 | Catch-up. | |

**Gate 2:** CI runs the adversarial suite under runsc. 500 jobs under random `kill -9` give no lost jobs, no duplicate results and no leaked containers.

## Week 3: Real users (16 to 22 Nov)

**Goal:** a person can compete, and an AI agent can use the engine.

| Day | Work | What you'll learn |
|---|---|---|
| Mon 16 | Auth, problem and submission routes, visibility rule. | JWT, API design, authorization |
| Tue 17 | Contest pages (server-rendered, plain and simple). | Keeping the frontend small |
| Wed 18 | Leaderboard, admin CLI, rejudge. | SQL window functions |
| Thu 19 | Leakage tests. Write the contest problems (you lead). | Testing for absence of data |
| Fri 20 | MCP base server and tools. | How MCP works |
| Sat 21 | Tasks layer and the no-Tasks path. Go/no-go on the contest date and on a stable URL. | Async protocols, reading a spec |
| Sun 22 | Real client demo. Catch-up. | |

**Gate 3:** a friend completes a mock contest in a browser. A real MCP client runs `verify_code` and gets a result after a reconnect.

## Week 4: Prove it and ship it (23 to 30 Nov)

**Goal:** real numbers, a real contest, and a README that shows both.

Started on 16 Nov with Gate 3 still open, at Aum's call (DECISIONS.md, 134). Gate 3 still has to pass before the contest.

| Day | Work | What you'll learn |
|---|---|---|
| Mon 23 | `bootstrap.sh`, encrypted OneDrive backups, keep-alive task, reboot test. | Automation, backups, Windows services |
| Tue 24 | Disaster-recovery drill: delete `judge-prod` and rebuild. Tunnel restart test. | Recovery planning |
| Wed 25 | k6 load test, calibration, timing and overhead numbers. | Load testing, percentiles |
| Thu 26 | Full dry run with friends. Fix what breaks. | Operating a live system |
| Fri 27 | Contest checklist. Freeze the code. | Release discipline |
| Sat 28 | **Contest** (target date, confirmed on 21 Nov). | Incident handling |
| Sun 29 | Publish contest numbers. Write the README. | Writing up results |
| Mon 30 | Demo video, resume bullets, interview practice. | Explaining your work |

**Gate 4:** the README publishes measured results, and the contest has run or the dry run stands in as evidence.

## Stretch: agent benchmark (only if all gates pass)

Run a local LLM on the laptop (Ollama, so ₹0) against N problems, with and without the `verify_code` feedback loop, and publish the solve-rate difference. Verdicts come from the judge itself, so scoring needs no LLM judge. This is the honest way to add agent evaluation to the project.

## Cut list, in order

1. Agent benchmark
2. MCP registry listing
3. Leaderboard polish
4. `list_problems` and `get_problem`
5. Tasks extension (keep MCP tools with `get_result`)
6. Live contest (replace with a recorded dry run)

**Never cut:** crash tests, the adversarial suite, measured numbers.

## Risks

| Risk | Plan |
|---|---|
| Power cut, internet outage or Windows reboot during the contest | Stay plugged in and pause updates. Test recovery on 24 Nov. Jobs survive through leases, but the contest pauses. Keep the dry-run recording as backup evidence |
| A spike result breaks an assumption | Change the design that day and record it. Finding this early is the reason for the spike |
| Less than 3 hours a day | Use the cut list from the top, and move the contest rather than skip testing |
| Exams or a college clash | Move the contest. Evidence can come from the dry run |
