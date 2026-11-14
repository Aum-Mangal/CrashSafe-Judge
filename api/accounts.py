"""Registering and logging in, shared by the REST routes and the HTML forms, so both get the same
rate limits (DECISIONS.md, 102) and the same answers. Failures raise HTTPException; the pages
turn it back into a form with a message."""

import asyncpg
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from api import ratelimit
from api.auth import hash_password, password_matches
from api.ratelimit import Limit, LoginLimits


class Register(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")
    # Shown on the public leaderboard instead of the email
    display_name: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=8, max_length=128)
    invite_code: str = Field(min_length=1, max_length=64)


class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


def client_address(request: Request) -> str:
    header = request.app.state.client_ip_header
    if header and request.headers.get(header):
        return request.headers[header].strip()
    return request.client.host if request.client else "unknown"


async def _take(request: Request, limit: Limit, value: str):
    try:
        return await ratelimit.take(request.app.state.pool, limit, value)
    except ratelimit.TooMany as e:
        raise HTTPException(429, "too many attempts, try again later", {"Retry-After": str(e.retry_after)}) from None


async def register(request: Request, body: Register) -> int:
    """Creates the account and returns its id."""
    pool: asyncpg.Pool = request.app.state.pool
    await _take(request, request.app.state.login_limits.per_address, client_address(request))
    # Registration is only open to people holding the code of a contest that hasn't ended
    if not await pool.fetchval("SELECT 1 FROM contests WHERE invite_code = $1 AND ends_at > now()", body.invite_code):
        raise HTTPException(403, "invalid invite code")
    name = " ".join(body.display_name.split())  # no leading, trailing or repeated spaces
    if not name:
        raise HTTPException(400, "choose a display name")
    password_hash = await hash_password(body.password)
    try:
        return await pool.fetchval(
            "INSERT INTO users (email, password_hash, display_name) VALUES ($1, $2, $3) RETURNING id",
            body.email.strip().lower(), password_hash, name,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "email already registered") from None


async def login(request: Request, body: Login) -> int:
    """Returns the user's id if the password is right."""
    pool: asyncpg.Pool = request.app.state.pool
    limits: LoginLimits = request.app.state.login_limits
    email = body.email.strip().lower()
    await _take(request, limits.per_address, client_address(request))
    # Taken before the check and given back on success, so guesses sent together can't all
    # slip under the limit. Unknown emails are limited too, so the limit reveals nothing.
    window = await _take(request, limits.per_email, email)

    row = await pool.fetchrow("SELECT id, password_hash FROM users WHERE email = $1", email)
    # The hash is checked even for an unknown email, and both mistakes get the same answer
    matches = await password_matches(row["password_hash"] if row else None, body.password)
    if row is None or not matches:
        raise HTTPException(401, "wrong email or password")
    await ratelimit.give_back(pool, limits.per_email, email, window)
    return row["id"]
