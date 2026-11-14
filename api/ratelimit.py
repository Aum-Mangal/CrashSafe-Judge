"""Limits on login and registration attempts (DECISIONS.md, 102).

Fixed windows: each key gets a counter per window (say 14:00 to 14:15), and the counter starts
again at zero in the next one. The time comes from Postgres, like every other clock in the system.

An attempt takes its slot before the password is checked, in one atomic upsert. Checking first and
counting after would let many guesses sent at the same moment all see a counter below the limit.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

import asyncpg


@dataclass(frozen=True)
class Limit:
    name: str  # part of the key, so the two limits never share a counter
    max: int
    window: timedelta


@dataclass(frozen=True)
class LoginLimits:
    # Every login and registration attempt from one address. Checked before any hashing,
    # so a flood is refused without costing CPU.
    per_address: Limit = Limit("address", 20, timedelta(minutes=1))
    # Failed logins for one email, from any address
    per_email: Limit = Limit("email", 10, timedelta(minutes=15))


class TooMany(Exception):
    """The caller answers 429 with Retry-After."""

    def __init__(self, retry_after: int):
        super().__init__(f"too many attempts, retry after {retry_after} s")
        self.retry_after = retry_after


def _key(limit: Limit, value: str) -> bytes:
    return hashlib.sha256(f"{limit.name}:{value}".encode()).digest()


_TAKE = """
    INSERT INTO login_limits (key_hash, window_start, count)
    VALUES ($1, date_bin($2::interval, now(), 'epoch'::timestamptz), 1)
    ON CONFLICT (key_hash, window_start) DO UPDATE SET count = login_limits.count + 1
    RETURNING count, window_start,
              greatest(1, ceil(extract(epoch FROM window_start + $2::interval - now())))::int AS retry_after
"""


async def take(pool: asyncpg.Pool, limit: Limit, value: str) -> datetime:
    """Uses one attempt and returns its window, or raises TooMany if none are left."""
    row = await pool.fetchrow(_TAKE, _key(limit, value), limit.window)
    if row["count"] > limit.max:
        raise TooMany(row["retry_after"])
    return row["window_start"]


async def give_back(pool: asyncpg.Pool, limit: Limit, value: str, window_start: datetime) -> None:
    """Returns an attempt that turned out not to count, such as a successful login."""
    await pool.execute(
        "UPDATE login_limits SET count = count - 1 WHERE key_hash = $1 AND window_start = $2",
        _key(limit, value), window_start,
    )
