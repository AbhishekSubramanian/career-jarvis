# BUILD PROMPT — "Career Jarvis" personal opportunity copilot
<!-- Paste everything below the line into Claude Sonnet 5 (high) or Opus 4.8 (high),
     ideally in Claude Code so it can write + test files. Attach career_profile.md. -->

---

You are my senior engineer. Build me a complete, production-quality, **runnable** personal system called **Career Jarvis**. Work meticulously and do not stop until every file compiles and the flows below are fully implemented. If a decision is ambiguous, choose the most reliable option and note it in the README — don't ask me mid-build unless truly blocking.

## What it does (the product, precisely)

A background service on my own machine/server that:
1. **Polls my Gmail** every N minutes for new messages, including **LinkedIn message/InMail notification emails** (I'll enable LinkedIn email notifications).
2. **Classifies every new message** with a cheap model into a structured verdict (real job opportunity worth my time? category? source = email vs linkedin? urgency? one-line summary?).
3. For genuine opportunities only, **drafts a reply in my voice** using the attached `career_profile.md` as ground truth, with a stronger model.
4. **Creates the reply as a Gmail draft** in the original thread so I edit + send from my phone in one tap.
5. **Pushes a notification to my phone** (channel is configurable — see below) with the summary + draft text.
6. It must **never send anything on my behalf.** Enforce structurally: request only Gmail `readonly` + `compose` scopes; never request `send`. I am always the send button.

## Non-negotiable engineering requirements

- **Language/stack:** Python 3.11+. Plain, typed functions with clear contracts — **no agent frameworks** (LangChain/CrewAI/etc.). Fewer moving parts = fewer failure modes.
- **Reliability is the headline feature:**
  - SQLite state store for dedup so a crash/restart never re-notifies me (mark each Gmail message id processed exactly once; use Gmail `historyId` as the incremental cursor).
  - Every message processed inside its own `try/except`; one poison message must never wedge the loop. On per-message failure, send a notification error alert and mark it processed so it can't loop forever.
  - Classifier returns **schema-validated JSON** (Pydantic v2), `temperature=0`, strict system prompt. On malformed output, retry once, then fail safe by flagging for manual review (never silently drop).
  - Wrap all network calls; the notifier must never crash the pipeline.
  - Structured logging throughout.
- **Config:** all secrets from `.env` via `python-dotenv`, validated at startup with a clear error if missing. Provide `.env.example`.

## REQUIREMENT A — Model-agnostic LLM layer (must-have)

I want to freely choose the model/provider — Anthropic, OpenAI, Google, **open-source / self-hosted models**, or anything else — without touching pipeline code. Implement a **provider abstraction**:

- Create `src/llm.py` defining a single function the rest of the app calls, e.g. `complete(role: str, system: str, user: str, json_mode: bool) -> str`, where `role` is `"classifier"` or `"drafter"`.
- **Use [LiteLLM](https://docs.litellm.ai) as the unification layer** so one code path covers 100+ providers via a single `litellm.completion(model=..., messages=...)` call. This natively supports Anthropic, OpenAI, Gemini, Groq, Together, OpenRouter, and **local/open-source** models via **Ollama** (`ollama/llama3.3`), **vLLM**, or any **OpenAI-compatible endpoint** (LM Studio, text-generation-webui) through a configurable `base_url`.
- **Model choice is pure config**, set per role in `.env`:
  ```
  CLASSIFIER_MODEL=...     # cheap/fast: e.g. claude-haiku, gpt-4o-mini, ollama/llama3.1, groq/llama-3.1-8b
  DRAFTER_MODEL=...        # stronger: e.g. claude-sonnet, gpt-4o, ollama/llama3.3, together/...
  LLM_BASE_URL=           # optional: OpenAI-compatible / local endpoint (e.g. http://localhost:11434 for Ollama)
  LLM_API_KEY=            # optional: provider key; blank for local models
  ```
- `json_mode=True` (classifier) must reliably yield parseable JSON across providers: prefer native JSON/structured-output mode where the provider supports it, and **always** keep the Pydantic validate-retry-failsafe path as the cross-provider safety net (open-source models are the least reliable at strict JSON — this matters most for them).
- The README must include a table: "To use X, set these env vars" for at least Anthropic, OpenAI, Groq, Together/OpenRouter, and **local Ollama** (fully offline). Include a note that classification runs on every email, so a cheap or local model there saves the most money.
- Ship `.env.example` pre-filled with a sensible default (Anthropic classifier+drafter) AND a commented-out **fully-local Ollama** block so I can flip to zero-API-cost mode.
- **Before coding, verify current model IDs and the LiteLLM call signature against official docs** — don't trust training-data model names.

## REQUIREMENT B — Pluggable notification channel (must-have)

I do **not** want Telegram. Make the notifier a swappable interface (`src/notifier.py` defines `send_opportunity_alert(...)` and `send_error_alert(...)`; concrete backends live in `src/notifiers/`). The active channel is chosen by `NOTIFY_CHANNEL` in `.env`. Implement **all** of these backends so I can switch with one env var:

1. **`whatsapp`** — via **CallMeBot** (free, self-messages my own number; I message the bot once to get an API key, then the app POSTs to `https://api.callmebot.com/whatsapp.php`). Document the one-time setup. Note it routes through a third-party relay (fine for low-volume personal alerts) and can rate-limit.
2. **`ntfy`** — via **ntfy.sh** (free, open-source, reliable push; I install the ntfy app, subscribe to a secret topic, the app POSTs to `https://ntfy.sh/<my-secret-topic>`). Support self-hosting via `NTFY_BASE_URL`. **Recommend this as the default** for reliable phone push with no account and no charges.
3. **`pushover`** — via **Pushover** ($5 one-time, purpose-built, very reliable; needs `PUSHOVER_TOKEN` + `PUSHOVER_USER`).
4. **`discord`** — via a **Discord webhook** (free; good mobile push; needs `DISCORD_WEBHOOK_URL`).

Requirements for the notifier layer:
- Common message format across channels: urgency icon + source (email/linkedin) + category + sender + subject + summary + the draft text + (for Gmail-source) a note that the draft is already in Gmail drafts.
- Respect each channel's length limits (truncate gracefully).
- A notification failure must be caught and logged, never crash the pipeline.
- `.env.example` documents every channel's vars; README has a "pick your notification channel" section with the 30-second setup for each and an honest one-line tradeoff.

## Deliverables (produce ALL as real files)

```
career-jarvis/
  src/
    __init__.py
    main.py              # orchestrator: poll loop, --once, --dry-run, per-message isolation
    config.py            # env loading + validation; model + channel selection
    llm.py               # provider-agnostic complete() via LiteLLM
    store.py             # SQLite: dedup, history cursor, classification log
    gmail_client.py      # OAuth (readonly+compose ONLY), fetch new msgs, create_draft
    notifier.py          # dispatch to the selected backend; error alerts
    notifiers/
      __init__.py
      whatsapp.py        # CallMeBot
      ntfy.py            # ntfy.sh (default)
      pushover.py
      discord.py
    agents/
      __init__.py
      classifier.py      # triage -> validated Pydantic verdict, retry, fail-safe
      drafter.py         # reply drafting, grounded in career_profile.md
  profile/
    career_profile.md    # copy the attached file here
  requirements.txt       # includes litellm, google-api-python-client, google-auth-oauthlib, pydantic, python-dotenv, requests
  .env.example
  README.md              # architecture diagram + model table + channel setup + 30-min setup + deploy + cost notes
  tests/
    test_classifier.py   # replay sample emails, assert categories (mock the llm layer)
    sample_emails.py     # 6-8 fixtures: real recruiter, interview invite, LinkedIn InMail,
                         #   agency spam, job-board digest, rejection, networking intro
```

## Classifier contract (implement exactly)

Pydantic model:
- `is_job_opportunity: bool` — true only for messages worth a reply (recruiter outreach, interview invite, actionable application update, networking). False for digests, rejections, mass agency spam, non-job mail.
- `category: Literal["recruiter_outreach","interview_invite","application_update","networking","job_alert_digest","rejection","not_job_related"]`
- `source: Literal["email","linkedin"]`
- `confidence: float` (0..1)
- `urgency: Literal["high","medium","low"]`
- `summary: str`

System prompt: classify conservatively — automated job-board digests and mass newsletters are NOT opportunities; a named human writing about a specific role IS. Apply the scam/spam heuristics from `career_profile.md`.

## Drafter (grounded)

Reads the attached `career_profile.md`. Drafts must honor its hard requirements (ask about H-1B sponsorship; ask total comp range up front; polite decline when a role conflicts), its tone rules (short, crisp, warm; banned-words list), and its scam filter. LinkedIn-source replies are formatted to paste into LinkedIn chat (no email salutation/signature). Never fabricate experience, availability, or work-authorization status.

## README must document

1. Google Cloud: enable Gmail API, OAuth consent (External, add myself as test user), Desktop OAuth client, download `credentials.json`.
2. Notification channel: setup for whichever of the four I pick (default ntfy).
3. LLM provider: the model table; how to run **fully local via Ollama** for zero API cost.
4. Enable LinkedIn email notifications for messages/InMail.
5. Fill `profile/career_profile.md`.
6. `pip install -r requirements.txt`; `python -m src.main --once` (first run does OAuth); then daemon mode.
7. Deployment with copy-paste configs: systemd on a home server/Pi; a cheap VPS; a GitHub Actions cron running `--once`. Rough API-cost expectations (and note local Ollama = $0).

## Acceptance checklist (verify before finishing)

- [ ] Every `.py` file imports and compiles.
- [ ] `python -m src.main --once --dry-run` runs the full loop against mocked Gmail + mocked LLM without live credentials, without crashing.
- [ ] Switching `CLASSIFIER_MODEL`/`DRAFTER_MODEL`/`LLM_BASE_URL` changes the model with **no code edits** (demonstrate Anthropic and Ollama configs in the README).
- [ ] Switching `NOTIFY_CHANNEL` between whatsapp/ntfy/pushover/discord works with no code edits.
- [ ] `tests/test_classifier.py` passes: recruiter->opportunity, agency-spam->not opportunity, interview->high urgency, LinkedIn fixture->source="linkedin".
- [ ] No `gmail.send` scope anywhere (grep to confirm).
- [ ] Classifier handles malformed model output (esp. from open-source models) without dropping the message.
- [ ] README has an ASCII architecture diagram, the model table, and the channel setup guide.
- [ ] Model IDs, LiteLLM signature, and Gmail SDK calls verified against current official docs, not memory.

Build it now, end to end. Show me the final file tree and the key files, then give me the exact commands to run it.
