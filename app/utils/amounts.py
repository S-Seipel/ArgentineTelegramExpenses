"""Deterministic amount parser for Argentine / Spanish-speaking users.

The LLM is asked to interpret amounts, but it sometimes misses the multiplier
on slang ("10 lucas", "10k", "10 mil"). When it does, we'd otherwise save
the wrong amount. This module is the source of truth for amount
normalization: it scans the original text and rewrites amounts that have
explicit thousand-multiplier suffixes.

The post-processor runs AFTER the LLM and only acts on expenses whose
``name`` overlaps with a matched amount phrase. It never invents a value —
if the original message doesn't contain a recognizable amount, the LLM's
value is kept (or replaced with None when invalid).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence


@dataclass
class ParsedAmount:
    value: Decimal
    text: str
    multiplier: Decimal
    start: int
    end: int


# Words that mean "thousand" in argentine slang.
THOUSAND_WORDS: tuple[str, ...] = (
    "k",
    "lucas",
    "luca",
    "mil",
    "palos",
    "palo",
    "mangos",
    "mango",
    "gambas",
    "gamba",
    "verdes",
    "verde",
)

# Currency words.
DOLLAR_WORDS: tuple[str, ...] = (
    "dólares",
    "dolares",
    "dólar",
    "dolar",
    "usd",
    "u$s",
    "verde",  # already in thousands, but in some contexts means USD
)
USD_HINT_WORDS: tuple[str, ...] = ("verdes",)


_SUFFIXES_PATTERN = "|".join(THOUSAND_WORDS)
NUM_THEN_SUFFIX = re.compile(
    r"""
    (?<!\d)
    (?P<number>\$?\s*\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)
    \s*
    (?P<suffix>""" + _SUFFIXES_PATTERN + r""")
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Matches plain numbers with a currency hint nearby (e.g. "20 dolares").
PLAIN_NUMBER = re.compile(
    r"(?<!\d)(?P<number>\d{1,3}(?:[.,]\d{3})*|\d+(?:[.,]\d{1,2})?)(?!\d)"
)


def _normalize_decimal(raw: str) -> Decimal | None:
    """Convert a number string with argentine-style punctuation to Decimal.

    Heuristics:
      * If only digits: that's the value.
      * If contains both ',' and '.' and ',' comes after '.': '..,' is
        decimal, '.' is thousands -> "1.234,56" -> 1234.56.
      * If contains ',' and no '.' (and exactly 3 digits after ',' or comma
        appears in a >2-digit cluster): could be thousands.
      * If contains '.': '.' is thousands sep if there are 3 digits after,
        else decimal.
    """
    s = raw.strip().lstrip("$").strip().replace(" ", "")
    if not s:
        return None
    has_comma = "," in s
    has_dot = "." in s

    try:
        if has_comma and has_dot:
            if s.rfind(",") > s.rfind("."):
                return Decimal(s.replace(".", "").replace(",", "."))
            else:
                return Decimal(s.replace(",", ""))
        if has_comma:
            head, _, tail = s.partition(",")
            if len(tail) == 3 and len(head) <= 3:
                return Decimal(s.replace(",", ""))
            return Decimal(s.replace(",", "."))
        if has_dot:
            head, _, tail = s.partition(".")
            if len(tail) == 3 and len(head) <= 3:
                return Decimal(s.replace(".", ""))
            return Decimal(s)
        return Decimal(s)
    except Exception:
        return None


def _multiply_for(suffix: str) -> Decimal:
    return Decimal("1000")


def find_thousand_amounts(text: str) -> list[ParsedAmount]:
    """Return amounts that have a thousand-multiplier suffix in the text."""
    found: list[ParsedAmount] = []
    seen_spans: list[tuple[int, int]] = []
    lower = text.lower()
    for match in NUM_THEN_SUFFIX.finditer(text):
        number_raw = match.group("number").strip()
        suffix = match.group("suffix").lower()
        base = _normalize_decimal(number_raw)
        if base is None:
            continue
        amount = base * _multiply_for(suffix)
        span = (match.start(), match.end())
        if any(span[0] < e and span[1] > s for s, e in seen_spans):
            continue
        seen_spans.append(span)
        found.append(
            ParsedAmount(
                value=amount,
                text=match.group(0),
                multiplier=Decimal("1000"),
                start=span[0],
                end=span[1],
            )
        )
    return found


