"""Text similarity for payee and description comparison.

Written here rather than taken from a library. The project needs one measure, and a
reviewer who asks "why did these two names score 0.94?" deserves an answer that can be
read in forty lines rather than traced through a dependency.

The measure follows the token-set approach: compare the shared words separately from
the leftovers, so word order and extra words matter less than the words themselves.
"ALTA VISTA BISTRO" and "BISTRO ALTA VISTA LLC" describe the same counterparty.

Every function is pure and deterministic: same input, same score, on any machine.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache

# Words that say nothing about who the counterparty is. Removed before comparison so
# "CANYON RIDGE GROCERY LLC" and "CANYON RIDGE GROCERY" are not penalized for it.
NOISE_TOKENS = frozenset({
    "LLC", "INC", "INCORPORATED", "CORP", "CORPORATION", "COMPANY", "CO", "LTD",
    "LP", "LLP", "THE", "AND", "OF", "GROUP", "HOLDINGS",
})


def tokens(text: str) -> list[str]:
    """Split into comparable words, dropping noise and anything containing digits."""
    return [word for word in (text or "").upper().split()
            if word not in NOISE_TOKENS and not any(character.isdigit() for character in word)]


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


@lru_cache(maxsize=100_000)
def similarity(left: str, right: str) -> float:
    """Similarity between two counterparty strings, 0.0 to 1.0.

    Three comparisons are made and the best is returned:

    1. sorted tokens, which ignores word order
    2. shared tokens plus each side's leftovers, which tolerates extra words
    3. the raw strings, which catches abbreviations that share a prefix

    Taking the best is deliberate. Each comparison fails on a different kind of
    difference, and a payee only has to be recognizable by one of them.
    """
    left_tokens, right_tokens = tokens(left), tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0

    if left_tokens == right_tokens:
        return 1.0

    sorted_ratio = _ratio(" ".join(sorted(left_tokens)), " ".join(sorted(right_tokens)))

    shared = sorted(set(left_tokens) & set(right_tokens))
    left_only = sorted(set(left_tokens) - set(right_tokens))
    right_only = sorted(set(right_tokens) - set(left_tokens))
    shared_text = " ".join(shared)
    set_ratio = max(
        _ratio(shared_text, " ".join(shared + left_only)),
        _ratio(shared_text, " ".join(shared + right_only)),
        _ratio(" ".join(shared + left_only), " ".join(shared + right_only)),
    ) if shared else 0.0

    raw_ratio = _ratio(" ".join(left_tokens), " ".join(right_tokens))

    return round(max(sorted_ratio, set_ratio, raw_ratio), 4)


def abbreviation_similarity(left: str, right: str) -> float:
    """Word-by-word score that rewards one word being a shortening of another.

    Bank statements abbreviate by dropping letters, usually vowels: SLVR for SILVER,
    WSTCH for WASATCH. Character overlap alone scores those poorly, so each word is
    compared with its best partner on the other side, with a prefix or subsequence
    match treated as strong evidence.
    """
    left_tokens, right_tokens = tokens(left), tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0

    def word_score(first: str, second: str) -> float:
        if first == second:
            return 1.0
        shorter, longer = (first, second) if len(first) <= len(second) else (second, first)
        if len(shorter) >= 3 and longer.startswith(shorter):
            return 0.95                                  # GROC inside GROCERY
        if len(shorter) >= 3 and _is_subsequence(shorter, longer):
            return 0.90                                  # SLVR inside SILVER
        return _ratio(first, second)

    total = 0.0
    for word in left_tokens:
        total += max(word_score(word, other) for other in right_tokens)
    return round(total / len(left_tokens), 4)


def _is_subsequence(short: str, long: str) -> bool:
    """Whether every letter of `short` appears in `long` in the same order."""
    iterator = iter(long)
    return all(character in iterator for character in short)


def best_similarity(left: str, right: str) -> float:
    """The measure used by the scoring layer: the stronger of the two views."""
    return round(max(similarity(left, right), abbreviation_similarity(left, right)), 4)


def shared_words(left: str, right: str) -> list[str]:
    """Words both sides have, for the explanation the reviewer reads."""
    return sorted(set(tokens(left)) & set(tokens(right)))
