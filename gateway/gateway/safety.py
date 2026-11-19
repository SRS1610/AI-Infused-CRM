"""Output screening (``/v1/moderate`` and the flags attached to every completion).

Rule-based and local, so it costs nothing and never sends text anywhere. Blocking flags are those
that make outbound content unsafe to send as-is; warnings are shown to the reviewer.
"""

from __future__ import annotations

import re

from gateway.envelope import ModerateResponse
from gateway.redaction import leaked_placeholders

URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
CREDENTIAL = re.compile(
    r"\b(send|share|provide|give|enter|reply with|tell (?:me|us))\b[^.\n]{0,60}\b"
    r"(password|passcode|pin code|api key|secret key|access token|one[- ]time code|2fa code|security code"
    r"|card number|cvv)\b",
    re.IGNORECASE,
)
COMMAND = re.compile(
    r"(\b(run|execute|paste|type)\b[^.\n]{0,40}\b(terminal|shell|command prompt|powershell|console)\b)"
    r"|(curl\s+[^|\n]+\|\s*(ba|z)?sh)|(\brm\s+-rf\b)|(powershell\s+-(enc|command))",
    re.IGNORECASE,
)
INJECTION_ECHO = re.compile(
    r"\b(ignore|disregard)\b[^.\n]{0,30}\b(previous|prior|above|all)\b[^.\n]{0,20}\binstructions?\b", re.IGNORECASE
)
BLOCKING = {"credential_request", "command_instruction", "placeholder_leak"}


def screen(text: str, context: str = "") -> list[str]:
    flags: list[str] = []
    if CREDENTIAL.search(text or ""):
        flags.append("credential_request")
    if COMMAND.search(text or ""):
        flags.append("command_instruction")
    if leaked_placeholders(text):
        flags.append("placeholder_leak")
    if INJECTION_ECHO.search(text or ""):
        flags.append("injection_echo")
    known = {u.rstrip(".,;") for u in URL.findall(context or "")}
    if any(u.rstrip(".,;") not in known for u in URL.findall(text or "")):
        flags.append("unknown_url")
    return flags


REASONS = {
    "credential_request": "asks the recipient for a password, code or key",
    "command_instruction": "tells the reader to run a command",
    "placeholder_leak": "contains an unresolved privacy placeholder",
    "injection_echo": "repeats an instruction-override phrase found in the input",
    "unknown_url": "links to an address that does not appear in the source material",
}


def moderate(text: str, context: str = "") -> ModerateResponse:
    flags = screen(text, context)
    return ModerateResponse(allowed=not (set(flags) & BLOCKING), flags=flags, reasons=[REASONS[f] for f in flags])
