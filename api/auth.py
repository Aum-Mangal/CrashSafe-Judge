"""Passwords and login tokens (ARCHITECTURE.md §9 and §10).

Passwords are stored as argon2id hashes. Each hash carries its own random salt and settings, so
two users with the same password get different hashes, and the settings can change later.

A login returns a JWT: a small JSON object signed with the server's secret, so the server can
check it without storing sessions. It holds only the user's id. Every request reloads the user
from the database, so deleting an account or changing a role takes effect at once.
"""

import asyncio
import time
from dataclasses import dataclass

import argon2
import asyncpg
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

TOKEN_SECONDS = 12 * 3600  # longer than a contest, shorter than a day
ALGORITHM = "HS256"

# OWASP's minimum for argon2id: 19 MiB and 2 passes. The library default of 64 MiB per login
# would let a few logins at once use most of the API's 0.3 GB (ARCHITECTURE.md §3).
_hasher = argon2.PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)
# Checked when the email is unknown, so a wrong email takes as long as a wrong password
_DUMMY_HASH = _hasher.hash("no user has this password")


async def hash_password(password: str) -> str:
    # A hash takes about 21 ms of CPU (DECISIONS.md, 96). In a thread, other requests keep being served.
    return await asyncio.to_thread(_hasher.hash, password)


async def password_matches(stored_hash: str | None, password: str) -> bool:
    try:
        return await asyncio.to_thread(_hasher.verify, stored_hash or _DUMMY_HASH, password)
    except argon2.exceptions.VerificationError:
        return False


def make_token(user_id: int, secret: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + TOKEN_SECONDS}, secret, ALGORITHM)


def user_id_from_token(token: str, secret: str) -> int | None:
    try:
        # algorithms is pinned: a token claiming "alg": "none" or another algorithm is refused
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub"]})
        return int(claims["sub"])
    except (jwt.InvalidTokenError, ValueError):
        return None


@dataclass(frozen=True)
class User:
    id: int
    role: str


_bearer = HTTPBearer(auto_error=False)
UNAUTHORIZED = HTTPException(401, "not signed in, or the token has expired", {"WWW-Authenticate": "Bearer"})


async def current_user(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> User:
    if credentials is None:
        raise UNAUTHORIZED
    user_id = user_id_from_token(credentials.credentials, request.app.state.jwt_secret)
    if user_id is None:
        raise UNAUTHORIZED
    pool: asyncpg.Pool = request.app.state.pool
    row = await pool.fetchrow("SELECT id, role FROM users WHERE id = $1", user_id)
    if row is None:
        raise UNAUTHORIZED  # the account was deleted after the token was issued
    return User(row["id"], row["role"])
