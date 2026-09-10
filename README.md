# Undercurrent

A headless daily research radar. It collects from eight public sources, dedupes
and scores them deterministically, tracks themes over time, and posts one
Discord digest a day.

There is no UI, no dashboard and no read API. The only output is the Discord
message. `app.py` exposes a single internal trigger endpoint so a scheduler can
start a run; it is not a user-facing service.

Target cost: **$0/month.** Every component runs on a genuine free tier.

Full design rationale lives in [SPEC.md](SPEC.md). This file covers what it is,
how to run it, and what is currently true about each source.

---

## What it actually does

```
COLLECT → NORMALIZE → DEDUPLICATE → ENRICH → UPDATE MEMORY
        → SCORE → SYNTHESIZE → DELIVER → RECORD
```

The design rule that shapes everything: **deterministic code decides what
matters; the LLM only writes the sentence.** Novelty, relevance, momentum,
confidence and decay are arithmetic in `intelligence/scoring.py`. By the time a
model is called, the shortlist is already fixed and every entry carries its
evidence links. A model that is unavailable degrades the digest's prose — it
never changes what was selected, and never cancels the run.

That ordering is what separates this from a news aggregator:

- **Near-duplicates are not independent evidence.** Union-find clusters the same
  story across sources and across the trailing 30 days. A second copy scores low
  on novelty but still raises confidence.
- **One loud source is capped.** A single source, however large its engagement
  numbers, cannot exceed `SINGLE_SOURCE_CONFIDENCE_CAP`.
- **Momentum is measured against a theme's own baseline**, so a permanently busy
  theme never looks like it is accelerating.
- **Convergence requires distinct clusters**, so a syndicated story cannot
  masquerade as independent agreement.
- **Contradictions are recorded, not filtered out.**
- **Sections may be empty.** Nothing is padded to fill the format.

## Sources

Status verified against live APIs on 2026-09-10.

| Source | Method | Auth | Status |
|---|---|---|---|
| Hacker News | Firebase REST | none | working — 115 fetched, 103 kept |
| arXiv | export API, category filters | none | working — 150 → 123 |
| Substack | RSS, curated publication list | none | working — 111 → 109 |
| daily.dev | public GraphQL `mostUpvotedFeed` | none | working — 50 → 30 |
| Medium | RSS tag feeds | none | working — 70 → 55 |
| GitHub | Search API, `created:>` as trending proxy | free PAT | working — 30 req/min |
| Reddit | `praw`, script app | client id + secret | **needs credentials** |
| X | v2 `tweets/search/recent` | bearer token | **not available at $0** |

Two honest caveats:

- **Reddit needs credentials and has no usable fallback.** Its public RSS feeds
  exist but are throttled to the point of uselessness — measured at 12-second
  spacing, only 1 of 5 subreddits returned data and the rest were HTTP 429. From
  a datacenter IP it would be worse. A script app takes two minutes to create
  ([reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) → create app →
  type **script**) and is the only real option.
- **X v2 reads are no longer free.** Every endpoint returns 402 "credits
  depleted", including a bare user lookup. The collector reports this as a skip
  rather than an error so it does not mark every run as degraded. Restoring it
  costs money and breaks the $0 target.

A source that cannot run skips itself and logs why. It never blocks the digest.

## LLM budget

Batched and capped, because free tiers are metered per-minute *and* per-day:

| Purpose | Batching | Cap per run |
|---|---|---|
| Extraction / classification | 25 items per call | 12 calls (300 items) |
| Entity disambiguation | only for genuine ambiguity | 2 calls |
| Digest synthesis | one call for the whole digest | 1 call |
| | | **15 requests/day** |

With both `GROQ_API_KEY` and `GEMINI_API_KEY` set, calls round-robin across
providers and fail over on quota, rate-limit, retired-model and transient
network errors — two free tiers behave like one larger allowance. Groq leads the
ring: measured on the extraction prompt it answers in ~1.5s against Gemini's
~13.7s.

Anything past the extraction cap falls back to a deterministic keyword
classifier. With no key configured at all, the whole pipeline still runs.

## Setup

### 1. Database

Create a Supabase project, then run [`db/schema.sql`](db/schema.sql) in the SQL
editor. Everything is `create ... if not exists`, so it is safe to re-run.

The schema enables RLS on all 15 tables with **no policies**. That is
deliberate: the pipeline authenticates with the service_role key, which bypasses
RLS, while Supabase's anon key is public by design. Without this, anyone holding
the anon key could read and rewrite the entire research memory. The linter
reports these as INFO "RLS enabled, no policy" — that is the intended end state.

### 2. Environment

Copy `.env.example` to `.env` and fill it in. Required: `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, `DISCORD_WEBHOOK_URL`, `RENDER_TRIGGER_TOKEN`, and
at least one LLM key.

```bash
pip install -r requirements.txt
python -c "import secrets; print(secrets.token_urlsafe(32))"   # RENDER_TRIGGER_TOKEN
```

### 3. Run it

```bash
python main.py --no-deliver --print   # full run, prints the digest, posts nothing
python main.py                        # full run, posts to Discord
python main.py --force                # regenerate even if today was delivered
```

A full run takes roughly 5–6 minutes, most of it collection.

### 4. Deploy

Push to GitHub, create a Render **free web service** from `render.yaml`, and set
the env vars it prompts for. Then add two GitHub Actions repository secrets:

- `RENDER_SERVICE_URL` — e.g. `https://undercurrent.onrender.com`
- `RENDER_TRIGGER_TOKEN` — must match the service's env var exactly

`.github/workflows/trigger-daily.yml` fires at 07:15 UTC. It pings `/healthz`
first to absorb Render's cold start, then POSTs `/trigger`, which returns 202 and
runs the pipeline in the background.

## Reliability

- **Idempotent.** `raw_items` upsert on `(source, external_id)`, the digest
  upserts on `digest_date`, and delivery checks a `delivered` flag. Re-running a
  day duplicates nothing and does not re-post.
- **Isolated failures.** A collector never raises past its own boundary.
- **Loud.** Source counts, failures, LLM usage, timing and delivery status land
  in `run_logs` / `source_runs` / `llm_usage`. A zero-item source is reported as
  an error, not as a quiet day. An outright run failure posts a notice to
  Discord rather than going silent.
- **One run at a time.** A concurrent trigger gets 409 instead of double-spending
  the LLM budget.

## Tests

```bash
python -m pytest tests/ -q
```

131 tests, all offline — no DB, no network, no LLM. They cover the deterministic
core and the rules it is supposed to enforce: that one loud source is not
confidence, that a busy theme is not momentum, that a syndicated story is not
convergence, and that a stranger cannot trigger a run.

`tests/test_themes.py::TestLabelSimilarityCalibration` holds the measured
evidence behind `THEME_MATCH_THRESHOLD`. Retune against that test, not by feel.

## Layout

```
main.py              pipeline orchestration
app.py               single trigger endpoint for the scheduler
summarize.py         the one synthesis call per day + digest rendering
deliver.py           Discord webhook, splitting, idempotency
normalize.py         shared normalization + the one similarity method
config.py            secrets from env, tuning constants in code
sources/             eight collectors + shared plumbing
intelligence/
  signals.py         dedup clustering, batched classification
  themes.py          linkage, momentum, decay, observations
  scoring.py         deterministic scoring (no LLM)
  hypotheses.py      Section 8 dimension scoring, evidence gates
  entities.py        entity resolution
  llm.py             provider rotation, rate limiting, budget
db/                  schema + Supabase client
```
