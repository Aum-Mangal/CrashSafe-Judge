"""Small helpers shared by the tests."""

import hashlib
import json
import uuid


async def insert_job(
    conn, *, key=None, owner_id=1, priority=2, kind="run", language="python",
    source="print(1)", stdin=None, tests=None, problem_id=None,
):
    """Inserts a queued job and returns its id."""
    return await conn.fetchval(
        """
        INSERT INTO jobs (kind, owner_type, owner_id, idempotency_key, request_hash,
                          language, source, stdin, tests, problem_id, priority)
        VALUES ($1, 'client', $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
        RETURNING id
        """,
        kind, owner_id, key, hashlib.sha256(b"request").digest(), language, source, stdin,
        json.dumps(tests) if tests is not None else None, problem_id, priority,
    )


async def insert_problem(conn, tests, *, time_limit_ms=1000, memory_limit_mb=256, checker="token"):
    """Inserts a problem and its tests, given as (input, expected, is_sample) tuples, and returns its id."""
    problem_id = await conn.fetchval(
        """
        INSERT INTO problems (slug, title, statement_md, time_limit_ms, memory_limit_mb, checker)
        VALUES ($1, 'Test problem', '', $2, $3, $4)
        RETURNING id
        """,
        f"p-{uuid.uuid4().hex[:8]}", time_limit_ms, memory_limit_mb, checker,
    )
    for ordinal, (test_input, expected, is_sample) in enumerate(tests, 1):
        await conn.execute(
            """INSERT INTO testcases (problem_id, ordinal, input_text, expected_text, is_sample)
               VALUES ($1, $2, $3, $4, $5)""",
            problem_id, ordinal, test_input, expected, is_sample,
        )
    return problem_id


async def expire_lease(conn, job_id):
    """Pretends the worker stopped long enough ago that its lease ran out."""
    await conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = $1", job_id)


async def skip_backoff(conn, job_id):
    """Pretends the retry backoff has passed."""
    await conn.execute("UPDATE jobs SET not_before = now() WHERE id = $1", job_id)
