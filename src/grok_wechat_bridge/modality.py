"""Modalities, the eight bridge directions, capability and evidence vocabularies."""

from __future__ import annotations

from enum import Enum


class Kind(str, Enum):
    TEXT = "text"
    VOICE = "voice"
    IMAGE = "image"
    VIDEO = "video"


class Flow(str, Enum):
    INBOUND = "wechat_to_grokbot"
    OUTBOUND = "grokbot_to_wechat"


class Direction(str, Enum):
    TEXT_IN = "text.wechat_to_grokbot"
    TEXT_OUT = "text.grokbot_to_wechat"
    VOICE_IN = "voice.wechat_to_grokbot"
    VOICE_OUT = "voice.grokbot_to_wechat"
    IMAGE_IN = "image.wechat_to_grokbot"
    IMAGE_OUT = "image.grokbot_to_wechat"
    VIDEO_IN = "video.wechat_to_grokbot"
    VIDEO_OUT = "video.grokbot_to_wechat"

    @property
    def kind(self) -> Kind:
        return Kind(self.value.split(".", 1)[0])

    @property
    def flow(self) -> Flow:
        return Flow(self.value.split(".", 1)[1])


def direction_for(kind: Kind, flow: Flow) -> Direction:
    return Direction(f"{kind.value}.{flow.value}")


class Capability(str, Enum):
    """Abilities the primary GrokBot adapter must declare before media is routed to it."""

    ASR = "asr"
    AUDIO_UNDERSTANDING = "audio_understanding"
    IMAGE_VISION = "image_vision"
    VIDEO_VISUAL = "video_visual"
    VIDEO_AUDIO = "video_audio"
    TTS = "tts"


class EvidenceKind(str, Enum):
    TEXT_RECEIVED = "text.received"
    VOICE_AUDIO_RETRIEVED = "voice.audio_retrieved"
    VOICE_TRANSCRIPT = "voice.transcript"
    VOICE_AUDIO_UNDERSTOOD = "voice.audio_understood"
    IMAGE_ORIGINAL_BYTES = "image.original_bytes"
    IMAGE_VIEW_DETAIL = "image.view_detail"
    VIDEO_COMPLETE_MP4 = "video.complete_mp4"
    VIDEO_VISUAL = "video.visual"
    VIDEO_AUDIO = "video.audio"
    VIDEO_AUDIO_TRACK_ABSENT = "video.audio_track_absent"


class Provenance(str, Enum):
    BRIDGE_COMPUTED = "bridge_computed"
    WECHAT_PLATFORM = "wechat_platform"
    SYNTHETIC = "synthetic"
    LIVE_PRIMARY_GROKBOT = "live_primary_grokbot"


class VoiceDeliveryForm(str, Enum):
    NATIVE_VOICE = "native_voice"
    AUDIO_ATTACHMENT = "audio_attachment"


class Route(str, Enum):
    PRIMARY = "primary_grokbot"
    PRIMARY_WITH_EXPLICIT_DELEGATION = "primary_grokbot_explicit_delegation"
