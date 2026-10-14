#!/usr/bin/env python3
"""Convert Live Photo image/video pairs to JPEG Motion Photos.

HEIC/HEIF inputs are converted to JPEG automatically, preserving HDR through
an Ultra HDR gain map. Existing JPEG inputs retain their image encoding.
Requires Python 3.9+, ExifTool, FFmpeg and FFprobe. HDR conversion also needs
macOS 15+, Swift and libultrahdr: brew install exiftool ffmpeg libultrahdr.
No third-party Python packages are needed for the macOS HDR workflow.

Examples:
  python3 live_photo_converter.py l2m -i IMG.heic -v IMG.mov -o IMG_MP.jpg
  python3 live_photo_converter.py l2m -i a.jpg b.heic -v a.mov b.mp4 -o a_MP.jpg b_MP.jpg
  python3 live_photo_converter.py dl2m -i ./photos -o ./motion

Output is always JPEG. Without --output, HEIC/HEIF inputs use the same stem
with .jpg; JPEG inputs retain their filename and need --overwrite for in-place
conversion.
The default oneplus profile adds Oplus recognition markers; --compatibility
google writes the generic modern format. See README.md for usage and limits.
"""

import argparse
import atexit
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
import ctypes as C
import ctypes.util
from datetime import datetime
import json
import math
import multiprocessing
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from xml.dom import minidom
from xml.parsers.expat import ExpatError
import xml.etree.ElementTree as ET

HEIC_EXTS = {'.heic', '.heif'}
IMAGE_EXTS = {'.jpg', '.jpeg'} | HEIC_EXTS
VIDEO_EXTS = {'.mov', '.mp4'}
NS = {
    'x': 'adobe:ns:meta/',
    'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
    'GCamera': 'http://ns.google.com/photos/1.0/camera/',
    'Container': 'http://ns.google.com/photos/1.0/container/',
    'Item': 'http://ns.google.com/photos/1.0/container/item/',
    'hdrgm': 'http://ns.adobe.com/hdr-gain-map/1.0/',
    'OpCamera': 'http://ns.oplus.com/photos/1.0/camera/',
}
for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)


class ConversionError(Exception):
    pass


# Core Image reconstructs Apple's HDR before generating an ISO gain map. The
# Python layer also adds Adobe gain-map metadata for Android 14 compatibility.
SWIFT_HDR = r'''
import Foundation
import ImageIO
import CoreImage
func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8)); exit(1)
}
guard #available(macOS 15.0, *) else { fail("HDR conversion requires macOS 15+") }
let source = URL(fileURLWithPath: CommandLine.arguments[1])
let output = URL(fileURLWithPath: CommandLine.arguments[2])
let allowSDR = CommandLine.arguments[3] == "sdr"
guard let sdr = CIImage(contentsOf: source, options: [.expandToHDR: false, .applyOrientationProperty: true]),
      let hdr = CIImage(contentsOf: source, options: [.expandToHDR: true, .applyOrientationProperty: true]) else {
    fail("Cannot decode source HEIC using Core Image")
}
let headroom = hdr.contentHeadroom
let hasHDR = headroom > 1.001
let context = CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.extendedLinearDisplayP3)!])
var options: [CIImageRepresentationOption: Any] = [
    CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.95
]
if hasHDR && !allowSDR { options[.hdrImage] = hdr; options[.hdrGainMapAsRGB] = true }
do {
    try context.writeJPEGRepresentation(of: sdr, to: output,
        colorSpace: CGColorSpace(name: CGColorSpace.displayP3)!, options: options)
} catch { fail("HDR JPEG conversion failed: \(error)") }
let result: [String: Any] = ["hdr_detected": hasHDR, "source_headroom": headroom,
                           "hdr_preserved": hasHDR && !allowSDR]
let json = try! JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
print(String(data: json, encoding: .utf8)!)
'''
_native_directory = None


def native_hdr_helper():
    global _native_directory
    if _native_directory is None:
        if sys.platform != 'darwin' or not shutil.which('swiftc'):
            raise ConversionError('HDR HEIC conversion requires macOS 15+ and Swift; --allow-sdr permits SDR fallback')
        directory = tempfile.TemporaryDirectory(prefix='live-photo-hdr-')
        try:
            source = Path(directory.name) / 'hdr.swift'
            source.write_text(SWIFT_HDR, encoding='utf-8')
            run('swiftc', '-O', source, '-o', Path(directory.name) / 'hdr')
        except Exception:
            directory.cleanup()
            raise
        _native_directory = directory
        atexit.register(directory.cleanup)
    return Path(_native_directory.name) / 'hdr'


class UhdrError(C.Structure):
    _fields_ = [('code', C.c_int), ('has_detail', C.c_int), ('detail', C.c_char * 256)]


class UhdrCompressed(C.Structure):
    _fields_ = [('data', C.c_void_p), ('size', C.c_size_t), ('capacity', C.c_size_t),
                ('gamut', C.c_int), ('transfer', C.c_int), ('range', C.c_int)]


