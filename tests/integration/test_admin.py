"""The admin CLI (admin/): importing problems, creating contests and API keys, and rejudging."""

import asyncio
import hashlib
import os
import sys

import pytest

from admin import commands
from core.jobs import NewJob, create_job
from core.leaderboard import board
from helpers import ROOT, insert_problem, make_contest


def problem_folder(root, slug="sum", hidden=(("5 5\n", "10\n"),), crlf=False):
    folder = root / slug
    (folder / "samples").mkdir(parents=True)
    (folder / "problem.toml").write_text('title = "Sum"\ntime_limit_ms = 1000\nmemory_limit_mb = 256\n')
    (folder / "statement.md").write_text("Add two numbers.")
    newline = "\r\n" if crlf else "\n"
    (folder / "samples" / "1.in").write_bytes(f"1 2{newline}".encode())
    (folder / "samples" / "1.out").write_bytes(f"3{newline}".encode())
    if hidden:
        (folder / "hidden").mkdir()
        for n, (test_in, test_out) in enumerate(hidden, 1):
            (folder / "hidden" / f"{n}.in").write_text(test_in)
            (folder / "hidden" / f"{n}.out").write_text(test_out)
    return folder


async def stored_tests(db, slug):
    return [tuple(r) for r in await db.fetch(
        """SELECT t.ordinal, t.input_text, t.expected_text, t.is_sample FROM testcases t
           JOIN problems p ON p.id = t.problem_id WHERE p.slug = $1 ORDER BY t.ordinal""",
        slug,
    )]


# --- import-problem


async def test_import_puts_samples_first_then_hidden_tests(db, tmp_path):
    hidden = [(f"{n} 0\n", f"{n}\n") for n in range(1, 12)]  # 1..11, so 10 must sort after 2
    problem_id, samples, hidden_count = await commands.import_problem(db, problem_folder(tmp_path, hidden=hidden))
    assert (samples, hidden_count) == (1, 11)
    found = await stored_tests(db, "sum")
    assert found[0] == (1, "1 2\n", "3\n", True)
    assert [t[1] for t in found[1:]] == [f"{n} 0\n" for n in range(1, 12)]
    assert not any(t[3] for t in found[1:])


async def test_windows_line_endings_are_normalised(db, tmp_path):
    await commands.import_problem(db, problem_folder(tmp_path, crlf=True))
    assert (await stored_tests(db, "sum"))[0][1:3] == ("1 2\n", "3\n")


async def test_reimporting_replaces_the_tests(db, tmp_path):
    folder = problem_folder(tmp_path)
    first_id, _, _ = await commands.import_problem(db, folder)
    (folder / "hidden" / "1.out").write_text("11\n")  # the setter fixes a wrong answer
    second_id, _, _ = await commands.import_problem(db, folder)
    assert first_id == second_id
    assert (await stored_tests(db, "sum"))[1][2] == "11\n"


async def test_a_test_without_an_answer_changes_nothing(db, tmp_path):
    folder = problem_folder(tmp_path)
    (folder / "hidden" / "2.in").write_text("1 1\n")
    with pytest.raises(commands.AdminError, match="2.in has no 2.out"):
        await commands.import_problem(db, folder)
    assert await db.fetchval("SELECT count(*) FROM problems") == 0


# --- create-contest and create-client


async def test_create_contest_labels_problems_in_order(db, tmp_path):
    for slug in ("first", "second"):
        await commands.import_problem(db, problem_folder(tmp_path, slug))
    contest, code = await commands.create_contest(db, "Mock", "2026-11-28 10:00", 120, ["second", "first"])
    row = await db.fetchrow(
        "SELECT starts_at AT TIME ZONE 'UTC' AS starts, ends_at - starts_at AS length FROM contests WHERE id = $1", contest
    )
    assert str(row["starts"]) == "2026-11-28 04:30:00"  # 10:00 in India is 04:30 UTC
    assert row["length"].total_seconds() == 120 * 60
    labels = await db.fetch(
        "SELECT cp.label, p.slug FROM contest_problems cp JOIN problems p ON p.id = cp.problem_id WHERE contest_id = $1 ORDER BY label",
        contest,
    )
    assert [tuple(r) for r in labels] == [("A", "second"), ("B", "first")]
    assert len(code) >= 12


async def test_a_contest_with_an_unknown_problem_is_not_created(db, tmp_path):
    await commands.import_problem(db, problem_folder(tmp_path))
    with pytest.raises(commands.AdminError, match="no problem 'typo'"):
        await commands.create_contest(db, "Mock", "2026-11-28 10:00", 120, ["sum", "typo"])
    assert await db.fetchval("SELECT count(*) FROM contests") == 0


async def test_an_api_key_is_stored_only_as_its_hash(db):
    client, key = await commands.create_client(db, "agent", 2)
    stored = await db.fetchrow("SELECT key_hash, max_inflight FROM api_clients WHERE id = $1", client)
    assert key.startswith("jk_") and key not in stored["key_hash"]
    assert stored["key_hash"] == hashlib.sha256(key.encode()).hexdigest() and stored["max_inflight"] == 2


# --- rejudge


