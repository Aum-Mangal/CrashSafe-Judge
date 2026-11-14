"""REST routes (ARCHITECTURE.md §9). The API only reads and writes rows: workers do the judging."""

import json
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from api.auth import User, current_user, hash_password, make_token, password_matches
from core import problems, queue
from core.jobs import IdempotencyConflict, NewJob, Rejected, create_job

router = APIRouter()

CONTEST_PRIORITY, PRACTICE_PRIORITY = 0, 1
NOT_FOUND = HTTPException(404, "not found")


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


# --- Auth


class Register(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=8, max_length=128)
    invite_code: str = Field(min_length=1, max_length=64)


class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


def _token_reply(request: Request, user_id: int) -> dict:
    return {"access_token": make_token(user_id, request.app.state.jwt_secret), "token_type": "bearer"}


@router.post("/auth/register", status_code=201)
async def register(body: Register, request: Request):
    pool = _pool(request)
    # Registration is only open to people holding the code of a contest that hasn't ended
    if not await pool.fetchval("SELECT 1 FROM contests WHERE invite_code = $1 AND ends_at > now()", body.invite_code):
        raise HTTPException(403, "invalid invite code")
    password_hash = await hash_password(body.password)
    try:
        user_id = await pool.fetchval(
            "INSERT INTO users (email, password_hash) VALUES ($1, $2) RETURNING id",
            body.email.strip().lower(), password_hash,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "email already registered") from None
    return _token_reply(request, user_id)


@router.post("/auth/login")
async def login(body: Login, request: Request):
    row = await _pool(request).fetchrow(
        "SELECT id, password_hash FROM users WHERE email = $1", body.email.strip().lower()
    )
    # The hash is checked even for an unknown email, and both mistakes get the same answer
    matches = await password_matches(row["password_hash"] if row else None, body.password)
    if row is None or not matches:
        raise HTTPException(401, "wrong email or password")
    return _token_reply(request, row["id"])


# --- Problems


@router.get("/problems")
async def list_problems(request: Request):
    async with _pool(request).acquire() as conn:
        return [dict(r) for r in await problems.list_visible(conn)]


@router.get("/problems/{slug}")
async def get_problem(slug: str, request: Request):
    async with _pool(request).acquire() as conn:
        problem = await problems.get_visible(conn, slug)
        if problem is None:
            raise NOT_FOUND  # also for a contest problem before the start: its existence is a secret too
        samples = await problems.samples(conn, problem["id"])
    return {
        "slug": problem["slug"],
        "title": problem["title"],
        "statement_md": problem["statement_md"],
        "time_limit_ms": problem["time_limit_ms"],
        "memory_limit_mb": problem["memory_limit_mb"],
        "checker": problem["checker"],
        "samples": [{"input": s["input_text"], "expected": s["expected_text"]} for s in samples],
    }


# --- Submissions


class Submit(BaseModel):
    problem: str = Field(max_length=100, description="the problem's slug")
    language: Literal["cpp", "python"]
    source: str = Field(min_length=1, max_length=65536)
    contest_id: int | None = Field(default=None, description="set when submitting during a contest")


