"""
Regex-based extraction of NK/NKÚ mentions from parsed MPK documents.
Handles Slovak declined language forms and acronyms.
"""

import re
from dataclasses import dataclass
from typing import List, Optional

# Abbreviations that must NOT be treated as sentence terminators
_NON_TERMINATING_ABBREVIATIONS = [
    r"Z\.\s?z\.", r"č\.", r"resp\.", r"tzv\.", r"napr\.", r"ods\.",
    r"písm\.", r"str\.", r"pozn\.", r"tj\.", r"tzn\.", r"cca\.", r"p\.",
    r"vz\.", r"š\.", r"ing\.", r"mgr\.", r"jud\.",
]

_ABBREV_PLACEHOLDER = "\uE000"  # private-use char

_PATTERNS = {
    "NK_FULL_NAME": re.compile(
        r"(?i)najvyšš\w*\s+kontroln\w*\s+úrad\w*(\s+S[Rr]\b)?"
    ),
    "NK_ACRONYM": re.compile(
        r"(?<!\w)NK(\s+S[Rr]\b)?(?!\w)"
    ),
    "NKU_ACRONYM": re.compile(
        r"(?<!\w)NKÚ(\s+S[Rr]\b)?(?!\w)"
    ),
}

# Order matters: check more specific patterns first
_PATTERN_ORDER = ["NK_FULL_NAME", "NKU_ACRONYM", "NK_ACRONYM"]

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ])")


@dataclass
class ExtractedMatch:
    pattern_matched: str
    matched_text: str
    context: str


def _protect_abbreviations(text: str) -> str:
    protected = text
    for abbrev in _NON_TERMINATING_ABBREVIATIONS:
        protected = re.sub(
            abbrev, lambda m: m.group(0).replace(".", _ABBREV_PLACEHOLDER),
            protected, flags=re.IGNORECASE,
        )
    return protected


def _restore_abbreviations(text: str) -> str:
    return text.replace(_ABBREV_PLACEHOLDER, ".")


def split_sentences(paragraph: str) -> List[str]:
    """Best-effort sentence splitter, abbreviation-aware."""
    protected = _protect_abbreviations(paragraph)
    raw_sentences = _SENTENCE_SPLIT_RE.split(protected)
    return [_restore_abbreviations(s).strip() for s in raw_sentences if s.strip()]


def find_matches_in_text(text: str) -> List[ExtractedMatch]:
    """
    Run all patterns against text and return matches with full sentence context.
    """
    results: List[ExtractedMatch] = []
    sentences = split_sentences(text) or [text]

    seen_spans_per_sentence = set()

    for sentence in sentences:
        for pattern_name in _PATTERN_ORDER:
            pattern = _PATTERNS[pattern_name]
            for m in pattern.finditer(sentence):
                key = (sentence, m.start(), m.end())
                if key in seen_spans_per_sentence:
                    continue
                seen_spans_per_sentence.add(key)
                results.append(
                    ExtractedMatch(
                        pattern_matched=pattern_name,
                        matched_text=m.group(0),
                        context=sentence,
                    )
                )

    return results
