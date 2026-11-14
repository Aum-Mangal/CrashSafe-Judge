"""ICPC ranking (core/leaderboard.py). Submissions are written straight into the tables at chosen
minutes after the start, so each rule can be checked with exact numbers."""

from core.leaderboard import board
from helpers import insert_problem, make_contest, open_api


async def setup(db, problems=2):
    """A contest that started 3 hours ago, with problems A, B, ..."""
    ids = [await insert_problem(db, [("1\n", "1\n", True)]) for _ in range(problems)]
    contest = await make_contest(db, -3 * 3600, 3600, ids)
    return contest, dict(zip("ABCDEF", ids))


async def user(db, name):
    return await db.fetchval(
        "INSERT INTO users (email, password_hash, display_name) VALUES ($1, 'x', $2) RETURNING id",
        f"{name.lower()}@example.com", name,
    )


async def sub(db, contest, problem, who, minute, verdict=None, status="completed", superseded=False):
    """A submission made `minute` minutes (plus a few seconds) after the start. verdict=None
    with status 'queued' is one still waiting to be judged."""
    job = await db.fetchval(
        """
        INSERT INTO jobs (kind, owner_type, owner_id, request_hash, language, source, problem_id,
                          contest_id, priority, status, superseded, submitted_at)
        SELECT 'submission', 'user', $1, '\\x00', 'python', 'print(1)', $2, $3, 0, $4, $5,
               starts_at + make_interval(mins => $6, secs => 7)
        FROM contests WHERE id = $3
        RETURNING id
        """,
        who, problem, contest, status, superseded, minute,
    )
    if verdict:
        await db.execute("INSERT INTO results (job_id, verdict) VALUES ($1, $2)", job, verdict)


def summary(found):
    return [(r["rank"], r["name"], r["solved"], r["penalty"]) for r in found.rows]


async def test_icpc_ranking(db):
    contest, p = await setup(db)
    ann, bob, cara = await user(db, "Ann"), await user(db, "Bob"), await user(db, "Cara")
    # Ann: A at 10 after two rejections (the CE is free), B at 30 first try: 10 + 40 + 30 = 80
    await sub(db, contest, p["A"], ann, 2, "WA")
    await sub(db, contest, p["A"], ann, 4, "CE")
    await sub(db, contest, p["A"], ann, 6, "TLE")
    await sub(db, contest, p["A"], ann, 10, "AC")
    await sub(db, contest, p["A"], ann, 40, "WA")  # after solving: ignored
    await sub(db, contest, p["B"], ann, 30, "AC")
    # Bob: A at 20, B at 25 after one RE: 20 + 25 + 20 = 65, so he beats Ann
    await sub(db, contest, p["A"], bob, 20, "AC")
    await sub(db, contest, p["B"], bob, 21, "RE")
    await sub(db, contest, p["B"], bob, 25, "AC")
    # Cara: only A, at minute 1
    await sub(db, contest, p["A"], cara, 1, "AC")

    found = await board(db, contest)
    assert found.labels == ["A", "B"]
    assert summary(found) == [(1, "Bob", 2, 65), (2, "Ann", 2, 80), (3, "Cara", 1, 1)]
    ann_a = found.rows[1]["cells"]["A"]
    assert ann_a == {"solved": True, "minute": 10, "rejected": 2, "pending": 0}


async def test_unsolved_problems_cost_nothing(db):
    contest, p = await setup(db)
    dan, eve = await user(db, "Dan"), await user(db, "Eve")
    for minute in (1, 2, 3):
        await sub(db, contest, p["A"], dan, minute, "WA")
    await sub(db, contest, p["B"], eve, 50, "AC")
    found = await board(db, contest)
    assert summary(found) == [(1, "Eve", 1, 50), (2, "Dan", 0, 0)]
    assert found.rows[1]["cells"]["A"] == {"solved": False, "minute": None, "rejected": 3, "pending": 0}


async def test_internal_errors_and_compile_errors_are_free(db):
    contest, p = await setup(db)
    fay = await user(db, "Fay")
    await sub(db, contest, p["A"], fay, 1, "IE", status="dead")
    await sub(db, contest, p["A"], fay, 2, "CE")
    await sub(db, contest, p["A"], fay, 5, "AC")
    assert summary(await board(db, contest)) == [(1, "Fay", 1, 5)]


async def test_ties_share_a_rank(db):
    contest, p = await setup(db)
    gus, hal, ivy = await user(db, "Gus"), await user(db, "Hal"), await user(db, "Ivy")
    await sub(db, contest, p["A"], gus, 10, "AC")
    await sub(db, contest, p["A"], hal, 10, "AC")
    await sub(db, contest, p["A"], ivy, 11, "AC")
    assert [r["rank"] for r in (await board(db, contest)).rows] == [1, 1, 3]


async def test_superseded_and_cancelled_dont_count_and_waiting_shows_as_pending(db):
    contest, p = await setup(db)
    jo = await user(db, "Jo")
    await sub(db, contest, p["A"], jo, 3, "AC", superseded=True)  # replaced by a rejudge
    await sub(db, contest, p["A"], jo, 3, "WA")  # the rejudge's verdict, same submitted time
    await sub(db, contest, p["B"], jo, 4, "WA", status="cancelled")
    await sub(db, contest, p["B"], jo, 8, None, status="queued")
    found = await board(db, contest)
    assert summary(found) == [(1, "Jo", 0, 0)]
    assert found.rows[0]["cells"]["A"]["rejected"] == 1
    assert found.rows[0]["cells"]["B"] == {"solved": False, "minute": None, "rejected": 0, "pending": 1}


async def test_before_the_start_the_board_is_empty(db):
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    contest = await make_contest(db, 3600, 7200, [problem])
    found = await board(db, contest)
    assert (found.started, found.labels, found.rows) == (False, [], [])
    assert await board(db, contest + 1) is None


# --- Served over REST and as a page


async def test_the_public_board_shows_names_never_emails(dsn, db):
    contest, p = await setup(db)
    await sub(db, contest, p["A"], await user(db, "<b>Kim</b>"), 7, "AC")
    async with open_api(dsn, leaderboard_seconds=0) as api:
        rest = await api.get(f"/contests/{contest}/leaderboard")
        page = await api.get(f"/c/{contest}/leaderboard")
        missing = await api.get(f"/contests/{contest + 1}/leaderboard")
    assert rest.json()["rows"][0]["name"] == "<b>Kim</b>" and "example.com" not in rest.text
    assert "&lt;b&gt;Kim&lt;/b&gt;" in page.text and "example.com" not in page.text
    assert missing.status_code == 404


async def test_before_the_start_no_problem_labels_are_shown(dsn, db):
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    contest = await make_contest(db, 3600, 7200, [problem])
    async with open_api(dsn) as api:
        assert (await api.get(f"/contests/{contest}/leaderboard")).json() == {"started": False, "problems": [], "rows": []}


async def test_the_board_is_reused_for_a_few_seconds(dsn, db):
    contest, p = await setup(db)
    lee = await user(db, "Lee")
    async with open_api(dsn, leaderboard_seconds=60) as api:
        before = (await api.get(f"/contests/{contest}/leaderboard")).json()
        await sub(db, contest, p["A"], lee, 3, "AC")
        cached = (await api.get(f"/contests/{contest}/leaderboard")).json()
    async with open_api(dsn, leaderboard_seconds=0) as api:
        fresh = (await api.get(f"/contests/{contest}/leaderboard")).json()
    assert before["rows"] == cached["rows"] == []
    assert fresh["rows"][0]["name"] == "Lee"
