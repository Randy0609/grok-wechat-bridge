"""Media transfer boundary.

Bytes are stored content-addressed under a runtime directory outside the repository.
Agents retrieve them only through short-lived references that are HMAC-bound to one
media item, one event and one conversation. Every fetch re-verifies size and SHA-256
and is logged as retrieval evidence."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import formats
from .modality import Flow, Kind
from .store import Store


class MediaRefError(PermissionError):
    """Base class: the reference cannot be honoured. Never substitute other media."""


class MediaRefExpired(MediaRefError):
    pass


class MediaRefMismatch(MediaRefError):
    pass


class MediaMissing(MediaRefError):
    pass


class MediaIntegrityError(MediaRefError):
    pass


@dataclass(frozen=True)
class MediaHandle:
    """What the primary agent receives: an opaque ref plus declared integrity facts."""

    ref: str
    media_id: str
    kind: Kind
    mime: str
    size: int
    sha256: str
    source_event_id: str
    format_detail: dict


@dataclass(frozen=True)
class StoredMedia:
    media_id: str
    kind: Kind
    mime: str
    size: int
    sha256: str
    format_detail: dict


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class MediaGateway:
    def __init__(self, store: Store, root: str | os.PathLike, clock: Callable[[], float],
                 ttl_seconds: float = 300.0, key: Optional[bytes] = None):
        self.store = store
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        self.ttl = ttl_seconds
        # Per-process key: refs never outlive the bridge process and no key is persisted.
        self._key = key or secrets.token_bytes(32)
        self._revoked: set[str] = set()

    # ingest -----------------------------------------------------------------
    def put(self, *, event_id: str, conversation_id: str, flow: Flow, kind: Kind, data: bytes,
            declared_size: Optional[int] = None,
            declared_sha256: Optional[str] = None) -> StoredMedia:
        size = len(data)
        digest = hashlib.sha256(data).hexdigest()
        if declared_size is not None and declared_size != size:
            raise MediaIntegrityError(f"declared size {declared_size} != received {size}")
        if declared_sha256 is not None and declared_sha256.lower() != digest:
            raise MediaIntegrityError("declared sha256 does not match received bytes")
        info = formats.inspect(kind, data)
        media_id = "med_" + hashlib.sha256(
            f"{event_id}|{flow.value}|{digest}".encode()).hexdigest()[:32]
        existing = self.store.get_media(media_id)
        if existing is None:
            path = self.root / digest[:2] / digest
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not path.exists():
                tmp = path.with_suffix(".part")
                with open(tmp, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
                os.chmod(path, 0o600)
            self.store.add_media(media_id=media_id, event_id=event_id,
                                 conversation_id=conversation_id, flow=flow.value,
                                 kind=kind.value, mime=info.mime, size=size, sha256=digest,
                                 path=str(path.relative_to(self.root)),
                                 format_detail=info.detail, created_at=self.clock())
        return StoredMedia(media_id, kind, info.mime, size, digest, info.detail)

    # references ---------------------------------------------------------------
    def issue(self, media_id: str, *, for_event_id: str, conversation_id: str) -> MediaHandle:
        row = self.store.get_media(media_id)
        if row is None:
            raise MediaMissing(f"no media {media_id}")
        if row["conversation_id"] != conversation_id:
            raise MediaRefMismatch("media belongs to a different conversation")
        if not self.store.is_associated(for_event_id, row["event_id"]):
            raise MediaRefMismatch("media is not the event's own media nor associated with it")
        payload = {
            "m": media_id,
            "e": for_event_id,
            "c": conversation_id,
            "x": self.clock() + self.ttl,
            "n": secrets.token_hex(8),
        }
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        sig = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        return MediaHandle(
            ref=f"mref1.{body}.{sig}", media_id=media_id, kind=Kind(row["kind"]),
            mime=row["mime"], size=row["size"], sha256=row["sha256"],
            source_event_id=row["event_id"], format_detail=json.loads(row["format_detail"]))

    def revoke(self, ref: str) -> None:
        self._revoked.add(ref)

    def _verify(self, ref: str) -> dict:
        try:
            ver, body, sig = ref.split(".")
        except ValueError as e:
            raise MediaRefMismatch("malformed media ref") from e
        if ver != "mref1":
            raise MediaRefMismatch("unknown media ref version")
        want = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(want, sig):
            raise MediaRefMismatch("media ref signature invalid (forged or from another process)")
        if ref in self._revoked:
            raise MediaRefExpired("media ref revoked")
        payload = json.loads(_unb64(body))
        if self.clock() >= payload["x"]:
            raise MediaRefExpired("media ref expired")
        return payload

    def fetch(self, ref: str, *, event_id: str, conversation_id: str, agent_id: str) -> bytes:
        payload = self._verify(ref)
        if payload["e"] != event_id or payload["c"] != conversation_id:
            raise MediaRefMismatch("media ref is bound to a different event/conversation")
        row = self.store.get_media(payload["m"])
        if row is None:
            raise MediaMissing("media record missing")
        path = self.root / row["path"]
        try:
            data = path.read_bytes()
        except FileNotFoundError as e:
            raise MediaMissing("media bytes missing from store") from e
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != row["size"] or digest != row["sha256"]:
            raise MediaIntegrityError("stored media failed size/sha256 verification")
        self.store.log_fetch(media_id=row["media_id"], event_id=event_id,
                             conversation_id=conversation_id, agent_id=agent_id,
                             size=len(data), sha256=digest, fetched_at=self.clock())
        return data

    def read_internal(self, media_id: str) -> bytes:
        """Bridge-internal read for outbound sending; verifies integrity, not logged as
        agent retrieval."""
        row = self.store.get_media(media_id)
        if row is None:
            raise MediaMissing(media_id)
        data = (self.root / row["path"]).read_bytes()
        if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise MediaIntegrityError("stored media failed size/sha256 verification")
        return data
