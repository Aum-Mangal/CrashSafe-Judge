"""The MCP server (api/mcp.py), spoken to the way a client speaks it: JSON-RPC over HTTP POST, in
both protocol eras. No worker runs; each test claims and finishes jobs itself, as one would."""

import asyncio
import json
import time
import uuid

import pytest

from admin.commands import create_client
from core.queue import Result
from helpers import LEGACY, MODERN, finish_next, insert_problem, make_contest, open_mcp, rpc

ERAS = pytest.mark.parametrize("era", [MODERN, LEGACY])
TOOLS = {"run_code", "verify_code", "submit_solution", "list_problems", "get_problem", "get_result", "cancel_run"}


@pytest.fixture
async def site(dsn):
    async with open_mcp(dsn) as client:
        yield client


@pytest.fixture
async def key(db):
    return (await create_client(db, "agent", 5))[1]


async def tool(client, key, name, era=MODERN, **arguments):
    """Calls a tool. Returns its structured result, or ('error', message) for a tool error."""
    reply = await rpc(client, key, "tools/call", {"name": name, "arguments": arguments}, era)
    assert reply.status_code == 200, reply.text
    result = reply.json()["result"]
    if result["isError"]:
        # The SDK puts this in front of a tool's own message
        return "error", result["content"][0]["text"].removeprefix(f"Error executing tool {name}: ")
    return result["structuredContent"]


# --- Who may call


@pytest.mark.parametrize("authorization", [None, "Bearer jk_made-up", "Bearer not-a-key", "Basic YWdlbnQ6eA=="])
async def test_a_request_without_a_known_key_is_refused(site, db, authorization):
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": LEGACY}
    if authorization:
        headers["Authorization"] = authorization
    reply = await site.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert reply.status_code == 401 and reply.headers["www-authenticate"] == "Bearer"
    assert "tools" not in reply.text


async def test_only_post_is_served(site, key):
    assert (await site.get("/mcp", headers={"Authorization": f"Bearer {key}"})).status_code == 405


# --- Discovery


@ERAS
async def test_both_protocol_eras_list_the_seven_tools(site, key, era):
    reply = await rpc(site, key, "tools/list", era=era)
    found = {t["name"]: t for t in reply.json()["result"]["tools"]}
    assert set(found) == TOOLS
    assert found["get_result"]["annotations"]["readOnlyHint"] is True
    assert "idempotency_key" in found["run_code"]["inputSchema"]["properties"]
    assert "verdict" in found["run_code"]["outputSchema"]["properties"]


async def test_server_discover_names_the_modern_protocol(site, key):
    reply = await rpc(site, key, "server/discover")
    assert MODERN in reply.json()["result"]["supportedVersions"]


# --- Running code


@ERAS
async def test_run_code_returns_its_job_and_later_its_output(site, db, key, era):
    started = await tool(site, key, "run_code", era, language="python", source="print(input())", stdin="hi\n")
    assert started["status"] == "queued" and "get_result" in started["note"]
    job = await db.fetchrow("SELECT * FROM jobs WHERE id = $1", uuid.UUID(started["job_id"]))
    assert (job["kind"], job["owner_type"], job["priority"], job["stdin"]) == ("run", "client", 2, "hi\n")
    assert 23.9 < (job["expires_at"] - job["created_at"]).total_seconds() / 3600 <= 24  # agent jobs expire

    await finish_next(db, Result("OK", stdout="hi\n", per_test=[{"verdict": "OK", "cpu_ms": 9}]))
    done = await tool(site, key, "get_result", era, job_id=started["job_id"])
    assert (done["status"], done["verdict"], done["meaning"], done["stdout"]) == ("completed", "OK", "Ran", "hi\n")
    assert done["note"] is None


async def test_a_tool_waits_for_the_verdict_and_wakes_on_the_notification(dsn, db, key):
    # The recheck is 30 s, so only the NOTIFY job_done can end this wait within the 3 s asserted
    async with open_mcp(dsn, mcp_wait_seconds=10, mcp_recheck_seconds=30) as site:
        started = time.monotonic()
        call = asyncio.create_task(tool(site, key, "run_code", language="python", source="print(1)"))
        while not await db.fetchval("SELECT count(*) FROM jobs"):
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        await finish_next(db, Result("OK", stdout="1\n"))
        done = await call
    assert done["status"] == "completed" and done["stdout"] == "1\n"
    assert time.monotonic() - started < 3


async def test_a_lost_notification_is_caught_by_the_recheck(dsn, db, key):
    async with open_mcp(dsn, mcp_wait_seconds=10, mcp_recheck_seconds=0.3) as site:
        call = asyncio.create_task(tool(site, key, "run_code", language="python", source="print(1)"))
        while not (job_id := await db.fetchval("SELECT id FROM jobs")):
            await asyncio.sleep(0.05)
        # Finished without any NOTIFY, as if it was sent while the listener was reconnecting
        await db.execute("UPDATE jobs SET status = 'completed' WHERE id = $1", job_id)
        await db.execute("INSERT INTO results (job_id, verdict, stdout_trunc) VALUES ($1, 'OK', '1\n')", job_id)
        done = await asyncio.wait_for(call, 5)
    assert done["status"] == "completed"


