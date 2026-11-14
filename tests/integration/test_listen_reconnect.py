"""The worker's LISTEN connection comes back after Postgres drops it (ARCHITECTURE.md §6).

pg_terminate_backend ends that one connection the way a Postgres restart ends all of them,
without disturbing other tests that share the server.
"""

import asyncio

import pytest

from helpers import FAST_WORKER, eventually, insert_job, start_worker

pytestmark = pytest.mark.sandbox

# A 60 s poll, so a claim within a few seconds can only come from a notification
SLOW_POLL = {**FAST_WORKER, "POLL_SECONDS": "60"}


async def listener_pid(db):
    return await db.fetchval(
        """SELECT pid FROM pg_stat_activity
           WHERE datname = current_database() AND application_name LIKE 'judge-listen %'"""
    )


async def test_the_worker_listens_again_after_its_connection_is_dropped(dsn, db, tmp_path):
    async def listening():
        return await listener_pid(db) is not None

    worker = await start_worker(dsn, "worker", tmp_path, SLOW_POLL)
    try:
        await eventually(listening, 30, "the worker to LISTEN", tmp_path)
        old = await listener_pid(db)
        assert await db.fetchval("SELECT pg_terminate_backend($1)", old)

        async def listening_again():
            pid = await listener_pid(db)
            return pid is not None and pid != old

        await eventually(listening_again, 15, "the worker to LISTEN again", tmp_path)

        job = await insert_job(db)
        loop = asyncio.get_running_loop()
        started = loop.time()
        # What core.jobs.create_job sends when it queues a job
        await db.execute("SELECT pg_notify('job_new', $1::uuid::text)", job)

        async def claimed():
            return await db.fetchval("SELECT status FROM jobs WHERE id = $1", job) != "queued"

        await eventually(claimed, 10, "the job to be claimed", tmp_path)
        elapsed = loop.time() - started

        async def completed():
            return await db.fetchval("SELECT status FROM jobs WHERE id = $1", job) == "completed"

        # Let it finish, so killing the worker leaves no sandbox behind
        await eventually(completed, 60, "the job to finish", tmp_path)
    finally:
        worker.kill()
        await worker.wait()

    print(f"\nnotification to claim after a reconnect: {elapsed:.2f} s")
    assert elapsed < 3, f"took {elapsed:.2f} s, so the notification didn't wake the worker"
