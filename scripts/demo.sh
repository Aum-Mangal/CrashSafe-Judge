#!/usr/bin/env bash
# A local demo of the contest site with a real worker: a fresh database judge_demo holding one
# running contest, then the worker and the API on http://localhost:8000. Run as root in the dev
# distro. Ctrl+C stops both. For rehearsing Gate 3, never for production.
#
# Demo account (test values, this database only): demo@example.com / demo-password-1
# Invite code for registering more: demo-invite
# An MCP key for this database is printed at the start: use it as Authorization: Bearer <key>
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV=/root/.venvs/judge
export PYTHONDONTWRITEBYTECODE=1
ADMIN_DSN=postgresql://judge:judge@127.0.0.1:55432/judge
export DATABASE_URL=postgresql://judge:judge@127.0.0.1:55432/judge_demo

docker compose --progress quiet -f "$ROOT/deploy/compose.dev.yml" up -d --wait
bash "$ROOT/scripts/build-images.sh"
cd "$ROOT"

"$VENV/bin/python" - <<'EOF'
import asyncio, os
import asyncpg
from api.auth import hash_password
from db.migrate import migrate

async def main():
    admin = await asyncpg.connect("postgresql://judge:judge@127.0.0.1:55432/judge")
    await admin.execute('DROP DATABASE IF EXISTS judge_demo WITH (FORCE)')
    await admin.execute('CREATE DATABASE judge_demo')
    await admin.close()
    await migrate(os.environ["DATABASE_URL"])
    db = await asyncpg.connect(os.environ["DATABASE_URL"])
    add = """INSERT INTO problems (slug, title, statement_md, time_limit_ms, memory_limit_mb)
             VALUES ($1, $2, $3, 1000, 256) RETURNING id"""
    a = await db.fetchval(add, "sum", "Sum of two numbers",
        "Read two integers *a* and *b* and print **a + b**.\n\nBoth are between -2·10^9 and 2·10^9, so the sum may not fit in 32 bits.")
    b = await db.fetchval(add, "reverse", "Reverse a line",
        "Read one line of text and print it backwards.")
    tests = [(a, 1, "1 2\n", "3\n", True), (a, 2, "-5 5\n", "0\n", False), (a, 3, "2000000000 2000000000\n", "4000000000\n", False),
             (b, 1, "hello\n", "olleh\n", True), (b, 2, "abc def\n", "fed cba\n", False)]
    await db.executemany("INSERT INTO testcases (problem_id, ordinal, input_text, expected_text, is_sample) VALUES ($1, $2, $3, $4, $5)", tests)
    contest = await db.fetchval("""INSERT INTO contests (title, invite_code, starts_at, ends_at)
        VALUES ('Mock contest', 'demo-invite', now() - interval '40 minutes', now() + interval '2 hours') RETURNING id""")
    await db.executemany("INSERT INTO contest_problems VALUES ($1, $2, $3)", [(contest, a, "A"), (contest, b, "B")])
    await db.execute("INSERT INTO users (email, password_hash, display_name) VALUES ('demo@example.com', $1, 'Demo')", await hash_password("demo-password-1"))
    # A few made-up contestants with results already in, so the leaderboard has something to rank
    history = {"Asha": [(a, 3, "WA"), (a, 9, "AC"), (b, 20, "AC")], "Ravi": [(a, 12, "AC"), (b, 15, "CE"), (b, 18, "TLE")],
               "Meera": [(b, 6, "AC"), (a, 30, "WA"), (a, 31, "WA")]}
    for name, attempts in history.items():
        user = await db.fetchval("INSERT INTO users (email, password_hash, display_name) VALUES ($1, 'x', $2) RETURNING id",
                                 f"{name.lower()}@demo.invalid", name)
        for problem, minute, verdict in attempts:
            job = await db.fetchval("""INSERT INTO jobs (kind, owner_type, owner_id, request_hash, language, source, problem_id,
                    contest_id, priority, status, submitted_at)
                SELECT 'submission', 'user', $1, '\\x00', 'python', 'print()', $2, $3, 0, 'completed', starts_at + make_interval(mins => $4)
                FROM contests WHERE id = $3 RETURNING id""", user, problem, contest, minute)
            await db.execute("INSERT INTO results (job_id, verdict) VALUES ($1, $2)", job, verdict)
    await db.close()

asyncio.run(main())
EOF

KEY=$("$VENV/bin/python" -m admin create-client --name demo-agent | sed -n 's/.*: \(jk_.*\)$/\1/p')

WORKER_ID=demo-worker "$VENV/bin/python" -m worker.main &
worker=$!
trap 'kill $worker 2> /dev/null' EXIT
echo "Open http://localhost:8000"
echo "MCP: http://localhost:8000/mcp with Authorization: Bearer $KEY"
JWT_SECRET=$(openssl rand -hex 32) COOKIE_SECURE=0 "$VENV/bin/uvicorn" api.app:from_env --factory --host 127.0.0.1 --port 8000
