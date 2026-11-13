"""The worker: claims jobs, judges them in sandboxes and commits results (ARCHITECTURE.md §6).

Run: python -m worker.main   (reads DATABASE_URL; one job at a time, so run one process per worker)

Three loops run side by side: claiming jobs, reaping jobs whose worker died, and removing
orphan sandboxes. While a job is judged, a heartbeat keeps its lease and watches for a cancel.
Timings come from the environment so tests can shrink them.
"""

import asyncio
import contextlib
import logging
import os
import socket
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID

import asyncpg

from core import queue
from core.queue import Claim
from db.migrate import DEFAULT_DSN
from worker.judge import judge
from worker.sandbox import DockerError, Sandbox, docker

log = logging.getLogger("worker")
DB_ERRORS = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError)


@dataclass(frozen=True)
class Settings:
    dsn: str
    worker_id: str
    lease: timedelta
    heartbeat: float
    poll: float
    reap_every: float
    reconcile_every: float
    # Labels this worker's sandboxes and limits reconciliation to them. Empty means the database name.
    instance: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        def seconds(name: str, default: float) -> float:
            return float(os.environ.get(name, default))

        return cls(
            dsn=os.environ.get("DATABASE_URL", DEFAULT_DSN),
            worker_id=os.environ.get("WORKER_ID", f"{socket.gethostname()}-{os.getpid()}"),
            lease=timedelta(seconds=seconds("LEASE_SECONDS", 30)),
            heartbeat=seconds("HEARTBEAT_SECONDS", 5),
            poll=seconds("POLL_SECONDS", 2),
            reap_every=seconds("REAP_SECONDS", 15),
            reconcile_every=seconds("RECONCILE_SECONDS", 60),
            instance=os.environ.get("JUDGE_INSTANCE", ""),
        )


async def run_job(pool: asyncpg.Pool, s: Settings, claim: Claim, cancel_signal: asyncio.Event) -> None:
    """Judges one job while heartbeating, then commits a result, a cancellation or a release.

    cancel_signal is set by NOTIFY job_cancel. It triggers an immediate heartbeat, which is what
    actually reads the cancel, so a lost notification only delays the cancel to the next heartbeat.
    """
    judging = asyncio.create_task(judge(pool, claim, s.instance))
    while True:
        signalled = asyncio.create_task(cancel_signal.wait())
        done, _ = await asyncio.wait(
            {judging, signalled}, timeout=s.heartbeat, return_when=asyncio.FIRST_COMPLETED
        )
        signalled.cancel()
        if judging in done:
            break
        cancel_signal.clear()
        try:
            async with pool.acquire() as conn:
                cancel = await queue.heartbeat(conn, claim.job_id, s.worker_id, claim.token, s.lease)
        except DB_ERRORS as e:
            log.warning("heartbeat failed for %s: %s", claim.job_id, e)
            continue
        if cancel is None or cancel:
            judging.cancel()
            # The judge's finally block removes its sandboxes while it unwinds
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await judging
            if cancel is None:
                log.warning("lost the lease on %s, so stopped judging it", claim.job_id)
                return
            async with pool.acquire() as conn:
                await queue.finish_cancelled(conn, claim.job_id, s.worker_id, claim.token)
            log.info("cancelled %s", claim.job_id)
            return

    async with pool.acquire() as conn:
        try:
            result = judging.result()
        except DockerError as e:
            status = await queue.release(conn, claim.job_id, s.worker_id, claim.token, str(e)[:500])
            log.warning("released %s after an infrastructure error, now %s: %s", claim.job_id, status, e)
            return
        except Exception as e:
            log.exception("worker bug while judging %s", claim.job_id)
            await queue.release(conn, claim.job_id, s.worker_id, claim.token, f"worker error: {type(e).__name__}")
            return
        committed = await queue.finish(conn, claim.job_id, s.worker_id, claim.token, result)
    if committed:
        log.info("finished %s: %s", claim.job_id, result.verdict)
    else:
        log.warning("finished %s too late: another worker owns it now, so the result was dropped", claim.job_id)


