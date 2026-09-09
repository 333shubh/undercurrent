[UNDERCURRENT_SPEC.md](https://github.com/user-attachments/files/32015155/UNDERCURRENT_SPEC.md)
# Undercurrent — Project Specification

> Source of truth for the product vision, architecture, intelligence model, research workflow, and implementation direction.

UNDERCURRENT
Complete Project Report — Persistent Research & Opportunity Radar
A daily automated system for discovering signals, connecting them over time, and surfacing research, project, startup, and opportunity hypotheses.

Document purpose
This document is the source-of-truth project specification for discussing, researching, designing, and eventually building Undercurrent. It intentionally describes the complete product direction rather than locking the project into a rigid implementation sequence. Individual technical decisions can be discussed with Claude or changed later without changing the core product thesis.
## 1. Product Definition
Undercurrent is a persistent research and opportunity radar. It continuously collects signals from multiple public sources, filters and deduplicates them, remembers important entities and themes over time, detects convergence and momentum, and turns the resulting evidence into a daily research-oriented digest.
The system combines two ideas:
- Daily discovery: a useful daily stream of things worth knowing, reading, researching, building, or investigating.
- Persistent intelligence: a memory layer that connects today's signals with signals from previous days so recurring patterns, emerging themes, gaps, and momentum become visible.
The important distinction is that Undercurrent is not intended to be a generic news aggregator. The system should help answer: What is changing? What is gaining momentum? Where are multiple independent signals converging? What problems remain poorly solved? What research is opening new possibilities? What might be worth investigating or building?
Core product thesis:
Collect signals → understand signals → connect signals over time → identify meaningful change → generate evidence-backed hypotheses → deliver useful decisions and next steps.
## 2. What the User Gets
The daily digest remains the primary user-facing output. Every digest is organized by what the signal means rather than simply by source.
| Section | Purpose |
| --- | --- |
| Opportunities | Gaps, underserved needs, timing signals, and openings worth investigating. |
| Research Ideas | Open questions, unexplored directions, limitations, and research opportunities. |
| Startup Ideas | Specific opportunities that plausibly have company potential, with the evidence behind them. |
| Papers to Read | Specific high-value papers, with why each paper is worth the user's time. |
| Emerging Trends | Technologies, behaviors, research areas, markets, or approaches showing meaningful momentum. |
| Project Ideas | Smaller, buildable experiments or prototypes that can validate an idea or explore a direction. |

A signal can appear in multiple sections when justified. A section can be empty. The system should never manufacture weak items merely to fill the format.
## 3. The Two-Layer Product Model
### Layer A — Daily Radar
This is the original Undercurrent concept: collect the day's strongest signals and turn them into a concise, actionable digest.
- What happened today?
- What is worth reading?
- What new research appeared?
- What are builders struggling with?
- What launched?
- What opportunities or projects are emerging?
### Layer B — Persistent Intelligence
This is the major extension. Undercurrent should not treat each daily digest as an isolated event. It should maintain memory of signals, entities, themes, problems, technologies, projects, organizations, researchers, and observations over time.
Example:
Day 1: New paper introduces a battery forecasting method.
Day 5: GitHub project implements a related approach.
Day 9: Founder discusses deployment problems in battery forecasting.
Day 14: Grid operator discussion reveals a recurring data bottleneck.
Day 18: Another paper addresses accuracy but not deployment.

Undercurrent:
- links the signals
- recognizes the recurring theme
- measures increasing activity
- identifies the deployment/data bottleneck
- distinguishes evidence from inference
- surfaces the theme as a potentially important opportunity
The daily digest tells the user what happened. The persistent layer tells the user what may be happening underneath the noise.
## 4. Intelligence Concepts
| Concept | Meaning in Undercurrent |
| --- | --- |
| Signal | A discrete piece of evidence: post, paper, launch, repository, discussion, article, or other source item. |
| Entity | A person, company, project, technology, research group, product, dataset, or other identifiable object. |
| Theme | A recurring topic or direction that connects multiple signals. |
| Problem | A pain point, bottleneck, limitation, unmet need, or recurring complaint. |
| Convergence | Independent signals from different sources pointing toward the same theme/problem/opportunity. |
| Momentum | Evidence that activity, attention, research, adoption, or builder interest is increasing over time. |
| Novelty | How meaningfully new the signal appears relative to already-known material. |
| Gap | A meaningful mismatch between demand/importance and the quality or availability of existing solutions/research. |
| Hypothesis | An explicit model-generated interpretation of what the evidence might imply. |
| Confidence | How strongly the available evidence supports a conclusion. |
| Evidence chain | The source items that support an observation or hypothesis. |

The system should avoid pretending that these scores are objective truths. They are decision-support signals and should remain explainable through the underlying evidence.
## 5. Sources
| Source | Method / Role | Notes |
| --- | --- | --- |
| Reddit | Official API via PRAW | Communities can reveal pain points, user complaints, projects, founder experiences, and early demand. |
| Medium | RSS feeds | Useful for essays, technical explanations, startup/technology discussion, and curated publications. |
| Substack | Publication RSS feeds | Useful for independent researchers, operators, investors, and domain specialists. |
| Hacker News | Firebase API | Strong source for technical launches, developer discussion, Show HN projects, and community reactions. |
| arXiv | Official API | Research signal source; initial focus can include cs.AI, cs.LG, cs.CY and later expand. |
| GitHub Trending | Permitted/defensive retrieval | Useful for detecting projects gaining developer attention; retrieval should be robust and compliant with site rules. |
| X | Read-only access where permitted | Potentially high-value source for fast-moving builders, researchers, founders, launches, pain points, and early narratives. Treat as the most fragile source and do not perform account actions. |

The source list is not sacred. The architecture should allow sources to be added, removed, or replaced without changing the intelligence model.
## 6. X / Twitter Signal Strategy
X is valuable because it can expose early-stage discussion, builders, researchers, launches, and pain points before they become polished articles. However, it is also the highest-risk and most fragile source.
The search strategy should avoid generic 'startup ideas' searches because they tend to produce recycled content. Instead, searches should target the places where useful signals naturally occur.
| Layer | What it finds | Example patterns |
| --- | --- | --- |
| Discover | People building, researching, launching, or open-sourcing | building in public; currently building; just shipped; Show HN; research prototype; just open sourced |
| Find pain | Unmet needs, broken processes, bottlenecks, and recurring complaints | why is there no; someone should build; manual process; doesn't scale; alternative to; unsolved problem; bottleneck |
| Find a business | Early traction, customer discovery, launches, founder activity | first customer; first 100 users; YC founder; just launched; demo day; 0 to $1k |

Domain modifiers can be combined with these patterns, for example AI, robotics, energy, climate tech, grid, developer tools, manufacturing, logistics, healthcare, and deep tech. The candidate set should rotate rather than repeatedly issuing the exact same search pattern.
The system must remain read-only: no likes, follows, replies, reposts, DMs, or other account actions. Authentication/session handling must follow the platform's rules and should not attempt to evade security systems or access controls.
## 7. Signal Processing Pipeline
The conceptual pipeline is:
COLLECT → NORMALIZE → DEDUPLICATE → ENRICH → UPDATE MEMORY → SCORE → SYNTHESIZE → DELIVER → RECORD
- Collect: retrieve source items.
- Normalize: convert different source formats into a common internal representation.
- Deduplicate: prevent the same item or substantially identical item from repeatedly entering the system.
- Enrich: extract entities, topics, problems, claims, and other useful metadata.
- Update memory: connect new observations to existing entities/themes and preserve historical relationships.
- Score: estimate relevance, novelty, momentum, convergence, and confidence.
- Synthesize: produce the daily digest and longer-term observations.
- Deliver: send the digest to Discord or another delivery channel.
- Record: preserve what was delivered and which evidence supported it.
## 8. Persistent Intelligence / Memory Model
The initial raw_items table is necessary but insufficient for the long-term vision. A future schema should be designed around both raw evidence and derived intelligence.
| Data object | Purpose |
| --- | --- |
| raw_items | Immutable-ish source evidence and source metadata. |
| entities | Canonical people, organizations, projects, technologies, products, papers, etc. |
| themes | Persistent concepts that can accumulate evidence over time. |
| problems | Recurring pain points, limitations, bottlenecks, or unmet needs. |
| signals | Structured interpretations of raw items. |
| relationships | Links such as item→theme, entity→theme, paper→problem, project→technology. |
| observations | Time-stamped statements about momentum, convergence, change, or recurring patterns. |
| hypotheses | Evidence-backed but explicitly inferential opportunity/research/startup hypotheses. |
| digests | Delivered daily outputs. |
| digest_items | Traceability from a digest back to the source evidence. |

The design should allow the same underlying evidence to support multiple outputs. For example, one paper can be a Paper to Read, contribute to an Emerging Trend, strengthen a Research Idea, and become evidence for an Opportunity.
## 9. Deduplication and Historical Memory
At minimum, raw source items should use a source-specific external identifier or canonical URL. The original design uses a unique (source, external_id) constraint.
raw_items
- id
- source
- external_id
- title
- url
- content_snippet
- author
- score
- collected_at
- published_at
- raw_metadata
- unique(source, external_id)
Historical memory should go beyond exact deduplication. Two different articles may describe the same underlying event or theme. The intelligence layer should therefore distinguish:
- Exact duplicate: same source item.
- Near duplicate: substantially same content or story.
- Related signal: different item supporting the same theme/problem.
- Contradictory signal: different item that challenges an existing interpretation.
This distinction is critical: the system should not mistake repeated copies of one story for independent convergence.
## 10. Reasoning Rules
Undercurrent's reasoning layer should follow these principles:
- Facts and model inference must be clearly separated.
- Important conclusions should retain links to the evidence supporting them.
- Independent sources should carry more weight than repeated copies of the same source.
- A high score should not automatically mean an idea is good; explain why the signal matters.
- Do not call something an 'emerging trend' from one weak signal.
- Do not call something a 'startup opportunity' merely because someone said 'someone should build this.'
- Look for repeated pain, new technical capability, research progress, adoption movement, and timing changes.
- Contradictory evidence should be retained rather than filtered away.
- Uncertainty should be explicit.
- The system should prefer fewer strong observations over many generic ones.
## 11. Opportunity Detection Framework
A strong opportunity hypothesis can be evaluated across several dimensions:
| Dimension | Question |
| --- | --- |
| Problem intensity | Is the problem painful, expensive, frequent, or consequential? |
| Evidence of demand | Are real users/builders/operators discussing or experiencing it? |
| Technical feasibility | Do recent research or engineering signals make a solution more plausible? |
| Solution gap | Are current solutions weak, expensive, fragmented, inaccessible, or poorly adopted? |
| Timing | Why might this be becoming possible or important now? |
| Competition | Are there existing projects or companies already addressing it? |
| Research gap | What remains technically unknown or unsolved? |
| Buildability | Can a small project test the hypothesis before a full company is required? |

Undercurrent should not declare that an opportunity is definitely viable. Its role is to surface and prioritize hypotheses worth human investigation.
## 12. Daily Digest Format
📡 UNDERCURRENT — [DATE]

🎯 Opportunities
- [Title] — [why this looks like an opening] ([evidence])

🔬 Research Ideas
- [Title] — [open question / unexplored direction] ([evidence])

🚀 Startup Ideas
- [Title] — [specific company hypothesis + why now] ([evidence])

📄 Papers to Read
- [Paper] — [what it contributes + why it matters] ([link])

📈 Emerging Trends
- [Trend] — [what is changing + evidence of momentum/convergence] ([evidence])

🛠️ Project Ideas
- [Project] — [small build/test that could validate the idea] ([evidence])

🧠 UNDERCURRENT SIGNALS
- [Important theme that strengthened/weakened today]
- [New convergence or contradiction]
- [Theme gaining/losing momentum]

[N] new/meaningful signals across [sources]
The final section is the bridge between the original daily digest and the persistent intelligence layer. It should focus on changes to the system's understanding, not generic news.
## 13. Suggested Supabase Data Model
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
  metadata jsonb,
  created_at timestamptz,
  updated_at timestamptz
)

