"""Submitting to a problem and reading submissions back, for REST, the contest pages and MCP alike
(ARCHITECTURE.md §9). The rules live here once, so the three can't disagree.
"""

import json
from uuid import UUID

import asyncpg

from core import problems, queue
from core.jobs import NewJob, create_job

CONTEST_PRIORITY, PRACTICE_PRIORITY = 0, 1


class NotFound(Exception):
    """Missing, or not visible to this caller. Both answer the same."""


class NotAllowed(Exception):
    """Visible, but not open for this submission right now."""


async def submit(
    conn: asyncpg.Connection,
    user_id: int,
    problem_slug: str,
    language: str,
    source: str,
    contest_id: int | None = None,
    idempotency_key: str | None = None,
) -> tuple[UUID, bool]:
    """Returns (job id, created). Raises NotFound, NotAllowed, and core.jobs' Rejected and
    IdempotencyConflict."""
    async with conn.transaction():
        # now() is fixed for the whole transaction, so the window checked below is the same
        # instant that create_job stores as submitted_at (DECISIONS.md, 100)
        problem = await problems.get_visible(conn, problem_slug)
        if problem is None:
            raise NotFound
        # A retry of a request that was already accepted gets its job, even if the contest
        # has ended since. create_job compares the request itself.
        retry = idempotency_key is not None and await conn.fetchval(
            "SELECT 1 FROM jobs WHERE owner_type = 'user' AND owner_id = $1 AND idempotency_key = $2",
            user_id, idempotency_key,
        )
        if contest_id is not None:
            running = await conn.fetchval(
                """
                SELECT now() >= c.starts_at AND now() < c.ends_at
                FROM contests c JOIN contest_problems cp ON cp.contest_id = c.id
                WHERE c.id = $1 AND cp.problem_id = $2
                """,
                contest_id, problem["id"],
            )
            if running is None:
                raise NotFound  # no such contest, or the problem isn't in it
            if not running and not retry:
                raise NotAllowed("the contest is not running")
            priority = CONTEST_PRIORITY
        else:
            if not problem["is_public"] and not retry:
                raise NotAllowed("this problem only takes submissions through its contest")
            priority = PRACTICE_PRIORITY

        return await create_job(
            conn,
            NewJob(
                kind="submission", owner_type="user", owner_id=user_id, language=language, source=source,
                priority=priority, problem_id=problem["id"], contest_id=contest_id,
                idempotency_key=idempotency_key,
            ),
        )


# Results show the verdict, failing test, time and memory. The worker stores program output only
# from a sample test, never a hidden one (worker/judge.py), so showing it leaks nothing.
_OWNED = """
    SELECT j.id, j.status, j.language, j.source, j.submitted_at, j.contest_id,
           p.slug AS problem, p.title AS problem_title, cp.label,
           r.verdict, r.failed_test, r.per_test, r.peak_memory_kb, r.compile_log,
           r.stdout_trunc, r.stderr_trunc
    FROM jobs j
    JOIN problems p ON p.id = j.problem_id
    LEFT JOIN contest_problems cp ON cp.contest_id = j.contest_id AND cp.problem_id = j.problem_id
    LEFT JOIN results r ON r.job_id = j.id
    WHERE j.kind = 'submission' AND j.owner_type = 'user' AND j.owner_id = $1
"""


def _shape(row: asyncpg.Record) -> dict:
    found = dict(row)
    found["tests"] = json.loads(found.pop("per_test")) if row["per_test"] is not None else None
    return found


async def get_owned(conn: asyncpg.Connection, user_id: int, job_id: UUID) -> dict | None:
    """The submission, or None if it doesn't exist or belongs to someone else."""
    row = await conn.fetchrow(_OWNED + " AND j.id = $2", user_id, job_id)
    return _shape(row) if row else None


async def list_owned(conn: asyncpg.Connection, user_id: int, contest_id: int, limit: int = 100) -> list[dict]:
    rows = await conn.fetch(
        _OWNED + " AND j.contest_id = $2 ORDER BY j.submitted_at DESC LIMIT $3", user_id, contest_id, limit
    )
    return [_shape(r) for r in rows]


async def cancel_owned(conn: asyncpg.Connection, user_id: int, job_id: UUID) -> str:
    """Returns the status afterwards. Raises NotFound for someone else's submission."""
    owned = await conn.fetchval(
        "SELECT 1 FROM jobs WHERE id = $1 AND kind = 'submission' AND owner_type = 'user' AND owner_id = $2",
        job_id, user_id,
    )
    if not owned:
        raise NotFound
    # Eventually consistent: a running job is stopped by its worker shortly after (§8)
    return await queue.request_cancel(conn, job_id)
