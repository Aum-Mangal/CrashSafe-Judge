"""The REST API (ARCHITECTURE.md §9): accounts, the visibility rule, the contest window and
submissions. No worker runs here; the queue itself is tested elsewhere."""

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import jwt
import pytest

from api.app import create_app
from api.ratelimit import Limit, LoginLimits
from core import queue
from helpers import insert_problem

SECRET = "test-secret-" + "x" * 32
CODE = "invite-123"
# The real limits, with hour-long windows: a test running across a window boundary would see
# its counter start again at zero and fail at random
LIMITS = LoginLimits(Limit("address", 20, timedelta(hours=1)), Limit("email", 10, timedelta(hours=1)))


@asynccontextmanager
async def open_api(dsn, **options):
    app = create_app(dsn, SECRET, **{"login_limits": LIMITS, **options})
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://judge") as client:
            yield client


@pytest.fixture
async def api(dsn):
    # As in production behind the tunnel, so each test request can say which address it comes from
    async with open_api(dsn, client_ip_header="CF-Connecting-IP") as client:
        yield client


async def make_contest(db, starts_in, ends_in, problem_ids=(), code=CODE):
    """A contest starting and ending this many seconds from now (negative for the past)."""
    contest = await db.fetchval(
        """
        INSERT INTO contests (title, invite_code, starts_at, ends_at)
        VALUES ('Mock', $1, now() + make_interval(secs => $2), now() + make_interval(secs => $3))
        RETURNING id
        """,
        code, starts_in, ends_in,
    )
    for n, problem in enumerate(problem_ids):
        await db.execute(
            "INSERT INTO contest_problems (contest_id, problem_id, label) VALUES ($1, $2, $3)",
            contest, problem, chr(ord("A") + n),
        )
    return contest


async def slug(db, problem_id):
    return await db.fetchval("SELECT slug FROM problems WHERE id = $1", problem_id)


async def public_problem(db):
    problem = await insert_problem(db, [("1 2\n", "3\n", True), ("SECRET-INPUT\n", "SECRET-ANSWER\n", False)])
    await db.execute("UPDATE problems SET is_public = true WHERE id = $1", problem)
    return problem


async def signed_in(api, db, email="ann@example.com"):
    """Registers a user and returns headers carrying their token."""
    if not await db.fetchval("SELECT 1 FROM contests WHERE invite_code = $1", CODE):
        await make_contest(db, 3600, 7200)
    reply = await api.post("/auth/register", json={"email": email, "password": "correct horse", "invite_code": CODE})
    assert reply.status_code == 201, reply.text
    return {"Authorization": f"Bearer {reply.json()['access_token']}"}


def source():
    return {"language": "python", "source": "print(sum(map(int, input().split())))"}


# --- Accounts


async def test_registration_needs_a_live_invite_code(api, db):
    await make_contest(db, -7200, -3600, code="old-contest")
    for code in ("wrong", "old-contest"):
        reply = await api.post("/auth/register", json={"email": "a@b.c", "password": "12345678", "invite_code": code})
        assert reply.status_code == 403
    assert await db.fetchval("SELECT count(*) FROM users") == 0


async def test_register_then_log_in(api, db):
    await signed_in(api, db, "Ann@Example.com")
    reply = await api.post("/auth/login", json={"email": "ann@example.com ", "password": "correct horse"})
    assert reply.status_code == 200
    # Only a salted hash is stored
    stored = await db.fetchval("SELECT password_hash FROM users")
    assert stored.startswith("$argon2id$") and "correct horse" not in stored


async def test_an_email_registers_only_once(api, db):
    await signed_in(api, db)
    reply = await api.post(
        "/auth/register", json={"email": "ann@example.com", "password": "another one", "invite_code": CODE}
    )
    assert reply.status_code == 409


async def test_a_wrong_password_and_an_unknown_email_look_the_same(api, db):
    await signed_in(api, db)
    wrong_password = await api.post("/auth/login", json={"email": "ann@example.com", "password": "guess"})
    unknown_email = await api.post("/auth/login", json={"email": "bob@example.com", "password": "guess"})
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


