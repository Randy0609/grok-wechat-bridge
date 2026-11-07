import unittest

from grok_wechat_bridge import fixtures
from grok_wechat_bridge.adapters import InboundMessage
from grok_wechat_bridge.bridge import IngestStatus
from grok_wechat_bridge.ids import IdPrecisionError, event_id_for
from grok_wechat_bridge.modality import Capability, EvidenceKind, Kind, Route
from grok_wechat_bridge.report import acceptance_record
from grok_wechat_bridge.synthetic import ScriptEntry

from support import BIG_ID, CONV, OTHER_CONV, BridgeTestCase, sha


def msg(src, kind=Kind.TEXT, conv=CONV, **kw):
    return InboundMessage(conversation_id=conv, source_msg_id=src, kind=kind, sent_at=0.0, **kw)


class TextInboundTests(BridgeTestCase):
    def test_text_replied_and_correlated(self):
        r = self.bridge.ingest(msg(BIG_ID, text="hi"))
        self.assertIs(r.status, IngestStatus.REPLIED)
        rec = acceptance_record(self.store, r.event_id)
        self.assertEqual(rec["source_msg_id"], BIG_ID)
        self.assertEqual(rec["primary_agent_id"], self.bot.agent_id)
        self.assertTrue(rec["primary_run_id"])
        self.assertEqual(rec["route"], Route.PRIMARY.value)
        self.assertEqual(self.bot.runs[0].source_msg_id, BIG_ID)
        self.assertEqual(len(self.wechat.delivered), 1)
        self.assertTrue(rec["outbound"][0]["interface_accepted"])
        self.assertFalse(rec["outbound"][0]["delivery_receipt"])
        self.assertFalse(rec["outbound"][0]["phone_confirmed"])

    def test_duplicate_processed_once_across_restart(self):
        self.bridge.ingest(msg(BIG_ID, text="hi"))
        self.assertIs(self.bridge.ingest(msg(BIG_ID, text="hi")).status, IngestStatus.DUPLICATE)
        self.restart()
        self.assertIs(self.bridge.ingest(msg(BIG_ID, text="hi")).status, IngestStatus.DUPLICATE)
        self.bridge.resume()
        self.assertEqual(len(self.bot.runs), 1)
        self.assertEqual(len(self.wechat.delivered), 1)

    def test_adjacent_big_ids_are_distinct(self):
        a = self.bridge.ingest(msg(BIG_ID, text="a"))
        b = self.bridge.ingest(msg(BIG_ID[:-1] + "4", text="b"))
        self.assertIs(b.status, IngestStatus.REPLIED)
        self.assertNotEqual(a.event_id, b.event_id)

    def test_numeric_id_rejected(self):
        with self.assertRaises(IdPrecisionError):
            self.bridge.ingest(msg(int(BIG_ID), text="x"))

    def test_unbound_conversation_ignored_and_not_persisted(self):
        r = self.bridge.ingest(msg("1", conv=OTHER_CONV, text="hi"))
        self.assertIs(r.status, IngestStatus.IGNORED_UNBOUND)
        self.assertIsNone(self.store.get_event(event_id_for(OTHER_CONV, "1")))
        self.assertEqual(self.bot.runs, [])
        self.assertEqual(self.wechat.delivered, [])

    def test_explicit_delegation_only(self):
        self.bridge.ingest(msg("1", text="please run the deploy"))
        self.bridge.ingest(msg("2", text="/delegate run the test suite"))
        self.bridge.ingest(msg("3", text="委派执行：整理日志"))
        routes = [r.route for r in self.bot.runs]
        self.assertEqual(routes, [Route.PRIMARY, Route.PRIMARY_WITH_EXPLICIT_DELEGATION,
                                  Route.PRIMARY_WITH_EXPLICIT_DELEGATION])
        # Delegated asks still return through the primary GrokBot's reply path.
        self.assertEqual(len(self.wechat.delivered), 3)

    def test_run_mismatch_discarded(self):
        self.bot.override_event_id = "evt_some_older_task"
        r = self.bridge.ingest(msg("1", text="hi"))
        self.assertIs(r.status, IngestStatus.FAILED_EXPLICIT)
        self.assertIn("run_mismatch", self.gap_codes(r.event_id))
        self.assertTrue(all(t.startswith("[桥接缺口]") for t in self.sent_texts()))

    def test_run_exception_explicit(self):
        self.bot.raise_on_run = RuntimeError("synthetic outage")
        r = self.bridge.ingest(msg("1", text="hi"))
        self.assertIs(r.status, IngestStatus.FAILED_EXPLICIT)
        self.assertIn("run_failed", self.gap_codes(r.event_id))


class VoiceInboundTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.audio = fixtures.silk_like(seed=5)
        self.wechat.media["h-voice"] = self.audio

    def test_audio_retrieved_and_transcript_evidence(self):
        self.bot.script[sha(self.audio)] = ScriptEntry(
            {EvidenceKind.VOICE_TRANSCRIPT: {"text": "fixture"}})
        r = self.bridge.ingest(msg("1", Kind.VOICE, media_handle="h-voice"))
        self.assertIs(r.status, IngestStatus.REPLIED)
        ev = self.evidence_kinds(r.event_id)
        self.assertIn(("voice.audio_retrieved", "bridge_computed"), ev)
        self.assertIn(("voice.transcript", "synthetic"), ev)
        rec = acceptance_record(self.store, r.event_id)
        self.assertFalse(rec["perception_live_verified"])
        self.assertEqual(rec["fetches"][0]["sha256"], sha(self.audio))

    def test_no_audio_no_transcript_fails_explicitly(self):
        r = self.bridge.ingest(msg("1", Kind.VOICE))
        self.assertIs(r.status, IngestStatus.FAILED_EXPLICIT)
        self.assertIn("voice_no_audio_or_transcript", self.gap_codes(r.event_id))
        self.assertEqual(self.bot.runs, [])

    def test_expired_audio_without_transcript(self):
        r = self.bridge.ingest(msg("1", Kind.VOICE, media_handle="h-gone"))
        self.assertIs(r.status, IngestStatus.FAILED_EXPLICIT)
        self.assertEqual(self.bot.runs, [])

    def test_same_message_transcript_fallback_is_recorded(self):
        r = self.bridge.ingest(msg("1", Kind.VOICE, media_handle="h-gone",
                                   platform_transcript="fixture words"))
        self.assertIs(r.status, IngestStatus.REPLIED)
        self.assertIn(("voice.transcript", "wechat_platform"), self.evidence_kinds(r.event_id))
        self.assertIn("voice_audio_unavailable", self.gap_codes(r.event_id))
        self.assertNotIn("voice.audio_retrieved", [k for k, _ in self.evidence_kinds(r.event_id)])

    def test_missing_asr_capability(self):
        self.bot.caps = frozenset(Capability) - {Capability.ASR, Capability.AUDIO_UNDERSTANDING}
        r = self.bridge.ingest(msg("1", Kind.VOICE, media_handle="h-voice"))
        self.assertIs(r.status, IngestStatus.FAILED_EXPLICIT)
        self.assertIn("capability_missing", self.gap_codes(r.event_id))
        self.assertEqual(self.bot.runs, [])

    def test_answer_without_understanding_evidence_is_not_sent(self):
        r = self.bridge.ingest(msg("1", Kind.VOICE, media_handle="h-voice"))
        self.assertIs(r.status, IngestStatus.FAILED_EXPLICIT)
        self.assertIn("evidence_missing", self.gap_codes(r.event_id))
        self.assertTrue(all(t.startswith("[桥接缺口]") for t in self.sent_texts()))


class ImageInboundTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.img = fixtures.png(seed=7)
        self.wechat.media["h-img"] = self.img
        self.bot.script[sha(self.img)] = ScriptEntry(
            {EvidenceKind.IMAGE_VIEW_DETAIL: {"fixture_label": "grid"}})

    def test_original_bytes_and_view_detail(self):
        r = self.bridge.ingest(msg("1", Kind.IMAGE, media_handle="h-img", text="what?",
                                   declared_size=len(self.img)))
        self.assertIs(r.status, IngestStatus.REPLIED)
        rec = acceptance_record(self.store, r.event_id)
        self.assertEqual(rec["media"][0]["sha256"], sha(self.img))
        self.assertEqual(rec["media"][0]["size"], len(self.img))
        self.assertEqual(rec["fetches"], [{"agent_id": self.bot.agent_id, "sha256": sha(self.img),
                                           "size": len(self.img)}])
        kinds = self.evidence_kinds(r.event_id)
        self.assertIn(("image.original_bytes", "bridge_computed"), kinds)
        self.assertIn(("image.view_detail", "synthetic"), kinds)
        self.assertEqual(self.bot.runs[0].route, Route.PRIMARY)

    def test_media_goes_to_primary_even_with_delegation_wording(self):
        self.bridge.ingest(msg("1", Kind.IMAGE, media_handle="h-img", text="/delegate fix it"))
        self.assertEqual(len(self.bot.runs), 1)
        self.assertEqual(self.bot.runs[0].media[0].sha256, sha(self.img))

    def test_missing_vision_capability(self):
        self.bot.caps = frozenset(Capability) - {Capability.IMAGE_VISION}
        r = self.bridge.ingest(msg("1", Kind.IMAGE, media_handle="h-img", text="what?"))
        self.assertIs(r.status, IngestStatus.FAILED_EXPLICIT)
        self.assertIn("capability_missing", self.gap_codes(r.event_id))
        self.assertEqual(self.bot.runs, [])

    def test_primary_that_never_fetched_is_rejected(self):
        self.bot.skip_fetch = True
        r = self.bridge.ingest(msg("1", Kind.IMAGE, media_handle="h-img", text="what?"))
        self.assertIs(r.status, IngestStatus.FAILED_EXPLICIT)
        self.assertIn("evidence_missing", self.gap_codes(r.event_id))

    def test_evidence_for_other_media_is_rejected(self):
        self.bot.stale_evidence_sha256 = sha(fixtures.png(seed=99))
        r = self.bridge.ingest(msg("1", Kind.IMAGE, media_handle="h-img", text="what?"))
        self.assertIs(r.status, IngestStatus.FAILED_EXPLICIT)
        self.assertIn("run_mismatch", self.gap_codes(r.event_id))

    def test_declared_size_mismatch(self):
        r = self.bridge.ingest(msg("1", Kind.IMAGE, media_handle="h-img", text="?",
                                   declared_size=len(self.img) + 1))
        self.assertIn("media_integrity", self.gap_codes(r.event_id))
        self.assertEqual(self.bot.runs, [])

    def test_expired_download(self):
        r = self.bridge.ingest(msg("1", Kind.IMAGE, media_handle="h-expired", text="?"))
        self.assertIn("media_download_failed", self.gap_codes(r.event_id))
        self.assertEqual(self.bot.runs, [])

    def test_expired_ref_during_run(self):
        self.store.close()
        self._open(ttl=0)
        r = self.bridge.ingest(msg("1", Kind.IMAGE, media_handle="h-img", text="?"))
        self.assertIs(r.status, IngestStatus.FAILED_EXPLICIT)
        self.assertIn("media_ref_error", self.gap_codes(r.event_id))


class VideoInboundTests(BridgeTestCase):
    def test_visual_and_audio_judged_separately(self):
        vid = fixtures.mp4(seed=1)
        self.wechat.media["h"] = vid
        self.bot.script[sha(vid)] = ScriptEntry({EvidenceKind.VIDEO_VISUAL: {"f": 1},
                                                 EvidenceKind.VIDEO_AUDIO: {"f": 2}})
        r = self.bridge.ingest(msg("1", Kind.VIDEO, media_handle="h", text="describe"))
        self.assertIs(r.status, IngestStatus.REPLIED)
        kinds = [k for k, _ in self.evidence_kinds(r.event_id)]
        for k in ("video.complete_mp4", "video.visual", "video.audio"):
            self.assertIn(k, kinds)

    def test_visual_without_audio_evidence_fails(self):
        vid = fixtures.mp4(seed=2)
        self.wechat.media["h"] = vid
        self.bot.script[sha(vid)] = ScriptEntry({EvidenceKind.VIDEO_VISUAL: {"f": 1}})
        r = self.bridge.ingest(msg("1", Kind.VIDEO, media_handle="h", text="describe"))
        self.assertIs(r.status, IngestStatus.FAILED_EXPLICIT)
        self.assertIn("video.audio", self.store.gaps_for(r.event_id)[0]["detail"])

    def test_silent_video_records_absent_audio_track(self):
        vid = fixtures.mp4(audio=False, seed=3)
        self.wechat.media["h"] = vid
        self.bot.caps = frozenset(Capability) - {Capability.VIDEO_AUDIO}
        self.bot.script[sha(vid)] = ScriptEntry({EvidenceKind.VIDEO_VISUAL: {"f": 1}})
        r = self.bridge.ingest(msg("1", Kind.VIDEO, media_handle="h", text="describe"))
        self.assertIs(r.status, IngestStatus.REPLIED)
        self.assertIn(("video.audio_track_absent", "bridge_computed"),
                      self.evidence_kinds(r.event_id))

    def test_missing_video_audio_capability(self):
        self.wechat.media["h"] = fixtures.mp4(seed=4)
        self.bot.caps = frozenset(Capability) - {Capability.VIDEO_AUDIO}
        r = self.bridge.ingest(msg("1", Kind.VIDEO, media_handle="h", text="describe"))
        self.assertIn("capability_missing", self.gap_codes(r.event_id))
        self.assertEqual(self.bot.runs, [])

    def test_incomplete_mp4_rejected(self):
        self.wechat.media["h"] = fixtures.mp4(seed=5)[:-7]
        r = self.bridge.ingest(msg("1", Kind.VIDEO, media_handle="h", text="describe"))
        self.assertIn("media_format", self.gap_codes(r.event_id))
        self.assertEqual(self.bot.runs, [])


class AssociationTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.img = fixtures.png(seed=11)
        self.wechat.media["h-img"] = self.img
        self.bot.script[sha(self.img)] = ScriptEntry(
            {EvidenceKind.IMAGE_VIEW_DETAIL: {"fixture_label": "grid"}})

    def test_media_then_question_within_window(self):
        held = self.bridge.ingest(msg("1", Kind.IMAGE, media_handle="h-img"))
        self.assertIs(held.status, IngestStatus.HELD_FOR_ASSOCIATION)
        self.assertEqual(self.bot.runs, [])
        self.clock.advance(30)
        r = self.bridge.ingest(msg("2", text="what is in it?"))
        self.assertIs(r.status, IngestStatus.REPLIED)
        self.assertEqual([h.sha256 for h in self.bot.runs[0].media], [sha(self.img)])
        rec = acceptance_record(self.store, r.event_id)
        self.assertEqual(rec["associations"][0]["reason"], "time_window")
        self.assertEqual(rec["media"][0]["source_event_id"], held.event_id)
        self.clock.advance(1000)
        self.assertEqual(self.bridge.tick(), [])

    def test_window_expiry_processes_media_alone_and_does_not_attach_later(self):
        self.bridge.ingest(msg("1", Kind.IMAGE, media_handle="h-img"))
        self.clock.advance(121)
        r = self.bridge.ingest(msg("2", text="unrelated question"))
        self.assertEqual(self.bot.runs[0].media, ())
        self.assertIs(r.status, IngestStatus.REPLIED)
        out = self.bridge.tick()
        self.assertEqual([x.status for x in out], [IngestStatus.REPLIED])
        self.assertEqual(len(self.bot.runs[1].media), 1)

    def test_quote_association(self):
        first = self.bridge.ingest(msg("1", Kind.IMAGE, media_handle="h-img"))
        self.clock.advance(200)
        self.bridge.tick()
        self.clock.advance(100)
        r = self.bridge.ingest(msg("2", text="about that picture", quote_source_msg_id="1"))
        self.assertIs(r.status, IngestStatus.REPLIED)
        self.assertEqual(self.bot.runs[-1].media[0].source_event_id, first.event_id)

    def test_quote_unknown_or_stale_is_explicit(self):
        r = self.bridge.ingest(msg("2", text="about that", quote_source_msg_id="404"))
        self.assertIn("quote_unavailable", self.gap_codes(r.event_id))
        self.bridge.ingest(msg("3", Kind.IMAGE, media_handle="h-img"))
        self.clock.advance(601)
        r = self.bridge.ingest(msg("4", text="about that", quote_source_msg_id="3"))
        self.assertIn("quote_unavailable", self.gap_codes(r.event_id))

    def test_too_many_pending_is_ambiguous(self):
        for i in range(5):
            self.wechat.media[f"h{i}"] = fixtures.png(seed=20 + i)
            self.bridge.ingest(msg(f"m{i}", Kind.IMAGE, media_handle=f"h{i}"))
        r = self.bridge.ingest(msg("q", text="compare them"))
        self.assertIn("association_ambiguous", self.gap_codes(r.event_id))
        self.assertEqual(self.bot.runs, [])


if __name__ == "__main__":
    unittest.main()
