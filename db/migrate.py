"""Applies the SQL files in db/migrations in name order, each exactly once.

Each file runs in one transaction together with its row in schema_migrations, so a
migration either applies completely or not at all.

Usage: python -m db.migrate   (reads DATABASE_URL)
"""

import asyncio
import os
from pathlib import Path

import asyncpg

MIGRATIONS = Path(__file__).parent / "migrations"
DEFAULT_DSN = "postgresql://judge:judge@127.0.0.1:55432/judge"


async def migrate(dsn: str) -> list[str]:
    """Applies pending migrations and returns the names of the ones it applied."""
    conn = await asyncpg.connect(dsn)
    try:
        # Only one migrator at a time, even if two start together. Released when the connection closes.
        await conn.execute("SELECT pg_advisory_lock(hashtext('schema_migrations'))")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")}

        done = []
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in applied:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            done.append(path.name)
        return done
    finally:
        await conn.close()


if __name__ == "__main__":
    names = asyncio.run(migrate(os.environ.get("DATABASE_URL", DEFAULT_DSN)))
    print("\n".join(f"applied {name}" for name in names) or "nothing to apply")
