"""Turns a judge-runner report into a verdict for one test (ARCHITECTURE.md §7).

The rules are checked in order, and the first match wins: a program that floods its output
and also runs too long gets OLE.
"""

from worker.sandbox import Limits, Report

# What a failed allocation prints: C++'s uncaught std::bad_alloc and Python's MemoryError
_OUT_OF_MEMORY = ("std::bad_alloc", "MemoryError")


def test_verdict(report: Report, limits: Limits, output_ok: bool | None) -> str:
    """output_ok is None when there's no expected output to compare, as for run_code."""
    if report.sandbox_died:
        return "MLE" if report.oom_killed else "RE"
    if report.output_exceeded:
        return "OLE"
    if report.timed_out or report.cpu_ms > limits.cpu_ms:
        return "TLE"
    if report.peak_kb > limits.memory_mb * 1024:
        return "MLE"
    failed = report.exit_code != 0 or report.signal != 0
    if failed and any(marker in report.stderr for marker in _OUT_OF_MEMORY):
        return "MLE"
    if failed:
        return "RE"
    if output_ok is False:
        return "WA"
    return "AC"
