"""Name normalization for cross-source player identity matching.

Used when reconciling Ourlads roster rows, 32beatwriters nuggets, and
Sleeper player names against the local `players` table. Sources disagree
about whether to include Jr/Sr/II/III/IV suffixes ("Marvin Harrison" vs
"Marvin Harrison Jr"), so the normalizer strips them. Genuine
same-team namesakes are extremely rare in the NFL, and when they do
exist `find_player_for_match` returns >1 candidate and the caller skips
the merge — so collapse-then-disambiguate is safer than never-collapse.
"""

from __future__ import annotations

import re
import unicodedata


_WHITESPACE_RE = re.compile(r"\s+")
_DROP_CHARS_RE = re.compile(r"['.‘’“”]")  # straight + smart quotes, periods
# Generational suffixes commonly trailing player names. Stripped after
# punctuation and case-folding so "Jr.", "JR", "  jr" all match. The
# Roman-numeral set covers I-V which is all the NFL uses in practice.
_SUFFIX_TOKENS: frozenset[str] = frozenset(
    {"jr", "sr", "ii", "iii", "iv", "v"}
)


def normalize_full_name(name: str) -> str:
    """Return a normalized form of a player's full name for identity matching.

    Steps:
      1. NFKD-decompose, drop combining marks (diacritics)
      2. Strip apostrophes and periods
      3. Lowercase
      4. Collapse internal whitespace; trim ends
      5. Strip a trailing generational suffix (Jr/Sr/II/III/IV/V) if present
    """
    if not name:
        return ""
    # NFKD: split combined characters; ascii fold by dropping combining marks.
    decomposed = unicodedata.normalize("NFKD", name)
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # Drop apostrophes and periods.
    no_punct = _DROP_CHARS_RE.sub("", no_marks)
    # Lowercase + collapse whitespace.
    folded = _WHITESPACE_RE.sub(" ", no_punct).strip().lower()
    # Strip a trailing generational suffix. Only one (real names rarely
    # carry two), and only when the prefix has at least one other token
    # (avoid eating standalone "Sr" inputs).
    parts = folded.split(" ")
    if len(parts) > 1 and parts[-1] in _SUFFIX_TOKENS:
        parts.pop()
    return " ".join(parts)


def synthesize_ourlads_id(team: str, jersey: str | None, normalized_name: str) -> str:
    """Build a stable synthesized id when Ourlads' HTML doesn't expose a per-
    player profile id. Format: `<TEAM>:<jersey>:<normalized_name_with_underscores>`.
    `?` is used in the jersey slot when unknown.
    """
    jersey_part = jersey if jersey else "?"
    name_part = normalized_name.replace(" ", "_")
    return f"{team}:{jersey_part}:{name_part}"
