# Career Jarvis — personal opportunity copilot

A background service that polls your Gmail, classifies every new message with a
cheap LLM (real opportunity vs. noise?), drafts a reply **in your voice** with
a stronger LLM, saves it as a **Gmail draft** in the original thread, and
pushes a phone notification with the summary + draft. **It never sends
anything for you** — you are always the send button.

This is a reliability-first, no-agent-frameworks Python service: SQLite dedup,
per-message isolation, schema-validated classifier output, model-agnostic LLM
layer (LiteLLM), and a pluggable notification channel.

---

## Architecture

```
                ┌─────────────────────────────────────────────────────────────┐
                │                       src/main.py (orchestrator)            │
                │   poll loop / --once / --dry-run / per-message try-except   │
                └───────────────┬───────────────────────────────┬─────────────┘
                                │                               │
            ┌───────────────────▼───────────┐   ┌───────────────▼───────────────┐
            │  Ingestion (incremental)      │   │  State: src/store.py (SQLite) │
            │  gmail_client.fetch_new()     │   │  - dedup (source,msg_id)      │
            │   historyId cursor, readonly  │   │  - history cursor             │
            │   + compose (drafts) scopes   │   │  - classification log         │
            │  [opt-in] linkedin_client.py  │   └───────────────────────────────┘
            └───────────────────┬───────────┘
                                │ EmailRecord(source=email|linkedin)
                                ▼
            ┌───────────────────────────────────────────────────────────────┐
            │  agents/classifier.py  (cheap model, json_mode, temp=0)       │
            │   -> Pydantic ClassificationVerdict  (validate -> retry ->     │
            │      fail-safe to ManualReview, NEVER drop)                    │
            └───────────────┬───────────────────────────────────────────────┘
                            │ if is_job_opportunity
                            ▼
            ┌───────────────────────────────────────────────────────────────┐
            │  agents/drafter.py  (stronger model, grounded in profile/)     │
            │   -> paste-ready reply text (email or LinkedIn chat format)    │
            └───────────────┬───────────────────────────────────────────────┘
                            │
              ┌─────────────┴──────────────┐
              ▼                            ▼
   gmail_client.create_draft()      notifier.py -> backends/
   (compose scope, NOT send)        ntfy (default) | whatsapp | pushover | discord
                                    (every send wrapped; can never crash pipeline)
```

**Data flow:** `Gmail/LDAP → fetch_new → (dedup via Store) → classify →
(if opportunity) draft → Gmail draft + phone alert → Store.mark_processed`.

---

## What it does, precisely

1. **Polls Gmail** every `POLL_MINUTES`, including LinkedIn message/InMail
   notification emails (enable LinkedIn email notifications).
2. **Classifies every new message** with a cheap model into a structured
   verdict: is this a real opportunity worth your time? category? source
   (email vs. linkedin)? urgency? one-line summary?
3. For genuine opportunities only, **drafts a reply in your voice** using
   `profile/career_profile.md` as ground truth, with a stronger model.
4. **Creates the reply as a Gmail draft** in the original thread — you edit +
   send from your phone in one tap.
5. **Pushes a phone notification** (ntfy/whatsapp/pushover/discord) with the
   summary + draft text.
6. **Never sends on your behalf.** Structurally enforced: only
   `gmail.readonly` + `gmail.compose` scopes are requested; `gmail.send` is
   never requested. `grep -r gmail.send src/` confirms its absence.

---

## Non-negotiable guarantees

- **SQLite dedup** — each message id processed exactly once across
  crashes/restarts; Gmail `historyId` is the incremental cursor.
- **Per-message isolation** — every message is processed inside its own
  `try/except`; one poison message never wedges the loop. On failure, an
  error alert is sent and the message is marked processed so it can't loop.
- **Schema-validated classifier** — Pydantic v2, `temperature=0`, strict
  system prompt. On malformed output: retry once, then fail safe to
  manual-review (never silently drop).
- **Notifier can't crash the pipeline** — every backend call is wrapped.
- **Structured logging** throughout.

## Security model (you're granting Gmail read+compose access)

- **Scopes locked:** only `gmail.readonly` + `gmail.compose` are requested.
  `gmail.send` is never requested and never called. You are always the send
  button. (`grep -r gmail.send src/` returns only negative-assertion comments.)
