"""Unmatched media copying, destination safety, and reporting."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_photo_converter as converter


class UnmatchedCopyTest(unittest.TestCase):
    def test_nested_unmatched_files_are_copied_and_reported(self):
        with tempfile.TemporaryDirectory() as work:
            source, output = Path(work) / 'input', Path(work) / 'output'
            nested = source / 'nested'
            nested.mkdir(parents=True)
            image, video = nested / 'alone.HEIC', nested / 'other.MOV'
            image.write_bytes(b'image')
            video.write_bytes(b'video')
            jobs, issues = converter.directory_jobs(source, output)
            copies = [(Path(p), output / Path(p).relative_to(source))
                      for issue in issues for key in ('images', 'videos') for p in issue[key]]
            args = SimpleNamespace(overwrite=False, copy_unmatched=True,
                                   allow_sdr=False, compatibility='oneplus')
            report = io.StringIO()
            with contextlib.redirect_stdout(report), contextlib.redirect_stderr(io.StringIO()):
                code = converter.execute(jobs, issues, args, output / 'logs', copies)
            self.assertEqual(code, 0)  # Unmatched groups are skips, not failures.
            self.assertEqual((output / 'nested/alone.HEIC').read_bytes(), b'image')
            self.assertEqual((output / 'nested/other.MOV').read_bytes(), b'video')
            self.assertIn('Unmatched media copied: 2', report.getvalue())
            log = next((output / 'logs').glob('*.jsonl'))
            summary = json.loads(log.read_text().splitlines()[-1])
            self.assertEqual(summary['unmatched_copied'], 2)
            self.assertEqual(summary['copy_failures'], 0)
            self.assertEqual(summary['failures'], 0)
            self.assertEqual(summary['skips'], 2)
            entries = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(sum(entry['status'] == 'skipped' for entry in entries), 2)
            self.assertIn('skips: 2', report.getvalue())

            # Re-running without overwrite reports actual copy errors as failures.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(converter.execute(jobs, issues, args, output / 'logs', copies), 1)

            # Skips without copying also finish successfully.
            args.copy_unmatched = False
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(converter.execute(jobs, issues, args, output / 'logs'), 0)

    def test_same_destination_is_already_present_and_existing_output_is_protected(self):
        with tempfile.TemporaryDirectory() as work:
            source, output = Path(work) / 'source.jpg', Path(work) / 'output.jpg'
            source.write_bytes(b'original')
            output.write_bytes(b'existing')
            self.assertFalse(converter.copy_unmatched(source, source, False, {source}))
            with self.assertRaises(converter.ConversionError):
                converter.copy_unmatched(source, output, False, {source})
            self.assertEqual(output.read_bytes(), b'existing')
            self.assertTrue(converter.copy_unmatched(source, output, True, {source}))
            self.assertEqual(output.read_bytes(), b'original')
            with self.assertRaises(converter.ConversionError):
                converter.copy_unmatched(source, output, True, {source, output})


if __name__ == '__main__':
    unittest.main()
