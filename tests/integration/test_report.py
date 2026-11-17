"""admin status and admin contest-report (admin/report.py). Jobs are inserted with exact
timestamps, so every number in the report is known in advance."""

import asyncio
import hashlib
import os
import sys

import pytest

from admin import report
from helpers import ROOT, insert_problem, make_contest

CANARIES = ("canary-email@example.test", "CanaryDisplayName", "CANARY_SOURCE_CODE")


async def add_job(db, problem_id, *, contest_id=None, owner_id=1, language="cpp", status="completed",
                  at=0.0, started=None, finished=None, attempts=1, verdict=None, rejudge_of=None,
                  worker_id=None, lease_in=None, base="date_trunc('minute', now()) - interval '50 minutes'"):
    """A job submitted `at` seconds after base, started and finished that many seconds after base."""
    def when(seconds):
        return None if seconds is None else f"{base} + make_interval(secs => {seconds})"

    job_id = await db.fetchval(
        f"""
        INSERT INTO jobs (kind, owner_type, owner_id, request_hash, language, source, problem_id, contest_id,
                          priority, status, attempts, submitted_at, created_at, started_at, finished_at,
                          rejudge_of, worker_id, lease_until)
        VALUES ('submission', 'user', $1, $2, $3, $4, $5, $6, 0, $7, $8, {when(at)}, {when(at)},
                {when(started) or 'NULL'}, {when(finished) or 'NULL'}, $9, $10,
                {f"now() + make_interval(secs => {lease_in})" if lease_in is not None else 'NULL'})
        RETURNING id
        """,
        owner_id, hashlib.sha256(os.urandom(8)).digest(), language, CANARIES[2], problem_id, contest_id,
        status, attempts, rejudge_of, worker_id,
    )
    if verdict:
        await db.execute("INSERT INTO results (job_id, verdict) VALUES ($1, $2)", job_id, verdict)
    return job_id


# --- contest-report


@pytest.fixture
async def finished_contest(db):
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    contest = await make_contest(db, -3600, -300, [problem], title="Mock round")
    users = [await db.fetchval(
        "INSERT INTO users (email, password_hash, display_name) VALUES ($1, 'x', $2) RETURNING id",
        f"{n}{CANARIES[0]}", f"{CANARIES[1]}{n}",
    ) for n in range(3)]
    a = await add_job(db, problem, contest_id=contest, owner_id=users[0], at=0, started=1, finished=3, verdict="AC")
    await add_job(db, problem, contest_id=contest, owner_id=users[0], language="python",
                  at=10, started=12, finished=13, attempts=2, verdict="WA")
    await add_job(db, problem, contest_id=contest, owner_id=users[1], at=20, started=20, finished=21, verdict="AC")
    await add_job(db, problem, contest_id=contest, owner_id=users[2], language="python",
                  at=30, started=34, finished=40, verdict="TLE")
    await add_job(db, problem, contest_id=contest, owner_id=users[2], language="python", status="dead",
                  at=30.5, started=31, finished=60, attempts=3, verdict="IE")
    # Neither of these is a contest submission: a rejudge, and a practice submission
    await add_job(db, problem, contest_id=contest, owner_id=users[0], at=200, started=200, finished=201,
                  verdict="AC", rejudge_of=a)
    await add_job(db, problem, owner_id=users[1], at=5, started=100, finished=101, verdict="WA")
    return contest


async def test_contest_report_numbers(db, finished_contest):
    text = await report.contest_report(db, finished_contest)
    assert "5 submissions from 3 contestants: 2 C++, 3 Python" in text
    assert "Verdicts: AC 2, IE 1, TLE 1, WA 1" in text
    assert "Busiest minute: 5 submissions" in text
    # At 30.5 s the jobs submitted at 30 and 30.5 were both still waiting
    assert "Most of the contest's submissions waiting at once: 2" in text
    # Completed jobs only: verdict times 1, 3, 3, 10 s; waits 0, 1, 2, 4 s
    assert "| Submission to verdict | 3000 |" in text and "| 10000 |" in text
    assert "| Waiting for a worker | 1500 |" in text and "| 4000 |" in text
    assert "more than one attempt): 2, at most 3 attempts" in text
    assert "Dead (3 failed attempts, judged IE): 1" in text
    assert "Rejudged afterwards: 1" in text
    assert "not final" not in text