- **Secrets never committed:** `.env`, `.data/` (OAuth credentials, token,
  SQLite), and the real `career_profile.md` / `wiki/` are all gitignored.
- **Token + DB file permissions:** the OAuth token and SQLite DB are written
  with `chmod 0o600` (owner-only) on POSIX.
- **Prompt-injection defense (PII guard):** emails are untrusted input. The
  drafter system prompt forbids including your phone/email/salary/visa
  status/employer in replies, and a post-filter (`_scrub_pii`) strips any of
  those literals that slip through, with a logged warning.
- **Alert privacy:** opportunity alerts point you to Gmail Drafts but do NOT
  include the draft text (ntfy topics are public-by-default). Error alerts
  send only a short summary — never stack traces or file paths — to the
  notifier. Full tracebacks stay in local logs.
- **ntfy header safety:** notification titles are sanitized to latin-1 so
  unicode (em-dashes, emoji) can't crash the HTTP send.
- **No em-dashes in drafts:** enforced both in the drafter prompt and by a
  code-level post-filter (`_strip_em_dashes`).
- **SQL is parameterized** everywhere; no injection surface.
- **No agent frameworks** — plain typed functions; LiteLLM is a thin client.
- **Data sent to your LLM provider:** the classifier sees every new email's
  sender/subject/body (first 4000 chars); the drafter sees your profile +
  the email. Anthropic's paid-API terms default to no-training with ~30-day
  retention; verify current terms at anthropic.com/legal. To send zero data
  to any third party, run the classifier on **local Ollama** (see Model table).

---

## Quick start

```bash
# 1. Clone / enter the project
cd career-jarvis

# 2. (Recommended) virtualenv
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1

# 3. Install deps
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# edit .env: set CLASSIFIER_MODEL, DRAFTER_MODEL, the relevant API key,
#           NOTIFY_CHANNEL + that channel's vars (default ntfy -> NTFY_TOPIC)

# 5. Put your Google OAuth client secret at .data/credentials.json (see below)

# 6. Smoke test the full pipeline with NO live credentials:
python -m src.main --once --dry-run

# 7. First real run (does OAuth in a browser on first call):
python -m src.main --once

# 8. Daemon mode:
python -m src.main            # polls every POLL_MINUTES
```

---

## 30-minute setup

### 1. Google Cloud — Gmail API (read + compose, **never send**)

1. Go to <https://console.cloud.google.com/>. Create (or pick) a project.
2. **APIs & Services → Library →** search **Gmail API → Enable**.
3. **OAuth consent screen:** User type **External**, fill minimal app info.
   Add **your own Gmail address** as a **Test user** (Testing status is fine
   for personal use — no verification needed).
4. **Credentials → Create credentials → OAuth client ID →** Application type
   **Desktop app**. Name it, create it.
5. Download the JSON (**Download client secret**). Save it as
   `.data/credentials.json` in the project root.
6. The app requests only these scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
   On first run, a browser opens; authorize with your test-user account.
   A token is cached at `.data/token.json`.

> The `gmail.send` scope is intentionally never requested. The system can
> only create drafts; you are always the send button.

### 2. Notification channel (default: **ntfy**)

Install the **ntfy** phone app, subscribe to a **secret** topic name you make
up (e.g. `career-jarvis-7f3a9b`), set `NTFY_TOPIC` to it. Done — no account,
no charges. See the **Pick your notification channel** section below for the
30-second setup of each option and the honest tradeoff.

### 3. LLM provider (default: **Anthropic**)

Set `CLASSIFIER_MODEL`, `DRAFTER_MODEL`, and `ANTHROPIC_API_KEY`. See the
**Model table** below. To run **fully local for $0**, see **Ollama**.

### 4. Enable LinkedIn email notifications

LinkedIn → **Settings → Communications →** enable email notifications for
**Messages** and **InMail**. The classifier tags these as `source="linkedin"`.

### 5. Profile

`profile/career_profile.md` is your ground truth. Edit it; the drafter reads
it verbatim on every draft.

### 6. Run

`pip install -r requirements.txt`, then `python -m src.main --once` (first run
does OAuth), then daemon mode.

---

## Model table (REQUIREMENT A — model-agnostic via LiteLLM)

Model choice is pure config — `CLASSIFIER_MODEL` / `DRAFTER_MODEL` in `.env`.
Switching providers requires **no code edits**. Classification runs on every
email, so a **cheap or local** classifier saves the most money.

