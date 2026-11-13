"""Chaos: 500 jobs, three workers, and one worker killed with SIGKILL and restarted every few
seconds. Afterwards every job must have exactly one result, attached to the right job, and no
sandbox may be left over (ARCHITECTURE.md §13, the first row).

Not part of the normal run. Start it on purpose with: scripts/dev-test.sh -m chaos -s
"""

import asyncio
import collections
import os
import random

import pytest

from helpers import eventually, insert_job, sandboxes_for, start_worker

pytestmark = pytest.mark.chaos

JOBS = int(os.environ.get("CHAOS_JOBS", "500"))
WORKERS = 3
GIVE_UP_SECONDS = 1200


async def test_no_job_is_lost_or_judged_twice(dsn, db, tmp_path):
    for n in range(JOBS):
        await insert_job(db, source=f"print({n})")

    loop = asyncio.get_running_loop()
    started = loop.time()
    workers = {slot: await start_worker(dsn, f"w{slot}-0", tmp_path) for slot in range(WORKERS)}
    kills = 0
    try:
        while await db.fetchval("SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')"):
            assert loop.time() - started < GIVE_UP_SECONDS, "the jobs didn't finish in time"
            await asyncio.sleep(random.uniform(3, 8))
            slot = random.randrange(WORKERS)
            workers[slot].kill()
            await workers[slot].wait()
            kills += 1
            workers[slot] = await start_worker(dsn, f"w{slot}-{kills}", tmp_path)
        elapsed = loop.time() - started

        async def no_sandboxes_left():
            return await sandboxes_for() == []

        # Orphans from killed workers are removed by the survivors' reconciliation
        await eventually(no_sandboxes_left, 30, "orphan sandboxes to be removed", tmp_path)
    finally:
        for proc in workers.values():
            proc.kill()
            await proc.wait()

    rows = await db.fetch(
        """
        SELECT j.status, j.attempts, j.source, j.last_error, count(r.job_id) AS results,
               max(r.verdict) AS verdict, max(r.stdout_trunc) AS stdout
        FROM jobs j LEFT JOIN results r ON r.job_id = j.id
        GROUP BY j.id
        """
    )
    attempts = collections.Counter(r["attempts"] for r in rows)
    statuses = collections.Counter(r["status"] for r in rows)
    print(
        f"\n{JOBS} jobs, {WORKERS} workers, {kills} SIGKILLs, {elapsed:.0f} s"
        f"\nstatuses: {dict(statuses)}"
        f"\nattempts per job: {dict(sorted(attempts.items()))}"
    )
    # Why each dead job's last attempt ended: a lease that expired after a kill, or something else
    for r in rows:
        if r["status"] == "dead":
            print(f"dead job: {r['source']!r}, last error: {r['last_error']}")

    assert len(rows) == JOBS
    # Nothing lost and nothing judged twice
    assert all(r["results"] == 1 for r in rows)
    for r in rows:
        if r["status"] == "dead":
            # Allowed only for a job that was interrupted on every attempt
            assert (r["verdict"], r["attempts"]) == ("IE", 3)
        else:
            # The right output on the right job: print(n) must have printed n
            expected = r["source"].removeprefix("print(").removesuffix(")") + "\n"
            assert (r["status"], r["verdict"], r["stdout"]) == ("completed", "OK", expected)
