"""Bridge core: one bound private conversation <-> the primary GrokBot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from . import formats
from .adapters import (
    InboundMessage,
    MediaDownloadError,
    OutboundPayload,
    PrimaryGrokBotAdapter,
    ReceiptStatus,
    ReplyPart,
    RunRequest,
    RunResult,
    SendOutcome,
    SendStatusUnknown,
    WeChatAdapter,
)
from .ids import event_id_for, idempotency_key_for, require_str_id
from .media import MediaGateway, MediaHandle, MediaIntegrityError, MediaRefError
from .modality import (
    Capability,
    EvidenceKind,
    Flow,
    Kind,
    Provenance,
    VoiceDeliveryForm,
    direction_for,
)
from .routing import route_for
from .store import EventState, SendState, Store, TERMINAL_SEND_STATES


@dataclass(frozen=True)
class BridgeConfig:
    bound_conversation_id: str
    association_window_s: float = 120.0
    quote_max_age_s: float = 600.0
    max_media_per_question: int = 4
    max_send_attempts: int = 3


class IngestStatus(str, Enum):
    IGNORED_UNBOUND = "ignored_unbound"
    DUPLICATE = "duplicate"
    HELD_FOR_ASSOCIATION = "held_for_association"
    REPLIED = "replied"
    SEND_PENDING = "send_pending"
    FAILED_EXPLICIT = "failed_explicit"


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    event_id: Optional[str] = None
    detail: str = ""


class Gap(str, Enum):
    MEDIA_MISSING = "media_missing"
    MEDIA_DOWNLOAD_FAILED = "media_download_failed"
    MEDIA_INTEGRITY = "media_integrity"
    MEDIA_FORMAT = "media_format"
    VOICE_NO_AUDIO_OR_TRANSCRIPT = "voice_no_audio_or_transcript"
    CAPABILITY_MISSING = "capability_missing"
    MEDIA_REF_ERROR = "media_ref_error"
    RUN_FAILED = "run_failed"
    RUN_MISMATCH = "run_mismatch"
    EVIDENCE_MISSING = "evidence_missing"
    EMPTY_REPLY = "empty_reply"
    QUOTE_UNAVAILABLE = "quote_unavailable"
    ASSOCIATION_AMBIGUOUS = "association_ambiguous"
    OUTBOUND_UNSUPPORTED = "outbound_unsupported"
    OUTBOUND_FORMAT = "outbound_format"
    VOICE_NATIVE_UNAVAILABLE = "voice_native_unavailable"
    VOICE_DOWNGRADE_UNCONFIRMED = "voice_downgrade_unconfirmed"


GAP_TEXT = {
    Gap.MEDIA_MISSING: "这条消息的媒体没有取到，本次无法处理。",
    Gap.MEDIA_DOWNLOAD_FAILED: "媒体下载失败或已过期，本次无法处理，请重新发送。",
    Gap.MEDIA_INTEGRITY: "媒体大小或 SHA-256 校验不一致，已拒绝处理。",
    Gap.MEDIA_FORMAT: "媒体格式不完整或无法识别，已拒绝处理。",
    Gap.VOICE_NO_AUDIO_OR_TRANSCRIPT: "这条语音既没有可用音频，也没有可核实的转写，无法回答。",
    Gap.CAPABILITY_MISSING: "主 GrokBot 当前缺少处理这类媒体所需的能力，本次没有作答。",
    Gap.MEDIA_REF_ERROR: "媒体取件引用失效或不匹配，本次没有作答。",
    Gap.RUN_FAILED: "主 GrokBot 本次运行失败，没有作答。",
    Gap.RUN_MISMATCH: "主 GrokBot 返回的结果与本条消息不对应，已丢弃。",
    Gap.EVIDENCE_MISSING: "缺少实际取件或理解证据，已丢弃未经核实的回答。",
    Gap.EMPTY_REPLY: "主 GrokBot 没有返回回复内容。",
    Gap.QUOTE_UNAVAILABLE: "引用的媒体不可用或已超出关联时限。",
    Gap.ASSOCIATION_AMBIGUOUS: "时间窗内待关联的媒体过多，请引用具体的那一条再提问。",
    Gap.OUTBOUND_UNSUPPORTED: "当前通道不支持发送这类媒体。",
    Gap.OUTBOUND_FORMAT: "待发送媒体格式不完整，已拦截。",
    Gap.VOICE_NATIVE_UNAVAILABLE: "当前通道无法发送原生语音；降级为音频附件需要确认。",
    Gap.VOICE_DOWNGRADE_UNCONFIRMED: "语音降级为音频附件尚未确认，已拦截。",
}

_HOLDABLE = {Kind.IMAGE, Kind.VIDEO}

_RETRIEVAL_EVIDENCE = {
    Kind.VOICE: EvidenceKind.VOICE_AUDIO_RETRIEVED,
    Kind.IMAGE: EvidenceKind.IMAGE_ORIGINAL_BYTES,
    Kind.VIDEO: EvidenceKind.VIDEO_COMPLETE_MP4,
}


class _ExplicitFailure(Exception):
    def __init__(self, gap: Gap, detail: str):
        super().__init__(detail)
        self.gap = gap
        self.detail = detail


class Bridge:
    def __init__(self, *, store: Store, gateway: MediaGateway, wechat: WeChatAdapter,
                 grokbot: PrimaryGrokBotAdapter, config: BridgeConfig,
                 clock: Callable[[], float]):
        self.store = store
        self.gateway = gateway
        self.wechat = wechat
        self.grokbot = grokbot
        self.config = config
        self.clock = clock
        require_str_id(config.bound_conversation_id, "bound_conversation_id")

    # ------------------------------------------------------------------ ingest
    def ingest(self, msg: InboundMessage) -> IngestResult:
        conv = require_str_id(msg.conversation_id, "conversation_id")
        src = require_str_id(msg.source_msg_id, "source_msg_id")
        if msg.quote_source_msg_id is not None:
            require_str_id(msg.quote_source_msg_id, "quote_source_msg_id")
        if conv != self.config.bound_conversation_id:
            # Nothing from unbound conversations is persisted.
            return IngestResult(IngestStatus.IGNORED_UNBOUND)

        now = self.clock()
        eid = event_id_for(conv, src)
        claim = self.store.claim_event(
            event_id=eid, conversation_id=conv, source_msg_id=src,
            direction=direction_for(msg.kind, Flow.INBOUND).value, kind=msg.kind.value,
            text=msg.text, quote_source_msg_id=msg.quote_source_msg_id,
            platform_transcript=msg.platform_transcript, sent_at=msg.sent_at,
            received_at=now, state=EventState.RECEIVED.value)
        if not claim.created:
            return IngestResult(IngestStatus.DUPLICATE, eid, claim.row["state"])

        if msg.text:
            self.store.add_evidence(eid, EvidenceKind.TEXT_RECEIVED.value,
                                    Provenance.BRIDGE_COMPUTED.value, None,
                                    {"chars": len(msg.text)}, now)
        try:
            if msg.kind is not Kind.TEXT:
                self._capture_media(eid, conv, msg)
                if msg.kind in _HOLDABLE and not msg.text:
                    self.store.set_event_state(eid, EventState.PENDING_ASSOCIATION, now)
                    return IngestResult(IngestStatus.HELD_FOR_ASSOCIATION, eid)
            if msg.kind is Kind.TEXT:
                self._associate(eid, conv, msg)
        except _ExplicitFailure as f:
            return self._fail(eid, conv, f.gap, f.detail)
        return self._process(eid)

    def _capture_media(self, eid: str, conv: str, msg: InboundMessage) -> None:
        now = self.clock()
        if msg.kind is Kind.VOICE and msg.platform_transcript:
            self.store.add_evidence(eid, EvidenceKind.VOICE_TRANSCRIPT.value,
                                    Provenance.WECHAT_PLATFORM.value, None,
                                    {"source": "wechat_platform_same_message",
                                     "chars": len(msg.platform_transcript)}, now)
        if msg.media_handle is None:
            if msg.kind is Kind.VOICE and msg.platform_transcript:
                self.store.add_gap(eid, "voice_audio_unavailable",
                                   "no audio handle; answering from same-message transcript", now)
                return
            if msg.kind is Kind.VOICE:
                raise _ExplicitFailure(Gap.VOICE_NO_AUDIO_OR_TRANSCRIPT, "no audio, no transcript")
            raise _ExplicitFailure(Gap.MEDIA_MISSING, "inbound media without handle")
        try:
            data = self.wechat.download_media(msg.media_handle)
        except MediaDownloadError as e:
            if msg.kind is Kind.VOICE and msg.platform_transcript:
                self.store.add_gap(eid, "voice_audio_unavailable", f"download failed: {e}", now)
                return
            if msg.kind is Kind.VOICE:
                raise _ExplicitFailure(Gap.VOICE_NO_AUDIO_OR_TRANSCRIPT, f"download failed: {e}")
            raise _ExplicitFailure(Gap.MEDIA_DOWNLOAD_FAILED, str(e))
        try:
            stored = self.gateway.put(event_id=eid, conversation_id=conv, flow=Flow.INBOUND,
                                      kind=msg.kind, data=data,
                                      declared_size=msg.declared_size,
                                      declared_sha256=msg.declared_sha256)
        except MediaIntegrityError as e:
            raise _ExplicitFailure(Gap.MEDIA_INTEGRITY, str(e))
        except formats.MediaFormatError as e:
            raise _ExplicitFailure(Gap.MEDIA_FORMAT, str(e))
        if msg.kind is Kind.VIDEO and not stored.format_detail.get("has_audio_track"):
            self.store.add_evidence(eid, EvidenceKind.VIDEO_AUDIO_TRACK_ABSENT.value,
                                    Provenance.BRIDGE_COMPUTED.value, stored.sha256,
                                    {"reason": "no soun handler in moov"}, now)

    def _associate(self, eid: str, conv: str, msg: InboundMessage) -> None:
        now = self.clock()
        if msg.quote_source_msg_id is not None:
            target = self.store.find_event(conv, msg.quote_source_msg_id)
            if (target is None or Kind(target["kind"]) not in _HOLDABLE
                    or not self.store.media_for_event(target["event_id"])
                    or now - target["received_at"] > self.config.quote_max_age_s):
                raise _ExplicitFailure(Gap.QUOTE_UNAVAILABLE, "quoted media unavailable")
            self.store.add_association(eid, target["event_id"], "quote", now)
            if target["state"] == EventState.PENDING_ASSOCIATION.value:
                self.store.set_event_state(target["event_id"], EventState.ASSOCIATED, now)
            return
        pending = [r for r in self.store.events_in_state(conv, EventState.PENDING_ASSOCIATION)
                   if now - r["received_at"] <= self.config.association_window_s]
        if not pending:
            return
        if len(pending) > self.config.max_media_per_question:
            raise _ExplicitFailure(Gap.ASSOCIATION_AMBIGUOUS, f"{len(pending)} pending media")
        for r in pending:
            self.store.add_association(eid, r["event_id"], "time_window", now)
            self.store.set_event_state(r["event_id"], EventState.ASSOCIATED, now)

    def tick(self) -> list[IngestResult]:
        """Process held media whose association window closed without a question."""
        now = self.clock()
        out = []
        for r in self.store.events_in_state(self.config.bound_conversation_id,
                                            EventState.PENDING_ASSOCIATION):
            if now - r["received_at"] > self.config.association_window_s:
                out.append(self._process(r["event_id"]))
        return out

    # ----------------------------------------------------------------- process
    def _media_for(self, eid: str) -> list:
        rows = list(self.store.media_for_event(eid))
        for a in self.store.associations_for(eid):
            rows.extend(self.store.media_for_event(a["media_event_id"]))
        return rows

    def _required_capabilities(self, rows) -> set[Capability]:
        need: set[Capability] = set()
        for r in rows:
            kind = Kind(r["kind"])
            if kind is Kind.IMAGE:
                need.add(Capability.IMAGE_VISION)
            elif kind is Kind.VIDEO:
                need.add(Capability.VIDEO_VISUAL)
                if json.loads(r["format_detail"]).get("has_audio_track"):
                    need.add(Capability.VIDEO_AUDIO)
        return need

    def _process(self, eid: str) -> IngestResult:
        ev = self.store.get_event(eid)
        conv = ev["conversation_id"]
        kind = Kind(ev["kind"])
        try:
            rows = self._media_for(eid)
            if kind in (Kind.IMAGE, Kind.VIDEO) and not any(r["event_id"] == eid for r in rows):
                raise _ExplicitFailure(Gap.MEDIA_MISSING, "event media not captured")
            if kind is Kind.VOICE and not rows and not ev["platform_transcript"]:
                raise _ExplicitFailure(Gap.VOICE_NO_AUDIO_OR_TRANSCRIPT, "nothing to answer from")
            caps = self.grokbot.capabilities()
            need = self._required_capabilities(rows)
            missing = sorted(c.value for c in need - caps)
            if any(Kind(r["kind"]) is Kind.VOICE for r in rows) and not (
                    {Capability.ASR, Capability.AUDIO_UNDERSTANDING} & caps):
                if ev["platform_transcript"]:
                    rows = [r for r in rows if Kind(r["kind"]) is not Kind.VOICE]
                    self.store.add_gap(eid, "voice_audio_not_understood",
                                       "primary lacks asr/audio; using same-message transcript",
                                       self.clock())
                else:
                    missing.append("asr|audio_understanding")
            if missing:
                raise _ExplicitFailure(Gap.CAPABILITY_MISSING, ",".join(missing))

            handles = tuple(self.gateway.issue(r["media_id"], for_event_id=eid,
                                               conversation_id=conv) for r in rows)
            route = route_for(ev["text"])
            self.store.set_event_state(eid, EventState.RUNNING, self.clock(), route=route.value,
                                       agent_id=self.grokbot.agent_id)
            req = RunRequest(event_id=eid, conversation_id=conv,
                             source_msg_id=ev["source_msg_id"], route=route, kind=kind,
                             text=ev["text"], media=handles,
                             platform_transcript=ev["platform_transcript"])
            try:
                result = self.grokbot.run(req, self.gateway)
            except MediaRefError as e:
                raise _ExplicitFailure(Gap.MEDIA_REF_ERROR, f"{type(e).__name__}: {e}")
            except Exception as e:  # noqa: BLE001 - adapter failures become explicit gaps
                raise _ExplicitFailure(Gap.RUN_FAILED, f"{type(e).__name__}: {e}")
            finally:
                for h in handles:
                    self.gateway.revoke(h.ref)
            self._accept_result(eid, conv, req, result)
        except _ExplicitFailure as f:
            return self._fail(eid, conv, f.gap, f.detail)
        return self._flush_event(eid)

    def _accept_result(self, eid: str, conv: str, req: RunRequest, result: RunResult) -> None:
        now = self.clock()
        if result.event_id != eid or result.agent_id != self.grokbot.agent_id:
            raise _ExplicitFailure(Gap.RUN_MISMATCH, "result event/agent does not match request")
        allowed = {h.sha256 for h in req.media}
        for ev in result.evidence:
            if ev.media_sha256 is not None and ev.media_sha256 not in allowed:
                raise _ExplicitFailure(Gap.RUN_MISMATCH, "evidence cites media not in this event")
        self.store.set_event_state(eid, EventState.RUNNING, now, run_id=result.run_id)
        for ev in result.evidence:
            self.store.add_evidence(eid, ev.kind.value, ev.provenance.value, ev.media_sha256,
                                    ev.detail, now)

        fetched = {(f["sha256"], f["agent_id"]) for f in self.store.fetches_for(eid)}
        have = {(e.kind, e.media_sha256) for e in result.evidence}
        missing: list[str] = []
        for h in req.media:
            if (h.sha256, result.agent_id) not in fetched:
                missing.append(f"{h.kind.value}:{h.sha256[:12]}:not_retrieved_by_primary")
                continue
            self.store.add_evidence(eid, _RETRIEVAL_EVIDENCE[h.kind].value,
                                    Provenance.BRIDGE_COMPUTED.value, h.sha256,
                                    {"agent_id": result.agent_id, "size": h.size,
                                     "verified": "fetch_log_sha256_size"}, now)
            if h.kind is Kind.IMAGE:
                need = [EvidenceKind.IMAGE_VIEW_DETAIL]
            elif h.kind is Kind.VIDEO:
                need = [EvidenceKind.VIDEO_VISUAL]
                if h.format_detail.get("has_audio_track"):
                    need.append(EvidenceKind.VIDEO_AUDIO)
            else:
                need = []
                if not ({(EvidenceKind.VOICE_TRANSCRIPT, h.sha256),
                         (EvidenceKind.VOICE_AUDIO_UNDERSTOOD, h.sha256)} & have):
                    missing.append(f"voice:{h.sha256[:12]}:no_transcript_or_audio_evidence")
            for k in need:
                if (k, h.sha256) not in have:
                    missing.append(f"{k.value}:{h.sha256[:12]}")
        if missing:
            raise _ExplicitFailure(Gap.EVIDENCE_MISSING, ";".join(missing))
        if not result.replies:
            raise _ExplicitFailure(Gap.EMPTY_REPLY, "no reply parts")
        for i, part in enumerate(result.replies):
            self._plan_part(eid, conv, f"reply.{i}", part)
        self.store.set_event_state(eid, EventState.REPLY_PLANNED, now)

    # ---------------------------------------------------------------- outbound
    def _plan_text(self, eid: str, conv: str, slot: str, text: str) -> None:
        self.store.plan_outbound(idempotency_key=idempotency_key_for(eid, slot), event_id=eid,
                                 conversation_id=conv, slot=slot,
                                 direction=direction_for(Kind.TEXT, Flow.OUTBOUND).value,
                                 kind=Kind.TEXT.value, text=text, now=self.clock())

    def _plan_part(self, eid: str, conv: str, slot: str, part: ReplyPart) -> None:
        caps = self.wechat.capabilities()
        if part.kind is Kind.TEXT:
            if not part.text:
                return self._plan_gap_part(eid, conv, slot, Gap.EMPTY_REPLY, "empty text part")
            return self._plan_text(eid, conv, slot, part.text)
        voice_form = None
        if part.kind is Kind.IMAGE and not caps.image_send:
            return self._plan_gap_part(eid, conv, slot, Gap.OUTBOUND_UNSUPPORTED, "image")
        if part.kind is Kind.VIDEO and not caps.video_send:
            return self._plan_gap_part(eid, conv, slot, Gap.OUTBOUND_UNSUPPORTED, "video")
        if part.kind is Kind.VOICE:
            voice_form = part.voice_form or VoiceDeliveryForm.NATIVE_VOICE
            if voice_form is VoiceDeliveryForm.NATIVE_VOICE and not caps.native_voice_send:
                return self._plan_gap_part(eid, conv, slot, Gap.VOICE_NATIVE_UNAVAILABLE,
                                           "native voice unsupported by channel")
            if voice_form is VoiceDeliveryForm.AUDIO_ATTACHMENT and not part.downgrade_confirmed:
                return self._plan_gap_part(eid, conv, slot, Gap.VOICE_DOWNGRADE_UNCONFIRMED,
                                           "attachment downgrade not confirmed")
        try:
            stored = self.gateway.put(event_id=eid, conversation_id=conv, flow=Flow.OUTBOUND,
                                      kind=part.kind, data=part.data or b"")
        except (formats.MediaFormatError, MediaIntegrityError) as e:
            return self._plan_gap_part(eid, conv, slot, Gap.OUTBOUND_FORMAT, str(e))
        self.store.plan_outbound(
            idempotency_key=idempotency_key_for(eid, slot), event_id=eid, conversation_id=conv,
            slot=slot, direction=direction_for(part.kind, Flow.OUTBOUND).value,
            kind=part.kind.value, media_id=stored.media_id,
            voice_form=voice_form.value if voice_form else None,
            downgrade_confirmed=int(bool(part.downgrade_confirmed)), now=self.clock())

    def _plan_gap_part(self, eid: str, conv: str, slot: str, gap: Gap, detail: str) -> None:
        self.store.add_gap(eid, gap.value, detail, self.clock())
        self._plan_text(eid, conv, slot, "[桥接缺口] " + GAP_TEXT[gap])

    def _fail(self, eid: str, conv: str, gap: Gap, detail: str) -> IngestResult:
        now = self.clock()
        self.store.add_gap(eid, gap.value, detail, now)
        self._plan_text(eid, conv, "gap", "[桥接缺口] " + GAP_TEXT[gap])
        self.store.set_event_state(eid, EventState.FAILED_EXPLICIT, now)
        for row in self.store.outbound_for_event(eid):
            self.send(row["idempotency_key"])
        return IngestResult(IngestStatus.FAILED_EXPLICIT, eid, f"{gap.value}: {detail}")

    def _flush_event(self, eid: str) -> IngestResult:
        for row in self.store.outbound_for_event(eid):
            self.send(row["idempotency_key"])
        rows = self.store.outbound_for_event(eid)
        if rows and all(r["state"] == SendState.INTERFACE_ACCEPTED.value for r in rows):
            self.store.set_event_state(eid, EventState.DONE, self.clock())
            return IngestResult(IngestStatus.REPLIED, eid)
        return IngestResult(IngestStatus.SEND_PENDING, eid,
                            ",".join(f"{r['slot']}={r['state']}" for r in rows))

    def send(self, key: str) -> SendState:
        """Send one planned outbound item at most once per confirmed attempt.

        SENDING/UNKNOWN items are never blindly re-sent: the platform receipt is checked
        first, and only a definitive NOT_FOUND permits another attempt."""
        row = self.store.get_outbound(key)
        state = SendState(row["state"])
        if state in TERMINAL_SEND_STATES:
            return state
        now = self.clock()
        if state in (SendState.SENDING, SendState.UNKNOWN):
            check = self.wechat.check_receipt(row["conversation_id"], key)
            if check.status is ReceiptStatus.FOUND:
                self.store.set_send_state(key, SendState.INTERFACE_ACCEPTED, now,
                                          platform_msg_id=check.platform_msg_id)
                return SendState.INTERFACE_ACCEPTED
            if check.status is ReceiptStatus.INDETERMINATE:
                self.store.set_send_state(key, SendState.UNKNOWN, now,
                                          error="receipt check indeterminate; not retrying")
                return SendState.UNKNOWN
        if row["attempts"] >= self.config.max_send_attempts:
            self.store.set_send_state(key, SendState.FAILED, now, error="max attempts reached")
            return SendState.FAILED

        kind = Kind(row["kind"])
        data = self.gateway.read_internal(row["media_id"]) if row["media_id"] else None
        mime = self.store.get_media(row["media_id"])["mime"] if row["media_id"] else None
        payload = OutboundPayload(
            conversation_id=row["conversation_id"], idempotency_key=key, kind=kind,
            text=row["text"], data=data, mime=mime,
            voice_form=VoiceDeliveryForm(row["voice_form"]) if row["voice_form"] else None)
        self.store.mark_sending(key, now)
        try:
            res = self.wechat.send(payload)
        except SendStatusUnknown as e:
            self.store.set_send_state(key, SendState.UNKNOWN, self.clock(), error=str(e))
            return SendState.UNKNOWN
        except Exception as e:  # noqa: BLE001 - any transport fault leaves status unknown
            self.store.set_send_state(key, SendState.UNKNOWN, self.clock(),
                                      error=f"{type(e).__name__}: {e}")
            return SendState.UNKNOWN
        if res.outcome is SendOutcome.ACCEPTED:
            self.store.set_send_state(key, SendState.INTERFACE_ACCEPTED, self.clock(),
                                      platform_msg_id=res.platform_msg_id)
            return SendState.INTERFACE_ACCEPTED
        self.store.set_send_state(key, SendState.REJECTED, self.clock(), error=res.error)
        return SendState.REJECTED

    # ------------------------------------------------------------- recovery
    def resume(self) -> list[IngestResult]:
        """After a restart: finish unsent outbound and re-drive interrupted events."""
        out = []
        for ev in self.store.events_unfinished():
            state = EventState(ev["state"])
            if state is EventState.REPLY_PLANNED:
                out.append(self._flush_event(ev["event_id"]))
            elif state in (EventState.RECEIVED, EventState.RUNNING):
                out.append(self._process(ev["event_id"]))
        for row in self.store.outbound_needing_attention():
            self.send(row["idempotency_key"])
        for ev in self.store.events_unfinished():
            if EventState(ev["state"]) is EventState.REPLY_PLANNED:
                self._flush_event(ev["event_id"])
        return out

    # --------------------------------------------------------------- receipts
    def record_delivery_receipt(self, key: str) -> None:
        """Platform-reported delivery (distinct from interface acceptance)."""
        self.store.record_delivery_receipt(key, self.clock())

    def record_phone_confirmation(self, key: str, note: str) -> None:
        """Owner's manual confirmation that the phone received/played the item."""
        self.store.record_phone_confirmation(key, note, self.clock())
