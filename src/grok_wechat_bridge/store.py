"""Durable state: event dedupe, media records, fetch log, associations, evidence,
explicit gaps and outbound send state. SQLite, one file, WAL journal."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator, Optional

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    source_msg_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT,
    quote_source_msg_id TEXT,
    platform_transcript TEXT,
    sent_at REAL,
    received_at REAL NOT NULL,
    state TEXT NOT NULL,
    route TEXT,
    agent_id TEXT,
    run_id TEXT,
    updated_at REAL NOT NULL,
    UNIQUE (conversation_id, source_msg_id)
);
CREATE TABLE IF NOT EXISTS media (
    media_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    conversation_id TEXT NOT NULL,
    flow TEXT NOT NULL,
    kind TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    format_detail TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS media_fetches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS associations (
    question_event_id TEXT NOT NULL,
    media_event_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (question_event_id, media_event_id)
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    provenance TEXT NOT NULL,
    media_sha256 TEXT,
    detail TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    code TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbound (
    idempotency_key TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    direction TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT,
    media_id TEXT,
    voice_form TEXT,
    downgrade_confirmed INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    platform_msg_id TEXT,
    last_error TEXT,
    interface_accepted_at REAL,
    delivery_receipt_at REAL,
    phone_confirmed_at REAL,
    phone_confirmation_note TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_conv_state ON events(conversation_id, state);
CREATE INDEX IF NOT EXISTS idx_outbound_event ON outbound(event_id);
"""


class EventState(str, Enum):
    RECEIVED = "received"
    PENDING_ASSOCIATION = "pending_association"
    ASSOCIATED = "associated"
    RUNNING = "running"
    REPLY_PLANNED = "reply_planned"
    DONE = "done"
    FAILED_EXPLICIT = "failed_explicit"


class SendState(str, Enum):
    PLANNED = "planned"
    SENDING = "sending"
    UNKNOWN = "unknown"
    INTERFACE_ACCEPTED = "interface_accepted"
    REJECTED = "rejected"
    FAILED = "failed"


TERMINAL_SEND_STATES = {SendState.INTERFACE_ACCEPTED, SendState.REJECTED, SendState.FAILED}


@dataclass
class ClaimResult:
    created: bool
    row: sqlite3.Row


