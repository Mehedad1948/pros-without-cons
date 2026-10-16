# igdigest

A personal Instagram monitor that runs on your own computer. Give it profiles; it checks them for new
posts and reels and sends them to you on Telegram: one message per account, with a picture, the caption
and a link to the original. An optional AI digest can summarize everything once a day.

- **Telegram bot** — add, remove, pause and check accounts from your phone; receive new posts as messages.
- **Web dashboard** — account status, check history, collected posts and reports at `http://127.0.0.1:8000`.
- **Scheduled checks** — every 6 hours by default; the first check of an account sends its 5 latest posts.
- **Optional AI digest** — a daily recap (Anthropic or any OpenAI-compatible API).
- **Safe by design** — you sign in yourself in a real browser window; your password is never stored and
  your session stays on your machine. If Instagram asks for a login or blocks the session, the app stops
  and tells you instead of trying to get around it.

## Requirements

- Windows, macOS or Linux with **Python 3.11+**
- An Instagram account and a Telegram account
- A proxy if Instagram or Telegram is blocked on your network (for example `127.0.0.1:10808`)

## Setup

Run these commands from the project folder (Windows PowerShell shown; on macOS/Linux use `.venv/bin/` instead of `.venv\Scripts\`).

**1. Install**

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[browser]"
.venv\Scripts\python -m playwright install chromium
copy .env.example .env
```

**2. Configure `.env`**

```ini
IGD_PROVIDER=browser
IGD_BROWSER_PROXY=socks5://127.0.0.1:10808      # only if Instagram needs a proxy
IGD_TELEGRAM_PROXY=socks5h://127.0.0.1:10808    # only if Telegram needs a proxy
IGD_TELEGRAM_BOT_TOKEN=                         # from @BotFather (step 4)
IGD_TELEGRAM_ALLOWED_USER_IDS=                  # your Telegram user ID (step 5)
```

Never commit `.env`; it is git-ignored.

**3. Sign in to Instagram**

```powershell
.venv\Scripts\igdigest login
```

A browser window opens: sign in, complete 2FA, and accept the cookie prompt if one appears. The window closes by itself.

**4. Create the Telegram bot**

In Telegram, open **@BotFather**, send `/newbot`, and put the token in `IGD_TELEGRAM_BOT_TOKEN`.

**5. Start the bot and allow yourself**

```powershell
.venv\Scripts\igdigest bot
```

Send `/start` to your bot. It replies with your user ID; put it in `IGD_TELEGRAM_ALLOWED_USER_IDS`, then restart the bot (Ctrl+C, run the command again).

**6. Use it**

In Telegram, tap **➕ Add account**, send usernames or profile links (e.g. `nasa bbcnews`), then tap **🔄 Check now**.
Keep the bot running; it also performs the scheduled checks.

## Useful commands

| Command | Purpose |
| --- | --- |
| `igdigest bot` | Telegram bot + scheduled checks |
| `igdigest serve` | Web dashboard + scheduled checks (add `IGD_TELEGRAM_ENABLED=true` to run the bot inside it) |
| `igdigest add <user>` / `list` / `remove <user>` | Manage accounts from the terminal |
| `igdigest collect` / `updates` | Check now / show new posts |
| `igdigest demo` | Try everything with sample data, no accounts needed |

Optional AI digest: set `IGD_LLM_PROVIDER=anthropic` and `IGD_LLM_API_KEY=...` in `.env`.

## Troubleshooting

- **"login required"** — run `igdigest login` again.
- **"cookie settings / consent page"** — run `igdigest login` and make the cookie choice in the window.
- **"collection blocked (HTTP 429)"** — Instagram is limiting this session or network; wait a few hours, check less often.
- **Bot does not answer** — check the token, and set `IGD_TELEGRAM_PROXY` if Telegram is blocked on your network.

More details (all settings, the official Instagram API option, how deduplication and digests work): [docs/GUIDE.md](docs/GUIDE.md).
