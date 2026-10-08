"""Redaction of credentials from every text that leaves the provider (GW-A7).

A ``Redactor`` masks the secrets of one transport and the credential shapes
that can echo back from git: an ``Authorization`` header and URL user-info.
It is applied to git output tails, ``gh`` output and wrapped exception text.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

MASK = "***"

_TOKEN_USER = "x-access-token"
_AUTHORIZATION_HEADER = re.compile(r"authorization:[ \t]*[A-Za-z]+[ \t]+\S+", re.IGNORECASE)
# User-info ends at the LAST '@' of the authority, so an '@' inside the
# password is masked too (greedy match up to the next '/', space or quote).
_URL_USER_INFO = re.compile(r"(?<=://)[^/\s'\"]+@")


def _forms(secret: str) -> set[str]:
    """The secret and the shapes derived from it that git may echo."""
    pair = f"{_TOKEN_USER}:{secret}"
    encoded = base64.b64encode(pair.encode("utf-8")).decode("ascii")
    return {secret, pair, encoded}


@dataclass(frozen=True)
class Redactor:
    """Masks ``secrets``, their derived forms, auth headers and URL user-info."""

    secrets: tuple[str, ...] = ()

    def redact(self, text: str) -> str:
        known = {form for secret in self.secrets if secret for form in _forms(secret)}
        # Longest first: the ``x-access-token:<token>`` pair must go before the
        # bare token it contains, or fragments of the pair would be left behind.
        for form in sorted(known, key=len, reverse=True):
            text = text.replace(form, MASK)
        text = _AUTHORIZATION_HEADER.sub(MASK, text)
        return _URL_USER_INFO.sub(f"{MASK}@", text)