# --- Login limits (DECISIONS.md, 102)


def login(api, password, address="10.0.0.1", email="ann@example.com"):
    return api.post(
        "/auth/login", json={"email": email, "password": password}, headers={"CF-Connecting-IP": address}
    )


async def test_failed_logins_are_limited_per_email(api, db):
    await signed_in(api, db)
    # From a new address each time, so only the per-email limit applies
    codes = [(await login(api, "guess", f"10.0.1.{n}")).status_code for n in range(11)]
    assert codes == [401] * 10 + [429]
    # Locked even with the right password: otherwise the 11th guess would still be checked
    locked = await login(api, "correct horse", "10.0.2.1")
    assert locked.status_code == 429
    assert 0 < int(locked.headers["Retry-After"]) <= 3600


async def test_successful_logins_use_no_attempts(api, db):
    await signed_in(api, db)
    for n in range(12):
        assert (await login(api, "correct horse", f"10.0.1.{n}")).status_code == 200
    assert (await login(api, "guess", "10.0.2.1")).status_code == 401


async def test_guesses_sent_together_cannot_pass_the_limit(api, db):
    await signed_in(api, db)
    replies = await asyncio.gather(*(login(api, "guess", f"10.0.1.{n}") for n in range(30)))
    codes = sorted(r.status_code for r in replies)
    assert codes == [401] * 10 + [429] * 20


async def test_one_address_is_limited_across_emails(api, db):
    await signed_in(api, db)  # registers from the test client's own address, not 10.9.9.9
    codes = [(await login(api, "guess", "10.9.9.9", f"user{n}@example.com")).status_code for n in range(21)]
    assert codes == [401] * 20 + [429]
    # Registration counts too, and other addresses are unaffected
    blocked = await api.post(
        "/auth/register", json={"email": "c@d.e", "password": "12345678", "invite_code": CODE},
        headers={"CF-Connecting-IP": "10.9.9.9"},
    )
    assert blocked.status_code == 429
    assert (await login(api, "correct horse", "10.9.9.10")).status_code == 200


async def test_the_address_header_is_ignored_unless_configured(dsn, db):
    async with open_api(dsn) as api:
        await signed_in(api, db)
        # Every request comes from the same peer, whatever the header claims
        codes = [(await login(api, "guess", f"10.0.1.{n}", f"user{n}@example.com")).status_code for n in range(20)]
        assert codes == [401] * 19 + [429]


async def test_limit_counters_store_no_email_or_address(api, db):
    await signed_in(api, db)
    await login(api, "guess", "10.1.2.3")
    stored = await db.fetch("SELECT key_hash FROM login_limits")
    assert stored and all(b"ann@example.com" not in bytes(r["key_hash"]) for r in stored)
    assert all(b"10.1.2.3" not in bytes(r["key_hash"]) for r in stored)


async def test_old_limit_windows_are_deleted(db):
    for table, key in (("rate_limits", "owner_type, owner_id"), ("login_limits", "key_hash")):
        values = "'user', 1" if table == "rate_limits" else "'\\x01'::bytea"
        await db.execute(
            f"""INSERT INTO {table} ({key}, window_start, count) VALUES
                ({values}, now() - interval '2 hours', 5), ({values}, now(), 5)"""
        )
    await queue.delete_expired(db)
    for table in ("rate_limits", "login_limits"):
        assert await db.fetchval(f"SELECT count(*) FROM {table}") == 1


@pytest.mark.parametrize(
    "token",
    [
        "not-a-token",
        jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, "some other secret, 32 characters at least", "HS256"),
        jwt.encode({"sub": "1", "exp": int(time.time()) - 60}, SECRET, "HS256"),  # expired
        jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, None, "none"),  # unsigned
        jwt.encode({"sub": "1"}, SECRET, "HS256"),  # never expires
    ],
    ids=["garbage", "wrong-secret", "expired", "alg-none", "no-expiry"],
)
async def test_bad_tokens_are_refused(api, db, token):
    await signed_in(api, db)
    reply = await api.post("/submissions", json={"problem": "x", **source()}, headers={"Authorization": f"Bearer {token}"})
    assert reply.status_code == 401


