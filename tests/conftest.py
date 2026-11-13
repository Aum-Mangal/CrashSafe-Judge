"""Shared fixtures. Every test gets its own fresh database with all migrations applied."""

import os
import uuid

import asyncpg
import pytest

from db.migrate import DEFAULT_DSN, migrate

ADMIN_DSN = os.environ.get("DATABASE_URL", DEFAULT_DSN)


@pytest.fixture
async def dsn():
    name = f"test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(ADMIN_DSN)
    await admin.execute(f'CREATE DATABASE "{name}"')
    test_dsn = ADMIN_DSN.rsplit("/", 1)[0] + f"/{name}"
    await migrate(test_dsn)
    try:
        yield test_dsn
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


@pytest.fixture
async def db(dsn):
    conn = await asyncpg.connect(dsn)
    yield conn
    await conn.close()


@pytest.fixture
async def pool(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()
