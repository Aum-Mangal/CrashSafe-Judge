"""Fair timing (ARCHITECTURE.md §13): the same programs, many times each, in gVisor (runsc) and in
plain runc, through the real judge-runner. Answers three questions with measured numbers:

1. How much does the CPU time of one program vary between runs? That decides how much spare room
   a time limit needs (check-problem asks for half).
2. What does gVisor cost? It intercepts every system call, so a program that makes many pays
   much more than one that only computes.
3. What do a new sandbox and one docker exec cost? That bounds throughput (spike question 10).

Run as root in the distro being measured, with Docker, runsc and the judge images:
    python -m loadtest.timing --runs 50 > timing.md
The programs are our own, so running them under runc, without gVisor, is safe.
"""

import argparse
import asyncio
import platform
import statistics
import subprocess
import time
import uuid

from worker.judge import COMPILE, CPP_FLAGS, PYTHON
from worker.sandbox import Limits, Sandbox

INSTANCE = "timing"  # its own label, so no worker's reconciliation removes these sandboxes
LIMITS = Limits(cpu_ms=10_000, memory_mb=512, wall_ms=30_000)
RUNTIMES = ("runsc", "runc")

CPU = """#include <cstdio>
int main() {  // about 0.3 s of arithmetic and no system calls
    unsigned long long x = 1;
    for (long i = 0; i < 300000000; i++) x = x * 6364136223846793005ULL + 1442695040888963407ULL;
    printf("%llu\\n", x);
}
"""
SYSCALLS = """#include <cstdio>
#include <fcntl.h>
#include <unistd.h>
int main() {  // 200 000 one-byte writes: almost nothing but system calls
    int fd = open("/dev/null", O_WRONLY);
    for (int i = 0; i < 200000; i++) write(fd, "x", 1);
    printf("done\\n");
}
"""
MEMORY = """#include <cstdio>
#include <cstdlib>
#include <cstring>
int main() {  // touches 256 MB, one page fault per 4 KB
    size_t n = 256u << 20;
    // volatile: otherwise the compiler sees only one byte is ever read and skips the rest
    volatile char *p = (volatile char *)malloc(n);
    for (size_t i = 0; i < n; i += 4096) p[i] = (char)(i >> 12);
    long sum = 0;
    for (size_t i = 0; i < n; i += 4096) sum += p[i];
    printf("%ld\\n", sum);
}
"""
PROGRAMS = {  # name: (language, source, what it measures)
    "cpu": ("cpp", CPU, "pure computation"),
    "syscalls": ("cpp", SYSCALLS, "200 000 tiny system calls"),
    "memory": ("cpp", MEMORY, "touching 256 MB"),
    "python": ("python", "print(sum(range(3_000_000)))\n", "Python start-up and a short loop"),
    "empty": ("cpp", "int main() {}\n", "the fixed cost of one run"),
}


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def summary(values: list[float]) -> str:
    """median, p95, min to max, and the spread as a share of the median."""
    median = statistics.median(values)
    spread = statistics.pstdev(values) / median if median else 0.0
    return f"{median:.0f} | {pct(values, 95):.0f} | {min(values):.0f} to {max(values):.0f} | {spread:.1%}"


async def compile_all() -> dict[str, bytes]:
    """Compiles each C++ program once, in gVisor, so both runtimes run the same binary."""
    box = await Sandbox.create("cpp", COMPILE.memory_mb, uuid.uuid4(), 0, INSTANCE)
    binaries = {}
    try:
        for name, (language, source, _) in PROGRAMS.items():
            if language != "cpp":
                continue
            await box.write_file("/work/main.cpp", source.encode())
            report = await box.run(["/usr/bin/g++", *CPP_FLAGS, "-o", "/work/a.out", "/work/main.cpp"],
                                   b"", COMPILE, address_space=False)
            if report.exit_code != 0:
                raise SystemExit(f"{name} didn't compile: {report.stderr}")
            binaries[name] = await box.read_file("/work/a.out")
    finally:
        await box.remove()
    return binaries