themes(
  id uuid primary key,
  name text,
  description text,
  status text,
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
  from_type text,
  from_id uuid,
  relation text,
  to_type text,
  to_id uuid,
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
This is a conceptual schema, not a demand to implement every table immediately. Claude should refine the model where necessary while preserving the underlying concepts.
## 14. Technical Stack
| Component | Proposed technology | Role |
| --- | --- | --- |
| Runtime | Python | Collectors, processing, orchestration. |
| Database | Supabase / PostgreSQL | Raw evidence, deduplication, historical memory, derived intelligence. |
| LLM | Anthropic API / Claude | Extraction, synthesis, classification, reasoning, digest generation. |
| Scheduler | Render Cron Job or equivalent | Daily unattended execution. |
| Delivery | Discord webhook | Simple notification/interface layer. |
| Browser automation | Playwright only where appropriate | Potential X retrieval; must remain read-only and compliant. |
| Repository | Git | Version control and reproducible configuration. |

## 15. Repository Structure
undercurrent/
├── main.py
├── sources/
│   ├── reddit.py
│   ├── medium.py
│   ├── substack.py
│   ├── hackernews.py
│   ├── arxiv.py
│   ├── github_trending.py
│   └── twitter.py
├── intelligence/
│   ├── entities.py
│   ├── themes.py
│   ├── signals.py
│   ├── scoring.py
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
The exact module boundaries can change. The important separation is between source collection, persistent intelligence, orchestration, summarization, and delivery.
## 16. Configuration and Secrets
Secrets must never be committed to the repository. Use environment variables or the deployment platform's secret manager.
| Variable | Purpose |
| --- | --- |
| ANTHROPIC_API_KEY | LLM calls |
| SUPABASE_URL | Database endpoint |
| SUPABASE_SERVICE_ROLE_KEY | Server-side database access |
| REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET | Reddit API |
| DISCORD_WEBHOOK_URL | Digest delivery |
| X-related credentials/session state | Only if the chosen X access method legitimately requires them; store securely and follow platform rules. |

## 17. Reliability Requirements
- Idempotent execution: retries should not create duplicate source records or duplicate digest deliveries.
- Fail loud: source failures should be logged and surfaced rather than becoming silent zero-result runs.
- Source isolation: one broken source should not prevent other sources from producing a digest.
- Bounded LLM input: pre-filter, rank, and truncate evidence before synthesis to control cost and context size.
- Traceability: important claims should be linked to their underlying source items.
- Observability: log source counts, failures, processing time, item counts, and delivery status.
- Graceful degradation: if a source is unavailable, the digest should say so internally/logically rather than pretending the absence of data means no activity.
- Data hygiene: normalize URLs, timestamps, authors, titles, and source identifiers.
## 18. Cost Philosophy
The system should remain inexpensive during development. Free or low-cost infrastructure is preferred where practical. The main variable cost is LLM processing, so the architecture should avoid sending every raw item to Claude.
- Collect broadly but intelligently.
- Filter obvious noise before LLM processing.
- Use lightweight deterministic processing for simple tasks.
- Use LLM calls for tasks where semantic reasoning adds real value.
- Cache or persist extracted information where possible.
- Keep daily synthesis bounded.
- Measure cost per daily digest.
## 19. Safety, Compliance, and Source Access
Undercurrent should be designed around legitimate access to information. API use should follow each provider's terms, rate limits, and authentication requirements. Browser automation should not attempt to bypass CAPTCHAs, anti-bot protections, access controls, or platform restrictions.
For X in particular, the intended behavior is read-only research retrieval where permitted. The product does not need to like, follow, repost, reply, message, or otherwise act on behalf of the user's account.
The system should also avoid turning weak online discussion into overconfident claims. Evidence quality and uncertainty are part of the product.
## 20. Failure Modes to Design Against
| Failure mode | Desired response |
| --- | --- |
| News aggregation instead of insight | Prioritize cross-source relationships, change over time, and implications. |
| LLM hallucination | Require evidence references and separate facts from inference. |
| Echo chamber / duplicate stories | Track source independence and near-duplicate relationships. |
| Trend inflation | Require repeated or meaningful evidence before labeling momentum. |
| Generic startup ideas | Tie startup hypotheses to observed problems, capabilities, timing, and evidence. |
| Information overload | Rank aggressively and prefer a small number of strong findings. |
| Historical memory becoming noise | Decay or down-weight stale signals while retaining historical context. |
| Source failure | Isolate failures and alert/log them. |
| Automation fragility | Keep collectors modular and testable. |
| Rising LLM cost | Pre-filter and cap context. |

## 21. What Makes the Concept Interesting
The potentially differentiated part of Undercurrent is not the act of scraping Reddit, reading arXiv, or asking an LLM for a summary. Those components are replaceable.
The more interesting system is the persistent layer that learns the structure of the information it has observed:
- Which themes keep resurfacing?
- Which themes are accelerating?
- Which researchers, builders, companies, and projects are connected?
- Which problems repeatedly appear without strong solutions?
- Which new technical capabilities could unlock previously impractical ideas?
- Where are research activity and real-world pain converging?
- Where are people excited about something but the evidence does not support the hype?
- Which opportunity hypotheses survive repeated evidence over time?
This creates the possibility of a personal research memory that becomes more useful the longer it runs.
## 22. Human-in-the-Loop Philosophy
Undercurrent is not intended to replace judgment. It is intended to improve the user's research surface area and reduce the cost of noticing important things.
The system should therefore optimize for:
- Attention, not maximum information.
- Evidence, not confident prose.
- Hypotheses, not declarations.
- Connections, not isolated summaries.
- Longitudinal context, not only today's feed.
- Actionable next questions, not generic inspiration.
## 23. Example End-to-End Scenario
Source signals:
- arXiv: new research method
- GitHub: implementation starts gaining stars
- HN: technical discussion
- Reddit: practitioners report a deployment problem
- X: founder/researcher discussion
- Substack: specialist writes about the same emerging capability

Persistent intelligence:
Theme = [technology]
Research activity = increasing
Builder activity = increasing
Practitioner pain = recurring
Existing solutions = fragmented
Deployment bottleneck = recurring
Cross-source convergence = high

Daily output:
Emerging Trend:
"[Technology] is moving from research toward practical experimentation."

Research Idea:
"Investigate whether [approach] can solve the recurring deployment bottleneck."

Startup Idea:
"Developer/operator tooling around [bottleneck], subject to customer validation."

Project Idea:
"Build a small benchmark/prototype testing [specific hypothesis]."

Paper to Read:
"[Paper]" — selected because it explains the technical development behind the trend.

Longitudinal observation:
"This theme has accumulated independent signals across six source types over
three weeks, with practitioner pain appearing after the initial research signal."
## 24. Questions to Discuss With Claude
These are deliberately left open for future design discussion rather than pre-decided:
- What is the best representation for persistent themes and relationships?
- Which scoring functions should be deterministic versus LLM-derived?
- How should signal independence be measured?
- How should near-duplicate stories be clustered?
- How should old themes decay without losing useful historical context?
- Should embeddings/vector search be added, and if so, where?
- How should contradictions be represented?
- What is the minimum memory model that produces useful longitudinal intelligence?
- How can LLM costs be kept predictable as the database grows?
- How should the system evaluate whether its own opportunity hypotheses were useful?
- What sources should eventually be added or removed based on signal quality?
- What should become a user-facing interface if Discord eventually becomes insufficient?
These are design questions, not requirements. The product thesis should remain stable while implementation choices are explored.
## 25. Success Criteria
Undercurrent is successful if, after running for a meaningful period, it consistently helps the user notice things they would otherwise have missed and gives them useful directions to investigate.
- The daily digest contains a small number of genuinely useful signals rather than generic news.
- Important themes become clearer over multiple days.
- Repeated independent signals can be connected.
- The system can explain why a theme is gaining attention.
- Research gaps and recurring pain points are surfaced with evidence.
- Startup/project ideas are grounded in observed signals rather than generic ideation.
- The user can trace an insight back to source material.
- The system becomes more useful as its historical memory grows.
## 26. Concise Claude Code Context Prompt
Use this as a compact context-setting prompt when discussing or building the project with Claude. It intentionally avoids unnecessary prose.
I’m building “Undercurrent”.

PRODUCT:
A persistent research + opportunity radar. It collects signals from public
sources, remembers them over time, connects related signals, detects
convergence/momentum/gaps, and produces a daily actionable digest.

TWO LAYERS:
1. Daily Radar: Opportunities, Research Ideas, Startup Ideas, Papers to Read,
   Emerging Trends, Project Ideas.
2. Persistent Intelligence: signals, entities, themes, problems, relationships,
   observations, momentum, convergence, contradictions, and evidence-backed
   hypotheses over time.

CORE LOOP:
Collect → Normalize → Deduplicate → Enrich → Update Memory → Score →
Synthesize → Deliver → Record.

SOURCES:
Reddit, Medium RSS, Substack RSS, Hacker News API, arXiv API, GitHub Trending
(where permitted), and eventually X via legitimate read-only access. X must not
perform likes/follows/replies/reposts/DMs or bypass platform protections.

STACK:
Python + Supabase/Postgres + Anthropic/Claude + Discord webhook + scheduled
runtime such as Render. Playwright only where legitimately appropriate.

IMPORTANT:
This is NOT a generic news summarizer. The valuable layer is longitudinal:
“What keeps appearing?”, “what is accelerating?”, “where do independent
signals converge?”, “what remains unsolved?”, “why now?”, and “what is worth
investigating/building?”

RULES:
- Separate facts from inference.
- Keep evidence/source links for important conclusions.
- Do not confuse duplicate reporting with independent convergence.
- Prefer fewer strong findings over many weak ones.
- Never pad categories.
- Keep LLM input/cost bounded.
- Make runs idempotent.
- Isolate source failures.
- Never commit secrets.
- Preserve historical context.

DESIGN FREEDOM:
Treat the above as the product thesis, not a fixed implementation. Challenge
technical choices when useful, propose better architecture/data models, and
explain tradeoffs. Do not re-explain the product back to me unless needed.
## 27. Final Product Definition
Undercurrent is a personal intelligence system for research and opportunity discovery. It combines the immediacy of a daily signal digest with the memory of a persistent research graph. Its job is not simply to tell the user what happened; it is to help the user see what is emerging, what is converging, what is missing, and what deserves deeper investigation.
The daily digest is the interface. The accumulated evidence and relationships are the intelligence. The hypotheses are the bridge from information to action.
