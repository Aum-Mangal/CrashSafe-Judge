"""Jobs for MCP clients (ARCHITECTURE.md §9, MCP): run_code, verify_code, and reading or
cancelling any of a client's own jobs. submit_solution goes through core/submissions.py, so
agents follow the same visibility rule as everyone else.

Every function takes the client's id and only ever touches that client's jobs. Another client's
job answers exactly like a missing one.
"""

import json
from uuid import UUID

import asyncpg

from core import queue
from core.jobs import NewJob, create_job
from core.submissions import AGENT_EXPIRY, AGENT_PRIORITY

# Job budgets (ARCHITECTURE.md §7). The worker enforces time and memory; these bound the input.
MAX_TESTS = 20
MAX_TEXT = 1 << 20  # stdin, or all tests together: 1 MiB


class Invalid(Exception):
    """The request breaks a budget. Nothing was created."""


class NotFound(Exception):
    """No such job for this client: missing, expired, or someone else's."""


async def run(
    conn: asyncpg.Connection, client_id: int, language: str, source: str, stdin: str, key: str | None
) -> tuple[UUID, bool]:
    if len(stdin.encode()) > MAX_TEXT:
        raise Invalid("stdin is over 1 MiB")
    return await create_job(conn, NewJob(
        kind="run", owner_type="client", owner_id=client_id, language=language, source=source,
        stdin=stdin, priority=AGENT_PRIORITY, idempotency_key=key, expires_in=AGENT_EXPIRY,
    ))


async def verify(
    conn: asyncpg.Connection, client_id: int, language: str, source: str, tests: list[dict], key: str | None
) -> tuple[UUID, bool]:
    """tests is a list of {"input": ..., "expected": ...}, the shape worker/judge.py reads."""
    if not 1 <= len(tests) <= MAX_TESTS:
        raise Invalid(f"give 1 to {MAX_TESTS} tests")
    if sum(len(t["input"].encode()) + len(t["expected"].encode()) for t in tests) > MAX_TEXT:
        raise Invalid("the tests together are over 1 MiB")
    return await create_job(conn, NewJob(
        kind="verify", owner_type="client", owner_id=client_id, language=language, source=source,
        tests=[{"input": t["input"], "expected": t["expected"]} for t in tests],
        priority=AGENT_PRIORITY, idempotency_key=key, expires_in=AGENT_EXPIRY,
    ))


_OWNED = """
    SELECT j.id, j.kind, j.status, j.language, p.slug AS problem,
           r.verdict, r.failed_test, r.per_test, r.peak_memory_kb, r.compile_log,
           r.stdout_trunc, r.stderr_trunc
    FROM jobs j
    LEFT JOIN problems p ON p.id = j.problem_id
    LEFT JOIN results r ON r.job_id = j.id
    WHERE j.id = $1 AND j.owner_type = 'client' AND j.owner_id = $2
      AND (j.expires_at IS NULL OR j.expires_at > now())
"""


async def get(conn: asyncpg.Connection | asyncpg.Pool, client_id: int, job_id: UUID) -> dict:
    """The job and its result, if it has one. Raises NotFound.

    Output is shown as stored: the worker keeps none from hidden tests (worker/judge.py)."""
    row = await conn.fetchrow(_OWNED, job_id, client_id)
    if row is None:
        raise NotFound
    found = dict(row)
    found["tests"] = json.loads(found.pop("per_test")) if row["per_test"] is not None else None
    return found


async def status(conn: asyncpg.Connection | asyncpg.Pool, job_id: UUID) -> str | None:
    return await conn.fetchval("SELECT status FROM jobs WHERE id = $1", job_id)


async def cancel(conn: asyncpg.Connection, client_id: int, job_id: UUID) -> str:
    """Returns the status afterwards. A running job stops shortly after (§8). Raises NotFound."""
    await get(conn, client_id, job_id)
    return await queue.request_cancel(conn, job_id)
