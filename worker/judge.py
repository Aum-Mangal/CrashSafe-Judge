"""Judges one claimed job: compile, run each test in a sandbox, decide verdicts (ARCHITECTURE.md §7).

Hidden test data never leaves this module. For a hidden test only the verdict, time and memory
are kept, never the program's output (DECISIONS.md, 22).
"""

from dataclasses import dataclass

import asyncpg

from core.queue import Claim, Result
from worker import checker
from worker.sandbox import Limits, Report, Sandbox
from worker.verdict import test_verdict

COMPILE = Limits(cpu_ms=10_000, memory_mb=512, wall_ms=10_000, output_kb=64)
RUN_CODE = Limits.for_time(10_000, memory_mb=256)
VERIFY = Limits.for_time(2_000, memory_mb=256)
VERIFY_BUDGET_MS = 30_000
MAX_BINARY_BYTES = 32 << 20
CPP_FLAGS = ["-std=c++20", "-O2", "-pipe", "-fmax-errors=10"]
PYTHON = "/usr/local/bin/python3"


@dataclass(frozen=True)
class Test:
    input: str
    expected: str | None  # None for run_code, which has nothing to compare against
    hidden: bool = False


class _Sandboxes:
    """The sandboxes of one job, so every one of them can be removed when the job ends."""

    def __init__(self, claim: Claim, instance: str):
        self.claim = claim
        self.instance = instance
        self.open: list[Sandbox] = []

    async def new(self, language: str, memory_mb: int) -> Sandbox:
        box = await Sandbox.create(language, memory_mb, self.claim.job_id, self.claim.token, self.instance)
        self.open.append(box)
        return box

    async def close(self, box: Sandbox) -> None:
        await box.remove()
        self.open.remove(box)

    async def close_all(self) -> None:
        for box in self.open:
            await box.remove()


async def judge(pool: asyncpg.Pool, claim: Claim, instance: str) -> Result:
    """Returns the job's result. Raises DockerError if the infrastructure failed.

    instance names this worker's database; its sandboxes carry it as a label (worker/sandbox.py).
    """
    limits, checker_kind, tests = await _plan(pool, claim)
    return await judge_tests(claim, limits, checker_kind, tests, instance)


async def judge_tests(claim: Claim, limits: Limits, checker_kind: str, tests: list[Test], instance: str) -> Result:
    """Judges the claim's code against tests it was given, with no database. The worker calls it
    from judge(); `python -m admin check-problem` calls it with a problem folder's tests."""
    boxes = _Sandboxes(claim, instance)
    try:
        prepared = await _prepare(claim, limits, boxes)
        if isinstance(prepared, Result):
            return prepared  # compile error
        box, argv, keep = prepared
        return await _run_tests(claim, box, argv, keep, limits, checker_kind, tests)
    finally:
        # Runs on success, failure and cancellation alike, so no sandbox outlives its job
        await boxes.close_all()


async def _plan(pool: asyncpg.Pool, claim: Claim) -> tuple[Limits, str, list[Test]]:
    if claim.kind == "run":
        return RUN_CODE, "token", [Test(claim.stdin or "", None)]
    if claim.kind == "verify":
        return VERIFY, "token", [Test(t["input"], t["expected"]) for t in claim.tests]

    async with pool.acquire() as conn:
        problem = await conn.fetchrow(
            "SELECT time_limit_ms, memory_limit_mb, output_limit_kb, checker FROM problems WHERE id = $1",
            claim.problem_id,
        )
        rows = await conn.fetch(
            "SELECT input_text, expected_text, is_sample FROM testcases WHERE problem_id = $1 ORDER BY ordinal",
            claim.problem_id,
        )
    limits = Limits.for_time(
        problem["time_limit_ms"], memory_mb=problem["memory_limit_mb"], output_kb=problem["output_limit_kb"]
    )
    tests = [Test(r["input_text"], r["expected_text"], hidden=not r["is_sample"]) for r in rows]
    return limits, problem["checker"], tests


