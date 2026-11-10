"""PII redaction before hosted-provider calls, and re-hydration of the answer.

Detection is rule-based (D-048): e-mail addresses, phone numbers, IBANs, payment card numbers
(Luhn-checked), US social security numbers, names the runtime knows are present in the context
(``metadata.redaction_names`` — partner and user names of the records it rendered), and
honorific-prefixed names. Each distinct value gets a stable placeholder (``[EMAIL_1]``), so the
model can still refer to "the same person"; the gateway restores the values in the answer.
"""

from __future__ import annotations

import re
from typing import Any

KINDS = ("PERSON", "EMAIL", "PHONE", "IBAN", "CARD", "ID")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
PHONE = re.compile(r"(?<![\w/])\+?\(?\d[\d\s().-]{6,}\d(?![\w/])")
HONORIFIC = re.compile(r"\b(?:Mr|Mrs|Ms|Mx|Dr|Prof)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?")
PLACEHOLDER = re.compile(r"\[?\b(" + "|".join(KINDS) + r")_(\d+)\b\]?")
STRICT_PLACEHOLDER = re.compile(r"\[(" + "|".join(KINDS) + r")_(\d+)\]")


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class Redactor:
    def __init__(self, names: list[str] | None = None) -> None:
        self.forward: dict[str, str] = {}  # original → placeholder
        self.backward: dict[str, str] = {}  # placeholder → original
        self.counters: dict[str, int] = dict.fromkeys(KINDS, 0)
        terms: set[str] = set()
        for raw in names or []:
            name = raw.strip()
            if len(name) < 3:
                continue
            terms.add(name)
            # people are often referred to by one part of their name ("Jordan", "Ms Lee")
            terms.update(p for p in re.split(r"[\s,]+", name) if len(p) >= 3 and p[:1].isupper())
        self.name_pattern = (
            re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")\b")
            if terms
            else None
        )

    @property
    def applied(self) -> bool:
        return bool(self.forward)

    def _placeholder(self, kind: str, value: str) -> str:
        if value in self.forward:
            return self.forward[value]
        self.counters[kind] += 1
        ph = f"[{kind}_{self.counters[kind]}]"
        self.forward[value] = ph
        self.backward[ph] = value
        return ph

    def redact(self, text: str) -> str:
        if not text:
            return text
        text = EMAIL.sub(lambda m: self._placeholder("EMAIL", m.group()), text)
        text = IBAN.sub(lambda m: self._placeholder("IBAN", m.group()), text)

        def card(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group())
            return self._placeholder("CARD", m.group()) if 13 <= len(digits) <= 19 and _luhn(digits) else m.group()

        text = CARD.sub(card, text)
        text = SSN.sub(lambda m: self._placeholder("ID", m.group()), text)

        def phone(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group())
            looks_like_date = re.fullmatch(r"\d{4}-\d{2}-\d{2}", m.group().strip()) is not None
            if 8 <= len(digits) <= 15 and not looks_like_date:
                return self._placeholder("PHONE", m.group())
            return m.group()

        text = PHONE.sub(phone, text)
        if self.name_pattern:
            text = self.name_pattern.sub(lambda m: self._placeholder("PERSON", m.group()), text)
        return HONORIFIC.sub(lambda m: self._placeholder("PERSON", m.group()), text)

    def restore(self, text: str) -> str:
        if not text or not self.backward:
            return text

        def sub(m: re.Match[str]) -> str:
            key = f"[{m.group(1)}_{m.group(2)}]"
            return self.backward.get(key, m.group())

        return PLACEHOLDER.sub(sub, text)

    def restore_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.restore(obj)
        if isinstance(obj, list):
            return [self.restore_obj(v) for v in obj]
        if isinstance(obj, dict):
            return {k: self.restore_obj(v) for k, v in obj.items()}
        return obj


def leaked_placeholders(text: str) -> list[str]:
    return [m.group() for m in STRICT_PLACEHOLDER.finditer(text or "")]


class StreamRestorer:
    """Restores placeholders in streamed text, holding back a trailing ``[`` fragment until it closes."""

    def __init__(self, redactor: Redactor | None) -> None:
        self.redactor = redactor
        self.buffer = ""

    def feed(self, text: str) -> str:
        if not self.redactor or not self.redactor.applied:
            return text
        self.buffer += text
        cut = self.buffer.rfind("[")
        if cut != -1 and "]" not in self.buffer[cut:] and len(self.buffer) - cut < 24:
            ready, self.buffer = self.buffer[:cut], self.buffer[cut:]
        else:
            ready, self.buffer = self.buffer, ""
        return self.redactor.restore(ready)

    def flush(self) -> str:
        rest, self.buffer = self.buffer, ""
        return self.redactor.restore(rest) if self.redactor else rest