| To use | Set these env vars | Example model strings |
|---|---|---|
| **Anthropic** (default) | `ANTHROPIC_API_KEY` | `claude-haiku-4-5-20251001` (classifier), `claude-sonnet-4-5-20250929` (drafter) |
| **OpenAI** | `OPENAI_API_KEY` | `gpt-4o-mini` (classifier), `gpt-4o` (drafter) |
| **Groq** (fast, free tier) | `GROQ_API_KEY` | `groq/llama-3.1-8b-instant` (classifier), `groq/llama-3.3-70b-versatile` (drafter) |
| **Together / OpenRouter** | `TOGETHERAI_API_KEY` or `OPENROUTER_API_KEY` | `together_ai/Meta-Llama-3.3-70B-Instruct-Turbo`, `openrouter/meta-llama/llama-3.3-70b-instruct` |
| **Local Ollama (offline, $0)** | `LLM_BASE_URL=http://localhost:11434` (optional) | `ollama/llama3.1` (classifier), `ollama/llama3.3` (drafter) |

> Model IDs verified against official docs (Jul 2026). Always confirm current
> IDs in your provider console before production use — they change.

### Fully-local, zero-API-cost mode (Ollama)

```bash
# Install Ollama: https://ollama.com  (or: curl -fsSL https://ollama.com/install.sh | sh)
ollama pull llama3.1        # cheap classifier
ollama pull llama3.3        # stronger drafter
```

In `.env`:
```dotenv
CLASSIFIER_MODEL=ollama/llama3.1
DRAFTER_MODEL=ollama/llama3.3
LLM_BASE_URL=http://localhost:11434
LLM_API_KEY=
ANTHROPIC_API_KEY=
```

> Open-source models are the least reliable at strict JSON. The classifier's
> Pydantic **validate → retry once → fail-safe to manual review** path is the
> cross-provider safety net and matters most here. JSON mode
> (`response_format={"type":"json_object"}`) is requested natively where
> supported; the schema is also pinned in the system prompt as guidance.

### Anthropic config example

```dotenv
CLASSIFIER_MODEL=claude-haiku-4-5-20251001
DRAFTER_MODEL=claude-sonnet-4-5-20250929
ANTHROPIC_API_KEY=sk-ant-...
```

### Ollama config example

```dotenv
CLASSIFIER_MODEL=ollama/llama3.1
DRAFTER_MODEL=ollama/llama3.3
LLM_BASE_URL=http://localhost:11434
```

---

## Pick your notification channel (REQUIREMENT B)

Set `NOTIFY_CHANNEL` in `.env` to one of: `ntfy` (default), `whatsapp`,
`pushover`, `discord`. Switching is one env var — no code edits.

| Channel | Setup (30 sec) | Vars | Honest tradeoff |
|---|---|---|---|
| **ntfy** (default) | Install ntfy app, subscribe to a secret topic, set `NTFY_TOPIC`. | `NTFY_TOPIC`, optional `NTFY_BASE_URL` / `NTFY_TOKEN` | Free, account-less, open-source, reliable. **Recommended.** |
| **whatsapp** | Add CallMeBot bot (+34 600 83 81 81), message it `I allow callmebot to send me messages`, paste the API key it replies with. | `WHATSAPP_PHONE`, `WHATSAPP_APIKEY` | Free; routes through a 3rd-party relay; can rate-limit. |
| **pushover** | Buy the $5 app, create an app at pushover.net, paste tokens. | `PUSHOVER_TOKEN`, `PUSHOVER_USER` | $5 one-time, purpose-built, very reliable. |
| **discord** | Create a webhook in a server channel's integrations. | `DISCORD_WEBHOOK_URL` | Free, good mobile push, needs a server. |

All channels: a notification failure is caught + logged, never crashes the
pipeline. Messages are truncated to each channel's length limit.

### Message format (all channels)

```
[icon] [Email|LinkedIn] <category> (urgency=<high|medium|low>, conf=<x>)
From: <sender>
Subject: <subject>
Summary: <one-line summary>

Draft:
<draft text>

✅ Gmail draft created (id=...). Edit + send from your phone.   # email source
# OR, for LinkedIn source:
⚠️ Open the LinkedIn thread and paste to send — the system will NOT send for you.
```

---

## ⚠️ OPTIONAL: direct LinkedIn message ingestion (opt-in, against LinkedIn ToS)

