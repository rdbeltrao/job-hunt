# job-hunt

A job-search pipeline you run on your own machine. It fetches postings from LinkedIn
(as you, in a real browser) and from company career pages on Greenhouse, Ashby and
Personio, scores every posting against a plain-text profile of you with
[Jev](https://jevhunt.com) (a typed decision model) through OpenRouter, keeps the reasons
behind each score, and appends new openings to a CSV you triage by hand. A Claude
session can write the "why apply" notes for the best matches and keep a dashboard
current; both are optional.

```bash
uv run python -m jobs run        # fetch -> score -> export, with a summary at the end
```

What you get per opening: title, company, location, workplace, posted date, link, full
description, a fit score from 0 to 100, a verdict (apply / maybe / skip), five typed
reasons (seniority, location, language, team type, stack fit), and room for your own
status and notes.

## Make it yours

Three files are yours and ignored by git; copy the examples and edit them:

```bash
cp profile.example.md profile.md         # who you are, what you want, what blocks you
cp searches.example.yaml searches.yaml   # your saved LinkedIn search URLs
cp companies.example.yaml companies.yaml # career pages to read directly (optional)
```

`profile.md` drives the scoring: the questions in `jobs/scoring/criteria.py` refer to its
sections (languages, workplace, preferences, stack). Write it as facts, not marketing.
`criteria.py` holds the question wording and is generic; tune it only if the scoring
disagrees with you in a way the profile cannot fix.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run playwright install chromium
uv run pytest
```

## Login (once)

The LinkedIn adapter drives a real Chromium browser as you, so sign in once:

```bash
uv run python -m jobs login
```

A browser window opens at the LinkedIn login page. Sign in there yourself (the script
never reads or types credentials, and handles 2FA the same way: you do it in the
window). The script polls for the feed page and exits once you are in. The session
lives in a persistent Chromium profile at `~/.job-hunt/browser-profile`, so later
`fetch` runs are already logged in. When LinkedIn logs you out or shows a security
checkpoint, `fetch` stops with a message telling you to run `login` again.

## Usage

```bash
uv run python -m jobs fetch
```

Runs every enabled search in `searches.yaml` (in random order) and every company in
`companies.yaml`, dedupes on `(source, source_id)`, prints a table and writes
`out/jobs.jsonl` (one JSON object per job, fields from `jobs/models.py`). LinkedIn
job ids whose details were opened are appended to `out/seen_ids.json`; the next run
skips those cards, so repeat runs only open new jobs.

Options:

- `--headed` shows the browser window while fetching (default is headless).
- `--max-pages N` overrides the per-search page cap (25 results per page, default 3).
- `--no-details` only reads the result cards, does not open any job and does not mark
  ids as seen (no description, much faster).
- `--dump-html [DIR]` saves the raw results list and detail pane HTML to `out/debug/`
  (or DIR) so a broken selector can be fixed from real markup.
- `--searches`, `--companies`, `--out`, `--seen` change the input and output paths.
- `-v` before the subcommand turns on debug logging.

Exit codes: 0 fine, 1 nothing fetched and something failed, 2 LinkedIn wants a login.

## Scoring (`python -m jobs score`)

```bash
uv run python -m jobs score            # score, verdict, seniority/location/language checks
uv run python -m jobs explain --list --out out/explain-pending.json  # then a Claude session writes the explanations
```

Needs `profile.md` at the project root (your CV summary, target roles, hard constraints
such as location and languages, preferences) and an OpenRouter key, read from
`OPENROUTER_API_KEY_JOB_SEARCH` first and `OPENROUTER_API_KEY` second. Each unscored
job in `out/jobs.jsonl` is sent with the profile and answered with five questions:
`fit` (0 to 100), `verdict` (apply / maybe / skip), `seniority_match` (below / match /
above), `location_ok` (yes / no / unclear), `language_blocker` (none / german_required /
other), `team_type` (product / platform_infra / unclear) and `stack_fit` (required_match /
desired_match / stack_agnostic / required_mismatch). The five choice answers are the
job's reasons: they are stored in the CSV next to the verdict so a skip explains itself.
The wording of those questions lives in one place,
`jobs/scoring/criteria.py`; tune it there. Results are written back into
`out/jobs.jsonl`, so `export` picks them up and a re-fetch keeps them.

Two scorers answer those same questions; `JOB_HUNT_SCORER` picks one:

- `jev` (default): Jev (`typesafe/jev-1.13`), a typed decision API behind OpenRouter's
  `/api/alpha/decisions`, built for calibrated typed answers. Needs the `typesafe`
  provider enabled in the OpenRouter account's allowed-providers setting; when it is
  not, OpenRouter answers HTTP 404 "No allowed providers are available" and the run
  falls back to the `llm` scorer for the remaining jobs, saying so. Jev's `fit` is asked
  as a `noul` probability and scaled to 0 to 100, because its public docs do not publish
  a `score` question shape.
- `llm`: an ordinary chat model through OpenRouter's chat completions endpoint with a
  strict JSON schema, temperature 0. `JOB_HUNT_SCORE_MODEL` sets the model, default
  `anthropic/claude-haiku-5.5`. Same five answers, but the scores and confidences are
  the model's own estimates, not calibrated like Jev's.

Jobs without a description are not sent anywhere: they get verdict `maybe`, no score,
and the note `no description`.

### Explanations (`python -m jobs explain`)

The `strong_matches`, `gaps`, `red_flags` and `pitch` columns are written by a Claude
session, not by an API call:

```bash
uv run python -m jobs explain --list --out out/explain-pending.json   # jobs with fit >= 60 and no explanation yet
uv run python -m jobs explain --apply out/explain-answers.json        # write the four fields back into the CSV
```

`--list` writes the profile, the writing instructions from `criteria.py` and the pending
jobs (title, company, location, workplace, score, verdict, description) as one JSON file.
Whoever writes the explanations (the scheduled Claude routine, or you) answers with
`{"jobs": [{"job_id", "strong_matches", "gaps", "red_flags", "pitch"}]}` and `--apply`
puts them into `out/jobs.csv`, leaving every other column alone.

`score --explain` and `rescore --explain` still exist as the paid alternative: they send
each pending job to an LLM through OpenRouter (`JOB_HUNT_EXPLAIN_MODEL`, default
`anthropic/claude-sonnet-5.5`, about 2 cents a job). Off unless you pass the flag.

## Dashboard and scheduled runs (optional, Claude Desktop)

The CSV is the source of truth. If you use Claude Desktop, a Claude session can turn it
into a dashboard (a "Dashboard" artifact fed by the uploaded CSV: tiles, score bands,
verdict by workplace, a sortable table with the reasons as chips, a detail panel) and a
scheduled routine can run `python -m jobs run`, write the explanations with
`jobs explain --list` / `--apply`, and re-upload the CSV. Ask Claude for both; nothing in
this repository depends on them.

## Application status (`python -m jobs status`)

```bash
uv run python -m jobs status                                        # counts, and every row that is not "new"
uv run python -m jobs status --set applied --note "Easy Apply" linkedin:4477094289
uv run python -m jobs status --set rejected --date 2026-10-25 linkedin:4477094289
```

`status` is one of `new`, `skipped`, `draft`, `applied`, `replied`, `interview`, `offer`,
`rejected`, `withdrawn`. Every change appends a dated line to `notes` (for example
`2026-10-19 applied: Easy Apply`), so the column keeps the history of each application.
Nothing else in the row is touched.

## Rescoring (`python -m jobs rescore`)

```bash
uv run python -m jobs rescore                        # every row in out/jobs.csv
uv run python -m jobs rescore --status new           # only rows you have not touched
uv run python -m jobs rescore linkedin:4479350977    # specific job_ids
```

After `profile.md` or `jobs/scoring/criteria.py` change, this scores rows already in
the CSV again and rewrites only the score columns (`score`, `verdict`,
`strong_matches`, `gaps`, `red_flags`, `pitch`). `status`, `notes` and the row order
stay as they are.

## CSV export (`python -m jobs export`)

```bash
uv run python -m jobs export            # appends to out/jobs.csv
uv run python -m jobs export --csv ~/Documents/openings.csv
```

Appends every job from `out/jobs.jsonl` whose `job_id` is not yet in the CSV, with
`status` set to `new`. Existing rows are never rewritten, so edit `status` and `notes` in
Numbers, Excel or Google Sheets (File > Import, or open the file directly) and keep the
file where it is; the next run only adds new rows below. The file is UTF-8 with a BOM so
Excel reads it correctly, and multi-line descriptions stay inside quoted cells. If the
file has a different header the export refuses to touch it. Columns:

```
job_id, first_seen, posted_date, search_name, source, title, company, location, workplace,
seniority, salary, url, description, score, verdict, seniority_match, location_ok,
language_blocker, team_type, stack_fit, strong_matches, gaps, red_flags, pitch, status, notes
```

A CSV written by an older version (without the five reason columns) is migrated in place
the first time it is read; the export says so once.

```
```

## Environment variables

Each setting is read from the process environment first (a `.env` file at the project
root, ignored by git, is loaded into it), then, on macOS, from the Keychain, where the
service name is the variable name and the account is your macOS user. On macOS secrets
belong in the Keychain; the `-w` flag without a value prompts for it, so the key never
enters shell history. On Linux use `.env`:

```
security add-generic-password -a "$USER" -s OPENROUTER_API_KEY -w
```

`OPENROUTER_API_KEY_JOB_SEARCH` is read first when set, so a key on a separate OpenRouter
account can sit next to a general one.

This block is the `.env` template; leave a secret empty to read it from the Keychain:

```
# Jev fit scoring through OpenRouter (`python -m jobs score`)
OPENROUTER_API_KEY=

# Optional: model for the explanation pass (`python -m jobs score --explain`)
JOB_HUNT_EXPLAIN_MODEL=anthropic/claude-sonnet-5.5

# Optional: scorer and fallback model (`python -m jobs score`); jev is the default, llm the fallback
JOB_HUNT_SCORER=jev
JOB_HUNT_SCORE_MODEL=anthropic/claude-haiku-5.5
```

With a variable unset, the step that needs it prints which variable is missing and is
skipped; the other steps still run. `python -m jobs run` ends with a per-step summary.

## Configuration

`searches.yaml` holds LinkedIn searches. Paste a saved LinkedIn jobs search URL from
your browser; it is opened as is, with `start=25,50,...` added for paging. Each entry
has `name`, `url`, optional `max_pages` (default 8), `enabled` (default true) and
`source`: `linkedin` (the browser adapter) or `linkedin_guest` (see below). Useful
URL params: `geoId=101282230` is Germany, `f_TPR=r86400` is "posted in the last 24
hours", `f_WT=2` is remote.

`companies.yaml` lists companies whose career pages are read through their
applicant-tracking system's public API: `ats: personio`, `greenhouse` or `ashby`, with
the `slug` from the company's career-page URL. A board lists every opening, so only
titles containing one of `title_keywords` (case-insensitive) are kept; `title_keywords:
[]` keeps everything. The fetch output says how many titles each board dropped.

## Sources

### LinkedIn (logged-in browser)

`jobs/adapters/linkedin_browser.py` opens each search URL in the persistent Chromium
profile, scrolls the results list so every card renders, reads the cards (job id,
title, company, location, posted date), then clicks each new card and reads the
detail pane (description, workplace type, seniority, employment type, salary when
shown). Pages are walked with the `start` URL parameter up to `max_pages`.

Pacing is deliberately human: a random 1.5 to 4 second pause between cards, 5 to 10
seconds between pages, searches in random order, and ids already in
`out/seen_ids.json` are never reopened. The volume this is built for is small (a few
titles, a few cities, 1 to 3 pages each, roughly 20 new jobs a day), which is about
what a person browsing would do. Keep it that way: no parallel runs, no cron every
hour. Scraping is against LinkedIn's terms of service, and this runs on your own
account; the pacing and volume limits are what keep the risk low.

If LinkedIn redirects to a login, checkpoint or authwall page, the run stops at once
with exit code 2 and a message to run `login` again. It never retries that.

LinkedIn changes its page markup regularly. The parsers (`parse_cards`,
`parse_detail`) carry several fallback selectors each; when a field comes back empty
after a LinkedIn redesign, run `fetch --dump-html --max-pages 1` and fix the
selectors against the saved HTML, then turn that HTML into a test fixture.

### LinkedIn guest endpoints (disabled)

`jobs/adapters/linkedin_guest.py` is the earlier, logged-out approach using the
unofficial `/jobs-guest/jobs/api/...` endpoints. It is kept for reference and wired
to `source: linkedin_guest`, disabled in `searches.yaml`: LinkedIn answers HTTP 429
after a handful of detail requests (4 of 10 in the first real run), so most jobs come
back without a description. The adapter backs off once on 429 and stops, never retries.

### Personio

Every Personio-hosted career page publishes an XML feed at
`https://{slug}.jobs.personio.de/xml`. That feed is stable and public; the adapter
parses it with the standard library and turns the HTML job description sections into
plain text.

### Greenhouse

`https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` is Greenhouse's
public job-board JSON, no key needed. It carries the HTML description (escaped once
more inside the JSON), the location, departments, offices and publication date; a
"Management Level" metadata value, when the company sets one, becomes `seniority`.

### Ashby

`https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` is
Ashby's public job-board JSON. It has a plain-text description, an explicit
`workplaceType` (Remote, Hybrid, OnSite), primary and secondary locations, the
publication date and, when the company publishes it, a salary summary. Unlisted
postings are skipped.

## Layout

```
jobs/models.py              Job dataclass and workplace guess
jobs/browser.py             persistent Chromium profile, login wait, login-wall detection
jobs/http.py                PoliteClient for plain HTTP sources: User-Agent, random sleep, 429 handling
jobs/html_text.py           HTML to plain text helpers
jobs/seen.py                out/seen_ids.json store
jobs/store.py               out/jobs.jsonl read/write (hand-off between fetch, score, export)
jobs/settings.py            env vars, .env loading
jobs/export.py              CSV export (out/jobs.csv, append-only)
jobs/scoring/criteria.py    the questions and wording Jev and Claude get; tune here
jobs/scoring/jev.py         Jev scorer (OpenRouter decisions endpoint) and shared result type
jobs/scoring/llm.py         chat-model scorer (OpenRouter chat completions, default)
jobs/scoring/explain.py     LLM explanation pass (OpenRouter)
jobs/adapters/linkedin_browser.py   logged-in Playwright adapter (default for LinkedIn)
jobs/adapters/linkedin_guest.py     guest-endpoint adapter, disabled
jobs/adapters/personio.py
jobs/adapters/greenhouse.py         Greenhouse job-board JSON adapter
jobs/adapters/ashby.py              Ashby job-board JSON adapter
jobs/config.py              searches.yaml / companies.yaml loaders
jobs/cli.py                 login, fetch, score, export, run
tests/                      fixtures and fakes only: no network, no browser, no API keys
```
