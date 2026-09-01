"""Normalize persisted event severity across legacy and structured messages.

The job event tables predate structured runtime messages.  Some callbacks wrote
messages such as ``[SJ88][RUN][ERROR][FINISH] ...`` with the default ``info``
column value.  This module is deliberately dependency-free so both write paths
and the read-only admin log query can share one normalization policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


EVENT_LEVELS = ("debug", "info", "warn", "error", "critical", "other")
_STORED_LEVEL_ALIASES = {
    "debug": "debug",
    "info": "info",
    "warning": "warn",
    "warn": "warn",
    "error": "error",
    "fatal": "critical",
    "critical": "critical",
}
_LEVEL_RANK = {
    "other": 0,
    "debug": 1,
    "info": 2,
    "warn": 3,
    "error": 4,
    "critical": 5,
}
_RUNTIME_PATTERN = re.compile(
    r"^\[SJ88\]\[(?P<scope>[A-Z0-9_]+)\]\[(?P<level>[A-Z0-9_]+)\]"
    r"\[(?P<code>[A-Z0-9_]+)\]\s*(?P<message>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_CRITICAL_MARKERS = ("[CRITICAL]", "[FATAL]")
_ERROR_MARKERS = ("[ERROR]", "[FILE_FAIL]")
_WARN_MARKERS = ("[WARN]", "[WARNING]", "[FILE_SKIP]", "[SKIP]", "[CANCEL]")


@dataclass(frozen=True, slots=True)
class StructuredEvent:
    scope: str
    level: str
    code: str
    message: str


def parse_structured_event(message: str) -> StructuredEvent | None:
    """Parse the existing SJ88 structured prefix without changing the message."""

    match = _RUNTIME_PATTERN.match(str(message or "").strip())
    if not match:
        return None
    level = _STORED_LEVEL_ALIASES.get(match.group("level").strip().lower(), "other")
    return StructuredEvent(
        scope=match.group("scope").strip().upper(),
        level=level,
        code=match.group("code").strip().upper(),
        message=match.group("message").strip(),
    )


def _message_level(message: str) -> str:
    text = str(message or "").strip()
    structured = parse_structured_event(text)
    if structured and structured.level != "other":
        return structured.level

    # Markers are intentionally bounded to the prefix.  A filename or a later
    # stack-trace line containing "[ERROR]" must not upgrade an unrelated event.
    prefix = text[:256].upper()
    if any(marker in prefix for marker in _CRITICAL_MARKERS):
        return "critical"
    if any(marker in prefix for marker in _ERROR_MARKERS):
        return "error"
    if any(marker in prefix for marker in _WARN_MARKERS):
        return "warn"
    return "other"


def effective_event_level(stored_level: str, message: str) -> str:
    """Return the strongest trustworthy severity for an existing event row."""

    stored = _STORED_LEVEL_ALIASES.get(str(stored_level or "").strip().lower(), "other")
    inferred = _message_level(message)
    return stored if _LEVEL_RANK[stored] >= _LEVEL_RANK[inferred] else inferred


def storage_event_level(stored_level: str, message: str) -> str:
    """Normalize a new write; unknown values fail safely to ``info``."""

    effective = effective_event_level(stored_level, message)
    return "info" if effective == "other" else effective


def effective_level_sql(level_expression: str, message_expression: str) -> str:
    """Build the portable SQL CASE used for legacy rows.

    The expressions are internal column aliases supplied by the service, never
    request input.  LOWER/UPPER/SUBSTR/INSTR/CASE work in both SQLite and MySQL.
    Keep this in parity with :func:`effective_event_level` via tests.
    """

    stored = f"LOWER({level_expression})"
    prefix = f"UPPER(SUBSTR({message_expression}, 1, 256))"

    def contains(markers: tuple[str, ...]) -> str:
        return " OR ".join(f"INSTR({prefix}, '{marker}') > 0" for marker in markers)

    return (
        "CASE "
        f"WHEN {stored} IN ('critical','fatal') OR ({contains(_CRITICAL_MARKERS)}) THEN 'critical' "
        f"WHEN {stored} = 'error' OR ({contains(_ERROR_MARKERS)}) THEN 'error' "
        f"WHEN {stored} IN ('warn','warning') OR ({contains(_WARN_MARKERS)}) THEN 'warn' "
        f"WHEN {stored} = 'debug' THEN 'debug' "
        f"WHEN {stored} = 'info' THEN 'info' "
        "ELSE 'other' END"
    )
