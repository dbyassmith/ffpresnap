"""Name normalization for cross-source player identity matching.

Used when reconciling Ourlads roster rows against existing Sleeper-keyed
`players` rows. The normalization is deliberately conservative — it does
NOT strip suffixes (Jr/Sr/II/III/IV) so namesakes like "Marvin Harrison"
and "Marvin Harrison Jr." disambiguate cleanly.
"""

from __future__ import annotations

import re
import unicodedata


_WHITESPACE_RE = re.compile(r"\s+")
_DROP_CHARS_RE = re.compile(r"['.‘’“”]")  # straight + smart quotes, periods


def normalize_full_name(name: str) -> str:
    """Return a normalized form of a player's full name for identity matching.

    Steps:
      1. NFKD-decompose, drop combining marks (diacritics)
      2. Strip apostrophes and periods
      3. Lowercase
      4. Collapse internal whitespace; trim ends

    Suffixes (Jr/Sr/II/III/IV) are intentionally preserved so namesakes
    on the same team disambiguate. Empty / whitespace-only input returns "".
    """
    if not name:
        return ""
    # NFKD: split combined characters; ascii fold by dropping combining marks.
    decomposed = unicodedata.normalize("NFKD", name)
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # Drop apostrophes and periods.
    no_punct = _DROP_CHARS_RE.sub("", no_marks)
    # Lowercase + collapse whitespace.
    return _WHITESPACE_RE.sub(" ", no_punct).strip().lower()


def synthesize_ourlads_id(team: str, jersey: str | None, normalized_name: str) -> str:
    """Build a stable synthesized id when Ourlads' HTML doesn't expose a per-
    player profile id. Format: `<TEAM>:<jersey>:<normalized_name_with_underscores>`.
    `?` is used in the jersey slot when unknown.
    """
    jersey_part = jersey if jersey else "?"
    name_part = normalized_name.replace(" ", "_")
    return f"{team}:{jersey_part}:{name_part}"
