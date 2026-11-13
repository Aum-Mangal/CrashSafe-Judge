"""The job queue: claim, heartbeat, finish, release, reap and cancel (ARCHITECTURE.md §5, §6, §8).

PostgreSQL is the queue. Each function is one statement or one short transaction, every
timestamp comes from the database's clock, and every write a worker makes is fenced: it
only applies while that worker still holds the job with the same fencing token.
"""

import json
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

import asyncpg

LEASE = timedelta(seconds=30)
RESULT_TEXT_CAP = 64 * 1024

# A worker's write only applies if it still owns the job: same worker, same token, still running
FENCE = "id = $1 AND worker_id = $2 AND fencing_token = $3 AND status = 'running'"

# What happens to a running job that lost its worker: cancelled if a cancel was requested,
# dead with an IE result once out of attempts, otherwise back in the queue after a backoff.
# It's one statement, so the status change and the IE result can never be separated.
_RETIRE = """
WITH gone AS (
    UPDATE jobs SET
        status = CASE WHEN cancel_requested THEN 'cancelled'
                      WHEN attempts >= max_attempts THEN 'dead'
                      ELSE 'queued' END,
        finished_at = CASE WHEN cancel_requested OR attempts >= max_attempts THEN now() END,
        not_before = now() + attempts * interval '10 seconds',
        worker_id = NULL,
        lease_until = NULL,
        last_error = {error}
    WHERE {where}
    RETURNING id, status
),
internal_error AS (
    INSERT INTO results (job_id, verdict)
    SELECT id, 'IE' FROM gone WHERE status = 'dead'
)
SELECT id, status, pg_notify(CASE status WHEN 'queued' THEN 'job_new' ELSE 'job_done' END, id::text)
FROM gone
"""


@dataclass(frozen=True)
class Claim:
    """A job a worker now holds. The token proves ownership on every later write."""

    job_id: UUID
    token: int
    attempt: int
    kind: str
    language: str
    source: str
    stdin: str | None
    tests: list | None
    problem_id: int | None


@dataclass(frozen=True)
class Result:
    verdict: str
    per_test: list = field(default_factory=list)
    peak_memory_kb: int | None = None
    failed_test: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    compile_log: str | None = None


def _cap(text: str | None) -> str | None:
    """Cuts text to the 64 KB the results table allows, without splitting a character."""
    if text is None:
        return None
    return text.encode()[:RESULT_TEXT_CAP].decode(errors="ignore")


async def claim(conn: asyncpg.Connection, worker_id: str, lease: timedelta = LEASE) -> Claim | None:
    """Takes the most urgent queued job, or returns None if there is none.

    SKIP LOCKED makes concurrent workers pass over a row another worker is taking,
    instead of waiting for it or taking it twice.
    """
    row = await conn.fetchrow(
        """
        UPDATE jobs
        SET status = 'running', worker_id = $1, lease_until = now() + $2::interval,
            attempts = attempts + 1, fencing_token = fencing_token + 1, started_at = now()
        WHERE id = (
            SELECT id FROM jobs
            WHERE status = 'queued' AND not_before <= now()
            ORDER BY priority, created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, fencing_token, attempts, kind, language, source, stdin, tests, problem_id
        """,
        worker_id,
        lease,
    )
    if row is None:
        return None
    return Claim(
        job_id=row["id"],
        token=row["fencing_token"],
        attempt=row["attempts"],
        kind=row["kind"],
        language=row["language"],
        source=row["source"],
        stdin=row["stdin"],
        tests=json.loads(row["tests"]) if row["tests"] else None,
        problem_id=row["problem_id"],
    )


async def heartbeat(
    conn: asyncpg.Connection, job_id: UUID, worker_id: str, token: int, lease: timedelta = LEASE
) -> bool | None:
    """Extends the lease and returns whether a cancel was requested.

    Returns None if the lease is lost: another worker may already own the job,
    so the caller must stop and kill its containers.
    """
    return await conn.fetchval(
        f"UPDATE jobs SET lease_until = now() + $4::interval WHERE {FENCE} RETURNING cancel_requested",
        job_id,
        worker_id,
        token,
        lease,
    )


