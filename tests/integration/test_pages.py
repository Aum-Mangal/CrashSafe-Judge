"""The contest pages (api/pages.py), driven the way a browser drives them: form posts, 303
redirects and a session cookie. No worker runs; a result row stands in for one."""

import re

import pytest

from helpers import insert_problem, make_contest, open_api

CODE = "invite-123"
PASSWORD = "correct horse"


@pytest.fixture
async def site(dsn):
    # cookie_secure=False only because the test client speaks plain HTTP
    async with open_api(dsn, cookie_secure=False) as client:
        yield client


async def register(site, email="ann@example.com"):
    reply = await site.post("/register", data={"email": email, "password": PASSWORD, "invite_code": CODE})
    assert reply.status_code == 303, reply.text
    return reply


def csrf_from(html):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def key_from(html):
    return re.search(r'name="key" value="([0-9a-f]+)"', html).group(1)


async def running_contest(db, **contest):
    """A contest that started a minute ago with one problem, A, plus a contestant signed in."""
    problem = await insert_problem(
        db, [("1 2\n", "3\n", True), ("HIDDEN-INPUT\n", "HIDDEN-ANSWER\n", False)]
    )
    await db.execute("UPDATE problems SET title = 'Sum', statement_md = 'Add **two** numbers.' WHERE id = $1", problem)
    return await make_contest(db, -60, 3600, [problem], **contest)


async def submit(site, contest, source="print(3)", **override):
    page = await site.get(f"/c/{contest}/A")
    form = {"language": "python", "source": source, "key": key_from(page.text), "csrf": csrf_from(page.text), **override}
    return await site.post(f"/c/{contest}/A", data=form)


# --- Accounts


async def test_the_session_cookie_is_httponly_and_samesite(site, db):
    await make_contest(db, 3600, 7200)
    cookie = (await register(site)).headers["set-cookie"]
    assert "judge_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie


async def test_the_session_cookie_is_secure_by_default(dsn, db):
    await make_contest(db, 3600, 7200)
    async with open_api(dsn) as site:
        assert "Secure" in (await register(site)).headers["set-cookie"]


async def test_log_in_and_out(site, db):
    await make_contest(db, 3600, 7200)
    await register(site)
    site.cookies.clear()

    wrong = await site.post("/login", data={"email": "ann@example.com", "password": "guess"})
    assert wrong.status_code == 401 and "Wrong email or password." in wrong.text
    right = await site.post("/login", data={"email": "ann@example.com", "password": PASSWORD})
    assert right.status_code == 303

    home = await site.get("/")
    assert "Log out" in home.text
    # Logging out is a signed-in form too, so it needs the token
    assert (await site.post("/logout", data={"csrf": "0" * 64})).status_code == 403
    out = await site.post("/logout", data={"csrf": csrf_from(home.text)})
    assert out.status_code == 303 and 'judge_session=""' in out.headers["set-cookie"]


async def test_a_bad_invite_code_shows_the_form_again(site, db):
    reply = await site.post("/register", data={"email": "a@b.c", "password": PASSWORD, "invite_code": "nope"})
    assert reply.status_code == 403 and "Invalid invite code." in reply.text and "set-cookie" not in reply.headers


# --- Contest and problem pages


async def test_problems_appear_only_at_the_start(site, db):
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    await db.execute("UPDATE problems SET title = 'Secret title' WHERE id = $1", problem)
    contest = await make_contest(db, 3600, 7200, [problem])

    page = await site.get(f"/c/{contest}")
    assert page.status_code == 200 and "Starts in" in page.text
    assert "Secret title" not in page.text
    assert (await site.get(f"/c/{contest}/A")).status_code == 404

    await db.execute("UPDATE contests SET starts_at = now() - interval '1 second' WHERE id = $1", contest)
    assert "Secret title" in (await site.get(f"/c/{contest}")).text
    assert (await site.get(f"/c/{contest}/A")).status_code == 200


async def test_a_problem_page_shows_samples_but_never_hidden_tests(site, db):
    contest = await running_contest(db)
    page = await site.get(f"/c/{contest}/A")
    assert "<strong>two</strong>" in page.text  # the statement's Markdown is rendered
    assert "1 2\n" in page.text and "HIDDEN" not in page.text


async def test_the_invite_code_is_never_shown(site, db):
    contest = await running_contest(db, code="the-secret-code")
    for path in ("/", f"/c/{contest}", f"/c/{contest}/A"):
        assert "the-secret-code" not in (await site.get(path)).text


