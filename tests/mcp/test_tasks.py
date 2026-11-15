"""The Tasks extension (api/tasks.py), against its specification. No worker runs; the tests move
jobs along themselves, as a worker would."""

import time
import uuid
from datetime import datetime

import pytest

from admin.commands import create_client
from core import queue
from core.queue import Result
from helpers import LEGACY, MODERN, TASKS, finish_next, open_mcp, rpc

RUN = {"name": "run_code", "arguments": {"language": "python", "source": "print(1)"}}


@pytest.fixture
async def site(dsn):
    async with open_mcp(dsn) as client:
        yield client


@pytest.fixture
async def key(db):
    return (await create_client(db, "agent", 5))[1]


async def result_of(client, key, method, params=None, **options):
    reply = await rpc(client, key, method, params, **options)
    assert "result" in reply.json(), reply.text
    return reply.json()["result"]


async def error_of(client, key, method, params=None, **options):
    reply = await rpc(client, key, method, params, **options)
    assert "error" in reply.json(), reply.text
    return reply.json()["error"]


async def new_task(client, key, call=RUN):
    task = await result_of(client, key, "tools/call", call, tasks=True)
    assert task["resultType"] == "task", task
    return task


def get(task_id):
    return {"taskId": task_id}


# --- Negotiation


async def test_server_discover_advertises_the_extension(site, key):
    result = await result_of(site, key, "server/discover")
    assert TASKS in result["capabilities"]["extensions"]


async def test_a_request_that_declares_tasks_gets_one_at_once(dsn, db, key):
    # The tool would wait 10 s for a verdict; as a task it must not wait at all
    async with open_mcp(dsn, mcp_wait_seconds=10) as site:
        started = time.monotonic()
        task = await new_task(site, key)
        assert time.monotonic() - started < 2
    job = await db.fetchrow("SELECT id, created_at, expires_at FROM jobs")
    assert task["taskId"] == str(job["id"])  # the task is the job, committed before this reply
    assert task["status"] == "working" and task["pollIntervalMs"] == 1000
    assert task["ttlMs"] == 24 * 3600 * 1000
    assert datetime.fromisoformat(task["createdAt"]) == job["created_at"]
    assert "result" not in task and "structuredContent" not in task


@pytest.mark.parametrize("era", [MODERN, LEGACY])
async def test_a_request_that_doesnt_declare_tasks_never_gets_one(site, db, key, era):
    result = await result_of(site, key, "tools/call", RUN, era=era)
    assert result.get("resultType", "complete") == "complete"
    assert result["structuredContent"]["status"] == "queued"


async def test_read_only_tools_answer_at_once_even_when_tasks_are_declared(site, key):
    result = await result_of(site, key, "tools/call", {"name": "list_problems", "arguments": {}}, tasks=True)
    assert result["resultType"] == "complete" and result["structuredContent"] == {"result": []}


async def test_a_refused_call_is_a_tool_error_not_a_task(site, db, key):
    tests = [{"input": "", "expected": ""}] * 21
    result = await result_of(site, key, "tools/call", {"name": "verify_code", "arguments": {
        "language": "python", "source": "print()", "tests": tests}}, tasks=True)
    assert result["isError"] is True and result["resultType"] == "complete"
    assert await db.fetchval("SELECT count(*) FROM jobs") == 0


async def test_a_retry_with_the_same_key_gets_the_same_task(site, db, key):
    call = {"name": "run_code", "arguments": {**RUN["arguments"], "idempotency_key": "k1"}}
    first, again = await new_task(site, key, call), await new_task(site, key, call)
    assert first["taskId"] == again["taskId"] and await db.fetchval("SELECT count(*) FROM jobs") == 1


# --- tasks/get follows the job


async def test_tasks_get_follows_the_job_to_its_result(site, db, key):
    task_id = (await new_task(site, key))["taskId"]
    queued = await result_of(site, key, "tasks/get", get(task_id), tasks=True)
    assert (queued["resultType"], queued["status"], queued["statusMessage"]) == ("complete", "working", "Waiting for a worker.")

    claimed = await queue.claim(db, "w")
    running = await result_of(site, key, "tasks/get", get(task_id), tasks=True)
    assert (running["status"], running["statusMessage"]) == ("working", "Being judged.")

    # A wrong answer is a successful judging: completed, with the tool's own result
    await queue.finish(db, claimed.job_id, "w", claimed.token, Result("RE", stderr="boom", failed_test=1))
    done = await result_of(site, key, "tasks/get", get(task_id), tasks=True)
    assert done["status"] == "completed" and "statusMessage" not in done
    assert done["result"]["isError"] is False
    assert (done["result"]["structuredContent"]["verdict"], done["result"]["structuredContent"]["stderr"]) == ("RE", "boom")
    assert done["lastUpdatedAt"] >= done["createdAt"]


