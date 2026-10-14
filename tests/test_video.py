"""Codec selection and timestamp regression checks."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_photo_converter as converter


class VideoEncodingTest(unittest.TestCase):
    def test_worker_count_must_be_positive(self):
        import argparse
        self.assertEqual(converter.positive_workers('4'), 4)
        for value in ('0', '-2', '1.5', 'abc'):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                converter.positive_workers(value)

    def prepare(self, source_codec, requested):
        info = {'streams': [{'index': 0, 'codec_type': 'video',
                             'codec_name': source_codec, 'duration': '3'}]}
        with patch.object(converter, 'run', side_effect=[json.dumps(info).encode(), b'']) as run, \
                patch.object(converter, 'apple_still_timestamp', return_value=1366667):
            result = converter.prepare_video(Path('source.mov'), Path('output.mp4'), requested)
        return result, run.call_args.args

    def test_inherited_and_matching_codecs_are_copied(self):
        for codec, label in (('h264', 'AVC'), ('hevc', 'HEVC'), ('av1', 'AV1')):
            for requested in (None, label):
                with self.subTest(codec=codec, requested=requested):
                    result, args = self.prepare(codec, requested)
                    self.assertFalse(result['video_transcoded'])
                    self.assertEqual(result['video_codec'], codec)
                    self.assertEqual(args[args.index('-c:v') + 1], 'copy')

    def test_requested_codec_transcodes_and_retains_timestamp(self):
        for label, codec, encoder in (('AVC', 'h264', 'libx264'),
                                      ('HEVC', 'hevc', 'libx265'),
                                      ('AV1', 'av1', 'libsvtav1')):
            with self.subTest(requested=label):
                result, args = self.prepare('mpeg4', label)
                self.assertTrue(result['video_transcoded'])
                self.assertEqual(result['video_codec'], codec)
                self.assertEqual(args[args.index('-c:v') + 1], encoder)
                self.assertEqual('-tag:v' in args, codec == 'hevc')
                self.assertEqual(result['presentation_timestamp_us'], 1366667)


if __name__ == '__main__':
    unittest.main()
