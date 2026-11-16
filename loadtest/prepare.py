"""Gets a database ready for the throughput test (loadtest/throughput.sh): imports the load-test
problem and creates contestant accounts, then writes one login token per account for k6.

The accounts go straight into the database, because registering through the API would hit our own
limit of 20 attempts per address per minute (DECISIONS.md, 102), and registration isn't what the
test measures. Nobody can log in to them: the password hash belongs to a random password that is
thrown away. Running it again reuses the same accounts.

    DATABASE_URL=... JWT_SECRET=... python -m loadtest.prepare --users 30 --out tokens.json
"""

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path

import asyncpg

from admin.commands import import_problem
from api.auth import hash_password, make_token

PROBLEM = Path(__file__).parent / "problem" / "loadtest-sum"
EMAIL = "loadtest-{:03d}@loadtest.invalid"  # .invalid can never be a real address


async def prepare(dsn: str, users: int) -> list[int]:
    conn = await asyncpg.connect(dsn)
    try:
        await import_problem(conn, PROBLEM)
        unusable = await hash_password(secrets.token_hex(32))
        await conn.executemany(
            """INSERT INTO users (email, password_hash, display_name) VALUES ($1, $2, $3)
               ON CONFLICT (email) DO NOTHING""",
            [(EMAIL.format(n), unusable, f"Load test {n}") for n in range(1, users + 1)],
        )
        rows = await conn.fetch(
            "SELECT id FROM users WHERE email = ANY($1::text[]) ORDER BY email",
            [EMAIL.format(n) for n in range(1, users + 1)],
        )
        return [r["id"] for r in rows]
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--users", type=int, default=30)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    ids = asyncio.run(prepare(os.environ["DATABASE_URL"], args.users))
    tokens = [make_token(user_id, os.environ["JWT_SECRET"]) for user_id in ids]
    # The tokens log these accounts in for 12 hours: only root should read them
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"problem": PROBLEM.name, "tokens": tokens}, f)
    print(f"{len(tokens)} accounts ready, problem {PROBLEM.name}")


if __name__ == "__main__":
    main()
