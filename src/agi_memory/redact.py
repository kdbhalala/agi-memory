"""Keep credentials out of memory.

Everything recorded is mirrored into the vault and pushed to its git remote, so
a key captured in passing -- an agent quoting a config file, a security note
naming the exposed value -- lives on in that history. Redaction runs where
memories enter: SessionLayer.record and import_from_vault.

Deliberately conservative: known key formats, credentials in URLs, and
`password = value` style assignments whose value looks like a credential. A
secret written as prose ("the token is ...") is not caught; `agi-memory redact
--values FILE` removes known literals from the whole store.
"""
from __future__ import annotations

import re
from typing import Iterable

REDACTED = "[REDACTED]"

_TOKENS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)
# scheme://user:password@host keeps the user and host.
_URL_CREDS = re.compile(r"([a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]{3,}(@)")
# Brackets, quotes and backslashes end a value, so a type such as
# Option<String> is not a value and redaction never splits a JSON escape.
_ASSIGN = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|secret|client_secret|api[_-]?key|apikey|access[_-]?token"
    r"|auth[_-]?token|private[_-]?key)\b[\"']?\s*(?::|=>|=)\s*[\"'`]?)([^\s\"'`,;\\<>()\[\]{}]{8,})")


def _looks_like_credential(value: str) -> bool:
    if not re.search(r"\d", value) and re.fullmatch(r"[A-Za-z_][\w.\-]*", value):
        return False  # an identifier: self.password_field, config.apiKey
    if value.startswith("$") or re.match(r"(?i)(x{3,}|\*{3,}|your|example|placeholder|changeme)", value):
        return False  # an env reference or placeholder
    classes = sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    return classes >= 3 or (classes == 2 and len(value) >= 16)


_SECRET_WORD = r"(?:pass|passwd|password|pwd|token|secret|key|auth|cred|bearer)"
_QVAL = r"(\"[^\"]*\"|'[^']*'|\S+)"
# Shell-command forms redact() misses. Only for command lines: in prose,
# `-pPASS`-style masking would eat ordinary text.
_CMD_SECRETS = (
    re.compile(r"(?i)(\bbearer\s+)" + _QVAL),                                  # Authorization: Bearer x
    re.compile(r"(?i)([\"']?--[\w-]*" + _SECRET_WORD + r"[\w-]*[\"']?[=\s]\s*)" + _QVAL),  # --password x, "--api-key"=x
    re.compile(r"(?i)(\b[A-Z_]*" + _SECRET_WORD + r"[A-Z_]*=)" + _QVAL),       # GH_TOKEN=x cmd
    re.compile(r"((?:^|\s)-[uHP]\s+)" + _QVAL),                                # curl -u u:p, -H "X-Key: k", -P pw
)
_ATTACHED_P = re.compile(r"((?:^|\s)-p)(?=[^\s-])\S+")                          # mysql -pPASS


def redact_command(cmd):
    """redact() plus the flag, header and env-var forms secrets take in a shell line."""
    if not cmd or not isinstance(cmd, str):
        return cmd
    cmd = redact(cmd)
    for pattern in _CMD_SECRETS:
        cmd = pattern.sub(lambda m: m.group(1) + REDACTED, cmd)
    return _ATTACHED_P.sub(lambda m: m.group(1) + REDACTED, cmd)


def redact(text, extra: Iterable[str] = ()):
    """Return text with credentials replaced by [REDACTED]. Non-strings pass through."""
    if not text or not isinstance(text, str):
        return text
    for value in extra:
        if value:
            text = text.replace(value, REDACTED)
    for pattern in _TOKENS:
        text = pattern.sub(REDACTED, text)
    text = _URL_CREDS.sub(lambda m: m.group(1) + REDACTED + m.group(2), text)
    return _ASSIGN.sub(
        lambda m: m.group(1) + REDACTED if _looks_like_credential(m.group(2)) else m.group(0), text)
