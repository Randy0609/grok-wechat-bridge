from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from grok_wechat_bridge.bridge import Bridge, BridgeConfig
from grok_wechat_bridge.media import MediaGateway
from grok_wechat_bridge.store import Store
from grok_wechat_bridge.synthetic import SyntheticPrimaryGrokBot, SyntheticWeChat

CONV = "syn-conv-0001"
OTHER_CONV = "syn-conv-0002"
# Larger than 2**53 so any float round-trip would corrupt it.
BIG_ID = "9007199254740993123"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class BridgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gwb-test-"))
        self.clock = Clock()
        self.wechat = SyntheticWeChat()
        self.bot = SyntheticPrimaryGrokBot()
        self.config = BridgeConfig(bound_conversation_id=CONV)
        self._open()

    def _open(self, ttl: float = 300.0) -> None:
        self.store = Store(str(self.tmp / "bridge.sqlite"))
        self.gateway = MediaGateway(self.store, self.tmp / "blobs", self.clock, ttl_seconds=ttl)
        self.bridge = Bridge(store=self.store, gateway=self.gateway, wechat=self.wechat,
                             grokbot=self.bot, config=self.config, clock=self.clock)

    def restart(self) -> None:
        """Simulate a process restart: new connection, new gateway key, same files."""
        self.store.close()
        self._open()

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def gap_codes(self, event_id: str) -> list[str]:
        return [g["code"] for g in self.store.gaps_for(event_id)]

    def evidence_kinds(self, event_id: str) -> list[tuple[str, str]]:
        return [(e["kind"], e["provenance"]) for e in self.store.evidence_for(event_id)]

    def sent_texts(self) -> list[str]:
        return [p.text for p in self.wechat.delivered if p.text is not None]
