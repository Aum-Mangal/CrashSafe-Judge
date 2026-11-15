"""The contest pages: plain HTML rendered on the server, with no JavaScript (ROADMAP.md, week 3).

Same rules as REST, from the same code: core/problems.py decides what is visible and
core/submissions.py what may be submitted. Signed-in forms carry a CSRF token (api/auth.py).
A successful post answers with a 303 redirect to a page, so refreshing never posts twice.
"""

import secrets
from datetime import datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from pydantic import ValidationError

from api import accounts
from api.auth import clear_session, csrf_ok, csrf_token, page_user, set_session
from core import problems, submissions
from core.jobs import IdempotencyConflict, Rejected

router = APIRouter(default_response_class=HTMLResponse)
# Jinja2Templates escapes every value by default: a contestant's code or email is shown as text
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
# Statements are written by us, but raw HTML in them is still switched off
markdown = MarkdownIt("commonmark", {"html": False})

VERDICTS = {
    "AC": "Accepted",
    "WA": "Wrong answer",
    "TLE": "Time limit exceeded",
    "MLE": "Memory limit exceeded",
    "OLE": "Output limit exceeded",
    "RE": "Runtime error",
    "CE": "Compilation error",
    "IE": "Internal error: the judge failed, not your code",
    "OK": "Ran",
}
LANGUAGES = {"cpp": "C++20", "python": "Python 3"}


def _local(request: Request, moment: datetime) -> str:
    return moment.astimezone(request.app.state.display_tz).strftime("%d %b %Y, %H:%M %Z")


def _phase(contest) -> str:
    if contest["db_now"] < contest["starts_at"]:
        return "upcoming"
    return "running" if contest["db_now"] < contest["ends_at"] else "ended"


