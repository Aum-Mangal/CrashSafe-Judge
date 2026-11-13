"""Guarantees the schema enforces by itself (ARCHITECTURE.md §4 to §6)."""

import asyncpg
import pytest

from db.migrate import migrate
from helpers import insert_job

# The claim query from ARCHITECTURE.md §6
CLAIM = """
    SELECT id FROM jobs
    WHERE status = 'queued' AND not_before <= now()
    ORDER BY priority, created_at
    LIMIT 1 FOR UPDATE SKIP LOCKED
"""


async def test_migrations_apply_only_once(dsn):
    assert await migrate(dsn) == []


async def test_a_job_has_at_most_one_result(db):
    job = await insert_job(db)
    await db.execute("INSERT INTO results (job_id, verdict) VALUES ($1, 'OK')", job)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute("INSERT INTO results (job_id, verdict) VALUES ($1, 'OK')", job)


async def test_idempotency_key_is_unique_per_owner(db):
    await insert_job(db, key="abc")
    with pytest.raises(asyncpg.UniqueViolationError):
        await insert_job(db, key="abc")
    # Another owner may reuse the key, and requests without a key never clash
    await insert_job(db, key="abc", owner_id=2)
    await insert_job(db)
    await insert_job(db)


async def test_a_running_job_must_have_a_lease(db):
    job = await insert_job(db)
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = $1", job)


async def test_two_workers_never_claim_the_same_job(dsn, db):
    first, second = await insert_job(db), await insert_job(db)
    workers = [await asyncpg.connect(dsn) for _ in range(3)]
    try:
        # Each worker keeps its transaction open, as it would while marking the job running
        transactions = [w.transaction() for w in workers]
        for t in transactions:
            await t.start()
        claimed = [await w.fetchval(CLAIM) for w in workers]

        assert {claimed[0], claimed[1]} == {first, second}
        # Both jobs are locked, so the third worker skips them instead of waiting
        assert claimed[2] is None

        for t in transactions:
            await t.rollback()
    finally:
        for w in workers:
            await w.close()


async def test_contest_jobs_are_claimed_before_agent_jobs(db):
    await insert_job(db, priority=2)
    contest_job = await insert_job(db, priority=0)  # created later, but more urgent
    assert await db.fetchval(CLAIM) == contest_job
