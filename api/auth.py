"""Passwords and login tokens (ARCHITECTURE.md §9 and §10).

Passwords are stored as argon2id hashes. Each hash carries its own random salt and settings, so
two users with the same password get different hashes, and the settings can change later.

A login returns a JWT: a small JSON object signed with the server's secret, so the server can
check it without storing sessions. It holds only the user's id. Every request reloads the user
from the database, so deleting an account or changing a role takes effect at once.
"""

import asyncio
import hashlib
import hmac
import time
from dataclasses import dataclass

import argon2
import asyncpg
import jwt
from fastapi import Depends, HTTPException, Request, Response
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


async def _load_user(request: Request, token: str | None) -> User | None:
    user_id = user_id_from_token(token, request.app.state.jwt_secret) if token else None
    if user_id is None:
        return None
    pool: asyncpg.Pool = request.app.state.pool
    row = await pool.fetchrow("SELECT id, role FROM users WHERE id = $1", user_id)
    # None if the account was deleted after the token was issued
    return User(row["id"], row["role"]) if row else None


async def current_user(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> User:
    """For REST: the token comes in the Authorization header."""
    user = await _load_user(request, credentials.credentials if credentials else None)
    if user is None:
        raise UNAUTHORIZED
    return user


# --- Browser sessions, for the contest pages
#
# A browser can't add an Authorization header to a form post, so the pages keep the same token in
# a cookie. HttpOnly: page scripts can't read it. SameSite=Lax: the browser leaves it off posts
# that come from other sites. Secure: only sent over HTTPS (off only for local testing).

SESSION_COOKIE = "judge_session"


def set_session(response: Response, request: Request, user_id: int) -> None:
    response.set_cookie(
        SESSION_COOKIE, make_token(user_id, request.app.state.jwt_secret), max_age=TOKEN_SECONDS,
        httponly=True, samesite="lax", secure=request.app.state.cookie_secure, path="/",
    )


def clear_session(response: Response, request: Request) -> None:
    response.delete_cookie(
        SESSION_COOKIE, httponly=True, samesite="lax", secure=request.app.state.cookie_secure, path="/"
    )


async def page_user(request: Request) -> User | None:
    """The signed-in user for a page, or None. Pages work signed out too."""
    return await _load_user(request, request.cookies.get(SESSION_COOKIE))


def csrf_token(request: Request) -> str:
    """Goes in a hidden field of every signed-in form.

    CSRF is another site making your browser post a form here while you're signed in. That site
    can make the browser send the cookie, but it can't read our pages, so it can't learn this
    value: an HMAC of the session token under the server's secret. Only our own forms carry it.
    """
    session = request.cookies.get(SESSION_COOKIE, "")
    secret = request.app.state.jwt_secret.encode()
    return hmac.new(secret, b"csrf:" + session.encode(), hashlib.sha256).hexdigest()


def csrf_ok(request: Request, submitted: str | None) -> bool:
    # compare_digest takes the same time wherever the strings differ, so timing reveals nothing
    return bool(submitted) and hmac.compare_digest(submitted, csrf_token(request))