@router.post("/submissions", status_code=201)
async def submit(
    body: Submit,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    idempotency_key: str | None = Header(default=None, max_length=100),
):
    async with _pool(request).acquire() as conn, conn.transaction():
        # now() is fixed for the whole transaction, so the window checked below is the same
        # instant that create_job stores as submitted_at
        problem = await problems.get_visible(conn, body.problem)
        if problem is None:
            raise NOT_FOUND
        # A retry of a request that was already accepted gets its job, even if the contest
        # has ended since. create_job compares the request itself.
        retry = idempotency_key is not None and await conn.fetchval(
            "SELECT 1 FROM jobs WHERE owner_type = 'user' AND owner_id = $1 AND idempotency_key = $2",
            user.id, idempotency_key,
        )
        if body.contest_id is not None:
            running = await conn.fetchval(
                """
                SELECT now() >= c.starts_at AND now() < c.ends_at
                FROM contests c JOIN contest_problems cp ON cp.contest_id = c.id
                WHERE c.id = $1 AND cp.problem_id = $2
                """,
                body.contest_id, problem["id"],
            )
            if running is None:
                raise NOT_FOUND  # no such contest, or the problem isn't in it
            if not running and not retry:
                raise HTTPException(403, "the contest is not running")
            priority = CONTEST_PRIORITY
        else:
            if not problem["is_public"] and not retry:
                raise HTTPException(403, "this problem only takes submissions through its contest")
            priority = PRACTICE_PRIORITY

        try:
            job_id, created = await create_job(
                conn,
                NewJob(
                    kind="submission", owner_type="user", owner_id=user.id, language=body.language,
                    source=body.source, priority=priority, problem_id=problem["id"],
                    contest_id=body.contest_id, idempotency_key=idempotency_key,
                ),
            )
        except IdempotencyConflict:
            raise HTTPException(409, "this Idempotency-Key was used for a different submission") from None
        except Rejected as e:
            raise HTTPException(429, e.reason, {"Retry-After": str(e.retry_after)}) from None

    response.headers["Location"] = f"/submissions/{job_id}"
    if not created:
        response.status_code = 200
    return {"id": str(job_id), "created": created}


# Results show the verdict, failing test, time and memory. The worker stores program output only
# from a sample test, never a hidden one (worker/judge.py), so showing it leaks nothing.
SUBMISSION = """
    SELECT j.id, j.status, j.language, j.submitted_at, j.contest_id, p.slug AS problem,
           r.verdict, r.failed_test, r.per_test, r.peak_memory_kb, r.compile_log,
           r.stdout_trunc, r.stderr_trunc
    FROM jobs j
    JOIN problems p ON p.id = j.problem_id
    LEFT JOIN results r ON r.job_id = j.id
    WHERE j.id = $1 AND j.kind = 'submission' AND j.owner_type = 'user' AND j.owner_id = $2
"""


@router.get("/submissions/{job_id}")
async def get_submission(job_id: UUID, request: Request, user: User = Depends(current_user)):
    row = await _pool(request).fetchrow(SUBMISSION, job_id, user.id)
    if row is None:
        raise NOT_FOUND  # someone else's submission looks exactly like a missing one
    return {
        "id": str(row["id"]),
        "problem": row["problem"],
        "contest_id": row["contest_id"],
        "language": row["language"],
        "status": row["status"],
        "submitted_at": row["submitted_at"].isoformat(),
        "verdict": row["verdict"],
        "failed_test": row["failed_test"],
        "tests": json.loads(row["per_test"]) if row["per_test"] is not None else None,
        "peak_memory_kb": row["peak_memory_kb"],
        "compile_log": row["compile_log"],
        "stdout": row["stdout_trunc"],
        "stderr": row["stderr_trunc"],
    }


@router.delete("/submissions/{job_id}", status_code=202)
async def cancel_submission(job_id: UUID, request: Request, user: User = Depends(current_user)):
    async with _pool(request).acquire() as conn:
        owned = await conn.fetchval(
            "SELECT 1 FROM jobs WHERE id = $1 AND kind = 'submission' AND owner_type = 'user' AND owner_id = $2",
            job_id, user.id,
        )
        if not owned:
            raise NOT_FOUND
        # Eventually consistent: a running job is stopped by its worker shortly after (§8)
        return {"status": await queue.request_cancel(conn, job_id)}


# --- Health


@router.get("/health/live")
async def live():
    return {"ok": True}


@router.get("/health/ready")
async def ready(request: Request):
    try:
        await _pool(request).fetchval("SELECT 1", timeout=2)
    except (OSError, asyncpg.PostgresError, TimeoutError):
        raise HTTPException(503, "database unavailable") from None
    return {"ok": True}