async def prepare(runtime: str, language: str, binary: bytes | None, source: str) -> tuple[Sandbox, list[str]]:
    box = await Sandbox.create(language, LIMITS.memory_mb, uuid.uuid4(), 0, INSTANCE, runtime=runtime)
    if language == "cpp":
        await box.write_file("/work/a.out", binary, executable=True)
        return box, ["/work/a.out"]
    await box.write_file("/work/main.py", source.encode())
    return box, [PYTHON, "-I", "-B", "/work/main.py"]


async def measure(runs: int) -> dict:
    binaries = await compile_all()
    results = {}
    for name, (language, source, _) in PROGRAMS.items():
        boxes = {rt: await prepare(rt, language, binaries.get(name), source) for rt in RUNTIMES}
        samples = {rt: {"cpu": [], "wall": [], "exec": []} for rt in RUNTIMES}
        try:
            # Alternate the runtimes, so a busy moment on the laptop hits both alike
            for _ in range(runs):
                for rt in RUNTIMES:
                    box, argv = boxes[rt]
                    started = time.monotonic()
                    report = await box.run(argv, b"", LIMITS, keep=[argv[-1].rsplit("/", 1)[1]])
                    elapsed = (time.monotonic() - started) * 1000
                    if report.exit_code != 0 or report.timed_out or report.sandbox_died:
                        raise SystemExit(f"{name} under {rt} failed: {report}")
                    samples[rt]["cpu"].append(report.cpu_ms)
                    samples[rt]["wall"].append(report.wall_ms)
                    samples[rt]["exec"].append(elapsed)
        finally:
            for box, _ in boxes.values():
                await box.remove()
        results[name] = samples

    starts = {rt: [] for rt in RUNTIMES}
    for _ in range(min(runs, 20)):
        for rt in RUNTIMES:
            started = time.monotonic()
            box = await Sandbox.create("cpp", LIMITS.memory_mb, uuid.uuid4(), 0, INSTANCE, runtime=rt)
            starts[rt].append((time.monotonic() - started) * 1000)
            await box.remove()
    return {"programs": results, "starts": starts}


def version(*argv: str) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=20).stdout.strip().splitlines()[0]
    except (OSError, IndexError, subprocess.TimeoutExpired):
        return "unknown"


def report(data: dict, runs: int) -> str:
    cpu_model = next((line.split(":", 1)[1].strip() for line in open("/proc/cpuinfo") if line.startswith("model name")), "?")
    lines = [
        "# Fair timing",
        "",
        f"Measured {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC on {platform.node()}: {cpu_model}, "
        f"kernel {platform.release()}, {version('docker', 'version', '--format', '{{.Server.Version}}')} docker, "
        f"{version('runsc', '--version')}. Each program ran {runs} times under each runtime, alternating, "
        "through judge-runner with the judge's own sandbox flags. Times are in ms.",
        "",
        "CPU and wall are what judge-runner measured for the program alone. Round trip is the whole "
        "`docker exec` as the worker sees it, runner included. Spread is the standard deviation as a "
        "share of the median.",
        "",
        "| Program | Runtime | CPU median | p95 | range | spread | Wall median | p95 | range | spread | Round trip median | p95 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, samples in data["programs"].items():
        for rt in RUNTIMES:
            s = samples[rt]
            lines.append(f"| {name}: {PROGRAMS[name][2]} | {rt} | {summary(s['cpu'])} | {summary(s['wall'])} | "
                         f"{statistics.median(s['exec']):.0f} | {pct(s['exec'], 95):.0f} |")
    lines += ["", "**gVisor's cost** (runsc median over runc median):", ""]
    for name, samples in data["programs"].items():
        ratios = []
        for kind in ("cpu", "wall", "exec"):
            base = statistics.median(samples["runc"][kind])
            ratios.append(f"{statistics.median(samples['runsc'][kind]) / base:.2f}x" if base else "n/a")
        lines.append(f"- {name}: CPU {ratios[0]}, wall {ratios[1]}, round trip {ratios[2]}")
    lines += ["", "**Starting a sandbox** (`docker run -d`, until Docker returns):", ""]
    for rt in RUNTIMES:
        values = data["starts"][rt]
        lines.append(f"- {rt}: median {statistics.median(values):.0f} ms, p95 {pct(values, 95):.0f} ms, {len(values)} starts")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=50)
    args = parser.parse_args()
    print(report(asyncio.run(measure(args.runs)), args.runs))


if __name__ == "__main__":
    main()
