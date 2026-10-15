"""Server-rendered web interface (FastAPI + Jinja2). Intended for 127.0.0.1 only."""

from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..collector import LOCK_NAME, run_collection
from ..config import Settings
from ..db import Database, init_db
from ..digest import DigestBusy, DigestUnavailable, build_digest
from ..extraction import Extractor
from ..reporting import fmt_local
from ..updates import UpdatesBusy, build_updates_report
from ..repo import InvalidAccount, add_account, find_account, lock_held, list_accounts, remove_account, set_paused
from ..session_store import session_exists

log = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(settings: Settings, *, start_scheduler: bool | None = None) -> FastAPI:
    db = init_db(settings.database_file)
    use_scheduler = settings.scheduler_enabled if start_scheduler is None else start_scheduler
    state: dict = {"scheduler": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if use_scheduler:
            from ..scheduler import create_scheduler

            sched = create_scheduler(db, settings)
            sched.start()
            state["scheduler"] = sched
        yield
        if state["scheduler"]:
            state["scheduler"].shutdown(wait=False)

    app = FastAPI(title="Instagram digest", lifespan=lifespan)
    TEMPLATES.env.filters["local"] = lambda v: fmt_local(v, settings.tz) if v else "—"

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        # Block cross-site form posts to this local app (simple CSRF protection).
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("Cross-origin request blocked", status_code=403)
        return await call_next(request)

    def ctx(request: Request, **extra) -> dict:
        jobs = []
        if state["scheduler"]:
            for job in state["scheduler"].get_jobs():
                jobs.append({"name": job.name, "next": job.next_run_time})
        return {
            "request": request,
            "settings": settings,
            "mode": settings.provider,
            "fixture": settings.is_fixture_mode,
            "session_saved": session_exists(settings.session_file),
            "digest_available": settings.digest_available,
            "check_running": lock_held(db, LOCK_NAME),
            "jobs": jobs,
            "msg": request.query_params.get("msg"),
            **extra,
        }

    def back(path: str, msg: str) -> RedirectResponse:
        return RedirectResponse(f"{path}?msg={quote(msg)}", status_code=303)

    def run_in_background(fn, *args, **kwargs) -> None:
        def target():
            try:
                fn(*args, **kwargs)
            except Exception:
                log.exception("Background task failed")

        threading.Thread(target=target, daemon=True).start()

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with db.read() as conn:
            digests = conn.execute(
                "SELECT id, kind, status, created_at, item_count, is_fixture FROM digests ORDER BY id DESC LIMIT 5"
            ).fetchall()
            updates = conn.execute(
                "SELECT id, kind, status, created_at, item_count, is_fixture FROM update_reports ORDER BY id DESC LIMIT 5"
            ).fetchall()
            runs = conn.execute(
                "SELECT cr.*, a.username FROM check_runs cr JOIN accounts a ON a.id = cr.account_id "
                "ORDER BY cr.id DESC LIMIT 15"
            ).fetchall()
        return TEMPLATES.TemplateResponse(request, "index.html", ctx(
            request, accounts=list_accounts(db), digests=digests, updates=updates, runs=runs,
            capabilities=Extractor(settings).capabilities(),
        ))

    @app.post("/accounts")
    def add(value: str = Form(...)):
        added, failed = [], []
        for part in value.replace(",", "\n").split():
            try:
                row, created = add_account(db, part)
                added.append(row["username"] if created else f"{row['username']} (already monitored)")
            except InvalidAccount as e:
                failed.append(str(e))
        return back("/", "; ".join([f"Added: {', '.join(added)}"] * bool(added) + failed) or "Nothing added")

    @app.post("/accounts/{account_id}/pause")
    def pause(account_id: int):
        set_paused(db, account_id, True)
        return back("/", "Paused")

    @app.post("/accounts/{account_id}/resume")
    def resume(account_id: int):
        set_paused(db, account_id, False)
        return back("/", "Resumed")

    @app.post("/accounts/{account_id}/delete")
    def delete(account_id: int):
        remove_account(db, account_id)
        return back("/", "Removed account and its collected items")

    @app.post("/accounts/{account_id}/check")
    def check_one(account_id: int):
        with db.read() as conn:
            row = find_account(conn, account_id)
        if not row:
            raise HTTPException(404)
        if lock_held(db, LOCK_NAME):
            return back("/", "A check is already running")
        run_in_background(run_collection, db, settings, trigger="manual", usernames=[row["username"]])
        return back("/", f"Checking @{row['username']} in the background; refresh to see results")

    @app.post("/check")
    def check_all():
        if lock_held(db, LOCK_NAME):
            return back("/", "A check is already running")
        run_in_background(run_collection, db, settings, trigger="manual")
        return back("/", "Checking all active accounts in the background; refresh to see results")

    # --- feature 1: updates (no LLM) -------------------------------------------
    @app.post("/updates")
    def make_updates(kind: str = Form("preview")):
        if kind not in ("preview", "scheduled"):
            raise HTTPException(400)
        try:
            report_id = build_updates_report(db, settings, kind=kind, trigger="manual")
        except UpdatesBusy as e:
            return back("/", str(e))
        return RedirectResponse(f"/updates/{report_id}", status_code=303)

    @app.get("/updates", response_class=HTMLResponse)
    def updates_list(request: Request):
        with db.read() as conn:
            rows = conn.execute(
                "SELECT id, kind, trigger, status, created_at, window_start, window_end, item_count, is_fixture "
                "FROM update_reports ORDER BY id DESC LIMIT 200"
            ).fetchall()
        return TEMPLATES.TemplateResponse(request, "updates.html", ctx(request, reports=rows))

    @app.get("/updates/{report_id}", response_class=HTMLResponse)
    def updates_detail(request: Request, report_id: int):
        with db.read() as conn:
            row = conn.execute("SELECT * FROM update_reports WHERE id = ?", (report_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        return TEMPLATES.TemplateResponse(request, "update.html", ctx(request, r=row, c=json.loads(row["content_json"])))

    @app.get("/updates/{report_id}/markdown", response_class=PlainTextResponse)
    def updates_md(report_id: int):
        with db.read() as conn:
            row = conn.execute("SELECT content_md FROM update_reports WHERE id = ?", (report_id,)).fetchone()
        if not row or not row["content_md"]:
            raise HTTPException(404)
        return PlainTextResponse(row["content_md"], media_type="text/markdown; charset=utf-8")

    # --- feature 2: LLM digest ---------------------------------------------------
    @app.post("/digests")
    def make_digest(kind: str = Form("preview")):
        if kind not in ("preview", "scheduled"):
            raise HTTPException(400)
        try:
            digest_id = build_digest(db, settings, kind=kind, trigger="manual")
        except (DigestBusy, DigestUnavailable) as e:
            return back("/", str(e))
        return RedirectResponse(f"/digests/{digest_id}", status_code=303)

    @app.get("/accounts/{account_id}", response_class=HTMLResponse)
    def account_detail(request: Request, account_id: int):
        with db.read() as conn:
            acct = find_account(conn, account_id)
            if not acct:
                raise HTTPException(404)
            items = conn.execute(
                "SELECT * FROM items WHERE account_id = ? ORDER BY COALESCE(published_at, collected_at) DESC LIMIT 100",
                (account_id,),
            ).fetchall()
            runs = conn.execute(
                "SELECT * FROM check_runs WHERE account_id = ? ORDER BY id DESC LIMIT 30", (account_id,)
            ).fetchall()
        return TEMPLATES.TemplateResponse(request, "account.html", ctx(request, acct=acct, items=items, runs=runs))

    @app.get("/digests", response_class=HTMLResponse)
    def digests(request: Request):
        with db.read() as conn:
            rows = conn.execute(
                "SELECT id, kind, trigger, status, created_at, window_start, window_end, item_count, "
                "summarizer, is_fixture, error FROM digests ORDER BY id DESC LIMIT 200"
            ).fetchall()
        return TEMPLATES.TemplateResponse(request, "digests.html", ctx(request, digests=rows))

    @app.get("/digests/{digest_id}", response_class=HTMLResponse)
    def digest_detail(request: Request, digest_id: int):
        with db.read() as conn:
            row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        content = json.loads(row["content_json"]) if row["content_json"] else None
        return TEMPLATES.TemplateResponse(request, "digest.html", ctx(request, d=row, c=content))

    @app.get("/digests/{digest_id}/markdown", response_class=PlainTextResponse)
    def digest_md(digest_id: int):
        with db.read() as conn:
            row = conn.execute("SELECT content_md FROM digests WHERE id = ?", (digest_id,)).fetchone()
        if not row or not row["content_md"]:
            raise HTTPException(404)
        return PlainTextResponse(row["content_md"], media_type="text/markdown; charset=utf-8")

    app.state.db = db
    return app
