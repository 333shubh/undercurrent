"""Central config: secrets from env, tuning constants in code.

Nothing here reads a secret at import time in a way that crashes the process --
a missing key disables the source that needs it and gets logged (Section 12,
"one broken source doesn't block the rest of the digest"). The only hard
requirements are the DB and the Discord webhook, checked in main.py.
"""

from __future__ import annotations

import os

try:  # local dev convenience; absent on Render, where env vars are real
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


# --------------------------------------------------------------- secrets --

SUPABASE_URL = _env("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = _env("SUPABASE_SERVICE_ROLE_KEY")

REDDIT_CLIENT_ID = _env("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = _env("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = _env("REDDIT_USER_AGENT", "undercurrent/0.1 (research radar)")

GITHUB_TOKEN = _env("GITHUB_TOKEN")
X_BEARER_TOKEN = _env("X_BEARER_TOKEN")

GEMINI_API_KEY = _env("GEMINI_API_KEY")
GROQ_API_KEY = _env("GROQ_API_KEY")

DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL")

# Public server invite, shown as the join button on the landing page. Delivery
# is Discord-only: readers join the server rather than subscribing, so there is
# no subscriber list, no opt-in flow and no per-recipient sending cost. Unset
# renders a disabled placeholder instead of a dead link.
DISCORD_INVITE_URL = _env("DISCORD_INVITE_URL")
RENDER_TRIGGER_TOKEN = _env("RENDER_TRIGGER_TOKEN")

# ------------------------------------------------------------ LLM budget --
#
# Free tiers are capped per-minute AND per-day (Section 9). Everything the LLM
# touches is batched: extraction runs over BATCH_SIZE items per call, with a
# hard ceiling of MAX_EXTRACTION_CALLS_PER_RUN, plus exactly one synthesis call
# per day. Worst case: 12 + 1 + a couple of disambiguations = ~15 calls/day,
# rate-limited to LLM_MAX_RPM. Gemini flash free tier is ~15 RPM / ~200 RPD;
# Groq free tier is ~30 RPM / much higher RPD. Both fit with room to spare.

LLM_PROVIDER = _env("LLM_PROVIDER", "auto")  # auto | gemini | groq
#
# Model ids verified against both live APIs on 2026-09-10. The previous
# defaults (gemini-2.0-flash, llama-3.3-70b-versatile) had both been retired and
# returned 404 -- for an unattended daily job a stale model id means no digest,
# so these are worth re-checking whenever a run logs an LLM 404.
#
# Groq is listed first in the rotation: measured on the extraction prompt it
# answered in ~1.5s against Gemini's ~13.7s, and its free daily allowance is the
# larger of the two.
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.6-flash")
GROQ_MODEL = _env("GROQ_MODEL", "openai/gpt-oss-120b")

# Round-robin across every configured provider rather than draining one. Each
# call starts at the next provider in the ring and falls through to the others
# on quota/rate-limit/transient errors, so two free tiers behave like one
# larger one and a single provider outage does not cost the day's digest.
LLM_ROTATE_PROVIDERS = _env("LLM_ROTATE_PROVIDERS", "1") not in ("0", "false", "no")

LLM_BATCH_SIZE = _env_int("LLM_BATCH_SIZE", 25)
LLM_MAX_EXTRACTION_CALLS = _env_int("LLM_MAX_EXTRACTION_CALLS", 12)
LLM_MAX_DISAMBIGUATION_CALLS = _env_int("LLM_MAX_DISAMBIGUATION_CALLS", 2)
LLM_MAX_RPM = _env_int("LLM_MAX_RPM", 10)
LLM_TIMEOUT_S = _env_int("LLM_TIMEOUT_S", 90)

# Gemini 3.x bills its internal reasoning against the output budget, and on a
# structured extraction prompt that reasoning can outweigh the JSON by ~9x. At
# 4096 a full synthesis prompt truncated mid-object and parsed to nothing --
# a silent loss of the day's prose. Sized to leave room for both.
LLM_MAX_OUTPUT_TOKENS = _env_int("LLM_MAX_OUTPUT_TOKENS", 16384)

# ------------------------------------------------------------- collection --

HTTP_TIMEOUT_S = _env_int("HTTP_TIMEOUT_S", 20)
USER_AGENT = _env("USER_AGENT", "undercurrent/0.1 (+https://github.com/undercurrent)")

# Per-source fetch ceilings. Deliberately modest: the pipeline optimizes for
# "fewer strong findings", and every extra item costs LLM budget downstream.
REDDIT_SUBREDDITS = [
    "MachineLearning",
    "LocalLLaMA",
    "singularity",
    "robotics",
    "energy",
    "climate",
    "devops",
    "programming",
    "startups",
    "SaaS",
    "ycombinator",
    "hardware",
    "manufacturing",
    "logistics",
    "healthIT",
]
REDDIT_LIMIT_PER_SUB = _env_int("REDDIT_LIMIT_PER_SUB", 12)
REDDIT_MIN_SCORE = _env_int("REDDIT_MIN_SCORE", 15)

MEDIUM_FEEDS = [
    "https://medium.com/feed/tag/artificial-intelligence",
    "https://medium.com/feed/tag/machine-learning",
    "https://medium.com/feed/tag/robotics",
    "https://medium.com/feed/tag/climate-tech",
    "https://medium.com/feed/tag/developer-tools",
    # towards-data-science removed 2026-09-10: the publication left Medium, its
    # feed's newest entry was 2025-02-03. A dead feed is worse than a missing
    # one -- it returns 200 and looks healthy while contributing nothing.
    "https://medium.com/feed/tag/llm",
    "https://medium.com/feed/tag/energy",
]

SUBSTACK_SLUGS = [
    "www.interconnects",
    "thezvi",
    "importai",
    "newsletter.pragmaticengineer",
    "www.construction-physics",
    "jack-clark",
    "semianalysis",
]

HN_STORY_LIMIT = _env_int("HN_STORY_LIMIT", 60)
HN_MIN_SCORE = _env_int("HN_MIN_SCORE", 25)

ARXIV_CATEGORIES = [
    "cs.AI",
    "cs.LG",
    "cs.CL",
    "cs.RO",
    "cs.SE",
    "eess.SY",
]
ARXIV_MAX_RESULTS = _env_int("ARXIV_MAX_RESULTS", 25)
ARXIV_REQUEST_DELAY_S = 3.0  # export.arxiv.org: 1 request per ~3s

GITHUB_QUERIES = [
    "created:>{since} stars:>15 topic:llm",
    "created:>{since} stars:>15 topic:agents",
    "created:>{since} stars:>10 topic:robotics",
    "created:>{since} stars:>10 topic:climate",
    "created:>{since} stars:>25 language:python",
]
GITHUB_PER_QUERY = _env_int("GITHUB_PER_QUERY", 10)
GITHUB_LOOKBACK_DAYS = _env_int("GITHUB_LOOKBACK_DAYS", 7)

# daily.dev: public GraphQL, no auth. An eighth source beyond SPEC.md Section 2,
# added on request -- it covers practitioner-facing engineering blogs the other
# seven miss. Its popular feed skews to consumer dev content, so the upvote
# floor is deliberately high: this source should contribute a few strong items,
# not bulk.
DAILYDEV_PERIOD = _env_int("DAILYDEV_PERIOD", 1)
DAILYDEV_LIMIT = _env_int("DAILYDEV_LIMIT", 50)
DAILYDEV_MIN_UPVOTES = _env_int("DAILYDEV_MIN_UPVOTES", 20)
DAILYDEV_MAX_AGE_DAYS = _env_int("DAILYDEV_MAX_AGE_DAYS", 10)

X_QUERIES_PER_RUN = _env_int("X_QUERIES_PER_RUN", 2)  # Section 3: 1-3/day max
X_RESULTS_PER_QUERY = _env_int("X_RESULTS_PER_QUERY", 20)

# Section 3: query layers x domain modifiers. Rotated via x_query_state so the
# exact query string is not repeated day to day.
X_QUERY_LAYERS = {
    "discover": [
        "building in public",
        "currently building",
        "just shipped",
        "research prototype",
        "just open sourced",
    ],
    "pain": [
        "why is there no",
        "someone should build",
        "manual process",
        "doesn't scale",
        "alternative to",
        "unsolved problem",
        "bottleneck",
    ],
    "business": [
        "first customer",
        "first 100 users",
        "just launched",
        "0 to $1k",
    ],
}
X_DOMAIN_MODIFIERS = [
    "AI",
    "robotics",
    "energy",
    "climate",
    "grid",
    "devtools",
    "manufacturing",
    "logistics",
    "healthcare",
    "deep tech",
]

# ---------------------------------------------------------------- scoring --

# Section 6 relevance: weighted keyword match against configured interests.
DOMAIN_KEYWORDS = {
    3.0: [
        "agent", "agents", "llm", "inference", "fine-tune", "rlhf", "evaluation",
        "robotics", "manipulation", "autonomy", "grid", "battery", "geothermal",
        "fusion", "carbon capture", "biomanufacturing", "materials",
    ],
    2.0: [
        "benchmark", "dataset", "open source", "open-source", "toolchain",
        "compiler", "simulation", "supply chain", "logistics", "warehouse",
        "diagnostics", "clinical", "sensor", "edge compute", "latency",
        "throughput", "cost per", "bottleneck",
    ],
    1.0: [
        "startup", "founder", "launch", "funding", "research", "paper",
        "deployment", "production", "scaling", "workflow", "automation",
    ],
}

NOVELTY_LOOKBACK_DAYS = _env_int("NOVELTY_LOOKBACK_DAYS", 30)
NEAR_DUP_THRESHOLD = float(_env("NEAR_DUP_THRESHOLD", "0.72"))
ENTITY_FUZZY_THRESHOLD = float(_env("ENTITY_FUZZY_THRESHOLD", "0.88"))
ENTITY_AMBIGUOUS_MARGIN = float(_env("ENTITY_AMBIGUOUS_MARGIN", "0.04"))

# Confidence ceiling for a claim backed by exactly one source (Section 6:
# "single source, however loud, caps confidence at a modest ceiling").
SINGLE_SOURCE_CONFIDENCE_CAP = 0.45

MOMENTUM_WINDOWS = (7, 14, 30)
MOMENTUM_MARGIN = float(_env("MOMENTUM_MARGIN", "1.4"))  # must beat baseline by 40%
THEME_HALF_LIFE_DAYS = {
    "general": 14.0,
    "research": 30.0,
    "product": 10.0,
    "market": 21.0,
}

# ----------------------------------------------------------------- digest --

DIGEST_MAX_PER_SECTION = _env_int("DIGEST_MAX_PER_SECTION", 4)
DIGEST_EVIDENCE_ITEMS = _env_int("DIGEST_EVIDENCE_ITEMS", 60)  # cap before synthesis
DIGEST_TIMEZONE = _env("DIGEST_TIMEZONE", "UTC")

LOG_LEVEL = _env("LOG_LEVEL", "INFO")
