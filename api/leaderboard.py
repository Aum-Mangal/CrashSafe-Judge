"""The leaderboard, cached for a few seconds (ARCHITECTURE.md §4).

The query reads every submission in the contest, and everyone refreshes the board at once, so its
answer is reused for `seconds`. When it runs out, one request recomputes it while the others wait
for that answer, instead of all running the query together (a cache stampede).
"""

import asyncio
import time

import asyncpg

from core.leaderboard import Board, board


class BoardCache:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self._boards: dict[int, tuple[float, Board]] = {}
        self._lock = asyncio.Lock()

    def _fresh(self, contest_id: int) -> Board | None:
        found = self._boards.get(contest_id)
        return found[1] if found and found[0] > time.monotonic() else None

    async def get(self, pool: asyncpg.Pool, contest_id: int) -> Board | None:
        if self.seconds <= 0:
            async with pool.acquire() as conn:
                return await board(conn, contest_id)
        if (cached := self._fresh(contest_id)) is not None:
            return cached
        async with self._lock:
            # Someone else may have refreshed it while this request waited for the lock
            if (cached := self._fresh(contest_id)) is not None:
                return cached
            async with pool.acquire() as conn:
                found = await board(conn, contest_id)
            # A missing contest isn't cached, so made-up contest ids can't fill memory
            if found is not None:
                self._boards[contest_id] = (time.monotonic() + self.seconds, found)
            return found
