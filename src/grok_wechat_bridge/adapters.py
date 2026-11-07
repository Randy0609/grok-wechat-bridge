"""Adapter boundaries between the bridge core and the outside world.

Live implementations are NOT implemented and NOT verified in this increment; see
``LiveWeChatAdapter`` / ``LivePrimaryGrokBotAdapter``. Only synthetic adapters
(``grok_wechat_bridge.synthetic``) are wired into tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional, Protocol

from .modality import Capability, EvidenceKind, Kind, Provenance, Route, VoiceDeliveryForm

if TYPE_CHECKING:
    from .media import MediaGateway, MediaHandle


# ---------------------------------------------------------------- WeChat side
@dataclass(frozen=True)
class InboundMessage:
    conversation_id: str
    source_msg_id: str
    kind: Kind
    sent_at: float
    text: Optional[str] = None
    media_handle: Optional[str] = None
    declared_size: Optional[int] = None
    declared_sha256: Optional[str] = None
    quote_source_msg_id: Optional[str] = None
    platform_transcript: Optional[str] = None


class MediaDownloadError(RuntimeError):
    """The platform could not provide the bytes (expired, missing, network)."""


@dataclass(frozen=True)
class OutboundPayload:
    conversation_id: str
    idempotency_key: str
    kind: Kind
    text: Optional[str] = None
    data: Optional[bytes] = None
    mime: Optional[str] = None
    voice_form: Optional[VoiceDeliveryForm] = None


class SendOutcome(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SendResult:
    outcome: SendOutcome
    platform_msg_id: Optional[str] = None
    error: Optional[str] = None


class SendStatusUnknown(RuntimeError):
    """Transport ended without a definitive answer (timeout, connection reset)."""


class ReceiptStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class ReceiptCheck:
    status: ReceiptStatus
    platform_msg_id: Optional[str] = None


@dataclass(frozen=True)
class WeChatCapabilities:
    native_voice_send: bool
    image_send: bool
    video_send: bool
    receipt_lookup: bool


class WeChatAdapter(Protocol):
    def capabilities(self) -> WeChatCapabilities: ...

    def download_media(self, media_handle: str) -> bytes: ...

    def send(self, payload: OutboundPayload) -> SendResult: ...

    def check_receipt(self, conversation_id: str, idempotency_key: str) -> ReceiptCheck: ...


# ---------------------------------------------------------------- GrokBot side
@dataclass(frozen=True)
class RunRequest:
    event_id: str
    conversation_id: str
    source_msg_id: str
    route: Route
    kind: Kind
    text: Optional[str]
    media: tuple["MediaHandle", ...]
    platform_transcript: Optional[str]


@dataclass(frozen=True)
class Evidence:
    kind: EvidenceKind
    provenance: Provenance
    media_sha256: Optional[str]
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReplyPart:
    kind: Kind
    text: Optional[str] = None
    data: Optional[bytes] = None
    voice_form: Optional[VoiceDeliveryForm] = None
    downgrade_confirmed: bool = False


@dataclass(frozen=True)
class RunResult:
    event_id: str
    agent_id: str
    run_id: str
    replies: tuple[ReplyPart, ...]
    evidence: tuple[Evidence, ...]


class PrimaryGrokBotAdapter(Protocol):
    agent_id: str

    def capabilities(self) -> frozenset[Capability]: ...

    def run(self, request: RunRequest, gateway: "MediaGateway") -> RunResult: ...


# ---------------------------------------------------------------- live stubs
class NotImplementedLiveIntegration(NotImplementedError):
    pass


class LiveWeChatAdapter:
    """Live WeChat transport: NOT implemented, NOT verified."""

    def __init__(self, *_, **__):
        raise NotImplementedLiveIntegration(
            "Live WeChat adapter is NOT implemented in increment 01; no real WeChat "
            "messages are read or sent.")


class LivePrimaryGrokBotAdapter:
    """Live primary GrokBot invocation: NOT implemented, NOT verified."""

    def __init__(self, *_, **__):
        raise NotImplementedLiveIntegration(
            "Live primary GrokBot adapter is NOT implemented in increment 01; no perception, "
            "ASR or TTS is performed.")
