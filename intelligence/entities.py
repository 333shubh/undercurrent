"""Entity resolution (Section 5) -- deterministic first, LLM only to break ties.

The resolution ladder, in order, exactly as Section 5 specifies:

  1. Normalize the candidate name (normalize.normalize_entity_name).
  2. Exact match against entity_aliases.alias, then entities.canonical_name.
  3. Fuzzy match above ENTITY_FUZZY_THRESHOLD. If two or more candidates are
     within ENTITY_AMBIGUOUS_MARGIN of each other, that is *ambiguous* and is
     the only case that may spend an LLM disambiguation call.
  4. No match -> new entity with status='unconfirmed'. It is promoted to
     'confirmed' only when a second *independent source* references it.
  5. Every alias seen is stored, which makes step 2 hit more often over time.

The whole entity table is loaded once per run and matched in memory. Supabase's
REST interface has no trigram operator, and at this scale (thousands of rows)
an in-process scan is both faster and simpler than an RPC round trip per name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import config
import db.client as db
from normalize import name_similarity, normalize_entity_name

log = logging.getLogger("undercurrent.entities")


@dataclass
class Resolution:
    entity_id: str | None
    canonical_name: str
    matched_by: str          # alias | canonical | fuzzy | llm | new
    score: float
    created: bool = False
    ambiguous_candidates: list[str] = field(default_factory=list)


class EntityIndex:
    """In-memory view of entities + aliases for one run."""

    def __init__(self, entities: list[dict], aliases: list[dict]):
        self.entities = {e["id"]: e for e in entities}
        self.by_canonical: dict[str, str] = {}
        self.by_alias: dict[str, str] = {}
        for e in entities:
            key = normalize_entity_name(e.get("canonical_name"))
            if key:
                self.by_canonical.setdefault(key, e["id"])
        for a in aliases:
            key = normalize_entity_name(a.get("alias"))
            if key and a.get("entity_id") in self.entities:
                self.by_alias.setdefault(key, a["entity_id"])
        # Sources that have already vouched for each entity, for step 4.
        self.sources_seen: dict[str, set[str]] = {
            eid: set((e.get("metadata") or {}).get("sources") or [])
            for eid, e in self.entities.items()
        }
        self._pending_aliases: list[dict] = []
        self._llm_calls_used = 0

    @classmethod
    def load(cls) -> "EntityIndex":
        return cls(db.all_entities(), db.all_aliases())

    # ---------------------------------------------------------- matching --

    def _fuzzy_candidates(self, norm: str) -> list[tuple[str, float]]:
        scored: list[tuple[str, float]] = []
        for eid, entity in self.entities.items():
            score = name_similarity(norm, entity.get("canonical_name"))
            if score >= config.ENTITY_FUZZY_THRESHOLD:
                scored.append((eid, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored

    def resolve(
        self,
        raw_name: str,
        *,
        entity_type: str = "unknown",
        source: str | None = None,
        description: str | None = None,
        context: str | None = None,
        create_if_missing: bool = True,
    ) -> Resolution | None:
        norm = normalize_entity_name(raw_name)
        if not norm or len(norm) < 2:
            return None

        # Step 2: exact, aliases first (they are the cheap accumulated cache).
        for table, matched_by in ((self.by_alias, "alias"), (self.by_canonical, "canonical")):
            eid = table.get(norm)
            if eid:
                self._record_sighting(eid, raw_name, norm, source)
                return Resolution(eid, self.entities[eid]["canonical_name"], matched_by, 1.0)

        # Step 3: fuzzy, with an explicit ambiguity check.
        candidates = self._fuzzy_candidates(norm)
        if candidates:
            best_id, best_score = candidates[0]
            tied = [
                self.entities[eid]["canonical_name"]
                for eid, score in candidates
                if best_score - score <= config.ENTITY_AMBIGUOUS_MARGIN
            ]
            if len(tied) > 1:
                chosen = self._disambiguate(raw_name, context, candidates)
                if chosen:
                    self._record_sighting(chosen, raw_name, norm, source)
                    return Resolution(
                        chosen,
                        self.entities[chosen]["canonical_name"],
                        "llm",
                        best_score,
                        ambiguous_candidates=tied,
                    )
                # Unresolved ambiguity: do not guess, do not merge. Section 7
                # says state uncertainty rather than manufacture a conclusion.
                log.info("ambiguous entity %r among %s -- left unresolved", raw_name, tied)
                return Resolution(None, raw_name, "ambiguous", best_score, ambiguous_candidates=tied)
            self._record_sighting(best_id, raw_name, norm, source)
            return Resolution(
                best_id, self.entities[best_id]["canonical_name"], "fuzzy", best_score
            )

        if not create_if_missing:
            return None

        # Step 4: new entity, unconfirmed until a second independent source.
        return self._create(raw_name, norm, entity_type, description, source)

    # ------------------------------------------------------------ writes --

    def _create(
        self,
        raw_name: str,
        norm: str,
        entity_type: str,
        description: str | None,
        source: str | None,
    ) -> Resolution:
        row = db.insert_entity(
            {
                "entity_type": entity_type,
                "canonical_name": raw_name.strip()[:200],
                "description": (description or "")[:500] or None,
                "status": "unconfirmed",
                "metadata": {"sources": [source] if source else []},
            }
        )
        if not row:
            return Resolution(None, raw_name, "new", 0.0)
        eid = row["id"]
        self.entities[eid] = row
        self.by_canonical[norm] = eid
        self.sources_seen[eid] = {source} if source else set()
        self._pending_aliases.append({"entity_id": eid, "alias": norm})
        return Resolution(eid, row["canonical_name"], "new", 1.0, created=True)

    def _record_sighting(
        self, entity_id: str, raw_name: str, norm: str, source: str | None
    ) -> None:
        """Step 5 (store the alias) + step 4 (promote on a 2nd independent source)."""
        if norm not in self.by_alias:
            self.by_alias[norm] = entity_id
            self._pending_aliases.append({"entity_id": entity_id, "alias": norm})

        if not source:
            return
        seen = self.sources_seen.setdefault(entity_id, set())
        if source in seen:
            return
        seen.add(source)
        entity = self.entities.get(entity_id) or {}
        metadata = dict(entity.get("metadata") or {})
        metadata["sources"] = sorted(seen)
        patch: dict = {"metadata": metadata}
        if len(seen) >= 2 and entity.get("status") != "confirmed":
            patch["status"] = "confirmed"
            log.info("entity confirmed by 2nd independent source: %s", entity.get("canonical_name"))
        try:
            db.update_entity(entity_id, patch)
            entity.update(patch)
        except Exception as exc:
            log.warning("could not update entity %s: %s", entity_id, exc)

    def flush(self) -> None:
        """Persist accumulated aliases in one batch."""
        if not self._pending_aliases:
            return
        try:
            db.insert_aliases(self._pending_aliases)
        except Exception as exc:
            log.warning("alias flush failed: %s", exc)
        self._pending_aliases = []

    # ---------------------------------------------------- llm tie-break --

    def _disambiguate(
        self, raw_name: str, context: str | None, candidates: list[tuple[str, float]]
    ) -> str | None:
        """Section 5 step 3: at most one LLM call, and only for real ambiguity."""
        if self._llm_calls_used >= config.LLM_MAX_DISAMBIGUATION_CALLS:
            return None
        from intelligence.llm import disambiguate_entity

        options = [
            {
                "id": eid,
                "name": self.entities[eid]["canonical_name"],
                "type": self.entities[eid].get("entity_type"),
                "description": self.entities[eid].get("description"),
            }
            for eid, _ in candidates[:5]
        ]
        self._llm_calls_used += 1
        try:
            chosen = disambiguate_entity(raw_name, context or "", options)
        except Exception as exc:
            log.warning("entity disambiguation call failed: %s", exc)
            return None
        return chosen if chosen in self.entities else None


def resolve_mentions(
    index: EntityIndex, mentions: list[dict]
) -> list[Resolution]:
    """Resolve a batch of {name, type, source, context} extraction outputs."""
    out: list[Resolution] = []
    for mention in mentions:
        resolution = index.resolve(
            mention.get("name", ""),
            entity_type=mention.get("type") or "unknown",
            source=mention.get("source"),
            description=mention.get("description"),
            context=mention.get("context"),
        )
        if resolution:
            out.append(resolution)
    index.flush()
    return out
