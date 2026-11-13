"""Creating jobs: idempotency, rate limits and caps (ARCHITECTURE.md §6, Create)."""

import asyncio
import uuid

import asyncpg
import pytest

from core.jobs import Caps, IdempotencyConflict, NewJob, Rejected, create_job


async def new_client(db, max_inflight=5):
    return await db.fetchval(
        "INSERT INTO api_clients (name, key_hash, max_inflight) VALUES ('test', $1, $2) RETURNING id",
        uuid.uuid4().hex, max_inflight,
    )


async def new_user(db):
    return await db.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", f"{uuid.uuid4().hex}@test"
    )


def run_job(owner_id, owner_type="client", **changes):
    fields = {"kind": "run", "owner_type": owner_type, "owner_id": owner_id,
              "language": "python", "source": "print(1)", "priority": 2}
    return NewJob(**{**fields, **changes})


async def test_creates_a_queued_job(db):
    client = await new_client(db)
    job_id, created = await create_job(db, run_job(client))
    assert created
    assert await db.fetchval("SELECT status FROM jobs WHERE id = $1", job_id) == "queued"


async def test_a_retry_with_the_same_key_gets_the_same_job(db):
    client = await new_client(db)
    first, _ = await create_job(db, run_job(client, idempotency_key="k1"))
    again, created = await create_job(db, run_job(client, idempotency_key="k1"))
    assert (again, created) == (first, False)


async def test_the_same_key_for_a_different_request_is_a_conflict(db):
    client = await new_client(db)
    await create_job(db, run_job(client, idempotency_key="k1"))
    with pytest.raises(IdempotencyConflict):
        await create_job(db, run_job(client, idempotency_key="k1", source="print(2)"))


async def test_twenty_simultaneous_retries_create_one_job(dsn, db):
    client = await new_client(db, max_inflight=50)
    pool = await asyncpg.create_pool(dsn, min_size=20, max_size=20)

    async def attempt():
        async with pool.acquire() as conn:
            return await create_job(conn, run_job(client, idempotency_key="same"))

    try:
        outcomes = await asyncio.gather(*(attempt() for _ in range(20)))
    finally:
        await pool.close()

    assert len({job_id for job_id, _ in outcomes}) == 1
    assert sum(created for _, created in outcomes) == 1
    assert await db.fetchval("SELECT count(*) FROM jobs") == 1


async def test_too_many_requests_in_a_minute_are_refused(db):
    client = await new_client(db, max_inflight=100)
    caps = Caps(per_minute=3)
    for _ in range(3):
        await create_job(db, run_job(client), caps)
    with pytest.raises(Rejected) as refused:
        await create_job(db, run_job(client), caps)
    assert refused.value.reason == "rate limit"
    assert 1 <= refused.value.retry_after <= 60


async def test_too_many_jobs_in_flight_are_refused(db):
    client = await new_client(db, max_inflight=2)
    for _ in range(2):
        await create_job(db, run_job(client))
    with pytest.raises(Rejected) as refused:
        await create_job(db, run_job(client))
    assert refused.value.reason == "too many jobs in flight"


async def test_users_have_their_own_inflight_cap(db):
    user = await new_user(db)
    caps = Caps(user_inflight=1)
    await create_job(db, run_job(user, owner_type="user"), caps)
    with pytest.raises(Rejected):
        await create_job(db, run_job(user, owner_type="user"), caps)


async def test_a_full_queue_refuses_new_jobs(db):
    client = await new_client(db, max_inflight=10)
    caps = Caps(global_queued=2)
    for _ in range(2):
        await create_job(db, run_job(client), caps)
    with pytest.raises(Rejected) as refused:
        await create_job(db, run_job(client), caps)
    assert refused.value.reason == "queue full"


async def test_an_unknown_owner_is_an_error(db):
    with pytest.raises(LookupError):
        await create_job(db, run_job(999_999))


async def test_creating_a_job_wakes_workers(dsn, db):
    listener = await asyncpg.connect(dsn)
    received = asyncio.Queue()
    await listener.add_listener("job_new", lambda conn, pid, channel, payload: received.put_nowait(payload))
    try:
        job_id, _ = await create_job(db, run_job(await new_client(db)))
        assert await asyncio.wait_for(received.get(), timeout=5) == str(job_id)
    finally:
        await listener.close()
