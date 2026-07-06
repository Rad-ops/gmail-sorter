"""Language detection for the Gmail sorter (v0.7).

The keyword rules in :mod:`sorter.policy` and the language overlays in
``config/policy.fr.yaml`` / ``config/policy.fa.yaml`` are picked by detected
language, not by hard-coded sender assumptions. The detector runs over the
cleaned body excerpt + subject so the same email in English, French, and
Farsi each routes to the right keyword set.

The detector is *only* used to pick the keyword overlay. It never blocks mail,
it never overrides the protection gate, and it never changes the message
itself. When the detector is uncertain the sorter falls back to English
silently — keyword coverage is a lever for accuracy, not a hard requirement.

Two backends, picked in order:

1. ``langdetect`` if installed (the only optional dependency). High accuracy
   for short text and code-mixed mail.
2. A pure-Python stopword-frequency fallback that ships with the sorter. The
   fallback ships tiny stopword lists for English, French, and Farsi. It
   returns ``"other"`` when none of the stopword sets hit enough times in
   the input, which the loader treats as English.

The detector's job is to be cheap, deterministic, and privacy-safe — it never
sees raw body text outside the in-process call, and it never persists the
detected language separately from the decision that already records the
matching keyword family.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("sorter.lang")

# Stopword sets for the pure-Python fallback. These are intentionally small
# (a few dozen per language) and were chosen to disambiguate EN/FR/FA on the
# kinds of headers and preambles that dominate short emails. The sets are
# not meant to be exhaustive — the detector is a *picker*, not a parser.

_EN_STOPWORDS = frozenset({
    "the", "and", "of", "to", "in", "is", "you", "for", "on", "with", "this",
    "that", "your", "are", "from", "have", "has", "was", "but", "not", "they",
    "we", "will", "would", "been", "their", "there", "than", "about", "into",
    "more", "some", "any", "all", "her", "him", "our", "out", "what", "when",
    "make", "made", "please", "thank", "thanks", "regards", "dear", "hello",
    "account", "order", "payment", "statement", "receipt",
})

_FR_STOPWORDS = frozenset({
    "le", "la", "les", "des", "un", "une", "et", "ou", "mais", "donc", "or",
    "ni", "car", "à", "de", "du", "en", "dans", "sur", "sous", "avec", "sans",
    "pour", "par", "vos", "votre", "notre", "nos", "leur", "leurs", "vous",
    "nous", "ils", "elles", "est", "sont", "était", "été", "être", "avoir",
    "fait", "faire", "merci", "bonjour", "chers", "chères", "madame",
    "monsieur", "immigration", "permis", "résidence", "travail", "étude",
    "carte", "banque", "relevé", "facture", "paiement", "commande", "rendez-vous",
    "biométrie", "biometrie", "avocat", "conseil", "juridique",
})

_FA_STOPWORDS = frozenset({
    # Common Farsi function words; matched as substrings within the
    # space-normalized text. The detection is a *picker*, not a parser.
    "از", "به", "در", "که", "این", "آن", "با", "برای", "تا", "یا", "اما",
    "اگر", "هم", "نیز", "را", "است", "بود", "شد", "می", "خود", "ما", "شما",
    "آنها", "ایشان", "دارد", "دارند", "بود", "سلام", "ممنون", "تشکر", "لطفا",
    "لطفاً", "اقای", "خانم", "دکتر", "حساب", "سفارش", "پرداخت", "قبض", "اقامت",
    "ویزا", "مهاجرت", "دعاوی", "وکیل", "وقت", "نوبت",
})

# When more than this fraction of a text's tokens are stopwords of a language,
# the language wins. 5% is a comfortable floor: real short emails rarely
# have less, and an accidental overlap with one or two English words in a
# French email still leaves the French set dominant.
_STOPWORD_HIT_FRACTION = 0.05

_TOKEN_RE = re.compile(r"[\wÀ-ɏ‌‌‍]+", flags=re.UNICODE)


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [token.lower() for token in _TOKEN_RE.findall(text)]


def _score_language(tokens: list[str], stopwords: frozenset[str]) -> int:
    if not tokens:
        return 0
    hits = 0
    for token in tokens:
        if token in stopwords:
            hits += 1
            continue
        # For Farsi, the stopword tokens are unigrams; substring match
        # inside a longer Persian word is enough since Farsi is mostly
        # space-separated.
        for stop in stopwords:
            if stop in token:
                hits += 1
                break
    return hits


def _pure_python_detect(text: str) -> str:
    """Stopword-frequency fallback. Returns one of ``en``/``fr``/``fa``/``other``."""

    tokens = _tokenize(text)
    if not tokens:
        return "other"
    en = _score_language(tokens, _EN_STOPWORDS)
    fr = _score_language(tokens, _FR_STOPWORDS)
    fa = _score_language(tokens, _FA_STOPWORDS)
    scores = {"en": en, "fr": fr, "fa": fa}
    best = max(scores.values())
    if best == 0:
        return "other"
    threshold = max(1, int(len(tokens) * _STOPWORD_HIT_FRACTION))
    if best < threshold:
        return "other"
    for lang, score in scores.items():
        if score == best:
            return lang
    return "other"


def _try_langdetect() -> Any | None:
    """Return the langdetect module if available, else None.

    We import lazily so the sorter does not hard-depend on langdetect. The
    fallback path is always available.
    """

    try:
        import langdetect  # type: ignore
    except ImportError:
        return None
    return langdetect


def detect(text: str) -> str:
    """Return one of ``en``/``fr``/``fa``/``other``.

    ``text`` should be the cleaned body excerpt (or a combination of subject
    + cleaned body) so the same code path used for categorization drives the
    detection. The function is pure and cheap; it never raises and never
    makes a network call.
    """

    if not text:
        return "other"
    langdetect = _try_langdetect()
    if langdetect is not None:
        try:
            result = langdetect.detect(text)
        except langdetect.LangDetectException as error:
            log.debug("langdetect returned no result: %s", error)
        except Exception as error:  # pragma: no cover - defensive
            log.debug("langdetect raised: %s", error)
        else:
            if result.startswith("fa"):
                return "fa"
            if result.startswith("fr"):
                return "fr"
            if result.startswith("en"):
                return "en"
            return "other"
    return _pure_python_detect(text)


# Normalize the four outcomes callers may see.
SUPPORTED = ("en", "fr", "fa", "other")


__all__ = ["detect", "SUPPORTED"]
