"""Gate 1: kill a worker with SIGKILL in the middle of a job. Another worker must recover the job
and record exactly one correct verdict, and no sandbox may be left behind.

The roadmap's gate names C++. Python is kept too: the two take different paths (C++ compiles
in a separate sandbox first), so the kill lands at a different point in each."""

import asyncio

import pytest

from helpers import eventually, insert_job, insert_problem, sandboxes_for, start_worker

pytestmark = pytest.mark.sandbox

# Sleeping uses no CPU, so these stay under the 2 s CPU limit while giving the kill time to land
SLOW_SOLUTIONS = {
    "cpp": (
        "#include <chrono>\n#include <iostream>\n#include <thread>\n"
        "int main() {\n"
        "    std::this_thread::sleep_for(std::chrono::seconds(3));\n"
        "    std::cout << \"done\\n\";\n"
        "}\n"
    ),
    "python": "import time\ntime.sleep(3)\nprint('done')",
}


@pytest.mark.parametrize("language", ["cpp", "python"])
async def test_a_killed_workers_job_recovers_to_one_verdict(dsn, db, tmp_path, language):
    problem = await insert_problem(db, [("", "done\n", False)], time_limit_ms=2000)
    job = await insert_job(
        db, kind="submission", problem_id=problem, priority=0, language=language,
        source=SLOW_SOLUTIONS[language],
    )

    async def status():
        return await db.fetchval("SELECT status FROM jobs WHERE id = $1", job)

    async def judging_in_a_sandbox():
        return await status() == "running" and len(await sandboxes_for(job)) > 0

    async def completed():
        return await status() == "completed"

    async def no_sandboxes_left():
        return await sandboxes_for(job) == []

    first = await start_worker(dsn, "worker-a", tmp_path)
    try:
        await eventually(judging_in_a_sandbox, 60, "worker A to start judging", tmp_path)
        await asyncio.sleep(1)  # the program is now sleeping inside the sandbox
    finally:
        first.kill()  # SIGKILL: no cleanup, no goodbye
        await first.wait()

    second = await start_worker(dsn, "worker-b", tmp_path)
    try:
        await eventually(completed, 90, "worker B to finish the job", tmp_path)
        await eventually(no_sandboxes_left, 30, "every sandbox for the job to be removed", tmp_path)
    finally:
        second.kill()
        await second.wait()

    row = await db.fetchrow("SELECT attempts, worker_id FROM jobs WHERE id = $1", job)
    assert (row["attempts"], row["worker_id"]) == (2, "worker-b")
    verdicts = await db.fetch("SELECT verdict FROM results WHERE job_id = $1", job)
    assert [r["verdict"] for r in verdicts] == ["AC"]
