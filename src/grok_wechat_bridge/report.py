"""Per-event acceptance record and the per-direction implementation status.

Interface acceptance, platform delivery receipt and owner phone confirmation are kept
as separate fields. Synthetic provenance never counts as live verification."""

from __future__ import annotations

import json

from .modality import Direction, EvidenceKind, Provenance
from .store import Store

LIVE_PROVENANCE = {Provenance.LIVE_PRIMARY_GROKBOT.value, Provenance.BRIDGE_COMPUTED.value,
                   Provenance.WECHAT_PLATFORM.value}

_PERCEPTION_KINDS = {
    EvidenceKind.VOICE_TRANSCRIPT.value, EvidenceKind.VOICE_AUDIO_UNDERSTOOD.value,
    EvidenceKind.IMAGE_VIEW_DETAIL.value, EvidenceKind.VIDEO_VISUAL.value,
    EvidenceKind.VIDEO_AUDIO.value,
}


def acceptance_record(store: Store, event_id: str) -> dict:
    ev = store.get_event(event_id)
    if ev is None:
        raise KeyError(event_id)
    media = list(store.media_for_event(event_id))
    for a in store.associations_for(event_id):
        media.extend(store.media_for_event(a["media_event_id"]))
    evidence = [
        {"kind": r["kind"], "provenance": r["provenance"], "media_sha256": r["media_sha256"],
         "detail": json.loads(r["detail"])}
        for r in store.evidence_for(event_id)
    ]
    perception = [e for e in evidence if e["kind"] in _PERCEPTION_KINDS]
    synthetic_perception = any(e["provenance"] == Provenance.SYNTHETIC.value for e in perception)
    outbound = [
        {
            "idempotency_key": r["idempotency_key"], "slot": r["slot"],
            "direction": r["direction"], "voice_form": r["voice_form"],
            "downgrade_confirmed": bool(r["downgrade_confirmed"]), "state": r["state"],
            "attempts": r["attempts"], "platform_msg_id": r["platform_msg_id"],
            "media_sha256": (store.get_media(r["media_id"])["sha256"] if r["media_id"] else None),
            "interface_accepted": r["interface_accepted_at"] is not None,
            "delivery_receipt": r["delivery_receipt_at"] is not None,
            "phone_confirmed": r["phone_confirmed_at"] is not None,
        }
        for r in store.outbound_for_event(event_id)
    ]
    return {
        "event_id": event_id,
        "conversation_id": ev["conversation_id"],
        "source_msg_id": ev["source_msg_id"],
        "direction": ev["direction"],
        "state": ev["state"],
        "route": ev["route"],
        "primary_agent_id": ev["agent_id"],
        "primary_run_id": ev["run_id"],
        "associations": [dict(a) for a in store.associations_for(event_id)],
        "media": [{"media_id": m["media_id"], "kind": m["kind"], "size": m["size"],
                   "sha256": m["sha256"], "source_event_id": m["event_id"]} for m in media],
        "fetches": [{"agent_id": f["agent_id"], "sha256": f["sha256"], "size": f["size"]}
                    for f in store.fetches_for(event_id)],
        "evidence": evidence,
        "gaps": [{"code": g["code"], "detail": g["detail"]} for g in store.gaps_for(event_id)],
        "outbound": outbound,
        "perception_live_verified": bool(perception) and not synthetic_perception,
        "phone_acceptance_complete": bool(outbound) and all(o["phone_confirmed"] for o in outbound),
    }


# Status of each direction in this increment. "core" = bridge-side logic implemented and
# tested; "synthetic" = exercised only against synthetic adapters; "live" = real WeChat /
# real GrokBot path.
DIRECTION_STATUS: dict[Direction, dict[str, str]] = {
    Direction.TEXT_IN: {
        "core": "implemented: binding, string-ID dedupe, explicit-delegation flag, association",
        "synthetic": "tested",
        "live": "NOT implemented / NOT verified",
    },
    Direction.TEXT_OUT: {
        "core": "implemented: durable idempotency key, unknown->receipt check, receipts split",
        "synthetic": "tested",
        "live": "NOT implemented / NOT verified",
    },
    Direction.VOICE_IN: {
        "core": "implemented: audio capture+sha256, same-message transcript fallback, "
                "ASR/audio capability gate, retrieval+transcript evidence required",
        "synthetic": "tested (transcript is a scripted fixture, NOT ASR)",
        "live": "NOT implemented / NOT verified (no ASR performed)",
    },
    Direction.VOICE_OUT: {
        "core": "implemented: native voice vs audio-attachment recorded separately; "
                "downgrade requires confirmation; container check",
        "synthetic": "tested (bytes are synthetic, NOT TTS, NOT playable-verified)",
        "live": "NOT implemented / NOT verified",
    },
    Direction.IMAGE_IN: {
        "core": "implemented: original bytes, size+sha256, bound ref fetch log, "
                "view-detail evidence required",
        "synthetic": "tested (view detail is scripted, NOT perception)",
        "live": "NOT implemented / NOT verified",
    },
    Direction.IMAGE_OUT: {
        "core": "implemented: image container check, hashed outbound media, idempotent send",
        "synthetic": "tested",
        "live": "NOT implemented / NOT verified (no phone open check)",
    },
    Direction.VIDEO_IN: {
        "core": "implemented: complete-MP4 box check, video/audio track detection, "
                "visual and audio evidence required separately",
        "synthetic": "tested (visual/audio evidence scripted, NOT perception)",
        "live": "NOT implemented / NOT verified",
    },
    Direction.VIDEO_OUT: {
        "core": "implemented: complete-MP4 check, hashed outbound media, idempotent send",
        "synthetic": "tested",
        "live": "NOT implemented / NOT verified (no phone playback check)",
    },
}
