"""The API process (ARCHITECTURE.md §1). It holds no Docker socket and never runs code.

Run it with:  JWT_SECRET=... uvicorn api.app:from_env --factory --port 8000
"""

import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI

from api.routes import router
from db.migrate import DEFAULT_DSN


def create_app(dsn: str, jwt_secret: str) -> FastAPI:
    # A short secret can be guessed offline from any one token, which forges every account
    if len(jwt_secret) < 32:
        raise ValueError("JWT_SECRET must be at least 32 characters")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
        try:
            yield
        finally:
            await app.state.pool.close()

    app = FastAPI(title="judge", lifespan=lifespan)
    app.state.jwt_secret = jwt_secret
    app.include_router(router)
    return app


def from_env() -> FastAPI:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise SystemExit("set JWT_SECRET, e.g. to the output of: openssl rand -hex 32")
    return create_app(os.environ.get("DATABASE_URL", DEFAULT_DSN), secret)