def _until(later: datetime, now: datetime) -> str:
    minutes = max(0, int((later - now).total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


async def _render(request: Request, name: str, status: int = 200, **context) -> HTMLResponse:
    user = context.pop("user", None) or await page_user(request)
    return templates.TemplateResponse(
        request, name, {"user": user, "csrf": csrf_token(request) if user else "", "local": lambda m: _local(request, m), **context},
        status_code=status,
    )


async def _message(request: Request, status: int, title: str, text: str = "") -> HTMLResponse:
    return await _render(request, "message.html", status, title=title, text=text)


def _see(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --- Home and accounts


@router.get("/")
async def home(request: Request):
    async with request.app.state.pool.acquire() as conn:
        found = await problems.contests(conn)
    return await _render(request, "home.html", contests=[{**dict(c), "phase": _phase(c)} for c in found])


@router.get("/login")
async def login_form(request: Request):
    return await _render(request, "login.html")


@router.post("/login")
async def login(request: Request, email: str = Form(""), password: str = Form("")):
    try:
        user_id = await accounts.login(request, accounts.Login(email=email, password=password))
    except ValidationError:
        return await _render(request, "login.html", 400, error="Check the email and password.", email=email)
    except HTTPException as e:
        return await _render(request, "login.html", e.status_code, error=e.detail.capitalize() + ".", email=email)
    response = _see("/")
    set_session(response, request, user_id)
    return response


@router.get("/register")
async def register_form(request: Request):
    return await _render(request, "register.html")


@router.post("/register")
async def register(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    invite_code: str = Form(""),
    display_name: str = Form(""),
):
    kept = {"email": email, "display_name": display_name}  # refilled if the form comes back
    try:
        body = accounts.Register(email=email, password=password, invite_code=invite_code, display_name=display_name)
    except ValidationError:
        error = "Use a valid email, a name of up to 40 characters and a password of 8 to 128 characters."
        return await _render(request, "register.html", 400, error=error, **kept)
    try:
        user_id = await accounts.register(request, body)
    except HTTPException as e:
        return await _render(request, "register.html", e.status_code, error=e.detail.capitalize() + ".", **kept)
    response = _see("/")
    set_session(response, request, user_id)
    return response


@router.post("/logout")
async def logout(request: Request, csrf: str = Form("")):
    if not csrf_ok(request, csrf):
        return await _message(request, 403, "This form has expired", "Go back, reload the page and try again.")
    response = _see("/")
    clear_session(response, request)
    return response


# --- Contests


@router.get("/c/{contest_id}")
async def contest_page(contest_id: int, request: Request):
    user = await page_user(request)
    async with request.app.state.pool.acquire() as conn:
        contest = await problems.contest(conn, contest_id)
        if contest is None:
            return await _message(request, 404, "Not found")
        listed = await problems.contest_problems(conn, contest_id)
        mine = await submissions.list_owned(conn, user.id, contest_id) if user else []
    return await _render(
        request, "contest.html", user=user, contest=contest, phase=_phase(contest), problems=listed,
        submissions=mine, verdicts=VERDICTS, starts_in=_until(contest["starts_at"], contest["db_now"]),
        ends_in=_until(contest["ends_at"], contest["db_now"]),
    )


@router.get("/c/{contest_id}/leaderboard")
async def leaderboard_page(contest_id: int, request: Request):
    async with request.app.state.pool.acquire() as conn:
        contest = await problems.contest(conn, contest_id)
    if contest is None:
        return await _message(request, 404, "Not found")
    found = await request.app.state.leaderboards.get(request.app.state.pool, contest_id)
    return await _render(request, "leaderboard.html", contest=contest, phase=_phase(contest), board=found)


@router.get("/c/{contest_id}/{label}")
async def problem_page(contest_id: int, label: str, request: Request):
    async with request.app.state.pool.acquire() as conn:
        contest = await problems.contest(conn, contest_id)
        problem = await problems.contest_problem(conn, contest_id, label) if contest else None
        if problem is None:
            # Before the start this is every problem: it must look like one that doesn't exist
            return await _message(request, 404, "Not found")
        samples = await problems.samples(conn, problem["id"])
    return await _render(
        request, "problem.html", contest=contest, phase=_phase(contest), problem=problem, samples=samples,
        statement=markdown.render(problem["statement_md"]), languages=LANGUAGES,
        # A fresh key per form: a double-click sends the same key twice and gets one job
        key=secrets.token_hex(16),
    )


@router.post("/c/{contest_id}/{label}")
async def submit(
    contest_id: int,
    label: str,
    request: Request,
    language: str = Form(""),
    source: str = Form(""),
    key: str = Form(""),
    csrf: str = Form(""),
):
    user = await page_user(request)
    if user is None:
        return _see("/login")
    if not csrf_ok(request, csrf):
        return await _message(request, 403, "This form has expired", "Go back, reload the page and try again.")
    if language not in LANGUAGES or not 1 <= len(source) <= 65536 or not 1 <= len(key) <= 100:
        return await _message(request, 400, "Check your submission", "Pick a language and paste up to 64 KB of code.")
    try:
        async with request.app.state.pool.acquire() as conn:
            problem = await problems.contest_problem(conn, contest_id, label)
            if problem is None:
                raise submissions.NotFound
            job_id, _ = await submissions.submit(conn, user.id, problem["slug"], language, source, contest_id, key)
    except submissions.NotFound:
        return await _message(request, 404, "Not found")
    except submissions.NotAllowed as e:
        return await _message(request, 403, "Not accepted", str(e).capitalize() + ".")
    except Rejected as e:
        text = f"Wait {e.retry_after} seconds and submit again. Your earlier submissions are still being judged."
        return await _message(request, 429, "Too many submissions", text)
    except IdempotencyConflict:
        return await _message(request, 409, "This form was already used", "Reload the problem page to submit again.")
    return _see(f"/s/{job_id}")


@router.get("/s/{job_id}")
async def submission_page(job_id: UUID, request: Request):
    user = await page_user(request)
    if user is None:
        return _see("/login")
    async with request.app.state.pool.acquire() as conn:
        found = await submissions.get_owned(conn, user.id, job_id)
    if found is None:
        return await _message(request, 404, "Not found")  # someone else's looks like a missing one
    return await _render(
        request, "submission.html", user=user, s=found, verdicts=VERDICTS, languages=LANGUAGES,
        # The page reloads itself every 2 s until the verdict is in: polling, with no script
        waiting=found["status"] in ("queued", "running"),
    )
