"""The organiser's commands (ARCHITECTURE.md §9, Admin CLI). Run inside judge-prod; there is no
admin web page, so nothing here is reachable from the internet.

A problem folder looks like this. Its name is the problem's slug.

    problems/sum/
        problem.toml      title, time_limit_ms, memory_limit_mb, and optionally checker, public
        statement.md
        samples/1.in 1.out 2.in 2.out ...   shown to contestants
        hidden/1.in 1.out ...               never in the public repo (.gitignore)
"""

import hashlib
import secrets
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from core import queue

REJUDGE_PRIORITY = 1  # behind live contest submissions (0), so contestants still get answers first


class AdminError(Exception):
    """A mistake in the command or the folder. Nothing was changed."""


def _read_tests(folder: Path) -> list[tuple[str, str]]:
    if not folder.is_dir():
        return []
    inputs = sorted(folder.glob("*.in"), key=lambda f: (len(f.stem), f.stem))  # 2 before 10
    tests = []
    for test_in in inputs:
        test_out = test_in.with_suffix(".out")
        if not test_out.is_file():
            raise AdminError(f"{test_in} has no {test_out.name}")
        # Files written on Windows end lines with \r\n. Judged output uses \n, so an exact
        # checker would fail every answer: normalise once, here.
        tests.append(tuple(f.read_text(encoding="utf-8").replace("\r\n", "\n") for f in (test_in, test_out)))
    return tests


async def import_problem(conn: asyncpg.Connection, folder: Path) -> tuple[int, int, int]:
    """Creates the problem, or replaces it and all its tests. Returns (id, samples, hidden)."""
    folder = Path(folder)
    try:
        meta = tomllib.loads((folder / "problem.toml").read_text(encoding="utf-8"))
        statement = (folder / "statement.md").read_text(encoding="utf-8")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise AdminError(f"can't read {folder}: {e}") from None
    samples, hidden = _read_tests(folder / "samples"), _read_tests(folder / "hidden")
    if not samples and not hidden:
        raise AdminError(f"{folder} has no tests")

    async with conn.transaction():
        problem_id = await conn.fetchval(
            """
            INSERT INTO problems (slug, title, statement_md, time_limit_ms, memory_limit_mb, checker, is_public)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (slug) DO UPDATE SET
                title = EXCLUDED.title, statement_md = EXCLUDED.statement_md,
                time_limit_ms = EXCLUDED.time_limit_ms, memory_limit_mb = EXCLUDED.memory_limit_mb,
                checker = EXCLUDED.checker, is_public = EXCLUDED.is_public
            RETURNING id
            """,
            folder.name, meta["title"], statement, meta["time_limit_ms"], meta["memory_limit_mb"],
            meta.get("checker", "token"), meta.get("public", False),
        )
        await conn.execute("DELETE FROM testcases WHERE problem_id = $1", problem_id)
        rows = [(problem_id, n, t_in, t_out, n <= len(samples)) for n, (t_in, t_out) in enumerate(samples + hidden, 1)]
        await conn.executemany(
            "INSERT INTO testcases (problem_id, ordinal, input_text, expected_text, is_sample) VALUES ($1, $2, $3, $4, $5)",
            rows,
        )
    return problem_id, len(samples), len(hidden)


async def create_contest(
    conn: asyncpg.Connection,
    title: str,
    start: str,
    minutes: int,
    problem_slugs: list[str],
    tz: str = "Asia/Kolkata",
    invite_code: str | None = None,
) -> tuple[int, str]:
    """start is local time in tz, like '2026-11-28 10:00'. Problems get labels A, B, ... in the
    order given. Returns (contest id, invite code)."""
    try:
        starts_at = datetime.strptime(start, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(tz))
    except ValueError:
        raise AdminError(f"start must look like 2026-11-28 10:00, not {start!r}") from None
    if minutes <= 0 or not problem_slugs or len(problem_slugs) > 26:
        raise AdminError("a contest needs a positive length and 1 to 26 problems")
    # Unguessable: the code is all that stands between strangers and free compute (decision 25)
    code = invite_code or secrets.token_urlsafe(9)

    async with conn.transaction():
        contest_id = await conn.fetchval(
            "INSERT INTO contests (title, invite_code, starts_at, ends_at) VALUES ($1, $2, $3, $4) RETURNING id",
            title, code, starts_at, starts_at + timedelta(minutes=minutes),
        )
        for n, slug in enumerate(problem_slugs):
            problem_id = await conn.fetchval("SELECT id FROM problems WHERE slug = $1", slug)
            if problem_id is None:
                raise AdminError(f"no problem {slug!r}: import it first")  # rolls the contest back too
            await conn.execute(
                "INSERT INTO contest_problems (contest_id, problem_id, label) VALUES ($1, $2, $3)",
                contest_id, problem_id, chr(ord("A") + n),
            )
    return contest_id, code


async def create_client(conn: asyncpg.Connection, name: str, max_inflight: int = 5) -> tuple[int, str]:
    """An API key for an MCP client. Returns (client id, key). The key is shown only this once:
    the database keeps its sha256.

    A password needs a slow hash because people choose guessable ones. This key is 256 random
    bits, so nobody can guess it from its hash, and a fast hash lets each request find the
    client by an index lookup."""
    key = "jk_" + secrets.token_urlsafe(32)
    client_id = await conn.fetchval(
        "INSERT INTO api_clients (name, key_hash, max_inflight) VALUES ($1, $2, $3) RETURNING id",
        name, hashlib.sha256(key.encode()).hexdigest(), max_inflight,
    )
    return client_id, key


async def rejudge(conn: asyncpg.Connection, contest_id: int, problem_slug: str) -> list[UUID]:
    """Judges every submission to this contest problem again, after its tests were fixed.

    In one transaction, each current submission gets a new job with the same code, owner and
    submitted_at, so penalty times don't move, and the old job is marked superseded. Old jobs
    and results stay for the record. Old jobs still waiting or running are cancelled: they may
    have read the old tests. Returns the new job ids."""
    async with conn.transaction():
        if not await conn.fetchval(
            """SELECT 1 FROM contest_problems cp JOIN problems p ON p.id = cp.problem_id
               WHERE cp.contest_id = $1 AND p.slug = $2""",
            contest_id, problem_slug,
        ):
            raise AdminError(f"contest {contest_id} has no problem {problem_slug!r}")
        new = await conn.fetch(
            """
            WITH old AS (
                SELECT j.* FROM jobs j JOIN problems p ON p.id = j.problem_id
                WHERE j.contest_id = $1 AND p.slug = $2 AND j.kind = 'submission'
                  AND NOT j.superseded AND j.status <> 'cancelled'
                FOR UPDATE OF j
            ), marked AS (
                UPDATE jobs SET superseded = true FROM old WHERE jobs.id = old.id
            )
            INSERT INTO jobs (kind, owner_type, owner_id, request_hash, language, source, stdin, tests,
                              problem_id, contest_id, priority, submitted_at, rejudge_of)
            SELECT kind, owner_type, owner_id, request_hash, language, source, stdin, tests,
                   problem_id, contest_id, $3, submitted_at, id
            FROM old
            RETURNING id, rejudge_of
            """,
            contest_id, problem_slug, REJUDGE_PRIORITY,
        )
        for row in new:
            await queue.request_cancel(conn, row["rejudge_of"])  # no-op unless queued or running
        await conn.execute("SELECT pg_notify('job_new', 'rejudge')")
    return [row["id"] for row in new]
