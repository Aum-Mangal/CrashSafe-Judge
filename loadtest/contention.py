"""Where do workers slow each other down? The throughput test (DECISIONS.md, 138) found that a job
takes longer to judge when more workers run at once. This runs the real judging code, judge_tests,
for 1, 2, 3 and 4 jobs at a time, and measures two things:

1. Each Docker call, by kind: creating a sandbox, copying a file, compiling, running a test,
   removing a sandbox. Every call goes through worker.sandbox.docker(), so wrapping that one
   function times them all. A kind that slows down as jobs are added is where they contend.
2. Whether the load changes a program's measured CPU time, which decides TLE. A probe sandbox runs
   the timing benchmark's 0.3 s computation over and over beside the jobs, and keeps what
   judge-runner measured. At 0 jobs it runs alone: that is the baseline.
3. CPU, from /proc: all CPUs together, and per process name (dockerd, containerd, the shims,
   gVisor's sentry and gofer, the docker CLI). If the CPUs are nearly all busy, the jobs are
   sharing too little CPU. If they aren't, something is making them wait their turn.

Each job is the throughput test's: loadtest-sum's 5 tests, a correct answer, C++ and Python in turn.
Run as root in a distro with Docker, runsc and the judge images; no database is needed:
    python -m loadtest.contention --seconds 60 > contention.md
"""

import argparse
import asyncio
import collections
import contextvars
import os
import statistics
import time
import uuid
from pathlib import Path

from admin.commands import read_problem
from core.queue import Claim
from worker import sandbox
from loadtest.timing import CPU
from worker.judge import COMPILE, CPP_FLAGS, Test, judge_tests
from worker.sandbox import Limits, Sandbox

INSTANCE = "contention"  # its own label, so no worker's reconciliation removes these sandboxes
HERE = Path(__file__).parent
SOURCES = {language: (HERE / "solutions" / f"sum.{ext}").read_text() for language, ext in (("cpp", "cpp"), ("python", "py"))}
TICK = os.sysconf("SC_CLK_TCK")
PHASES = ["create", "copy file", "compile", "syntax check", "test run", "remove"]


# --- Timing every Docker call

samples: dict[str, list[float]] | None = None  # None outside the measured window
timed = contextvars.ContextVar("timed", default=True)  # the probe's own calls aren't the jobs'
_docker = sandbox.docker


def _phase(args: tuple[str, ...]) -> str:
    if args[0] == "run":
        return "create"
    if args[0] == "rm":
        return "remove"
    if args[0] == "exec" and sandbox.RUNNER in args:
        if "/usr/bin/g++" in args:
            return "compile"
        return "syntax check" if "py_compile" in args else "test run"
    if args[0] == "exec":
        return "copy file"
    return args[0]


async def _timed_docker(*args: str, stdin: bytes | None = None, timeout: float):
    started = time.monotonic()
    try:
        return await _docker(*args, stdin=stdin, timeout=timeout)
    finally:
        if samples is not None and timed.get():
            samples[_phase(args)].append((time.monotonic() - started) * 1000)


sandbox.docker = _timed_docker  # Sandbox's methods look the name up when they run


# --- CPU from /proc


def _busy_ticks() -> tuple[int, int]:
    """(busy, all) CPU ticks since boot, summed over every CPU."""
    fields = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    idle = fields[3] + fields[4]  # idle and iowait
    return sum(fields) - idle, sum(fields)


def _processes() -> dict[int, tuple[str, int, int]]:
    """pid: (name, parent pid, CPU ticks used by the process itself)."""
    found = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = open(f"/proc/{entry}/stat").read()
        except OSError:
            continue  # it exited while we looked
        name = raw[raw.index("(") + 1 : raw.rindex(")")]
        rest = raw[raw.rindex(")") + 2 :].split()
        found[int(entry)] = (name, int(rest[1]), int(rest[11]) + int(rest[12]))
    return found


