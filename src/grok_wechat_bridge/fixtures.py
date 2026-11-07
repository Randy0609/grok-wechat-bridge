"""Synthetic media generated in code. No real media is stored in the repository.

The voice payloads only carry a valid container header; they are not decodable speech."""

from __future__ import annotations

import io
import struct
import wave
import zlib


def png(width: int = 2, height: int = 2, seed: int = 0) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    rows = []
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            row += bytes(((x * 40 + seed) % 256, (y * 40 + seed) % 256, seed % 256))
        rows.append(bytes(row))
    raw = b"".join(rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _box(tag: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + tag + body


def _trak(handler: bytes) -> bytes:
    hdlr = _box(b"hdlr", b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + handler + b"\x00" * 13)
    return _box(b"trak", _box(b"mdia", hdlr))


def mp4(video: bool = True, audio: bool = True, seed: int = 0, payload_len: int = 64) -> bytes:
    """Structurally complete ISO-BMFF skeleton (ftyp, moov with handler boxes, mdat).
    Not decodable video; used to exercise completeness and track detection."""
    ftyp = _box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2mp41")
    traks = (_trak(b"vide") if video else b"") + (_trak(b"soun") if audio else b"")
    moov = _box(b"moov", _box(b"mvhd", b"\x00" * 100) + traks)
    mdat = _box(b"mdat", bytes((seed + i) % 256 for i in range(payload_len)))
    return ftyp + moov + mdat


def wav(seconds: float = 0.1, seed: int = 0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        n = int(8000 * seconds)
        w.writeframes(b"".join(struct.pack("<h", ((i * 37 + seed) % 2000) - 1000)
                               for i in range(n)))
    return buf.getvalue()


def silk_like(seed: int = 0, length: int = 48) -> bytes:
    """SILK container magic followed by filler; NOT decodable audio."""
    return b"\x02#!SILK_V3" + bytes((seed * 7 + i) % 256 for i in range(length))