class UhdrMetadata(C.Structure):
    _fields_ = [(key, C.c_float * 3) for key in
                ('max_boost', 'min_boost', 'gamma', 'offset_sdr', 'offset_hdr')] + [
                ('capacity_min', C.c_float), ('capacity_max', C.c_float), ('use_base_cg', C.c_int)]


def probe_ultrahdr(path, decode=False):
    """Use Google's reference codec, not HDR tag presence, to validate the JPEG."""
    library = ctypes.util.find_library('uhdr')
    candidates = [library, '/opt/homebrew/lib/libuhdr.dylib', '/usr/local/lib/libuhdr.dylib']
    lib = None
    for candidate in candidates:
        if candidate:
            try:
                lib = C.CDLL(candidate)
                break
            except OSError:
                pass
    if lib is None:
        raise ConversionError('HDR validation requires libultrahdr: brew install libultrahdr')
    def bind(name, arguments, result):
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = arguments, result
        return fn
    def check(error):
        if error.code:
            raise ConversionError('Ultra HDR validation failed: ' + error.detail.decode(errors='replace'))
    create = bind('uhdr_create_decoder', [], C.c_void_p)
    release = bind('uhdr_release_decoder', [C.c_void_p], None)
    set_image = bind('uhdr_dec_set_image', [C.c_void_p, C.POINTER(UhdrCompressed)], UhdrError)
    probe = bind('uhdr_dec_probe', [C.c_void_p], UhdrError)
    get_metadata = bind('uhdr_dec_get_gainmap_metadata', [C.c_void_p], C.POINTER(UhdrMetadata))
    data = path.read_bytes()
    buffer = C.create_string_buffer(data)
    image = UhdrCompressed(C.cast(buffer, C.c_void_p), len(data), len(data), -1, -1, -1)
    decoder = create()
    if not decoder:
        raise ConversionError('Could not allocate Ultra HDR decoder')
    try:
        check(set_image(decoder, C.byref(image)))
        if decode:
            check(bind('uhdr_dec_set_out_img_format', [C.c_void_p, C.c_int], UhdrError)(decoder, 4))
            check(bind('uhdr_dec_set_out_color_transfer', [C.c_void_p, C.c_int], UhdrError)(decoder, 0))
        check(probe(decoder))
        pointer = get_metadata(decoder)
        if not pointer:
            raise ConversionError('JPEG has no decodable HDR gain-map metadata')
        meta = pointer.contents
        result = {key: list(getattr(meta, key)) for key in
                  ('max_boost', 'min_boost', 'gamma', 'offset_sdr', 'offset_hdr')}
        result.update(capacity_min=meta.capacity_min, capacity_max=meta.capacity_max,
                      use_base_cg=bool(meta.use_base_cg))
        if decode:
            # Linear half-float output confirms the gain map can actually be applied.
            check(bind('uhdr_decode', [C.c_void_p], UhdrError)(decoder))
        return result
    finally:
        release(decoder)


def serialize_xmp(root):
    # ElementTree hoists namespace declarations to x:xmpmeta. Adobe's XMP
    # parser (also used by Oplus) rejects an rdf:RDF node without attributes,
    # even when the inherited namespace makes it valid XML. Keep this explicit
    # declaration on rdf:RDF, matching native-camera XMP packets.
    rdf = root.find(f'.//{{{NS["rdf"]}}}RDF')
    if rdf is None:
        raise ConversionError('XMP packet is missing rdf:RDF')
    rdf.set('xmlns:rdf', NS['rdf'])
    return ET.tostring(root, encoding='utf-8', xml_declaration=False)


def validate_motion_xmp(raw, video_size, gain_size):
    """Check the packet actually written, including Adobe reader constraints."""
    try:
        document = minidom.parseString(raw)
        rdf_nodes = document.getElementsByTagNameNS(NS['rdf'], 'RDF')
        if len(rdf_nodes) != 1 or rdf_nodes[0].getAttribute('xmlns:rdf') != NS['rdf']:
            raise ConversionError('Motion XMP lacks the explicit rdf:RDF namespace required by Adobe readers')
        root = ET.fromstring(raw)
        descriptions = root.findall(f'.//{{{NS["rdf"]}}}Description')
        if len(descriptions) != 1 or descriptions[0].get(f'{{{NS["GCamera"]}}}MotionPhoto') != '1':
            raise ConversionError('Motion XMP must have one description containing the motion flag')
        for node in root.iter():
            if any(key.startswith(f'{{{NS["GCamera"]}}}MicroVideo') for key in node.attrib) or \
                    node.tag.startswith(f'{{{NS["GCamera"]}}}MicroVideo'):
                raise ConversionError('Legacy MicroVideo metadata conflicts with modern motion readers')
        items = root.findall(f'.//{{{NS["Container"]}}}Item')
        expected = [('Primary', 'image/jpeg', 0)]
        if gain_size:
            expected.append(('GainMap', 'image/jpeg', gain_size))
        expected.append(('MotionPhoto', 'video/mp4', video_size))
        actual = [(item.get(f'{{{NS["Item"]}}}Semantic'), item.get(f'{{{NS["Item"]}}}Mime'),
                   int(item.get(f'{{{NS["Item"]}}}Length', '0'))) for item in items]
        if actual != expected:
            raise ConversionError('Motion XMP directory does not match the packaged media')
    except (ET.ParseError, ExpatError, ValueError) as exc:
        raise ConversionError(f'Invalid written Motion Photo XMP: {exc}') from exc


