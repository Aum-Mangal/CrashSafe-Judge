"""Hostile programs, each judged in a real gVisor sandbox (ARCHITECTURE.md §13, Isolation).

Every attack must end in an ordinary verdict for the submission. A DockerError here would mean
the judge blamed itself and would retry, letting one submission burn all its attempts.
"""

import pytest

from helpers import insert_problem, judge_job

pytestmark = pytest.mark.sandbox


async def submit(db, pool, language, source):
    # Two tests, so an attack that breaks the sandbox would also break the second test
    problem = await insert_problem(db, [("", "ok\n", False), ("", "ok\n", False)])
    return await judge_job(db, pool, kind="submission", problem_id=problem, language=language, source=source)


async def run(db, pool, source):
    # run_code returns stdout and stderr, so the test can see what the program saw
    return await judge_job(db, pool, language="python", source=source)


@pytest.mark.parametrize(
    "language, source, allowed",
    [
        ("cpp", "#include <unistd.h>\nint main() { while (true) fork(); }", {"TLE", "RE"}),
        # RLIMIT_AS is per process, so 64 Python copies can together outgrow the sandbox: then MLE
        ("python", "import os\nwhile True:\n    os.fork()", {"TLE", "RE", "MLE"}),
        ("python", "import time\ntime.sleep(100)", {"TLE"}),
        ("python", "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)", {"OLE"}),
        ("cpp", '#include "/dev/zero"\nint main() {}', {"CE"}),
        ("cpp", "char big[100 << 20] = {1};\nint main() { return big[0] - 1; }", {"CE"}),
    ],
    ids=["C++ fork bomb", "Python fork bomb", "sleeps forever", "infinite output",
         "compiler reads /dev/zero", "100 MB binary"],
)
async def test_an_attack_gets_an_ordinary_verdict(db, pool, language, source, allowed):
    result = await submit(db, pool, language, source)
    assert result.verdict in allowed


async def test_the_network_is_unreachable(db, pool):
    result = await run(db, pool, "import socket\nsocket.create_connection(('1.1.1.1', 80), timeout=3)")
    assert result.verdict == "RE"
    assert "unreachable" in result.stderr.lower()


async def test_system_files_cannot_be_changed(db, pool):
    result = await run(db, pool, "open('/etc/passwd', 'a').write('owned')")
    assert result.verdict == "RE"
    assert "PermissionError" in result.stderr


async def test_the_host_is_invisible(db, pool):
    result = await run(db, pool, "import os\nprint(os.path.exists('/mnt/c'), sorted(os.environ))")
    assert result.stdout == "False ['HOME', 'LANG', 'PATH', 'TMPDIR']\n"


async def test_the_program_cannot_kill_the_runner(db, pool):
    # kill(-1) reaches every process the sandbox user may signal. Inside the sandbox that's nobody:
    # it fails with "no such process", because the runner is root and Linux spares PID 1.
    # If it reached the runner or the sandbox, the second test couldn't run and this wouldn't be AC.
    source = (
        "import os, signal\n"
        "try:\n    os.kill(-1, signal.SIGKILL)\n"
        "except ProcessLookupError:\n    pass\n"
        "print('ok')"
    )
    result = await submit(db, pool, "python", source)
    assert result.verdict == "AC"


async def test_files_from_one_test_are_gone_in_the_next(db, pool):
    # Each test prints ok only if it can't see the file the previous test left behind
    source = (
        "import os\n"
        "print('ok' if not os.path.exists('/work/left-behind') else 'leaked')\n"
        "os.makedirs('/work/left-behind/deeper')\n"
        "open('/work/left-behind/deeper/note', 'w').write('x')"
    )
    result = await submit(db, pool, "python", source)
    assert result.verdict == "AC"


async def test_work_is_the_only_writable_place(db, pool):
    # /work is emptied before every test. Anywhere else a program could write would let it
    # carry data from one test to the next, so there must be nowhere else (DECISIONS.md, 86).
    source = (
        "for place in ('/tmp', '/dev/shm', '/var/tmp', '/work'):\n"
        "    try:\n"
        "        open(place + '/note', 'w').write('x')\n"
        "        print(place, 'writable')\n"
        "    except OSError:\n"
        "        print(place, 'blocked')"
    )
    result = await run(db, pool, source)
    assert result.stdout == "/tmp blocked\n/dev/shm blocked\n/var/tmp blocked\n/work writable\n"


async def test_shared_memory_does_not_survive_between_tests(db, pool):
    # System V shared memory lives in the kernel, not in a folder, so emptying /work doesn't touch it.
    # Each test prints ok only if the segment the previous test made is gone.
    source = """#include <cstdio>
#include <sys/shm.h>
int main() {
    if (shmget(4242, 4096, 0600) >= 0) { puts("leaked"); return 0; }
    shmget(4242, 4096, IPC_CREAT | 0600);
    puts("ok");
}
"""
    result = await submit(db, pool, "cpp", source)
    assert result.verdict == "AC"


async def test_kill_minus_one_finds_no_process_to_signal(db, pool):
    result = await run(db, pool, "import os, signal\nos.kill(-1, signal.SIGKILL)")
    assert result.verdict == "RE"
    assert "ProcessLookupError" in result.stderr
