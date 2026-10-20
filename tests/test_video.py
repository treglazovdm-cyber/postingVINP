import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import video


class VideoTests(unittest.TestCase):
    def test_text_cover_supports_russian_and_literal_special_characters(self):
        with tempfile.TemporaryDirectory() as folder:
            result = video.text_cover('Качаешь пресс, а живот всё равно выпирает? 100% пользы: %{n}', Path(folder))
            self.assertTrue(result.read_bytes().startswith(b'\xff\xd8'))
            self.assertLess(result.stat().st_size, 200 * 1024)
            info = json.loads(video.run(['ffprobe','-v','error','-show_streams','-of','json',str(result)]))
            self.assertEqual((info['streams'][0]['width'], info['streams'][0]['height']), (320,180))

    def test_black_intro_gets_visible_thumbnail_and_streaming_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / 'source.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                            'color=c=black:s=160x120:d=1:r=10', '-f', 'lavfi', '-i',
                            'color=c=red:s=160x120:d=2:r=10', '-filter_complex',
                            '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]', '-c:v',
                            'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', str(source)], check=True, capture_output=True)
            original = source.read_bytes()
            ready, thumbnail, fields = video.prepare(source, folder)
            self.assertEqual(fields['duration'], 3)
            self.assertEqual((fields['width'], fields['height']), (160, 120))
            self.assertEqual(fields['supports_streaming'], 'true')
            self.assertLess(thumbnail.stat().st_size, 200 * 1024)
            gray = video.run(['ffmpeg', '-v', 'error', '-i', str(thumbnail), '-frames:v', '1', '-vf', 'scale=32:32', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'])
            self.assertGreater(sum(gray) / len(gray), 30)
            encoded = ready.read_bytes()
            self.assertLess(encoded.index(b'moov'), encoded.index(b'mdat'))
            self.assertEqual(original, source.read_bytes())

    def test_corrupt_file_fails_before_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'invalid.mp4'
            source.write_bytes(b'invalid')
            with self.assertRaises(video.VideoError):
                video.prepare(source, Path(folder))

    def test_incompatible_codec_has_clear_error(self):
        probe = {'streams': [{'codec_type':'video', 'codec_name':'hevc', 'pix_fmt':'yuv420p', 'width':100, 'height':100}], 'format': {'duration':'3'}}
        with patch('video.run', return_value=json.dumps(probe).encode()):
            with self.assertRaisesRegex(video.VideoError, 'H.264'):
                video.prepare(Path('video.mp4'), Path('.'))