def gainmap_xmp(metadata):
    root = ET.Element(f'{{{NS["x"]}}}xmpmeta')
    rdf = ET.SubElement(root, f'{{{NS["rdf"]}}}RDF')
    desc = ET.SubElement(rdf, f'{{{NS["rdf"]}}}Description', {f'{{{NS["rdf"]}}}about': ''})
    values = {'Version': '1.0', 'BaseRenditionIsHDR': 'False',
              'HDRCapacityMin': str(math.log2(metadata['capacity_min'])),
              'HDRCapacityMax': str(math.log2(metadata['capacity_max']))}
    for xml_name, field, transform in [
        ('GainMapMin', 'min_boost', math.log2), ('GainMapMax', 'max_boost', math.log2),
        ('Gamma', 'gamma', lambda x: x), ('OffsetSDR', 'offset_sdr', lambda x: x),
        ('OffsetHDR', 'offset_hdr', lambda x: x),
    ]:
        values[xml_name] = [str(transform(value)) for value in metadata[field]]
    for key, value in values.items():
        if isinstance(value, list):
            node = ET.SubElement(desc, f'{{{NS["hdrgm"]}}}{key}')
            seq = ET.SubElement(node, f'{{{NS["rdf"]}}}Seq')
            for channel in value:
                ET.SubElement(seq, f'{{{NS["rdf"]}}}li').text = channel
        else:
            desc.set(f'{{{NS["hdrgm"]}}}{key}', value)
    return serialize_xmp(root)


def mpf_layout(data):
    """Return the two-image MPF entry positions and absolute gain-map position."""
    if data[:2] != b'\xff\xd8':
        raise ConversionError('Invalid JPEG signature')
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 255:
            break
        marker = data[pos + 1]
        if marker in (0xda, 0xd9):
            break
        size = int.from_bytes(data[pos + 2:pos + 4], 'big')
        end = pos + 2 + size
        if size < 2 or end > len(data):
            raise ConversionError('Invalid JPEG marker length')
        if marker == 0xe2 and data[pos + 4:pos + 8] == b'MPF\0':
            tiff = pos + 8
            order = '<' if data[tiff:tiff + 2] == b'II' else '>'
            def u32(offset):
                return struct.unpack_from(order + 'I', data, offset)[0]
            ifd = tiff + u32(tiff + 4)
            count = struct.unpack_from(order + 'H', data, ifd)[0]
            for index in range(count):
                entry = ifd + 2 + 12 * index
                tag, _, length = struct.unpack_from(order + 'HHI', data, entry)
                if tag == 0xb002:
                    if length != 32:
                        raise ConversionError('Only a primary JPEG plus one HDR gain map is supported')
                    records = tiff + u32(entry + 8)
                    if records + 32 > end:
                        raise ConversionError('Invalid MPF image records')
                    gain_start, gain_size = tiff + u32(records + 24), u32(records + 20)
                    if data[gain_start:gain_start + 2] != b'\xff\xd8' or gain_start + gain_size != len(data):
                        raise ConversionError('HDR gain map is not the final JPEG resource')
                    return order, records, gain_start, gain_size
        pos = end
    raise ConversionError('JPEG has no supported HDR gain-map MPF index')


def prepare_gainmap(still, work):
    """Add Adobe XMP alongside ISO metadata without recompressing either JPEG."""
    metadata = probe_ultrahdr(still)
    if not metadata['use_base_cg']:
        raise ConversionError('Alternate-color-space gain maps need an ISO-only conversion backend')
    data = still.read_bytes()
    _, _, start, size = mpf_layout(data)
    gain = work / 'gainmap.jpg'
    gain.write_bytes(data[start:start + size])
    packet = work / 'gainmap.xmp'
    packet.write_bytes(gainmap_xmp(metadata))
    run('exiftool', '-overwrite_original', f'-XMP<={packet}', gain)
    if not run('exiftool', '-b', '-XMP', gain).strip():
        raise ConversionError('ExifTool did not write gain-map metadata')
    return gain.read_bytes(), metadata


def replace_gainmap(still, gain):
    data = bytearray(still.read_bytes())
    order, records, start, _ = mpf_layout(data)
    struct.pack_into(order + 'I', data, records + 4, start)
    struct.pack_into(order + 'I', data, records + 20, len(gain))
    still.write_bytes(data[:start] + gain)


def add_single_image_mpf(still):
    """Oplus also expects an MPF index for SDR JPEG motion photos."""
    data = still.read_bytes()
    if data[:2] != b'\xff\xd8':
        raise ConversionError('Invalid JPEG for OnePlus profile')
    # Standard MPF TIFF IFD: version, image count, and one 16-byte MP entry.
    entries = (struct.pack('>HHI4s', 0xb000, 7, 4, b'0100') +
               struct.pack('>HHII', 0xb001, 4, 1, 1) +
               struct.pack('>HHII', 0xb002, 7, 16, 50))
    header = b'MPF\0MM\0*\0\0\0\x08' + struct.pack('>H', 3) + entries + b'\0' * 4
    segment_size = 4 + len(header) + 16
    record = struct.pack('>IIIHH', 0x030000, len(data) + segment_size, 0, 0, 0)
    segment = b'\xff\xe2' + struct.pack('>H', len(header) + len(record) + 2) + header + record
    still.write_bytes(data[:2] + segment + data[2:])


