import unittest

from grok_wechat_bridge import fixtures, formats
from grok_wechat_bridge.ids import (
    IdPrecisionError,
    event_id_for,
    idempotency_key_for,
    loads_envelope,
    require_str_id,
)
from grok_wechat_bridge.modality import Direction, Flow, Kind, direction_for

from support import BIG_ID


class IdTests(unittest.TestCase):
    def test_non_string_ids_rejected(self):
        for bad in (int(BIG_ID), float(BIG_ID), True, None):
            with self.assertRaises(IdPrecisionError):
                require_str_id(bad)

    def test_envelope_keeps_exact_digits(self):
        env = loads_envelope('{"msg_id": %s, "size": 12}' % BIG_ID)
        self.assertEqual(env["msg_id"], BIG_ID)
        self.assertNotEqual(str(int(float(BIG_ID))), BIG_ID)

    def test_envelope_rejects_float_tokens(self):
        with self.assertRaises(IdPrecisionError):
            loads_envelope('{"msg_id": 1.5e18}')

    def test_ids_deterministic_and_distinct(self):
        a = event_id_for("c", BIG_ID)
        self.assertEqual(a, event_id_for("c", BIG_ID))
        self.assertNotEqual(a, event_id_for("c", BIG_ID[:-1] + "4"))
        self.assertNotEqual(a, event_id_for("c" + BIG_ID[0], BIG_ID[1:]))
        self.assertNotEqual(idempotency_key_for(a, "reply.0"), idempotency_key_for(a, "reply.1"))


class DirectionTests(unittest.TestCase):
    def test_eight_distinct_directions(self):
        self.assertEqual(len(set(Direction)), 8)
        pairs = {(d.kind, d.flow) for d in Direction}
        self.assertEqual(pairs, {(k, f) for k in Kind for f in Flow})
        for d in Direction:
            self.assertIs(direction_for(d.kind, d.flow), d)


class FormatTests(unittest.TestCase):
    def test_complete_mp4_with_tracks(self):
        info = formats.inspect_mp4(fixtures.mp4())
        self.assertTrue(info.has_video_track and info.has_audio_track)
        self.assertEqual(info.top_level_boxes, ("ftyp", "moov", "mdat"))

    def test_mp4_without_audio_track(self):
        info = formats.inspect_mp4(fixtures.mp4(audio=False))
        self.assertTrue(info.has_video_track)
        self.assertFalse(info.has_audio_track)

    def test_truncated_mp4_rejected(self):
        data = fixtures.mp4()
        for cut in (1, 10, len(data) // 2):
            with self.assertRaises(formats.MediaFormatError):
                formats.inspect_mp4(data[:-cut])

    def test_mp4_missing_moov_rejected(self):
        data = fixtures.mp4()
        ftyp_len = int.from_bytes(data[:4], "big")
        moov_len = int.from_bytes(data[ftyp_len:ftyp_len + 4], "big")
        with self.assertRaises(formats.MediaFormatError):
            formats.inspect_mp4(data[:ftyp_len] + data[ftyp_len + moov_len:])

    def test_video_kind_requires_video_track(self):
        with self.assertRaises(formats.MediaFormatError):
            formats.inspect(Kind.VIDEO, fixtures.mp4(video=False))

    def test_images(self):
        self.assertEqual(formats.sniff_image(fixtures.png()), "image/png")
        with self.assertRaises(formats.MediaFormatError):
            formats.sniff_image(b"\xff\xd8\xff\xe0" + b"\x00" * 20)
        with self.assertRaises(formats.MediaFormatError):
            formats.sniff_image(b"not an image")

    def test_voice(self):
        self.assertEqual(formats.sniff_voice(fixtures.silk_like()), "audio/silk")
        self.assertEqual(formats.sniff_voice(fixtures.wav()), "audio/wav")
        with self.assertRaises(formats.MediaFormatError):
            formats.sniff_voice(b"xxxxxxxxxxxx")

    def test_empty_rejected(self):
        with self.assertRaises(formats.MediaFormatError):
            formats.inspect(Kind.IMAGE, b"")


if __name__ == "__main__":
    unittest.main()
