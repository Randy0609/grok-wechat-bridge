"""Routing: everything goes to the primary GrokBot. Delegation to an execution agent is
only flagged when the user explicitly asks for it, and the primary GrokBot still owns
the conversation and the reply. Media is never routed anywhere except the primary."""

from __future__ import annotations

import re
from typing import Optional

from .modality import Route

_EXPLICIT_DELEGATION = re.compile(
    r"^\s*(?:/delegate\b|委派执行\s*[:：]|请委派执行\s*[:：])", re.IGNORECASE)


def route_for(text: Optional[str]) -> Route:
    if text and _EXPLICIT_DELEGATION.match(text):
        return Route.PRIMARY_WITH_EXPLICIT_DELEGATION
    return Route.PRIMARY
