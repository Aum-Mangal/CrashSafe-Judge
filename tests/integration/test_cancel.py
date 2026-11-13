"""Cancelling a running job through the fast path (ARCHITECTURE.md §8).

The worker keeps the normal 5 s heartbeat here, so a cancel that finishes well under 5 s
proves NOTIFY job_cancel woke it, rather than the next heartbeat.
"""

import asyncio

import pytest

from core import queue
from helpers import FAST_WORKER, eventually, insert_job, sandboxes_for, start_worker

pytestmark = pytest.mark.sandbox

NORMAL_HEARTBEAT = {**FAST_WORKER, "HEARTBEAT_SECONDS": "5", "LEASE_SECONDS": "30"}


async def test_a_cancel_stops_the_sandbox_well_before_the_next_heartbeat(dsn, db, tmp_path):
    job = await insert_job(db, source="import time\ntime.sleep(60)")

    async def running_in_a_sandbox():
        status = await db.fetchval("SELECT status FROM jobs WHERE id = $1", job)
        return status == "running" and len(await sandboxes_for(job)) > 0

    async def cancelled_and_gone():
        status = await db.fetchval("SELECT status FROM jobs WHERE id = $1", job)
        return status == "cancelled" and await sandboxes_for(job) == []

    worker = await start_worker(dsn, "worker", tmp_path, NORMAL_HEARTBEAT)
    try:
        await eventually(running_in_a_sandbox, 60, "the job to start", tmp_path)
        await asyncio.sleep(1.5)  # past the syntax check, so the program is running

        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await queue.request_cancel(db, job) == "running"
        await eventually(cancelled_and_gone, 15, "the cancel to finish", tmp_path)
        elapsed = loop.time() - started
    finally:
        worker.kill()
        await worker.wait()

    print(f"\ncancel to sandbox removed: {elapsed:.2f} s")
    assert elapsed < 3, f"took {elapsed:.2f} s, so the fast path didn't fire"
    assert await db.fetchval("SELECT count(*) FROM results WHERE job_id = $1", job) == 0