async def test_verify_code_stores_the_tests_and_refuses_more_than_twenty(site, db, key):
    tests = [{"input": "1\n", "expected": "1"}, {"input": "2\n", "expected": "2"}]
    started = await tool(site, key, "verify_code", language="cpp", source="int main(){}", tests=tests)
    stored = await db.fetchval("SELECT tests FROM jobs WHERE id = $1", uuid.UUID(started["job_id"]))
    assert json.loads(stored) == tests

    kind, message = await tool(site, key, "verify_code", language="cpp", source="int main(){}", tests=tests * 11)
    assert kind == "error" and await db.fetchval("SELECT count(*) FROM jobs") == 1


async def test_a_failed_job_says_it_is_not_the_codes_fault(site, db, key):
    started = await tool(site, key, "run_code", language="python", source="print(1)")
    await db.execute("UPDATE jobs SET status = 'dead' WHERE id = $1", uuid.UUID(started["job_id"]))
    done = await tool(site, key, "get_result", job_id=started["job_id"])
    assert done["status"] == "failed" and "not your code's fault" in done["note"]


# --- Problems


async def test_submit_solution_takes_public_problems_only(site, db, key):
    public = await insert_problem(db, [("1\n", "1\n", True), ("SECRET\n", "2\n", False)])
    await db.execute("UPDATE problems SET is_public = true WHERE id = $1", public)
    running = await insert_problem(db, [("1\n", "1\n", True)])
    upcoming = await insert_problem(db, [("1\n", "1\n", True)])
    await make_contest(db, -60, 3600, [running])
    await make_contest(db, 3600, 7200, [upcoming], code="other")
    slug = {p: await db.fetchval("SELECT slug FROM problems WHERE id = $1", p) for p in (public, running, upcoming)}

    started = await tool(site, key, "submit_solution", problem_slug=slug[public], language="python", source="print(1)")
    job = await db.fetchrow("SELECT kind, owner_type, priority, contest_id FROM jobs WHERE id = $1", uuid.UUID(started["job_id"]))
    assert tuple(job) == ("submission", "client", 2, None)

    # An agent can't probe a live contest's hidden tests (DECISIONS.md, 28)
    assert (await tool(site, key, "submit_solution", problem_slug=slug[running], language="python", source="1"))[0] == "error"
    # A problem whose contest hasn't started answers exactly like one that doesn't exist
    early = await tool(site, key, "submit_solution", problem_slug=slug[upcoming], language="python", source="1")
    missing = await tool(site, key, "submit_solution", problem_slug="no-such", language="python", source="1")
    assert early == missing == ("error", "no such problem")
    assert await db.fetchval("SELECT count(*) FROM jobs") == 1


async def test_get_problem_shows_samples_only_and_hides_unstarted_problems(site, db, key):
    public = await insert_problem(db, [("1 2\n", "3\n", True), ("SECRET-IN\n", "SECRET-OUT\n", False)])
    await db.execute("UPDATE problems SET is_public = true WHERE id = $1", public)
    upcoming = await insert_problem(db, [("1\n", "1\n", True)])
    await make_contest(db, 3600, 7200, [upcoming])
    slug, early = [await db.fetchval("SELECT slug FROM problems WHERE id = $1", p) for p in (public, upcoming)]

    problem = await tool(site, key, "get_problem", slug=slug)
    assert problem["samples"] == [{"input": "1 2\n", "expected": "3\n"}]
    assert "SECRET" not in json.dumps(problem)
    assert await tool(site, key, "get_problem", slug=early) == await tool(site, key, "get_problem", slug="no-such")
    listed = await tool(site, key, "list_problems")
    assert [p["slug"] for p in listed["result"]] == [slug]


# --- One client, its own jobs


async def test_another_clients_job_looks_exactly_like_a_missing_one(site, db, key):
    other = (await create_client(db, "rival", 5))[1]
    mine = (await tool(site, key, "run_code", language="python", source="print(1)"))["job_id"]
    for name in ("get_result", "cancel_run"):
        theirs = await tool(site, other, name, job_id=mine)
        assert theirs == await tool(site, other, name, job_id=str(uuid.uuid4())) == ("error", "no such job")
        assert theirs == await tool(site, other, name, job_id="not-a-uuid")
    assert await db.fetchval("SELECT status FROM jobs WHERE id = $1", uuid.UUID(mine)) == "queued"


async def test_an_expired_job_is_gone(site, db, key):
    mine = (await tool(site, key, "run_code", language="python", source="print(1)"))["job_id"]
    await db.execute("UPDATE jobs SET expires_at = now() - interval '1 second'")
    assert await tool(site, key, "get_result", job_id=mine) == ("error", "no such job")


async def test_cancel_run_cancels_a_queued_job(site, db, key):
    mine = (await tool(site, key, "run_code", language="python", source="print(1)"))["job_id"]
    assert (await tool(site, key, "cancel_run", job_id=mine))["status"] == "cancelled"


@ERAS
async def test_an_idempotency_key_makes_a_retry_safe(site, db, key, era):
    call = {"language": "python", "source": "print(1)", "idempotency_key": "try-1"}
    first, retry = await tool(site, key, "run_code", era, **call), await tool(site, key, "run_code", era, **call)
    assert first["job_id"] == retry["job_id"] and await db.fetchval("SELECT count(*) FROM jobs") == 1
    kind, message = await tool(site, key, "run_code", era, **{**call, "source": "print(2)"})
    assert kind == "error" and "different request" in message


async def test_a_busy_client_is_told_when_to_retry(site, db):
    _, key = await create_client(db, "small", 1)
    await tool(site, key, "run_code", language="python", source="print(1)")
    assert await tool(site, key, "run_code", language="python", source="print(2)") == (
        "error", "refused for now (too many jobs in flight): try again in 5 seconds"
    )