def find_dollar_amounts(text: str) -> list[ParsedAmount]:
    """Return amounts explicitly tagged as USD.

    Supports both orderings: "20 dolares" and "dolares 20".
    """
    found: list[ParsedAmount] = []
    number_then_word = re.compile(
        r"(?<!\d)(?P<number>\d{1,3}(?:[.,]\d{3})*|\d+(?:[.,]\d{1,2})?)\s*"
        r"(?P<suffix>d[oó]lares?|dolares?|usd|u\$s)\b",
        re.IGNORECASE,
    )
    word_then_number = re.compile(
        r"\b(?P<suffix>usd|u\$s)\s*(?P<number>\d{1,3}(?:[.,]\d{3})*|\d+(?:[.,]\d{1,2})?)",
        re.IGNORECASE,
    )
    for match in number_then_word.finditer(text):
        base = _normalize_decimal(match.group("number"))
        if base is None:
            continue
        found.append(
            ParsedAmount(
                value=base,
                text=match.group(0),
                multiplier=Decimal("1"),
                start=match.start(),
                end=match.end(),
            )
        )
    for match in word_then_number.finditer(text):
        base = _normalize_decimal(match.group("number"))
        if base is None:
            continue
        span = (match.start(), match.end())
        if any(span[0] < e and span[1] > s for s, e in [(f.start, f.end) for f in found]):
            continue
        found.append(
            ParsedAmount(
                value=base,
                text=match.group(0),
                multiplier=Decimal("1"),
                start=span[0],
                end=span[1],
            )
        )
    return found


def detect_currency_hint(text: str) -> str | None:
    """Return 'USD' / 'ARS' / etc. if the message strongly hints at a currency."""
    lower = text.lower()
    if any(w in lower for w in DOLLAR_WORDS):
        return "USD"
    if "peso" in lower or "pesos" in lower or "$" in text:
        return "ARS"
    if any(w in lower for w in THOUSAND_WORDS):
        return "ARS"
    return None


def normalize_amount(
    ai_amount: Decimal | None,
    original_text: str,
) -> Decimal | None:
    """Pick the best amount for a single-expense message.

    If the text contains an explicit thousand-multiplier expression
    ("10 lucas", "10k", "10 mil"), that value wins. Otherwise the AI
    amount is returned.
    """
    parsed = find_thousand_amounts(original_text)
    if parsed:
        return parsed[0].value
    parsed_dollars = find_dollar_amounts(original_text)
    if parsed_dollars:
        return parsed_dollars[0].value
    return ai_amount


def normalize_amounts_multi(
    ai_amounts: Sequence[Decimal | None],
    original_text: str,
) -> list[Decimal | None]:
    """Pairwise redistribution for multi-expense messages.

    For a message like "gasté 5k en café y 12k en Uber" we expect two
    AI amounts (5000 and 12000). We scan the text for the same number of
    thousand-multiplier expressions. If we find a 1-to-1 mapping, we
    override each AI amount with the parsed value. If we find fewer
    parsed amounts than AI amounts, the unmatched AI amounts are kept
    individually fed through ``normalize_amount``. Otherwise the simple
    per-amount single-text logic kicks in.
    """
    parsed = find_thousand_amounts(original_text)
    if not parsed:
        parsed = find_dollar_amounts(original_text)

    if len(parsed) == len(ai_amounts):
        return [p.value for p in parsed]

    if len(parsed) > 0 and len(parsed) < len(ai_amounts):
        out: list[Decimal | None] = []
        for ai_amount, p in zip(ai_amounts, parsed):
            out.append(p.value)
        for ai_amount in ai_amounts[len(parsed):]:
            out.append(normalize_amount(ai_amount, original_text))
        return out

    return [
        normalize_amount(ai_amount, original_text) for ai_amount in ai_amounts
    ]
