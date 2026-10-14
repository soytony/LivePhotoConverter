"""Regression checks for Android streaming XMP reader compatibility."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_photo_converter as converter


class MotionXmpTest(unittest.TestCase):
    def packet(self, descriptions):
        source = ('<x:xmpmeta xmlns:x="adobe:ns:meta/" '
                  'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
                  'xmlns:apple="http://ns.apple.com/HDRGainMap/1.0/" '
                  'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                  '<rdf:RDF>' + descriptions + '</rdf:RDF></x:xmpmeta>').encode()
        with patch.object(converter, 'run', return_value=source):
            return converter.xmp_for(
                Path('source.jpg'), 4096,
                gain_size=512, timestamp=1366667, compatibility='oneplus')

    def build(self, descriptions):
        return ET.fromstring(self.packet(descriptions))

    def test_motion_flag_shares_description_with_original_metadata(self):
        # The original implementation appended a second Description. Android's
        # parser returned null on this first, Apple-only description.
        root = self.build('<rdf:Description rdf:about="">'
                          '<apple:HDRGainMapVersion>131072</apple:HDRGainMapVersion>'
                          '</rdf:Description>')
        descriptions = root.findall('.//{' + converter.NS['rdf'] + '}Description')
        self.assertEqual(len(descriptions), 1)
        desc = descriptions[0]
        self.assertEqual(desc.get('{' + converter.NS['GCamera'] + '}MotionPhoto'), '1')
        self.assertEqual(desc.find('{http://ns.apple.com/HDRGainMap/1.0/}HDRGainMapVersion').text, '131072')
        self.assertEqual(desc.get('{' + converter.NS['OpCamera'] + '}VideoLength'), '4096')
        items = root.findall('.//{' + converter.NS['Container'] + '}Item')
        self.assertEqual([i.get('{' + converter.NS['Item'] + '}Semantic') for i in items],
                         ['Primary', 'GainMap', 'MotionPhoto'])
        self.assertEqual([i.get('{' + converter.NS['Item'] + '}Length') for i in items],
                         ['0', '512', '4096'])

    def test_same_subject_descriptions_preserve_unrelated_metadata(self):
        root = self.build('<rdf:Description rdf:about="" dc:format="image/jpeg" />'
                          '<rdf:Description rdf:about=""><dc:title>Original title</dc:title>'
                          '</rdf:Description>')
        descriptions = root.findall('.//{' + converter.NS['rdf'] + '}Description')
        self.assertEqual(len(descriptions), 1)
        self.assertEqual(descriptions[0].get('{http://purl.org/dc/elements/1.1/}format'), 'image/jpeg')
        self.assertEqual(descriptions[0].find('{http://purl.org/dc/elements/1.1/}title').text, 'Original title')

    def test_packet_meets_adobe_namespace_and_modern_only_constraints(self):
        raw = self.packet('<rdf:Description rdf:about="" />')
        converter.validate_motion_xmp(raw, 4096, 512)
        self.assertIn(b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"', raw)
        self.assertNotIn(b'MicroVideo', raw)
        # This packet is valid XML, but Adobe's parser rejects the inherited
        # namespace layout that ElementTree produces by default.
        inherited = raw.replace(b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"', b'<rdf:RDF')
        ET.fromstring(inherited)
        with self.assertRaises(converter.ConversionError):
            converter.validate_motion_xmp(inherited, 4096, 512)

    def test_gain_map_packet_also_declares_rdf_namespace(self):
        metadata = {key: [1.0] * 3 for key in ('min_boost', 'max_boost', 'gamma', 'offset_sdr', 'offset_hdr')}
        metadata.update(capacity_min=1.0, capacity_max=4.0)
        raw = converter.gainmap_xmp(metadata)
        self.assertIn(b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"', raw)
        self.assertEqual(len(ET.fromstring(raw).findall('.//{' + converter.NS['rdf'] + '}Description')), 1)

    def test_default_output_names_use_jpeg_for_heic_inputs(self):
        self.assertEqual(converter.output_for(Path('photo.HEIC')), Path('photo.jpg'))
        self.assertEqual(converter.output_for(Path('photo.heif')), Path('photo.jpg'))
        self.assertEqual(converter.output_for(Path('photo.JPEG')), Path('photo.JPEG'))

    def test_different_subjects_are_not_silently_conflated(self):
        with self.assertRaises(converter.ConversionError):
            self.build('<rdf:Description rdf:about="one" /><rdf:Description rdf:about="two" />')


if __name__ == '__main__':
    unittest.main()
