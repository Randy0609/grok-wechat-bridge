"""Structural checks on media bytes. These verify container integrity only; they do not
perceive content."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .modality import Kind


class MediaFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Mp4Info:
    top_level_boxes: tuple[str, ...]
    has_video_track: bool
    has_audio_track: bool


@dataclass(frozen=True)
class FormatInfo:
    kind: Kind
    mime: str
    detail: dict = field(default_factory=dict)


_CONTAINERS = {b"moov", b"trak", b"mdia"}


def _iter_boxes(buf: bytes, start: int, end: int):
    off = start
    while off < end:
        if end - off < 8:
            raise MediaFormatError(f"truncated box header at offset {off}")
        size, btype = struct.unpack(">I4s", buf[off : off + 8])
        header = 8
        if size == 1:
            if end - off < 16:
                raise MediaFormatError("truncated largesize header")
            size = struct.unpack(">Q", buf[off + 8 : off + 16])[0]
            header = 16
        elif size == 0:
            size = end - off
        if size < header or off + size > end:
            raise MediaFormatError(f"box {btype!r} at {off} overruns container")
        yield btype, off + header, off + size
        off += size


def _handlers(buf: bytes, start: int, end: int, out: list[bytes]) -> None:
    for btype, body, stop in _iter_boxes(buf, start, end):
        if btype in _CONTAINERS:
            _handlers(buf, body, stop, out)
        elif btype == b"hdlr":
            if stop - body < 12:
                raise MediaFormatError("truncated hdlr box")
            out.append(buf[body + 8 : body + 12])


def inspect_mp4(data: bytes) -> Mp4Info:
    """Require a complete ISO-BMFF file: boxes tile the buffer exactly, ftyp first,
    moov and mdat present."""
    boxes = [(t, b, s) for t, b, s in _iter_boxes(data, 0, len(data))]
    names = tuple(t.decode("latin-1") for t, _, _ in boxes)
    if not names or names[0] != "ftyp":
        raise MediaFormatError("MP4 must start with ftyp box")
    if "moov" not in names:
        raise MediaFormatError("MP4 missing moov box (incomplete or fragmented upload)")
    if "mdat" not in names:
        raise MediaFormatError("MP4 missing mdat box")
    handlers: list[bytes] = []
    for t, b, s in boxes:
        if t == b"moov":
            _handlers(data, b, s, handlers)
    return Mp4Info(names, b"vide" in handlers, b"soun" in handlers)


def sniff_image(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
            raise MediaFormatError("JPEG missing end-of-image marker (truncated)")
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    raise MediaFormatError("unrecognised image container")


def sniff_voice(data: bytes) -> str:
    if data.startswith(b"#!SILK_V3") or data.startswith(b"\x02#!SILK_V3"):
        return "audio/silk"
    if data.startswith(b"#!AMR\n"):
        return "audio/amr"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    raise MediaFormatError("unrecognised voice/audio container")


def inspect(kind: Kind, data: bytes) -> FormatInfo:
    if not data:
        raise MediaFormatError("empty media payload")
    if kind is Kind.IMAGE:
        return FormatInfo(kind, sniff_image(data))
    if kind is Kind.VOICE:
        return FormatInfo(kind, sniff_voice(data))
    if kind is Kind.VIDEO:
        info = inspect_mp4(data)
        if not info.has_video_track:
            raise MediaFormatError("MP4 has no video track")
        return FormatInfo(
            kind,
            "video/mp4",
            {
                "boxes": list(info.top_level_boxes),
                "has_video_track": info.has_video_track,
                "has_audio_track": info.has_audio_track,
            },
        )
    raise MediaFormatError(f"kind {kind} carries no media")