def apple_still_timestamp(source, streams):
    """Locate Apple's timed still-frame track, using FFprobe's edit-list timing."""
    def boxes(data):
        offset = 0
        while offset + 8 <= len(data):
            size, kind = struct.unpack_from('>I4s', data, offset)
            header = 8
            if size == 1:
                if offset + 16 > len(data):
                    return
                size = struct.unpack_from('>Q', data, offset + 8)[0]
                header = 16
            elif size == 0:
                size = len(data) - offset
            if size < header or offset + size > len(data):
                return
            yield kind, data[offset + header:offset + size]
            offset += size
    with source.open('rb') as file:
        length = source.stat().st_size
        while file.tell() + 8 <= length:
            offset = file.tell()
            size, kind = struct.unpack('>I4s', file.read(8))
            header = 8
            if size == 1:
                raw = file.read(8)
                if len(raw) != 8:
                    return None
                size, header = struct.unpack('>Q', raw)[0], 16
            elif size == 0:
                size = length - offset
            if size < header or offset + size > length:
                return None
            if kind == b'moov' and size <= 64 * 1024 * 1024:
                moov = file.read(size - header)
                for tag, track in boxes(moov):
                    if tag != b'trak' or b'com.apple.quicktime.still-image-time' not in track:
                        continue
                    for child, payload in boxes(track):
                        if child == b'tkhd' and len(payload) >= 24:
                            track_id = struct.unpack_from('>I', payload, 20 if payload[0] == 1 else 12)[0]
                            for stream in streams:
                                if int(stream.get('id', '0'), 0) == track_id and stream.get('start_time'):
                                    return round(float(stream['start_time']) * 1000000)
                return None
            file.seek(offset + size)
    return None


def prepare_video(source, target, video_encoding=None):
    info = json.loads(run('ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', source))
    streams = info.get('streams', [])
    videos = [s for s in streams if s.get('codec_type') == 'video' and
              not s.get('disposition', {}).get('attached_pic') and
              'Auxiliary' not in s.get('tags', {}).get('handler_name', '')]
    if not videos:
        raise ConversionError('Input video has no primary video track')
    primary = videos[0]
    codecs = {'AVC': 'h264', 'HEVC': 'hevc', 'AV1': 'av1'}
    output_codec = codecs[video_encoding] if video_encoding else primary.get('codec_name')
    if output_codec not in codecs.values():
        raise ConversionError('Motion Photo video must use H.264, HEVC, or AV1')
    transcoded = output_codec != primary.get('codec_name')
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    arguments = ['ffmpeg', '-v', 'error', '-nostdin', '-i', source, '-map', f'0:{primary["index"]}']
    if transcoded:
        encoders = {'h264': ('libx264', '20', 'medium'),
                    'hevc': ('libx265', '20', 'medium'),
                    'av1': ('libsvtav1', '28', '8')}
        encoder, crf, preset = encoders[output_codec]
        arguments += ['-c:v', encoder, '-crf', crf, '-preset', preset]
    else:
        arguments += ['-c:v', 'copy']
    if audios:
        audio = audios[0]
        arguments += ['-map', f'0:{audio["index"]}', '-c:a', 'copy' if audio.get('codec_name') == 'aac' else 'aac']
    if output_codec == 'hevc':
        arguments += ['-tag:v', 'hvc1']
    arguments += ['-map_metadata', '-1', '-movflags', '+faststart', '-f', 'mp4', target]
    run(*arguments)
    duration = float(primary.get('duration') or info.get('format', {}).get('duration') or 0)
    timestamp = round(duration * 500000) if duration > 0 else -1
    # ExifTool's SampleTime can omit MOV edit-list offsets; use track timing.
    candidate = apple_still_timestamp(source, streams)
    timestamp_source = 'midpoint'
    if candidate is not None and 0 <= candidate <= duration * 1000000:
        timestamp = candidate
        timestamp_source = 'apple-still-image-time'
    return {'video_container': 'mp4', 'video_codec': output_codec,
            'source_video_codec': primary.get('codec_name'), 'video_transcoded': transcoded,
            'audio_transcoded': bool(audios and audios[0].get('codec_name') != 'aac'),
            'presentation_timestamp_us': timestamp, 'timestamp_source': timestamp_source}


def run(*args):
    result = subprocess.run([str(a) for a in args], capture_output=True)
    if result.returncode:
        detail = result.stderr.decode(errors='replace').strip()
        raise ConversionError(detail or result.stdout.decode(errors='replace').strip()
                              or f'{args[0]} exited with {result.returncode}')
    return result.stdout


