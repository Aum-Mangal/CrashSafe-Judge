"""Small helpers shared by the tests."""

import asyncio
import contextlib
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Short timings so crash tests take seconds. Production uses the defaults in worker/main.py.
FAST_WORKER = {
    "LEASE_SECONDS": "4",
    "HEARTBEAT_SECONDS": "1",
    "REAP_SECONDS": "1",
    "RECONCILE_SECONDS": "2",
    "POLL_SECONDS": "0.5",
}


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


async def judge_job(db, pool, **job):
    """Inserts a job, claims it and judges it in a real sandbox, returning the Result."""
    from core import queue
    from worker.judge import judge

    job_id = await insert_job(db, **job)
    claimed = await queue.claim(db, "test-worker")
    assert claimed.job_id == job_id
    return await judge(pool, claimed, await db.fetchval("SELECT current_database()"))


async def start_worker(dsn, name, log_dir, settings=FAST_WORKER):
    """Starts a real worker process, logging to log_dir/<name>.log."""
    log = open(log_dir / f"{name}.log", "wb")
    env = {**os.environ, **settings, "DATABASE_URL": dsn, "WORKER_ID": name}
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "worker.main", cwd=ROOT, env=env, stdout=log, stderr=log
    )


async def sandboxes_for(job_id=None):
    """IDs of sandbox containers for one job, or for every job when job_id is None."""
    label = f"judge.job={job_id}" if job_id else "judge.job"
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-aq", "--filter", f"label={label}", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    return out.split()


async def eventually(check, timeout, what, log_dir=None):
    """Polls check() until it's true, or fails with the workers' logs attached."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await check():
            return
        await asyncio.sleep(0.2)
    logs = ""
    if log_dir is not None:
        logs = "\n".join(
            f"--- {p.name}\n{p.read_text(errors='replace')[-3000:]}" for p in sorted(log_dir.glob("*.log"))
        )
    raise AssertionError(f"timed out waiting for {what}\n{logs}")


TEST_SECRET = "test-secret-" + "x" * 32


@contextlib.asynccontextmanager
async def open_api(dsn, **options):
    """The API app, called in-process: httpx speaks ASGI to it directly, with no network."""
    import httpx

    from api.app import create_app

    app = create_app(dsn, TEST_SECRET, **options)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://judge") as client:
            yield client


async def make_contest(conn, starts_in, ends_in, problem_ids=(), code="invite-123", title="Mock"):
    """A contest starting and ending this many seconds from now (negative for the past)."""
    contest = await conn.fetchval(
        """
        INSERT INTO contests (title, invite_code, starts_at, ends_at)
        VALUES ($1, $2, now() + make_interval(secs => $3), now() + make_interval(secs => $4))
        RETURNING id
        """,
        title, code, starts_in, ends_in,
    )
    for n, problem in enumerate(problem_ids):
        await conn.execute(
            "INSERT INTO contest_problems (contest_id, problem_id, label) VALUES ($1, $2, $3)",
            contest, problem, chr(ord("A") + n),
        )
    return contest
