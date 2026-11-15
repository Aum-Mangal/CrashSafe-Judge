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
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg

from core import queue
from worker.judge import Test, judge_tests
from worker.sandbox import Limits

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


@dataclass(frozen=True)
class ProblemFolder:
    slug: str
    meta: dict
    statement: str
    samples: list[tuple[str, str]]
    hidden: list[tuple[str, str]]


def read_problem(folder: Path) -> ProblemFolder:
    """Reads and checks a problem folder. import-problem and check-problem both start here."""
    folder = Path(folder)
    try:
        meta = tomllib.loads((folder / "problem.toml").read_text(encoding="utf-8"))
        statement = (folder / "statement.md").read_text(encoding="utf-8")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise AdminError(f"can't read {folder}: {e}") from None
    missing = [key for key in ("title", "time_limit_ms", "memory_limit_mb") if key not in meta]
    if missing:
        raise AdminError(f"{folder / 'problem.toml'} is missing {', '.join(missing)}")
    samples, hidden = _read_tests(folder / "samples"), _read_tests(folder / "hidden")
    if not samples and not hidden:
        raise AdminError(f"{folder} has no tests")
    return ProblemFolder(folder.name, meta, statement, samples, hidden)


async def import_problem(conn: asyncpg.Connection, folder: Path) -> tuple[int, int, int]:
    """Creates the problem, or replaces it and all its tests. Returns (id, samples, hidden)."""
    problem = read_problem(folder)
    meta, samples, hidden = problem.meta, problem.samples, problem.hidden

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
            problem.slug, meta["title"], problem.statement, meta["time_limit_ms"], meta["memory_limit_mb"],
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


LANGUAGES = {".cpp": "cpp", ".cc": "cpp", ".py": "python"}
CHECK_INSTANCE = "check-problem"  # its own sandbox label, so no worker's reconciliation removes them
# A reference solution should run in at most half the time limit, so a correct solution that is
# a little slower than the setter's, or a busy machine, still passes
HEADROOM = 0.5


@dataclass(frozen=True)
class Check:
    problem: ProblemFolder
    result: queue.Result
    slowest_ms: int
    time_limit_ms: int

    @property
    def too_slow(self) -> bool:
        """Accepted, but with too little time to spare. Only an accepted run can be too slow:
        a TLE is already a clear answer."""
        return self.result.verdict == "AC" and self.slowest_ms > self.time_limit_ms * HEADROOM


async def check_problem(folder: Path, solution: Path) -> Check:
    """Judges one solution against every test in a problem folder, in real sandboxes, without a
    database or a worker. For writing problems: a reference solution must get AC with time to
    spare, and a slow or wrong one must not. Runs every test, even after a failure, so the
    setter sees the whole picture. The output stays on the organiser's own terminal."""
    problem = read_problem(folder)
    language = LANGUAGES.get(Path(solution).suffix)
    if language is None:
        raise AdminError(f"{solution}: the name must end in {', '.join(LANGUAGES)}")
    try:
        source = Path(solution).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise AdminError(f"can't read {solution}: {e}") from None

    meta = problem.meta
    limits = Limits.for_time(meta["time_limit_ms"], memory_mb=meta["memory_limit_mb"])
    tests = [Test(i, o) for i, o in problem.samples] + [Test(i, o, hidden=True) for i, o in problem.hidden]
    claim = queue.Claim(uuid4(), 0, 1, "check", language, source, None, None, None)
    result = await judge_tests(claim, limits, meta.get("checker", "token"), tests, CHECK_INSTANCE)
    slowest = max((t.get("cpu_ms", 0) for t in result.per_test), default=0)
    return Check(problem, result, slowest, meta["time_limit_ms"])


def describe(check: Check, solution: Path) -> str:
    """The check-problem report: one line per test, then the verdict and the time margin."""
    problem, result = check.problem, check.result
    lines = [f"{problem.slug} with {Path(solution).name}: {result.verdict}"]
    if result.compile_log:
        lines.append(result.compile_log.rstrip())
    if result.per_test:
        lines.append(f"{'test':>4}  {'kind':<6}  {'verdict':<7}  {'cpu ms':>6}  {'wall ms':>7}  {'memory':>8}")
    for n, test in enumerate(result.per_test, 1):
        kind = "sample" if n <= len(problem.samples) else "hidden"
        if "cpu_ms" not in test:
            lines.append(f"{n:>4}  {kind:<6}  {test['verdict']:<7}")
            continue
        lines.append(f"{n:>4}  {kind:<6}  {test['verdict']:<7}  {test['cpu_ms']:>6}  {test['wall_ms']:>7}  "
                     f"{test['peak_kb'] / 1024:>5.1f} MB")
    if result.per_test:
        share = check.slowest_ms / check.time_limit_ms
        lines.append(f"slowest test: {check.slowest_ms} ms of the {check.time_limit_ms} ms limit ({share:.0%})")
        if check.too_slow:
            lines.append(f"warning: a reference solution should use at most {HEADROOM:.0%} of the limit. "
                         "Raise time_limit_ms or speed the solution up.")
    return "\n".join(lines)
