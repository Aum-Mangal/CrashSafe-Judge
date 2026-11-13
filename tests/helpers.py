"""Small helpers shared by the tests."""

import hashlib


async def insert_job(conn, *, key=None, owner_id=1, priority=2):
    """Inserts a queued run job and returns its id."""
    return await conn.fetchval(
        """
        INSERT INTO jobs (kind, owner_type, owner_id, idempotency_key, request_hash,
                          language, source, priority)
        VALUES ('run', 'client', $1, $2, $3, 'python', 'print(1)', $4)
        RETURNING id
        """,
        owner_id, key, hashlib.sha256(b"request").digest(), priority,
    )


async def expire_lease(conn, job_id):
    """Pretends the worker stopped long enough ago that its lease ran out."""
    await conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = $1", job_id)


async def skip_backoff(conn, job_id):
    """Pretends the retry backoff has passed."""
    await conn.execute("UPDATE jobs SET not_before = now() WHERE id = $1", job_id)
