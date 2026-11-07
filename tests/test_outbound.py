import unittest

from grok_wechat_bridge import fixtures
from grok_wechat_bridge.adapters import InboundMessage, ReplyPart, WeChatCapabilities
from grok_wechat_bridge.bridge import IngestStatus
from grok_wechat_bridge.modality import Kind, VoiceDeliveryForm
from grok_wechat_bridge.report import acceptance_record
from grok_wechat_bridge.store import SendState

from support import CONV, BridgeTestCase, sha


def text(src, t="hi"):
    return InboundMessage(conversation_id=CONV, source_msg_id=src, kind=Kind.TEXT, sent_at=0.0,
                          text=t)


class SendStateTests(BridgeTestCase):
    def _only_key(self, eid):
        rows = self.store.outbound_for_event(eid)
        self.assertEqual(len(rows), 1)
        return rows[0]["idempotency_key"]

    def test_timeout_after_delivery_resolved_by_receipt_not_resend(self):
        self.wechat.send_faults = ["timeout_after_delivery"]
        r = self.bridge.ingest(text("1"))
        self.assertIs(r.status, IngestStatus.SEND_PENDING)
        key = self._only_key(r.event_id)
        self.assertEqual(self.store.get_outbound(key)["state"], SendState.UNKNOWN.value)
        self.restart()
        self.bridge.resume()
        row = self.store.get_outbound(key)
        self.assertEqual(row["state"], SendState.INTERFACE_ACCEPTED.value)
        self.assertEqual(self.wechat.deliveries_for(key), 1)
        self.assertEqual(self.wechat.send_calls, 1)
        self.assertEqual(self.store.get_event(r.event_id)["state"], "done")
        self.assertEqual(len(self.bot.runs), 1)

    def test_timeout_before_delivery_retries_after_not_found(self):
        self.wechat.send_faults = ["timeout_before_delivery"]
        r = self.bridge.ingest(text("1"))
        key = self._only_key(r.event_id)
        self.restart()
        self.bridge.resume()
        row = self.store.get_outbound(key)
        self.assertEqual(row["state"], SendState.INTERFACE_ACCEPTED.value)
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(self.wechat.deliveries_for(key), 1)

    def test_indeterminate_receipt_never_blind_retries(self):
        self.wechat.send_faults = ["timeout_before_delivery"]
        self.wechat.receipt_indeterminate = True
        r = self.bridge.ingest(text("1"))
        key = self._only_key(r.event_id)
        for _ in range(3):
            self.bridge.resume()
        self.assertEqual(self.wechat.send_calls, 1)
        self.assertEqual(self.store.get_outbound(key)["state"], SendState.UNKNOWN.value)

    def test_crash_mid_send_is_receipt_checked(self):
        r = self.bridge.ingest(text("1"))
        key = self._only_key(r.event_id)
        # Simulate a crash after the durable SENDING mark of a fresh item.
        self.store.conn.execute("UPDATE outbound SET state='sending' WHERE idempotency_key=?",
                                (key,))
        self.bridge.resume()
        self.assertEqual(self.wechat.deliveries_for(key), 1)
        self.assertEqual(self.store.get_outbound(key)["state"], "interface_accepted")

    def test_attempt_limit(self):
        self.wechat.send_faults = ["timeout_before_delivery"] * 5
        r = self.bridge.ingest(text("1"))
        key = self._only_key(r.event_id)
        for _ in range(5):
            self.bridge.resume()
        self.assertEqual(self.store.get_outbound(key)["state"], SendState.FAILED.value)
        self.assertEqual(self.wechat.send_calls, 3)

    def test_definitive_rejection(self):
        self.wechat.send_faults = ["reject"]
        r = self.bridge.ingest(text("1"))
        key = self._only_key(r.event_id)
        self.bridge.resume()
        self.assertEqual(self.store.get_outbound(key)["state"], SendState.REJECTED.value)
        self.assertEqual(self.wechat.send_calls, 1)

    def test_interface_delivery_phone_are_separate(self):
        r = self.bridge.ingest(text("1"))
        key = self._only_key(r.event_id)
        rec = acceptance_record(self.store, r.event_id)["outbound"][0]
        self.assertEqual((rec["interface_accepted"], rec["delivery_receipt"],
                          rec["phone_confirmed"]), (True, False, False))
        self.bridge.record_delivery_receipt(key)
        rec = acceptance_record(self.store, r.event_id)
        self.assertEqual((rec["outbound"][0]["delivery_receipt"],
                          rec["outbound"][0]["phone_confirmed"]), (True, False))
        self.assertFalse(rec["phone_acceptance_complete"])
        self.bridge.record_phone_confirmation(key, "owner confirmed on phone (synthetic test)")
        self.assertTrue(acceptance_record(self.store, r.event_id)["phone_acceptance_complete"])

    def test_receipts_ignored_for_unaccepted_items(self):
        self.wechat.send_faults = ["timeout_before_delivery"]
        self.wechat.receipt_indeterminate = True
        r = self.bridge.ingest(text("1"))
        key = self._only_key(r.event_id)
        self.bridge.record_delivery_receipt(key)
        self.bridge.record_phone_confirmation(key, "x")
        row = self.store.get_outbound(key)
        self.assertIsNone(row["delivery_receipt_at"])
        self.assertIsNone(row["phone_confirmed_at"])


