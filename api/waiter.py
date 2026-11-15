"""Waiting for a job to finish, for MCP tools (ARCHITECTURE.md §9, Clients without Tasks).

A worker sends NOTIFY job_done with the job's id when one finishes (core/queue.py). One
connection per API process listens, and wakes whichever tool calls wait for that job.

A notification can be lost: Postgres only delivers to connections listening at that moment, so
one sent while this connection is reconnecting is gone. So every wait also re-reads the job
every few seconds. The notification makes waiting fast; the re-read makes it correct. The same
rule as the worker's (DECISIONS.md: notifications are a hint, the table is the truth).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import asyncpg

log = logging.getLogger("judge.waiter")


class JobWaiter:
    def __init__(self, dsn: str, recheck_seconds: float = 2.0):
        self.dsn = dsn
        self.recheck = recheck_seconds
        self._waiting: dict[str, set[asyncio.Event]] = {}
        self._conn: asyncpg.Connection | None = None
        self._keeper: asyncio.Task | None = None

    async def start(self) -> None:
        await self._listen()
        self._keeper = asyncio.create_task(self._keep_listening())

    async def stop(self) -> None:
        if self._keeper:
            self._keeper.cancel()
        if self._conn and not self._conn.is_closed():
            await self._conn.close()

    async def _listen(self) -> None:
        self._conn = await asyncpg.connect(self.dsn)
        await self._conn.add_listener("job_done", self._notified)

    async def _keep_listening(self) -> None:
        while True:
            await asyncio.sleep(5)
            if self._conn is None or self._conn.is_closed():
                try:
                    await self._listen()
                    log.info("listening for job_done again")
                except (OSError, asyncpg.PostgresError) as e:
                    log.warning("can't listen for job_done yet: %s", type(e).__name__)

    def _notified(self, conn, pid, channel, payload: str) -> None:
        for event in self._waiting.get(payload, ()):
            event.set()

    async def wait(self, job_id, timeout: float, finished: Callable[[], Awaitable[bool]]) -> bool:
        """Returns True once finished() is true, or False when timeout seconds pass first."""
        key = str(job_id)
        event = asyncio.Event()
        # Registered before the first check, so a job that finishes in between still wakes us
        self._waiting.setdefault(key, set()).add(event)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                if await finished():
                    return True
                left = deadline - loop.time()
                if left <= 0:
                    return False
                try:
                    await asyncio.wait_for(event.wait(), min(left, self.recheck))
                except TimeoutError:
                    pass
                event.clear()
        finally:
            waiting = self._waiting.get(key)
            if waiting is not None:
                waiting.discard(event)
                if not waiting:
                    del self._waiting[key]
