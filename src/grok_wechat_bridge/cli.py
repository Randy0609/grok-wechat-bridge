"""Command line: ``directions`` prints per-direction status; ``selftest`` drives all eight
directions end to end against synthetic adapters in a throwaway directory."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import fixtures
from .adapters import InboundMessage, ReplyPart
from .bridge import Bridge, BridgeConfig, IngestStatus
from .media import MediaGateway
from .modality import Direction, EvidenceKind, Kind, VoiceDeliveryForm
from .report import DIRECTION_STATUS, acceptance_record
from .store import Store
from .synthetic import ScriptEntry, SyntheticPrimaryGrokBot, SyntheticWeChat

CONV = "syn-conv-0001"


class _Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _directions() -> int:
    print(f"{'direction':28} | {'core':5} | {'synthetic':9} | live")
    for d in Direction:
        s = DIRECTION_STATUS[d]
        print(f"{d.value:28} | {'yes':5} | {'yes':9} | {s['live']}")
        print(f"{'':28}   core: {s['core']}")
        print(f"{'':28}   synthetic: {s['synthetic']}")
    return 0


def _selftest() -> int:
    import hashlib

    with tempfile.TemporaryDirectory(prefix="gwb-selftest-") as tmp:
        clock = _Clock()
        store = Store(str(Path(tmp) / "bridge.sqlite"))
        gateway = MediaGateway(store, Path(tmp) / "blobs", clock)
        wechat = SyntheticWeChat()
        img, vid, voice = fixtures.png(seed=1), fixtures.mp4(seed=2), fixtures.silk_like(seed=3)
        sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
        bot = SyntheticPrimaryGrokBot(script={
            sha(img): ScriptEntry({EvidenceKind.IMAGE_VIEW_DETAIL: {"fixture_label": "2x2 grid"}}),
            sha(vid): ScriptEntry({EvidenceKind.VIDEO_VISUAL: {"fixture_label": "frames"},
                                   EvidenceKind.VIDEO_AUDIO: {"fixture_label": "tone"}}),
            sha(voice): ScriptEntry({EvidenceKind.VOICE_TRANSCRIPT: {"text": "fixture words"}}),
        }, extra_replies=[
            ReplyPart(Kind.VOICE, data=fixtures.wav(), voice_form=VoiceDeliveryForm.NATIVE_VOICE),
            ReplyPart(Kind.IMAGE, data=fixtures.png(seed=9)),
            ReplyPart(Kind.VIDEO, data=fixtures.mp4(seed=8)),
        ])
        wechat.media.update({"h-img": img, "h-vid": vid, "h-voice": voice})
        bridge = Bridge(store=store, gateway=gateway, wechat=wechat, grokbot=bot,
                        config=BridgeConfig(bound_conversation_id=CONV), clock=clock)
        msgs = [
            InboundMessage(CONV, "90071992547409930001", Kind.TEXT, clock.t, text="hello"),
            InboundMessage(CONV, "90071992547409930002", Kind.VOICE, clock.t,
                           media_handle="h-voice"),
            InboundMessage(CONV, "90071992547409930003", Kind.IMAGE, clock.t,
                           media_handle="h-img", text="what is in this picture?"),
            InboundMessage(CONV, "90071992547409930004", Kind.VIDEO, clock.t,
                           media_handle="h-vid", text="describe the video and its sound"),
        ]
        results = [bridge.ingest(m) for m in msgs]
        records = [acceptance_record(store, r.event_id) for r in results]
        directions_seen = {r["direction"] for r in records} | {
            o["direction"] for r in records for o in r["outbound"]}
        ok = (all(r.status is IngestStatus.REPLIED for r in results)
              and directions_seen == {d.value for d in Direction}
              and not any(r["perception_live_verified"] for r in records))
        print(json.dumps({
            "ok": ok,
            "note": "synthetic adapters only; perception/ASR/TTS/phone delivery NOT performed",
            "results": [{"source_msg_id": rec["source_msg_id"], "status": r.status.value,
                         "direction": rec["direction"],
                         "outbound": [f"{o['direction']}={o['state']}"
                                      for o in rec["outbound"]],
                         "perception_live_verified": rec["perception_live_verified"],
                         "phone_acceptance_complete": rec["phone_acceptance_complete"]}
                        for r, rec in zip(results, records)],
            "directions_exercised": sorted(directions_seen),
        }, indent=2, ensure_ascii=False))
        store.close()
        return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="grok-wechat-bridge")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("directions", help="print per-direction implementation status")
    sub.add_parser("selftest", help="run all eight directions against synthetic adapters")
    args = p.parse_args(argv)
    if args.cmd == "directions":
        return _directions()
    return _selftest()


if __name__ == "__main__":
    sys.exit(main())
