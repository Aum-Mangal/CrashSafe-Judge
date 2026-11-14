"""Creating jobs: idempotency, rate limits and capacity caps (ARCHITECTURE.md §6, Create).

Everything happens in one transaction that first locks the owner's row. Two requests from the
same owner therefore run one after the other, so none of the checks below can race each other.
A refused request rolls back, so it isn't counted against the rate limit.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class Caps:
    per_minute: int = 30  # requests per owner per minute
    user_inflight: int = 3  # a user's queued plus running jobs; API clients have their own column
    global_queued: int = 500


class Rejected(Exception):
    """Refused for now. The caller answers 429 with a Retry-After header."""

    def __init__(self, reason: str, retry_after: int):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class IdempotencyConflict(Exception):
    """The key was already used for a different request. The caller answers 409."""


@dataclass(frozen=True)
class NewJob:
    kind: str
    owner_type: str
    owner_id: int
    language: str
    source: str
    priority: int
    stdin: str | None = None
    tests: list | None = None
    problem_id: int | None = None
    contest_id: int | None = None
    idempotency_key: str | None = None
    expires_in: timedelta | None = None

    def request_hash(self) -> bytes:
        """A fingerprint of what was asked for, so a key reused for a different request is caught."""
        asked = {
            "kind": self.kind,
            "language": self.language,
            "source": self.source,
            "stdin": self.stdin,
            "tests": self.tests,
            "problem_id": self.problem_id,
            "contest_id": self.contest_id,
        }
        return hashlib.sha256(json.dumps(asked, sort_keys=True).encode()).digest()


async def create_job(conn: asyncpg.Connection, job: NewJob, caps: Caps = Caps()) -> tuple[UUID, bool]:
    """Returns (job id, created). created is False when a retry got the job it created before."""
    request_hash = job.request_hash()
    async with conn.transaction():
        max_inflight = await _lock_owner(conn, job, caps)

        if job.idempotency_key is not None:
            existing = await conn.fetchrow(
                "SELECT id, request_hash FROM jobs WHERE owner_type = $1 AND owner_id = $2 AND idempotency_key = $3",
                job.owner_type, job.owner_id, job.idempotency_key,
            )
            if existing is not None:
                if bytes(existing["request_hash"]) != request_hash:
                    raise IdempotencyConflict(f"key {job.idempotency_key!r} was used for a different request")
                return existing["id"], False

        rate = await conn.fetchrow(
            """
            INSERT INTO rate_limits (owner_type, owner_id, window_start, count)
            VALUES ($1, $2, date_trunc('minute', now()), 1)
            ON CONFLICT (owner_type, owner_id, window_start) DO UPDATE SET count = rate_limits.count + 1
            RETURNING count, greatest(1, 60 - extract(second FROM now())::int) AS retry_after
            """,
            job.owner_type, job.owner_id,
        )
        if rate["count"] > caps.per_minute:
            raise Rejected("rate limit", rate["retry_after"])

        # Rejudge jobs don't count: the organiser made them, and they mustn't block anyone (DECISIONS.md, 113)
        inflight = await conn.fetchval(
            """SELECT count(*) FROM jobs WHERE owner_type = $1 AND owner_id = $2
               AND status IN ('queued', 'running') AND rejudge_of IS NULL""",
            job.owner_type, job.owner_id,
        )
        if inflight >= max_inflight:
            raise Rejected("too many jobs in flight", 5)

        queued = await conn.fetchval("SELECT count(*) FROM jobs WHERE status = 'queued' AND rejudge_of IS NULL")
        if queued >= caps.global_queued:
            raise Rejected("queue full", 10)

        job_id = await conn.fetchval(
            """
            INSERT INTO jobs (kind, owner_type, owner_id, idempotency_key, request_hash, language, source,
                              stdin, tests, problem_id, contest_id, priority, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, now() + $13::interval)
            RETURNING id
            """,
            job.kind, job.owner_type, job.owner_id, job.idempotency_key, request_hash, job.language,
            job.source, job.stdin, json.dumps(job.tests) if job.tests is not None else None,
            job.problem_id, job.contest_id, job.priority, job.expires_in,
        )
        # Delivered at commit, so a worker never wakes for a job it can't see yet
        await conn.execute("SELECT pg_notify('job_new', $1::uuid::text)", job_id)
    return job_id, True


async def _lock_owner(conn: asyncpg.Connection, job: NewJob, caps: Caps) -> int:
    """Locks the owner's row until the transaction ends and returns their in-flight cap."""
    if job.owner_type == "client":
        cap = await conn.fetchval("SELECT max_inflight FROM api_clients WHERE id = $1 FOR UPDATE", job.owner_id)
    else:
        cap = await conn.fetchval("SELECT $2::int FROM users WHERE id = $1 FOR UPDATE", job.owner_id, caps.user_inflight)
    if cap is None:
        raise LookupError(f"no {job.owner_type} with id {job.owner_id}")
    return cap
