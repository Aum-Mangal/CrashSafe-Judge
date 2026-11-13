"""End-to-end judging inside real gVisor sandboxes. Needs Docker, runsc and the judge images."""

import pytest

from core import queue
from helpers import insert_job, insert_problem
from worker.judge import judge

pytestmark = pytest.mark.sandbox

SUM_CPP = """#include <bits/stdc++.h>
int main() { long long a, b; std::cin >> a >> b; std::cout << a + b << "\\n"; }
"""
MEMORY_BOMB_CPP = """#include <bits/stdc++.h>
int main() {
    std::vector<char*> blocks;
    while (true) { char* p = new char[16 << 20]; memset(p, 1, 16 << 20); blocks.push_back(p); }
}
"""


async def judge_job(db, pool, **job):
    job_id = await insert_job(db, **job)
    claimed = await queue.claim(db, "test-worker")
    assert claimed.job_id == job_id
    return await judge(pool, claimed)


async def sum_problem(db):
    # One sample test, which contestants can see, and one hidden test
    return await insert_problem(db, [("1 2\n", "3\n", True), ("40 2\n", "42\n", False)])


async def test_run_code_returns_the_output(db, pool):
    result = await judge_job(db, pool, language="python", source="print(input()[::-1])", stdin="olleh\n")
    assert result.verdict == "OK"
    assert result.stdout == "hello\n"


async def test_a_correct_cpp_submission_is_accepted(db, pool):
    problem = await sum_problem(db)
    result = await judge_job(db, pool, kind="submission", problem_id=problem, language="cpp", source=SUM_CPP)
    assert result.verdict == "AC"
    assert [t["verdict"] for t in result.per_test] == ["AC", "AC"]


async def test_failing_a_hidden_test_stores_no_output(db, pool):
    problem = await sum_problem(db)
    passes_only_the_sample = "input()\nprint(3)"
    result = await judge_job(
        db, pool, kind="submission", problem_id=problem, language="python", source=passes_only_the_sample
    )
    assert (result.verdict, result.failed_test) == ("WA", 2)
    assert result.stdout is None and result.stderr is None


async def test_a_cpp_compile_error_returns_the_compiler_message(db, pool):
    problem = await sum_problem(db)
    result = await judge_job(
        db, pool, kind="submission", problem_id=problem, language="cpp", source="int main() { return x; }"
    )
    assert result.verdict == "CE"
    # g++ quotes names with UTF-8 curly quotes, which must survive the trip through the runner
    assert "‘x’ was not declared" in result.compile_log


async def test_a_python_syntax_error_is_a_compile_error(db, pool):
    problem = await sum_problem(db)
    result = await judge_job(db, pool, kind="submission", problem_id=problem, language="python", source="print(")
    assert result.verdict == "CE"
    assert "SyntaxError" in result.compile_log


async def test_an_infinite_loop_is_a_time_limit(db, pool):
    problem = await insert_problem(db, [("", "", False)], time_limit_ms=1000)
    result = await judge_job(
        db, pool, kind="submission", problem_id=problem, language="python", source="while True:\n    pass"
    )
    assert result.verdict == "TLE"


async def test_a_cpp_memory_bomb_is_a_memory_limit(db, pool):
    problem = await insert_problem(db, [("", "", False)], memory_limit_mb=64)
    result = await judge_job(
        db, pool, kind="submission", problem_id=problem, language="cpp", source=MEMORY_BOMB_CPP
    )
    assert result.verdict == "MLE"


async def test_a_python_memory_error_is_a_memory_limit(db, pool):
    problem = await insert_problem(db, [("", "", False)], memory_limit_mb=64)
    result = await judge_job(
        db, pool, kind="submission", problem_id=problem, language="python", source="x = bytearray(1 << 30)"
    )
    assert result.verdict == "MLE"


async def test_an_exception_is_a_runtime_error(db, pool):
    result = await judge_job(db, pool, language="python", source="print(1 // 0)")
    assert result.verdict == "RE"
    assert "ZeroDivisionError" in result.stderr


async def test_verify_runs_every_test_and_shows_the_first_failure(db, pool):
    tests = [
        {"input": "1 2\n", "expected": "3\n"},
        {"input": "2 2\n", "expected": "5\n"},
        {"input": "5 5\n", "expected": "10\n"},
    ]
    result = await judge_job(
        db, pool, kind="verify", language="python", tests=tests,
        source="a, b = map(int, input().split())\nprint(a + b)",
    )
    assert (result.verdict, result.failed_test) == ("WA", 2)
    assert [t["verdict"] for t in result.per_test] == ["AC", "WA", "AC"]
    assert result.stdout == "4\n"
