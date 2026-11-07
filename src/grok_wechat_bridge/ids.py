"""Identifier handling. WeChat message IDs are opaque strings and must never pass
through a float/int representation that could lose precision."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_ID_RE = re.compile(r"^[0-9A-Za-z_\-:.@]{1,128}$")


class IdPrecisionError(ValueError):
    """Raised when an identifier is not carried as a string."""


def require_str_id(value: Any, field: str = "id") -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise IdPrecisionError(
            f"{field} must be a string to preserve precision, got {type(value).__name__}"
        )
    if not _ID_RE.match(value):
        raise ValueError(f"{field} has invalid format")
    return value


def _reject_float(token: str) -> Any:
    raise IdPrecisionError(f"non-integer numeric token {token!r} is not accepted in envelopes")


def loads_envelope(raw: str) -> Any:
    """Parse a JSON envelope keeping every integer token as its exact digit string.

    Adapters convert known numeric fields (e.g. sizes) explicitly; identifiers stay strings.
    """
    return json.loads(raw, parse_int=str, parse_float=_reject_float)


def _h(*parts: str) -> str:
    m = hashlib.sha256()
    for p in parts:
        b = p.encode("utf-8")
        m.update(len(b).to_bytes(8, "big"))
        m.update(b)
    return m.hexdigest()


def event_id_for(conversation_id: str, source_msg_id: str) -> str:
    return "evt_" + _h("event", conversation_id, source_msg_id)[:32]


def idempotency_key_for(event_id: str, slot: str) -> str:
    return "out_" + _h("outbound", event_id, slot)[:32]