async def claim_loop(
    pool: asyncpg.Pool, s: Settings, wake: asyncio.Event, cancel_signals: dict[str, asyncio.Event]
) -> None:
    while True:
        try:
            async with pool.acquire() as conn:
                claimed = await queue.claim(conn, s.worker_id, s.lease)
        except DB_ERRORS as e:
            log.warning("claim failed: %s", e)
            await asyncio.sleep(s.poll)
            continue
        if claimed is None:
            # Woken early by NOTIFY job_new; the timeout is the guarantee if a notification is lost
            wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), s.poll)
            continue
        log.info("claimed %s (%s, attempt %d, token %d)", claimed.job_id, claimed.kind, claimed.attempt, claimed.token)
        cancel_signal = cancel_signals[str(claimed.job_id)] = asyncio.Event()
        try:
            await run_job(pool, s, claimed, cancel_signal)
        finally:
            del cancel_signals[str(claimed.job_id)]


async def reaper_loop(pool: asyncpg.Pool, s: Settings) -> None:
    # Wait one heartbeat first, so live jobs can renew their leases after a Postgres restart
    await asyncio.sleep(s.heartbeat)
    while True:
        try:
            async with pool.acquire() as conn:
                for job_id, status in await queue.reap(conn):
                    log.warning("reaped %s: its lease expired, now %s", job_id, status)
                await queue.delete_expired(conn)
        except DB_ERRORS as e:
            log.warning("reaper failed: %s", e)
        await asyncio.sleep(s.reap_every)


async def reconcile(pool: asyncpg.Pool, instance: str) -> None:
    """Removes this instance's sandboxes whose job no longer runs under the same token.

    An expired lease alone is not enough: the owner may only be slow, and its next heartbeat
    can still renew the lease. Deciding that a lease is dead is the reaper's job. Once the
    reaper retires the job, its status or token changes and the sandbox is removed here.

    Sandboxes of other instances are never touched: their jobs live in another database,
    so from here they would all look like orphans.
    """
    code, out, _ = await docker(
        "ps", "-a", "--filter", f"label=judge.instance={instance}",
        "--format", '{{.ID}} {{.Label "judge.job"}} {{.Label "judge.token"}}',
        timeout=20,
    )
    if code != 0:
        return
    for line in out.decode().splitlines():
        try:
            container, job, token = line.split()
            job_id, token_value = UUID(job), int(token)
        except ValueError:
            continue
        async with pool.acquire() as conn:
            live = await conn.fetchval(
                "SELECT 1 FROM jobs WHERE id = $1 AND status = 'running' AND fencing_token = $2",
                job_id,
                token_value,
            )
        if not live:
            log.warning("removing orphan sandbox %s (job %s, token %d)", container, job_id, token_value)
            await Sandbox(container).remove()


async def reconcile_loop(pool: asyncpg.Pool, s: Settings) -> None:
    while True:
        try:
            await reconcile(pool, s.instance)
        except (DockerError, *DB_ERRORS) as e:
            log.warning("reconciliation failed: %s", e)
        await asyncio.sleep(s.reconcile_every)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings.from_env()
    pool = await asyncpg.create_pool(s.dsn, min_size=2, max_size=4)
    if not s.instance:
        s = replace(s, instance=await pool.fetchval("SELECT current_database()"))
    wake = asyncio.Event()
    cancel_signals: dict[str, asyncio.Event] = {}  # job id -> signal, for the job this worker is judging

    def on_cancel(conn, pid, channel, job_id):
        if job_id in cancel_signals:
            cancel_signals[job_id].set()

    listener = await asyncpg.connect(s.dsn)
    await listener.add_listener("job_new", lambda *_: wake.set())
    await listener.add_listener("job_cancel", on_cancel)
    log.info("worker %s started for instance %s", s.worker_id, s.instance)
    try:
        await asyncio.gather(
            claim_loop(pool, s, wake, cancel_signals), reaper_loop(pool, s), reconcile_loop(pool, s)
        )
    finally:
        await listener.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