async def contestant(db, name):
    return await db.fetchval(
        "INSERT INTO users (email, password_hash, display_name) VALUES ($1, 'x', $2) RETURNING id",
        f"{name}@example.com", name,
    )


async def submission(db, contest, problem, user, status, verdict=None, minute=5):
    job = await db.fetchval(
        """
        INSERT INTO jobs (kind, owner_type, owner_id, request_hash, language, source, problem_id,
                          contest_id, priority, status, submitted_at, worker_id, lease_until)
        SELECT 'submission', 'user', $1, '\\x00', 'python', 'print(1)', $2, $3, 0, $4,
               starts_at + make_interval(mins => $5),
               CASE WHEN $4 = 'running' THEN 'w' END, CASE WHEN $4 = 'running' THEN now() END
        FROM contests WHERE id = $3 RETURNING id
        """,
        user, problem, contest, status, minute,
    )
    if verdict:
        await db.execute("INSERT INTO results (job_id, verdict) VALUES ($1, $2)", job, verdict)
    return job


async def test_rejudge_replaces_each_submission_and_keeps_its_time(db):
    a, b = await insert_problem(db, [("1\n", "1\n", True)]), await insert_problem(db, [("1\n", "1\n", True)])
    contest = await make_contest(db, -3600, 3600, [a, b])
    slug_a = await db.fetchval("SELECT slug FROM problems WHERE id = $1", a)
    ann, bob, cy = await contestant(db, "ann"), await contestant(db, "bob"), await contestant(db, "cy")
    judged = await submission(db, contest, a, ann, "completed", "WA", minute=7)
    running = await submission(db, contest, a, bob, "running")
    cancelled = await submission(db, contest, a, cy, "cancelled")
    other_problem = await submission(db, contest, b, ann, "completed", "AC")

    new = await commands.rejudge(db, contest, slug_a)

    assert len(new) == 2
    rows = {r["rejudge_of"]: r for r in await db.fetch(
        """SELECT n.rejudge_of, n.status, n.priority, n.submitted_at = o.submitted_at AS same_time
           FROM jobs n JOIN jobs o ON o.id = n.rejudge_of WHERE n.id = ANY($1)""",
        new,
    )}
    assert set(rows) == {judged, running}
    assert all(r["status"] == "queued" and r["priority"] == 1 and r["same_time"] for r in rows.values())
    old = {r["id"]: r for r in await db.fetch("SELECT id, superseded, cancel_requested FROM jobs")}
    assert old[judged]["superseded"] and old[running]["superseded"]
    assert old[running]["cancel_requested"]  # it may have read the old tests
    assert not old[cancelled]["superseded"] and not old[other_problem]["superseded"]
    assert await db.fetchval("SELECT verdict FROM results WHERE job_id = $1", judged) == "WA"  # kept for the record

    # The board counts the new verdict, at the original minute
    for job in new:
        await db.execute("UPDATE jobs SET status = 'completed' WHERE id = $1", job)
        await db.execute("INSERT INTO results (job_id, verdict) VALUES ($1, 'AC') ON CONFLICT DO NOTHING", job)
    by_name = {r["name"]: r for r in (await board(db, contest)).rows}
    assert by_name["ann"]["cells"]["A"] == {"solved": True, "minute": 7, "rejected": 0, "pending": 0}


async def test_rejudge_jobs_dont_block_the_contestant(db):
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    contest = await make_contest(db, -3600, 3600, [problem])
    slug = await db.fetchval("SELECT slug FROM problems WHERE id = $1", problem)
    ann = await contestant(db, "ann")
    for minute in (1, 2, 3, 4):
        await submission(db, contest, problem, ann, "completed", "WA", minute)
    assert len(await commands.rejudge(db, contest, slug)) == 4

    # Four queued rejudges, yet a fresh submission is accepted under the cap of 3
    job = NewJob(kind="submission", owner_type="user", owner_id=ann, language="python", source="print(2)",
                 priority=0, problem_id=problem, contest_id=contest)
    _, created = await create_job(db, job)
    assert created


async def test_rejudging_a_problem_not_in_the_contest_changes_nothing(db):
    contest = await make_contest(db, -3600, 3600, [await insert_problem(db, [("1\n", "1\n", True)])])
    with pytest.raises(commands.AdminError):
        await commands.rejudge(db, contest, "nope")


# --- The command line itself


async def test_the_command_line(dsn, tmp_path):
    folder = problem_folder(tmp_path)

    async def cli(*args):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "admin", *args, cwd=ROOT, env={**os.environ, "DATABASE_URL": dsn},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode() + err.decode()

    assert await cli("import-problem", str(folder)) == (0, "imported sum as problem 1: 1 samples, 1 hidden tests\n"
                                                         "If it is in a contest that has started, run rejudge for it.\n")
    code, out = await cli("create-contest", "--title", "Mock", "--start", "2026-11-28 10:00", "--minutes", "90",
                          "--problems", "sum", "--invite-code", "abc")
    assert (code, out) == (0, "created contest 1\ninvite code: abc\n")
    code, out = await cli("create-contest", "--title", "Bad", "--start", "tomorrow", "--minutes", "90", "--problems", "sum")
    assert code == 1 and "error: start must look like" in out