async def test_a_job_the_judge_gave_up_on_is_a_failed_task_with_an_error(site, db, key):
    task_id = (await new_task(site, key))["taskId"]
    await db.execute("UPDATE jobs SET status = 'dead' WHERE id = $1", uuid.UUID(task_id))
    failed = await result_of(site, key, "tasks/get", get(task_id), tasks=True)
    assert failed["status"] == "failed" and failed["error"]["code"] == -32603
    assert "not your code's fault" in failed["statusMessage"] and "result" not in failed


# --- tasks/cancel and tasks/update


async def test_tasks_cancel_acknowledges_and_cancels_a_queued_job(site, db, key):
    task_id = (await new_task(site, key))["taskId"]
    acknowledged = await result_of(site, key, "tasks/cancel", get(task_id), tasks=True)
    # An empty acknowledgement: the SDK adds only serverInfo in _meta to every 2026-07-28 result
    assert acknowledged["resultType"] == "complete" and set(acknowledged) <= {"resultType", "_meta"}
    assert (await result_of(site, key, "tasks/get", get(task_id), tasks=True))["status"] == "cancelled"


async def test_tasks_cancel_asks_the_worker_to_stop_a_running_job(site, db, key):
    task_id = (await new_task(site, key))["taskId"]
    await queue.claim(db, "w")
    acknowledged = await result_of(site, key, "tasks/cancel", get(task_id), tasks=True)
    assert set(acknowledged) <= {"resultType", "_meta"}
    job = await db.fetchrow("SELECT status, cancel_requested FROM jobs")
    assert (job["status"], job["cancel_requested"]) == ("running", True)  # eventually consistent


async def test_tasks_update_acknowledges_a_known_task(site, key):
    task_id = (await new_task(site, key))["taskId"]
    acknowledged = await result_of(site, key, "tasks/update", {"taskId": task_id, "inputResponses": {"x": {}}}, tasks=True)
    assert set(acknowledged) <= {"resultType", "_meta"} and acknowledged["resultType"] == "complete"


# --- What a caller can't learn


@pytest.mark.parametrize("method", ["tasks/get", "tasks/update", "tasks/cancel"])
async def test_unknown_expired_and_other_clients_tasks_answer_alike(site, db, key, method):
    other = (await create_client(db, "rival", 5))[1]
    mine = (await new_task(site, key))["taskId"]
    old = (await new_task(site, key, {"name": "run_code", "arguments": {"language": "python", "source": "print(2)"}}))["taskId"]
    await db.execute("UPDATE jobs SET expires_at = now() - interval '1 second' WHERE id = $1", uuid.UUID(old))

    answers = [
        await error_of(site, other, method, get(mine), tasks=True),  # someone else's
        await error_of(site, key, method, get(old), tasks=True),  # expired
        await error_of(site, key, method, get(str(uuid.uuid4())), tasks=True),  # never existed
        await error_of(site, key, method, get("not-a-uuid"), tasks=True),
    ]
    assert all(a == {"code": -32602, "message": "no such task"} for a in answers), answers
    assert await db.fetchval("SELECT status FROM jobs WHERE id = $1", uuid.UUID(mine)) == "queued"


@pytest.mark.parametrize("method", ["tasks/get", "tasks/update", "tasks/cancel"])
async def test_tasks_methods_need_the_declaration_on_that_request(site, key, method):
    task_id = (await new_task(site, key))["taskId"]
    # Checked before the task is looked up, so a missing declaration reveals nothing either
    for asked in (task_id, str(uuid.uuid4())):
        error = await error_of(site, key, method, get(asked))
        assert error["code"] == -32021
        assert error["data"] == {"requiredCapabilities": {"extensions": {TASKS: {}}}}


async def test_tasks_methods_dont_exist_in_the_2025_era(site, key):
    task_id = (await new_task(site, key))["taskId"]
    assert (await error_of(site, key, "tasks/get", get(task_id), era=LEGACY))["code"] == -32601


async def test_a_finished_task_reads_the_same_from_a_fresh_server(dsn, db, key):
    """The jobs table is the only state: a task outlives the process that created it."""
    async with open_mcp(dsn) as first:
        task_id = (await new_task(first, key))["taskId"]
    await finish_next(db, Result("OK", stdout="1\n"))
    async with open_mcp(dsn) as restarted:
        done = await result_of(restarted, key, "tasks/get", get(task_id), tasks=True)
    assert done["status"] == "completed" and done["result"]["structuredContent"]["stdout"] == "1\n"
