"""A sandbox that disappears is never the submission's fault (ARCHITECTURE.md §7, blame rule).

A program can make its sandbox exit, but it cannot make Docker forget the container. So a
container Docker no longer knows was removed by someone else, and the job must be retried.
"""

import uuid

import pytest

from core import queue
from helpers import expire_lease, insert_job, sandboxes_for
from worker.main import reconcile
from worker.sandbox import DockerError, Limits, Sandbox

pytestmark = pytest.mark.sandbox

PYTHON = "/usr/local/bin/python3"


async def test_a_removed_sandbox_is_an_infrastructure_failure():
    box = await Sandbox.create("python", 64, uuid.uuid4(), 1, "test-vanished")
    await box.remove()  # what another worker's reconciliation would do
    with pytest.raises(DockerError):
        await box.run([PYTHON, "-c", "print(1)"], b"", Limits.for_time(1000, 64))


async def test_reconciliation_leaves_an_expired_lease_to_the_reaper(db, pool):
    instance = await db.fetchval("SELECT current_database()")
    job = await insert_job(db)
    claimed = await queue.claim(db, "worker-a")
    box = await Sandbox.create("python", 64, job, claimed.token, instance)
    try:
        # The lease ran out, but the worker may only be slow: its next heartbeat can still renew it
        await expire_lease(db, job)
        await reconcile(pool, instance)
        assert await sandboxes_for(job) != []

        # Once the reaper has taken the job away, the sandbox really is an orphan
        await queue.reap(db)
        await reconcile(pool, instance)
        assert await sandboxes_for(job) == []
    finally:
        await box.remove()


async def test_reconciliation_leaves_other_instances_alone(db, pool):
    # A sandbox from another database's worker: its job isn't in this database,
    # so without the instance label this worker would see it as an orphan and remove it
    job = uuid.uuid4()
    box = await Sandbox.create("python", 64, job, 1, "some-other-database")
    try:
        await reconcile(pool, await db.fetchval("SELECT current_database()"))
        assert await sandboxes_for(job) != []
    finally:
        await box.remove()
