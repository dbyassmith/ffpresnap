from ffpresnap._naming import normalize_full_name, synthesize_ourlads_id


# --- normalize_full_name ---


def test_normalize_handles_diacritics():
    assert normalize_full_name("José") == "jose"
    assert normalize_full_name("Saquón") == "saquon"


def test_normalize_strips_apostrophes_and_periods():
    assert normalize_full_name("D'Andre Swift") == "dandre swift"
    assert normalize_full_name("A.J. Brown") == "aj brown"


def test_normalize_collapses_whitespace():
    assert normalize_full_name("  Patrick   Mahomes  ") == "patrick mahomes"


def test_normalize_lowercases():
    assert normalize_full_name("PATRICK MAHOMES") == "patrick mahomes"


def test_normalize_preserves_suffix():
    """Suffixes are intentionally NOT stripped — namesakes must disambiguate."""
    assert normalize_full_name("Marvin Harrison Jr.") == "marvin harrison jr"
    assert normalize_full_name("Steve Smith Sr") == "steve smith sr"
    assert normalize_full_name("Kenneth Walker III") == "kenneth walker iii"


def test_normalize_distinguishes_suffix_from_no_suffix():
    """Father/son disambiguation — the whole point of preserving suffixes."""
    assert normalize_full_name("Marvin Harrison") != normalize_full_name(
        "Marvin Harrison Jr."
    )


def test_normalize_empty_input():
    assert normalize_full_name("") == ""
    assert normalize_full_name("   ") == ""


def test_normalize_idempotent():
    once = normalize_full_name("José D'Andre")
    twice = normalize_full_name(once)
    assert once == twice


def test_normalize_smart_quotes():
    assert normalize_full_name("D’Andre") == "dandre"


# --- synthesize_ourlads_id ---


def test_synthesize_id_with_jersey():
    assert synthesize_ourlads_id("ATL", "7", "bijan robinson") == "ATL:7:bijan_robinson"


def test_synthesize_id_without_jersey():
    assert (
        synthesize_ourlads_id("ATL", None, "bijan robinson") == "ATL:?:bijan_robinson"
    )


def test_synthesize_id_empty_jersey():
    """Empty string jersey is treated as missing."""
    assert synthesize_ourlads_id("ATL", "", "bijan robinson") == "ATL:?:bijan_robinson"


def test_synthesize_id_multi_word_name():
    assert (
        synthesize_ourlads_id("KC", "15", "patrick lavon mahomes")
        == "KC:15:patrick_lavon_mahomes"
    )
