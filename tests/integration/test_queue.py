"""The queue's behaviour, including the crash and race cases the README promises (ARCHITECTURE.md §5)."""

import asyncio

import asyncpg

from core import queue
from core.queue import Result
from helpers import expire_lease, insert_job, skip_backoff


async def test_claim_takes_the_most_urgent_job_and_issues_a_token(db):
    await insert_job(db, priority=2)
    urgent = await insert_job(db, priority=0)

    claimed = await queue.claim(db, "w1")

    assert (claimed.job_id, claimed.token, claimed.attempt) == (urgent, 1, 1)
    row = await db.fetchrow("SELECT status, worker_id FROM jobs WHERE id = $1", urgent)
    assert (row["status"], row["worker_id"]) == ("running", "w1")


async def test_claim_respects_the_backoff(db):
    job = await insert_job(db)
    await db.execute("UPDATE jobs SET not_before = now() + interval '1 hour' WHERE id = $1", job)
    assert await queue.claim(db, "w1") is None


async def test_heartbeat_extends_the_lease_and_reports_a_cancel(db):
    job = await insert_job(db)
    claimed = await queue.claim(db, "w1")

    assert await queue.heartbeat(db, job, "w1", claimed.token) is False
    await queue.request_cancel(db, job)
    assert await queue.heartbeat(db, job, "w1", claimed.token) is True


async def test_a_frozen_worker_cannot_write_after_its_job_moves_on(db):
    """Worker 1 freezes, its lease expires and worker 2 takes over. When worker 1 wakes up,
    its heartbeat and its result are both refused, so only worker 2's result is saved."""
    job = await insert_job(db)
    old = await queue.claim(db, "w1")
    await expire_lease(db, job)
    assert await queue.reap(db) == [(job, "queued")]
    await skip_backoff(db, job)
    new = await queue.claim(db, "w2")
    assert new.token == old.token + 1

    assert await queue.heartbeat(db, job, "w1", old.token) is None
    assert await queue.finish(db, job, "w1", old.token, Result("WA")) is False
    assert await queue.finish(db, job, "w2", new.token, Result("AC")) is True
    assert await db.fetchval("SELECT verdict FROM results WHERE job_id = $1", job) == "AC"


async def test_a_job_that_keeps_crashing_ends_dead_with_an_internal_error(db):
    job = await insert_job(db)
    for attempt in (1, 2, 3):
        assert (await queue.claim(db, "w1")).attempt == attempt
        await expire_lease(db, job)
        retired = await queue.reap(db)
        await skip_backoff(db, job)

    assert retired == [(job, "dead")]
    assert await db.fetchval("SELECT verdict FROM results WHERE job_id = $1", job) == "IE"
    assert await queue.claim(db, "w1") is None


async def test_a_cancel_survives_a_worker_crash(db):
    """Found in review: a crash during a cancel must not put the job back in the queue."""
    job = await insert_job(db)
    await queue.claim(db, "w1")
    assert await queue.request_cancel(db, job) == "running"

    await expire_lease(db, job)

    assert await queue.reap(db) == [(job, "cancelled")]


async def test_cancelling_a_queued_job_is_immediate(db):
    job = await insert_job(db)
    assert await queue.request_cancel(db, job) == "cancelled"
    assert await queue.claim(db, "w1") is None


async def test_a_late_cancel_leaves_the_result_alone(db):
    job = await insert_job(db)
    claimed = await queue.claim(db, "w1")
    await queue.finish(db, job, "w1", claimed.token, Result("AC"))

    assert await queue.request_cancel(db, job) == "completed"


async def test_the_worker_confirms_a_cancel(db):
    job = await insert_job(db)
    claimed = await queue.claim(db, "w1")
    await queue.request_cancel(db, job)

    assert await queue.finish_cancelled(db, job, "w1", claimed.token) is True
    assert await db.fetchval("SELECT status FROM jobs WHERE id = $1", job) == "cancelled"


async def test_release_requeues_after_a_backoff(db):
    job = await insert_job(db)
    claimed = await queue.claim(db, "w1")

    assert await queue.release(db, job, "w1", claimed.token, "docker timeout") == "queued"
    assert await queue.claim(db, "w2") is None
    await skip_backoff(db, job)
    assert (await queue.claim(db, "w2")).attempt == 2


async def test_finish_wakes_listeners(dsn, db):
    listener = await asyncpg.connect(dsn)
    received = asyncio.Queue()
    await listener.add_listener("job_done", lambda conn, pid, channel, payload: received.put_nowait(payload))
    try:
        job = await insert_job(db)
        claimed = await queue.claim(db, "w1")
        await queue.finish(db, job, "w1", claimed.token, Result("OK"))

        assert await asyncio.wait_for(received.get(), timeout=5) == str(job)
    finally:
        await listener.close()


async def test_many_workers_claim_every_job_exactly_once(dsn, db):
    jobs = {await insert_job(db) for _ in range(60)}
    pool = await asyncpg.create_pool(dsn, min_size=10, max_size=10)
    claimed = []

    async def worker(n):
        while True:
            async with pool.acquire() as conn:
                job = await queue.claim(conn, f"w{n}")
            if job is None:
                return
            claimed.append(job.job_id)

    try:
        await asyncio.gather(*(worker(n) for n in range(10)))
    finally:
        await pool.close()

    assert len(claimed) == len(jobs)
    assert set(claimed) == jobs
