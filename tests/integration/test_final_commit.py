"""The worker's last write survives a database blip (ARCHITECTURE.md §6).

Each test swaps the judge for one that answers at once, so no sandbox is needed, and makes
queue.finish fail the way a dropped connection does.
"""

import asyncio
from datetime import timedelta

from core import queue
from core.queue import Result
from helpers import expire_lease, insert_job
from worker import main
from worker.main import Settings, run_job

LEASE = timedelta(seconds=2)


def settings(dsn):
    return Settings(
        dsn=dsn, worker_id="w1", lease=LEASE, heartbeat=0.5, poll=1, reap_every=1, reconcile_every=60,
        instance="test",
    )


async def answers_ok(pool, claim, instance):
    return Result("OK", stdout="1\n")


async def claimed_job(db, monkeypatch):
    monkeypatch.setattr(main, "judge", answers_ok)
    job = await insert_job(db)
    claimed = await queue.claim(db, "w1", LEASE)
    return job, claimed


async def outcome(db, job):
    row = await db.fetchrow(
        """
        SELECT j.status, count(r.job_id) AS results, max(r.verdict) AS verdict
        FROM jobs j LEFT JOIN results r ON r.job_id = j.id
        WHERE j.id = $1 GROUP BY j.id
        """,
        job,
    )
    return tuple(row)


async def test_a_failed_final_commit_is_retried(dsn, db, pool, monkeypatch):
    job, claimed = await claimed_job(db, monkeypatch)
    real_finish, calls = queue.finish, []

    async def drops_twice(conn, *args):
        calls.append(1)
        if len(calls) <= 2:
            raise ConnectionResetError("connection reset by peer")
        return await real_finish(conn, *args)

    monkeypatch.setattr(queue, "finish", drops_twice)
    await run_job(pool, settings(dsn), claimed, asyncio.Event())

    assert len(calls) == 3
    assert await outcome(db, job) == ("completed", 1, "OK")


async def test_a_commit_whose_reply_was_lost_is_not_saved_twice(dsn, db, pool, monkeypatch):
    # The worst case: the commit lands, but the reply never reaches the worker, so it retries.
    # The fence must turn the retry into a no-op.
    job, claimed = await claimed_job(db, monkeypatch)
    real_finish, calls = queue.finish, []

    async def commits_then_drops(conn, *args):
        calls.append(1)
        saved = await real_finish(conn, *args)
        if len(calls) == 1:
            raise ConnectionResetError("connection reset by peer")
        return saved

    monkeypatch.setattr(queue, "finish", commits_then_drops)
    await run_job(pool, settings(dsn), claimed, asyncio.Event())

    assert len(calls) == 2
    assert await outcome(db, job) == ("completed", 1, "OK")


async def test_the_worker_gives_up_when_the_lease_runs_out(dsn, db, pool, monkeypatch):
    job, claimed = await claimed_job(db, monkeypatch)

    async def database_down(conn, *args):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(queue, "finish", database_down)
    loop = asyncio.get_running_loop()
    started = loop.time()
    # Returns instead of raising, so the worker process lives on
    await asyncio.wait_for(run_job(pool, settings(dsn), claimed, asyncio.Event()), 10)
    elapsed = loop.time() - started

    assert elapsed < LEASE.total_seconds() + 0.5
    assert await outcome(db, job) == ("running", 0, None)
    # The job isn't lost: once the lease is gone, the reaper puts it back in the queue
    await expire_lease(db, job)
    assert await queue.reap(db) == [(job, "queued")]
