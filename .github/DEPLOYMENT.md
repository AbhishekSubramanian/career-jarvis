# Deploying Career Jarvis on GitHub Actions (free, always-on)

This is the recommended free deployment for a personal app. A scheduled
workflow runs `python -m src.main --once` every 15 minutes on GitHub's
runners. No server, no daemon, $0.

## One-time local setup (do this first, on your laptop)

1. **Generate the OAuth token locally** (a cloud runner can't open a browser):
   ```powershell
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   # Fill .env first (ANTHROPIC_API_KEY, NTFY_TOPIC)
   python -m src.main --once
   ```
   A browser opens once, you authorize, and the token caches at
   `.data/token.json`. Confirm you have these two files locally:
   - `.data/credentials.json` (the Google OAuth client secret you downloaded)
   - `.data/token.json` (the cached access+refresh token)

2. **Add repository Secrets** (GitHub repo → Settings → Secrets and variables
   → Actions → New repository secret):

   | Secret name | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | Your Anthropic API key |
   | `NTFY_TOPIC` | Your secret ntfy topic |
   | `GMAIL_CREDENTIALS_JSON` | **Entire contents** of `.data/credentials.json` (paste the whole JSON, newlines are fine) |
   | `GMAIL_TOKEN_JSON` | **Entire contents** of `.data/token.json` (paste the whole JSON) |
   | `CLASSIFIER_MODEL` | e.g. `claude-haiku-4-5-20251001` |
   | `DRAFTER_MODEL` | e.g. `claude-sonnet-4-5-20250929` |
   | `NTFY_BASE_URL` *(optional)* | Only if you self-host ntfy |
   | `NTFY_TOKEN` *(optional)* | Only for protected ntfy topics |

   > **Tip for pasting JSON secrets:** open the file, select all, copy, paste
   > into the GitHub secret value box. GitHub preserves newlines. The workflow
   > uses `printf '%s'` so no trailing newline is added when writing it back
   > out, and validates the JSON before running.

3. **Push the workflow file** (already in the repo at
   `.github/workflows/career-jarvis.yml`) to `main`.

## How state persists across ephemeral runners

The single tricky part of the Actions deployment is that GitHub runners are
**ephemeral** — the filesystem is wiped after each job. Career Jarvis relies
on a SQLite DB (`.data/career_jarvis.db`) for two things that MUST persist:

- **Dedup** — so a message is never processed/notified twice.
- **The Gmail `historyId` cursor** — so each run fetches only *new* mail.

The workflow solves this with `actions/cache@v4` on the `.data/` directory,
using a **fixed cache key** (`career-jarvis-state-db`). A fixed key means
every run restores the *same* cache slot and, at job end, saves the updated
DB back into that slot. The credentials/token files written from secrets are
also in `.data/`, but they're re-written from secrets every run, so caching
them is harmless (and the `chmod 600` step keeps them tight).

**If the cache is ever evicted** (GitHub can evict unused caches after ~7
days of inactivity, and a 15-min cron keeps it warm so this is unlikely),
the next run will have no cursor and will re-backfill the 5 most recent
messages (`INITIAL_BACKFILL`). Dedup is lost for that one run, so you might
get a duplicate notification for a very recent message. Not dangerous — the
app never sends — just briefly noisy. The cursor re-seeds itself and
incremental fetch resumes.

## Running / monitoring

- **Manual run:** Actions tab → "career-jarvis" workflow → "Run workflow"
  button (defined via `workflow_dispatch:`). Use this to test after setup.
- **Logs:** click any run to see `--once` output (classifier verdicts,
  drafted opportunities, Gmail draft ids). Stack traces from errors are
  visible here but are NOT sent to ntfy (only a short summary is pushed).
- **Concurrency:** the workflow uses a concurrency group so runs don't
  overlap (avoids double-processing on slow runs).

## When the OAuth token expires

Gmail refresh tokens can expire (rare, but happens on password change, OAuth
app changes, or 6 months of no use). In CI there's no browser, so a refresh
failure will fail the run with a clear error. To fix:

1. On your laptop, run `python -m src.main --once` again (it'll re-auth in
   the browser and refresh `.data/token.json`).
2. Update the `GMAIL_TOKEN_JSON` repository secret with the new
   `.data/token.json` contents.
3. Re-run the workflow.

## Cost

Free for private repos up to 2000 Actions minutes/month. Each `--once` run
takes ~30-60 seconds, so 15-minute cron = ~96 runs/month ≈ 1-2 hours of
minutes. Well within the free tier. LLM cost is separate (Anthropic) and
runs well under $1/month for a light inbox.

## Security notes

- All secrets live in GitHub Secrets, never in the repo or logs.
- `.data/` is gitignored and never committed.
- The OAuth token is written with `chmod 600` and the app also sets 0o600
  on the SQLite DB + token.
- Error alerts pushed to ntfy contain only a short summary (no stack traces,
  no file paths, no draft text). Opportunity alerts point you to Gmail
  Drafts but do NOT include the draft text in the push (ntfy topics are
  public-by-default).
- The classifier sees every new email's content (sent to Anthropic). If
  that bothers you, switch `CLASSIFIER_MODEL` to a local Ollama model and
  run the workflow on a self-hosted runner instead — see README "Ollama".
