"""Command-line interface: `igdigest <command>` (or `python -m igdigest <command>`)."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .collector import RunSummary, run_collection
from .config import Settings, get_settings
from .db import init_db
from .reporting import fmt_local
from .repo import InvalidAccount, add_account, list_accounts, remove_account, set_paused
from .updates import UpdatesBusy, build_updates_report


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs at INFO; keep it quiet so tokens can never reach the logs.
    for noisy in ("httpx", "httpcore", "apscheduler.executors", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _print_summary(summary: RunSummary) -> None:
    print(f"Collection: {summary.status} — {summary.message}")
    for r in summary.results:
        tag = " (initial recap)" if r.is_initial and r.status == "ok" else ""
        line = f"  @{r.username}: {r.status}{tag}, {r.new_count} new / {r.fetched_count} fetched via {r.provider}"
        if r.attempts > 1:
            line += f", {r.attempts} attempts"
        if r.error:
            line += f" — {r.error}"
        print(line)
        for n in r.notes:
            print(f"      note: {n}")


def _print_digest(db, digest_id: int) -> None:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    if row["status"] != "ok":
        print(f"Digest #{digest_id} FAILED: {row['error']} (posts were not marked as delivered)")
        return
    print(row["content_md"])


def _print_updates(db, report_id: int | None) -> None:
    if report_id is None:
        print("Nothing to report (no new posts, no failed checks).")
        return
    with db.read() as conn:
        row = conn.execute("SELECT content_md FROM update_reports WHERE id = ?", (report_id,)).fetchone()
    print(row["content_md"])


# --- commands -------------------------------------------------------------------

def cmd_init_db(args, s: Settings) -> int:
    db = init_db(s.database_file)
    with db.read() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    print(f"Database ready at {s.database_file} (schema version {version})")
    return 0


def cmd_add(args, s: Settings) -> int:
    db = init_db(s.database_file)
    code = 0
    for value in args.accounts:
        try:
            row, created = add_account(db, value)
            print(f"{'Added' if created else 'Already monitored'}: @{row['username']}")
        except InvalidAccount as e:
            print(f"Error: {e}", file=sys.stderr)
            code = 1
    return code


def cmd_remove(args, s: Settings) -> int:
    ok = remove_account(init_db(s.database_file), args.account)
    print("Removed" if ok else "No such account")
    return 0 if ok else 1


def cmd_pause(args, s: Settings, paused: bool) -> int:
    ok = set_paused(init_db(s.database_file), args.account, paused)
    print(("Paused" if paused else "Resumed") if ok else "No such account")
    return 0 if ok else 1


def cmd_list(args, s: Settings) -> int:
    rows = list_accounts(init_db(s.database_file))
    if not rows:
        print("No accounts. Add one with: igdigest add <username or profile URL>")
        return 0
    print(f"{'account':<28} {'state':<16} {'last success':<17} {'items':>5}  last error")
    for r in rows:
        state = ("paused/" if r["paused"] else "") + r["status"]
        last = fmt_local(r["last_success_at"], s.tz) if r["last_success_at"] else "never"
        print(f"@{r['username']:<27} {state:<16} {last:<17} {r['item_count']:>5}  {r['last_error'] or ''}")
    return 0


def cmd_login(args, s: Settings) -> int:
    from .providers.browser import interactive_login

    print(f"Session will be saved to {s.session_file} (owner-only permissions, git-ignored).")
    ok = interactive_login(s.session_file, s.browser_proxy, s.login_timeout_minutes)
    if ok:
        print("Login detected; session saved and the browser closed.")
        return 0
    print("Login was not completed (window closed or timed out). Nothing was saved.")
    return 1


def cmd_collect(args, s: Settings) -> int:
    db = init_db(s.database_file)
    summary = run_collection(db, s, trigger="manual", usernames=args.account or None)
    _print_summary(summary)
    return 0 if summary.status in ("ok", "no_accounts") else 2


def cmd_updates(args, s: Settings) -> int:
    db = init_db(s.database_file)
    try:
        report_id = build_updates_report(db, s, kind="preview" if args.preview else "scheduled", trigger="manual")
    except UpdatesBusy as e:
        print(str(e), file=sys.stderr)
        return 1
    _print_updates(db, report_id)
    return 0


def cmd_digest(args, s: Settings) -> int:
    from .digest import DigestBusy, DigestUnavailable, build_digest

    db = init_db(s.database_file)
    try:
        digest_id = build_digest(db, s, kind="preview" if args.preview else "scheduled", trigger="manual")
    except (DigestBusy, DigestUnavailable) as e:
        print(str(e), file=sys.stderr)
        return 1
    _print_digest(db, digest_id)
    return 0


def cmd_serve(args, s: Settings) -> int:
    import uvicorn

    from .web.app import create_app

    app = create_app(s, start_scheduler=not args.no_scheduler and s.scheduler_enabled)
    uvicorn.run(app, host=args.host or s.web_host, port=args.port or s.web_port, log_level="info")
    return 0


def cmd_demo(args, s: Settings) -> int:
    """End-to-end fixture run in an isolated database. Needs no credentials."""
    demo_dir = s.data_dir / "demo"
    if demo_dir.exists():
        shutil.rmtree(demo_dir)
    demo_dir.mkdir(parents=True)
    fixture = demo_dir / "fixture.json"
    shutil.copyfile(s.fixture_path, fixture)
    ds = s.model_copy(update={
        "provider": "fixture", "fixture_path": fixture, "db_path": demo_dir / "demo.db",
        "retry_base_seconds": 0.2,  # keep the simulated transient failures quick
    })
    db = init_db(ds.database_file)

    def step(title: str) -> None:
        print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)

    step("1. Add the fixture accounts")
    for username in json.loads(fixture.read_text(encoding="utf-8"))["accounts"]:
        row, _ = add_account(db, username)
        print(f"  monitoring @{row['username']}")

    step("2. First check: initial recap (at most 5 latest posts per account)")
    _print_summary(run_collection(db, ds, trigger="manual"))

    step("3. Updates report (no LLM): new posts with links; marks them delivered for updates")
    _print_updates(db, build_updates_report(db, ds, kind="scheduled"))

    step("4. Second check: nothing new; the flaky account still fails")
    _print_summary(run_collection(db, ds, trigger="manual"))

    step("5. Updates report: 'no new posts' is kept separate from failed checks")
    _print_updates(db, build_updates_report(db, ds, kind="scheduled"))

    step("6. A new post appears in the fixture; third check collects only that post")
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["accounts"]["demo_city_cycling"]["posts"].append({
        "id": "cyc-new-1", "shortcode": "CYCNEW1", "media_type": "image",
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caption": "Route update: the river path reopens Saturday 08:00 after resurfacing. "
                   "Detour signs come down Friday evening.",
        "media": [{"kind": "image", "text": "RIVER PATH REOPENS SAT 08:00"}],
    })
    fixture.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(run_collection(db, ds, trigger="manual"))

    step("7. Updates report: only the post not yet delivered")
    _print_updates(db, build_updates_report(db, ds, kind="scheduled"))

    step("8. Digest (LLM recap) - a separate, optional feature with its own delivery tracking")
    if ds.digest_available:
        from .digest import DigestUnavailable, build_digest

        try:
            # Covers all 12 posts: digests track delivery independently of updates reports.
            _print_digest(db, build_digest(db, ds, kind="preview"))
        except DigestUnavailable as e:
            print(f"  skipped: {e}")
    else:
        print("  skipped: no LLM configured (IGD_LLM_PROVIDER=none). Everything above ran without it.")

    with db.read() as conn:
        items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        dups = conn.execute(
            "SELECT COUNT(*) FROM (SELECT account_id, post_key FROM items GROUP BY 1, 2 HAVING COUNT(*) > 1)"
        ).fetchone()[0]
    step("Done")
    print(f"  {items} items stored, {dups} duplicates. Demo database: {ds.database_file}")
    print("  Browse it with:")
    print(f'    $env:IGD_DB_PATH="{ds.database_file}"; $env:IGD_FIXTURE_PATH="{fixture}"; igdigest serve --no-scheduler')
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="igdigest", description="Personal Instagram monitoring and digest app")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create or migrate the database")
    a = sub.add_parser("add", help="monitor accounts (username, @username or profile URL)")
    a.add_argument("accounts", nargs="+")
    for name, help_text in (("remove", "stop monitoring and delete collected items"),
                            ("pause", "pause checks for an account"), ("resume", "resume checks")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("account")
    sub.add_parser("list", help="list monitored accounts and their status")
    sub.add_parser("login", help="interactive Instagram login in a visible browser; saves the session locally")
    c = sub.add_parser("collect", help="run one collection now")
    c.add_argument("--account", action="append", help="only this account (repeatable)")
    u = sub.add_parser("updates", help="list new posts with links, no LLM (marks them delivered unless --preview)")
    u.add_argument("--preview", action="store_true", help="do not mark posts as delivered")
    d = sub.add_parser("digest", help="LLM recap of new posts; needs IGD_LLM_PROVIDER (marks delivered unless --preview)")
    d.add_argument("--preview", action="store_true", help="do not mark posts as delivered")
    sv = sub.add_parser("serve", help="run the web app (and the scheduler)")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sv.add_argument("--no-scheduler", action="store_true")
    sub.add_parser("demo", help="end-to-end fixture demonstration in data/demo (no credentials needed)")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    s = get_settings()
    handlers = {
        "init-db": cmd_init_db, "add": cmd_add, "remove": cmd_remove, "list": cmd_list,
        "login": cmd_login, "collect": cmd_collect, "updates": cmd_updates, "digest": cmd_digest,
        "serve": cmd_serve,
        "demo": cmd_demo,
        "pause": lambda a, st: cmd_pause(a, st, True),
        "resume": lambda a, st: cmd_pause(a, st, False),
    }
    return handlers[args.command](args, s)


if __name__ == "__main__":
    raise SystemExit(main())
