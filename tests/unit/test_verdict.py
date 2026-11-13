"""Verdict rules and output checkers. Pure functions, so no Docker or database needed."""

import pytest

from worker.checker import check
from worker.sandbox import Limits, Report
from worker.verdict import test_verdict as verdict_for

LIMITS = Limits.for_time(1000, memory_mb=64)


def test_a_clean_run_with_the_right_output_is_accepted():
    assert verdict_for(Report(cpu_ms=100, peak_kb=1000), LIMITS, True) == "AC"


def test_the_wrong_output_is_a_wrong_answer():
    assert verdict_for(Report(), LIMITS, False) == "WA"


def test_run_code_with_nothing_to_compare_is_accepted():
    assert verdict_for(Report(), LIMITS, None) == "AC"


@pytest.mark.parametrize(
    "report",
    [Report(cpu_ms=1010), Report(timed_out=True, signal=9), Report(cpu_ms=2000, signal=9)],
    ids=["over the CPU limit", "wall clock ran out", "killed by RLIMIT_CPU"],
)
def test_too_slow_is_a_time_limit(report):
    assert verdict_for(report, LIMITS, None) == "TLE"


@pytest.mark.parametrize(
    "report",
    [
        Report(peak_kb=64 * 1024 + 1),
        Report(signal=6, stderr="terminate called after throwing an instance of 'std::bad_alloc'"),
        Report(exit_code=1, stderr="Traceback (most recent call last):\nMemoryError"),
        Report(sandbox_died=True, oom_killed=True),
    ],
    ids=["peak over the limit", "C++ bad_alloc", "Python MemoryError", "sandbox OOM-killed"],
)
def test_too_much_memory_is_a_memory_limit(report):
    assert verdict_for(report, LIMITS, None) == "MLE"


def test_output_flood_wins_over_time():
    assert verdict_for(Report(output_exceeded=True, timed_out=True), LIMITS, None) == "OLE"


@pytest.mark.parametrize(
    "report",
    [Report(exit_code=3), Report(signal=11), Report(sandbox_died=True)],
    ids=["non-zero exit", "segfault", "sandbox died without OOM"],
)
def test_a_crash_is_a_runtime_error(report):
    assert verdict_for(report, LIMITS, True) == "RE"


def test_token_checker_ignores_whitespace():
    assert check("token", "1  2\n3\n\n", "1 2 3")
    assert not check("token", "1 2", "1 2 3")


def test_exact_checker_keeps_lines_but_forgives_trailing_space():
    assert check("exact", "a  \nb\n\n", "a\nb")
    assert not check("exact", "a b", "a\nb")