class CpuWatch:
    """Adds up CPU per process name over a window by sampling /proc every 0.2 s.

    A process that starts and ends between two samples is missed, so the names don't add up to
    the total: the rest is reported as "not seen". The docker CLI is the exception. Each one is a
    child of this process, so its CPU is counted exactly, in our own cutime, when it's reaped."""

    def __init__(self):
        self.me = os.getpid()
        self.baseline: dict[int, int] = {}
        self.last: dict[int, tuple[str, int]] = {}

    def _scan(self) -> None:
        for pid, (name, parent, ticks) in _processes().items():
            if parent == self.me:
                continue  # a docker CLI, counted through cutime
            self.baseline.setdefault(pid, 0)  # started inside the window
            self.last[pid] = (name, ticks)

    def _own(self) -> tuple[int, int]:
        rest = open("/proc/self/stat").read().rsplit(")", 1)[1].split()
        return int(rest[11]) + int(rest[12]), int(rest[13]) + int(rest[14])

    async def watch(self, stop: asyncio.Event) -> dict[str, float]:
        """Returns CPU seconds per name, plus "all CPUs", until stop is set."""
        self.baseline = {pid: ticks for pid, (_, parent, ticks) in _processes().items() if parent != self.me}
        own_start, children_start = self._own()
        busy_start, _ = _busy_ticks()
        while not stop.is_set():
            self._scan()
            try:
                await asyncio.wait_for(stop.wait(), 0.2)
            except TimeoutError:
                pass
        self._scan()
        busy_end, _ = _busy_ticks()
        own_end, children_end = self._own()

        by_name: dict[str, float] = collections.Counter()
        for pid, (name, ticks) in self.last.items():
            if pid != self.me:
                by_name[name] += (ticks - self.baseline.get(pid, 0)) / TICK
        by_name["benchmark (python)"] = (own_end - own_start) / TICK
        by_name["docker CLI"] = (children_end - children_start) / TICK
        by_name["all CPUs"] = (busy_end - busy_start) / TICK
        return dict(by_name)


# --- The load


def tests() -> list[Test]:
    problem = read_problem(HERE / "problem" / "loadtest-sum")
    return [Test(t_in, t_out) for t_in, t_out in problem.samples]


async def slot(number: int, cases: list[Test], limits: Limits, until: float, done: list[tuple[str, float, float]]):
    """One worker's worth of load: judges jobs back to back until the time is up."""
    n = number
    while time.monotonic() < until:
        language = ("cpp", "python")[n % 2]
        n += 1
        claim = Claim(uuid.uuid4(), 0, 1, "submission", language, SOURCES[language], None, None, None)
        started = time.monotonic()
        result = await judge_tests(claim, limits, "token", cases, INSTANCE)
        if result.verdict != "AC":
            raise SystemExit(f"{language} got {result.verdict}: a correct answer must get AC")
        done.append((language, started, time.monotonic()))


async def probe(window_start: float, window_end: float, runs: list[tuple[int, int]]) -> None:
    """Runs a 0.3 s computation back to back in its own sandbox, keeping (CPU, wall) in ms as
    judge-runner measured them, for runs that started inside the window."""
    timed.set(False)
    box = await Sandbox.create("cpp", COMPILE.memory_mb, uuid.uuid4(), 0, INSTANCE)
    try:
        await box.write_file("/work/main.cpp", CPU.encode())
        built = await box.run(["/usr/bin/g++", *CPP_FLAGS, "-o", "/work/a.out", "/work/main.cpp"],
                              b"", COMPILE, address_space=False)
        if built.exit_code != 0:
            raise SystemExit(f"the probe didn't compile: {built.stderr}")
        while time.monotonic() < window_end:
            started = time.monotonic()
            report = await box.run(["/work/a.out"], b"", Limits.for_time(5000, 256), keep=["a.out"])
            if report.exit_code != 0 or report.timed_out:
                raise SystemExit(f"the probe failed: {report}")
            if started >= window_start:
                runs.append((report.cpu_ms, report.wall_ms))
    finally:
        await box.remove()


async def measure(concurrency: int, seconds: float, warmup: float) -> dict:
    global samples
    cases = tests()
    limits = Limits.for_time(1000, memory_mb=256)
    loop_start = time.monotonic()
    window_start, window_end = loop_start + warmup, loop_start + warmup + seconds
    done: list[tuple[str, float, float]] = []
    probed: list[tuple[int, int]] = []
    slots = [asyncio.create_task(slot(i, cases, limits, window_end, done)) for i in range(concurrency)]
    slots.append(asyncio.create_task(probe(window_start, window_end, probed)))

    await asyncio.sleep(warmup)
    samples = collections.defaultdict(list)
    stop = asyncio.Event()
    cpu = asyncio.create_task(CpuWatch().watch(stop))
    await asyncio.sleep(window_end - time.monotonic())
    stop.set()
    measured, samples = samples, None
    cpu_seconds = await cpu
    await asyncio.gather(*slots)  # the jobs still running finish, outside the window

    finished = [(lang, end - start) for lang, start, end in done if window_start <= end < window_end]
    return {"jobs": finished, "phases": measured, "cpu": cpu_seconds, "seconds": seconds, "probe": probed}