async def test_contest_report_publishes_no_personal_data_or_code(db, finished_contest):
    text = await report.contest_report(db, finished_contest)
    for canary in CANARIES:
        assert canary.lower() not in text.lower()


async def test_contest_report_says_when_the_contest_is_still_running(db):
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    contest = await make_contest(db, -60, 3600, [problem])
    text = await report.contest_report(db, contest)
    assert "Still running: these numbers are not final." in text
    assert "0 submissions from 0 contestants" in text


async def test_cancelled_before_starting_stops_counting_as_waiting(db):
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    contest = await make_contest(db, -3600, -300, [problem])
    await add_job(db, problem, contest_id=contest, status="cancelled", at=0, finished=1)
    await add_job(db, problem, contest_id=contest, at=5, started=6, finished=7, verdict="AC")
    text = await report.contest_report(db, contest)
    assert "waiting at once: 1" in text  # the cancelled job had left the queue by 5 s


async def test_unknown_contest(db, dsn):
    with pytest.raises(report.NoSuchContest):
        await report.contest_report(db, 999)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "admin", "contest-report", "--contest", "999", cwd=ROOT,
        env={**os.environ, "DATABASE_URL": dsn}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    assert (proc.returncode, err.decode().strip()) == (1, "error: no contest 999")


# --- status


async def test_status_quiet(db):
    text, warnings = await report.status(db)
    assert warnings == []
    assert "No contest running or starting within a day" in text
    assert "Queue: 0 waiting (+0 rejudges), 0 running" in text
    assert text.endswith("No warnings")


async def test_status_shows_the_running_contest(db, finished_contest):
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    contest = await make_contest(db, -600, 1800, [problem], code="other", title="Live round")
    await add_job(db, problem, contest_id=contest, status="queued", base="now()", at=-5)
    text, _ = await report.status(db)
    assert f'Contest {contest} "Live round" is running, 30 min left, 1 submissions' in text
    assert "Mock round" not in text  # it ended


async def test_status_warns_about_a_job_waiting_too_long(db):
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    await add_job(db, problem, status="queued", base="now()", at=-90)
    text, warnings = await report.status(db)
    assert len(warnings) == 1 and "a job has waited 90 s: are the workers running?" in warnings[0]
    assert "WARNING: a job has waited" in text


async def test_status_warns_about_a_lease_nobody_reaped(db):
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    await add_job(db, problem, status="running", base="now()", at=-60, started=-60, worker_id="w1", lease_in=-30)
    await add_job(db, problem, status="running", base="now()", at=-5, started=-5, worker_id="w2", lease_in=25)
    text, warnings = await report.status(db)
    assert "w1: a submission for 60.0 s, attempt 1, lease -30.0 s" in text
    assert "w2: a submission for 5.0 s, attempt 1, lease +25.0 s" in text
    assert len(warnings) == 1 and warnings[0].startswith("w1's lease ran out 30 s ago")


async def test_status_warns_about_ie_and_dead_jobs(db):
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    await add_job(db, problem, base="now()", at=-20, started=-19, finished=-17, verdict="AC")
    await add_job(db, problem, base="now()", at=-20, started=-18, finished=-15, attempts=2, verdict="WA")
    await add_job(db, problem, status="dead", base="now()", at=-30, started=-29, finished=-1, attempts=3, verdict="IE")
    text, warnings = await report.status(db)
    assert "Last 10 min: 3 finished (" in text and "2 needed a retry" in text
    assert "p50 4.0 s" in text  # completed only: 3 and 5 s
    assert len(warnings) == 1 and warnings[0].startswith("1 IE verdicts and 1 dead jobs")
