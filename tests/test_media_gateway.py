import os
import unittest

from grok_wechat_bridge import fixtures
from grok_wechat_bridge.media import (
    MediaGateway,
    MediaIntegrityError,
    MediaMissing,
    MediaRefExpired,
    MediaRefMismatch,
)
from grok_wechat_bridge.modality import Flow, Kind

from support import CONV, OTHER_CONV, BridgeTestCase, sha


class GatewayTests(BridgeTestCase):
    def _put(self, eid="evt_a", conv=CONV, data=None):
        self.store.claim_event(event_id=eid, conversation_id=conv, source_msg_id=eid + "-src",
                               direction="image.wechat_to_grokbot", kind="image", text=None,
                               quote_source_msg_id=None, platform_transcript=None, sent_at=0,
                               received_at=self.clock(), state="received")
        return self.gateway.put(event_id=eid, conversation_id=conv, flow=Flow.INBOUND,
                                kind=Kind.IMAGE, data=data or fixtures.png())

    def test_put_records_size_and_sha(self):
        data = fixtures.png(seed=3)
        m = self._put(data=data)
        self.assertEqual((m.size, m.sha256), (len(data), sha(data)))
        stored = (self.tmp / "blobs" / self.store.get_media(m.media_id)["path"]).read_bytes()
        self.assertEqual(stored, data)

    def test_declared_mismatch_rejected(self):
        with self.assertRaises(MediaIntegrityError):
            self.gateway.put(event_id="e", conversation_id=CONV, flow=Flow.INBOUND,
                             kind=Kind.IMAGE, data=fixtures.png(), declared_size=1)
        with self.assertRaises(MediaIntegrityError):
            self.gateway.put(event_id="e", conversation_id=CONV, flow=Flow.INBOUND,
                             kind=Kind.IMAGE, data=fixtures.png(), declared_sha256="0" * 64)

    def test_fetch_ok_and_logged(self):
        m = self._put()
        h = self.gateway.issue(m.media_id, for_event_id="evt_a", conversation_id=CONV)
        data = self.gateway.fetch(h.ref, event_id="evt_a", conversation_id=CONV, agent_id="agent")
        self.assertEqual(sha(data), m.sha256)
        f = self.store.fetches_for("evt_a")
        self.assertEqual([(r["sha256"], r["agent_id"]) for r in f], [(m.sha256, "agent")])

    def test_expired_ref(self):
        m = self._put()
        h = self.gateway.issue(m.media_id, for_event_id="evt_a", conversation_id=CONV)
        self.clock.advance(301)
        with self.assertRaises(MediaRefExpired):
            self.gateway.fetch(h.ref, event_id="evt_a", conversation_id=CONV, agent_id="a")

    def test_revoked_ref(self):
        m = self._put()
        h = self.gateway.issue(m.media_id, for_event_id="evt_a", conversation_id=CONV)
        self.gateway.revoke(h.ref)
        with self.assertRaises(MediaRefExpired):
            self.gateway.fetch(h.ref, event_id="evt_a", conversation_id=CONV, agent_id="a")

    def test_ref_bound_to_event_and_conversation(self):
        m = self._put()
        h = self.gateway.issue(m.media_id, for_event_id="evt_a", conversation_id=CONV)
        with self.assertRaises(MediaRefMismatch):
            self.gateway.fetch(h.ref, event_id="evt_b", conversation_id=CONV, agent_id="a")
        with self.assertRaises(MediaRefMismatch):
            self.gateway.fetch(h.ref, event_id="evt_a", conversation_id=OTHER_CONV, agent_id="a")

    def test_issue_refuses_unassociated_or_foreign(self):
        m = self._put()
        with self.assertRaises(MediaRefMismatch):
            self.gateway.issue(m.media_id, for_event_id="evt_other", conversation_id=CONV)
        with self.assertRaises(MediaRefMismatch):
            self.gateway.issue(m.media_id, for_event_id="evt_a", conversation_id=OTHER_CONV)
        with self.assertRaises(MediaMissing):
            self.gateway.issue("med_nope", for_event_id="evt_a", conversation_id=CONV)

    def test_forged_and_foreign_process_refs(self):
        m = self._put()
        h = self.gateway.issue(m.media_id, for_event_id="evt_a", conversation_id=CONV)
        ver, body, sig = h.ref.split(".")
        with self.assertRaises(MediaRefMismatch):
            self.gateway.fetch(f"{ver}.{body}.{sig[:-2]}AA", event_id="evt_a",
                               conversation_id=CONV, agent_id="a")
        other = MediaGateway(self.store, self.tmp / "blobs", self.clock)
        with self.assertRaises(MediaRefMismatch):
            other.fetch(h.ref, event_id="evt_a", conversation_id=CONV, agent_id="a")

    def test_tampered_or_missing_bytes(self):
        m = self._put()
        path = self.tmp / "blobs" / self.store.get_media(m.media_id)["path"]
        h = self.gateway.issue(m.media_id, for_event_id="evt_a", conversation_id=CONV)
        os.chmod(path, 0o600)
        path.write_bytes(path.read_bytes() + b"x")
        with self.assertRaises(MediaIntegrityError):
            self.gateway.fetch(h.ref, event_id="evt_a", conversation_id=CONV, agent_id="a")
        path.unlink()
        with self.assertRaises(MediaMissing):
            self.gateway.fetch(h.ref, event_id="evt_a", conversation_id=CONV, agent_id="a")


if __name__ == "__main__":
    unittest.main()