async def finish(conn: asyncpg.Connection, job_id: UUID, worker_id: str, token: int, result: Result) -> bool:
    """Saves the final result. Returns False, writing nothing, if the worker no longer owns the job."""
    async with conn.transaction():
        owned = await conn.fetchval(
            f"UPDATE jobs SET status = 'completed', lease_until = NULL, finished_at = now() WHERE {FENCE} RETURNING id",
            job_id,
            worker_id,
            token,
        )
        if owned is None:
            return False
        await conn.execute(
            """
            INSERT INTO results (job_id, verdict, per_test, peak_memory_kb, failed_test,
                                 stdout_trunc, stderr_trunc, compile_log)
            VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8)
            """,
            job_id,
            result.verdict,
            json.dumps(result.per_test),
            result.peak_memory_kb,
            result.failed_test,
            _cap(result.stdout),
            _cap(result.stderr),
            _cap(result.compile_log),
        )
        # Delivered when the transaction commits, never before
        await conn.execute("SELECT pg_notify('job_done', $1::uuid::text)", job_id)
    return True


async def finish_cancelled(conn: asyncpg.Connection, job_id: UUID, worker_id: str, token: int) -> bool:
    """Marks the job cancelled after the worker killed its containers. Fenced like finish."""
    row = await conn.fetchrow(
        f"""
        WITH done AS (
            UPDATE jobs SET status = 'cancelled', lease_until = NULL, finished_at = now()
            WHERE {FENCE} RETURNING id
        )
        SELECT id, pg_notify('job_done', id::text) FROM done
        """,
        job_id,
        worker_id,
        token,
    )
    return row is not None


async def release(conn: asyncpg.Connection, job_id: UUID, worker_id: str, token: int, error: str) -> str | None:
    """Gives a job back after an infrastructure failure, such as a Docker error or timeout.

    Never used when the submission itself failed: that gets a verdict instead.
    Returns the job's new status, or None if the worker no longer owned it.
    """
    row = await conn.fetchrow(_RETIRE.format(where=FENCE, error="$4"), job_id, worker_id, token, error)
    return row["status"] if row else None


async def reap(conn: asyncpg.Connection, batch: int = 100) -> list[tuple[UUID, str]]:
    """Retires running jobs whose lease ran out, meaning their worker crashed or hung.

    Safe to run in every worker at once: SKIP LOCKED gives each expired job to one reaper.
    Returns (job id, new status) for each job it retired.
    """
    expired = f"""
        id IN (SELECT id FROM jobs
               WHERE status = 'running' AND lease_until < now()
               LIMIT {int(batch)}
               FOR UPDATE SKIP LOCKED)
    """
    rows = await conn.fetch(_RETIRE.format(where=expired, error="$1"), "lease expired")
    return [(row["id"], row["status"]) for row in rows]


async def delete_expired(conn: asyncpg.Connection) -> int:
    """Deletes finished jobs past their expiry, taking their source, input and result with them."""
    status = await conn.execute(
        "DELETE FROM jobs WHERE expires_at < now() AND status IN ('completed', 'cancelled', 'dead')"
    )
    return int(status.split()[-1])


async def request_cancel(conn: asyncpg.Connection, job_id: UUID) -> str | None:
    """Cancels a queued job at once, or asks the owning worker to stop a running one.

    One UPDATE decides which, under the row's lock, so it can't race with a worker claiming
    the job at the same moment. A finished job is left alone. Returns the job's status
    afterwards, or None if it doesn't exist. Checking who may cancel belongs to the caller.
    """
    async with conn.transaction():
        status = await conn.fetchval(
            """
            UPDATE jobs SET
                status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END,
                cancel_requested = cancel_requested OR status = 'running'
            WHERE id = $1
            RETURNING status
            """,
            job_id,
        )
        if status == "running":
            await conn.execute("SELECT pg_notify('job_cancel', $1::uuid::text)", job_id)
    return status
