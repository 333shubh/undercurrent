"""Cross-source normalization and the one similarity method (Section 12/14).

Not in the spec's file list, but every collector and half of intelligence/ needs
these, and duplicating them per module is how sources drift apart. Section 12
explicitly asks for normalized URLs, timestamps, authors and titles "across
sources", which means one shared implementation.

Section 14 open question -- embeddings vs. keyword overlap for similarity:
picked keyword overlap. Reasons: it is deterministic (Section 6 requires the
scores to be deterministic code), it needs no embedding API or vector store, and
it costs nothing. Implementation is a blend of token-set Jaccard and character
trigram ratio, which handles both reworded headlines and typo'd titles.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rapidfuzz.distance import JaroWinkler
from rapidfuzz.fuzz import token_set_ratio

# Tracking params that change per-referrer but never change the destination.
_TRACKING_PREFIXES = ("utm_", "ref_", "mc_")
_TRACKING_KEYS = {
    "ref", "source", "src", "amp", "at_medium", "at_campaign", "fbclid",
    "gclid", "igshid", "spm", "share", "shared", "cmpid", "smid",
}

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does",
    "for", "from", "has", "have", "how", "i", "in", "is", "it", "its", "of",
    "on", "or", "that", "the", "their", "there", "this", "to", "was", "we",
    "what", "when", "which", "who", "why", "will", "with", "you", "your",
    "show", "hn", "ask", "new", "using", "use", "via",
}

_COMPANY_SUFFIXES = {
    "inc", "inc.", "llc", "ltd", "ltd.", "corp", "corp.", "co", "co.", "gmbh",
    "labs", "lab", "ai", "io", "technologies", "technology", "systems",
    "research", "the", "sa", "plc", "bv", "nv", "srl", "oy", "ab",
}

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s\-\.]", re.UNICODE)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-\+\.]*")


# ------------------------------------------------------------------ text --


def collapse_ws(text: str | None) -> str:
    return _WS_RE.sub(" ", (text or "")).strip()


def clean_text(text: str | None, limit: int = 1200) -> str:
    """Strip HTML-ish junk out of RSS bodies and clamp length."""
    s = collapse_ws(text)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&(nbsp|amp|quot|#39|lt|gt);", " ", s)
    s = collapse_ws(s)
    return s[:limit]


def normalize_title(title: str | None) -> str:
    """Lowercased, punctuation-light form used for near-duplicate comparison."""
    s = collapse_ws(title).lower()
    s = re.sub(r"^(show|ask|tell)\s+hn:?\s*", "", s)
    s = re.sub(r"^\[[^\]]{1,20}\]\s*", "", s)  # [P], [R], [D] on r/MachineLearning
    s = _PUNCT_RE.sub(" ", s)
    return collapse_ws(s)


def tokens(text: str | None) -> list[str]:
    return [
        t
        for t in _TOKEN_RE.findall(collapse_ws(text).lower())
        if t not in _STOPWORDS and len(t) > 2
    ]


def normalize_entity_name(name: str | None) -> str:
    """Section 5 step 1: lowercase, strip suffixes (Inc., Labs, .ai), collapse ws."""
    s = collapse_ws(name).lower()
    s = re.sub(r"[‘’“”]", "'", s)
    s = re.sub(r"\.(ai|io|dev|com|co|sh|xyz|app)\b", "", s)
    s = _PUNCT_RE.sub(" ", s)
    parts = [p for p in collapse_ws(s).split(" ") if p]
    while len(parts) > 1 and parts[-1].strip(".") in _COMPANY_SUFFIXES:
        parts.pop()
    while len(parts) > 1 and parts[0] in {"the"}:
        parts.pop(0)
    return " ".join(parts).strip()


def slugify(text: str | None, limit: int = 80) -> str:
    s = collapse_ws(text).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:limit] or "untitled"


# ------------------------------------------------------------------- url --


def normalize_url(url: str | None) -> str:
    """Strip tracking params, fragments, trailing slash, www, and force https.

    Two collectors that find the same article behind different referrers must
    produce the same url_norm, or the dedup layer counts one story twice and
    inflates convergence (Section 13).
    """
    raw = collapse_ws(url)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    scheme = "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc.endswith(":443") or netloc.endswith(":80"):
        netloc = netloc.rsplit(":", 1)[0]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_KEYS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def content_hash(title: str | None, url: str | None, snippet: str | None = None) -> str:
    """Stable hash of the normalized story identity -- exact-duplicate detection."""
    basis = f"{normalize_title(title)}|{normalize_url(url)}"
    if not basis.strip("|"):
        basis = clean_text(snippet, 400)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ time --


def to_utc(value) -> datetime | None:
    """Accept epoch seconds, struct_time, ISO strings, datetimes -> aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, (tuple, list)) and len(value) >= 6:
        try:
            dt = datetime(*value[:6], tzinfo=timezone.utc)
        except ValueError:
            return None
    else:
        dt = _parse_datetime_string(str(value))
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_datetime_string(s: str) -> datetime | None:
    s = collapse_ws(s)
    candidate = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def normalize_author(author: str | None) -> str:
    s = collapse_ws(author)
    s = re.sub(r"^(by|u/|/u/|@)\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\(.*?\)\s*$", "", s)
    return s[:120]


# ------------------------------------------------------------ similarity --


def similarity(a: str | None, b: str | None) -> float:
    """One similarity method, used everywhere (novelty, near-dup, theme match).

    Blend of token-set overlap (catches reordering and padding words) and
    Jaro-Winkler over the normalized strings (catches small edits). Returns 0..1.
    """
    ta, tb = normalize_title(a), normalize_title(b)
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    token_score = token_set_ratio(ta, tb) / 100.0
    char_score = JaroWinkler.similarity(ta, tb)
    jac = jaccard(tokens(ta), tokens(tb))
    return max(jac, 0.5 * token_score + 0.3 * char_score + 0.2 * jac)


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def name_similarity(a: str | None, b: str | None) -> float:
    """Entity-name similarity (Section 5 step 2 fuzzy match)."""
    na, nb = normalize_entity_name(a), normalize_entity_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return max(
        JaroWinkler.similarity(na, nb),
        token_set_ratio(na, nb) / 100.0,
    )