def same_file(a, b):
    if a.resolve() == b.resolve():
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def inspect(path, expected):
    if not path.is_file():
        raise ConversionError(f'Not a file: {path}')
    if path.suffix.lower() not in expected:
        raise ConversionError(f'Unsupported extension: {path}')
    info = json.loads(run('exiftool', '-j', '-FileType', '-MIMEType', path))[0]
    allowed = {'JPEG', 'HEIC'} if expected == IMAGE_EXTS else {'MOV', 'MP4'}
    if info.get('FileType') not in allowed:
        raise ConversionError(f'Unsupported or invalid media: {path} ({info.get("FileType", "unknown")})')
    # Supported images may have misleading names after an export or backup.
    # Decode by the detected contents rather than trusting the suffix.
    return info


def xmp_for(image, video_size, gain_size=0, timestamp=-1, compatibility='oneplus'):
    raw = run('exiftool', '-b', '-XMP', image)
    if raw.strip():
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ConversionError(f'Cannot preserve malformed source XMP: {exc}') from exc
    else:
        root = ET.Element(f'{{{NS["x"]}}}xmpmeta')
    rdf = root.find(f'.//{{{NS["rdf"]}}}RDF')
    if rdf is None:
        rdf = ET.SubElement(root, f'{{{NS["rdf"]}}}RDF')
    # Preserve unrelated XMP, replacing only motion-photo properties.
    for node in root.iter():
        for key in list(node.attrib):
            if key.startswith('{' + NS['GCamera'] + '}'):
                name = key.split('}', 1)[1]
                if name in {'MotionPhoto', 'MicroVideo'} and node.attrib[key] == '1':
                    raise ConversionError('Input is already marked as a Motion Photo; use the original still image')
                if name.startswith(('MotionPhoto', 'MicroVideo')):
                    del node.attrib[key]
        for child in list(node):
            if child.tag.startswith('{' + NS['GCamera'] + '}'):
                name = child.tag.split('}', 1)[1]
                if name in {'MotionPhoto', 'MicroVideo'} and child.text == '1':
                    raise ConversionError('Input is already marked as a Motion Photo; use the original still image')
                if name.startswith(('MotionPhoto', 'MicroVideo')):
                    node.remove(child)
            elif child.tag == f'{{{NS["Container"]}}}Directory':
                node.remove(child)
    # Android's streaming Motion Photo parser rejects ANY rdf:Description that
    # lacks the motion flag, even if a later description supplies it. Combine
    # same-subject XMP into one description instead of appending a second one.
    description_tag = f'{{{NS["rdf"]}}}Description'
    about_key = f'{{{NS["rdf"]}}}about'
    descriptions = [node for node in rdf if node.tag == description_tag]
    if descriptions:
        desc = descriptions[0]
        subject = desc.get(about_key, '')
        for other in descriptions[1:]:
            if other.get(about_key, '') != subject:
                raise ConversionError('Multiple XMP subjects cannot be combined for Android Motion Photo readers')
            for key, value in other.attrib.items():
                if key in desc.attrib and desc.attrib[key] != value:
                    raise ConversionError('Conflicting source XMP properties cannot be combined safely')
                desc.set(key, value)
            for child in list(other):
                desc.append(child)
            rdf.remove(other)
        desc.set(about_key, subject)
    else:
        desc = ET.SubElement(rdf, description_tag, {about_key: ''})
    if any(node.tag == description_tag and node is not desc for node in root.iter()):
        raise ConversionError('Nested XMP descriptions are unsupported by Android Motion Photo readers')
    camera = {'MotionPhoto': '1', 'MotionPhotoVersion': '1',
              'MotionPhotoPresentationTimestampUs': str(timestamp)}
    # Do not mix legacy MicroVideo V1 with Motion Photo V1. The Oplus reader
    # returns null as soon as MicroVideoVersion is present, even when the modern
    # directory and motion flag are valid. Modern Android uses the directory.
    for key, value in camera.items():
        desc.set(f'{{{NS["GCamera"]}}}{key}', value)
    if gain_size:
        desc.set(f'{{{NS["hdrgm"]}}}Version', '1.0')
    if compatibility == 'oneplus':
        for key, value in {'MotionPhotoPrimaryPresentationTimestampUs': str(timestamp),
                           'MotionPhotoOwner': 'oplus', 'OLivePhotoVersion': '2',
                           'VideoLength': str(video_size), 'MotionPhotoFeatureFlag': '1'}.items():
            desc.set(f'{{{NS["OpCamera"]}}}{key}', value)
    directory = ET.SubElement(desc, f'{{{NS["Container"]}}}Directory')
    seq = ET.SubElement(directory, f'{{{NS["rdf"]}}}Seq')
    items = [('image/jpeg', 'Primary', 0, 0)]
    if gain_size:
        items.append(('image/jpeg', 'GainMap', gain_size, None))
    items.append(('video/mp4', 'MotionPhoto', video_size, None))
    for mime, semantic, length, padding in items:
        li = ET.SubElement(seq, f'{{{NS["rdf"]}}}li', {f'{{{NS["rdf"]}}}parseType': 'Resource'})
        item = ET.SubElement(li, f'{{{NS["Container"]}}}Item')
        values = {'Mime': mime, 'Semantic': semantic, 'Length': str(length)}
        if padding is not None:
            values['Padding'] = str(padding)
        for key, value in values.items():
            item.set(f'{{{NS["Item"]}}}{key}', value)
    return serialize_xmp(root)


