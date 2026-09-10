[SPEC.md](https://github.com/user-attachments/files/32047551/SPEC.md)
# Undercurrent — Project Spec

Persistent research/opportunity radar. Collects signals from public sources, dedupes them, tracks entities/themes/problems over time, detects convergence and momentum, outputs a daily digest.

Not a news aggregator. Optimize for: fewer strong findings > many weak ones. Evidence-linked claims > confident prose. Facts separated from inference.

Loop: `COLLECT → NORMALIZE → DEDUPLICATE → ENRICH → UPDATE MEMORY → SCORE → SYNTHESIZE → DELIVER → RECORD`

Target cost: **$0/month.** Every component below has a genuine free tier. Anthropic/Claude API is NOT free at volume — not used in the automated pipeline. See Stack + Cost sections.

---

## 1. Digest output format

```
📡 UNDERCURRENT — [DATE]

🎯 Opportunities
- [Title] — [why this looks like an opening] ([evidence])

🔬 Research Ideas
- [Title] — [open question] ([evidence])

🚀 Startup Ideas
- [Title] — [company hypothesis + why now] ([evidence])

📄 Papers to Read
- [Paper] — [what it contributes] ([link])

📈 Emerging Trends
- [Trend] — [what's changing + momentum evidence] ([evidence])

🛠️ Project Ideas
- [Project] — [small build/test that validates the idea] ([evidence])

🧠 UNDERCURRENT SIGNALS
- [theme that strengthened/weakened today]
- [new convergence or contradiction]
- [theme gaining/losing momentum]

[N] new/meaningful signals across [sources]
```

Rules: a signal can appear in multiple sections. A section can be empty. Never pad a section to fill the format. The SIGNALS block reflects change in the system's understanding, not generic news.

## 2. Sources — free-tier implementation

| Source | Method | Free cap | Notes |
|---|---|---|---|
| Reddit | `praw`, free script app (client id/secret from reddit.com/prefs/apps). `.new()` + `.top(time_filter="day")` on configured subreddit list. | ~100 req/min | |
| Medium | RSS: `medium.com/feed/<tag-or-publication>`, no auth | none material | |
| Substack | RSS: `<pub>.substack.com/feed`, no auth. Maintain curated slug list in config. | none material | |
| Hacker News | Firebase REST: `/v0/topstories.json`, `/v0/newstories.json`, `/v0/item/<id>.json`, no auth | none material | |
| arXiv | `export.arxiv.org/api/query`, category filters (`cat:cs.AI` etc.), no auth | 1 req/~3s | |
| GitHub | Search API `/search/repositories?q=created:>...` as trending proxy (no official trending API) | 10 req/min unauth, 30 req/min with free PAT | use free token |
| X | Official free API tier, `v2/tweets/search/recent` | small monthly read cap, changes over time — check current limit before relying on it | run 1–3 rotating queries/day max (Section 3), not the full pattern set in one run. No unofficial scrapers — ToS violation, breaks pipeline. |

Every collector logs `(source, items_fetched, items_after_filter, errors)` per run — a source silently returning zero items must be visible immediately.

## 3. X query patterns (rotate daily, don't repeat exact queries)

| Layer | Finds | Patterns |
|---|---|---|
| Discover | builders/researchers/launches | building in public; currently building; just shipped; Show HN; research prototype; just open sourced |
| Find pain | unmet needs, bottlenecks | why is there no; someone should build; manual process; doesn't scale; alternative to; unsolved problem; bottleneck |
| Find a business | early traction, launches | first customer; first 100 users; YC founder; just launched; demo day; 0 to $1k |

Combine with domain modifiers (AI, robotics, energy, climate, grid, devtools, manufacturing, logistics, healthcare, deep tech). Read-only: no likes/follows/replies/reposts/DMs. No CAPTCHA/anti-bot bypass.

## 4. Data model (Supabase/Postgres)

```sql
raw_items(
  id uuid primary key,
  source text not null,
  external_id text not null,
  title text,
  url text,
  content_snippet text,
  author text,
  score numeric,
  collected_at timestamptz,
  published_at timestamptz,
  raw_metadata jsonb,
  unique(source, external_id)
)

entities(
  id uuid primary key,
  entity_type text,
  canonical_name text,
  description text,
  status text,              -- 'unconfirmed' | 'confirmed'
  metadata jsonb,
  created_at timestamptz,
  updated_at timestamptz
)

entity_aliases(
  id uuid primary key,
  entity_id uuid references entities(id),
  alias text not null,
  unique(alias)
)

themes(
  id uuid primary key,
  name text,
  description text,
  status text,
  momentum_score numeric default 0,
  last_signal_at timestamptz,
  created_at timestamptz,
  updated_at timestamptz
)

problems(
  id uuid primary key,
  description text,
  domain text,
  created_at timestamptz,
  updated_at timestamptz
)

signals(
  id uuid primary key,
  raw_item_id uuid references raw_items(id),
  signal_type text,
  novelty numeric,
  relevance numeric,
  confidence numeric,
  metadata jsonb,
  created_at timestamptz
)

relationships(
  id uuid primary key,
  from_type text, from_id uuid,
  relation text,
  to_type text, to_id uuid,
  confidence numeric,
  evidence jsonb,
  created_at timestamptz
)

observations(
  id uuid primary key,
  theme_id uuid references themes(id),
  observation_type text,
  statement text,
  score numeric,
  observed_at timestamptz,
  evidence jsonb
)

hypotheses(
  id uuid primary key,
  hypothesis_type text,
  statement text,
  confidence numeric,
  status text,
  created_at timestamptz,
  updated_at timestamptz
)

digests(
  id uuid primary key,
  digest_date date unique,
  content text not null,
  item_count int,
  created_at timestamptz
)

digest_items(
  digest_id uuid references digests(id),
  raw_item_id uuid references raw_items(id),
  primary key(digest_id, raw_item_id)
)
```

Refine this schema as needed during implementation — concepts matter more than exact column list.

## 5. Dedup and entity resolution

**Dedup types**: exact duplicate (same item) / near duplicate (same story) / related signal (different item, same theme) / contradictory signal (challenges existing interpretation). Do not count near-duplicates as independent convergence.

**Entity resolution** (deterministic first, LLM fallback only):
1. Normalize candidate name: lowercase, strip suffixes (Inc., Labs, .ai), collapse whitespace.
2. Match against `entities.canonical_name` and `entity_aliases.alias` — exact match first, then fuzzy (trigram/Levenshtein) above a threshold.
3. If ambiguous (multiple candidates above threshold), one LLM disambiguation call.
4. New entity → `status='unconfirmed'` until a second independent source references it.
5. Store every alias seen for cheaper future exact matches.

## 6. Scoring (all deterministic — no LLM calls for these)

- **Novelty** = `1 - max_similarity(item, raw_items/signals from last N days)`. Pick one similarity method (embedding or keyword-overlap) and keep it consistent.
- **Relevance** = weighted match against configured domain/interest keywords + boost if linked to existing theme/problem.
- **Momentum** = count of independent-source signals on a theme in trailing 7/14/30-day windows vs. that theme's own trailing baseline. "Gaining momentum" requires exceeding baseline by a set margin, not just >0.
- **Confidence** = f(evidence count, source independence, recency). Single source, however loud, caps confidence at a modest ceiling regardless of engagement numbers.
- **Decay**: `momentum *= 0.5 ** (days_since_last_signal / half_life_days)`, half_life_days configurable per theme type. Decayed themes stay queryable, just stop competing for digest space.

LLM calls are reserved for: extraction/classification (bulk, free-tier model) and final digest synthesis (one call/day). Not for computing the scores above.

## 7. Reasoning rules

- Facts and inference clearly separated.
- Conclusions retain evidence links.
- Independent sources outweigh repeated copies of one source.
- Don't call something an "emerging trend" from one weak signal.
- Don't call something a "startup opportunity" from one "someone should build this" post.
- Contradictory evidence retained, not filtered out.
- Uncertainty stated explicitly.

## 8. Opportunity evaluation dimensions

| Dimension | Question |
|---|---|
| Problem intensity | painful/expensive/frequent/consequential? |
| Evidence of demand | real users/builders/operators discussing it? |
| Technical feasibility | recent research/engineering makes it more plausible? |
| Solution gap | current solutions weak/expensive/fragmented/inaccessible? |
| Timing | why now? |
| Competition | existing projects/companies already on it? |
| Research gap | what's technically unknown? |
| Buildability | can a small project test the hypothesis first? |

Undercurrent surfaces and ranks hypotheses. It does not declare an opportunity "viable."

## 9. Stack ($0 target)

| Component | Technology | Free-tier limit to respect |
|---|---|---|
| Runtime | Python | — |
| Backend host | Render free Web Service — pipeline runs behind one HTTP endpoint (`app.py`) | spins down after ~15min idle, cold-starts on next request. Do NOT use Render's paid Cron product. |
| Scheduler | GitHub Actions scheduled workflow (or cron-job.org) sends 1 HTTP request/day to the Render endpoint | GH Actions free on public repos, ~2000 free min/mo private |
| Database | Supabase free tier (Postgres) | ~500MB; project auto-pauses after ~1wk idle — the daily job's own DB read/write is the keep-alive |
| LLM (bulk) | Gemini free tier or Groq free tier (open models) | per-minute/per-day request caps — batch calls, log usage |
| LLM (synthesis) | Same free-tier model for v1 | Anthropic/Claude is an optional later upgrade, not a default dependency |
| Delivery | Discord webhook | none material |
| Repo | GitHub | free |

## 10. Repo structure

```
undercurrent/
├── .github/workflows/trigger-daily.yml   # scheduler → pings Render endpoint
├── app.py                                 # HTTP wrapper around main.py, for Render
├── main.py
├── sources/
│   ├── reddit.py
│   ├── medium.py
│   ├── substack.py
│   ├── hackernews.py
│   ├── arxiv.py
│   ├── github_search.py
│   └── twitter.py
├── intelligence/
│   ├── entities.py       # includes entity resolution (Section 5)
│   ├── themes.py
│   ├── signals.py
│   ├── scoring.py         # deterministic scoring + decay (Section 6)
│   └── hypotheses.py
├── db/
│   ├── client.py
│   └── schema.sql
├── summarize.py
├── deliver.py
├── config.py
├── requirements.txt
├── render.yaml
└── README.md
```

## 11. Config / secrets

| Variable | Purpose | Where it lives |
|---|---|---|
| SUPABASE_URL | DB endpoint | Render env vars + local `.env` |
| SUPABASE_SERVICE_ROLE_KEY | server-side DB access | Render env vars (never commit) |
| REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET | Reddit API | Render env vars |
| GEMINI_API_KEY or GROQ_API_KEY | LLM calls | Render env vars |
| GITHUB_TOKEN | raises Search API rate limit | Render env vars |
| X_BEARER_TOKEN | X free-tier API | Render env vars |
| DISCORD_WEBHOOK_URL | digest delivery | Render env vars |
| ANTHROPIC_API_KEY | optional synthesis upgrade, omit for $0 build | Render env vars, if used |
| RENDER_TRIGGER_TOKEN | shared secret so `app.py`'s trigger endpoint can't be hit by randoms | Render env vars + GitHub Actions repo secret |

Never commit secrets to the repo.

## 12. Reliability

- Idempotent runs — retries don't duplicate raw_items rows or digest deliveries.
- One broken source doesn't block the rest of the digest.
- Fail loud — log and surface source failures, don't silently return zero results.
- Pre-filter/rank/truncate evidence before it hits the LLM synthesis call.
- Log source counts, failures, processing time, delivery status per run.
- Normalize URLs, timestamps, authors, titles across sources.
- Allow 30–60s timeout on the trigger call for Render cold starts.

## 13. Failure modes

| Failure mode | Response |
|---|---|
| News aggregation instead of insight | prioritize cross-source relationships and change over time |
| LLM hallucination | require evidence refs, separate fact from inference |
| Echo chamber / duplicate stories | track source independence + near-dup relationships |
| Trend inflation | require repeated evidence before labeling momentum |
| Generic startup ideas | tie hypotheses to observed problem/capability/timing evidence |
| Information overload | rank aggressively, prefer few strong findings |
| Stale memory becoming noise | apply decay (Section 6) |
| Source failure | isolate + log |
| Rising LLM cost | pre-filter, cap context, use free-tier model |

## 14. Open questions (resolve during build, don't block on them)

- Best representation for theme/relationship graph — plain tables vs. graph extension?
- Similarity method for novelty/dedup — embeddings vs. keyword overlap — pick one for v1.
- Should vector search be added, and where?
- How to evaluate whether past hypotheses were actually useful?

## 15. Success criteria

Digest has a small number of genuinely useful signals, not generic news. Themes get clearer over days. Independent signals connect. System explains *why* a theme is gaining attention. Every claim traces back to source material. System gets more useful as memory accumulates.
