"""
Regex-based extraction of "Najvyšší kontrolný úrad SR" / "NKÚ" mentions
from parsed document text blocks, with full-sentence context.

Design notes:
- Slovak is a declined language; matching literal strings like
  "Najvyšší kontrolný úrad" only catches the nominative case and misses
  "Najvyššieho kontrolného úradu", "Najvyššiemu kontrolnému úradu", etc.
  We match on word stems + \\w* instead of enumerating every case ending.
- Bare "NKÚ" is short and collision-prone. We require a word boundary on
  both sides (so it won't match inside a longer alphanumeric token) but
  we do NOT try to disambiguate semantically -- expect some false
  positives if "NKÚ" is reused as an unrelated acronym in a given text;
  that's a fundamental limit of regex, not something to paper over.
- Sentence splitting is regex-based and imperfect: Slovak legal text is
  full of abbreviations ("Z. z.", "č.", "resp.", "tzv.", "napr.", "ods.",
  "písm.") that contain periods but don't end a sentence. We special-case
  the common ones; anything else may cause an over-eager sentence split.
"""

import re
from dataclasses import dataclass
from typing import List, Optional

# Abbreviations that must NOT be treated as sentence terminators.
_NON_TERMINATING_ABBREVIATIONS = [
    r"Z\.\s?z\.", r"č\.", r"resp\.", r"tzv\.", r"napr\.", r"ods\.",
    r"písm\.", r"str\.", r"pozn\.", r"tj\.", r"tzn\.", r"cca\.", r"p\.",
    r"vz\.", r"š\.", r"ing\.", r"mgr\.", r"jud\.",
]

_ABBREV_PLACEHOLDER = "\uE000"  # private-use char, safe temporary substitute

_PATTERNS = {
    "NKU_FULL_NAME": re.compile(
        r"(?i)najvyšš\w*\s+kontroln\w*\s+úrad\w*(\s+SR\b)?"
    ),
    "NKU_SPRAVA_PHRASE": re.compile(
        r"(?i)vyplýva\s+zo\s+správy\s+najvyšš\w*\s+kontroln\w*\s+úrad\w*(\s+SR\b)?"
    ),
    "NKU_ACRONYM": re.compile(
        r"(?<!\w)NKÚ(\s+SR\b)?(?!\w)"
    ),
}

# Order matters: check the more specific "správy NKÚ" phrase first so a
# single mention isn't double-counted under both a generic and specific tag.
_PATTERN_ORDER = ["NKU_SPRAVA_PHRASE", "NKU_FULL_NAME", "NKU_ACRONYM"]

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
    """Best-effort sentence splitter, abbreviation-aware. See module docstring."""
    protected = _protect_abbreviations(paragraph)
    raw_sentences = _SENTENCE_SPLIT_RE.split(protected)
    return [_restore_abbreviations(s).strip() for s in raw_sentences if s.strip()]


def find_matches_in_text(text: str) -> List[ExtractedMatch]:
    """
    Run all patterns against a block of text (paragraph or page text) and
    return one ExtractedMatch per hit, with the full containing sentence
    as context. If sentence splitting fails to isolate a clean unit (e.g.
    a table row with no punctuation), falls back to the whole block.
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
                # Skip a generic-name hit that's already covered by the
                # more specific "vyplýva zo správy..." phrase in this sentence.
                if pattern_name == "NKU_FULL_NAME" and any(
                    p == "NKU_SPRAVA_PHRASE" and _PATTERNS[p].search(sentence)
                    for p in ("NKU_SPRAVA_PHRASE",)
                ):
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


# --- Reading stage (prvé / druhé / tretie čítanie) ----------------------
# The explanatory document's first page states which legislative reading a
# tlač is currently at (e.g. "Spoločná správa výborov ... v druhom čítaní").
# Same word-stem approach as the NKÚ patterns above, to cover declined
# forms: "prvé čítanie" (nominative) vs. "v prvom čítaní" (locative), etc.
_READING_PATTERNS = {
    "prvé čítanie": re.compile(r"(?i)\bprv\w*\s+čítan\w*"),
    "druhé čítanie": re.compile(r"(?i)\bdruh\w*\s+čítan\w*"),
    "tretie čítanie": re.compile(r"(?i)\btre(ti|ť)\w*\s+čítan\w*"),
}
# Checked in this order so that if a page somehow mentions more than one
# (e.g. a summary table listing all three stages), the earliest stage wins
# -- a tlač's own document should really only state its current stage, but
# this keeps behavior predictable rather than "whichever regex happens to
# run last".
_READING_ORDER = ["prvé čítanie", "druhé čítanie", "tretie čítanie"]


def detect_reading_stage(text: str) -> Optional[str]:
    """Returns 'prvé čítanie' / 'druhé čítanie' / 'tretie čítanie' if found in text, else None."""
    for label in _READING_ORDER:
        if _READING_PATTERNS[label].search(text):
            return label
    return None


def find_reading_stage(blocks) -> Optional[str]:
    """
    Looks for the reading-stage mention on a document's first page (where
    it's stated in practice), falling back to the first available block if
    page numbers aren't available (e.g. a degraded-markup fallback in
    parse_document_preview_html). `blocks` is a list of objects with `.text`
    and `.page` attributes (parsers.TextBlock) -- not imported directly here
    to avoid a circular import between extractor.py and parsers.py.
    """
    first_page_blocks = [b for b in blocks if getattr(b, "page", None) == 1]
    candidates = first_page_blocks or (list(blocks)[:1] if blocks else [])
    for block in candidates:
        stage = detect_reading_stage(block.text)
        if stage:
            return stage
    return None