class Store:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self.conn.close()

    def _tx(self):
        return _Tx(self.conn)

    # events -----------------------------------------------------------------
    def claim_event(self, **f: Any) -> ClaimResult:
        """Insert the event if new. Uniqueness on (conversation_id, source_msg_id) makes
        replays of the same WeChat message a no-op across restarts."""
        with self._tx():
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO events(event_id, conversation_id, source_msg_id,
                direction, kind, text, quote_source_msg_id, platform_transcript, sent_at,
                received_at, state, updated_at)
                VALUES (:event_id, :conversation_id, :source_msg_id, :direction, :kind, :text,
                :quote_source_msg_id, :platform_transcript, :sent_at, :received_at, :state,
                :received_at)""",
                f,
            )
            row = self.get_event(f["event_id"])
            return ClaimResult(cur.rowcount == 1, row)

    def get_event(self, event_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()

    def find_event(self, conversation_id: str, source_msg_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE conversation_id=? AND source_msg_id=?",
            (conversation_id, source_msg_id),
        ).fetchone()

    def set_event_state(self, event_id: str, state: EventState, now: float, **extra: Any) -> None:
        cols = ", ".join(f"{k}=:{k}" for k in extra)
        sql = "UPDATE events SET state=:state, updated_at=:now" + (", " + cols if cols else "")
        self.conn.execute(sql + " WHERE event_id=:event_id",
                          {"state": state.value, "now": now, "event_id": event_id, **extra})

    def events_in_state(self, conversation_id: str, state: EventState) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE conversation_id=? AND state=? ORDER BY received_at",
            (conversation_id, state.value),
        ).fetchall()

    def events_unfinished(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE state IN (?, ?, ?) ORDER BY received_at",
            (EventState.RECEIVED.value, EventState.RUNNING.value, EventState.REPLY_PLANNED.value),
        ).fetchall()

    # media ------------------------------------------------------------------
    def add_media(self, **f: Any) -> None:
        f = dict(f, format_detail=json.dumps(f.get("format_detail", {}), sort_keys=True))
        self.conn.execute(
            """INSERT INTO media(media_id, event_id, conversation_id, flow, kind, mime, size,
            sha256, path, format_detail, created_at) VALUES (:media_id, :event_id,
            :conversation_id, :flow, :kind, :mime, :size, :sha256, :path, :format_detail,
            :created_at)""",
            f,
        )

    def get_media(self, media_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM media WHERE media_id=?", (media_id,)).fetchone()

    def media_for_event(self, event_id: str, flow: str = "wechat_to_grokbot") -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM media WHERE event_id=? AND flow=? ORDER BY created_at",
            (event_id, flow),
        ).fetchall()

    def log_fetch(self, **f: Any) -> None:
        self.conn.execute(
            """INSERT INTO media_fetches(media_id, event_id, conversation_id, agent_id, size,
            sha256, fetched_at) VALUES (:media_id, :event_id, :conversation_id, :agent_id,
            :size, :sha256, :fetched_at)""",
            f,
        )

    def fetches_for(self, event_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM media_fetches WHERE event_id=? ORDER BY id", (event_id,)
        ).fetchall()

    # associations -------------------------------------------------------------
    def add_association(self, question_event_id: str, media_event_id: str, reason: str,
                        now: float) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO associations VALUES (?, ?, ?, ?)",
            (question_event_id, media_event_id, reason, now),
        )

    def associations_for(self, question_event_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM associations WHERE question_event_id=?", (question_event_id,)
        ).fetchall()

    def is_associated(self, question_event_id: str, media_event_id: str) -> bool:
        if question_event_id == media_event_id:
            return True
        return self.conn.execute(
            "SELECT 1 FROM associations WHERE question_event_id=? AND media_event_id=?",
            (question_event_id, media_event_id),
        ).fetchone() is not None

    # evidence / gaps --------------------------------------------------------
    def add_evidence(self, event_id: str, kind: str, provenance: str, media_sha256: Optional[str],
                     detail: dict, now: float) -> None:
        self.conn.execute(
            """INSERT INTO evidence(event_id, kind, provenance, media_sha256, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, kind, provenance, media_sha256, json.dumps(detail, sort_keys=True,
                                                                  ensure_ascii=False), now),
        )

    def evidence_for(self, event_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM evidence WHERE event_id=? ORDER BY id", (event_id,)
        ).fetchall()

    def add_gap(self, event_id: str, code: str, detail: str, now: float) -> None:
        self.conn.execute(
            "INSERT INTO gaps(event_id, code, detail, created_at) VALUES (?, ?, ?, ?)",
            (event_id, code, detail, now),
        )

    def gaps_for(self, event_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM gaps WHERE event_id=? ORDER BY id", (event_id,)
        ).fetchall()

    # outbound ---------------------------------------------------------------
    def plan_outbound(self, **f: Any) -> bool:
        f.setdefault("text", None)
        f.setdefault("media_id", None)
        f.setdefault("voice_form", None)
        f.setdefault("downgrade_confirmed", 0)
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO outbound(idempotency_key, event_id, conversation_id, slot,
            direction, kind, text, media_id, voice_form, downgrade_confirmed, state,
            created_at, updated_at) VALUES (:idempotency_key, :event_id, :conversation_id,
            :slot, :direction, :kind, :text, :media_id, :voice_form, :downgrade_confirmed,
            'planned', :now, :now)""",
            f,
        )
        return cur.rowcount == 1

    def get_outbound(self, key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM outbound WHERE idempotency_key=?", (key,)
        ).fetchone()

    def outbound_for_event(self, event_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM outbound WHERE event_id=? ORDER BY created_at, slot", (event_id,)
        ).fetchall()

    def mark_sending(self, key: str, now: float) -> None:
        self.conn.execute(
            """UPDATE outbound SET state='sending', attempts=attempts+1, updated_at=?
            WHERE idempotency_key=?""",
            (now, key),
        )

    def set_send_state(self, key: str, state: SendState, now: float, *,
                       platform_msg_id: Optional[str] = None, error: Optional[str] = None) -> None:
        accepted = now if state is SendState.INTERFACE_ACCEPTED else None
        self.conn.execute(
            """UPDATE outbound SET state=?, updated_at=?,
            platform_msg_id=COALESCE(?, platform_msg_id),
            last_error=COALESCE(?, last_error),
            interface_accepted_at=COALESCE(interface_accepted_at, ?)
            WHERE idempotency_key=?""",
            (state.value, now, platform_msg_id, error, accepted, key),
        )

    def record_delivery_receipt(self, key: str, now: float) -> None:
        self.conn.execute(
            """UPDATE outbound SET delivery_receipt_at=COALESCE(delivery_receipt_at, ?),
            updated_at=? WHERE idempotency_key=? AND state='interface_accepted'""",
            (now, now, key),
        )

    def record_phone_confirmation(self, key: str, note: str, now: float) -> None:
        self.conn.execute(
            """UPDATE outbound SET phone_confirmed_at=?, phone_confirmation_note=?, updated_at=?
            WHERE idempotency_key=? AND state='interface_accepted'""",
            (now, note, now, key),
        )

    def outbound_needing_attention(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute(
            "SELECT * FROM outbound WHERE state IN ('planned','sending','unknown') ORDER BY created_at"
        ).fetchall()


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False