async def _prepare(
    claim: Claim, limits: Limits, boxes: _Sandboxes
) -> tuple[Sandbox, list[str], list[str]] | Result:
    """Gets the program ready to run. Returns (sandbox, command, files to keep in /work), or a CE result."""
    if claim.language == "cpp":
        binary = await _compile_cpp(claim, boxes)
        if isinstance(binary, Result):
            return binary
        box = await boxes.new("cpp", limits.memory_mb)
        await box.write_file("/work/a.out", binary, executable=True)
        return box, ["/work/a.out"], ["a.out"]

    box = await boxes.new("python", limits.memory_mb)
    await box.write_file("/work/main.py", claim.source.encode())
    syntax = await box.run([PYTHON, "-m", "py_compile", "/work/main.py"], b"", COMPILE, address_space=False)
    if _failed(syntax):
        return Result("CE", compile_log=syntax.stderr or "Syntax check failed.")
    return box, [PYTHON, "-I", "-B", "/work/main.py"], ["main.py"]


async def _compile_cpp(claim: Claim, boxes: _Sandboxes) -> bytes | Result:
    """Compiles in its own sandbox, because the compiler needs more memory than the program may use."""
    box = await boxes.new("cpp", COMPILE.memory_mb)
    await box.write_file("/work/main.cpp", claim.source.encode())
    report = await box.run(
        ["/usr/bin/g++", *CPP_FLAGS, "-o", "/work/a.out", "/work/main.cpp"], b"", COMPILE, address_space=False
    )
    if report.sandbox_died:
        return Result("CE", compile_log="The compiler ran out of memory.")
    if report.timed_out:
        return Result("CE", compile_log="Compilation took longer than 10 seconds.")
    if _failed(report):
        return Result("CE", compile_log=report.stderr or "Compilation failed.")

    binary = await box.read_file("/work/a.out")
    await boxes.close(box)
    if len(binary) > MAX_BINARY_BYTES:
        return Result("CE", compile_log="The compiled program is larger than 32 MB.")
    return binary


async def _run_tests(
    claim: Claim,
    box: Sandbox | None,
    argv: list[str],
    keep: list[str],
    limits: Limits,
    checker_kind: str,
    tests: list[Test],
) -> Result:
    per_test: list[dict] = []
    verdict = failed_test = None
    shown: Report | None = None  # the run whose output may be stored; never a hidden test's
    cpu_used = 0

    for number, test in enumerate(tests, 1):
        over_budget = claim.kind == "verify" and cpu_used > VERIFY_BUDGET_MS
        if box is None or over_budget:
            per_test.append({"verdict": "skipped"})
            continue

        # Each test starts with only the program in /work (DECISIONS.md, 80)
        report = await box.run(argv, test.input.encode(), limits, keep=keep)
        cpu_used += report.cpu_ms
        output_ok = None
        if test.expected is not None:
            output_ok = checker.check(checker_kind, report.stdout.decode(errors="replace"), test.expected)
        result = test_verdict(report, limits, output_ok)
        per_test.append(
            {"verdict": result, "cpu_ms": report.cpu_ms, "wall_ms": report.wall_ms, "peak_kb": report.peak_kb}
        )
        if report.sandbox_died:
            box = None  # nothing more can run in it

        if claim.kind == "run" and not test.hidden:
            shown = report
        if result != "AC" and verdict is None:
            verdict, failed_test = result, number
            if not test.hidden:
                shown = report
        # A contest submission stops at its first failure; verify_code and check-problem run every test
        if result != "AC" and claim.kind == "submission":
            break

    if claim.kind == "run":
        verdict = verdict or "OK"
    return Result(
        verdict=verdict or "AC",
        per_test=per_test,
        peak_memory_kb=max((t.get("peak_kb", 0) for t in per_test), default=0),
        failed_test=failed_test,
        stdout=shown.stdout.decode(errors="replace") if shown else None,
        stderr=shown.stderr if shown else None,
    )


def _failed(report: Report) -> bool:
    return report.exit_code != 0 or report.signal != 0 or report.timed_out or report.sandbox_died
