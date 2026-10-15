# igdigest — personal Instagram monitor and digest

A small local app: you give it Instagram profiles. It checks them on a schedule for new posts and
reels, stores them in SQLite and extracts text where it can. It has two separate features on top of that:

| Feature | Needs an LLM? | What you get |
| --- | --- | --- |
| **Updates** | No | A list of new posts per account (time, type, caption, link) plus failed checks. Written after each scheduled check when there is something to report, or on demand with `igdigest updates`. |
| **Digest** | Yes | An LLM-written recap: overview, key points per account, recurring themes, every point linked to its post. Daily at 20:00, or on demand with `igdigest digest`. |

Updates never depend on the digest. With `IGD_LLM_PROVIDER=none` (the default) the digest is simply off: its scheduled
job is not registered and its buttons explain how to enable it. Each feature keeps its own record of which posts it has
delivered, so a post appears once in updates and, separately, once in a digest. The collection and updates code never
imports any LLM code; a test enforces this.

Stack: Python 3.11+, FastAPI and Jinja2 (server-rendered pages), SQLite (stdlib `sqlite3`) and APScheduler 3.x.
Optional: Playwright, Tesseract OCR, OpenAI or faster-whisper transcription, and Anthropic or OpenAI-compatible LLMs.

## Quick start: fixture mode, no credentials

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\igdigest demo          # end-to-end demo in data/demo (isolated database)
.venv\Scripts\python -m pytest       # tests
```

On macOS/Linux, use `.venv/bin/...` instead of `.venv\Scripts\...`.

`igdigest demo` adds four fictional fixture accounts and then runs these steps:

1. A first check: each account gets an initial recap of at most its 5 latest posts. One account always fails.
2. An updates report listing those posts, which marks them as delivered for updates.
3. A second check that finds nothing new. The updates report after it says "no new posts" separately from the failed account.
4. A new post is added to the fixture, and a third check collects only that post.
5. An updates report that contains only that post.
6. A digest preview, only if an LLM is configured. Otherwise the demo says the digest was skipped.

To browse the demo database in the web UI:

```powershell
$env:IGD_DB_PATH="data\demo\demo.db"; $env:IGD_FIXTURE_PATH="data\demo\fixture.json"
.venv\Scripts\igdigest serve --no-scheduler     # http://127.0.0.1:8000
```

Fixture mode is labelled everywhere: a banner in the UI, `FIXTURE MODE` in digests, `is_fixture` on stored items,
and `example.com` links. A fixture account that is not in the fixture file fails as "not accessible". Live
providers never fall back to fixture data.

## Commands

| Command | What it does |
| --- | --- |
| `igdigest init-db` | Create the database or apply pending migrations (every command also does this on demand). |
| `igdigest add <user\|@user\|profile URL> ...` | Start monitoring accounts. |
| `igdigest remove <user>` / `pause <user>` / `resume <user>` / `list` | Manage accounts. |
| `igdigest login` | Open a visible browser. You sign in and complete 2FA yourself, and the session state is saved locally. |
| `igdigest collect [--account user]` | Run one collection now. |
| `igdigest updates [--preview]` | List new posts now (no LLM). Without `--preview` it marks them as delivered for updates. |
| `igdigest digest [--preview]` | Run one LLM digest now (needs `IGD_LLM_PROVIDER`). Without `--preview` it marks the posts as delivered for digests. |
| `igdigest serve [--host --port] [--no-scheduler]` | Start the web app and the scheduler. |
| `igdigest demo` | Run the fixture demonstration above. |

`python -m igdigest <command>` works too.

## Configuration

Copy `.env.example` to `.env`. All settings are environment variables with the `IGD_` prefix.

| Setting | Default |
| --- | --- |
| `IGD_PROVIDER` | `fixture`. Also `graph_api`, `browser` or `auto`. |
| `IGD_CHECK_INTERVAL_HOURS` | `6` |
| `IGD_DIGEST_TIME` / `IGD_TIMEZONE` | `20:00` / `Asia/Tehran` |
| `IGD_INITIAL_IMPORT_LIMIT` | `5` |
| `IGD_MAX_POSTS_PER_CHECK` / `IGD_MAX_MEDIA_PER_POST` / `IGD_MAX_MEDIA_BYTES` | `12` / `4` / 50 MB |
| `IGD_RETRY_ATTEMPTS` / `IGD_RETRY_BASE_SECONDS` | `3` / `5` (exponential backoff for transient errors) |
| `IGD_UPDATES_ENABLED` / `IGD_UPDATES_AFTER_CHECK` | `true` / `true` |
| `IGD_DIGEST_ENABLED` | `true`, but the digest only runs when an LLM provider is set |
| `IGD_LLM_PROVIDER` / `IGD_LLM_MODEL` / `IGD_LLM_API_KEY` | `none` / provider default / unset |
| `IGD_DIGEST_LANGUAGE` | `English` (for example `Persian`) |
| `IGD_OCR_ENABLED`, `IGD_TRANSCRIBE_PROVIDER` | off / `none` |

The scheduler runs inside `igdigest serve`. The check job writes an updates report afterwards when there are new
posts or failed checks. The 20:00 digest job is registered only when an LLM is configured, and it runs a fresh check
first (`IGD_COLLECT_BEFORE_DIGEST`).

## Instagram access: what each provider can and cannot do

All collection goes through `igdigest/providers/base.py::Provider` (`fetch_recent`, `download`). Adding
another access method means writing one class.

### `graph_api`: official API (preferred where it applies)

Uses **Business Discovery** in the Instagram API with Facebook Login (`GET /<your-ig-user-id>?fields=business_discovery.username(x){media{...}}`).
I checked this against Meta's docs on 2026-10-15; their examples use `v25.0`.

- It can read only **Instagram Business or Creator accounts**. Personal profiles and age-gated accounts are not returned.
- The query runs through **your own Instagram professional account** (set `IGD_GRAPH_IG_USER_ID`). It needs a Facebook
  User access token with `instagram_basic`, `instagram_manage_insights` and `pages_read_engagement`.
  Add `ads_management` or `ads_read` if your Page role comes from Business Manager.
- A personal-account token cannot read arbitrary profiles. The Instagram Basic Display API was shut down on 2024-12-04.
- `media_url` is omitted for media with copyrighted content, such as reels with licensed music. Those posts are marked
  partial and summarized from the caption.
- An expired token is reported as "login required". A rate limit is reported as "collection blocked" and stops the run.

### `browser`: best-effort local connector (Playwright)

```powershell
.venv\Scripts\python -m pip install -e ".[browser]"
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\igdigest login        # sign in yourself in the window that opens, including 2FA
```

- `login` never reads or asks for your password. It waits until Instagram sets its session cookie, then saves Playwright's
  storage state to `data/session/instagram_state.json`. On Windows that file is restricted with an ACL to your user only;
  elsewhere it gets `chmod 600`. `data/` is git-ignored.
- Collection opens each profile with your session and reads it the way instagram.com's own web client does. It handles
  one profile at a time, with a fixed pause between profiles (`IGD_BROWSER_DELAY_SECONDS`).
- If Instagram redirects to login, the account is marked **login required**. A challenge, checkpoint or HTTP 429 is
  marked **collection blocked**. In both cases the whole run stops and the remaining accounts are recorded as "not checked".
  Run `igdigest login` again after resolving it in a normal browser.
- Not implemented on purpose: CAPTCHA or challenge bypass, proxy rotation, fingerprint spoofing and account rotation.
  `IGD_BROWSER_PROXY` is a single fixed network proxy for your own connectivity, nothing more.
- Instagram's web endpoints are undocumented and change often, so expect this connector to need maintenance.

### `auto`

Tries `graph_api` first. It falls back to `browser` only when the API says the account cannot be read, for example
a personal profile. Login and blocked errors are never retried on the other connector.

### Broadcast channels (not implemented)

Instagram broadcast channels are separate from profile posts. This MVP treats "channels" as normal profiles.
Links on `ig.me` and `instagram.com/channel/...` paths are rejected with a message asking for the creator's profile
instead. The Instagram Platform pages I checked (changelog, Business Discovery, IG Media reference) document no API
for reading channels you follow. Supporting them would need a new `Provider` and is out of scope here.

## How it behaves

- **Deduplication.** Each post is keyed by its shortcode, which is the same in the web UI and the Graph API. The
  database has `UNIQUE(account_id, post_key)` and all state lives in SQLite, so it survives restarts.
- **First check.** At most the 5 latest posts are imported and flagged `initial recap`, in the check history and in the
  digest. On later checks, unseen posts that are older than the first check (for example a newly pinned old post) are
  ignored and noted.
- **No overlapping checks.** A database lock covers both processes (CLI and web), and APScheduler runs each job with
  `max_instances=1`. Accounts that keep failing are skipped by scheduled checks for an exponentially growing period
  (30 min up to 24 h). Manual checks ignore that backoff.
- **Extraction.** Images go through Tesseract OCR and videos through OpenAI or faster-whisper transcription, but only
  when configured. Media is downloaded to a temporary directory that is deleted after each post. If any media can't be
  processed, the post is marked `partial` or `caption_only`, and the digest lists why under "Coverage".
- **Untrusted content.** Captions, OCR text and transcripts are sent to the LLM as an escaped JSON data block, with
  instructions that it is third-party data, never instructions. The model can only cite app-assigned refs (`P3`).
  The app maps refs to stored URLs and drops points with unknown refs, so the model never writes links. The web UI
  escapes all source text.
- **Digest content.** Each digest has an overview, then points per account. Each account separates
  "What the creator says" from "Interpretation (the app's analysis)". Recurring themes across accounts are listed when
  present. The digest also shows the time window, failed or unchecked accounts, partially processed media, and the
  output language.
- **Which posts go into a report.** An updates report includes every post not already in an earlier updates report;
  a digest includes every post not already in a successful scheduled digest. The two are tracked separately. Previews
  never mark posts as delivered. A failed digest marks nothing, so its posts roll into the next one.
- **Nothing new.** Both updates and digests say one of: "No new posts in this window", "No new posts from the accounts checked
  successfully; N had failed checks", "No account could be checked … not the same as no new posts", or "No checks ran".
- **LLM.** With `IGD_LLM_PROVIDER=anthropic` the default model is `claude-opus-5-5`. Refusal fallback
  (`fallbacks: "default"`) is enabled for models that support it; set `IGD_LLM_REFUSAL_FALLBACK=false` to disable it.
  With `openai`, any OpenAI-compatible endpoint works through `IGD_LLM_BASE_URL`. With `none` the digest is
  off. If the LLM call fails, the digest is stored as failed and its posts wait for the next digest; the plain list of
  those posts is still available in updates.

## Database and migrations

The schema lives in `igdigest/db.py::MIGRATIONS` and is versioned with `PRAGMA user_version`. To change it, append a new
SQL string to that list and never edit an applied migration. The tables are `accounts`, `check_runs`, `items`,
`update_reports` and `update_report_items` (feature 1), `digests` and `digest_items` (feature 2), and `locks`.

## Project layout

```
igdigest/
  config.py        settings (IGD_* env vars, .env)
  db.py            SQLite connection + migrations
  repo.py          accounts, username parsing, cross-process locks
  providers/       base interface, fixture, graph_api, browser (Playwright), auto
  collector.py     check runs: retries/backoff, bounds, dedupe, initial recap, failure recording
  extraction.py    OCR / transcription with temp media
  reporting.py     shared: delivery selection, check coverage, "no new posts" vs "checks failed"
  updates.py       feature 1: new-post lists with links (no LLM)
  llm.py           feature 2: LLM clients (anthropic / openai-compatible), prompt, schema
  digest.py        feature 2: LLM recap, coverage, markdown rendering
  scheduler.py     APScheduler jobs
  web/             FastAPI app + templates
  cli.py           commands
fixtures/sample_posts.json   fictional sample data
tests/                       pytest suite
```