# --- The report


def median(values: list[float]) -> str:
    return f"{statistics.median(values):.0f}" if values else "-"


def report(runs: dict[int, dict], seconds: float, warmup: float) -> str:
    cpus = os.cpu_count()
    lines = [
        "# Where workers contend",
        "",
        f"Measured {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC on {os.uname().nodename}, {cpus} CPUs. "
        f"N jobs judged at once, back to back, plus one probe (below), through `judge_tests` (the worker's own code, without the database), "
        f"for {seconds:.0f} s after {warmup:.0f} s of warm-up. Each job is loadtest-sum's 5 tests, C++ and Python in turn. "
        "Times are medians in ms, for the whole `docker` command as the worker sees it.",
        "",
        "| At once | Jobs/s | C++ job | Python job | " + " | ".join(PHASES) + " |",
        "|---" * (4 + len(PHASES)) + "|",
    ]
    for n, run in runs.items():
        jobs = run["jobs"]
        cells = [
            str(n), f"{len(jobs) / run['seconds']:.2f}",
            median([t * 1000 for lang, t in jobs if lang == "cpp"]),
            median([t * 1000 for lang, t in jobs if lang == "python"]),
        ]
        cells += [median(run["phases"].get(p, [])) for p in PHASES]
        lines.append("| " + " | ".join(cells) + " |")

    base = runs.get(0, next(iter(runs.values())))["probe"]
    base_cpu = statistics.median([c for c, _ in base])
    lines += [
        "",
        "**The probe**: a 0.3 s computation (`loadtest/timing.py`'s `cpu`), run back to back in its own sandbox "
        "beside the N jobs. CPU and wall are what judge-runner measured, in ms. Against alone is the CPU median "
        "over the median with no jobs running. This is the number a time limit is compared with.",
        "",
        "| At once | Runs | CPU median | p95 | max | Wall median | Against alone |",
        "|---|---|---|---|---|---|---|",
    ]
    for n, run in runs.items():
        cpu_ms = sorted(c for c, _ in run["probe"])
        wall_ms = [w for _, w in run["probe"]]
        p95 = cpu_ms[min(len(cpu_ms) - 1, round(0.95 * (len(cpu_ms) - 1)))]
        lines.append(f"| {n} | {len(cpu_ms)} | {statistics.median(cpu_ms):.0f} | {p95} | {cpu_ms[-1]} | "
                     f"{statistics.median(wall_ms):.0f} | {statistics.median(cpu_ms) / base_cpu:.2f}x |")

    names = collections.Counter()
    for run in runs.values():
        for name, secs in run["cpu"].items():
            if name != "all CPUs":
                names[name] += secs
    top = [name for name, _ in names.most_common(8)]
    lines += [
        "",
        f"**CPU in use**, as CPUs kept busy on average (out of {cpus}). \"Not seen\" is CPU used by processes "
        "that started and ended between two 0.2 s samples.",
        "",
        "| At once | All | " + " | ".join(top) + " | Not seen |",
        "|---" * (3 + len(top)) + "|",
    ]
    for n, run in runs.items():
        cpu, secs = run["cpu"], run["seconds"]
        seen = sum(cpu.get(name, 0) for name in cpu if name != "all CPUs")
        cells = [str(n), f"{cpu['all CPUs'] / secs:.2f}"] + [f"{cpu.get(name, 0) / secs:.2f}" for name in top]
        cells.append(f"{max(0.0, cpu['all CPUs'] - seen) / secs:.2f}")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--warmup", type=float, default=10)
    parser.add_argument("--at-once", default="0,1,2,3,4,6")
    args = parser.parse_args()
    runs = {}
    for n in (int(x) for x in args.at_once.split(",")):
        runs[n] = asyncio.run(measure(n, args.seconds, args.warmup))
    print(report(runs, args.seconds, args.warmup))


if __name__ == "__main__":
    main()
