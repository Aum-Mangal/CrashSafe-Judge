"""python -m admin check-problem: a problem setter's solution judged against a folder, in real
gVisor sandboxes, with no database and no worker."""

import asyncio
import os
import sys

import pytest

from admin import commands
from core.queue import Result
from helpers import ROOT

pytestmark = pytest.mark.sandbox

GOOD = '#include <cstdio>\nint main() { long long a, b; scanf("%lld %lld", &a, &b); printf("%lld\\n", a + b); }\n'
OVERFLOW = '#include <cstdio>\nint main() { int a, b; scanf("%d %d", &a, &b); printf("%d\\n", a + b); }\n'


def sum_folder(root):
    """One sample; the overflow test comes first among the hidden ones, so a test runs after it."""
    folder = root / "sum"
    for part in ("samples", "hidden"):
        (folder / part).mkdir(parents=True)
    (folder / "problem.toml").write_text('title = "Sum"\ntime_limit_ms = 1000\nmemory_limit_mb = 256\n')
    (folder / "statement.md").write_text("Add two numbers.")
    tests = {"samples/1": ("1 2", "3"), "hidden/1": ("2000000000 2000000000", "4000000000"), "hidden/2": ("-5 5", "0")}
    for name, (test_in, test_out) in tests.items():
        (folder / f"{name}.in").write_text(test_in + "\n")
        (folder / f"{name}.out").write_text(test_out + "\n")
    return folder


def solution(root, name, source):
    path = root / name
    path.write_text(source)
    return path


async def leftover_sandboxes():
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-aq", "--filter", f"label=judge.instance={commands.CHECK_INSTANCE}",
        stdout=asyncio.subprocess.PIPE,
    )
    return (await proc.communicate())[0].split()


async def test_a_correct_solution_passes_every_test_with_time_to_spare(tmp_path):
    found = await commands.check_problem(sum_folder(tmp_path), solution(tmp_path, "good.cpp", GOOD))
    assert found.result.verdict == "AC"
    assert [t["verdict"] for t in found.result.per_test] == ["AC", "AC", "AC"]
    assert not found.too_slow
    assert await leftover_sandboxes() == []


async def test_every_test_runs_even_after_a_failure(tmp_path):
    found = await commands.check_problem(sum_folder(tmp_path), solution(tmp_path, "int.cpp", OVERFLOW))
    assert (found.result.verdict, found.result.failed_test) == ("WA", 2)
    assert [t["verdict"] for t in found.result.per_test] == ["AC", "WA", "AC"]  # a submission would stop at 2
    report = commands.describe(found, tmp_path / "int.cpp")
    assert "   2  hidden  WA" in report and "   3  hidden  AC" in report


async def test_python_solutions_are_checked_too(tmp_path):
    source = "a, b = map(int, input().split())\nprint(a + b)\n"
    found = await commands.check_problem(sum_folder(tmp_path), solution(tmp_path, "sum.py", source))
    assert found.result.verdict == "AC"  # Python's integers don't overflow


async def test_a_compile_error_shows_the_compiler_log(tmp_path):
    found = await commands.check_problem(sum_folder(tmp_path), solution(tmp_path, "broken.cpp", "int main( {\n"))
    assert found.result.verdict == "CE"
    assert "error:" in commands.describe(found, tmp_path / "broken.cpp")


@pytest.mark.parametrize("problem_toml, message", [
    ('title = "Sum"\nmemory_limit_mb = 256\n', "missing time_limit_ms"),
    ("title = \n", "can't read"),
])
async def test_a_broken_folder_is_refused_before_anything_runs(tmp_path, problem_toml, message):
    folder = sum_folder(tmp_path)
    (folder / "problem.toml").write_text(problem_toml)
    with pytest.raises(commands.AdminError, match=message):
        await commands.check_problem(folder, solution(tmp_path, "good.cpp", GOOD))


async def test_an_unknown_language_is_refused(tmp_path):
    with pytest.raises(commands.AdminError, match="must end in"):
        await commands.check_problem(sum_folder(tmp_path), solution(tmp_path, "sum.java", "class A {}"))


def test_an_accepted_run_that_uses_over_half_the_limit_is_too_slow(tmp_path):
    """Timing in a sandbox varies, so this rule is checked on made-up numbers."""
    folder = commands.read_problem(sum_folder(tmp_path))

    def check(verdict, cpu_ms):
        result = Result(verdict, per_test=[{"verdict": verdict, "cpu_ms": cpu_ms, "wall_ms": cpu_ms, "peak_kb": 1024}])
        return commands.Check(folder, result, cpu_ms, 1000)

    assert not check("AC", 500).too_slow and check("AC", 510).too_slow
    assert "warning" in commands.describe(check("AC", 510), tmp_path / "a.cpp")
    assert not check("TLE", 2000).too_slow  # a TLE is already a clear answer


async def test_the_exit_code_says_whether_the_solution_did_what_was_expected(tmp_path):
    folder, good = sum_folder(tmp_path), solution(tmp_path, "good.cpp", GOOD)

    async def cli(*args):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "admin", "check-problem", *map(str, args), cwd=ROOT, env=os.environ,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode() + err.decode()

    code, out = await cli(folder, good)
    assert code == 0 and out.startswith("sum with good.cpp: AC\n") and out.endswith("as expected\n")
    code, out = await cli(folder, good, "--expect", "tle")
    assert code == 1 and "NOT as expected (wanted TLE)" in out
    code, out = await cli(folder, tmp_path / "missing.py")
    assert code == 1 and out.startswith("error: can't read")
