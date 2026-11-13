"""Gate 1: kill a worker with SIGKILL in the middle of a job. Another worker must recover the job
and record exactly one correct verdict, and no sandbox may be left behind."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from helpers import insert_job, insert_problem

pytestmark = pytest.mark.sandbox

ROOT = Path(__file__).resolve().parents[2]
# Short timings so the test takes seconds. Production uses the defaults in worker/main.py.
FAST = {
    "LEASE_SECONDS": "4",
    "HEARTBEAT_SECONDS": "1",
    "REAP_SECONDS": "1",
    "RECONCILE_SECONDS": "2",
    "POLL_SECONDS": "0.5",
}
SLOW_SOLUTION = "import time\ntime.sleep(3)\nprint('done')"


async def start_worker(dsn, name, log_dir):
    log = open(log_dir / f"{name}.log", "wb")
    env = {**os.environ, **FAST, "DATABASE_URL": dsn, "WORKER_ID": name}
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "worker.main", cwd=ROOT, env=env, stdout=log, stderr=log
    )


async def sandboxes_for(job_id):
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-aq", "--filter", f"label=judge.job={job_id}", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    return out.split()


async def eventually(check, timeout, what, log_dir):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await check():
            return
        await asyncio.sleep(0.2)
    logs = "\n".join(f"--- {p.name}\n{p.read_text(errors='replace')[-3000:]}" for p in sorted(log_dir.glob("*.log")))
    raise AssertionError(f"timed out waiting for {what}\n{logs}")


async def test_a_killed_workers_job_recovers_to_one_verdict(dsn, db, tmp_path):
    problem = await insert_problem(db, [("", "done\n", False)], time_limit_ms=2000)
    job = await insert_job(db, kind="submission", problem_id=problem, priority=0, source=SLOW_SOLUTION)

    async def status():
        return await db.fetchval("SELECT status FROM jobs WHERE id = $1", job)

    async def judging_in_a_sandbox():
        return await status() == "running" and len(await sandboxes_for(job)) > 0

    async def completed():
        return await status() == "completed"

    async def no_sandboxes_left():
        return await sandboxes_for(job) == []

    first = await start_worker(dsn, "worker-a", tmp_path)
    try:
        await eventually(judging_in_a_sandbox, 60, "worker A to start judging", tmp_path)
        await asyncio.sleep(1)  # the program is now sleeping inside the sandbox
    finally:
        first.kill()  # SIGKILL: no cleanup, no goodbye
        await first.wait()

    second = await start_worker(dsn, "worker-b", tmp_path)
    try:
        await eventually(completed, 90, "worker B to finish the job", tmp_path)
        await eventually(no_sandboxes_left, 30, "every sandbox for the job to be removed", tmp_path)
    finally:
        second.kill()
        await second.wait()

    row = await db.fetchrow("SELECT attempts, worker_id FROM jobs WHERE id = $1", job)
    assert (row["attempts"], row["worker_id"]) == (2, "worker-b")
    verdicts = await db.fetch("SELECT verdict FROM results WHERE job_id = $1", job)
    assert [r["verdict"] for r in verdicts] == ["AC"]