def to_jpeg(source, target, allow_sdr=False):
    result = {'hdr_detected': False, 'hdr_preserved': False}
    if sys.platform == 'darwin' and shutil.which('swiftc'):
        result = json.loads(run(native_hdr_helper(), source, target, 'sdr' if allow_sdr else 'hdr'))
    elif not allow_sdr:
        raise ConversionError('HEIC to JPEG requires the macOS HDR backend; use --allow-sdr for explicit SDR conversion')
    elif shutil.which('sips') and sys.platform == 'darwin':
        run('sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '100', source, '--out', target)
    elif shutil.which('magick'):
        run('magick', source, '-quality', '100', target)
    else:
        try:
            import pillow_heif
            from PIL import Image
        except ImportError as exc:
            raise ConversionError('HEIC to JPEG requires sips (macOS), ImageMagick, or pip install Pillow pillow-heif') from exc
        pillow_heif.register_heif_opener()
        with Image.open(source) as image:
            image.convert('RGB').save(target, 'JPEG', quality=100, icc_profile=image.info.get('icc_profile'))
    # Retain source metadata, but leave the decoder's pixel orientation intact.
    run('exiftool', '-overwrite_original', '-TagsFromFile', source,
        '-EXIF:all', '-XMP:all', '-IPTC:all', '--Orientation', '--ExifImageWidth',
        '--ExifImageHeight', '-Orientation#=1', target)
    return result


def convert(image, video, output, overwrite, protected, allow_sdr=False, compatibility='oneplus',
            video_encoding=None):
    image_info = inspect(image, IMAGE_EXTS)
    inspect(video, VIDEO_EXTS)
    if output.is_symlink():
        raise ConversionError(f'Refusing a symlink destination: {output}')
    for source in protected:
        if same_file(output, source) and not same_file(source, image):
            raise ConversionError(f'Output would overwrite another input: {source}')
    if output.exists() and not overwrite:
        raise ConversionError(f'Output exists; use --overwrite: {output}')
    if output.suffix.lower() not in {'.jpg', '.jpeg'}:
        raise ConversionError('Motion Photo output must have a .jpg or .jpeg extension')
    if compatibility not in {'google', 'oneplus'}:
        raise ConversionError('Compatibility must be google or oneplus')
    # Reject existing motion media before any expensive decoding.
    xmp_for(image, 0)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.live-photo-', dir=output.parent) as work:
        work = Path(work)
        still = work / 'still.jpg'
        converted_video = work / 'video.mp4'
        result = prepare_video(video, converted_video, video_encoding)
        result.update(hdr_detected=False, hdr_preserved=False, hdr_status='not_detected',
                      compatibility=compatibility, source_image_format=image_info['FileType'],
                      image_extension_mismatch=(image.suffix.lower() in HEIC_EXTS) !=
                      (image_info['FileType'] == 'HEIC'))
        if image_info['FileType'] == 'HEIC':
            result.update(to_jpeg(image, still, allow_sdr))
        else:
            shutil.copy2(image, still)
        gain = None
        # An MPF secondary image must be understood before describing the
        # container; treating a gain map as padding breaks HDR.
        has_mpf = b'MPF\0' in still.read_bytes()[:1024 * 1024]
        if result['hdr_preserved'] or has_mpf:
            gain, metadata = prepare_gainmap(still, work)
            result.update(hdr_detected=True, hdr_preserved=True,
                          hdr_headroom=metadata['capacity_max'])
        if compatibility == 'oneplus':
            # This flag is a gallery recognition marker, not the camera maker.
            # Keep original Make/Model and all gain-map resources intact.
            run('exiftool', '-overwrite_original', '-EXIF:UserComment=oplus_8388608', still)
            result['oplus_motion_flag'] = 8388608
        video_size = converted_video.stat().st_size
        xmp = xmp_for(still, video_size, len(gain) if gain else 0,
                      result['presentation_timestamp_us'], compatibility)
        packet = work / 'motion.xmp'
        packet.write_bytes(xmp)
        run('exiftool', '-overwrite_original', f'-XMP<={packet}', still)
        written_xmp = run('exiftool', '-b', '-XMP', still)
        if not written_xmp.strip():
            raise ConversionError('ExifTool did not write the Motion Photo XMP')
        validate_motion_xmp(written_xmp, video_size, len(gain) if gain else 0)
        if gain:
            replace_gainmap(still, gain)
            probe_ultrahdr(still)
        elif compatibility == 'oneplus':
            add_single_image_mpf(still)
        with still.open('ab') as dest, converted_video.open('rb') as src:
            shutil.copyfileobj(src, dest, length=1024 * 1024)
        if gain:
            # Probe the final file as well: the video must not hide the gain map.
            probe_ultrahdr(still)
        result['hdr_status'] = ('preserved' if result['hdr_preserved'] else
                                'explicit_sdr' if allow_sdr and image_info['FileType'] == 'HEIC' else 'not_detected')
        result['embedded_video_bytes'] = video_size
        if overwrite:
            os.replace(still, output)
        else:
            try:
                os.link(still, output)
            except FileExistsError as exc:
                raise ConversionError(f'Output exists; use --overwrite: {output}') from exc
        return result


def output_for(image):
    return image.with_suffix('.jpg') if image.suffix.lower() in HEIC_EXTS else image


def progress(done, total):
    width = 24
    filled = width * done // total if total else width
    print(f'\r[{"#" * filled}{"-" * (width - filled)}] {done}/{total}', end='', file=sys.stderr, flush=True)


def copy_unmatched(source, output, overwrite, protected):
    if same_file(source, output):
        return False  # Already in the requested output directory.
    if output.is_symlink():
        raise ConversionError(f'Refusing a symlink destination: {output}')
    if any(same_file(output, other) for other in protected):
        raise ConversionError(f'Copy would overwrite an input: {output}')
    if output.exists() and not overwrite:
        raise ConversionError(f'Output exists; use --overwrite: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.live-photo-copy-', dir=output.parent) as work:
        temporary = Path(work) / source.name
        shutil.copy2(source, temporary)
        if overwrite:
            os.replace(temporary, output)
        else:
            os.link(temporary, output)
    return True


def conversion_entry(job, overwrite, protected, allow_sdr, compatibility, video_encoding):
    """Run in either the main process or a spawned pool worker."""
    image, video, output = job
    entry = {'image': str(image), 'video': str(video), 'output': str(output)}
    try:
        details = convert(image, video, output, overwrite, protected,
                          allow_sdr, compatibility, video_encoding)
    except (ConversionError, OSError, ValueError) as exc:
        return {'status': 'failed', **entry, 'error': str(exc)}
    return {'status': 'success', **entry, **details}


def positive_workers(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('workers must be a positive integer')
    if number < 1:
        raise argparse.ArgumentTypeError('workers must be a positive integer')
    return number


def execute(jobs, issues, args, log_dir, copies=()):
    log_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='live_photo_converter_' +
            datetime.now().strftime('%Y%m%d_%H%M%S_'), suffix='.jsonl', dir=log_dir, delete=False) as log:
        log_path = Path(log.name)
        success = 0
        failures = 0
        skips = len(issues)
        copied = copy_failures = already_present = 0
        protected = {p for image, video, _ in jobs for p in (image, video)}
        for issue in issues:
            protected.update(Path(p) for key in ('images', 'videos') for p in issue.get(key, []))
        destinations = defaultdict(list)
        for index, (_, _, output) in enumerate(jobs):
            destinations[output.resolve()].append(index)
        for index, (_, output) in enumerate(copies):
            destinations[output.resolve()].append(('copy', index))
        def record(entry):
            log.write(json.dumps(entry, ensure_ascii=False) + '\n')
            log.flush()
        for issue in issues:
            record({'status': 'skipped', **issue})
        total = len(jobs) + len(issues) + len(copies)
        progress(len(issues), total)
        completed = len(issues)
        pending = []
        for index, (image, video, output) in enumerate(jobs):
            entry = {'image': str(image), 'video': str(video), 'output': str(output)}
            try:
                if len(destinations[output.resolve()]) > 1:
                    raise ConversionError('Multiple pairs target the same output')
                if any(same_file(output, source)
                       for other_index, (other_image, other_video, _) in enumerate(jobs)
                       if other_index != index for source in (other_image, other_video)):
                    raise ConversionError('Output would overwrite an input used by another pair')
            except (ConversionError, OSError, ValueError) as exc:
                failures += 1
                record({'status': 'failed', **entry, 'error': str(exc)})
                completed += 1
                progress(completed, total)
            else:
                pending.append((image, video, output))

        def finish(entry):
            nonlocal success, failures, completed
            success += int(entry['status'] == 'success')
            failures += int(entry['status'] == 'failed')
            record(entry)
            completed += 1
            progress(completed, total)

        options = (args.overwrite, protected, args.allow_sdr, args.compatibility,
                   getattr(args, 'video_encoding', None))
        workers = min(getattr(args, 'workers', 1), len(pending))
        if workers > 1:
            # Spawn avoids inheriting Core Image / codec library state on macOS.
            with ProcessPoolExecutor(max_workers=workers,
                                     mp_context=multiprocessing.get_context('spawn')) as pool:
                futures = {pool.submit(conversion_entry, job, *options): job for job in pending}
                for future in as_completed(futures):
                    try:
                        entry = future.result()
                    except Exception as exc:
                        image, video, output = futures[future]
                        entry = {'status': 'failed', 'image': str(image), 'video': str(video),
                                 'output': str(output), 'error': f'Worker failed: {exc}'}
                    finish(entry)
        else:
            for job in pending:
                finish(conversion_entry(job, *options))
        for index, (source, output) in enumerate(copies):
            entry = {'operation': 'copy-unmatched', 'source': str(source), 'output': str(output)}
            try:
                if len(destinations[output.resolve()]) > 1:
                    raise ConversionError('Multiple operations target the same output')
                did_copy = copy_unmatched(source, output, args.overwrite, protected)
            except (ConversionError, OSError) as exc:
                copy_failures += 1
                record({'status': 'failed', **entry, 'error': str(exc)})
            else:
                copied += int(did_copy)
                already_present += int(not did_copy)
                record({'status': 'copied' if did_copy else 'already-present', **entry})
            progress(len(issues) + len(jobs) + index + 1, total)
        record({'status': 'summary', 'success': success, 'failures': failures,
                'skips': skips, 'total': total,
                'unmatched_copied': copied, 'copy_failures': copy_failures,
                'unmatched_already_present': already_present})
    print(file=sys.stderr)
    print(f'Success: {success}; failures: {failures}; skips: {skips}')
    if getattr(args, 'copy_unmatched', False):
        print(f'Unmatched media copied: {copied}; already present: {already_present}; copy failures: {copy_failures}')
    print(f'Log: {log_path}')
    return 1 if failures or copy_failures else 0


def directory_jobs(source, destination):
    groups = defaultdict(lambda: {'images': [], 'videos': []})
    for path in sorted(source.rglob('*')):
        if not path.is_file():
            continue
        # Exclude a separate output tree nested inside the input tree.
        if destination != source and source in destination.parents and destination in path.parents:
            continue
        suffix = path.suffix.lower()
        category = 'images' if suffix in IMAGE_EXTS else 'videos' if suffix in VIDEO_EXTS else None
        if category:
            groups[(path.parent, path.stem.casefold())][category].append(path)
    jobs, issues = [], []
    for group in groups.values():
        images, videos = group['images'], group['videos']
        if len(images) != 1 or len(videos) != 1:
            issues.append({'images': [str(p) for p in images], 'videos': [str(p) for p in videos],
                           'reason': 'Unmatched media' if not images or not videos else 'Ambiguous matching pair'})
            continue
        image, video = images[0], videos[0]
        output = output_for(destination / image.relative_to(source))
        jobs.append((image, video, output))
    return jobs, issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest='command', required=True)
    batch = subs.add_parser('l2m', help='Convert one or more image/video pairs')
    batch.add_argument('-i', '--img', nargs='+', action='extend', required=True, type=Path)
    batch.add_argument('-v', '--video', nargs='+', action='extend', required=True, type=Path)
    batch.add_argument('-o', '--output', nargs='+', action='extend', type=Path)
    batch.add_argument('--video-encoding', choices=('AVC', 'HEVC', 'AV1'),
                       help='Re-encode video only if its codec differs; default: inherit source codec')
    directory = subs.add_parser('dl2m', help='Recursively convert matching pairs in a directory')
    directory.add_argument('-i', '--input-dir', required=True, type=Path)
    directory.add_argument('-o', '--output-dir', type=Path, help='Default: input directory')
    directory.add_argument('--copy-unmatched', action='store_true',
                           help='Copy unmatched or ambiguous media to the output directory')
    for sub in (batch, directory):
        sub.add_argument('-w', '--workers', type=positive_workers,
                         default=max(1, os.cpu_count() or 1),
                         help='Conversion processes (positive integer; default: CPU count, minimum 1)')
        sub.add_argument('--overwrite', action='store_true', help='Allow replacement of existing output photos')
        sub.add_argument('--allow-sdr', action='store_true', help='Explicitly permit HDR loss during HEIC to JPEG conversion')
        sub.add_argument('--compatibility', choices=('google', 'oneplus'), default='oneplus',
                         help='oneplus adds Oplus recognition markers; default: oneplus')
    args = parser.parse_args(argv)
    copies = []
    if args.command == 'l2m':
        if len(args.img) != len(args.video) or (args.output is not None and len(args.output) != len(args.img)):
            parser.error('--img, --video, and explicit --output lists must have equal lengths')
        images = [p.expanduser().absolute() for p in args.img]
        videos = [p.expanduser().absolute() for p in args.video]
        outputs = ([p.expanduser().absolute() for p in args.output] if args.output else
                   [output_for(p) for p in images])
        jobs, issues = list(zip(images, videos, outputs)), []
        log_dir = Path.cwd() / 'logs'
    else:
        source = args.input_dir.expanduser().resolve()
        if not source.is_dir():
            parser.error(f'Input directory does not exist: {source}')
        destination = args.output_dir.expanduser().resolve() if args.output_dir else source
        jobs, issues = directory_jobs(source, destination)
        if args.copy_unmatched:
            copies = [(Path(path), destination / Path(path).relative_to(source))
                      for issue in issues for key in ('images', 'videos')
                      for path in issue.get(key, [])]
        log_dir = destination / 'logs'
        if not jobs and not issues:
            issues.append({'reason': 'No supported image/video files found', 'directory': str(source)})
    for tool in ('exiftool', 'ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            parser.error(f'{tool} is required on PATH. macOS: brew install exiftool ffmpeg libultrahdr')
    try:
        return execute(jobs, issues, args, log_dir, copies)
    except OSError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nInterrupted; completed outputs and log entries are retained.', file=sys.stderr)
        sys.exit(130)