> **Read `LINKEDIN_ADDENDUM.md` before enabling.** Direct browser automation of
> your logged-in LinkedIn session **violates LinkedIn's User Agreement**.
> LinkedIn actively detects automation; the realistic risk is **account
> restriction or a permanent ban**. The email-notification baseline (this
> app's default) is the safe, supported path.

This is **off by default** (`LINKEDIN_ENABLED=false`). The baseline already
ingests LinkedIn messages via their notification emails — no automation, no
risk. Enable direct access only if you accept the risk.

To enable (optional):
1. `pip install playwright && playwright install chromium`
2. Set `LINKEDIN_ENABLED=true` in `.env`.
3. Log into LinkedIn **manually once** in the persistent Playwright profile at
   `.data/linkedin_profile/` (temporarily set the launch to `headless=False`
   or use a one-time login script — the app itself **never** enters your
   credentials and **never** types into or sends any message).
4. It polls on its own slow schedule (`LINKEDIN_POLL_HOURS`, default 6),
   jittered, read-only, capped at `LINKEDIN_MAX_THREADS` (default 20).

Enforced: `LINKEDIN_ENABLED=false` fully disables this path; no credential
entry; no send/type-into-message code path exists in `linkedin_client.py`.

---

## Deployment

### systemd (home server / Pi / VPS)

```ini
# /etc/systemd/system/career-jarvis.service
[Unit]
Description=Career Jarvis opportunity copilot
After=network-online.target

[Service]
Type=simple
User=abhis
WorkingDirectory=/home/abhis/career-jarvis
EnvironmentFile=/home/abhis/career-jarvis/.env
ExecStart=/home/abhis/career-jarvis/.venv/bin/python -m src.main
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now career-jarvis
journalctl -u career-jarvis -f
```

> First run does OAuth and needs a browser. Run `python -m src.main --once`
> once interactively (or via SSH with port forwarding) to authorize, then
> start the service — the token is cached at `.data/token.json`.

### Cheap VPS

Same as systemd on a $4/mo VPS (Hetzner/Contabo). Keep `.data/` on persistent
storage. Make sure the VPS IP isn't on Gmail's suspicious-login list — use a
residential-ish region and authorize it during OAuth.

### GitHub Actions cron (run `--once`) — recommended free deployment

A complete, ready-to-use workflow is included at
[`.github/workflows/career-jarvis.yml`](.github/workflows/career-jarvis.yml),
with a full step-by-step guide in [`.github/DEPLOYMENT.md`](.github/DEPLOYMENT.md).

**Short version:**
1. Generate the OAuth token locally once (`python -m src.main --once`) — a
   cloud runner can't open a browser.
2. Add repository Secrets: `ANTHROPIC_API_KEY`, `NTFY_TOPIC`,
   `CLASSIFIER_MODEL`, `DRAFTER_MODEL`, and the **entire JSON contents** of
   your local `.data/credentials.json` and `.data/token.json` as
   `GMAIL_CREDENTIALS_JSON` and `GMAIL_TOKEN_JSON`.
3. Push to `main`. The workflow runs `--once` every 15 minutes on GitHub's
   runners, free.

The critical detail (handled by the included workflow): GitHub runners are
ephemeral, so the SQLite state DB (dedup + `historyId` cursor) **must** be
cached across runs. The workflow uses `actions/cache@v4` on `.data/` with a
**fixed cache key** so every run restores and re-saves the same DB slot.
Without this, every run would either re-backfill or miss messages. The
workflow also writes the OAuth files from secrets each run, `chmod 600`s
them, validates the JSON, and uses a concurrency group so runs don't
overlap. See `.github/DEPLOYMENT.md` for monitoring, token-refresh, and
cache-eviction notes.

### Rough API-cost expectations

- **Anthropic:** Haiku 4.5 classifier ≈ fractions of a cent per email; Sonnet
  drafter only on genuine opportunities. A light inbox (a few dozen
  recruiter-ish emails/day) → well under $1/month.
- **OpenAI:** `gpt-4o-mini` classifier is ~$0.15/1M input tokens — pennies
  per day. `gpt-4o` drafting only on real opportunities.
- **Groq:** has a generous free tier; near-zero cost for personal volume.
- **Local Ollama:** **$0** API cost (you pay only electricity/compute).

> Classification runs on **every** email, so a cheap/local classifier is the
> biggest lever. Drafting only fires on genuine opportunities.

---

## Testing

```bash
# Acceptance: classifier behavior on sample fixtures (mocked LLM)
pytest -q

# End-to-end pipeline with NO live credentials (mocked Gmail + LLM + notifier)
python -m src.main --once --dry-run
```

Sample fixtures live in `tests/sample_emails.py`: real recruiter, interview
invite, LinkedIn InMail, agency spam, job-board digest, rejection, networking
intro, non-job mail.

---

## Acceptance checklist (self-verified)

- [x] Every `.py` file imports and compiles (`python -m py_compile`).
- [x] `python -m src.main --once --dry-run` runs the full loop against mocked
      Gmail + mocked LLM with no live credentials, without crashing.
- [x] Switching `CLASSIFIER_MODEL`/`DRAFTER_MODEL`/`LLM_BASE_URL` changes the
      model with no code edits (Anthropic and Ollama configs shown above).
- [x] Switching `NOTIFY_CHANNEL` between ntfy/whatsapp/pushover/discord works
      with no code edits (dispatcher in `src/notifier.py`).
- [x] `tests/test_classifier.py` passes: recruiter→opportunity,
      agency-spam→not opportunity, interview→high urgency, LinkedIn
      fixture→source="linkedin".
- [x] No `gmail.send` scope anywhere — grep confirms only `gmail.readonly` +
      `gmail.compose` are requested in `src/gmail_client.py`.
- [x] Classifier handles malformed model output (incl. from open-source
      models) without dropping the message: retry once → fail-safe to manual
      review (see `tests/test_classifier.py`).
- [x] README has an ASCII architecture diagram, the model table, and the
      channel setup guide.
- [x] Model IDs, LiteLLM signature, and Gmail SDK calls verified against
      current official docs (Jul 2026), not memory.

---

## File tree

```
career-jarvis/
  src/
    __init__.py
    main.py              # orchestrator: poll loop, --once, --dry-run, isolation
    config.py            # env loading + validation; model + channel selection
    llm.py               # provider-agnostic complete() via LiteLLM
    store.py             # SQLite: dedup, history cursor, classification log
    gmail_client.py      # OAuth (readonly+compose ONLY), fetch new, create_draft
    linkedin_client.py   # OPTIONAL direct LinkedIn reader (opt-in, default OFF)
    notifier.py          # dispatch to selected backend; error alerts; crash-safe
    notifiers/
      __init__.py
      base.py            # uniform backend interface
      whatsapp.py        # CallMeBot
      ntfy.py            # ntfy.sh (default)
      pushover.py
      discord.py
    agents/
      __init__.py
      classifier.py      # triage -> validated Pydantic verdict, retry, fail-safe
      drafter.py         # reply drafting, grounded in career_profile.md
  profile/
    career_profile.example.md  # sanitized template (real profile is gitignored)
  requirements.txt
  .env.example
  .gitignore           # excludes .env, .data/, real profile, wiki/
  README.md
  .github/
    DEPLOYMENT.md      # GitHub Actions deploy guide
    workflows/
      career-jarvis.yml  # cron --once workflow (free, always-on)
  tests/
    __init__.py
    test_classifier.py   # replay sample emails, assert categories (mocked LLM)
    test_drafter.py      # security regression: em-dash, latin-1, PII scrub, alert privacy
    sample_emails.py     # 8 fixtures
```

---

## Design decisions (noted where the prompt was ambiguous)

- **First-run Gmail behavior:** with no stored cursor, we backfill the most
  recent `INITIAL_BACKFILL` (5) messages and persist `historyId`, then go
  purely incremental via `users.history.list`. This avoids re-notifying on
  years of old mail while giving immediate value. Adjust
  `INITIAL_BACKFILL` in `src/gmail_client.py` if you want more/less.
- **History expiry:** Gmail purges `history` after ~1 week. If a poll cycle
  is older than that, we reseed the cursor and skip that cycle rather than
  miss or duplicate messages.
- **Drafter schema grounding:** the profile is injected verbatim into the
  drafter system prompt; the model is told never to fabricate and to use only
  the proof points present. Banned words and tone rules are enforced in the
  prompt (a model could still slip — review drafts before sending).
- **No agent frameworks:** plain typed functions, as required. LiteLLM is the
  only LLM dependency; it is a thin client, not an orchestration framework.
```
