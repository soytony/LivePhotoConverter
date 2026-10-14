# Live Photo Converter

A single-file Python utility that combines a still image and its companion video into a JPEG Motion Photo.

The supported workflow uses **JPEG for the still cover**, preserving HDR with an Ultra HDR gain map. HEIC/HEIF are supported as inputs and automatically converted to JPEG. HEIC motion-photo output is no longer supported.

Motion Photos can use a HEIC/HEIF still cover, but playback support is not widespread. Samsung Gallery and Google Photos are currently the only apps with known support in this project's compatibility scope, and support varies by file structure and app version. [Google Photos community reports](https://support.google.com/photos/thread/271415784/i-could-play-heic-motion-photos-in-photos-including-sound-clip?hl=en) describe playback in both apps. In our OnePlus testing, the original Samsung HEIF Motion Photo played in Google Photos but not Oplus Gallery; our generated HEIC variants were not recognized by either app. For broader compatibility, this project always converts the still cover to JPEG, preserving HDR through a gain map when available.

## Setup

Use Python 3.9 or later. On macOS:

```bash
brew install exiftool ffmpeg libultrahdr
xcode-select --install  # If Swift / Command Line Tools are not already installed
```

HDR conversion from HEIC/HEIF requires macOS 15 or later, Swift, and `libultrahdr`. The Swift helper is embedded in the Python script, compiled once per process in a temporary directory, and cleaned up automatically. No third-party Python packages are required for this workflow.

JPEG inputs without an HDR gain map require only ExifTool, FFmpeg, and FFprobe. Ultra HDR JPEG inputs also require `libultrahdr` for validation. On other platforms, explicit SDR conversion can use ImageMagick (`magick`) or Pillow with `pillow-heif`.

## Convert individual pairs: `l2m`

```bash
python3 live_photo_converter.py l2m \
  -i photo.heic -v photo.mov -o photo_MP.jpg
```

Batch inputs are paired by position:

```bash
python3 live_photo_converter.py l2m \
  -i a.heic b.jpg -v a.mov b.mp4 \
  -o a_MP.jpg b_MP.jpg
```

`--img` and `--video` must contain equal numbers of paths. When `--output` is supplied, provide one output path for every pair. Quote paths containing spaces.

Without `--output`, HEIC/HEIF inputs use the same directory and stem with `.jpg`; JPEG inputs retain their filename. In-place JPEG conversion therefore requires `--overwrite`.

For `l2m`, `--video-encoding AVC`, `HEVC`, or `AV1` selects the video codec for all supplied pairs. By default the source codec is inherited. Matching codecs are copied without re-encoding; differing codecs are re-encoded using FFmpeg's `libx264`, `libx265`, or `libsvtav1`, respectively. Your FFmpeg build must include the requested encoder. Re-encoding is lossy and may take longer, especially for AV1. Logs record both source/output codecs and whether video was re-encoded.

```bash
python3 live_photo_converter.py l2m -i photo.heic -v photo.mov -o photo_MP.jpg --video-encoding AVC
```

## Convert a directory: `dl2m`

```bash
python3 live_photo_converter.py dl2m -i ./photos -o ./motion
```

The script scans recursively for `.heic`, `.heif`, `.jpg`, `.jpeg`, `.mov`, and `.mp4`, matching case-insensitive stems within each folder. It preserves relative subfolders in the output directory and excludes a nested output directory from the scan.

Unmatched and ambiguous groups are logged as skips, one skip per group. Conversion continues with other pairs. A progress bar and final success/failure/skip counts are displayed. Without `--output-dir`, outputs are written beside the inputs.

Add `--copy-unmatched` to copy supported media from unmatched or ambiguous groups to the output directory, retaining original filenames, file contents, and relative folders. This does not copy pairs whose conversion fails. Existing destinations require `--overwrite`; input files remain protected. When input and output directories are the same, these files are counted as already present.

The final report and JSONL summary include unmatched files copied, files already present, and copy failures. Unmatched groups count as skips even when their files are copied successfully. Skips alone do not cause a nonzero exit code; conversion or copy errors do.

```bash
python3 live_photo_converter.py dl2m -i ./photos -o ./motion --copy-unmatched
```

## Options shared by both commands

| Option | Behavior |
| --- | --- |
| `-w N`, `--workers N` | Use up to N conversion processes for multiple pairs; positive integer, default `max(1, cpu_count)`. A single pair runs directly. |
| `--overwrite` | Allow replacement of existing destination photos. Videos and other input photos in the batch remain protected. |
| `--compatibility oneplus` | Default. Write modern Motion Photo metadata plus Oplus EXIF/XMP recognition markers. |
| `--compatibility google` | Write generic modern Motion Photo metadata without Oplus markers. |
| `--allow-sdr` | Explicitly export HEIC/HEIF as SDR JPEG, discarding its HDR rendition. |

Explicit output filenames must end in `.jpg` or `.jpeg`. Existing Motion Photos are rejected to avoid appending a second video.

Supported input image extensions are used for scanning, but detected file contents determine decoding. For example, HEIC data named `.JPG` is converted to JPEG rather than rejected or copied unchanged. Logs record the detected source format and any extension mismatch. Unsupported file contents are still rejected.

For example, `dl2m -i ./photos -o ./motion -w 4` converts up to four pairs concurrently using a process pool. Progress and JSONL logs are written by the main process as conversions finish, so log entries may appear in completion order. Unmatched copies run after conversions. Each worker compiles its HDR helper once when needed. More workers can increase memory and CPU use because FFmpeg also uses threads internally.

## HDR, video, and safety

For HDR HEIC/HEIF inputs, Core Image reconstructs the HDR rendition and generates a JPEG gain map. The script writes Adobe gain-map metadata alongside ISO metadata, maintains the JPEG MPF index, and validates the resulting image with Google's Ultra HDR reference codec. HDR backend failures are reported instead of silently falling back to SDR.

The primary video is remuxed into MP4 without re-encoding by default. `l2m --video-encoding` can request a different codec. The first audio track is retained, converting it to AAC when needed. Apple auxiliary video and metadata tracks are omitted. The still-frame timestamp is recovered from Apple's timed metadata when available; otherwise the video midpoint is used.

**Video HDR conversion is not implemented.** The script does not reconstruct HDR from Apple auxiliary tracks or guarantee end-to-end HDR video preservation, including when changing codecs.

Outputs are built in temporary files and published atomically. Existing files require `--overwrite`; sources are unchanged unless an explicitly permitted output targets that source photo. Oplus mode replaces the output's EXIF UserComment with its recognition marker, while retaining the original camera Make/Model.

## Logs and verification

`l2m` logs are written under `./logs/`. Directory conversion logs are written under `<output-dir>/logs/`. Each JSONL log records the input/output paths, conversion details or explicit error, and a summary. Earlier investigation logs are preserved under `logs/history/`.

Exit codes: `0` when there are no conversion or copy failures (skips are allowed), `1` for conversion or copy failures, `2` for argument/setup errors, and `130` for interruption.

Run the regression tests:

```bash
python3 -m unittest discover -s tests -v
```

The JPEG HDR workflow was confirmed working on the user's OnePlus. Recognition may differ across other gallery apps and devices; file structure checks alone are not a playback guarantee.

Reference: 
 - [Android Motion Photo format](https://developer.android.com/media/platform/motion-photo-format). 
 - Original inspiration: [combine_photo_video.sh](https://gist.github.com/jkitching/3fa5a0c238fa825278db83acd05d7742).