async def test_a_deleted_users_token_stops_working(api, db):
    headers = await signed_in(api, db)
    await db.execute("DELETE FROM users")
    reply = await api.get(f"/submissions/{'0' * 8}-0000-0000-0000-{'0' * 12}", headers=headers)
    assert reply.status_code == 401


# --- Visibility (ARCHITECTURE.md §9: a hidden problem answers exactly like a missing one)


async def test_a_contest_problem_is_hidden_until_the_start(api, db):
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    contest = await make_contest(db, 3600, 7200, [problem])
    name = await slug(db, problem)

    assert (await api.get(f"/problems/{name}")).json() == (await api.get("/problems/no-such-problem")).json()
    assert (await api.get(f"/problems/{name}")).status_code == 404
    assert name not in (await api.get("/problems")).text

    await db.execute("UPDATE contests SET starts_at = now() - interval '1 second' WHERE id = $1", contest)
    assert (await api.get(f"/problems/{name}")).status_code == 200
    assert name in (await api.get("/problems")).text


async def test_a_problem_in_no_contest_and_not_public_is_hidden(api, db):
    name = await slug(db, await insert_problem(db, [("1\n", "1\n", True)]))
    assert (await api.get(f"/problems/{name}")).status_code == 404
    assert (await api.get("/problems")).json() == []


async def test_a_problem_shows_samples_but_never_hidden_tests(api, db):
    name = await slug(db, await public_problem(db))
    reply = await api.get(f"/problems/{name}")
    assert reply.status_code == 200
    assert reply.json()["samples"] == [{"input": "1 2\n", "expected": "3\n"}]
    assert "SECRET" not in reply.text


# --- Submissions


async def test_practice_submission_on_a_public_problem(api, db):
    headers = await signed_in(api, db)
    name = await slug(db, await public_problem(db))
    reply = await api.post("/submissions", json={"problem": name, **source()}, headers=headers)
    assert reply.status_code == 201
    job = reply.json()["id"]
    assert reply.headers["Location"] == f"/submissions/{job}"
    row = await db.fetchrow("SELECT kind, priority, status, contest_id FROM jobs WHERE id = $1::uuid", job)
    assert dict(row) == {"kind": "submission", "priority": 1, "status": "queued", "contest_id": None}

    shown = await api.get(f"/submissions/{job}", headers=headers)
    assert shown.json()["status"] == "queued" and shown.json()["verdict"] is None


async def test_submitting_needs_a_token(api, db):
    name = await slug(db, await public_problem(db))
    assert (await api.post("/submissions", json={"problem": name, **source()})).status_code == 401


async def test_a_hidden_problem_cannot_be_submitted_to(api, db):
    headers = await signed_in(api, db)
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    contest = await make_contest(db, 3600, 7200, [problem], code="later")
    for extra in ({}, {"contest_id": contest}):
        reply = await api.post("/submissions", json={"problem": await slug(db, problem), **source(), **extra}, headers=headers)
        assert reply.status_code == 404
    assert await db.fetchval("SELECT count(*) FROM jobs") == 0


async def test_contest_submissions_only_inside_the_window(api, db):
    headers = await signed_in(api, db)
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    contest = await make_contest(db, -60, 3600, [problem], code="running")
    body = {"problem": await slug(db, problem), "contest_id": contest, **source()}

    reply = await api.post("/submissions", json=body, headers=headers)
    assert reply.status_code == 201
    row = await db.fetchrow(
        "SELECT j.priority, j.submitted_at < c.ends_at AS in_time FROM jobs j, contests c WHERE c.id = $1", contest
    )
    assert (row["priority"], row["in_time"]) == (0, True)

    # Without contest_id, a non-public problem has no practice mode
    assert (await api.post("/submissions", json={**body, "contest_id": None}, headers=headers)).status_code == 403

    await db.execute("UPDATE contests SET ends_at = now() - interval '1 second' WHERE id = $1", contest)
    reply = await api.post("/submissions", json={**body, "source": "print(2)"}, headers=headers)
    assert reply.status_code == 403
    assert await db.fetchval("SELECT count(*) FROM jobs") == 1


