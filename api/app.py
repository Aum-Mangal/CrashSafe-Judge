"""The API process (ARCHITECTURE.md §1). It holds no Docker socket and never runs code.
It serves the REST routes (api/routes.py), the contest pages (api/pages.py) and MCP (api/mcp.py).

Run it with:  JWT_SECRET=... uvicorn api.app:from_env --factory --port 8000
"""

import asyncio
import os
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import FastAPI, Request

from api import mcp
from api.leaderboard import BoardCache
from api.pages import router as pages_router
from api.ratelimit import LoginLimits
from api.routes import router
from api.waiter import JobWaiter
from db.migrate import DEFAULT_DSN

# Sent with every response. The pages use no scripts at all, so the browser is told to run none:
# even if some text slipped past escaping, an injected <script> would not execute.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; "
    "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


async def _run_mcp(server, started: asyncio.Event, stopping: asyncio.Event) -> None:
    """Runs the SDK's session manager until stopping is set. It holds an anyio task group, which
    must be entered and left by the same task; whoever drives the app's startup and shutdown may
    use two different ones (pytest does), so the manager gets a task of its own."""
    async with server.session_manager.run():
        started.set()
        await stopping.wait()


def create_app(
    dsn: str,
    jwt_secret: str,
    *,
    client_ip_header: str | None = None,
    login_limits: LoginLimits = LoginLimits(),
    cookie_secure: bool = True,
    display_tz: str = "Asia/Kolkata",
    leaderboard_seconds: float = 5,
    mcp_wait_seconds: float = 20,
    mcp_recheck_seconds: float = 2,
) -> FastAPI:
    """client_ip_header names a header holding the caller's real address, set by a proxy in front
    of the API. Only name one if nothing can reach the API except through that proxy: anyone
    else could write any address into it.

    cookie_secure=False lets the session cookie travel over plain HTTP, for local testing only.
    display_tz is the time zone the pages show times in. leaderboard_seconds is how long a computed
    leaderboard is reused; 0 turns the cache off. An MCP tool waits mcp_wait_seconds for its
    job, re-reading it every mcp_recheck_seconds in case a notification was lost."""
    # A short secret can be guessed offline from any one token, which forges every account
    if len(jwt_secret) < 32:
        raise ValueError("JWT_SECRET must be at least 32 characters")

    mcp_server, mcp_route = mcp.build()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
        app.state.job_waiter = JobWaiter(dsn, mcp_recheck_seconds)
        mcp_running = asyncio.create_task(_run_mcp(mcp_server, started := asyncio.Event(), stopping := asyncio.Event()))
        try:
            await app.state.job_waiter.start()
            await started.wait()
            yield
        finally:
            stopping.set()
            await mcp_running
            await app.state.job_waiter.stop()
            await app.state.pool.close()

    app = FastAPI(title="judge", lifespan=lifespan)
    app.state.jwt_secret = jwt_secret
    app.state.client_ip_header = client_ip_header
    app.state.login_limits = login_limits
    app.state.cookie_secure = cookie_secure
    app.state.display_tz = ZoneInfo(display_tz)
    app.state.leaderboards = BoardCache(leaderboard_seconds)
    app.state.mcp_wait_seconds = mcp_wait_seconds
    app.include_router(router)
    app.include_router(pages_router)
    app.router.routes.append(mcp_route)  # POST /mcp, behind its API key check

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    return app


def from_env() -> FastAPI:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise SystemExit("set JWT_SECRET, e.g. to the output of: openssl rand -hex 32")
    # In production the API is reached only through the Cloudflare tunnel: CLIENT_IP_HEADER=CF-Connecting-IP
    return create_app(
        os.environ.get("DATABASE_URL", DEFAULT_DSN),
        secret,
        client_ip_header=os.environ.get("CLIENT_IP_HEADER"),
        cookie_secure=os.environ.get("COOKIE_SECURE", "1") != "0",
        display_tz=os.environ.get("DISPLAY_TZ", "Asia/Kolkata"),
    )