class MediaOutboundTests(BridgeTestCase):
    def _reply(self, *parts):
        self.bot.extra_replies = list(parts)
        r = self.bridge.ingest(text("1"))
        return r, acceptance_record(self.store, r.event_id)

    def test_image_and_video_out_hashed(self):
        img, vid = fixtures.png(seed=31), fixtures.mp4(seed=32)
        r, rec = self._reply(ReplyPart(Kind.IMAGE, data=img), ReplyPart(Kind.VIDEO, data=vid))
        self.assertIs(r.status, IngestStatus.REPLIED)
        dirs = {o["direction"]: o for o in rec["outbound"]}
        self.assertEqual(dirs["image.grokbot_to_wechat"]["media_sha256"], sha(img))
        self.assertEqual(dirs["video.grokbot_to_wechat"]["media_sha256"], sha(vid))
        sent = {p.kind: p for p in self.wechat.delivered}
        self.assertEqual(sent[Kind.IMAGE].data, img)
        self.assertEqual(sent[Kind.VIDEO].mime, "video/mp4")

    def test_bad_outbound_media_blocked(self):
        r, rec = self._reply(ReplyPart(Kind.IMAGE, data=b"nope"),
                             ReplyPart(Kind.VIDEO, data=fixtures.mp4()[:-3]))
        self.assertEqual(self.gap_codes(r.event_id), ["outbound_format", "outbound_format"])
        self.assertFalse(any(p.kind is not Kind.TEXT for p in self.wechat.delivered))

    def test_native_voice_recorded(self):
        r, rec = self._reply(ReplyPart(Kind.VOICE, data=fixtures.wav()))
        v = [o for o in rec["outbound"] if o["direction"] == "voice.grokbot_to_wechat"][0]
        self.assertEqual(v["voice_form"], VoiceDeliveryForm.NATIVE_VOICE.value)
        self.assertTrue(v["interface_accepted"])
        self.assertFalse(v["phone_confirmed"])

    def test_native_voice_unavailable_is_not_silently_downgraded(self):
        self.wechat.caps = WeChatCapabilities(native_voice_send=False, image_send=True,
                                              video_send=True, receipt_lookup=True)
        r, rec = self._reply(ReplyPart(Kind.VOICE, data=fixtures.wav()))
        self.assertIn("voice_native_unavailable", self.gap_codes(r.event_id))
        self.assertFalse(any(p.kind is Kind.VOICE for p in self.wechat.delivered))

    def test_attachment_downgrade_requires_confirmation(self):
        r, _ = self._reply(ReplyPart(Kind.VOICE, data=fixtures.wav(),
                                     voice_form=VoiceDeliveryForm.AUDIO_ATTACHMENT))
        self.assertIn("voice_downgrade_unconfirmed", self.gap_codes(r.event_id))
        self.assertFalse(any(p.kind is Kind.VOICE for p in self.wechat.delivered))

    def test_confirmed_attachment_downgrade_recorded_separately(self):
        r, rec = self._reply(ReplyPart(Kind.VOICE, data=fixtures.wav(),
                                       voice_form=VoiceDeliveryForm.AUDIO_ATTACHMENT,
                                       downgrade_confirmed=True))
        v = [o for o in rec["outbound"] if o["direction"] == "voice.grokbot_to_wechat"][0]
        self.assertEqual(v["voice_form"], "audio_attachment")
        self.assertTrue(v["downgrade_confirmed"])
        sent = [p for p in self.wechat.delivered if p.kind is Kind.VOICE][0]
        self.assertIs(sent.voice_form, VoiceDeliveryForm.AUDIO_ATTACHMENT)

    def test_channel_without_video_send(self):
        self.wechat.caps = WeChatCapabilities(native_voice_send=True, image_send=True,
                                              video_send=False, receipt_lookup=True)
        r, _ = self._reply(ReplyPart(Kind.VIDEO, data=fixtures.mp4()))
        self.assertIn("outbound_unsupported", self.gap_codes(r.event_id))


if __name__ == "__main__":
    unittest.main()
