"""REST routes (ARCHITECTURE.md §9). The API only reads and writes rows: workers do the judging."""

from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from api import accounts
from api.auth import User, current_user, make_token
from core import problems, submissions
from core.jobs import IdempotencyConflict, Rejected

router = APIRouter()

NOT_FOUND = HTTPException(404, "not found")


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


# --- Auth


def _token_reply(request: Request, user_id: int) -> dict:
    return {"access_token": make_token(user_id, request.app.state.jwt_secret), "token_type": "bearer"}


@router.post("/auth/register", status_code=201)
async def register(body: accounts.Register, request: Request):
    return _token_reply(request, await accounts.register(request, body))


@router.post("/auth/login")
async def login(body: accounts.Login, request: Request):
    return _token_reply(request, await accounts.login(request, body))


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
    try:
        async with _pool(request).acquire() as conn:
            job_id, created = await submissions.submit(
                conn, user.id, body.problem, body.language, body.source, body.contest_id, idempotency_key
            )
    except submissions.NotFound:
        raise NOT_FOUND from None
    except submissions.NotAllowed as e:
        raise HTTPException(403, str(e)) from None
    except IdempotencyConflict:
        raise HTTPException(409, "this Idempotency-Key was used for a different submission") from None
    except Rejected as e:
        raise HTTPException(429, e.reason, {"Retry-After": str(e.retry_after)}) from None

    response.headers["Location"] = f"/submissions/{job_id}"
    if not created:
        response.status_code = 200
    return {"id": str(job_id), "created": created}


@router.get("/submissions/{job_id}")
async def get_submission(job_id: UUID, request: Request, user: User = Depends(current_user)):
    async with _pool(request).acquire() as conn:
        found = await submissions.get_owned(conn, user.id, job_id)
    if found is None:
        raise NOT_FOUND  # someone else's submission looks exactly like a missing one
    return {
        "id": str(found["id"]),
        "problem": found["problem"],
        "contest_id": found["contest_id"],
        "language": found["language"],
        "status": found["status"],
        "submitted_at": found["submitted_at"].isoformat(),
        "verdict": found["verdict"],
        "failed_test": found["failed_test"],
        "tests": found["tests"],
        "peak_memory_kb": found["peak_memory_kb"],
        "compile_log": found["compile_log"],
        "stdout": found["stdout_trunc"],
        "stderr": found["stderr_trunc"],
    }


@router.delete("/submissions/{job_id}", status_code=202)
async def cancel_submission(job_id: UUID, request: Request, user: User = Depends(current_user)):
    try:
        async with _pool(request).acquire() as conn:
            return {"status": await submissions.cancel_owned(conn, user.id, job_id)}
    except submissions.NotFound:
        raise NOT_FOUND from None


# --- Leaderboard


@router.get("/contests/{contest_id}/leaderboard")
async def leaderboard(contest_id: int, request: Request):
    found = await request.app.state.leaderboards.get(_pool(request), contest_id)
    if found is None:
        raise NOT_FOUND
    return {"started": found.started, "problems": found.labels, "rows": found.rows}


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
