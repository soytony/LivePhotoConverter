"""Image decoding must follow contents when backup filenames are misleading."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_photo_converter as converter


class InspectImageTest(unittest.TestCase):
    def test_supported_contents_are_accepted_independently_of_image_suffix(self):
        with tempfile.TemporaryDirectory() as work:
            for name, kind in (('image.JPG', 'HEIC'), ('image.HEIC', 'JPEG'),
                               ('image.jpg', 'JPEG'), ('image.heif', 'HEIC')):
                with self.subTest(name=name, kind=kind):
                    image = Path(work) / name
                    image.write_bytes(b'fixture')
                    with patch.object(converter, 'run', return_value=json.dumps([{'FileType': kind}]).encode()):
                        self.assertEqual(converter.inspect(image, converter.IMAGE_EXTS)['FileType'], kind)

    def test_unsupported_contents_are_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            image = Path(work) / 'image.jpg'
            image.write_bytes(b'fixture')
            with patch.object(converter, 'run', return_value=b'[{"FileType":"PNG"}]'):
                with self.assertRaises(converter.ConversionError):
                    converter.inspect(image, converter.IMAGE_EXTS)


if __name__ == '__main__':
    unittest.main()
