"""Doc-type vocabulary: exact aliases first, then a per-word fuzzy match for typos."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from uarb_agent.models import DocType

ALIASES: dict[DocType, tuple[str, ...]] = {
    DocType.EXHIBITS: ("exhibits", "exhibit"),
    DocType.KEY_DOCUMENTS: ("key documents", "key document", "key docs", "key doc", "keydocs"),
    DocType.OTHER_DOCUMENTS: ("other documents", "other document", "other docs", "other doc", "other files", "others"),
    DocType.TRANSCRIPTS: ("transcripts", "transcript"),
    DocType.RECORDINGS: ("audio recordings", "video recordings", "recordings", "recording", "audio", "video"),
}

# Only full spellings are fuzzy targets; short words ("key", "doc", "audio") must match exactly.
_FUZZY_TARGETS: tuple[tuple[tuple[str, ...], DocType], ...] = (
    (("exhibits",), DocType.EXHIBITS),
    (("key", "documents"), DocType.KEY_DOCUMENTS),
    (("other", "documents"), DocType.OTHER_DOCUMENTS),
    (("transcripts",), DocType.TRANSCRIPTS),
    (("recordings",), DocType.RECORDINGS),
)
FUZZY_CUTOFF = 85
_MIN_FUZZY_WORD = 5


def _phrase_pattern(phrase: str) -> str:
    return r"[\s-]*".join(re.escape(w) for w in phrase.split())


_EXACT = [
    (re.compile(rf"(?i)\b(?:{'|'.join(_phrase_pattern(p) for p in sorted(phrases, key=len, reverse=True))})\b"), t)
    for t, phrases in ALIASES.items()
]
_WORD = re.compile(r"[^\W\d_]+")


@dataclass(frozen=True)
class TypeMention:
    start: int
    end: int
    doc_type: DocType
    fuzzy: bool = False


def _word_close(word: str, target: str) -> bool:
    if word == target:
        return True
    if len(target) < _MIN_FUZZY_WORD or len(word) < _MIN_FUZZY_WORD or word[0] != target[0]:
        return False
    # singular forms count too ("exhibt" ~ "exhibit")
    return max(fuzz.ratio(word, target), fuzz.ratio(word, target.rstrip("s"))) >= FUZZY_CUTOFF


def find_types(text: str) -> list[TypeMention]:
    """Every doc-type mention in `text`, in order of appearance."""
    found: list[TypeMention] = []
    taken: list[tuple[int, int]] = []
    for rx, doc_type in _EXACT:
        for m in rx.finditer(text):
            if not any(m.start() < e and s < m.end() for s, e in taken):
                found.append(TypeMention(m.start(), m.end(), doc_type))
                taken.append(m.span())

    words = [m for m in _WORD.finditer(text) if not any(m.start() < e and s < m.end() for s, e in taken)]
    i = 0
    while i < len(words):
        for target, doc_type in _FUZZY_TARGETS:
            window = words[i : i + len(target)]
            if len(window) == len(target) and all(
                _word_close(w.group().lower(), t) for w, t in zip(window, target, strict=True)
            ):
                found.append(TypeMention(window[0].start(), window[-1].end(), doc_type, fuzzy=True))
                i += len(target) - 1
                break
        i += 1
    return sorted(found, key=lambda t: t.start)


def match_type(text: str) -> DocType | None:
    """The single doc type a short reply names ('exibits', 'key docs'), or None if zero or several."""
    types = {t.doc_type for t in find_types(text)}
    return types.pop() if len(types) == 1 else None
