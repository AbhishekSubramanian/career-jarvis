# ADDENDUM — Direct LinkedIn message reading (append to BUILD_PROMPT.md)
<!-- This is an OPTIONAL, opt-in extension. Read the "Read this first" section before using it. -->

## Read this first (honest framing)

You asked whether the system can read your **LinkedIn messages directly**, accepting that it won't be fully compliant with LinkedIn's User Agreement since it's personal use only. Here's the accurate picture so you can decide with open eyes:

- **LinkedIn has no public/personal-messaging API.** There is no sanctioned programmatic way to read your own DMs/InMail. So "direct" reading means **browser automation of your logged-in session** — a headless or attached browser driving the real LinkedIn web app.
- **This does violate LinkedIn's User Agreement** (the prohibition on scraping/automated access), and LinkedIn actively detects automation. The realistic risk is **account restriction or permanent ban** — which is a genuine cost while you're maintaining a professional network and a warm job-search pipeline. Personal-use intent does not change LinkedIn's detection or their enforcement.
- **Lower-risk alternative that needs no automation:** the baseline build already ingests **LinkedIn email notifications**, which is officially supported and captures the message content. For most of your goal (get notified, get a draft), that already works. I'd strongly suggest running the baseline for a few weeks before adding direct access.
- **Safest "direct" middle ground:** drive the browser through **your own real Chrome via the Chrome DevTools Protocol / an attached profile** rather than a fresh automated browser, act **human-slow**, poll **infrequently** (e.g., a few times a day, not every few minutes), and **only read** — never auto-send. This minimizes (does not eliminate) detection surface. Replies stay manual: the system drafts, you paste and send inside LinkedIn yourself.

If, understanding the above, you still want it: below are the exact additions to bolt onto the build prompt. This keeps drafting/notification identical and only changes how LinkedIn messages *enter* the pipeline.

---

## Additions to paste after the main BUILD_PROMPT

> ### Extension: direct LinkedIn message ingestion (opt-in, personal use)
>
> Add a second ingestion source alongside Gmail. Keep everything downstream (classifier, drafter, notifier, store) unchanged — LinkedIn messages must flow into the **same** `Email`-like record with `source="linkedin"`.
>
> **Approach:** Playwright (Python) driving a **persistent browser context** that reuses my real, already-logged-in LinkedIn session, so no credentials are ever typed by the code and no login automation happens.
>
> **New file `src/linkedin_client.py`:**
> - Use `playwright.sync_api` with `launch_persistent_context(user_data_dir=...)` pointed at a dedicated browser profile I will log into **manually once**. The code must **never** enter my username/password — if not logged in, it should raise a clear error telling me to log in manually in that profile, and stop.
> - `fetch_new_messages()` navigates to the messaging page, reads the list of conversation threads, and for each thread newer than the last-seen marker, extracts: sender name, sender headline/title if visible, the latest message text, and a stable thread identifier (the conversation URL or thread id in the DOM).
> - Map each into the same record shape the pipeline already uses (`id`, `thread_id`, `sender`, `subject` = e.g. `"LinkedIn: <sender name>"`, `body` = message text, `source="linkedin"`). Dedup via `store` on the thread+message id exactly like Gmail.
> - **Read-only. No send, no react, no auto-anything.** There must be no code path that types into a LinkedIn message box or clicks send.
>
> **Anti-detection / good-citizen constraints (implement all):**
> - Poll LinkedIn on a **separate, slow schedule**: default once every 4–6 hours, jittered, and only during daytime hours. Make it configurable via env (`LINKEDIN_POLL_HOURS`), default 6.
> - Randomized human-like delays between DOM actions (e.g., 1.5–5s), and a small random scroll before reading. Never hammer the page.
> - Reuse one browser session; don't relaunch per poll if avoidable.
> - Hard cap: read at most the most recent ~20 threads per poll.
> - A global kill switch env var `LINKEDIN_ENABLED=false` that fully disables this path.
>
> **Reply handling for LinkedIn:** the drafter already formats LinkedIn replies as paste-ready chat text (no email formatting). For LinkedIn-source opportunities, the phone notification must include the **conversation URL** and the draft text, with an explicit note: "Open this LinkedIn thread and paste to send — the system will not send for you." Do **not** create a Gmail draft for LinkedIn-source messages.
>
> **Orchestrator change (`main.py`):** run two ingestion passes per cycle — Gmail every `POLL_MINUTES`, LinkedIn on its own slow timer — merge results, and process through the identical downstream pipeline. Guard the LinkedIn pass behind `LINKEDIN_ENABLED`.
>
> **README additions:** a clearly-labeled "⚠️ LinkedIn direct access (optional, against LinkedIn ToS — personal use, account-risk)" section documenting: how to create and manually log into the dedicated Playwright profile once; `playwright install chromium`; the new env vars; and a plain-English warning that this can get the account restricted and that the email-notification path is the safe default. Keep this section visually separated so it's an explicit, informed opt-in.
>
> **Dependencies:** add `playwright>=1.40` to requirements and document `playwright install chromium`.
>
> **Acceptance additions:**
> - [ ] `LINKEDIN_ENABLED=false` fully disables the LinkedIn path and the rest runs normally.
> - [ ] No credential entry and no send/type-into-message code path exists in `linkedin_client.py` (grep to confirm).
> - [ ] LinkedIn messages dedup correctly and reach the classifier with `source="linkedin"`.

---

## My recommendation in one line

Run the **email-notification baseline** (fully supported, zero account risk) for a few weeks; add the Playwright path only if the notification coverage genuinely isn't enough, and if so, keep it read-only, slow, jittered, and behind the kill switch — accepting that even then it breaches LinkedIn's ToS and carries a real ban risk you're choosing to take.