async def test_text_from_the_database_is_escaped(site, db):
    contest = await running_contest(db, title="<script>alert(1)</script>")
    await db.execute("UPDATE problems SET statement_md = '<img src=x onerror=alert(2)>'")
    home = await site.get("/")
    assert "<script>alert(1)" not in home.text and "&lt;script&gt;alert(1)" in home.text
    problem = (await site.get(f"/c/{contest}/A")).text
    assert "<img" not in problem and "&lt;img" in problem


async def test_every_page_tells_the_browser_to_run_no_scripts(site):
    policy = (await site.get("/")).headers["content-security-policy"]
    assert "script-src 'none'" in policy and "frame-ancestors 'none'" in policy


# --- Submitting


async def test_submit_and_see_the_verdict(site, db):
    contest = await running_contest(db)
    await register(site)
    reply = await submit(site, contest, source="print(3)  # </pre><script>alert(1)</script>")
    assert reply.status_code == 303
    location = reply.headers["location"]
    job = location.removeprefix("/s/")
    row = await db.fetchrow("SELECT contest_id, priority, status FROM jobs WHERE id = $1::uuid", job)
    assert dict(row) == {"contest_id": contest, "priority": 0, "status": "queued"}

    waiting = await site.get(location)
    assert "In the queue" in waiting.text and 'http-equiv="refresh"' in waiting.text
    assert "<script>alert(1)" not in waiting.text  # the code is shown as text

    # Stands in for a worker's commit
    await db.execute("UPDATE jobs SET status = 'completed' WHERE id = $1::uuid", job)
    await db.execute(
        """INSERT INTO results (job_id, verdict, per_test, failed_test)
           VALUES ($1::uuid, 'WA', '[{"verdict": "AC", "cpu_ms": 4, "peak_kb": 9000}, {"verdict": "WA", "cpu_ms": 5}]', 2)""",
        job,
    )
    done = await site.get(location)
    assert "Wrong answer" in done.text and "on test 2" in done.text
    assert 'http-equiv="refresh"' not in done.text
    assert "Wrong answer" in (await site.get(f"/c/{contest}")).text  # listed on the contest page


async def test_a_double_click_makes_one_submission(site, db):
    contest = await running_contest(db)
    await register(site)
    page = await site.get(f"/c/{contest}/A")
    form = {"language": "python", "source": "print(3)", "key": key_from(page.text), "csrf": csrf_from(page.text)}
    first = await site.post(f"/c/{contest}/A", data=form)
    second = await site.post(f"/c/{contest}/A", data=form)
    assert first.headers["location"] == second.headers["location"]
    assert await db.fetchval("SELECT count(*) FROM jobs") == 1


@pytest.mark.parametrize("csrf", ["", "0" * 64, "from-another-session"])
async def test_a_submit_without_our_csrf_token_is_refused(site, db, dsn, csrf):
    contest = await running_contest(db)
    if csrf == "from-another-session":
        # A token that was valid, but for someone else's session
        async with open_api(dsn, cookie_secure=False) as other:
            await register(other, "bob@example.com")
            csrf = csrf_from((await other.get(f"/c/{contest}/A")).text)
    await register(site)
    reply = await submit(site, contest, csrf=csrf)
    assert reply.status_code == 403
    assert await db.fetchval("SELECT count(*) FROM jobs") == 0


async def test_signed_out_visitors_are_sent_to_log_in(site, db):
    contest = await running_contest(db)
    page = await site.get(f"/c/{contest}/A")
    assert "Log in</a> to submit" in page.text
    reply = await site.post(f"/c/{contest}/A", data={"language": "python", "source": "x", "key": "k", "csrf": ""})
    assert (reply.status_code, reply.headers["location"]) == (303, "/login")


async def test_after_the_end_the_form_is_gone_and_posts_are_refused(site, db):
    contest = await running_contest(db)
    await register(site)
    page = await site.get(f"/c/{contest}/A")
    await db.execute("UPDATE contests SET ends_at = now() - interval '1 second'")
    assert "submissions are closed" in (await site.get(f"/c/{contest}/A")).text
    form = {"language": "python", "source": "print(3)", "key": key_from(page.text), "csrf": csrf_from(page.text)}
    reply = await site.post(f"/c/{contest}/A", data=form)
    assert reply.status_code == 403 and "The contest is not running." in reply.text


async def test_another_users_submission_looks_missing(site, db, dsn):
    contest = await running_contest(db)
    await register(site)
    location = (await submit(site, contest)).headers["location"]
    async with open_api(dsn, cookie_secure=False) as bob:
        await register(bob, "bob@example.com")
        assert (await bob.get(location)).status_code == 404
