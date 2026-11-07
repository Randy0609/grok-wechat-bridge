"""Synthetic adapters for tests and the self-test command.

These adapters move real bytes through the bridge boundaries (download, hash, bound
ref fetch, send, receipt lookup) but perform NO perception, ASR, TTS or mobile
delivery. Every piece of understanding evidence they emit is labelled
``Provenance.SYNTHETIC`` and comes from a fixture script keyed by SHA-256."""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from typing import Optional

from .adapters import (
    Evidence,
    MediaDownloadError,
    OutboundPayload,
    ReceiptCheck,
    ReceiptStatus,
    ReplyPart,
    RunRequest,
    RunResult,
    SendOutcome,
    SendResult,
    SendStatusUnknown,
    WeChatCapabilities,
)
from .media import MediaGateway
from .modality import Capability, EvidenceKind, Kind, Provenance


@dataclass
class SyntheticWeChat:
    caps: WeChatCapabilities = field(default_factory=lambda: WeChatCapabilities(
        native_voice_send=True, image_send=True, video_send=True, receipt_lookup=True))
    media: dict[str, bytes] = field(default_factory=dict)
    delivered: list[OutboundPayload] = field(default_factory=list)
    send_calls: int = 0
    # Failure injection, consumed in order per send call.
    send_faults: list[str] = field(default_factory=list)
    receipt_indeterminate: bool = False
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _by_key: dict[str, str] = field(default_factory=dict)

    def capabilities(self) -> WeChatCapabilities:
        return self.caps

    def download_media(self, media_handle: str) -> bytes:
        if media_handle not in self.media:
            raise MediaDownloadError("synthetic: handle missing or expired")
        return self.media[media_handle]

    def send(self, payload: OutboundPayload) -> SendResult:
        self.send_calls += 1
        fault = self.send_faults.pop(0) if self.send_faults else None
        if fault == "timeout_before_delivery":
            raise SendStatusUnknown("synthetic timeout (not delivered)")
        if fault == "reject":
            return SendResult(SendOutcome.REJECTED, error="synthetic rejection")
        # No platform-side dedupe here on purpose, so duplicate sends stay observable.
        msg_id = f"syn-out-{next(self._ids)}"
        self.delivered.append(payload)
        self._by_key[payload.idempotency_key] = msg_id
        if fault == "timeout_after_delivery":
            raise SendStatusUnknown("synthetic timeout (actually delivered)")
        return SendResult(SendOutcome.ACCEPTED, platform_msg_id=msg_id)

    def check_receipt(self, conversation_id: str, idempotency_key: str) -> ReceiptCheck:
        if self.receipt_indeterminate:
            return ReceiptCheck(ReceiptStatus.INDETERMINATE)
        if idempotency_key in self._by_key:
            return ReceiptCheck(ReceiptStatus.FOUND, self._by_key[idempotency_key])
        return ReceiptCheck(ReceiptStatus.NOT_FOUND)

    def deliveries_for(self, key: str) -> int:
        return sum(1 for p in self.delivered if p.idempotency_key == key)


@dataclass
class ScriptEntry:
    """Scripted evidence for one media SHA-256. Values are fixture text, not perception."""

    evidence: dict[EvidenceKind, dict]
    reply_text: Optional[str] = None


@dataclass
class SyntheticPrimaryGrokBot:
    agent_id: str = "synthetic-primary-grokbot"
    caps: frozenset[Capability] = frozenset(Capability)
    script: dict[str, ScriptEntry] = field(default_factory=dict)
    extra_replies: list[ReplyPart] = field(default_factory=list)
    skip_fetch: bool = False
    raise_on_run: Optional[Exception] = None
    override_event_id: Optional[str] = None
    stale_evidence_sha256: Optional[str] = None
    runs: list[RunRequest] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def capabilities(self) -> frozenset[Capability]:
        return self.caps

    def run(self, request: RunRequest, gateway: MediaGateway) -> RunResult:
        self.runs.append(request)
        if self.raise_on_run is not None:
            raise self.raise_on_run
        evidence: list[Evidence] = []
        lines = []
        for h in request.media:
            if not self.skip_fetch:
                data = gateway.fetch(h.ref, event_id=request.event_id,
                                     conversation_id=request.conversation_id,
                                     agent_id=self.agent_id)
                assert hashlib.sha256(data).hexdigest() == h.sha256
            entry = self.script.get(h.sha256)
            if entry is None:
                continue
            for kind, detail in entry.evidence.items():
                evidence.append(Evidence(kind, Provenance.SYNTHETIC, h.sha256,
                                         dict(detail, synthetic=True)))
            if entry.reply_text:
                lines.append(entry.reply_text)
        if self.stale_evidence_sha256:
            evidence.append(Evidence(EvidenceKind.IMAGE_VIEW_DETAIL, Provenance.SYNTHETIC,
                                     self.stale_evidence_sha256, {"synthetic": True}))
        if request.text:
            lines.append(f"[synthetic reply] {request.route.value}: {request.text}")
        elif request.platform_transcript:
            lines.append(f"[synthetic reply] transcript: {request.platform_transcript}")
        if not lines and request.media:
            lines.append("[synthetic reply] media received")
        replies = tuple([ReplyPart(Kind.TEXT, text="\n".join(lines))] + self.extra_replies) \
            if lines else tuple(self.extra_replies)
        return RunResult(
            event_id=self.override_event_id or request.event_id,
            agent_id=self.agent_id,
            run_id=f"syn-run-{next(self._ids)}",
            replies=replies,
            evidence=tuple(evidence),
        )