async def test_a_retry_after_the_contest_ended_gets_its_job(api, db):
    headers = await signed_in(api, db)
    problem = await insert_problem(db, [("1\n", "1\n", True)])
    contest = await make_contest(db, -60, 3600, [problem], code="running")
    body = {"problem": await slug(db, problem), "contest_id": contest, **source()}
    first = await api.post("/submissions", json=body, headers={**headers, "Idempotency-Key": "k1"})

    # The reply was lost, and the retry arrives just after the end
    await db.execute("UPDATE contests SET ends_at = now() - interval '1 second' WHERE id = $1", contest)
    retry = await api.post("/submissions", json=body, headers={**headers, "Idempotency-Key": "k1"})
    assert (retry.status_code, retry.json()["id"]) == (200, first.json()["id"])


async def test_an_idempotency_key_reused_for_other_code_is_a_conflict(api, db):
    headers = {**await signed_in(api, db), "Idempotency-Key": "k1"}
    name = await slug(db, await public_problem(db))
    first = await api.post("/submissions", json={"problem": name, **source()}, headers=headers)
    same = await api.post("/submissions", json={"problem": name, **source()}, headers=headers)
    other = await api.post("/submissions", json={"problem": name, "language": "python", "source": "print(3)"}, headers=headers)
    assert (first.status_code, same.status_code, other.status_code) == (201, 200, 409)
    assert same.json()["id"] == first.json()["id"]


async def test_too_many_submissions_in_flight_get_429(api, db):
    headers = await signed_in(api, db)
    name = await slug(db, await public_problem(db))
    codes = []
    for n in range(4):
        reply = await api.post("/submissions", json={"problem": name, "language": "python", "source": f"print({n})"}, headers=headers)
        codes.append(reply.status_code)
    assert codes == [201, 201, 201, 429]  # a user may have 3 queued or running (core/jobs.py)
    assert int(reply.headers["Retry-After"]) > 0


async def test_another_users_submission_looks_missing(api, db):
    ann = await signed_in(api, db, "ann@example.com")
    bob = await signed_in(api, db, "bob@example.com")
    name = await slug(db, await public_problem(db))
    job = (await api.post("/submissions", json={"problem": name, **source()}, headers=ann)).json()["id"]

    assert (await api.get(f"/submissions/{job}", headers=bob)).status_code == 404
    assert (await api.delete(f"/submissions/{job}", headers=bob)).status_code == 404
    assert await db.fetchval("SELECT status FROM jobs WHERE id = $1::uuid", job) == "queued"


async def test_cancelling_a_queued_submission(api, db):
    headers = await signed_in(api, db)
    name = await slug(db, await public_problem(db))
    job = (await api.post("/submissions", json={"problem": name, **source()}, headers=headers)).json()["id"]
    reply = await api.delete(f"/submissions/{job}", headers=headers)
    assert (reply.status_code, reply.json()) == (202, {"status": "cancelled"})


async def test_a_result_shows_verdict_and_timing(api, db):
    headers = await signed_in(api, db)
    name = await slug(db, await public_problem(db))
    job = (await api.post("/submissions", json={"problem": name, **source()}, headers=headers)).json()["id"]
    # Stands in for a worker's commit
    await db.execute("UPDATE jobs SET status = 'completed' WHERE id = $1::uuid", job)
    await db.execute(
        """INSERT INTO results (job_id, verdict, per_test, failed_test)
           VALUES ($1::uuid, 'WA', '[{"verdict": "AC", "cpu_ms": 5}, {"verdict": "WA", "cpu_ms": 6}]', 2)""",
        job,
    )
    shown = (await api.get(f"/submissions/{job}", headers=headers)).json()
    assert (shown["verdict"], shown["failed_test"], shown["tests"][1]["cpu_ms"]) == ("WA", 2, 6)


async def test_health(api):
    assert (await api.get("/health/live")).status_code == 200
    assert (await api.get("/health/ready")).status_code == 200


def test_a_short_jwt_secret_is_refused():
    with pytest.raises(ValueError):
        create_app("postgresql://unused", "short")
